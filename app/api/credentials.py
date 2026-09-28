"""全局 Git 凭据管理。

凭据集中存放的意义：多处复用的令牌只需维护一份，轮换时改一处即可；
并且可以在界面上主动测试它是否仍然有效（令牌过期是导致部署突然失败的
常见原因，早发现比事后翻日志划算）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import config, gitops
from ..gitops import GitCredential
from ..service import Service
from ..store import CREDENTIAL_KINDS
from ..validation import ValidationError, validate_credential_payload
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# 与 gitops.KIND_LABELS 同源，避免两处维护导致显示不一致。
KIND_LABELS = gitops.KIND_LABELS


@router.get("")
def list_credentials(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """列出全部凭据。**永不返回密钥明文**，只返回是否已设置与测试结果。"""
    items = []
    for row in service.store.credentials.list_decoded():
        item = dict(row)
        item["kind_label"] = KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        item["used_by"] = service.store.credentials.usage_count(int(item["id"]))
        items.append(item)
    return {
        "credentials": items,
        "kinds": [{"value": k, "label": KIND_LABELS.get(k, k)} for k in CREDENTIAL_KINDS],
        "total": len(items),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_credential(
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        body = validate_credential_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    if service.store.credentials.name_taken(body.get("name", "")):
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败", "errors": {"name": "凭据名称已被使用"}},
        )

    credential_id = service.store.credentials.create(body)
    audit(
        service, "credential_created", actor=user["username"],
        target=f"credential:{credential_id}", detail=body.get("name", ""),
        ip=client_ip(request),
    )
    created = service.store.credentials.get_decoded(credential_id) or {}
    created["kind_label"] = KIND_LABELS.get(str(created.get("kind")), "")
    created["used_by"] = 0
    return {"credential": created}


@router.get("/kinds")
def list_kinds(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """凭据类型选项（供任务表单与凭据表单使用）。"""
    return {
        "kinds": [
            {
                "value": "https_token",
                "label": KIND_LABELS["https_token"],
                "hint": "适用于 GitHub / GitLab 等 HTTPS 地址，推荐使用细粒度只读令牌",
                "secret_label": "访问令牌",
                "secret_placeholder": "ghp_xxx 或 github_pat_xxx",
            },
            {
                "value": "ssh_key",
                "label": KIND_LABELS["ssh_key"],
                "hint": "适用于 git@host:owner/repo.git 形式的地址，需把公钥登记到仓库的 Deploy Keys",
                "secret_label": "SSH 私钥",
                "secret_placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----",
            },
        ]
    }


@router.get("/{credential_id}")
def get_credential(
    credential_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    item = service.store.credentials.get_decoded(credential_id)
    if item is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    item["kind_label"] = KIND_LABELS.get(str(item.get("kind")), "")
    item["used_by"] = service.store.credentials.usage_count(credential_id)
    tasks = service.store.tasks.list_all()
    item["tasks"] = [
        {"id": task["id"], "name": task["name"]}
        for task in tasks
        if task.get("credential_id") == credential_id
    ]
    return {"credential": item}


@router.put("/{credential_id}")
@router.patch("/{credential_id}")
def update_credential(
    credential_id: int,
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    existing = service.store.credentials.get(credential_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    try:
        body = validate_credential_payload(payload, partial=True, existing=existing)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    if "name" in body and service.store.credentials.name_taken(body["name"], exclude_id=credential_id):
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验失败", "errors": {"name": "凭据名称已被其他凭据使用"}},
        )

    service.store.credentials.update(credential_id, body)
    # 凭据变更后旧的测试结论不再可信，清掉让用户重新测试。
    if "secret" in body or "kind" in body:
        service.store.credentials.record_test(credential_id, ok=False, error="凭据已修改，请重新测试")
    audit(
        service, "credential_updated", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"字段: {', '.join(sorted(body.keys()))}", ip=client_ip(request),
    )
    item = service.store.credentials.get_decoded(credential_id) or {}
    item["kind_label"] = KIND_LABELS.get(str(item.get("kind")), "")
    item["used_by"] = service.store.credentials.usage_count(credential_id)
    return {"credential": item}


@router.delete("/{credential_id}")
def delete_credential(
    credential_id: int,
    request: Request,
    force: bool = False,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    existing = service.store.credentials.get(credential_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    used_by = service.store.credentials.usage_count(credential_id)
    if used_by and not force:
        # 直接删除会让引用它的任务静默失去凭据，必须让用户先确认。
        raise HTTPException(
            status_code=409,
            detail=f"仍有 {used_by} 个任务在使用该凭据，请先改绑或使用强制删除",
        )
    service.store.credentials.delete(credential_id)
    audit(
        service, "credential_deleted", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"{existing.get('name', '')}（影响 {used_by} 个任务）", ip=client_ip(request),
    )
    return {"ok": True, "affected_tasks": used_by}


@router.post("/{credential_id}/test")
def test_credential(
    credential_id: int,
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """用该凭据试连一个仓库，把结果落库并返回。

    需要一个仓库地址作为测试目标：同一个令牌对不同仓库的权限可能不同，
    用真实地址测才有意义。
    """
    row = service.store.credentials.get(credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    repo_url = str(payload.get("repo_url") or "").strip()
    if not repo_url:
        raise HTTPException(status_code=422, detail="请提供用于测试的仓库地址")

    credential = GitCredential(
        kind=str(row.get("kind") or "https_token"),
        username=str(row.get("username") or ""),
        token=str(row.get("secret") or "") if row.get("kind") == "https_token" else "",
        private_key=str(row.get("secret") or "") if row.get("kind") == "ssh_key" else "",
        passphrase=str(row.get("passphrase") or ""),
        name=str(row.get("name") or ""),
    )

    settings = service.settings
    proxy = config.proxy_env(settings)
    tmp_root = Path(tempfile.mkdtemp(prefix="autodeploy-credtest-", dir=str(config.TMP_DIR)))
    try:
        ok, message = gitops.test_credentials(
            repo_url=repo_url,
            credential=credential,
            tmp_dir=tmp_root,
            timeout=min(60, int(settings.git_timeout_seconds)),
            proxy=proxy,
            home=tmp_root,
        )
    finally:
        import shutil

        shutil.rmtree(tmp_root, ignore_errors=True)

    service.store.credentials.record_test(credential_id, ok=ok, error="" if ok else message)
    audit(
        service, "credential_tested", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"{'成功' if ok else '失败'}: {message}", ip=client_ip(request),
    )
    return {"ok": ok, "message": message, "credential": service.store.credentials.get_decoded(credential_id)}
