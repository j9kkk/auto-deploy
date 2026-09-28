"""Settings and schedule-preview routes (admin-only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import config
from ..schedule import (
    describe_schedule,
    parse_cron,
    parse_interval,
    validate_schedule,
)
from ..service import Service
from ..validation import (
    EDITABLE_BOOL_SETTINGS,
    EDITABLE_SETTINGS,
    ValidationError,
    validate_settings,
)
from .deps import audit, client_ip, get_service, require_admin

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    settings = service.export_settings()
    return {
        "settings": settings,
        "editable": {
            key: {"type": "int", "min": low, "max": high}
            for key, (_, low, high) in EDITABLE_SETTINGS.items()
        },
        "editable_bools": list(EDITABLE_BOOL_SETTINGS),
        "readonly": ["host", "port", "base_url", "shell", "timezone"],
        "proxy_fields": [
            "proxy_enabled", "proxy_url", "proxy_username",
            "proxy_password", "proxy_no_proxy", "proxy_for_scripts",
        ],
        "proxy_description": config.describe_proxy(service.settings),
        "config_path": str(config.settings_path()),
        "data_dir": str(config.DATA_DIR),
    }


@router.put("")
@router.patch("")
def update_settings(
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        patch = validate_settings(payload, service.settings)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    if not patch:
        return {"settings": service.export_settings(), "changed": []}

    service.save_settings(patch)
    audit(
        service, "settings_updated", actor=user["username"],
        detail=f"字段: {', '.join(sorted(patch.keys()))}", ip=client_ip(request),
    )
    # Some changes (host/port) only take effect after a restart.
    restart_fields = {"host", "port", "base_url", "shell", "secure_cookies"}
    needs_restart = bool(restart_fields & set(patch.keys()))
    return {
        "settings": service.export_settings(),
        "changed": sorted(patch.keys()),
        "restart_required": needs_restart,
    }


@router.post("/schedule/preview")
def preview_schedule(
    payload: dict[str, Any],
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Validate a schedule and show the next few fire times."""
    schedule_type = str(payload.get("schedule_type") or "interval").strip().lower()
    expression = str(payload.get("schedule_expression") or "").strip()
    count = int(payload.get("count") or 5)
    count = max(1, min(20, count))

    problem = validate_schedule(schedule_type, expression)
    if problem:
        return {"ok": False, "error": problem, "next_runs": []}

    return {
        "ok": True,
        "description": describe_schedule(schedule_type, expression),
        "next_runs": service.scheduler.preview(schedule_type, expression, count),
        "error": "",
    }


@router.post("/schedule/validate")
def validate_schedule_expression(
    payload: dict[str, Any],
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    schedule_type = str(payload.get("schedule_type") or "interval").strip().lower()
    expression = str(payload.get("schedule_expression") or "").strip()
    problem = validate_schedule(schedule_type, expression)
    details: dict[str, Any] = {"ok": problem is None, "error": problem or ""}
    if problem is None and schedule_type == "interval":
        details["seconds"] = parse_interval(expression)
    if problem is None and schedule_type == "cron":
        cron = parse_cron(expression)
        details["minutes"] = sorted(cron.minutes)
        details["hours"] = sorted(cron.hours)
    if problem is None:
        details["description"] = describe_schedule(schedule_type, expression)
    return details


@router.get("/defaults")
def defaults(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Defaults used to pre-fill a new task form."""
    settings = service.settings
    from ..deployer import DEPLOY_METHODS, METHOD_LABELS
    from ..validation import SCHEDULE_TYPES

    return {
        "schedule_types": sorted(SCHEDULE_TYPES),
        "deploy_methods": [
            {"value": method, "label": METHOD_LABELS.get(method, method)}
            for method in DEPLOY_METHODS
        ],
        "repo_branch": settings.default_branch,
        "timeout_seconds": settings.default_timeout_seconds,
        "keep_releases": settings.release_retention_count,
        "schedule_defaults": {"interval": "1h", "cron": "0 */6 * * *"},
    }
