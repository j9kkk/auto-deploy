"""Global configuration and filesystem layout for AutoDeploy.

Precedence for every setting is: built-in default < data/config.json < environment
variable.  The JSON file is the one edited from the web UI, so it must win over
defaults; environment variables win over everything so that operators can pin a
value in the systemd unit without the UI silently overriding it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent


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

    # --- retention --------------------------------------------------------
    run_retention_days: int = 30
    run_retention_count: int = 500
    release_retention_count: int = 5
    artifact_retention_count: int = 5
    log_retention_days: int = 14
    # A single run log is truncated after this many bytes to protect the disk.
    log_max_bytes: int = 8_000_000

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


def load_settings() -> Settings:
    """Build the effective settings object from file + environment."""
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
    return WORKSPACES_DIR / f"task-{task_id}"


def releases_dir(task_id: int) -> Path:
    return RELEASES_DIR / f"task-{task_id}"


def artifacts_dir(task_id: int) -> Path:
    return ARTIFACTS_DIR / f"task-{task_id}"


def run_log_path(run_id: int) -> Path:
    return LOGS_DIR / f"run-{run_id}.log"
