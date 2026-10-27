"""Task management routes: CRUD, manual runs, cancellation and rollback."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config
from ..deployer import (
    METHOD_LABELS,
    METHOD_BINARIES,
    REPO_FREE_METHODS,
    REQUIRED_BINARIES,
    cleanup_path,
    current_release,
    directory_size,
    disk_usage,
    list_releases,
    preflight,
    purge_target_release_history,
    resolve_release_paths,
    stop_task_containers,
)
from ..schedule import iso, next_run_time, utcnow
from ..security import new_token
from ..service import Service
from ..remote import RemoteError
from ..publicurl import external_base_url
from ..store import TaskNameConflict, decode_task
from ..target import TargetError, build_target, resolve_remote_layout
from ..validation import (
    ValidationError,
    describe_host,
    describe_task_schedule,
    validate_task_payload,
)
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_UNSET = object()


def _decorate(
    task: dict[str, Any],
    service: Service,
    request: Request | None = None,
    *,
    last_run: Any = _UNSET,
) -> dict[str, Any]:
    """Add derived fields the UI needs but the table does not store.

    ``last_run`` 由调用方批量预取（列表页一次查询取回全部任务的最近运行）；
    未预取时才逐任务回查，保持单任务装饰路径不变。
    """
    if task is None:
        return {}
    enriched = dict(task)
    enriched["schedule_description"] = describe_task_schedule(task)
    enriched["method_label"] = METHOD_LABELS.get(
        str(task.get("deploy_method") or ""), str(task.get("deploy_method") or "")
    )
    # 触发地址要交给外部系统访问：优先用设置里的外网访问地址，其次反代
    # 转发头，最后回退到请求自身地址（见 publicurl.external_base_url）。
    secret = str(task.get("webhook_secret") or "")
    if secret and task.get("id") is not None:
        base = external_base_url(request, service.settings)
        enriched["webhook_url"] = f"{base}/api/webhooks/{task['id']}/{secret}" if base else ""
    else:
        enriched["webhook_url"] = ""
    enriched["active_run"] = None
    # 目标主机的展示标签：本机 / user@addr，或「已删除的主机 #N」。
    enriched["host_label"] = "本机"
    enriched["host_missing"] = False
    host_id = task.get("host_id")
    if host_id:
        host_row = service.store.hosts.get(int(host_id))
        if host_row is None:
            enriched["host_label"] = f"已删除的主机 #{host_id}"
            enriched["host_missing"] = True
        else:
            host = describe_host(host_row)
            enriched["host_label"] = str(host_row.get("name") or "") or host
            enriched["host_target"] = host
    if last_run is _UNSET:
        runs = service.store.runs.list_for_task(int(task["id"]), limit=1)
        last_run = runs[0] if runs else None
    enriched["last_run"] = last_run
    # Report whether the checkout exists so the UI can offer a first run hint.
    if str(task.get("deploy_method") or "").lower() in REPO_FREE_METHODS:
        # 不依赖仓库的方式没有 Git 工作副本：说清原因，否则界面会一直提示
        # 「首次运行前需要拉取代码」，而这类任务根本不需要拉取。
        enriched["workspace_exists"] = True
        enriched["workspace"] = ""
        enriched["workspace_note"] = "该部署方式不拉取仓库，没有 Git 工作副本"
    else:
        _describe_workspace(task, enriched)
    enriched["releases_root"], enriched["current_link"] = (
        str(part) for part in resolve_release_paths(task)
    )
    return enriched


def _describe_workspace(task: dict[str, Any], enriched: dict[str, Any]) -> None:
    """把任务的 Git 工作副本位置与存在性写进响应，供界面判断首次运行提示。"""
    try:
        workspace = config.workspace_dir_for_task(task)
    except ValueError as exc:
        enriched["workspace_error"] = str(exc)
        enriched["workspace_exists"] = False
        enriched["workspace"] = ""
        return
    if not (workspace / ".git").exists():
        legacy = config.workspace_dir(int(task["id"]))
        if legacy in config.owned_workspace_dirs(task) and (legacy / ".git").exists():
            workspace = legacy
    enriched["workspace_exists"] = (workspace / ".git").exists()
    enriched["workspace"] = str(workspace)


@router.get("")
def list_tasks(
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    rows = service.store.tasks.list_all()
    # 一次查询取回全部任务的最近运行，避免逐任务 N+1。
    latest_runs = service.store.runs.latest_by_task()
    tasks = [
        _decorate(
            decode_task(row) or {},
            service,
            request,
            last_run=latest_runs.get(int(row["id"])) if row["id"] is not None else None,
        )
        for row in rows
    ]
    active_by_task: dict[int, dict[str, Any]] = {}
    for run in service.store.runs.active():
        active_by_task.setdefault(int(run["task_id"]), run)
    for task in tasks:
        active = active_by_task.get(int(task["id"]))
        if active:
            task["active_run"] = active
    return {"tasks": tasks, "total": len(tasks)}


def _ensure_credential_exists(service: Service, body: dict[str, Any]) -> None:
    """所选集中凭据必须真实存在：外键错误落到通用 handler 会变成 500。"""
    credential_id = body.get("credential_id")
    if credential_id is not None and service.store.credentials.get(int(credential_id)) is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败",
                    "errors": {"credential_id": "所选凭据不存在，可能已被删除"}},
        )


def _ensure_host_exists(service: Service, body: dict[str, Any]) -> None:
    """所选目标主机必须真实存在。

    host_id 为空表示本机，是合法值；指定了却查不到则说明主机被删，
    继续保存会让任务在运行时莫名退回本机部署。
    """
    host_id = body.get("host_id")
    if host_id is not None and service.store.hosts.get(int(host_id)) is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败",
                    "errors": {"host_id": "所选目标主机不存在，可能已被删除"}},
        )


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
    _ensure_credential_exists(service, body)
    _ensure_host_exists(service, body)

    try:
        task_id = service.store.tasks.create(body)
    except TaskNameConflict as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败", "errors": {"name": str(exc)}},
        ) from exc
    service.scheduler.reschedule(task_id)
    task = _decorate(decode_task(service.store.tasks.get(task_id)) or {}, service, request)
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
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = _decorate(decode_task(row) or {}, service, request)
    if row.get("host_id"):
        # 远程任务的发布目录在目标主机上：详情页不为渲染去发起 SSH，
        # 只说清位置，并提供主机名给界面提示。
        return {
            "task": task,
            "releases": [],
            "releases_remote": True,
            "releases_note": (
                f"发布版本位于目标主机「{task.get('host_label') or '远程主机'}」，"
                "请在目标主机上查看"
            ),
            "preflight": preflight(row),
            "runs": service.store.runs.list_for_task(task_id, limit=20),
        }
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
    # 与手动/定时派发串行，锁内重新读取名称和运行状态，避免改名与派发竞态。
    with service.scheduler._lock:
        existing = service.store.tasks.get_decoded(task_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        try:
            body = validate_task_payload(payload, partial=True, existing=existing)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
        _ensure_credential_exists(service, body)
        _ensure_host_exists(service, body)

        if "name" in body and body["name"] != existing["name"]:
            if service.store.runs.has_active_for_task(task_id):
                raise HTTPException(status_code=409, detail="任务正在运行或排队，暂不能修改任务名")
        try:
            service.store.tasks.update(task_id, body)
        except TaskNameConflict as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "参数校验失败", "errors": {"name": str(exc)}},
            ) from exc
        service.scheduler.reschedule(task_id)
    task = _decorate(decode_task(service.store.tasks.get(task_id)) or {}, service, request)
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
    purge: bool = Query(True, description="同时删除工作目录、发布产物与历史存档"),
    purge_data: bool = Query(
        False,
        description="同时删除容器挂载的持久化业务数据（persistent/）；默认保留以便迁移",
    ),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """删除任务：停止容器、清空存档、再删除记录。

    顺序是有意的：容器停止是唯一的**外部破坏性动作**，它需要任务记录里的
    部署方式与项目名，并且 ``docker compose down`` 依赖发布目录中仍存在
    compose 文件，所以必须在清理目录之前完成；而清理目录放在最后，即使中途
    失败也不会留下一个「记录已删、容器还在跑」的孤儿。

    调度锁只覆盖状态检查与记录删除这些数据库操作：一旦记录消失，调度器就
    不会再派发该任务，``has_active_for_task`` 也就失去意义。Docker 命令最长
    可能等待 ``CONTAINER_STOP_TIMEOUT_SECONDS``，若把它放在锁内，一个卡住的
    docker 会让全站所有部署一起停摆。

    ``purge=False`` 只保留文件，容器仍会被停止——任务记录消失后它已无人
    管理，继续运行只会占用端口与资源。

    ``persistent/`` 下的容器挂载数据**不受 purge 影响**，只有 ``purge_data``
    才会删除：那个目录里是用户业务数据，删掉无法重建，不能因为「清理存档」
    这个默认动作被一并抹掉。保留时会在返回的 ``kept`` 里给出路径。
    """
    with service.scheduler._lock:
        row = service.store.tasks.get(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if service.store.runs.has_active_for_task(task_id):
            raise HTTPException(status_code=409, detail="任务正在运行，请先取消后再删除")

        name = str(row.get("name") or "")
        # 日志路径要在删除记录前取好：runs 会随任务级联删除，之后就无从查找，
        # 日志文件将永久留在磁盘上成为孤儿。
        log_paths = [
            path
            for path in map(_safe_log_path, service.store.runs.log_paths_for_task(task_id))
            if path is not None
        ]
        run_total = service.store.runs.count(task_id=task_id)
        service.store.tasks.delete(task_id)
        service.scheduler.poke()

    # 锁外执行：容器停止依赖的 compose 文件此时仍在发布目录里。
    container_log: list[str] = []
    if row.get("host_id"):
        # 远程任务的容器与发布目录都在目标主机上：本机的 stop_task_containers
        # 只会对着控制机做无害的 no-op，反而让用户误以为已经清理干净。
        host_row = service.store.hosts.get(int(row["host_id"]))
        host_name = str((host_row or {}).get("name") or "") or f"主机 #{row['host_id']}"
        container_log.append(
            f"! 该任务部署在远程主机「{host_name}」上：容器与发布目录未在目标主机清理，"
            f"请登录该主机自行停止容器并删除发布目录"
        )
        containers: list[str] = []
    else:
        containers = stop_task_containers(row, log=container_log.append)

    purged: list[str] = []
    kept: list[str] = []
    if purge:
        targets = [*config.owned_workspace_dirs(row), *config.owned_storage_dirs(row)]
        for path in targets:
            if path.exists() or path.is_symlink():
                cleanup_path(path)
                purged.append(str(path))
        purged.extend(purge_target_release_history(row, log=container_log.append))
        for log_path in log_paths:
            try:
                log_path.unlink(missing_ok=True)
                purged.append(str(log_path))
            except OSError:
                continue
        target_dir = (row.get("target_dir") or "").strip()
        if target_dir:
            kept.append(target_dir)
    else:
        kept.append("工作目录、发布产物与运行日志已按 purge=false 保留")

    # 容器挂载的业务数据与上面那批可重建产物分开处理：purge 管的是存档，
    # 不该顺带删掉用户数据；只有 purge_data 才动它。
    data_dirs = config.owned_data_dirs(row)
    for path in data_dirs:
        if not (path.exists() or path.is_symlink()):
            continue
        if purge_data:
            cleanup_path(path)
            purged.append(str(path))
        else:
            kept.append(f"持久化数据：{path}（如需一并删除，请再次确认时选择删除业务数据）")

    audit(
        service, "task_deleted", actor=user["username"], target=f"task:{task_id}",
        detail=(f"{name} purge={purge} purge_data={purge_data} "
                f"容器:{len(containers)} 运行记录:{run_total}"),
        ip=client_ip(request),
    )
    return {
        "ok": True,
        "name": name,
        "purged": purged,
        "kept": kept,
        "containers": containers,
        "runs_deleted": run_total,
        "log": container_log,
    }


def _safe_log_path(raw: str) -> Path | None:
    """只接受位于日志目录内的路径，避免历史数据里的越界路径被当作删除目标。"""
    if not raw:
        return None
    path = Path(str(raw))
    try:
        resolved = path.resolve()
        logs_root = config.LOGS_DIR.resolve()
    except OSError:
        return None
    if resolved.parent != logs_root:
        return None
    return resolved


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


@router.post("/{task_id}/webhook/reset")
def reset_task_webhook(
    task_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """重新生成 Webhook 触发令牌；已分发的旧地址随之立即失效。"""
    if service.store.tasks.get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    secret = new_token()
    service.store.tasks.update(task_id, {"webhook_secret": secret})
    base = external_base_url(request, service.settings)
    audit(
        service, "webhook_reset", actor=user["username"], target=f"task:{task_id}",
        ip=client_ip(request),
    )
    return {"ok": True, "webhook_url": f"{base}/api/webhooks/{task_id}/{secret}" if base else ""}


@router.post("/{task_id}/toggle")
def toggle_task(
    task_id: int,
    request: Request,
    payload: dict[str, Any] | None = None,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """切换任务总开关。

    这里是「任务是否允许被自动触发」的单一位；定时与 Webhook 各自的开关
    在任务配置里维护。总开关关闭时两个触发通道都不工作，手动运行不受影响。
    """
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    body = payload or {}
    enabled = body.get("enabled")
    if enabled is None:
        enabled = not bool(row.get("enabled"))
    enabled = bool(enabled)
    schedule_enabled = bool(row.get("schedule_enabled", 1))
    next_at = None
    # 只有总开关与定时开关都打开时才排期；否则写回 None 以清掉残留的旧时间。
    if enabled and schedule_enabled:
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
        detail=f"task_enabled={enabled} schedule_enabled={schedule_enabled}",
        ip=client_ip(request),
    )
    return {
        "ok": True,
        "enabled": enabled,
        "schedule_enabled": schedule_enabled,
        "next_run_at": next_at,
    }


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


@router.post("/{task_id}/rollback", status_code=status.HTTP_202_ACCEPTED)
def rollback(
    task_id: int,
    request: Request,
    payload: dict[str, Any] | None = None,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """发起后台回滚：立即返回 202 与 run_id，进度经运行详情/增量日志轮询获取。"""
    run_id = None
    if payload is not None:
        run_id = payload.get("run_id")
        if set(payload) != {"run_id"} or type(run_id) is not int or not 0 < run_id <= 2**63 - 1:
            raise HTTPException(status_code=422, detail="回滚参数必须仅包含正整数 run_id")

    with service.scheduler._lock:
        row = service.store.tasks.get(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        # Check admission inside the same lock used by run_now and self-update.
        if service.store.runs.has_active_for_task(task_id):
            raise HTTPException(status_code=409, detail="任务正在运行，无法回滚")
        if service.selfupdate.deployment_blocked():
            raise HTTPException(status_code=409, detail="系统正在更新或回滚，暂不能回滚部署")
        # 准入通过后立即占位：先建运行记录再放锁，杜绝并发触发同名任务。
        # 运行版本合法性（存在/成功/目录有效）留到后台执行时校验——那需要
        # 解析发布目录的软链与路径边界，放在锁内会拖住其它调度操作。
        selected_run = (service.store.runs.get(run_id) or {}) if run_id is not None else None
        rollback_host_id, rollback_host_name = service.store.hosts.snapshot_for_run(row)
        rollback_run_id = service.store.runs.create(
            row, trigger="rollback",
            host_id=rollback_host_id, host_name=rollback_host_name,
        )

    threading.Thread(
        target=service.runner.execute_rollback,
        args=(rollback_run_id, row, selected_run),
        kwargs={
            "timeout": min(600, int(row.get("timeout_seconds") or 300)),
            "kill_grace_seconds": service.settings.kill_grace_seconds,
        },
        name=f"rollback-{rollback_run_id}",
        daemon=True,
    ).start()
    audit(
        service, "rollback", actor=user["username"], target=f"task:{task_id}",
        detail=(f"run:{run_id} " if run_id is not None else "") + f"已发起回滚 run:{rollback_run_id}",
        ip=client_ip(request),
    )
    return {"ok": True, "run_id": rollback_run_id, "message": "回滚已开始"}


@router.get("/{task_id}/preflight")
def task_preflight(
    task_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.tasks.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    releases_root, current_link = resolve_release_paths(row)
    task = _decorate(decode_task(row) or {}, service, request)
    # 远程任务：把检查项也落到目标主机上（这是用户主动点「环境检查」的路径，
    # 发一次 SSH 是可接受的；发布列表那种被动 GET 则不发）。
    checks = preflight(row)
    remote_info: dict[str, Any] = {}
    if row.get("host_id"):
        host_row = service.store.hosts.get(int(row["host_id"]))
        if host_row is None:
            checks = [{"name": "目标主机", "ok": False, "message": "任务指定的目标主机已被删除"}]
            remote_info = {"host_missing": True}
        else:
            credential_row = None
            if host_row.get("credential_id"):
                credential_row = service.store.credentials.get(int(host_row["credential_id"]))
            try:
                target = build_target(
                    host_row, credential_row,
                    work_dir=config.TMP_DIR / f"task-{task_id}-host",
                )
                layout = resolve_remote_layout(host_row, row)
                checks = preflight(row, target=None) + preflight(row, target=target)
                remote_info = {
                    "remote": True,
                    "host_name": str(host_row.get("name") or ""),
                    "host_target": describe_host(host_row),
                    "workspace": layout.workspace,
                    "releases_root": layout.releases_root,
                    "current_link": layout.current_link,
                }
            except (TargetError, RemoteError) as exc:
                checks = [{"name": "目标主机", "ok": False, "message": str(exc)}]
                remote_info = {"remote": True, "host_error": str(exc)}
    return {
        "checks": checks,
        "required_binaries": sorted({*REQUIRED_BINARIES, *METHOD_BINARIES.values()}),
        "releases_root": remote_info.get("releases_root", str(releases_root)),
        "current_link": remote_info.get("current_link", str(current_link)),
        "current_target": (
            "" if remote_info.get("remote")
            else str(current_release(releases_root, current_link) or "")
        ),
        "workspace": remote_info.get("workspace", task["workspace"]),
        "workspace_error": task.get("workspace_error", ""),
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
    # 远程任务的发布目录在目标主机上：一次列表请求不该为此发起 SSH，
    # 否则慢连接会把整个任务详情页拖住。明确告知位置即可。
    if row.get("host_id"):
        host_row = service.store.hosts.get(int(row["host_id"]))
        host_name = str((host_row or {}).get("name") or "") or f"主机 #{row['host_id']}"
        return {
            "releases": [],
            "current": "",
            "root": "",
            "remote": True,
            "host_name": host_name,
            "note": f"该任务的发布版本位于目标主机「{host_name}」，请在目标主机上查看",
        }
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
    # 触发地址含机密令牌，导出的定义文件不得携带。
    task.pop("webhook_secret", None)
    task.pop("webhook_url", None)
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
