"""Run history, live log tailing and cancellation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config
from ..runner import STATUS_LABELS
from ..service import Service
from .deps import audit, client_ip, current_user, get_service

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Number of trailing log lines the detail view shows and the tail endpoint
# windows over. Both must use the same value: the client's "lines I already
# have" counter is an offset into this window, so a mismatch would duplicate or
# skip output while polling.
LOG_WINDOW_LINES = 2000


def _log_path(run: dict[str, Any]) -> Path | None:
    """Resolve a run's log path, refusing anything outside the log directory."""
    raw = run.get("log_path") or ""
    if not raw:
        return None
    path = Path(raw)
    try:
        resolved = path.resolve()
        logs_root = config.LOGS_DIR.resolve()
    except OSError:
        return None
    if logs_root != resolved.parent and logs_root not in resolved.parents:
        return None
    return resolved


@router.get("")
def list_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query("", description="按状态过滤"),
    task_id: int | None = Query(None),
    search: str = Query("", description="按任务名/提交信息/错误搜索"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    runs = service.store.runs.list_recent(
        limit=limit, offset=offset, status=status, task_id=task_id, search=search
    )
    total = service.store.runs.count(status=status, task_id=task_id, search=search)
    for run in runs:
        run["status_label"] = STATUS_LABELS.get(str(run.get("status")), str(run.get("status")))
        run["is_active"] = run.get("status") in ("queued", "running")
    return {
        "runs": runs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "statuses": [
            {"value": key, "label": label} for key, label in STATUS_LABELS.items()
        ],
    }


@router.get("/{run_id}")
def get_run(
    run_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    run = service.store.runs.get_decoded(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    run["status_label"] = STATUS_LABELS.get(str(run.get("status")), str(run.get("status")))
    run["is_active"] = run.get("status") in ("queued", "running")
    # Fall back to the in-memory tail, which is ahead of the flushed log file.
    # The window must match the tail endpoint's, since the client uses this
    # line count as its `after` offset when polling.
    tail = service.runner.tail(run_id, limit=LOG_WINDOW_LINES)
    if tail is None and run.get("log_path"):
        tail = _read_tail(_log_path(run), lines=LOG_WINDOW_LINES)
    run["log_tail"] = tail or []
    task = service.store.tasks.get_decoded(int(run["task_id"]))
    run["task"] = (
        {k: task.get(k) for k in ("id", "name", "deploy_method", "repo_url", "repo_branch")}
        if task
        else None
    )
    return {"run": run}


def _read_tail(path: Path | None, *, lines: int) -> list[str]:
    """Read the last N lines without loading a large log into memory."""
    if path is None or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, max(4096, lines * 200))
            handle.seek(size - block)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


@router.get("/{run_id}/log")
def get_run_log(
    run_id: int,
    tail: int = Query(0, ge=0, le=5000, description="仅返回最后 N 行"),
    download: bool = Query(False),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> Any:
    run = service.store.runs.get_decoded(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    path = _log_path(run)
    if path is None or not path.is_file():
        # An active run may not have flushed anything yet; show the live tail.
        live = service.runner.tail(run_id, limit=tail or 500)
        if live:
            return PlainTextResponse("\n".join(live), media_type="text/plain; charset=utf-8")
        raise HTTPException(status_code=404, detail="日志文件不存在")
    if download:
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename=f"run-{run_id}.log",
        )
    if tail:
        content = "\n".join(_read_tail(path, lines=tail))
        return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取日志失败: {exc}") from exc
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.get("/{run_id}/tail")
def tail_run(
    run_id: int,
    after: int = Query(0, ge=0, description="已获取的行数，用于增量拉取"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Incremental log polling for the live view.

    ``after`` is the number of lines the client already has, counted against the
    same trailing window returned in ``total``. The server sends whatever
    follows, so the browser only transfers new output.
    """
    run = service.store.runs.get_decoded(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")

    status_payload = {
        "status": run.get("status"),
        "status_label": STATUS_LABELS.get(str(run.get("status")), str(run.get("status"))),
        "duration_ms": run.get("duration_ms"),
        "exit_code": run.get("exit_code"),
    }

    def response(lines: list[str], total: int, *, reset: bool, active: bool) -> dict[str, Any]:
        return {"lines": lines, "total": total, "reset": reset, "active": active, **status_payload}

    # 活跃运行先用轻量计数比对：客户端已追平输出时直接返回空增量，
    # 免去复制整个 2000 行环形缓冲的开销。
    active, live_total = service.runner.tail_meta(run_id)
    if active and after == live_total:
        return response([], live_total, reset=False, active=True)

    if active:
        lines = service.runner.tail(run_id, limit=LOG_WINDOW_LINES) or []
    else:
        lines = _read_tail(_log_path(run), lines=LOG_WINDOW_LINES)

    if after > len(lines):
        # The client holds more lines than the server can see (the in-memory
        # ring buffer was trimmed, or the service restarted). Tell it to reset
        # and resend the whole tail so both sides agree again.
        return response(lines, len(lines), reset=True, active=active)
    # `after == len(lines)` means the client is fully caught up; the slice below
    # then yields an empty list, which is what keeps the live view from
    # duplicating output on every poll.
    return response(lines[after:], len(lines), reset=False, active=active)


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    run = service.store.runs.get_decoded(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if run.get("status") not in ("queued", "running"):
        raise HTTPException(status_code=409, detail="该运行已结束，无法取消")
    ok = service.runner.cancel(run_id)
    audit(
        service, "run_cancelled", actor=user["username"], target=f"run:{run_id}",
        detail="已请求取消" if ok else "取消失败", ip=client_ip(request),
    )
    return {"ok": ok, "message": "已请求取消" if ok else "无法取消该运行"}


@router.delete("/{run_id}")
def delete_run(
    run_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    run = service.store.runs.get_decoded(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if run.get("status") in ("queued", "running"):
        raise HTTPException(status_code=409, detail="运行中的记录不能删除")
    path = _log_path(run)
    service.store.db.execute_rowcount("DELETE FROM runs WHERE id = ?", (run_id,))
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    audit(
        service, "run_deleted", actor=user["username"], target=f"run:{run_id}",
        ip=client_ip(request),
    )
    return {"ok": True}
