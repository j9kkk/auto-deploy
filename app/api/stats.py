"""Statistics, dashboard summary and storage reporting."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import __version__ as VERSION
from .. import config
from .. import selfupdate
from ..selfupdate import ACTIVE_STAGES
from ..deployer import METHOD_LABELS, cleanup_path
from ..executor import redact
from ..runner import STATUS_LABELS
from ..schedule import iso, local_tz, utcnow
from ..service import Service
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/health")
def health(service: Service = Depends(get_service)) -> dict[str, Any]:
    """Unauthenticated liveness probe for systemd / a load balancer."""
    return {
        "status": "ok",
        "service": "autodeploy",
        "version": VERSION,
        "time": iso(utcnow()),
        "timezone_offset_minutes": int(local_tz().utcoffset(datetime.now()).total_seconds() // 60),
        "scheduler_running": service.scheduler.running,
    }


@router.get("/dashboard")
def dashboard(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Everything the dashboard renders, in one request."""
    overview = service.store.runs.stats_overview()
    tasks = service.store.tasks.list_all()

    active_runs = []
    for run in service.store.runs.active():
        run["status_label"] = STATUS_LABELS.get(str(run.get("status")), str(run.get("status")))
        active_runs.append(run)

    enabled = sum(1 for task in tasks if task.get("enabled"))
    failing = sum(1 for task in tasks if task.get("last_status") == "failed")
    now = utcnow()
    upcoming = []
    for task in tasks:
        next_at = task.get("next_run_at")
        if not task.get("enabled") or not next_at:
            continue
        upcoming.append(
            {
                "task_id": task["id"],
                "name": task.get("name"),
                "next_run_at": next_at,
                "schedule_type": task.get("schedule_type"),
                "schedule_expression": task.get("schedule_expression"),
            }
        )
    upcoming.sort(key=lambda item: item["next_run_at"] or "")

    recent = service.store.runs.list_recent(limit=15)
    for run in recent:
        run["status_label"] = STATUS_LABELS.get(str(run.get("status")), str(run.get("status")))
        run["is_active"] = run.get("status") in ("queued", "running")

    return {
        "version": VERSION,
        "now": iso(now),
        "timezone_offset_minutes": int(local_tz().utcoffset(datetime.now()).total_seconds() // 60),
        "overview": overview,
        "tasks": {
            "total": len(tasks),
            "enabled": enabled,
            "disabled": len(tasks) - enabled,
            "failing": failing,
        },
        "scheduler": service.scheduler.status(),
        "active_runs": active_runs,
        "upcoming": upcoming[:8],
        "recent_runs": recent,
        "daily": service.store.runs.stats_daily(14),
    }


@router.get("/stats")
def stats(
    days: int = Query(14, ge=1, le=90),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return {
        "overview": service.store.runs.stats_overview(),
        "daily": service.store.runs.stats_daily(days),
        "by_task": service.store.runs.stats_by_task(limit=50),
        "statuses": [{"value": k, "label": v} for k, v in STATUS_LABELS.items()],
        "method_labels": METHOD_LABELS,
    }


@router.get("/storage")
def storage(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    from ..scheduler import workspace_summary

    report = service.storage_report()
    report["tasks"] = workspace_summary(service.store)
    report["data_dir"] = str(config.DATA_DIR)
    return report


@router.get("/audit")
def audit_log(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    entries = service.store.audit.list_recent(limit=limit, offset=offset)
    return {"entries": entries, "limit": limit, "offset": offset}


@router.get("/sessions")
def sessions(
    user: dict[str, Any] = Depends(current_user),
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    entries = service.store.sessions.list_active()
    for entry in entries:
        entry["is_current"] = int(entry["id"]) == int(user["session_id"])
    return {"sessions": entries}


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Log out every session except the one making the request."""
    current = service.store.sessions.list_active()
    token_session_id = int(user["session_id"])
    revoked = 0
    for entry in current:
        if int(entry["id"]) == token_session_id:
            continue
        if int(entry["user_id"]) != int(user["id"]):
            continue
        service.store.db.execute_rowcount("DELETE FROM sessions WHERE id = ?", (entry["id"],))
        revoked += 1
    audit(service, "sessions_revoked", actor=user["username"], detail=f"撤销 {revoked} 个会话")
    return {"ok": True, "revoked": revoked}


@router.get("/system")
def system_info(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Host and runtime facts shown on the settings page."""
    from ..executor import which

    usage = shutil.disk_usage(str(config.DATA_DIR))
    from ..config import ROOT_DIR

    return {
        "version": VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": platform.node(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "root_dir": str(ROOT_DIR),
        "data_dir": str(config.DATA_DIR),
        "db_path": str(config.DB_PATH),
        "db_size": config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0,
        "uptime_seconds": round(service.scheduler.status().get("last_maintenance_seconds_ago") or 0, 1),
        "binaries": {
            name: which(name) for name in ("git", "tar", "rsync", "docker", "systemctl", "gzip")
        },
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        "scheduler": service.scheduler.status(),
    }


@router.post("/maintenance/run")
def run_maintenance(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Trigger retention and cleanup immediately."""
    report = service.scheduler.maintenance()
    audit(service, "maintenance_run", actor=user["username"], detail=str(report))
    return {"ok": True, "report": report}


@router.post("/maintenance/cleanup-task/{task_id}")
def cleanup_task_files(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a task's workspace, releases and artifacts, keeping the task."""
    row = service.store.tasks.get(task_id)
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="任务不存在")
    if service.store.runs.has_active_for_task(task_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="任务正在运行，无法清理文件")

    removed: list[str] = []
    for path in (
        *config.owned_workspace_dirs(row),
        config.releases_dir(task_id),
        config.artifacts_dir(task_id),
    ):
        if path.exists():
            cleanup_path(path)
            removed.append(str(path))
    audit(service, "task_files_cleaned", actor=user["username"], target=f"task:{task_id}",
          detail=", ".join(removed))
    return {"ok": True, "removed": removed}


@router.get("/export")
def export_all(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Download a JSON snapshot of tasks and recent runs."""
    import json

    from fastapi.responses import PlainTextResponse

    snapshot = service.store.export_snapshot()
    snapshot["version"] = VERSION
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return PlainTextResponse(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="autodeploy-export.json"'},
    )


@router.get("/audit/export")
def export_audit(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> Any:
    import csv
    import io

    from fastapi.responses import PlainTextResponse

    entries = service.store.audit.list_recent(limit=1000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "时间", "操作人", "动作", "对象", "详情", "IP"])
    for entry in entries:
        writer.writerow(
            [
                entry["id"], entry["ts"], entry["actor"], entry["action"],
                entry["target"], entry["detail"], entry["ip"],
            ]
        )
    # A BOM keeps Excel from mangling the Chinese column headers.
    return PlainTextResponse(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


@router.get("/system/update/check")
def check_update(
    force: bool = Query(False, description="跳过缓存强制查询"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """查询是否有新版本。结果缓存 10 分钟，避免频繁请求 GitHub API。"""
    return service.selfupdate.check(force=force).as_dict()


def _upgrade_hint() -> str:
    """按部署形态给出升级途径说明；可自升级时为空。"""
    mode = selfupdate.docker_run_mode()
    if mode == "docker":
        return ""
    if mode == "docker_no_socket":
        return ("容器内未挂载 Docker socket，面板无法自升级。请在服务器执行一键脚本，"
                "或手动：cd 安装目录 && docker compose pull && docker compose up -d")
    if mode == "systemd":
        return ""
    return ("当前运行环境不支持面板自升级（未检测到 systemd 单元或容器形态）。"
            "请使用一键脚本或 docker compose 拉取新镜像升级。")


@router.get("/system/self-update/status")
def self_update_status(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    state = service.selfupdate.state()
    mode = selfupdate.docker_run_mode()
    blocked = service.selfupdate.deployment_blocked()
    return {
        **state,
        "active": state.get("stage") in ACTIVE_STAGES or blocked,
        "blocked": blocked,
        **service.selfupdate.backup_status(),
        "current_version": VERSION,
        "run_mode": mode,
        "docker_available": mode == "docker",
        "upgrade_hint": _upgrade_hint(),
    }


@router.get("/system/self-update/history")
def self_update_history(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """历次更新的日志与可回滚的目标版本。

    「升级日志」按钮的数据来源：每条记录都带该次操作的完整日志与最终结果，
    ``backups`` 是当前仍可回滚的版本清单（按备份校验结果给出，不含路径）；
    Docker 形态的记录额外带 ``from_image``/``to_image``，供界面按镜像 tag 回退。
    """
    return {
        "entries": service.selfupdate.history(),
        "backups": service.selfupdate.backups(),
        "current_version": VERSION,
        "run_mode": selfupdate.docker_run_mode(),
    }


@router.post("/system/self-update")
def start_self_update(
    payload: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]  # FastAPI 按名注入；显式默认 None 仅为审计容错
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    body = payload or {}
    try:
        state = service.selfupdate.start(body.get("target_version"))
    except RuntimeError as exc:
        audit(service, "self_update_rejected", actor=user["username"],
              target="self-update", detail=redact(f"target={body.get('target_version')} error={exc}"),
              ip=client_ip(request) if request else "")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(service, "self_update_started", actor=user["username"],
          target="self-update", detail=f"target={state.get('target_version')}",
          ip=client_ip(request) if request else "")
    return {"ok": True, "state": state}


@router.post("/system/self-update/rollback")
def rollback_self_update(
    payload: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]  # FastAPI 按名注入；显式默认 None 仅为审计容错
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    body = payload or {}
    target = body.get("target_version")
    target_image = body.get("target_image")
    try:
        result = service.selfupdate.rollback(
            target if isinstance(target, str) else None,
            target_image if isinstance(target_image, str) else None,
        )
    except RuntimeError as exc:
        audit(service, "self_update_rejected", actor=user["username"],
              target="self-update", detail=redact(f"rollback target={target or target_image} error={exc}"),
              ip=client_ip(request) if request else "")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(service, "self_update_rollback", actor=user["username"],
          target="self-update",
          detail=redact(f"image={target_image}" if target_image else (f"target={target}" if target else "最近备份")),
          ip=client_ip(request) if request else "")
    return result


@router.post("/system/self-update/attention")
def handle_attention(
    payload: dict[str, Any] | None = None,
    request: Request = None,  # type: ignore[assignment]  # FastAPI 按名注入；显式默认 None 仅为审计容错
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """人工处置结果不确定的升级：继续等待执行器，或确认已手动处理。"""
    body = payload or {}
    action = body.get("action")
    try:
        state = service.selfupdate.attention_action(action if isinstance(action, str) else "")
    except RuntimeError as exc:
        audit(service, "self_update_attention_rejected", actor=user["username"],
              target="self-update", detail=redact(f"action={action} error={exc}"),
              ip=client_ip(request) if request else "")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(service, "self_update_attention", actor=user["username"],
          target="self-update", detail=f"action={action}",
          ip=client_ip(request) if request else "")
    return {"ok": True, "state": state}
