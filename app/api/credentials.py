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

from .. import config, gitops, sshkey
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


@router.get("/guide")
def credential_guide(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """凭据获取指引，供界面上的帮助面板渲染。

    放在后端而不是写死在前端，是为了让「该点哪个链接、需要哪个权限」
    这类随 GitHub 改版而变化的信息只有一处来源。
    """
    return {
        "intro": "私有仓库需要凭据才能拉取代码。两种方式任选其一："
                 "个人访问令牌配置更快、可跨仓库复用；Deploy Key 权限只限单个仓库且不会过期。",
        "kinds": [
            {
                "kind": "https_token",
                "label": KIND_LABELS["https_token"],
                "best_for": "个人仓库，或一个令牌要覆盖多个仓库时推荐",
                "create_url": "https://github.com/settings/personal-access-tokens/new",
                "manage_url": "https://github.com/settings/personal-access-tokens",
                "steps": [
                    "打开创建页面，Resource owner 选择仓库的属主（个人或组织）。",
                    "Repository access 选 Only select repositories，勾选要部署的仓库。",
                    "Permissions → Repository permissions → 把 Contents 设为 Read-only"
                    "（Metadata 会自动变为只读，这是必需的）。",
                    "点 Generate token，复制生成的令牌（只显示一次）。",
                    "回到本页新建凭据：类型选「HTTPS 访问令牌」，用户名填 x-access-token，粘贴令牌。",
                ],
                "notes": [
                    "细粒度令牌有有效期，到期后部署会失败；建议看到凭据「不可用」时及时更换。",
                    "组织仓库若限制令牌，需要组织管理员批准；也可改用 Deploy Key。",
                    "仓库地址保持 https://github.com/owner/repo.git 形式，无需修改。",
                ],
            },
            {
                "kind": "ssh_key",
                "label": KIND_LABELS["ssh_key"],
                "best_for": "只给单个仓库授权、且希望凭据长期有效时推荐",
                "create_url": "https://github.com/REPO_OWNER/REPO_NAME/settings/keys",
                "manage_url": "",
                "steps": [
                    "在本页新建凭据时选「SSH 私钥」，点「自动生成密钥对」，系统直接生成密钥。",
                    "复制页面上显示的公钥（以 ssh-ed25519 或 ssh-rsa 开头的一整行）。",
                    "打开仓库的 Settings → Deploy Keys → Add deploy key，把公钥粘贴到 Key 输入框。",
                    "「Allow write access」保持不勾选——部署只需要读取权限。",
                    "保存凭据，并把任务的仓库地址改成 git@github.com:owner/repo.git 形式。",
                ],
                "notes": [
                    "私钥只保存在服务端，保存后不再回传浏览器；请自行备份，丢失后只能重新生成。",
                    "必须先到 GitHub 添加公钥，再点「测试」验证，否则会提示密钥不被接受。",
                    "同一个公钥对同一个仓库只能添加一次，GitHub 会拒绝重复添加。",
                ],
            },
        ],
    }


@router.post("/generate-keypair")
def generate_ssh_keypair(
    payload: dict[str, Any] | None = None,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """服务端生成一对 SSH 密钥，返回私钥与公钥供用户立即配置。

    私钥只在这一个响应里返回一次；用户保存为凭据后服务端不再回传。
    这一步免去了手工运行 ``ssh-keygen`` 并找对文件、分清公私钥的麻烦。
    """
    body = payload or {}
    key_type = str(body.get("key_type") or "ed25519").strip().lower()
    comment = str(body.get("comment") or "autodeploy").strip()[:100] or "autodeploy"
    try:
        private_key, public_key, fingerprint = sshkey.generate_keypair(
            key_type=key_type, comment=comment
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    audit(
        service, "ssh_keypair_generated", actor=user["username"],
        target="credential:new", detail=f"{key_type} {fingerprint}",
    )
    return {
        "private_key": private_key,
        "public_key": public_key,
        "fingerprint": fingerprint,
        "key_type": sshkey.key_type_of(private_key) or key_type,
        "key_types": [{"value": k, "label": v} for k, v in sshkey.KEY_TYPES.items()],
    }


@router.get("/{credential_id}/public-key")
def credential_public_key(
    credential_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """取回某份 SSH 凭据的公钥，便于重新配置 Deploy Key 或核对指纹。

    只返回公钥与指纹——两者都不是机密；私钥永不回传浏览器。
    """
    row = service.store.credentials.get(credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    if str(row.get("kind")) != "ssh_key":
        raise HTTPException(status_code=422, detail="只有「SSH 私钥」类型的凭据才有公钥")
    private_key = str(row.get("secret") or "")
    name = str(row.get("name") or "autodeploy")
    public_key = sshkey.public_key_line(private_key, name)
    if not public_key:
        raise HTTPException(
            status_code=422,
            detail="无法从该私钥推导公钥：仅支持 OpenSSH 格式"
                   "（以 -----BEGIN OPENSSH PRIVATE KEY----- 开头）",
        )
    return {
        "public_key": public_key,
        "fingerprint": sshkey.fingerprint(private_key),
        "key_type": sshkey.key_type_of(private_key),
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
