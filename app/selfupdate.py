"""受控自更新：只读验证 systemd，备份代码，启动后确认版本。

Docker 形态的升级由独立执行器容器完成（见 upgrade_exec.py）：面板只负责
预检、准入与预拉镜像，切换与验证发生在被替换容器之外，失败可自动恢复。
"""

from __future__ import annotations

import ast
import hashlib
import uuid
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import config
from .executor import run_command

UPDATE_REPO_DEFAULT = "https://github.com/j9kkk/auto-deploy.git"
STATE_FILE_NAME = "self-update-state.json"
HISTORY_FILE_NAME = "self-update-history.json"
BACKUP_DIR_NAME = "self-update-backups"
PLAN_FILE_NAME = "upgrade-plan.json"
MARKER_FILE_NAME = "upgrade-started.json"
CHECK_TTL_SECONDS = 600
RESTART_TIMEOUT = 120
# 独立执行器的整体预算（拉镜像已在面板完成，执行器只做切换/重建/验证）。
EXECUTOR_TIMEOUT = 2100
STATE_SCHEMA_VERSION = 2
HISTORY_LIMIT = 20
PROCESS_BOOT_ID = uuid.uuid4().hex
EXECUTOR_LABEL = "com.autodeploy.upgrade-executor"

# 与部署脚本同一套产物：这些路径构成一次完整的程序更新。
UPDATE_ITEMS = ("app", "web", "requirements.txt", "run.sh")

# 裸机（systemd）链路的活跃阶段。
BARE_ACTIVE_STAGES = ("queued", "checking", "downloading", "backing_up", "applying", "dependencies",
                      "restarting")
# Docker 委托链路：面板侧（queued→预拉镜像→执行器运行中）与执行器侧（准备→切换→验证）。
WEB_DELEGATE_STAGES = ("queued", "pulling_image", "executing")
EXECUTOR_ACTIVE_STAGES = ("preparing", "prepared", "switching", "verifying")
ACTIVE_STAGES = BARE_ACTIVE_STAGES + WEB_DELEGATE_STAGES + EXECUTOR_ACTIVE_STAGES + ("recreating",)
# 结果不确定、需要用户处置的阻塞阶段：不算活跃操作，但禁止发起新更新/新任务。
BLOCKED_STAGES = ("attention", "recovery_required")
# 终态：监控线程与启动确认看到这些阶段即停止干预。
TERMINAL_STAGES = ("done", "failed", "unverified", "rolled_back", "attention", "recovery_required")

# Docker 镜像引用：仓库（可含 / 与 .）+ 可选 tag。用于 .env 的镜像切换与回退目标，
# 防止任意字符串注入 compose 命令。
IMAGE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}(:[A-Za-z0-9][A-Za-z0-9._-]{0,127})?")

# Docker 形态的环境变量名（与 bootstrap.sh / docker-compose.yml 约定一致）。
IMAGE_TAG_ENV_KEY = "AUTODEPLOY_IMAGE_TAG"
IMAGE_ENV_KEY = "AUTODEPLOY_IMAGE"
DEFAULT_IMAGE = "ghcr.io/j9kkk/auto-deploy"
DEFAULT_IMAGE_TAG = "latest"

DOCKER_CHECK_CACHE_SECONDS = 30
_docker_check_cache: tuple[float, bool] | None = None


def in_container() -> bool:
    """是否运行在容器里（Docker/K8s 等）。

    标准探测：/.dockerenv 存在，或 cgroup 内容出现容器运行时标识。
    这是事实判断；「能不能操作 docker」另由 docker_available() 判定。
    """
    try:
        if Path("/.dockerenv").exists():
            return True
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        return any(k in cgroup for k in ("docker", "containerd", "kubepods", "podman"))
    except OSError:
        return False


def docker_available() -> bool:
    """能否操作宿主 docker：判据是实际能跑通 `docker info`，而不是在不在容器里。

    容器挂了 socket 就完全能做（与宿主等价）；宿主没装 docker 反而不能。
    结果缓存 30 秒：状态接口被前端轮询，每次都探测会拖慢响应。
    """
    global _docker_check_cache
    now = time.monotonic()
    if _docker_check_cache is not None and now - _docker_check_cache[0] < DOCKER_CHECK_CACHE_SECONDS:
        return _docker_check_cache[1]
    ok = run_command(["docker", "info"], timeout=15).ok
    _docker_check_cache = (now, ok)
    return ok


def docker_run_mode() -> str:
    """部署形态：docker（容器内且能操作 docker）、docker_no_socket、systemd、unknown。

    systemd 判定复用 _restart_plan（要求真实单元主进程 + Restart=always），
    与裸机升级的准入条件一致。
    """
    if in_container():
        return "docker" if docker_available() else "docker_no_socket"
    strategy, _ = SelfUpdateManager._restart_plan_static()
    return "systemd" if strategy == "self-exit" else "unknown"


# ---------------------------------------------------------------------------
# 面板与升级执行器共享的底层助手：两者写同一份状态/历史文件，必须用同一套
# 围栏规则，避免旧执行器覆盖新操作、或并发写入互相踩踏。
# ---------------------------------------------------------------------------

def state_file_path() -> Path:
    return config.DATA_DIR / STATE_FILE_NAME


def history_file_path() -> Path:
    return config.DATA_DIR / HISTORY_FILE_NAME


def plan_file_path() -> Path:
    return config.DATA_DIR / PLAN_FILE_NAME


def marker_file_path() -> Path:
    return config.DATA_DIR / MARKER_FILE_NAME


def load_state_file() -> dict[str, Any]:
    """只读加载状态文件；不存在或损坏返回空字典（不代表没有操作在进行）。"""
    try:
        data = json.loads(state_file_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state_file(state: dict[str, Any]) -> bool:
    """带围栏的原子写入：同一操作按 generation 递增，异操作不得覆盖未终态记录。

    返回 False 表示写入被拒绝（调用方必须停止推进，绝不能继续假装自己在
    管理这次操作）。
    """
    path = state_file_path()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_state_file()
    if existing:
        old_op = str(existing.get("operation_id") or "")
        new_op = str(state.get("operation_id") or "")
        blocked_now = (existing.get("stage") in ACTIVE_STAGES
                       or existing.get("stage") in BLOCKED_STAGES)
        if old_op and new_op and old_op != new_op and blocked_now:
            # 例外：人工处置（attention/recovery_required）下允许"恢复到升级前镜像"
            # 的回退操作接管状态——这是用户在界面明确选择的恢复路径。
            recovery_takeover = (existing.get("stage") in BLOCKED_STAGES
                                 and state.get("operation") == "rollback"
                                 and str(state.get("to_image_ref") or "") == str(existing.get("from_image_ref") or ""))
            if not recovery_takeover:
                return False
        old_gen = existing.get("generation")
        new_gen = state.get("generation")
        # generation 只防"同一操作的旧快照回写"。操作内正常推进永远基于最新
        # 读回的状态（state() 每次从文件解析），但测试与历史调用方可能持有
        # 旧 dict：同操作且文件代数更高时视为落后快照，先对齐再允许写入——
        # 拒绝会卡死整个推进链（真实事故：确认后回写被拒，状态永远停在旧阶段）。
        if old_op and old_op == new_op:
            if isinstance(old_gen, int) and isinstance(new_gen, int):
                if new_gen > old_gen:
                    # state 声明了比文件更高的代数：旧执行器的未来写入，拒绝。
                    return False
                # state 落后于文件（基于旧快照推进）：对齐后允许写入。
    state["generation"] = int(state.get("generation") or 0) + 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True


def log_line(state: dict[str, Any], message: str) -> None:
    state.setdefault("log", []).append(f"[{time.strftime('%H:%M:%S')}] {message}")
    state["log"] = state["log"][-200:]


def append_history_entry(state: dict[str, Any]) -> None:
    """把一次操作的结果追加到历史；同一 operation_id 只保留最新一条。"""
    operation_id = str(state.get("operation_id") or "")
    if not operation_id:
        return
    entry = {
        "operation_id": operation_id,
        "operation": str(state.get("operation") or "update"),
        "stage": str(state.get("stage") or ""),
        "target_version": normalize_tag(str(state.get("target_version") or "")),
        "previous_version": normalize_tag(str(state.get("previous_version") or "")),
        "current_version": normalize_tag(str(state.get("version") or __version__)),
        "backup_version": normalize_tag(str(state.get("backup_version") or state.get("previous_version") or "")),
        # Docker 形态：升级前后的完整镜像引用（含 tag），供界面按 tag 回退。
        # 新状态字段是 *_image_ref；from_image/to_image 保留兼容旧记录。
        "from_image": str(state.get("from_image_ref") or state.get("from_image") or ""),
        "to_image": str(state.get("to_image_ref") or state.get("to_image") or ""),
        "started_at": state.get("started_at") or 0,
        "finished_at": time.time(),
        "error": str(state.get("error") or ""),
        "log": list(state.get("log") or [])[-200:],
    }
    entries = [item for item in _load_history_entries() if item.get("operation_id") != operation_id]
    entries.insert(0, entry)
    payload = {"entries": entries[:HISTORY_LIMIT]}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = history_file_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, history_file_path())


