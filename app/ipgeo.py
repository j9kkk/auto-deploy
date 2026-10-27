"""来源 IP 的识别与归属地解析。

操作日志里的「来源 IP」在两种情况下会失真，必须分开处理：

* **内网 / 保留地址**：容器里看到的通常是 Docker 网桥网关（如 172.23.0.1），
  不是真实客户端。这类地址不能送外部接口查询，只能在本地标注它是哪一类
  地址，并提示运维如何拿到真实来源。
* **公网地址**：交给在线归属地接口解析成「国家 省份 城市 + 运营商」。

查询会主动联网，因此受「来源 IP 归属地解析」开关控制；关闭后不发起任何
外部请求，历史记录保留已有结果。解析结果同时写入审计行，重复 IP 走进程内
缓存，避免每次刷新都打接口。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("autodeploy.ipgeo")

# 成功结果缓存 24 小时，失败缓存 30 分钟：接口临时不可用时避免反复重试，
# 恢复后又能较快补上。
SUCCESS_TTL_SECONDS = 24 * 3600
FAILURE_TTL_SECONDS = 1800
REQUEST_TIMEOUT_SECONDS = 5.0
# 免费接口对频率敏感，串行查询并保持约 1 秒间隔。
RESOLVE_INTERVAL_SECONDS = 1.0
MAX_LABEL_LENGTH = 120


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把字符串解析成 IP 对象；非法输入返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    # X-Forwarded-For 可能带端口（1.2.3.4:5678）或 IPv6 方括号。
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_public(value: str) -> bool:
    """是否公网地址——只有公网地址才值得送去外部接口。"""
    address = parse_ip(value)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return bool(address.is_global)


def _default_gateway() -> str | None:
    """Linux 下读 /proc/net/route 得到默认网关，用于识别 Docker 网桥。"""
    try:
        lines = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[1] != "00000000":
            continue
        try:
            # 路由表里的地址是小端十六进制。
            raw = bytes.fromhex(parts[2])
        except ValueError:
            continue
        return ".".join(str(octet) for octet in reversed(raw))
    return None


def _inside_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def local_label(value: str) -> str:
    """不对网络查询就能确定的标注；公网地址返回空串（需要联网解析）。"""
    if not (value or "").strip():
        return ""
    address = parse_ip(value)
    if address is None:
        return "地址无法识别"
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "本机回环"
    if address.is_link_local:
        return "链路本地地址"
    if address.is_multicast:
        return "组播地址"
    if address.is_private:
        gateway = _default_gateway()
        if gateway and str(address) == gateway and _inside_container():
            return "Docker 网桥网关"
        return "内网地址"
    if address.is_reserved:
        return "保留地址"
    # 既非公网也非上面几类（0.0.0.0、文档地址段等）：照样标注，
    # 否则这些行会永远留在待解析队列里。
    if not address.is_global:
        return "非公网地址"
    return ""


_LOCAL_LABEL_CACHE: dict[str, str] = {}
_LOCAL_LABEL_STAMP = 0.0
_LOCAL_LABEL_TTL = 60.0


def local_label_cached(value: str) -> str:
    """``local_label`` 的带缓存版本。

    ``/proc/net/route`` 每次读都有系统调用开销，而写审计日志是请求主路径，
    因此网关判断结果缓存一分钟——网关在服务生命周期内几乎不会变。
    """
    global _LOCAL_LABEL_STAMP
    text = (value or "").strip()
    if not text:
        return ""
    now = time.monotonic()
    if not _LOCAL_LABEL_CACHE or now - _LOCAL_LABEL_STAMP > _LOCAL_LABEL_TTL:
        _LOCAL_LABEL_CACHE.clear()
        _LOCAL_LABEL_STAMP = now
    if text not in _LOCAL_LABEL_CACHE:
        _LOCAL_LABEL_CACHE[text] = local_label(text)
    return _LOCAL_LABEL_CACHE[text]


# ---------------------------------------------------------------------------
# 在线归属地查询
# ---------------------------------------------------------------------------

def _request_json(url: str, *, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "AutoDeploy", "Accept": "application/json"}
    )
    from . import config

    settings = config.load_settings()
    proxy = config.proxy_env(settings)
    if not proxy:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    proxy_url = proxy.get("https_proxy") or proxy.get("http_proxy") or ""
    host = urllib.parse.urlsplit(url).hostname or ""
    no_proxy = proxy.get("no_proxy") or ""
    if not proxy_url or config.no_proxy_bypassed(host, no_proxy):
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def _join(parts: list[str]) -> str:
    """拼接地点片段并去掉相邻重复（「北京 北京市」保留长的那个）。"""
    out: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text or text in out:
            continue
        if any(text in existing or existing in text for existing in out[-1:]):
            # 后一段更具体（北京市 → 北京）或更笼统时，保留更具体的那段。
            if len(text) > len(out[-1]):
                out[-1] = text
            continue
        out.append(text)
    return " ".join(out)


def _clean_isp(text: str) -> str:
    """清理运营商名里的通用前缀（如「IDC, China Telecom」的前半段）。

    免费接口会返回 ``IDC, China Telecommunications Corporation`` 这类
    拼接值，前半段对用户没有信息量，只剩噪声。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    generic = {"idc", "isp", "as", "cdn", "datacenter", "data center"}
    segments = [part.strip() for part in raw.split(",") if part.strip()]
    kept = [
        part for part in segments
        if not (part.isascii() and part.lower() in generic)
    ]
    return ", ".join(kept or segments)


