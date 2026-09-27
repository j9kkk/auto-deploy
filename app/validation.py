"""Request validation for the API.

Validation lives here rather than in the route handlers so that every rule has
one home and can be unit-tested directly.  Each ``validate_*`` function returns
``(cleaned_payload, errors)``: an empty error list means the payload is safe to
store.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from . import deployer, gitops
from .config import Settings
from .schedule import describe_schedule, validate_schedule
from .security import password_problem

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
TASK_NAME_MAX = 120
SCRIPT_MAX = 200_000

DEPLOY_METHODS = set(deployer.DEPLOY_METHODS)
SCHEDULE_TYPES = {"manual", "interval", "cron"}


class ValidationError(Exception):
    """Raised when a payload cannot be coerced into something storable."""

    def __init__(self, errors: Mapping[str, str] | list[str]) -> None:
        if isinstance(errors, Mapping):
            self.errors: dict[str, str] = {str(k): str(v) for k, v in errors.items()}
        else:
            self.errors = {f"field{index}": str(item) for index, item in enumerate(errors)}
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))

    def to_dict(self) -> dict[str, Any]:
        return {"message": "参数校验失败", "errors": self.errors}


def _clean_str(value: Any, *, limit: int = 500) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def _clean_int(value: Any, default: int, *, low: int, high: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def validate_login(payload: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    username = _clean_str(payload.get("username"), limit=64)
    password = str(payload.get("password") or "")
    if not username:
        errors["username"] = "请输入用户名"
    if not password:
        errors["password"] = "请输入密码"
    if len(password) > 4096:
        errors["password"] = "密码长度异常"
    if errors:
        raise ValidationError(errors)
    return {"username": username, "password": password}


def validate_password_change(payload: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    current = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    confirm = str(payload.get("confirm_password") or "")
    if not current:
        errors["current_password"] = "请输入当前密码"
    if not new_password:
        errors["new_password"] = "请输入新密码"
    elif confirm and new_password != confirm:
        errors["confirm_password"] = "两次输入的新密码不一致"
    else:
        problem = password_problem(new_password)
        if problem:
            errors["new_password"] = problem
    if new_password and new_password == current:
        errors["new_password"] = "新密码不能与当前密码相同"
    if errors:
        raise ValidationError(errors)
    return {"current_password": current, "new_password": new_password}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def validate_task_payload(
    payload: Mapping[str, Any], *, partial: bool = False, existing: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate a task create/update body.

    With ``partial=True`` (a PATCH-style update) only the keys present in the
    payload are validated, and absent keys are left untouched — which is what
    lets the UI save a form that deliberately omits the git token.
    """
    existing = dict(existing or {})
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}

    def has(key: str) -> bool:
        return key in payload

    # --- identity ------------------------------------------------------
    if has("name") or not partial:
        name = _clean_str(payload.get("name") or existing.get("name"), limit=TASK_NAME_MAX)
        if not name:
            errors["name"] = "任务名称不能为空"
        out["name"] = name
    if has("description") or not partial:
        out["description"] = _clean_str(
            payload.get("description", existing.get("description", "")), limit=2000
        )

    # --- repository ----------------------------------------------------
    if has("repo_url") or not partial:
        repo_url = _clean_str(payload.get("repo_url") or existing.get("repo_url"), limit=1000)
        problem = gitops.validate_repo_url(repo_url)
        if problem:
            errors["repo_url"] = problem
        out["repo_url"] = repo_url
    if has("repo_branch") or not partial:
        branch = _clean_str(payload.get("repo_branch") or existing.get("repo_branch") or "main", limit=200)
        problem = gitops.validate_branch(branch)
        if problem:
            errors["repo_branch"] = problem
        out["repo_branch"] = branch
    if has("repo_subdir") or not partial:
        subdir = _clean_str(payload.get("repo_subdir", existing.get("repo_subdir", "")), limit=500)
        if subdir:
            problem = deployer.validate_relative_path(subdir, field_name="仓库子目录")
            if problem:
                errors["repo_subdir"] = problem
        out["repo_subdir"] = subdir.strip("/")
    if has("git_depth") or not partial:
        out["git_depth"] = _clean_int(
            payload.get("git_depth", existing.get("git_depth", 1)), 1, low=0, high=10_000
        )
    if has("git_username") or not partial:
        out["git_username"] = _clean_str(
            payload.get("git_username", existing.get("git_username", "")), limit=200
        )
    if has("git_token"):
        token = _clean_str(payload.get("git_token"), limit=2000)
        # An empty value that is explicitly sent means "keep what is stored",
        # so clearing a token requires the separate clear_token flag.
        if token or _clean_bool(payload.get("clear_token"), False):
            out["git_token"] = token
    elif not partial:
        out["git_token"] = ""

    # --- schedule -------------------------------------------------------
    if has("schedule_type") or not partial:
        schedule_type = _clean_str(
            payload.get("schedule_type") or existing.get("schedule_type") or "interval", limit=20
        ).lower()
        if schedule_type not in SCHEDULE_TYPES:
            errors["schedule_type"] = f"调度类型必须是 {sorted(SCHEDULE_TYPES)} 之一"
        out["schedule_type"] = schedule_type
    if has("schedule_expression") or not partial:
        expression = _clean_str(
            payload.get("schedule_expression", existing.get("schedule_expression", "1h")),
            limit=200,
        )
        out["schedule_expression"] = expression
    if has("enabled") or not partial:
        out["enabled"] = _clean_bool(payload.get("enabled", existing.get("enabled", True)), True)

    # Only a full schedule pair can be checked; a partial update checks the
    # merged result so a schedule type change without an expression still fails.
    merged_type = out.get("schedule_type", existing.get("schedule_type", "interval"))
    merged_expr = out.get("schedule_expression", existing.get("schedule_expression", "1h"))
    if merged_type in {"interval", "cron"}:
        problem = validate_schedule(merged_type, merged_expr)
        if problem:
            key = "schedule_expression" if "schedule_expression" in out else "schedule_type"
            errors.setdefault(key, problem)

    # --- deploy ---------------------------------------------------------
    if has("deploy_method") or not partial:
        method = _clean_str(
            payload.get("deploy_method") or existing.get("deploy_method") or "script", limit=40
        ).lower()
        if method not in DEPLOY_METHODS:
            errors["deploy_method"] = f"部署方式必须是 {sorted(DEPLOY_METHODS)} 之一"
        out["deploy_method"] = method

    for field, label in (
        ("prepare_script", "准备脚本"),
        ("deploy_script", "部署脚本"),
        ("rollback_script", "回滚脚本"),
    ):
        if has(field) or not partial:
            script = str(payload.get(field, existing.get(field, "")) or "")
            if len(script) > SCRIPT_MAX:
                errors[field] = f"{label}过长（上限 {SCRIPT_MAX} 字符）"
            out[field] = script

    if has("artifact_paths") or not partial:
        raw_paths = payload.get("artifact_paths", existing.get("artifact_paths", ""))
        patterns = deployer.parse_path_list(raw_paths)
        for pattern in patterns:
            problem = deployer.validate_relative_path(pattern, field_name="打包路径")
            if problem:
                errors["artifact_paths"] = problem
                break
        # Stored as newline-separated text so the textarea round-trips cleanly.
        out["artifact_paths"] = "\n".join(patterns)

    if has("target_dir") or not partial:
        target = _clean_str(payload.get("target_dir", existing.get("target_dir", "")), limit=1000)
        if target and not target.startswith(("/", "~")):
            errors["target_dir"] = "目标目录必须是绝对路径（以 / 开头）"
        out["target_dir"] = target

    if has("keep_releases") or not partial:
        out["keep_releases"] = _clean_int(
            payload.get("keep_releases", existing.get("keep_releases", 5)), 5, low=0, high=1000
        )

    if has("service_name") or not partial:
        service = _clean_str(
            payload.get("service_name", existing.get("service_name", "")), limit=200
        )
        if service and not re.fullmatch(r"[A-Za-z0-9@._:\\-]+", service):
            errors["service_name"] = "服务名称包含非法字符"
        out["service_name"] = service

    if has("docker_image") or not partial:
        image = _clean_str(payload.get("docker_image", existing.get("docker_image", "")), limit=300)
        if image and not re.fullmatch(r"[A-Za-z0-9._:/@-]+", image):
            errors["docker_image"] = "镜像名称包含非法字符"
        out["docker_image"] = image

    if has("docker_command") or not partial:
        out["docker_command"] = str(
            payload.get("docker_command", existing.get("docker_command", "")) or ""
        )
    if has("docker_compose_file") or not partial:
        compose = _clean_str(
            payload.get("docker_compose_file", existing.get("docker_compose_file", "")), limit=300
        )
        if compose:
            problem = deployer.validate_relative_path(compose, field_name="compose 文件")
            if problem:
                errors["docker_compose_file"] = problem
        out["docker_compose_file"] = compose

    if has("rsync_target") or not partial:
        rsync_target = _clean_str(
            payload.get("rsync_target", existing.get("rsync_target", "")), limit=1000
        )
        if rsync_target.startswith("-"):
            errors["rsync_target"] = "rsync 目标不能以 - 开头"
        out["rsync_target"] = rsync_target

    if has("rsync_options") or not partial:
        options = _clean_str(
            payload.get("rsync_options", existing.get("rsync_options", "-az --delete")), limit=500
        )
        if "\n" in options:
            errors["rsync_options"] = "rsync 参数不能包含换行"
        out["rsync_options"] = options

    # --- runtime --------------------------------------------------------
    if has("env_vars") or not partial:
        env, env_error = _normalise_env_vars(payload.get("env_vars", existing.get("env_vars", {})))
        if env_error:
            errors["env_vars"] = env_error
        out["env_vars"] = env

    if has("timeout_seconds") or not partial:
        out["timeout_seconds"] = _clean_int(
            payload.get("timeout_seconds", existing.get("timeout_seconds", 1800)),
            1800, low=10, high=86_400,
        )
    if has("notify_webhook") or not partial:
        webhook = _clean_str(
            payload.get("notify_webhook", existing.get("notify_webhook", "")), limit=1000
        )
        if webhook and not webhook.startswith(("http://", "https://")):
            errors["notify_webhook"] = "通知地址必须以 http:// 或 https:// 开头"
        out["notify_webhook"] = webhook
    if has("skip_if_no_changes") or not partial:
        out["skip_if_no_changes"] = _clean_bool(
            payload.get("skip_if_no_changes", existing.get("skip_if_no_changes", True)), True
        )
    if has("run_on_create") or not partial:
        out["run_on_create"] = _clean_bool(
            payload.get("run_on_create", existing.get("run_on_create", False)), False
        )

    # Method-specific required fields.
    method = out.get("deploy_method", existing.get("deploy_method", "script"))
    if not partial or "deploy_method" in payload:
        _check_method_requirements(method, out, existing, errors)

    if errors:
        raise ValidationError(errors)
    return out