def _load_history_entries() -> list[dict[str, Any]]:
    try:
        data = json.loads(history_file_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def parse_image_ref(ref: str) -> tuple[str, str, str] | None:
    """解析镜像引用为 (repo, tag, digest)；非法返回 None。

    支持带端口的私有 registry（localhost:5000/app）与 digest 引用
    （repo@sha256:...）。最后一个冒号只有出现在最后一个斜杠之后才是 tag
    分隔符，否则属于 registry 端口。
    """
    text = (ref or "").strip()
    if not text or len(text) > 300 or re.search(r"\s", text):
        return None
    digest = ""
    if "@" in text:
        text, _, digest = text.rpartition("@")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            return None
        if not text:
            return None
    last_slash = text.rfind("/")
    colon = text.rfind(":")
    tag = ""
    if colon > last_slash:
        repo, tag = text[:colon], text[colon + 1:]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag or ""):
            return None
    else:
        repo = text
    if (not repo or len(repo) > 250
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", repo)
            or repo.startswith("/") or repo.endswith("/") or "//" in repo):
        return None
    return repo, tag, digest


def repo_of_image_ref(ref: str) -> str:
    parsed = parse_image_ref(ref)
    return parsed[0] if parsed else ""


def sanitize_url(url: str) -> str:
    """去掉 URL 中的 userinfo（可能内嵌 Git 凭据），其余部分原样保留。"""
    try:
        parts = urllib.parse.urlsplit(url or "")
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return url or ""


# compose 子进程使用清洗后的环境：镜像自带 AUTODEPLOY_PORT 等默认值会覆盖
# 安装目录 .env 的插值（shell 环境优先级高于 .env），导致自定义端口在升级后
# 被改回默认——真实事故。这里只保留运行所必需与代理相关变量。
_COMPOSE_ENV_ALLOW = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "PYTHONUNBUFFERED", "TMPDIR", "TERM",
})


def clean_compose_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in _COMPOSE_ENV_ALLOW or key.lower() in ("http_proxy", "https_proxy", "no_proxy")
    }
    env.setdefault("HOME", "/root")
    return env


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def rewrite_env_content(content: str, updates: dict[str, str]) -> str:
    """改写 .env 文本中的指定键（保持其余行、注释与顺序），缺键则追加。"""
    wanted = dict(updates)
    out: list[str] = []
    seen: set[str] = set()
    for line in (content or "").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return ("\n".join(out) + "\n") if out else ""