def _from_ipwho(payload: dict[str, Any]) -> str:
    if not payload.get("success"):
        return ""
    connection = payload.get("connection") or {}
    place = _join([
        str(payload.get("country") or ""),
        str(payload.get("region") or ""),
        str(payload.get("city") or ""),
    ])
    isp = _clean_isp(str(connection.get("isp") or connection.get("org") or ""))
    if place and isp:
        return f"{place} · {isp}"
    return place or isp


def _from_ipapi(payload: dict[str, Any]) -> str:
    if payload.get("status") != "success":
        return ""
    place = _join([
        str(payload.get("country") or ""),
        str(payload.get("regionName") or ""),
        str(payload.get("city") or ""),
    ])
    isp = _clean_isp(str(payload.get("isp") or ""))
    if place and isp:
        return f"{place} · {isp}"
    return place or isp


def lookup_online(value: str) -> str:
    """查询公网 IP 的归属地；全部接口失败返回空串。"""
    ip = (value or "").strip()
    if not ip:
        return ""
    quoted = urllib.parse.quote(ip, safe="")
    providers = (
        (f"https://ipwho.is/{quoted}?lang=zh-CN", _from_ipwho),
        # 备用接口只有 HTTP 版本（免费额度不支持 HTTPS），仅主接口失败时使用。
        (
            "http://ip-api.com/json/" + quoted
            + "?lang=zh-CN&fields=status,country,regionName,city,isp",
            _from_ipapi,
        ),
    )
    for url, parser in providers:
        try:
            payload = _request_json(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("归属地接口不可用 %s: %s", urllib.parse.urlsplit(url).hostname, exc)
            continue
        if not isinstance(payload, dict):
            continue
        label = _join([parser(payload)])[:MAX_LABEL_LENGTH]
        if label:
            return label
    return ""


class IpLocationResolver:
    """后台把审计行里缺失的归属地补齐。

    写审计时只做零成本标注（内网 / 网关 / 本机）；公网地址留给这个线程，
    避免请求主路径被外部接口的延迟绑住。
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="autodeploy-ipgeo", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    # -- 查询与缓存 -------------------------------------------------------
    def _cached(self, ip: str) -> str | None:
        with self._lock:
            entry = self._cache.get(ip)
        if entry is None:
            return None
        stamp, label = entry
        ttl = SUCCESS_TTL_SECONDS if label else FAILURE_TTL_SECONDS
        if time.monotonic() - stamp > ttl:
            with self._lock:
                self._cache.pop(ip, None)
            return None
        return label

    def _remember(self, ip: str, label: str) -> None:
        with self._lock:
            self._cache[ip] = (time.monotonic(), label)

    def resolve(self, ip: str) -> str:
        """返回归属地文本；无需解析或解析失败返回空串。"""
        text = (ip or "").strip()
        if not text:
            return ""
        label = local_label_cached(text)
        if label:
            return label
        if not is_public(text):
            return ""
        cached = self._cached(text)
        if cached is not None:
            return cached
        label = lookup_online(text)[:MAX_LABEL_LENGTH]
        self._remember(text, label)
        return label

    # -- 主循环 -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=60)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._drain()
            except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
                logger.exception("来源 IP 归属地解析失败")

    def _drain(self) -> None:
        from . import config

        if not getattr(config.load_settings(), "ip_geo_enabled", True):
            return
        while not self._stop.is_set():
            rows = self.store.audit.pending_locations(limit=20)
            if not rows:
                return
            progressed = False
            for row in rows:
                if self._stop.is_set():
                    return
                ip = str(row.get("ip") or "")
                label = self.resolve(ip)
                if not label:
                    # 记下失败，避免同一批行被反复选中。
                    self._remember(ip, "")
                    continue
                self.store.audit.set_location(int(row["id"]), label)
                progressed = True
                self._stop.wait(RESOLVE_INTERVAL_SECONDS)
            if not progressed:
                return
