"""Global configuration and filesystem layout for AutoDeploy.

Precedence for every setting is: built-in default < data/config.json < environment
variable.  The JSON file is the one edited from the web UI, so it must win over
defaults; environment variables win over everything so that operators can pin a
value in the systemd unit without the UI silently overriding it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent

# 自我更新默认代码来源。
UPDATE_REPO_DEFAULT = "https://github.com/j9kkk/auto-deploy.git"


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime settings shared by the API, the scheduler and the runner."""

    # --- web server -------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8770
    # Set when the service sits behind a reverse proxy so generated links and
    # the cookie path stay correct.
    base_url: str = ""
    # Trust X-Forwarded-For / X-Forwarded-Proto from a fronting proxy.
    trust_proxy_headers: bool = False
    secure_cookies: bool = False

    # --- authentication ---------------------------------------------------
    session_ttl_hours: int = 12
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    # --- scheduler --------------------------------------------------------
    # How often the scheduler loop wakes up to look for due tasks.
    poll_interval_seconds: int = 2
    # Upper bound on deploy pipelines running at the same time, across tasks.
    max_global_workers: int = 3
    # When a task is still running and its next tick arrives: skip the tick
    # (False) instead of queueing another run behind it (True).
    queue_while_running: bool = False

    # --- execution --------------------------------------------------------
    default_timeout_seconds: int = 1800
    git_timeout_seconds: int = 900
    # Shell used to execute scripts. Must accept `-c`.
    shell: str = "/bin/bash"
    # Seconds between SIGTERM and SIGKILL when stopping a run.
    kill_grace_seconds: int = 10

    # --- network proxy ----------------------------------------------------
    # Applied to git operations and (optionally) deploy scripts. Kept as
    # environment variables rather than `git -c http.proxy=...` so a proxy
    # password never appears in the process list.
    proxy_enabled: bool = False
    # e.g. http://proxy.corp.local:8080 or socks5://127.0.0.1:1080
    proxy_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    # Hosts/domains that must bypass the proxy (comma separated), e.g. the
    # internal GitLab. Without this every internal fetch would go through the
    # external proxy and fail.
    proxy_no_proxy: str = "localhost,127.0.0.1,::1"
    # Also export the proxy to prepare/deploy/rollback scripts, so that
    # `npm install` / `pip install` inside a build can reach the network.
    proxy_for_scripts: bool = True

    # --- retention --------------------------------------------------------
    run_retention_days: int = 30
    run_retention_count: int = 500
    release_retention_count: int = 5
    artifact_retention_count: int = 5
    log_retention_days: int = 14
    # A single run log is truncated after this many bytes to protect the disk.
    log_max_bytes: int = 8_000_000

    # --- self update ------------------------------------------------------
    # 自我更新的代码来源；可指向镜像或私有副本（需可匿名读取或配合代理）。
    update_repo: str = UPDATE_REPO_DEFAULT

    # --- misc -------------------------------------------------------------
    timezone: str = "UTC"
    notify_webhook: str = ""
    default_branch: str = "main"
    git_http_timeout_seconds: int = 300

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Environment variable for every field, derived from the field name.
_ENV_PREFIX = "AUTODEPLOY_"


def settings_path() -> Path:
    return DATA_DIR / "config.json"


# 配置文件读多写少，按 (mtime_ns, size) 缓存解析结果；未变化时免磁盘 IO。
# 单线程写（save_settings 后主动失效），多线程读，最坏情况是重复构建一次。
_settings_cache: Settings | None = None
_settings_cache_key: tuple[int, int] | None = None


def _settings_fingerprint() -> tuple[int, int] | None:
    try:
        stat = settings_path().stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_settings() -> Settings:
    """Build the effective settings object from file + environment."""
    global _settings_cache, _settings_cache_key
    fingerprint = _settings_fingerprint()
    if _settings_cache is not None and fingerprint == _settings_cache_key:
        return _settings_cache
    settings = Settings()
    path = settings_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stored = {}
        if isinstance(stored, dict):
            valid = {f.name: f.type for f in fields(Settings)}
            for key, value in stored.items():
                if key in valid:
                    setattr(settings, key, value)

    for field_info in fields(Settings):
        env_name = _ENV_PREFIX + field_info.name.upper()
        current = getattr(settings, field_info.name)
        if isinstance(current, bool):
            setattr(settings, field_info.name, _env_bool(env_name, current))
        elif isinstance(current, int):
            setattr(settings, field_info.name, _env_int(env_name, current))
        else:
            raw = _env_str(env_name)
            if raw is not None:
                setattr(settings, field_info.name, raw)

    _coerce(settings)
    _settings_cache = settings
    _settings_cache_key = _settings_fingerprint()
    return settings


