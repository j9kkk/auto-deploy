"""远程主机管理。

主机是「部署到哪里」的一等对象，通过 SSH 连接。这里的接口保证：

* 任何响应都不返回私钥内容——凭据只在 SSH 传输层内部读取。
* 「测试连接」既验证连通性，也返回主机指纹供用户确认后再钉住。
* 删除主机前要求先解除任务引用，避免任务悄悄退回本机部署。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import config
from ..remote import RemoteError, SshTransport, transport_for_host
from ..service import Service
from ..store import HOST_STATUSES
from ..validation import ValidationError, describe_host, validate_host_payload
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/hosts", tags=["hosts"])

STATUS_LABELS = {
    "unknown": "未检测",
    "online": "在线",
    "offline": "离线",
    "error": "异常",
}


def _decorate(host: dict[str, Any], service: Service, *, used_by: int = 0) -> dict[str, Any]:
    """补充派生字段；绝不输出任何秘密内容。"""
    item = dict(host)
    item.pop("host_key", None)  # 指纹单独以 fingerprint 字段给出，避免误当作可写字段
    item["fingerprint"] = host.get("host_key") or ""
    item["status_label"] = STATUS_LABELS.get(str(item.get("status")), str(item.get("status")))
    item["target"] = describe_host(host)
    item["used_by"] = used_by
    credential_id = item.get("credential_id")
    item["credential_missing"] = bool(credential_id) and service.store.credentials.get(
        int(credential_id)
    ) is None
    return item


def _require_ssh_credential(service: Service, credential_id: Any) -> None:
    if credential_id in (None, "", 0):
        raise HTTPException(status_code=422, detail="请选择用于 SSH 登录的私钥凭据")
    row = service.store.credentials.get(int(credential_id))
    if row is None:
        raise HTTPException(status_code=422, detail="所选凭据不存在")
    if row.get("kind") != "ssh_key":
        raise HTTPException(
            status_code=422,
            detail="所选凭据不是 SSH 私钥类型；请先在「凭据」页创建 ssh_key 类型的凭据",
        )


@router.get("")
def list_hosts(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    counts = service.store.hosts.usage_counts()
    items = [
        _decorate(row, service, used_by=counts.get(int(row["id"]), 0))
        for row in service.store.hosts.list_all()
    ]
    return {
        "hosts": items,
        "total": len(items),
        "statuses": [{"value": key, "label": STATUS_LABELS[key]} for key in HOST_STATUSES],
    }


@router.get("/{host_id}")
def get_host(
    host_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    row = service.store.hosts.get(host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="主机不存在")
    counts = service.store.hosts.usage_counts()
    return {"host": _decorate(row, service, used_by=counts.get(host_id, 0))}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_host(
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        body = validate_host_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    if service.store.hosts.name_taken(body["name"]):
        raise HTTPException(status_code=409, detail="主机名称已被使用，请换一个名称")
    _require_ssh_credential(service, body.get("credential_id"))

    host_id = service.store.hosts.create(body)
    audit(
        service, "host_created", actor=user["username"], target=f"host:{host_id}",
        detail=f"{body['name']} → {describe_host(body)}", ip=client_ip(request),
    )
    row = service.store.hosts.get(host_id) or {}
    return {"ok": True, "host": _decorate(row, service)}


@router.put("/{host_id}")
def update_host(
    host_id: int,
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    row = service.store.hosts.get(host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="主机不存在")
    try:
        body = validate_host_payload(payload, partial=True, existing=row)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    if "name" in body and service.store.hosts.name_taken(body["name"], exclude_id=host_id):
        raise HTTPException(status_code=409, detail="主机名称已被使用，请换一个名称")
    if "credential_id" in body:
        _require_ssh_credential(service, body.get("credential_id"))
    # 地址/端口/用户名/凭据变化会让已钉住的指纹失去意义，需重新探测确认。
    identity_changed = any(
        key in body and str(body[key]) != str(row.get(key))
        for key in ("address", "port", "username", "credential_id")
    )
    if identity_changed:
        service.store.hosts.update(host_id, body)
        service.store.hosts.record_check(host_id, status="unknown", error="", host_key="")
    else:
        service.store.hosts.update(host_id, body)

    audit(
        service, "host_updated", actor=user["username"], target=f"host:{host_id}",
        detail="连接信息变更，需重新测试" if identity_changed else "已更新",
        ip=client_ip(request),
    )
    updated = service.store.hosts.get(host_id) or {}
    return {"ok": True, "host": _decorate(updated, service), "fingerprint_reset": identity_changed}


@router.delete("/{host_id}")
def delete_host(
    host_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    row = service.store.hosts.get(host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="主机不存在")
    used = service.store.hosts.usage_counts().get(host_id, 0)
    if used:
        raise HTTPException(
            status_code=409,
            detail=f"还有 {used} 个任务指向该主机；请先在这些任务里改回本机或别的目标主机",
        )
    service.store.hosts.delete(host_id)
    audit(
        service, "host_deleted", actor=user["username"], target=f"host:{host_id}",
        detail=str(row.get("name") or ""), ip=client_ip(request),
    )
    return {"ok": True, "message": "主机已删除"}


def _open_transport(service: Service, row: dict[str, Any]) -> SshTransport:
    credential = None
    if row.get("credential_id"):
        credential = service.store.credentials.get(int(row["credential_id"]))
    work_dir = config.TMP_DIR / f"host-{int(row['id'])}"
    return transport_for_host(row, credential=credential, work_dir=work_dir)


@router.post("/{host_id}/test")
def test_host(
    host_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """探测连通性与工具链，成功时钉住主机指纹。

    失败一律返回 200 + ``ok: false``：这是「检查结果」而不是请求错误，
    前端要能同时展示原因与已有指纹。
    """
    row = service.store.hosts.get(host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="主机不存在")
    try:
        with _open_transport(service, row) as transport:
            probe = transport.probe()
    except RemoteError as exc:
        service.store.hosts.record_check(host_id, status="error", error=str(exc))
        audit(
            service, "host_test_failed", actor=user["username"], target=f"host:{host_id}",
            detail=str(exc)[:200], ip=client_ip(request),
        )
        return {"ok": False, "message": str(exc)}

    if not probe.ok:
        service.store.hosts.record_check(host_id, status="offline", error=probe.message)
        audit(
            service, "host_test_failed", actor=user["username"], target=f"host:{host_id}",
            detail=probe.message[:200], ip=client_ip(request),
        )
        return {"ok": False, "message": probe.message, "probe": probe.as_dict()}

    # 指纹校验：已钉住且本次读到不同指纹时拒绝，提示人工确认后重置。
    pinned = str(row.get("host_key") or "")
    observed = probe.host_fingerprint
    if pinned and observed and pinned != observed:
        message = (
            "目标主机指纹与已保存的不一致，可能重装了系统或存在中间人攻击。"
            "确认是预期变更后，请在此处点「重置指纹」再重新测试。"
        )
        service.store.hosts.record_check(host_id, status="error", error=message)
        return {
            "ok": False,
            "message": message,
            "fingerprint_changed": True,
            "saved_fingerprint": pinned,
            "observed_fingerprint": observed,
        }

    new_key = observed if (observed and not pinned) else None
    service.store.hosts.record_check(host_id, status="online", host_key=new_key)
    audit(
        service, "host_tested", actor=user["username"], target=f"host:{host_id}",
        detail="在线，已钉住指纹" if new_key else "在线", ip=client_ip(request),
    )
    return {
        "ok": True,
        "message": "连接成功",
        "probe": probe.as_dict(),
        "fingerprint_pinned": bool(new_key) or bool(pinned),
    }


@router.post("/{host_id}/reset-fingerprint")
def reset_fingerprint(
    host_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """清空已钉住的指纹；用于目标机重装后重新信任。

    这是一次显式的安全降级，必须由管理员主动触发并留审计。
    """
    row = service.store.hosts.get(host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="主机不存在")
    service.store.hosts.record_check(host_id, status="unknown", error="", host_key="")
    audit(
        service, "host_fingerprint_reset", actor=user["username"], target=f"host:{host_id}",
        detail=f"原指纹 {row.get('host_key') or '(空)'}", ip=client_ip(request),
    )
    return {"ok": True, "message": "指纹已重置，请重新测试连接以钉住新指纹"}
