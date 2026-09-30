"""Webhook trigger endpoint:外部系统凭地址内的随机令牌触发部署，无需登录。

安全模型与登录接口完全不同：没有会话，只有每个任务一个 32 字节随机令牌，
拼在路径里（``/api/webhooks/{task_id}/{secret}``）。因此所有失败——任务不
存在、令牌为空、令牌不匹配——都必须返回同一个 404，避免向探测者泄露任何
任务存在性信息；比对用常量时间函数，防止逐字节猜测令牌。

端点只做鉴权、准入检查与派发编排，部署逻辑一律走 ``scheduler.run_now``，
与手动触发共享同一条链路（并发上限、自更新封锁、运行记录都由此保证）。
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..service import Service
from .deps import audit, client_ip, get_service

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# 与任务主键同域的合法范围：越过即视为无效地址，而不是数据库错误。
_MAX_TASK_ID = 2**63 - 1


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="触发地址无效")


@router.api_route("/{task_id}/{secret}", methods=["GET", "POST"])
def trigger_webhook(
    task_id: str,
    secret: str,
    request: Request,
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    """触发一次部署。GET 与 POST 等价，方便 curl 与各类 webhook 服务接入。"""
    try:
        task_id_num = int(task_id)
    except ValueError:
        raise _not_found() from None
    if not 0 < task_id_num <= _MAX_TASK_ID:
        raise _not_found()

    row = service.store.tasks.get(task_id_num)
    stored = str((row or {}).get("webhook_secret") or "")
    if row is None or not stored or not hmac.compare_digest(
        stored.encode("utf-8"), secret.encode("utf-8")
    ):
        raise _not_found()

    if service.store.runs.count_active() >= service.settings.max_global_workers:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"并发运行数已达上限（{service.settings.max_global_workers}），请稍后再试",
        )
    try:
        run_id = service.scheduler.run_now(task_id_num, trigger="webhook")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if run_id is None:  # pragma: no cover - 任务刚被并发删除
        raise _not_found()

    audit(
        service, "run_triggered", actor="webhook", target=f"task:{task_id_num}",
        detail=f"run:{run_id}", ip=client_ip(request),
    )
    return {"ok": True, "run_id": run_id, "task_id": task_id_num}