def _coerce(settings: Settings) -> None:
    """Clamp values that would otherwise break the service at runtime."""
    settings.port = max(1, min(65535, int(settings.port)))
    settings.poll_interval_seconds = max(1, int(settings.poll_interval_seconds))
    settings.max_global_workers = max(1, min(32, int(settings.max_global_workers)))
    settings.session_ttl_hours = max(1, int(settings.session_ttl_hours))
    settings.default_timeout_seconds = max(5, int(settings.default_timeout_seconds))
    settings.log_max_bytes = max(100_000, int(settings.log_max_bytes))


def save_settings(settings: Settings) -> None:
    """Atomically persist settings to ``data/config.json``."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings.as_dict(), indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, settings_path())
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    global _settings_cache, _settings_cache_key
    _settings_cache = None
    _settings_cache_key = None


def data_dir() -> Path:
    return Path(
        _env_str("AUTODEPLOY_DATA_DIR") or (ROOT_DIR / "data")
    ).resolve()


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "autodeploy.db"
WORKSPACES_DIR = DATA_DIR / "workspaces"
RELEASES_DIR = DATA_DIR / "releases"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
LOGS_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"
WEB_DIR = ROOT_DIR / "web"


def ensure_dirs() -> None:
    for path in (
        DATA_DIR,
        WORKSPACES_DIR,
        RELEASES_DIR,
        ARTIFACTS_DIR,
        LOGS_DIR,
        TMP_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def workspace_dir(task_id: int) -> Path:
    """工作目录：按 id 定位（历史行为），新代码请用 :func:`workspace_dir_for_task`。"""
    return WORKSPACES_DIR / f"task-{task_id}"


def _sanitize_dirname(name: str) -> str:
    """把任务名转成文件系统安全的目录名。

    保留中文等 Unicode 字母以便目录可读；只清除路径分隔符、控制字符与
    各平台保留字符。空结果回退为 'task'。
    """
    cleaned_chars: list[str] = []
    for ch in name:
        code = ord(ch)
        if code < 32 or code == 127:  # 控制字符
            cleaned_chars.append("-")
        elif ch in '/\\:*?"<>|':  # 跨平台非法/保留字符
            cleaned_chars.append("-")
        elif ch == " ":
            cleaned_chars.append("-")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip("-.")
    # 清洗后只剩标点（或为空）时回退为 'task'，避免产生全符号目录名。
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return "task"
    # Windows 保留名（CON/PRN/...）即使带扩展名也非法，统一加前缀规避。
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned[:80] or "task"


CLAIM_FILE = ".autodeploy-owner"


def _claimed_by(directory: Path, task_id: Any) -> bool:
    """目录内的认领标记是否指向该任务。"""
    marker = directory / CLAIM_FILE
    if directory.is_symlink() or marker.is_symlink():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == str(task_id)
    except (OSError, UnicodeError):
        return False


def _claim(directory: Path, task_id: Any) -> None:
    """把目录标记为该任务所有。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / CLAIM_FILE).write_text(str(task_id), encoding="utf-8")
    except OSError:
        pass


def _workspace_basename(task: dict[str, Any]) -> str:
    """新标识原样用作 POSIX 目录名，兼容已认领的旧清洗目录，不迁移数据。"""
    name = str(task.get("name") or "")
    legacy = _sanitize_dirname(name)
    if re.fullmatch(r"[A-Za-z_]{1,80}", name):
        # CON、纯下划线在本项目运行的 POSIX 平台上均是合法目录名。
        # 旧版曾将它们映射到 _CON / task，已有认领目录必须继续可访问。
        if legacy != name:
            for base in (legacy, f"{legacy}-{task.get('id')}"):
                candidate = WORKSPACES_DIR / base
                if not candidate.is_symlink() and _claimed_by(candidate, task.get("id")):
                    return base
        return name
    return legacy


