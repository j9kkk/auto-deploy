"""对外访问地址的推导。

Webhook 触发地址要交给外部系统（GitHub、Gitee、curl）访问，必须是对外可
达的 scheme + host，而不是服务自己看到的监听地址。推导顺序：

1. 设置的「外网访问地址」（``settings.base_url``）—— 显式配置最可靠，尤其
   在 TLS 终止发生在反代、或反代未透传 ``X-Forwarded-*`` 的时候；
2. 反代转发头 ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` —— 仅在开启
   「信任反向代理头」时采纳，否则任何客户端都能伪造地址；
3. 请求自身的 scheme + Host —— 直连场景下的正确值。

模块内不放 ``Request`` 依赖，只用鸭子类型读取 ``headers`` / ``url``，方便
在自检脚本里用轻量对象覆盖各分支。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from . import config

_ALLOWED_SCHEMES = ("http", "https")


def normalize_base_url(value: Any) -> tuple[str, str]:
    """规范化用户填写的外网访问地址，返回 ``(地址, 错误信息)``。

    空字符串表示「跟随请求自动推断」（错误信息恒为空）。规范化只做去除
    首尾空白、统一小写 scheme、去掉结尾的 ``/``，不擅自补 scheme：写成
    ``example.com`` 却悄悄当成 https 会让触发地址在错误的地方静默生效。
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    lowered = text.lower()
    if not lowered.startswith(("http://", "https://")):
        return "", "外网访问地址必须以 http:// 或 https:// 开头"
    parts = urlsplit(text)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        return "", "外网访问地址缺少有效的主机名"
    if parts.query or parts.fragment:
        return "", "外网访问地址不能包含查询参数或片段"
    if parts.username or parts.password:
        return "", "外网访问地址不能包含用户名或密码"
    path = parts.path.rstrip("/")
    if ".." in path:
        return "", "外网访问地址的路径不能包含 .."
    return f"{parts.scheme.lower()}://{parts.netloc}{path}", ""


def _first_header(value: str) -> str:
    """多级代理会追加逗号分隔的值，取最左侧（最接近客户端）的一个。"""
    return (value or "").split(",")[0].strip()


def external_base_url(request: Any = None, settings: Any = None) -> str:
    """推导对外访问的基地址（不含结尾 ``/``，无可用信息时返回空串）。"""
    settings = settings or config.load_settings()
    configured, _ = normalize_base_url(getattr(settings, "base_url", ""))
    if configured:
        return configured
    if request is None:
        return ""

    headers = getattr(request, "headers", None) or {}
    url = getattr(request, "url", None)
    scheme = str(getattr(url, "scheme", "") or "")
    host = str(headers.get("host", "") or getattr(url, "netloc", "") or "")

    if getattr(settings, "trust_proxy_headers", False):
        forwarded_proto = _first_header(headers.get("x-forwarded-proto", "")).lower()
        if forwarded_proto in _ALLOWED_SCHEMES:
            scheme = forwarded_proto
        forwarded_host = _first_header(headers.get("x-forwarded-host", ""))
        if forwarded_host:
            host = forwarded_host

    if not host:
        return ""
    return f"{scheme or 'http'}://{host}"
