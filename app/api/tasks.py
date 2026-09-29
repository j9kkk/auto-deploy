"""Task management routes: CRUD, manual runs, cancellation and rollback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config
from ..deployer import (
    METHOD_LABELS,
    METHOD_BINARIES,
    REQUIRED_BINARIES,
    cleanup_path,
    current_release,
    directory_size,
    disk_usage,
    list_releases,
    preflight,
    resolve_release_paths,
    rollback_task,
)
from ..schedule import iso, next_run_time, utcnow
from ..service import Service
from ..store import decode_task
from ..validation import (
    ValidationError,
    describe_task_schedule,
    validate_task_payload,
)
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _decorate(task: dict[str, Any], service: Service) -> dict[str, Any]:
    """Add derived fields the UI needs but the table does not store."""
    if task is None:
        return {}
    enriched = dict(task)
    enriched["schedule_description"] = describe_task_schedule(task)
    enriched["method_label"] = METHOD_LABELS.get(
        str(task.get("deploy_method") or ""), str(task.get("deploy_method") or "")
    )
    enriched["active_run"] = None
    runs = service.store.runs.list_for_task(int(task["id"]), limit=1)
    if runs:
        enriched["last_run"] = runs[0]
    # Report whether the checkout exists so the UI can offer a first run hint.
    workspace = config.workspace_dir_for_task(task)
    if not (workspace / ".git").exists():
        legacy = config.workspace_dir(int(task["id"]))
        if (legacy / ".git").exists():
            workspace = legacy
    enriched["workspace_exists"] = (workspace / ".git").exists()
    enriched["workspace"] = str(workspace)
    enriched["releases_root"], enriched["current_link"] = (
        str(part) for part in resolve_release_paths(task)
    )
    return enriched


@router.get("")
def list_tasks(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    tasks = [_decorate(decode_task(row) or {}, service) for row in service.store.tasks.list_all()]
    active_by_task: dict[int, dict[str, Any]] = {}
    for run in service.store.runs.active():
        active_by_task.setdefault(int(run["task_id"]), run)
    for task in tasks:
        active = active_by_task.get(int(task["id"]))
        if active:
            task["active_run"] = active
    return {"tasks": tasks, "total": len(tasks)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        body = validate_task_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    # 任务名全局唯一：它同时用作工作目录名与各处展示标识。
    if service.store.tasks.name_taken(body.get("name", "")):
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败", "errors": {"name": "任务名已被使用，请换一个名称"}},
        )

    task_id = service.store.tasks.create(body)
    service.scheduler.reschedule(task_id)
    task = _decorate(decode_task(service.store.tasks.get(task_id)) or {}, service)
    audit(
        service, "task_created", actor=user["username"],
        target=f"task:{task_id}", detail=body.get("name", ""), ip=client_ip(request),
    )

    result: dict[str, Any] = {"task": task}
    if body.get("run_on_create"):
        try:
            run_id = service.scheduler.run_now(task_id, trigger="manual")
        except RuntimeError as exc:
            result["warning"] = f"任务已创建，但未开始部署：{exc}"
            return result
        result["run_id"] = run_id
        audit(service, "run_triggered", actor=user["username"], target=f"task:{task_id}",
              detail=f"run:{run_id} 创建后立即运行", ip=client_ip(request))
    return result


@router.get("/{task_id}")
def get_task(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = _decorate(decode_task(row) or {}, service)
    releases_root, current_link = resolve_release_paths(row)
    releases = [
        {
            "name": release.name,
            "path": str(release),
            "size": directory_size(release, limit_entries=20_000),
            "is_current": current_release(releases_root, current_link) == release,
        }
        for release in list_releases(releases_root)[:50]
    ]
    return {
        "task": task,
        "releases": releases,
        "preflight": preflight(row),
        "runs": service.store.runs.list_for_task(task_id, limit=20),
    }


@router.put("/{task_id}")
@router.patch("/{task_id}")
def update_task(
    task_id: int,
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    existing = service.store.tasks.get_decoded(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        # A partial update leaves unmentioned fields alone, which is what lets
        # the edit form save without re-sending the git token.
        body = validate_task_payload(payload, partial=True, existing=existing)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    # 改名不得与其它任务重名（自己除外）。
    if "name" in body and service.store.tasks.name_taken(body["name"], exclude_id=task_id):
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败", "errors": {"name": "任务名已被其他任务使用"}},
        )

    if not service.store.tasks.update(task_id, body):
        # Nothing changed, which is not an error.
        pass
    service.scheduler.reschedule(task_id)
    task = _decorate(decode_task(service.store.tasks.get(task_id)) or {}, service)
    changed = sorted(body.keys())
    audit(
        service, "task_updated", actor=user["username"], target=f"task:{task_id}",
        detail=f"字段: {', '.join(changed)}", ip=client_ip(request),
    )
    return {"task": task, "changed": changed}


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    request: Request,
    purge: bool = Query(True, description="同时删除工作目录与发布产物"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if service.store.runs.has_active_for_task(task_id):
        raise HTTPException(status_code=409, detail="任务正在运行，请先取消后再删除")

    name = row.get("name", "")
    # Collect log paths before the delete: the runs table cascades on task
    # removal, so afterwards there is nothing left to find them from.
    log_paths = [
        Path(run["log_path"])
        for run in service.store.runs.list_for_task(task_id, limit=10_000)
        if run.get("log_path")
    ]
    service.store.tasks.delete(task_id)
    purged: list[str] = []
    if purge:
        targets = [
            config.workspace_dir(task_id),
            # 名字目录可能带或不带 -id 后缀，两种形态都清理。
            config.WORKSPACES_DIR / config._sanitize_dirname(str(row.get("name") or "")),
            config.WORKSPACES_DIR / f"{config._sanitize_dirname(str(row.get('name') or ''))}-{task_id}",
            config.releases_dir(task_id),
            config.artifacts_dir(task_id),
        ]
        target_dir = (row.get("target_dir") or "").strip()
        for path in targets:
            if path.exists():
                cleanup_path(path)
                purged.append(str(path))
        for log_path in log_paths:
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                continue
        if target_dir:
            purged.append(f"保留 {target_dir}（目标目录未删除）")
    audit(
        service, "task_deleted", actor=user["username"], target=f"task:{task_id}",
        detail=f"{name} purge={purge}", ip=client_ip(request),
    )
    return {"ok": True, "purged": purged}


@router.post("/{task_id}/run")
def run_task(
    task_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if service.store.runs.count_active() >= service.settings.max_global_workers:
        raise HTTPException(
            status_code=429,
            detail=f"并发运行数已达上限（{service.settings.max_global_workers}），请稍后再试",
        )
    try:
        run_id = service.scheduler.run_now(task_id, trigger="manual")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run_id is None:
        raise HTTPException(status_code=500, detail="无法创建运行记录")
    audit(
        service, "run_triggered", actor=user["username"], target=f"task:{task_id}",
        detail=f"run:{run_id}", ip=client_ip(request),
    )
    return {"ok": True, "run_id": run_id}


@router.post("/{task_id}/toggle")
def toggle_task(
    task_id: int,
    request: Request,
    payload: dict[str, Any] | None = None,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    body = payload or {}
    enabled = body.get("enabled")
    if enabled is None:
        enabled = not bool(row.get("enabled"))
    enabled = bool(enabled)
    next_at = None
    if enabled:
        try:
            following = next_run_time(
                str(row.get("schedule_type") or "manual"),
                str(row.get("schedule_expression") or ""),
                reference=utcnow(),
                enabled=True,
            )
        except Exception:  # noqa: BLE001 - an invalid schedule simply has no next run
            following = None
        next_at = iso(following)
    service.store.tasks.set_enabled(task_id, enabled, next_at)
    service.scheduler.poke()
    audit(
        service, "task_toggled", actor=user["username"], target=f"task:{task_id}",
        detail=f"enabled={enabled}", ip=client_ip(request),
    )
    return {"ok": True, "enabled": enabled, "next_run_at": next_at}


@router.post("/{task_id}/cancel")
def cancel_task_run(
    task_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    active = [
        run for run in service.store.runs.active() if int(run["task_id"]) == task_id
    ]
    if not active:
        raise HTTPException(status_code=409, detail="该任务当前没有运行中的部署")
    cancelled: list[int] = []
    for run in active:
        if service.runner.cancel(int(run["id"])):
            cancelled.append(int(run["id"]))
    audit(
        service, "run_cancelled", actor=user["username"], target=f"task:{task_id}",
        detail=f"runs: {cancelled}", ip=client_ip(request),
    )
    return {"ok": True, "cancelled": cancelled}


@router.post("/{task_id}/rollback")
def rollback(
    task_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if service.store.runs.has_active_for_task(task_id):
        raise HTTPException(status_code=409, detail="任务正在运行，无法回滚")

    lines: list[str] = []
    with service.scheduler._lock:
        if service.selfupdate.deployment_blocked():
            raise HTTPException(status_code=409, detail="系统正在更新或回滚，暂不能回滚部署")
        ok, message = rollback_task(
            row,
            log=lines.append,
            timeout=min(600, int(row.get("timeout_seconds") or 300)),
            kill_grace_seconds=service.settings.kill_grace_seconds,
        )
    audit(
        service, "rollback", actor=user["username"], target=f"task:{task_id}",
        detail=message, ip=client_ip(request),
    )
    if not ok:
        raise HTTPException(status_code=400, detail={"message": message, "log": lines})
    return {"ok": True, "message": message, "log": lines}


@router.get("/{task_id}/preflight")
def task_preflight(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    releases_root, current_link = resolve_release_paths(row)
    return {
        "checks": preflight(row),
        "required_binaries": sorted({*REQUIRED_BINARIES, *METHOD_BINARIES.values()}),
        "releases_root": str(releases_root),
        "current_link": str(current_link),
        "current_target": (
            str(current_release(releases_root, current_link) or "")
        ),
        "workspace": str(config.workspace_dir_for_task(decode_task(row) or {})),
        "disk": disk_usage(config.DATA_DIR),
        "next_run_at": row.get("next_run_at"),
        "schedule_description": describe_task_schedule(decode_task(row) or {}),
    }


@router.get("/{task_id}/releases")
def task_releases(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    releases_root, current_link = resolve_release_paths(row)
    active = current_release(releases_root, current_link)
    return {
        "releases": [
            {
                "name": release.name,
                "path": str(release),
                "modified_at": iso(_mtime(release)),
                "size": directory_size(release, limit_entries=20_000),
                "is_current": release == active,
            }
            for release in list_releases(releases_root)[:100]
        ],
        "current": active.name if active else "",
        "root": str(releases_root),
    }


def _mtime(path: Path):
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
    except OSError:
        return None


@router.get("/{task_id}/export")
def export_task(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> Any:
    """Download a task definition as JSON, with the token omitted."""
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = decode_task(row) or {}
    task.pop("git_token", None)
    payload = json.dumps({"version": 1, "task": task}, ensure_ascii=False, indent=2)
    filename = f"task-{task_id}.json"
    return PlainTextResponse(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{task_id}/runs")
def task_runs(
    task_id: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    runs = service.store.runs.list_for_task(task_id, limit=limit, offset=offset)
    total = service.store.runs.count(task_id=task_id)
    return {"runs": runs, "total": total, "limit": limit, "offset": offset}


@router.get("/{task_id}/artifacts/{filename}")
def download_artifact(
    task_id: int,
    filename: str,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> Any:
    """Serve a packaged bundle, refusing any path that escapes the task dir."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="非法的文件名")
    root = config.artifacts_dir(task_id).resolve()
    candidate = (root / filename).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(
        candidate,
        media_type="application/gzip",
        filename=filename,
    )


@router.get("/{task_id}/artifacts")
def list_artifacts(
    task_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    root = config.artifacts_dir(task_id)
    artifacts = []
    if root.exists():
        for path in sorted(root.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file():
                artifacts.append(
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "modified_at": iso(_mtime(path)),
                        "url": f"/api/tasks/{task_id}/artifacts/{path.name}",
                    }
                )
    return {"artifacts": artifacts}