def owned_workspace_dirs(task: dict[str, Any]) -> list[Path]:
    """列出可安全清理的任务目录，不认领、不迁移、不跟随软链。"""
    task_id = int(task["id"])
    name = str(task.get("name") or "")
    bases = {_sanitize_dirname(name)}
    if re.fullmatch(r"[A-Za-z_]{1,80}", name):
        bases.add(name)
    legacy = workspace_dir(task_id)
    candidates = {legacy}
    for base in bases:
        candidates.update((WORKSPACES_DIR / base, WORKSPACES_DIR / f"{base}-{task_id}"))
    owned: list[Path] = []
    for directory in sorted(candidates):
        if directory.is_symlink() or not directory.is_dir():
            continue
        marker = directory / CLAIM_FILE
        if marker.is_symlink():
            continue
        if _claimed_by(directory, task_id):
            owned.append(directory)
        elif directory == legacy and not marker.exists() and (directory / ".git").is_dir():
            # 认领标记引入前的 task-id Git checkout；有他人标记时绝不清理。
            owned.append(directory)
    return owned


def workspace_dir_for_task(task: dict[str, Any]) -> Path:
    """以「任务名」命名的工作目录；同名冲突时追加任务 id 保证唯一。

    任务创建/首次运行时会通过认领标记（目录内 ``.autodeploy-owner`` 文件）
    把目录与任务 id 绑定，因此同一任务每次解析结果一致、不同任务目录互不
    相同，且不受其他任务目录存在与否的影响。
    """
    base = _workspace_basename(task)
    task_id = task.get("id")
    candidates = (WORKSPACES_DIR / base, WORKSPACES_DIR / f"{base}-{task_id}")
    # 优先沿用本任务已认领的目录（即使后来基础名字空闲了），拒绝跟随软链。
    for candidate in candidates:
        if not candidate.is_symlink() and _claimed_by(candidate, task_id):
            return candidate
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ValueError("任务工作目录已被其他任务或文件占用，请检查目录归属")


def migrate_workspace_to_name(task: dict[str, Any], *, log: Any = None) -> Path:
    """把 ``task-<id>`` 旧工作目录改名为以任务名命名的新目录。

    返回该任务当前应使用的工作目录。改名是幂等的：旧目录不存在时直接
    返回既有目录。改名前目标位置若已存在且属于其他任务，则改用带任务
    id 后缀的目录，绝不覆盖别人的数据。
    """
    task_id = int(task.get("id") or 0)
    old = WORKSPACES_DIR / f"task-{task_id}"
    old_marker = old / CLAIM_FILE
    if old.is_symlink() or (old.exists() and not old.is_dir()):
        raise ValueError("旧任务工作目录不是安全的真实目录，无法迁移")
    if (old_marker.exists() or old_marker.is_symlink()) and not _claimed_by(old, task_id):
        raise ValueError("旧任务工作目录归属不明或属于其他任务，无法迁移")
    candidate = workspace_dir_for_task(task)
    if old == candidate:
        return candidate
    if old.exists():
        if candidate.exists():
            if _claimed_by(candidate, task_id):
                # 任务名目录已是本任务的（此前迁移过）：保留它，
                # 清掉重复出现的旧目录并返回已认领目录。
                try:
                    old.rmdir()
                except OSError:
                    pass
                return candidate
            # 解析后目录若被外部并发占用，也不覆盖或重新认领。
            raise ValueError("任务工作目录已被占用，无法迁移")
        try:
            old.rename(candidate)
        except OSError:
            if log:
                log(f"! 工作目录改名失败，继续使用 {old.name}")
            return old
        _claim(candidate, task_id)
        if log:
            log(f"工作目录已由 {old.name} 更名为 {candidate.name}")
        return candidate
    # 旧目录不存在：从未运行过，或早已迁移。确保认领标记存在——
    # 首次 clone 前先占住名字；目录被外部删除也能自动重建认领。
    if not candidate.exists() or not _claimed_by(candidate, task_id):
        if not candidate.exists():
            _claim(candidate, task_id)
            return candidate
        if _claimed_by(candidate, task_id):
            return candidate
    return workspace_dir_for_task(task)