def write_env_atomic(env_path: Path, content: str, mode: int) -> None:
    """安全原子写入 .env：随机独占临时文件 + fsync + replace + 目录 fsync。

    拒绝软链（固定名临时文件历史上可被预置软链劫持写入目标）；临时文件
    建在同目录保证 os.replace 原子性。
    """
    if env_path.is_symlink() or env_path.parent.is_symlink():
        raise RuntimeError(f"{env_path} 是软链，拒绝写入")
    fd, tmp_name = tempfile.mkstemp(prefix=".env.tmp-upgrade-", dir=str(env_path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, env_path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(env_path.parent)


def read_env_snapshot(env_path: Path) -> dict[str, Any]:
    """切换前抓取 .env 快照（内容 + 权限位 + 指纹），用于失败后恢复。"""
    try:
        content = env_path.read_text(encoding="utf-8")
        mode = env_path.stat().st_mode & 0o777
    except OSError as exc:
        raise RuntimeError(f"无法读取 {env_path}：{exc}") from exc
    return {
        "content": content,
        "mode": mode,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def save_env_snapshot(operation_id: str, snapshot: dict[str, Any]) -> Path:
    """快照落盘到数据目录（含敏感内容，0600，绝不进日志）。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / f"upgrade-{operation_id}.env.snapshot"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    return path


def load_env_snapshot(operation_id: str) -> dict[str, Any] | None:
    path = config.DATA_DIR / f"upgrade-{operation_id}.env.snapshot"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def drop_env_snapshot(operation_id: str) -> None:
    try:
        (config.DATA_DIR / f"upgrade-{operation_id}.env.snapshot").unlink()
    except OSError:
        pass


def restore_env_snapshot(env_path: Path, snapshot: dict[str, Any], expected_sha: str) -> None:
    """恢复 .env 快照；当前内容与切换时不符（可能被人工改过）则拒绝覆盖。"""
    if env_path.is_symlink():
        raise RuntimeError(".env 是软链，拒绝自动恢复")
    try:
        current = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取 {env_path}：{exc}") from exc
    if expected_sha:
        current_sha = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_sha != expected_sha:
            raise RuntimeError("当前 .env 与切换后的内容不一致（可能被人工修改），拒绝覆盖；请人工处理")
    write_env_atomic(env_path, str(snapshot.get("content") or ""), int(snapshot.get("mode") or 0o600))


def compare_versions(a: str, b: str) -> int:
    """比较两个版本号，返回 -1/0/1。无法解析的段落按字符串比较。"""
    def parts(v: str) -> list[Any]:
        cleaned = (v or "").strip().lstrip("vV")
        chunks: list[Any] = []
        for chunk in cleaned.split("."):
            if chunk.isdigit():
                chunks.append(int(chunk))
            else:
                # 预发布后缀（如 1.2.0-rc1）排在同号正式版之前。
                match = re.match(r"(\d+)(.*)", chunk)
                if match:
                    chunks.append(int(match.group(1)))
                    if match.group(2):
                        chunks.append(match.group(2))
                else:
                    chunks.append(chunk)
        return chunks

    pa, pb = parts(a), parts(b)
    for index in range(max(len(pa), len(pb))):
        va = pa[index] if index < len(pa) else 0
        vb = pb[index] if index < len(pb) else 0
        if va == vb:
            continue
        # int 与 str 混合比较时统一转字符串，避免 TypeError。
        if isinstance(va, int) != isinstance(vb, int):
            va, vb = str(va), str(vb)
        return -1 if va < vb else 1
    return 0


def normalize_tag(tag: str) -> str:
    return (tag or "").strip().lstrip("vV")


@dataclass
class UpdateCheck:
    current: str
    latest: str
    update_available: bool
    checked_at: str
    published_at: str = ""
    notes_url: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "checked_at": self.checked_at,
            "published_at": self.published_at,
            "notes_url": self.notes_url,
            "error": self.error,
            "ok": not self.error,
        }


def _tmp_root() -> Path:
    """A writable directory for throwaway files, even before the first deploy."""
    try:
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        return config.TMP_DIR
    except OSError:
        return Path(tempfile.gettempdir())


def _dir_size(path: Path) -> int:
    """Total bytes of a directory tree, ignoring anything unreadable."""
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _proxy_bypassed(host: str, no_proxy: str) -> bool:
    """``no_proxy`` 是否覆盖该主机。

    ``urllib`` 的 ``proxy_bypass`` 只读进程环境变量，而这里的代理来自应用设置，
    因此必须自己按 ``no_proxy`` 判断，否则内网更新源会被强行推过外网代理。
    """
    return config.no_proxy_bypassed(host, no_proxy)


def urlopen_with_proxy(
    request: urllib.request.Request,
    proxy: dict[str, str] | None,
    *,
    timeout: float,
) -> Any:
    """按应用设置打开 URL：显式使用配置的代理，并尊重 no_proxy。

    默认的 ``urllib.request.urlopen`` 只看进程环境变量，因此「设置页里配好的
    代理」对更新检查完全不起作用——在必须走代理才能访问 GitHub 的服务器上，
    Releases API 这条主路径会直接失败。这里把代理显式装进 opener。
    """
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    url = proxy.get("https_proxy") or proxy.get("http_proxy") or ""
    no_proxy = proxy.get("no_proxy") or proxy.get("NO_PROXY") or ""
    host = urllib.parse.urlsplit(request.full_url).hostname or ""
    if not url or _proxy_bypassed(host, no_proxy):
        return urllib.request.urlopen(request, timeout=timeout)
    # socks5 代理 urllib 原生不支持，会直接报错并回退到 git 协议查询；
    # 这里不做特殊处理，避免引入标准库之外的依赖。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    return opener.open(request, timeout=timeout)


class SelfUpdateManager:
    """内置自我更新的状态机与执行器。"""

    def __init__(self, store: Any, gate=None) -> None:
        self.store = store
        self._lock = gate or threading.RLock()
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._check_cache: tuple[float, UpdateCheck] | None = None

    def _urlopen(self, request: urllib.request.Request, proxy: dict[str, str] | None, *, timeout: float) -> Any:
        return urlopen_with_proxy(request, proxy, timeout=timeout)

    # ------------------------------------------------------------- 状态
    @property
    def state_path(self) -> Path:
        return config.DATA_DIR / STATE_FILE_NAME

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._read_state()

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("stage", "idle")
                data.setdefault("log", [])
                changed = False
                if "schema_version" not in data:
                    data["schema_version"] = STATE_SCHEMA_VERSION
                    changed = True
                    # v1.3.6 已有进程确认字段，但尚未写入格式版本。
                    modern = any(key in data for key in ("operation_id", "before_boot_id", "expected_version"))
                    if not modern and data["stage"] != "idle":
                        data["legacy_stage"] = data["stage"]
                        data["notice"] = "此记录来自旧版更新机制，缺少进程确认信息；当前运行版本不代表该次操作已确认成功。可继续检查更新，原日志已保留。"
                        if data["stage"] in (*ACTIVE_STAGES, "done"):
                            data["stage"] = "unverified"
                if data.get("schema_version") != STATE_SCHEMA_VERSION:
                    return {**data, "stage": "unverified", "notice": "无法识别更新记录格式，未确认操作成功；原记录保持不变。"}
                # 重启确认期限属于「进程自我退出/旧版容器内重建」两类操作；
                # 委托执行器的操作（restart=docker-executor）由执行器与监控线程
                # 给出终态，不在此判超时。旧版记录没有 restart 字段，同样按
                # 自我退出处理（历史上只有这两类会停在 restarting）。
                if (data.get("stage") == "restarting"
                        and data.get("restart", "self-exit") != "docker-executor"):
                    deadline = data.get("restart_deadline")
                    valid_deadline = (type(deadline) in (int, float) and math.isfinite(deadline) and deadline > 0)
                    error = ""
                    if not valid_deadline:
                        error = "重启确认记录缺少有效期限，未确认操作成功；请检查服务"
                    elif time.time() > deadline:
                        error = "重启确认超时；未确认目标版本运行，请检查服务"
                    if error:
                        data.update(stage="failed", error=error)
                        self._log(data, error)
                        changed = True
                if changed:
                    self._save_state(data)
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"stage": "idle", "log": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            if not save_state_file(state):
                self._log(state, "状态写入被拒绝：已存在更新的操作记录，本线程停止推进")

    def _write_state(self, state: dict[str, Any]) -> None:
        # 保留旧入口：测试与兼容代码直接落盘时也走同一套围栏规则。
        save_state_file(state)

    def _log(self, state: dict[str, Any], message: str) -> None:
        log_line(state, message)

    # ------------------------------------------------------------- 历史
    @property
    def history_path(self) -> Path:
        return history_file_path()

    def history(self) -> list[dict[str, Any]]:
        """历次更新/回滚记录（新→旧）。

        每次操作结束（成功、失败或确认）时落一条，供界面展示「每次升级的日志」
        并按版本回滚。记录只保留可展示字段与日志，不含路径或代币。
        """
        return _load_history_entries()

    def _append_history(self, state: dict[str, Any]) -> None:
        append_history_entry(state)

    @property
    def backups_dir(self) -> Path:
        return config.DATA_DIR / BACKUP_DIR_NAME

    def _check_backup_root(self) -> None:
        if self.backups_dir.is_symlink() or (self.backups_dir.exists() and not self.backups_dir.is_dir()):
            raise RuntimeError("备份目录不安全或类型错误，尚未修改程序文件")

    def _validate_backup(self, backup: Path) -> str:
        self._check_backup_root()
        if (backup.is_symlink() or not backup.is_dir()
                or backup.resolve().parent != self.backups_dir.resolve()):
            raise RuntimeError("备份路径不安全")
        marker = backup / "complete.json"
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError("备份缺少有效完成标记，无法确认备份完整性")
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            version = metadata.get("version") if isinstance(metadata, dict) else None
            if not isinstance(version, str) or not version.strip():
                raise ValueError("version")
            return self._validate_tree(backup, version)
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            raise RuntimeError("备份完成标记或程序内容无效") from exc

    def _available_backup(self) -> tuple[Path | None, str]:
        if self.backups_dir.is_symlink():
            return None, "备份目录是软链，已禁用自动回滚；请人工核查备份。"
        if not self.backups_dir.exists():
            return None, "尚无可用的代码备份。"
        try:
            entries = sorted(self.backups_dir.iterdir(), key=lambda p: p.lstat().st_mtime_ns, reverse=True)
        except OSError:
            return None, "无法读取备份目录，请检查访问权限。"
        available = None
        legacy = invalid = 0
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    invalid += 1
                    continue
                marker = entry / "complete.json"
                if not marker.exists() and not marker.is_symlink():
                    legacy += 1
                    continue
                self._validate_backup(entry)
                if available is None:
                    available = entry
            except (RuntimeError, OSError):
                invalid += 1
        notices = []
        if legacy:
            notices.append(f"发现 {legacy} 份旧版或未完成备份，缺少完成标记，无法确认完整性；已保留但不用于自动回滚，请人工核查后恢复。")
        if invalid:
            notices.append(f"另有 {invalid} 份备份未通过校验，已跳过并保留。")
        if available is not None:
            notices.append("可回滚到最近通过结构及版本校验的代码备份；数据库和 Python 依赖不会回滚。")
        elif not notices:
            notices.append("尚无可用的代码备份。")
        return available, " ".join(notices)

    def latest_backup(self) -> Path | None:
        return self._available_backup()[0]

    def backup_status(self) -> dict[str, Any]:
        backup, notice = self._available_backup()
        return {"can_rollback": backup is not None, "backup_notice": notice}

    def _valid_backups(self) -> list[tuple[Path, str, float]]:
        """所有通过校验的备份，按时间从新到旧：``(路径, 版本, mtime)``。

        与 ``_available_backup`` 用同一套校验（完成标记 + 结构 + 版本），
        因此历史列表里的回滚目标和「回滚上一版本」可信度一致。
        """
        if self.backups_dir.is_symlink() or not self.backups_dir.is_dir():
            return []
        try:
            entries = sorted(
                self.backups_dir.iterdir(), key=lambda p: p.lstat().st_mtime_ns, reverse=True
            )
        except OSError:
            return []
        found: list[tuple[Path, str, float]] = []
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                marker = entry / "complete.json"
                if not marker.exists() or marker.is_symlink():
                    continue
                version = self._validate_backup(entry)
                found.append((entry, version, entry.lstat().st_mtime))
            except (RuntimeError, OSError):
                continue
        return found

    def backup_for_version(self, version: str) -> Path | None:
        """按版本号查一个可回滚的备份；查不到返回 None。"""
        wanted = normalize_tag(version)
        for path, backed_up, _ in self._valid_backups():
            if normalize_tag(backed_up) == wanted:
                return path
        return None

    def backups(self) -> list[dict[str, Any]]:
        """界面用的备份清单（不含路径，避免把服务器目录结构暴露成接口契约）。"""
        return [
            {"version": normalize_tag(version), "created_at": created, "size": _dir_size(path)}
            for path, version, created in self._valid_backups()
        ]

    # ------------------------------------------------------------- 检查
    def check(self, *, force: bool = False) -> UpdateCheck:
        """查询最新版本。成功结果缓存 10 分钟，避免频繁请求 GitHub API。

        **失败结果不缓存**：把一次网络抖动缓存十分钟，会让界面持续显示
        「检查失败」，用户点多少次都没用，只能等缓存过期。失败必须即时可见。
        """
        with self._lock:
            if not force and self._check_cache is not None:
                cached_at, cached = self._check_cache
                if time.monotonic() - cached_at < CHECK_TTL_SECONDS:
                    return cached

        result = self._fetch_latest()
        self._check_cache = None if result.error else (time.monotonic(), result)
        return result

    def _repo_slug(self) -> tuple[str, str]:
        from urllib.parse import urlparse

        repo = (config.load_settings().update_repo or UPDATE_REPO_DEFAULT).strip().rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        try:
            path = urlparse(repo).path.strip("/")
            owner, name = path.split("/", 1)
            return owner, name
        except (ValueError, AttributeError):
            return "j9kkk", "auto-deploy"

    def _fetch_latest(self) -> UpdateCheck:
        from .schedule import iso, utcnow

        checked_at = iso(utcnow()) or ""
        current = __version__
        settings = config.load_settings()
        repo = (settings.update_repo or UPDATE_REPO_DEFAULT).strip()
        proxy = config.proxy_env(settings)

        # GitHub 仓库优先用 Releases API；失败（限流/内网镜像）则回退到
        # git ls-remote --tags，两者都不通才报错。
        if "github.com" in repo:
            owner, name = self._repo_slug()
            api = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
            request = urllib.request.Request(
                api, headers={"Accept": "application/vnd.github+json", "User-Agent": "AutoDeploy"}
            )
            try:
                with self._urlopen(request, proxy, timeout=15) as response:
                    payload = json.load(response)
                latest = str(payload.get("tag_name") or "").strip()
                if latest:
                    return UpdateCheck(
                        current=current, latest=latest,
                        update_available=compare_versions(latest, current) > 0,
                        checked_at=checked_at,
                        published_at=str(payload.get("published_at") or ""),
                        notes_url=sanitize_url(str(payload.get("html_url") or "")),
                    )
            except (urllib.error.URLError, json.JSONDecodeError, OSError):
                pass  # 回退到 git 协议查询

        # git ls-remote --tags：不依赖 API，且对镜像/私有副本同样有效。
        # 必须复用 gitops 的环境构造，才能带上 HTTP/1.1 等抗抖配置与代理。
        from . import gitops

        with tempfile.TemporaryDirectory(prefix="update-check-", dir=_tmp_root()) as tmp:
            result = gitops.ls_remote_tags(
                repo_url=repo, tmp_dir=Path(tmp), timeout=30, proxy=proxy
            )
        if not result.ok:
            return UpdateCheck(
                current=current, latest="", update_available=False,
                checked_at=checked_at,
                error=result.error or "无法查询最新版本，请检查网络或代理设置",
            )
        candidates = []
        for line in result.output.splitlines():
            ref = line.split("refs/tags/", 1)[-1].strip()
            if not ref or "^{}" in line:
                continue
            normalized = normalize_tag(ref)
            if re.fullmatch(r"\d+(\.\d+)*", normalized):
                candidates.append((normalized, ref))
        if not candidates:
            return UpdateCheck(current=current, latest="", update_available=False,
                               checked_at=checked_at, error="仓库中没有可识别的版本 tag")
        # 保留原始 tag 名（含 v 前缀）用于拉取；归一化值只用于比较。
        # 排序键必须是可全序比较的元组：compare_versions 只能两两比较，
        # 用它当 key 会让所有非零版本并列（实测曾因此选错目标版本）。
        def sort_key(pair: tuple[str, str]) -> tuple[int, ...]:
            return tuple(int(part) for part in pair[0].split("."))

        latest = max(candidates, key=sort_key)[1]
        return UpdateCheck(
            current=current, latest=latest,
            update_available=compare_versions(latest, current) > 0,
            checked_at=checked_at,
            notes_url=sanitize_url(f"{repo}/releases/tag/{latest}"),
        )

    # 更新与部署共用一把准入锁；活动状态持久化后才放行 HTTP 请求。
    def deployment_blocked(self) -> bool:
        stage = self.state().get("stage")
        return stage in ACTIVE_STAGES or stage in BLOCKED_STAGES

    def _preflight(self) -> str:
        strategy, service = self._restart_plan()
        if strategy != "self-exit":
            raise RuntimeError("不支持自动重启：必须在真实 systemd 单元主进程中运行，且有效 Restart=always；尚未修改程序文件")
        if self.store.runs.count_active():
            raise RuntimeError("仍有部署任务在运行，请稍后再更新或回滚")
        root = Path(config.ROOT_DIR)
        if not os.access(root, os.W_OK):
            raise RuntimeError("安装目录不可写")
        if (root / ".git").exists():
            result = run_command(["git", "status", "--porcelain"], cwd=root, timeout=30)
            if not result.ok or result.output.strip():
                raise RuntimeError("源码目录存在未提交改动或无法确认状态，已中止")
        self._check_backup_root()
        self._validate_tree(root)
        return service

    def _new_state(self, operation: str, target: str, service: str) -> dict[str, Any]:
        return dict(schema_version=STATE_SCHEMA_VERSION, stage="queued", operation=operation, operation_id=uuid.uuid4().hex,
                    target_version=target, expected_version=normalize_tag(target),
                    previous_version=__version__, before_boot_id=PROCESS_BOOT_ID,
                    before_pid=os.getpid(), service=service, started_at=time.time(),
                    log=[], error="", backup="",
                    delegated=False, executor_container="", exec_deadline=0,
                    from_image_ref="", to_image_ref="", from_image_id="", to_image_id="")

    def start(self, target_version: str | None = None) -> dict[str, Any]:
        if not isinstance(target_version, str) or not re.fullmatch(r"[vV]?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.-]+)?", target_version):
            raise RuntimeError("请先检查更新并确认有效目标版本")
        # 环境准入（无网络）先行：环境不支持时给出准确原因，而不是耗 45 秒查网络
        # 后报一句「目标已变化」。版本与网络的强校验在准入通过后、全局锁外完成，
        # 慢网络不会阻塞部署/调度准入（调度器与本管理器共用同一把锁）。
        in_docker = in_container()
        if in_docker:
            self._docker_preflight(lambda m: None)
            if self.store.runs.count_active():
                raise RuntimeError("仍有部署任务在运行，容器重建会中断任务，请稍后再更新或回滚")
        else:
            self._preflight()
        with self._lock:
            if self.deployment_blocked():
                raise RuntimeError("已有更新或回滚正在进行")
        checked = self.check(force=True)
        if checked.error or checked.latest != target_version or not checked.update_available:
            raise RuntimeError("更新目标已变化或不可用，请重新检查并确认")
        with self._lock:
            if self.deployment_blocked():
                raise RuntimeError("已有更新或回滚正在进行")
            if in_docker:
                state = self._new_state("update", target_version, "")
                state["restart"] = "docker-executor"
                state["delegated"] = True
                self._save_state(state)
                self._thread = threading.Thread(target=self._run_docker_delegated, args=(state,), daemon=True)
                self._thread.start()
                return dict(state)
            service = self._preflight()
            state = self._new_state("update", target_version, service)
            self._save_state(state)
            self._thread = threading.Thread(target=self._run, args=(state,), daemon=True)
            self._thread.start()
            return dict(state)

    def _set_stage(self, state: dict[str, Any], stage: str) -> None:
        with self._lock:
            state["stage"] = stage
            self._save_state(state)

    @staticmethod
    def _validate_tree(root: Path, expected: str = "") -> str:
        for item in UPDATE_ITEMS:
            path = root / item
            if path.is_symlink() or not path.exists():
                raise RuntimeError(f"程序内容缺失或包含软链：{item}")
            if path.is_dir() != (item in ("app", "web")):
                raise RuntimeError(f"程序路径类型错误：{item}")
            for child in [path, *path.rglob("*")] if path.is_dir() else [path]:
                if child.is_symlink() or not (child.is_file() or child.is_dir()):
                    raise RuntimeError("程序内容含不安全链接或特殊文件")
        for item in ("app/__init__.py", "app/main.py", "web/index.html"):
            if not (root / item).is_file():
                raise RuntimeError(f"程序缺少关键文件：{item}")
        version = ""
        for node in ast.parse((root / "app/__init__.py").read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    version = normalize_tag(node.value.value)
        if not version or (expected and version != normalize_tag(expected)):
            raise RuntimeError("下载或备份版本与预期不符")
        return version

    def _replace(self, source: Path, root: Path) -> None:
        # 每项先完整复制至同盘临时位置，避免复制失败先删除旧代码。
        for item in UPDATE_ITEMS:
            src, dst = source / item, root / item
            staged = root / (".update-" + uuid.uuid4().hex)
            old = root / (".update-old-" + uuid.uuid4().hex)
            try:
                if src.is_dir():
                    shutil.copytree(src, staged)
                else:
                    shutil.copy2(src, staged)
                if dst.exists():
                    os.replace(dst, old)
                try:
                    os.replace(staged, dst)
                except Exception:
                    if old.exists():
                        os.replace(old, dst)
                    raise
            finally:
                for path in (staged, old):
                    if path == old and not dst.exists():
                        continue  # 恢复失败时保留原文件，绝不继续删除。
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()

    def _backup(self, root, state, log):
        self._check_backup_root()
        backup = self.backups_dir / (str(time.time_ns()) + "-" + state["operation_id"])
        backup.mkdir(parents=True)
        for item in UPDATE_ITEMS:
            src = root / item
            if src.is_dir():
                shutil.copytree(src, backup / item)
            else:
                shutil.copy2(src, backup / item)
        version = self._validate_tree(backup)
        (backup / "complete.json").write_text(json.dumps({"version": version}))
        self._validate_backup(backup)
        state["backup"] = str(backup)
        state["backup_version"] = version
        log("已备份程序代码；不包含数据库和 Python 依赖，不能保证降级兼容")
        return backup

    def _restore_backup(self, root, state):
        if not state.get("backup"):
            return
        backup = Path(state["backup"])
        self._validate_backup(backup)
        self._replace(backup, root)
        self._log(state, "已恢复程序代码；数据库和依赖未回滚")

    def _run(self, state):
        import tempfile
        root = Path(config.ROOT_DIR)
        changed = False
        def log(message):
            self._log(state, message)
            self._save_state(state)
        try:
            settings = config.load_settings()
            self._set_stage(state, "downloading")
            config.TMP_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="selfupdate-", dir=config.TMP_DIR) as tmp:
                self._download(state["target_version"], Path(tmp), settings, log, state)
                source = Path(tmp) / "src"
                self._validate_tree(source, state["expected_version"])
                self._set_stage(state, "backing_up")
                self._backup(root, state, log)
                self._set_stage(state, "applying")
                changed = True
                self._replace(source, root)
                self._set_stage(state, "dependencies")
                pip = run_command([sys_executable(), "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                                  timeout=600, env={**os.environ, **config.proxy_env(settings, for_scripts=True)})
                if not pip.ok:
                    raise RuntimeError("依赖安装失败；依赖可能已部分改变，请检查服务环境")
                self._finish(state, log)
        except Exception as exc:
            self._fail(state, exc, changed)

    def _fail(self, state, exc, changed):
        error = str(exc)
        if changed:
            try:
                self._restore_backup(Path(config.ROOT_DIR), state)
            except Exception as restore_error:
                error += f"；代码恢复失败：{restore_error}；请从保留的备份人工恢复"
        state.update(stage="failed", error=error)
        self._log(state, error)
        self._save_state(state)
        self._append_history(state)

    def _download(self, target, tmp_root, settings, log, state):
        from . import gitops
        gitops.sync_checkout(repo_url=settings.update_repo or UPDATE_REPO_DEFAULT,
                             branch=target, workspace=tmp_root / "src", depth=1,
                             tmp_dir=tmp_root / "creds", timeout=int(settings.git_timeout_seconds),
                             log=log, proxy=config.proxy_env(settings), home=tmp_root / "creds")
        source = tmp_root / "src"
        head = run_command(["git", "rev-parse", "HEAD"], cwd=source, timeout=10)
        tag = run_command(["git", "rev-parse", "--verify", "refs/tags/" + target + "^{commit}"], cwd=source, timeout=10)
        if not head.ok or not tag.ok or head.output.strip() != tag.output.strip():
            raise RuntimeError("下载结果不是已确认的目标标签")
        log("已检出目标标签")

    def _cgroup_path(self) -> str:
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                _, controllers, path = line.split(":", 2)
                if controllers in ("", "name=systemd"):
                    return path
        except (OSError, ValueError):
            pass
        return ""

    @staticmethod
    def _restart_plan_static() -> tuple[str, str]:
        """无实例形态检测：供 docker_run_mode 在未建 manager 时复用同一套判定。"""
        return SelfUpdateManager(None)._restart_plan()

    def _restart_plan(self) -> tuple[str, str]:
        path = self._cgroup_path()
        service = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9@_.-]+\.service", service):
            return "unsupported", ""
        result = run_command(["systemctl", "show", service, "--no-pager",
                              "--property=Id,LoadState,ActiveState,MainPID,ControlGroup,Restart,RestartPreventExitStatus"], timeout=10)
        props = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
        if (result.ok and props.get("Id") == service and props.get("LoadState") == "loaded"
                and props.get("ActiveState") == "active" and props.get("MainPID") == str(os.getpid())
                and props.get("ControlGroup") == path and props.get("Restart") == "always"
                and not props.get("RestartPreventExitStatus")):
            return "self-exit", service
        return "unsupported", service

    # ------------------------------------------------------------- Docker 形态
    @staticmethod
    def _self_container_id() -> str:
        """当前容器 ID（hostname 即短 ID 的惯例写法）；非容器返回空。"""
        if not in_container():
            return ""
        try:
            cid = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return cid if re.fullmatch(r"[0-9a-f]{12,64}", cid) else ""

    def _docker_self_info(self) -> dict[str, Any]:
        """读取自身容器的 compose 标签、镜像身份与挂载。

        依赖 docker CLI 查询（inspect 输出 JSON），不引入任何非标准库依赖。
        失败返回空字典，由调用方给出明确报错。
        """
        cid = self._self_container_id()
        if not cid:
            return {}
        result = run_command(
            ["docker", "inspect", cid, "--format",
             '{{json .Config.Labels}}\t{{.Config.Image}}\t{{.Image}}\t{{json .Mounts}}\t'
             '{{index .Config.Labels "com.docker.compose.project.working_dir"}}\t'
             '{{index .Config.Labels "com.docker.compose.project"}}\t'
             '{{index .Config.Labels "com.docker.compose.project.config_files"}}\t'
             '{{index .Config.Labels "com.docker.compose.service"}}'],
            timeout=15,
        )
        if not result.ok:
            return {}
        parts = result.output.strip().split("\t")

        def _json_part(index: int) -> Any:
            if len(parts) <= index or not parts[index] or parts[index] == "null":
                return None
            try:
                return json.loads(parts[index])
            except json.JSONDecodeError:
                return None

        labels = _json_part(0) or {}
        mounts = _json_part(3)
        info: dict[str, Any] = {}
        if isinstance(labels, dict):
            info["project_working_dir"] = str(labels.get("com.docker.compose.project.working_dir") or "")
            info["project"] = str(labels.get("com.docker.compose.project") or "")
            info["config_files"] = str(labels.get("com.docker.compose.project.config_files") or "")
            info["service"] = str(labels.get("com.docker.compose.service") or "")
        info["image"] = parts[1].strip() if len(parts) > 1 else ""
        info["image_id"] = parts[2].strip() if len(parts) > 2 else ""
        info["mounts"] = mounts if isinstance(mounts, list) else []
        info["working_dir"] = parts[4].strip() if len(parts) > 4 else ""
        info["project"] = info.get("project") or (parts[5].strip() if len(parts) > 5 else "")
        return info

    def _compose_env_paths(self, info: dict[str, str]) -> tuple[Path | None, str]:
        """定位安装目录 .env，返回 (路径, 不可用原因)；路径可用时原因为空串。

        路径来自容器的 compose 标签，是宿主机路径：只有在容器内同样可见
        （一键脚本把它挂载进来）时才能读写。0.3.2 及更早的安装没有这个
        挂载，必须在这里拦下并指引重跑一键脚本，而不是半路失败。
        """
        workdir = info.get("working_dir") or info.get("project_working_dir") or ""
        if not workdir:
            return None, "无法从容器 compose 标签确定宿主机安装目录"
        path = Path(workdir) / ".env"
        try:
            resolved = path.resolve()
            # 越界防护：工作目录标签理论上可被伪造，确认其父目录真实存在。
            if not resolved.parent.is_dir():
                return None, (
                    f"宿主机安装目录 {workdir} 在容器内不可见（0.3.2 及更早的一键脚本"
                    f"没有把它挂载进容器），面板无法改写其中的 .env。请在服务器重新执行"
                    "一键脚本完成本次升级，之后面板内升级即可正常使用"
                )
        except OSError:
            return None, f"宿主机安装目录 {workdir} 无法访问"
        if not path.exists():
            return None, (
                f"安装目录 {workdir} 下没有 .env，无法切换镜像版本。"
                "请重新执行一键脚本，或手动执行：docker compose pull && docker compose up -d"
            )
        return path, ""

    def _compose_env_file(self, info: dict[str, str]) -> Path | None:
        """安装目录的 .env（compose 启动目录下的 AUTODEPLOY_IMAGE_TAG 所在文件）。"""
        path, _ = self._compose_env_paths(info)
        return path

    def _current_image_ref(self, info: dict[str, str]) -> str:
        """当前镜像引用（如 ghcr.io/j9kkk/auto-deploy:0.3.5）。

        用 parse_image_ref 而不是旧正则：旧正则不支持带端口的私有 registry
        （localhost:5000/app），这类安装会被误判为"引用无法识别"而拒绝升级。
        """
        image = (info.get("image") or "").strip()
        return image if image and parse_image_ref(image) else ""

    def _docker_upgrade_context(self) -> dict[str, Any]:
        """收集升级所需的完整上下文：容器、项目、配置文件、服务、挂载。

        重建必须忠实复现原始部署身份：项目名、有序配置文件与目标服务都来自
        容器上的 compose 标签，而不是"在目录里碰运气"。任何一环拿不到都在
        改动之前拒绝，并给出人工升级途径。
        """
        if os.environ.get("DOCKER_HOST"):
            raise RuntimeError(
                "当前环境设置了自定义 DOCKER_HOST，面板无法确认守护进程与安装目录的对应关系；"
                "请人工执行 docker compose pull && docker compose up -d 升级"
            )
        info = self._docker_self_info()
        if not info.get("image"):
            raise RuntimeError(
                "无法确定当前容器信息（缺少 docker inspect 或 compose 标签），已中止升级；"
                "请使用一键脚本或手动 docker compose pull && docker compose up -d"
            )
        env_file, env_problem = self._compose_env_paths(info)
        if env_file is None:
            raise RuntimeError(f"{env_problem}；已中止升级，未做任何更改")
        project = str(info.get("project") or "").strip()
        service = str(info.get("service") or "").strip()
        files_label = str(info.get("config_files") or "").strip()
        workdir = str(info.get("working_dir") or info.get("project_working_dir") or "").strip()
        if not project or not service or not workdir:
            raise RuntimeError(
                "容器缺少 compose 项目/服务/工作目录标签（可能不是由 compose 创建），"
                "面板无法忠实重建原配置；请使用一键脚本或手动升级"
            )
        if not files_label:
            raise RuntimeError(
                "容器缺少 compose 配置文件标签，面板无法按原配置重建；请使用一键脚本或手动升级"
            )
        wd = Path(workdir)
        try:
            wd_resolved = wd.resolve()
        except OSError as exc:
            raise RuntimeError(f"安装目录无法解析：{exc}") from exc
        files: list[str] = []
        for item in files_label.split(","):
            item = item.strip()
            if not item:
                continue
            path = Path(item)
            if not path.is_absolute():
                path = wd / path
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise RuntimeError(f"compose 配置文件无法解析：{item}（{exc}）") from exc
            if resolved != wd_resolved and wd_resolved not in resolved.parents:
                raise RuntimeError(
                    f"compose 配置文件 {item} 位于安装目录之外，执行器容器内不可见，"
                    "面板无法保证按原配置重建；请使用一键脚本或手动升级"
                )
            if not resolved.is_file():
                raise RuntimeError(f"compose 配置文件在容器内不可见：{item}")
            files.append(str(resolved))
        if not files:
            raise RuntimeError("compose 配置文件列表为空，无法重建")
        mounts = info.get("mounts") or []
        data_mount = None
        for mount in mounts:
            if isinstance(mount, dict) and mount.get("Destination") == "/app/data":
                data_mount = mount
                break
        if not data_mount or not (data_mount.get("Name") or data_mount.get("Source")):
            raise RuntimeError(
                "未在当前容器上找到 /app/data 数据卷，升级执行器无法与面板共享状态；"
                "请使用一键脚本或手动升级"
            )
        env_path = Path(workdir) / ".env"
        if env_path.is_symlink():
            raise RuntimeError(".env 是软链，拒绝自动升级；请人工核查后处理")
        try:
            env_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"无法读取安装目录 .env（权限不足或 IO 错误）：{exc}") from exc
        image_ref = self._current_image_ref(info)
        if not image_ref:
            raise RuntimeError(
                f"当前镜像引用无法识别（{info.get('image')}）；已中止升级，未做任何更改"
            )
        return {
            "container_id": self._self_container_id(),
            "project": project,
            "service": service,
            "working_dir": workdir,
            "config_files": files,
            "env_file": str(env_path),
            "image_ref": image_ref,
            "image_id": str(info.get("image_id") or ""),
            "mounts": [m for m in mounts if isinstance(m, dict)],
        }

    def _docker_preflight(self, log) -> dict[str, Any]:
        """Docker 升级前置检查：能力、完整 compose 上下文与镜像身份。"""
        if not docker_available():
            raise RuntimeError(
                "当前环境无法操作 docker（未安装 docker，或容器未挂载 /var/run/docker.sock），"
                "面板无法自升级。请在服务器执行一键脚本，或手动：docker compose pull && docker compose up -d"
            )
        context = self._docker_upgrade_context()
        log(f"已定位安装目录：{context['working_dir']}")
        log(f"compose 上下文：项目 {context['project']} · 服务 {context['service']} · "
            f"{len(context['config_files'])} 个配置文件")
        return context

    def _ensure_image(self, ref: str, log) -> str:
        """确保镜像在本地存在并返回其不可变 ID（sha256:…）。

        本地已有直接使用（离线/内网/自建仓库可用）；缺失才拉取。回退始终
        以镜像 ID 为准，不信任可变 tag 的内容。
        """
        image_id = self._image_id_of(ref)
        if image_id:
            log(f"镜像已存在本地：{ref}")
            return image_id
        log(f"拉取镜像 {ref}（可能需要几分钟）…")
        settings = config.load_settings()
        result = run_command(["docker", "pull", ref], timeout=1800, log=log,
                             env=config.proxy_env(settings, for_scripts=True))
        if not result.ok:
            raise RuntimeError(f"镜像拉取失败（{ref}）；旧容器未受影响，请检查网络或该版本是否存在")
        image_id = self._image_id_of(ref)
        if not image_id:
            raise RuntimeError(f"拉取后无法读取镜像 ID（{ref}），已中止")
        log(f"镜像已就绪：{ref}")
        return image_id

    def _image_id_of(self, ref: str) -> str:
        result = run_command(["docker", "image", "inspect", "--format", "{{.Id}}", ref], timeout=20)
        if result.ok:
            value = result.output.strip().splitlines()
            if value and re.fullmatch(r"sha256:[a-f0-9]{64}", value[0].strip()):
                return value[0].strip()
        return ""

    def _container_running(self, name: str) -> bool:
        if not name:
            return False
        result = run_command(["docker", "inspect", "--format", "{{.State.Running}}", name], timeout=15)
        return result.ok and result.output.strip().lower() == "true"

    def _cleanup_executor_containers(self, log) -> None:
        """清理历史执行器容器（带标签且已退出），避免残留堆积。"""
        result = run_command(
            ["docker", "ps", "-a", "--filter", f"label={EXECUTOR_LABEL}",
             "--format", "{{.ID}} {{.State}}"],
            timeout=30,
        )
        if not result.ok:
            return
        for line in result.output.splitlines():
            cid, _, container_state = line.strip().partition(" ")
            if cid and container_state.strip() != "running":
                run_command(["docker", "rm", "-f", cid], timeout=30, log=log)

    def _write_plan(self, context: dict[str, Any], state: dict[str, Any],
                    expected_version: str, restore_image_id: str) -> Path:
        """把操作计划写入数据卷，供执行器容器读取（0600，不含任何秘密）。"""
        plan = {
            "operation_id": state["operation_id"],
            "operation": state["operation"],
            "target_image_ref": state.get("to_image_ref") or "",
            "expected_version": expected_version,
            "restore_image_id": restore_image_id,
            "from_image_ref": context["image_ref"],
            "from_image_id": context["image_id"],
            "web_container_id": context["container_id"],
            "before_boot_id": PROCESS_BOOT_ID,
            "project": context["project"],
            "working_dir": context["working_dir"],
            "config_files": context["config_files"],
            "service": context["service"],
            "created_at": time.time(),
        }
        if not plan["target_image_ref"]:
            raise RuntimeError("目标镜像引用缺失，已中止")
        path = plan_file_path()
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        return path

    def _start_docker_executor(self, state: dict[str, Any], context: dict[str, Any],
                               expected_version: str, restore_image_id: str, log) -> None:
        """启动独立执行器容器并转入监控。

        执行器以 root 在独立容器中运行，镜像必须是**目标镜像**（含新版
        upgrade_exec），而不是当前镜像——旧代码不认识新协议；挂载同一组卷，
        与被替换的 Web 容器是兄弟关系：Web 重建不影响升级推进。
        """
        self._write_plan(context, state, expected_version, restore_image_id)
        try:
            marker_file_path().unlink()
        except OSError:
            pass
        self._cleanup_executor_containers(log)
        name = f"auto-deploy-upgrade-{state['operation_id'][:12]}"
        # 先落盘执行器标识与阶段，再启动容器：执行器可能先于本线程继续推进，
        # 状态文件必须从一开始就带上 executor_container。
        state["executor_container"] = name
        state["exec_deadline"] = time.time() + EXECUTOR_TIMEOUT
        self._set_stage(state, "executing")
        command = ["docker", "run", "-d", "--name", name, "--user", "0",
                   "--entrypoint", "python",
                   "--label", f"{EXECUTOR_LABEL}=1",
                   "--label", f"com.autodeploy.upgrade-operation={state['operation_id']}"]
        for mount in context["mounts"]:
            source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
            destination = mount.get("Destination") or ""
            if not source or not destination:
                continue
            suffix = "" if mount.get("RW", True) else ":ro"
            command += ["-v", f"{source}:{destination}{suffix}"]
        executor_image = state.get("to_image_id") or state.get("to_image_ref") or ""
        if not executor_image:
            raise RuntimeError("目标镜像身份缺失，无法启动升级执行器")
        command += [executor_image,
                    "-m", "app.upgrade_exec", "--plan", "/app/data/upgrade-plan.json"]
        log(f"启动独立升级执行器（{name}）：切换、重建与验证都在面板容器之外完成")
        result = run_command(command, timeout=120, log=log)
        if not result.ok:
            raise RuntimeError(f"升级执行器启动失败：{result.error or result.output[-300:]}")
        self._monitor_thread = threading.Thread(
            target=self._monitor_delegated, args=(dict(state),), daemon=True)
        self._monitor_thread.start()

    def _monitor_delegated(self, snapshot: dict[str, Any]) -> None:
        """跟踪执行器结果：文件终态即返回；容器消失/超时且无终态转人工处置。"""
        operation_id = str(snapshot.get("operation_id") or "")
        name = str(snapshot.get("executor_container") or "")
        deadline = snapshot.get("exec_deadline") or (time.time() + EXECUTOR_TIMEOUT)
        while True:
            fresh = load_state_file()
            if fresh.get("operation_id") != operation_id:
                return
            if fresh.get("stage") in TERMINAL_STAGES:
                return
            if time.time() > deadline:
                self._mark_attention(fresh, "升级执行器超时且未记录结果，结果不确定；请处置后继续")
                return
            if not self._container_running(name):
                time.sleep(1.0)  # 执行器退出与终态落盘之间可能有一个小窗口
                fresh = load_state_file()
                if (fresh.get("operation_id") == operation_id
                        and fresh.get("stage") in TERMINAL_STAGES):
                    return
                self._mark_attention(
                    fresh, f"升级执行器已退出但未记录结果，结果不确定；可查看执行器日志：docker logs {name}")
                return
            time.sleep(2.0)

    def _mark_attention(self, fresh: dict[str, Any], reason: str) -> None:
        with self._lock:
            current = load_state_file()
            if current.get("operation_id") != fresh.get("operation_id"):
                return
            if current.get("stage") in TERMINAL_STAGES:
                return
            current.update(stage="attention", error=reason)
            self._log(current, reason)
            if save_state_file(current):
                append_history_entry(current)

    def _fail_docker(self, state: dict[str, Any], exc: Exception) -> None:
        error = str(exc)
        state.update(stage="failed", error=error)
        self._log(state, error)
        self._save_state(state)
        self._append_history(state)

    def _run_docker_delegated(self, state: dict[str, Any]) -> None:
        """Docker 升级：预拉镜像 → 写计划 → 启动独立执行器 → 监控。"""
        def log(message):
            self._log(state, message)
            self._save_state(state)
        try:
            context = self._docker_upgrade_context()
            repo = repo_of_image_ref(context["image_ref"])
            if not repo:
                raise RuntimeError(f"当前镜像引用无法解析（{context['image_ref']}）；已中止")
            target_tag = state["expected_version"]
            state["from_image_ref"] = context["image_ref"]
            state["to_image_ref"] = f"{repo}:{target_tag}"
            self._save_state(state)
            self._set_stage(state, "pulling_image")
            state["to_image_id"] = self._ensure_image(state["to_image_ref"], log)
            self._save_state(state)
            self._start_docker_executor(state, context, target_tag, "", log)
        except Exception as exc:
            self._fail_docker(state, exc)

    def _expected_version_for_image(self, ref: str) -> str:
        """从历史推断回退目标的程序版本；推断不出返回空串（执行器跳过版本校验）。"""
        for item in self.history():
            if str(item.get("to_image") or "") == ref:
                return normalize_tag(str(item.get("target_version") or ""))
            if str(item.get("from_image") or "") == ref:
                return normalize_tag(str(item.get("previous_version") or ""))
        return ""

    def _run_docker_rollback_delegated(self, state: dict[str, Any], target_ref: str,
                                       expected_version: str, restore_image_id: str) -> None:
        """Docker 回退：与升级同一委托链路，目标为历史中的旧镜像。"""
        def log(message):
            self._log(state, message)
            self._save_state(state)
        try:
            context = self._docker_upgrade_context()
            state["from_image_ref"] = context["image_ref"]
            state["to_image_ref"] = target_ref
            self._save_state(state)
            self._set_stage(state, "pulling_image")
            if restore_image_id:
                # 已记录旧镜像 ID：不重新拉取，确保恢复的比特与升级前一致。
                state["to_image_id"] = ""
                log("将按记录的旧镜像 ID 恢复（不信任可变 tag 内容）")
            else:
                state["to_image_id"] = self._ensure_image(target_ref, log)
            self._save_state(state)
            self._start_docker_executor(state, context, expected_version, restore_image_id, log)
        except Exception as exc:
            self._fail_docker(state, exc)

    def _finish(self, state, log):
        if self._restart_plan() != ("self-exit", state["service"]):
            raise RuntimeError("重启能力已变化，已中止更新")
        state.update(restart="self-exit", restart_deadline=time.time() + RESTART_TIMEOUT)
        self._set_stage(state, "restarting")
        log("即将退出，等待 systemd 拉起后确认版本")
        self._exit_for_restart(state)

    def _exit_for_restart(self, state):
        def exit_later():
            time.sleep(1.5)
            os._exit(0)
        threading.Thread(target=exit_later, daemon=True).start()

    def rollback(self, target_version: str | None = None, target_image: str | None = None):
        """回退。

        Docker 形态（``target_image`` 提供镜像引用，如 ``ghcr.io/j9kkk/auto-deploy:v0.3.0``）：
        把 .env 切回目标镜像并重建容器。引用必须来自升级历史（界面只展示历史里
        出现过的 from_image），且经 IMAGE_REF_RE 校验，无法注入任意字符串。

        裸机形态：回滚到指定版本的代码备份；不指定则回滚最近一份可用备份。
        只在**通过校验的备份**里按版本号查找（清单来自 ``_valid_backups``，
        不接受任何路径），因此界面传入的值无法越界或指向备份目录之外的代码。
        """
        docker_ref = ""
        if target_image:
            docker_ref = str(target_image).strip()
            parsed = parse_image_ref(docker_ref)
            if not parsed or not (parsed[1] or parsed[2]):
                raise RuntimeError("回退目标镜像引用无效")
        with self._lock:
            current_stage = self.state().get("stage")
            if current_stage in ACTIVE_STAGES:
                raise RuntimeError("更新或回滚正在进行，无法重复操作")
            if docker_ref:
                current = load_state_file()
                blocked_ref = str(current.get("from_image_ref") or "")
                if current_stage in BLOCKED_STAGES and docker_ref != blocked_ref:
                    raise RuntimeError("当前操作等待人工处理，仅允许恢复到升级前镜像；处置后再试其他目标")
                # Docker 回退：目标必须出现在升级历史里（防止凭空构造引用）。
                known = {str(item.get("from_image") or "") for item in self.history()}
                known |= {str(item.get("to_image") or "") for item in self.history()}
                known.discard("")
                if docker_ref not in known:
                    raise RuntimeError("回退目标不在升级历史中，已拒绝")
                if self.store.runs.count_active():
                    raise RuntimeError("仍有部署任务在运行，容器重建会中断任务，请稍后再回滚")
                # preflight 失败（无 socket/定位不到 compose）直接抛错，尚未动任何文件。
                context = self._docker_preflight(lambda m: None)
                expected = self._expected_version_for_image(docker_ref)
                # 恢复到"本次升级前镜像"时使用记录的镜像 ID，不信任可变 tag。
                restore_id = ""
                if current_stage in BLOCKED_STAGES and blocked_ref == docker_ref:
                    restore_id = str(current.get("from_image_id") or "")
                state = self._new_state("rollback", docker_ref, "")
                state["restart"] = "docker-executor"
                state["delegated"] = True
                state["expected_version"] = expected
                state["from_image_ref"] = context["image_ref"]
                state["to_image_ref"] = docker_ref
                self._save_state(state)
                self._thread = threading.Thread(
                    target=self._run_docker_rollback_delegated,
                    args=(state, docker_ref, expected, restore_id), daemon=True)
                self._thread.start()
                return {"ok": True, "state": dict(state)}
            service = self._preflight()
            if target_version:
                wanted = str(target_version).strip()
                if not re.fullmatch(r"[vV]?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.-]+)?", wanted):
                    raise RuntimeError("回滚目标版本号无效")
                backup = self.backup_for_version(wanted)
                if backup is None:
                    raise RuntimeError(f"没有版本 {normalize_tag(wanted)} 的可用代码备份")
            else:
                backup = self.latest_backup()
                if backup is None:
                    raise RuntimeError("没有可用的代码备份")
            version = self._validate_backup(backup)
            state = self._new_state("rollback", version, service)
            self._save_state(state)
            def run():
                changed = False
                try:
                    self._set_stage(state, "backing_up")
                    self._backup(Path(config.ROOT_DIR), state, lambda m: self._log(state, m))
                    self._set_stage(state, "applying")
                    changed = True
                    self._replace(backup, Path(config.ROOT_DIR))
                    self._finish(state, lambda m: self._log(state, m))
                except Exception as exc:
                    self._fail(state, exc, changed)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            return {"ok": True, "state": dict(state)}

    def reconcile_on_startup(self):
        with self._lock:
            state = self.state()
            if state.get("stage") not in ACTIVE_STAGES:
                return
            if state.get("delegated"):
                self._reconcile_delegated(state)
                return
            # 裸机/旧版记录的确认：进程身份以 boot_id 为准。PID 不再作为必要
            # 条件——容器各自有独立 PID namespace，新容器完全可能复用相同
            # PID 数值（实测因此把成功升级误判为失败）。
            valid = (state.get("stage") == "restarting" and state.get("operation") in ("update", "rollback")
                     and isinstance(state.get("operation_id"), str) and bool(state["operation_id"])
                     and isinstance(state.get("before_boot_id"), str) and bool(state["before_boot_id"])
                     and state["before_boot_id"] != PROCESS_BOOT_ID
                     and state.get("expected_version") == normalize_tag(__version__)
                     and normalize_tag(str(state.get("target_version", ""))) == normalize_tag(__version__))
            if valid:
                state.update(stage="done", version=__version__, confirmed_operation=state["operation"],
                             confirmed_operation_id=state["operation_id"], boot_id=PROCESS_BOOT_ID, pid=os.getpid())
                self._log(state, "启动后已确认操作及目标版本运行")
            else:
                state.update(stage="failed", error="更新或回滚中断，或启动版本/进程与预期不符；未确认成功")
                self._log(state, state["error"])
            self._save_state(state)
            # 只有这里（重启后确认/落定为失败）才是操作的终态，历史在此写入，
            # 使「升级日志」里的每条记录都带最终结果而不是中途状态。
            self._append_history(state)

    def _reconcile_delegated(self, state: dict[str, Any]) -> None:
        """委托操作的启动收尾：新进程报告身份，等待执行器完成最终验证。

        这里绝不落 done：成功由执行器在验证 HTTP 健康、镜像身份与启动标记
        之后统一判定；执行器缺席且无终态则转入人工处置。
        """
        matches = (state.get("operation") in ("update", "rollback")
                   and isinstance(state.get("operation_id"), str) and bool(state["operation_id"])
                   and isinstance(state.get("before_boot_id"), str) and bool(state["before_boot_id"])
                   and state["before_boot_id"] != PROCESS_BOOT_ID
                   and state.get("expected_version") == normalize_tag(__version__))
        if matches:
            marker = {
                "operation_id": state["operation_id"],
                "version": __version__,
                "boot_id": PROCESS_BOOT_ID,
                "pid": os.getpid(),
                "container_id": self._self_container_id(),
                "time": time.time(),
            }
            try:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                path = marker_file_path()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(marker, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, 0o600)
                self._log(state, "新进程已启动并报告身份，等待升级执行器完成验证")
            except OSError as exc:
                self._log(state, f"启动标记写入失败：{exc}")
        name = str(state.get("executor_container") or "")
        if name and self._container_running(name):
            self._log(state, "检测到升级执行器仍在运行，继续跟踪其结果")
            self._monitor_thread = threading.Thread(
                target=self._monitor_delegated, args=(dict(state),), daemon=True)
            self._monitor_thread.start()
            return
        fresh = load_state_file()
        if (fresh.get("operation_id") == state.get("operation_id")
                and fresh.get("stage") in TERMINAL_STAGES):
            return
        payload = fresh if fresh.get("operation_id") == state.get("operation_id") else dict(state)
        self._mark_attention(payload, "升级执行器不在运行且未记录结果，结果不确定；请处置后继续")

    def attention_action(self, action: str) -> dict[str, Any]:
        """人工处置入口：继续等待执行器，或确认已手动处理并解除禁入。"""
        if action not in ("wait", "resolve"):
            raise RuntimeError("不支持的处理动作")
        with self._lock:
            state = self.state()
            if state.get("stage") not in BLOCKED_STAGES:
                raise RuntimeError("当前没有等待人工处理的操作")
            if action == "wait":
                if state.get("stage") == "recovery_required":
                    raise RuntimeError("自动恢复已失败，无法继续等待；请人工处理或标记已处理")
                name = str(state.get("executor_container") or "")
                if not self._container_running(name):
                    raise RuntimeError("升级执行器已退出，无法继续等待；请恢复到升级前版本或标记已处理")
                state["stage"] = "executing"
                state["exec_deadline"] = time.time() + EXECUTOR_TIMEOUT
                state["error"] = ""
                self._log(state, "选择继续等待升级执行器结果")
                if not save_state_file(state):
                    raise RuntimeError("状态更新失败，请重试")
                self._monitor_thread = threading.Thread(
                    target=self._monitor_delegated, args=(dict(state),), daemon=True)
                self._monitor_thread.start()
                return dict(state)
            error = str(state.get("error") or "")
            state.update(stage="failed",
                         error=f"用户已确认人工处理。原记录：{error}" if error else "用户已确认人工处理")
            self._log(state, "操作标记为人工处理，解除更新与部署禁入")
            if save_state_file(state):
                append_history_entry(state)
            return dict(state)


def sys_executable() -> str:
    import sys
    return sys.executable