def _check_method_requirements(
    method: str,
    out: dict[str, Any],
    existing: Mapping[str, Any],
    errors: dict[str, str],
) -> None:
    """Ensure the fields a deploy method needs are present."""

    def value(key: str) -> str:
        return str(out.get(key, existing.get(key, "")) or "").strip()

    if method == "script" and not value("deploy_script"):
        errors["deploy_script"] = "部署方式为自定义脚本时必须填写部署脚本"
    if method == "systemd" and not value("service_name"):
        errors["service_name"] = "部署方式为 systemd 时必须填写服务名称"
    if method == "docker" and not value("docker_image"):
        errors["docker_image"] = "部署方式为 Docker 时必须填写镜像名称"
    if method == "rsync" and not value("rsync_target"):
        errors["rsync_target"] = "部署方式为 rsync 时必须填写目标地址"


def _normalise_env_vars(raw: Any) -> tuple[dict[str, str], str | None]:
    """Accept a dict or ``KEY=value`` text and return a plain string map."""
    if raw in (None, ""):
        return {}, None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = _parse_lines(text)
            if parsed is None:
                return {}, "环境变量格式无法解析，请使用 KEY=value 每行一条"
            return parsed, None
        raw = parsed
    if not isinstance(raw, Mapping):
        return {}, "环境变量必须是键值对象或 KEY=value 文本"
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return {}, f"环境变量名非法: {name}"
        cleaned[name] = "" if value is None else str(value)
    if len(cleaned) > 200:
        return {}, "环境变量数量过多（上限 200）"
    return cleaned, None