def releases_dir(task_id: int) -> Path:
    return RELEASES_DIR / f"task-{task_id}"


def artifacts_dir(task_id: int) -> Path:
    return ARTIFACTS_DIR / f"task-{task_id}"


def temp_dir(task_id: int) -> Path:
    """按任务的临时目录（git 凭据助手文件等）。"""
    return TMP_DIR / f"task-{task_id}"


def owned_storage_dirs(task: dict[str, Any]) -> list[Path]:
    """本任务在数据目录下拥有的持久化目录（不含工作目录）。

    删除任务与「清理任务文件」共用这一份归属定义，避免两处规则漂移后
    一方漏删、另一方误删。
    """
    task_id = int(task["id"])
    return [releases_dir(task_id), artifacts_dir(task_id), temp_dir(task_id)]


def run_log_path(run_id: int) -> Path:
    return LOGS_DIR / f"run-{run_id}.log"


# ---------------------------------------------------------------------------
# 代理
# ---------------------------------------------------------------------------

def proxy_url_with_auth(settings: "Settings") -> str:
    """把账号密码拼进代理 URL（git/curl 都接受这种形式）。

    密码做百分号编码，避免密码里的 ``@`` / ``:`` 破坏 URL 结构。
    """
    url = (settings.proxy_url or "").strip()
    if not url:
        return ""
    user = (settings.proxy_username or "").strip()
    password = settings.proxy_password or ""
    if not user:
        return url
    from urllib.parse import quote

    credentials = quote(user, safe="")
    if password:
        credentials += ":" + quote(password, safe="")
    # 在 scheme:// 之后插入凭证。
    scheme, sep, rest = url.partition("://")
    if not sep:
        return f"{credentials}@{url}"
    return f"{scheme}://{credentials}@{rest}"


def proxy_env(settings: "Settings", *, for_scripts: bool = False) -> dict[str, str]:
    """构造代理相关的环境变量；未启用时返回空 dict。

    ``for_scripts=True`` 时额外尊重「同时用于构建脚本」开关：
    curl/git 这类工具必须走代理，而构建脚本（npm/pip）是否走代理由运维决定，
    内网构建源通常不需要。
    """
    if not settings.proxy_enabled:
        return {}
    if for_scripts and not settings.proxy_for_scripts:
        return {}
    url = proxy_url_with_auth(settings)
    if not url:
        return {}
    env = {
        "http_proxy": url,
        "https_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
    }
    no_proxy = (settings.proxy_no_proxy or "").strip()
    if no_proxy:
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
    return env


def no_proxy_bypassed(host: str, no_proxy: str) -> bool:
    """``no_proxy`` 是否覆盖该主机。

    ``urllib`` 的 ``proxy_bypass`` 只读进程环境变量，而这里的代理可能来自应用
    设置，因此必须自己按 ``no_proxy`` 判断，否则内网更新源会被强行推过外网代理。
    """
    host = (host or "").strip().lower()
    if not host or not no_proxy:
        return False
    for entry in str(no_proxy).split(","):
        entry = entry.strip().lower().lstrip(".")
        if not entry:
            continue
        if entry == "*" or host == entry or host.endswith("." + entry):
            return True
    return False


def env_proxy_url() -> str:
    """进程环境里配置的代理地址；git 会读它，连通性探测也必须一致。

    应用的「网络代理」设置以环境变量下发给 git 子进程，但运维也可能直接在
    systemd 单元里设置 ``http_proxy``。此时 git 会走代理，而探测若直连目标主机
    就会把「走代理能通」误判为不可达。
    """
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def env_no_proxy() -> str:
    for name in ("no_proxy", "NO_PROXY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def describe_proxy(settings: "Settings") -> str:
    """给界面显示的代理描述，绝不包含密码。"""
    if not settings.proxy_enabled:
        return "未启用"
    url = (settings.proxy_url or "").strip()
    if not url:
        return "已启用但未填写地址"
    user = (settings.proxy_username or "").strip()
    if user:
        scheme, sep, rest = url.partition("://")
        host = rest if sep else url
        return f"{scheme}://{user}:***@{host}" if sep else f"{user}:***@{host}"
    return url