def _parse_lines(text: str) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            return None
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            return None
        result[key] = value.strip().strip('"').strip("'")
    return result


def describe_task_schedule(task: Mapping[str, Any]) -> str:
    return describe_schedule(
        str(task.get("schedule_type") or "manual"),
        str(task.get("schedule_expression") or ""),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Fields the settings UI may change, with their accepted ranges.
EDITABLE_SETTINGS: dict[str, tuple[type, int, int]] = {
    "session_ttl_hours": (int, 1, 24 * 30),
    "login_max_attempts": (int, 3, 50),
    "login_lockout_seconds": (int, 10, 86_400),
    "poll_interval_seconds": (int, 1, 300),
    "max_global_workers": (int, 1, 32),
    "default_timeout_seconds": (int, 10, 86_400),
    "git_timeout_seconds": (int, 30, 86_400),
    "kill_grace_seconds": (int, 1, 300),
    "run_retention_days": (int, 0, 3650),
    "run_retention_count": (int, 0, 1_000_000),
    "release_retention_count": (int, 0, 1000),
    "artifact_retention_count": (int, 0, 1000),
    "log_retention_days": (int, 0, 3650),
    "log_max_bytes": (int, 100_000, 1_000_000_000),
}

EDITABLE_BOOL_SETTINGS = ("trust_proxy_headers", "secure_cookies", "queue_while_running")


def validate_settings(payload: Mapping[str, Any], current: Settings) -> dict[str, Any]:
    """Validate a settings patch against the editable allow-list."""
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}

    for key, (_, low, high) in EDITABLE_SETTINGS.items():
        if key not in payload:
            continue
        raw = payload[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            errors[key] = "必须是整数"
            continue
        if value < low or value > high:
            errors[key] = f"取值需在 {low} 到 {high} 之间"
            continue
        out[key] = value

    for key in EDITABLE_BOOL_SETTINGS:
        if key in payload:
            out[key] = _clean_bool(payload[key], bool(getattr(current, key, False)))

    if "timezone" in payload:
        timezone = _clean_str(payload["timezone"], limit=64)
        if timezone and not re.fullmatch(r"[A-Za-z0-9_+\-/]+", timezone):
            errors["timezone"] = "时区名称包含非法字符"
        out["timezone"] = timezone

    if "notify_webhook" in payload:
        webhook = _clean_str(payload["notify_webhook"], limit=1000)
        if webhook and not webhook.startswith(("http://", "https://")):
            errors["notify_webhook"] = "通知地址必须以 http:// 或 https:// 开头"
        out["notify_webhook"] = webhook

    if "default_branch" in payload:
        branch = _clean_str(payload["default_branch"], limit=200) or "main"
        problem = gitops.validate_branch(branch)
        if problem:
            errors["default_branch"] = problem
        out["default_branch"] = branch

    if "shell" in payload:
        shell = _clean_str(payload["shell"], limit=300) or "/bin/bash"
        if not shell.startswith("/"):
            errors["shell"] = "shell 必须是绝对路径"
        out["shell"] = shell

    # A worker count above the retention-independent limit is fine, but the
    # poll interval must stay below the smallest useful interval.
    if errors:
        raise ValidationError(errors)
    return out


def validate_username(username: Any) -> str:
    text = _clean_str(username, limit=64)
    if not USERNAME_RE.fullmatch(text):
        raise ValidationError(
            {"username": "用户名需为 3-64 位字母、数字或 . _ @ -"}
        )
    return text
