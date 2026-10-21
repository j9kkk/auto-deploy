"""全局 Git 凭据管理。

凭据集中存放的意义：多处复用的令牌只需维护一份，轮换时改一处即可；
并且可以在界面上主动测试它是否仍然有效（令牌过期是导致部署突然失败的
常见原因，早发现比事后翻日志划算）。

同一张表承载两类用途完全不同的秘密：

* ``https_token`` —— 只能用于拉取仓库。
* ``ssh_key`` —— 既可作为仓库 Deploy Key，也可作为远程主机登录密钥。
  因此引用计数、删除保护与「测试」都必须同时覆盖任务与主机两侧，
  否则只绑定主机（不碰仓库）的私钥在界面上会显示「未使用」，
  测试也只问仓库地址，用户无从验证它能否登录服务器。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import config, gitops, sshkey
from ..gitops import GitCredential
from ..remote import RemoteError, SshTransport
from ..service import Service
from ..store import CREDENTIAL_KINDS
from ..validation import (
    ValidationError,
    validate_credential_payload,
    validate_host_payload,
)
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# 与 gitops.KIND_LABELS 同源，避免两处维护导致显示不一致。
KIND_LABELS = gitops.KIND_LABELS


def _attach_usage(item: dict[str, Any], service: Service, *, hosts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """补齐「被谁引用」：任务与主机是两类独立引用。

    只统计任务曾导致 SSH 私钥显示为「未使用」，删除保护也会漏掉主机绑定。
    """
    credential_id = int(item["id"])
    task_count = service.store.credentials.usage_count(credential_id)
    host_rows = hosts if hosts is not None else service.store.credentials.hosts_using(credential_id)
    item["used_by"] = task_count
    item["used_by_hosts"] = len(host_rows)
    item["hosts"] = [
        {
            "id": row["id"], "name": row["name"], "address": row["address"],
            # 测试主机登录时直接回填，不必让用户记得当初填了什么账号。
            "port": row.get("port") or 22, "username": row.get("username") or "",
        }
        for row in host_rows
    ]
    return item


@router.get("")
def list_credentials(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """列出全部凭据。**永不返回密钥明文**，只返回是否已设置与测试结果。"""
    task_counts = service.store.credentials.usage_counts()
    host_counts = service.store.credentials.host_usage_counts()
    items = []
    for row in service.store.credentials.list_decoded():
        item = dict(row)
        item["kind_label"] = KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        item["used_by"] = task_counts.get(int(item["id"]), 0)
        item["used_by_hosts"] = host_counts.get(int(item["id"]), 0)
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
    created["used_by_hosts"] = 0
    created["hosts"] = []
    return {"credential": created}


@router.get("/kinds")
def list_kinds(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """凭据类型选项（供任务表单与凭据表单使用）。

    ``usages`` 说明该类型能用在哪些地方：SSH 私钥是「一钥两用」（仓库
    Deploy Key + 主机登录），这一点必须出现在选择类型的当下，否则用户
    会以为凭据页只管仓库，转身去主机页找不到密钥从哪来。
    """
    return {
        "kinds": [
            {
                "value": "https_token",
                "label": KIND_LABELS["https_token"],
                "hint": "适用于 GitHub / GitLab 等 HTTPS 地址，推荐使用细粒度只读令牌",
                "secret_label": "访问令牌",
                "secret_placeholder": "ghp_xxx 或 github_pat_xxx",
                "usages": ["拉取私有仓库（HTTPS 地址）"],
            },
            {
                "value": "ssh_key",
                "label": KIND_LABELS["ssh_key"],
                "hint": "两种用途可任选或同时使用：登记为仓库 Deploy Key 拉取代码，"
                        "或添加到远程主机的 authorized_keys 用于登录与部署",
                "secret_label": "SSH 私钥",
                "secret_placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----",
                "usages": [
                    "拉取私有仓库（git@host:owner/repo.git）",
                    "登录远程主机并部署（在「主机」页绑定）",
                ],
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

    SSH 私钥有两条互不相干的登记路径：仓库侧是 Deploy Keys，主机侧是
    目标机的 ``authorized_keys``。只讲前者会让「部署到远程主机」的用户
    在凭据页找不到方向，因此 ``ssh_key`` 额外给出 ``host_steps``。
    """
    return {
        "intro": "私有仓库需要凭据才能拉取代码。两种方式任选其一："
                 "个人访问令牌配置更快、可跨仓库复用；Deploy Key 权限只限单个仓库且不会过期。"
                 "另外，SSH 私钥除拉取仓库外，还可以用来登录远程主机——两种用途互不影响，"
                 "同一把钥匙可以只用于其中一种。",
        "kinds": [
            {
                "kind": "https_token",
                "label": KIND_LABELS["https_token"],
                "best_for": "个人仓库，或一个令牌要覆盖多个仓库时推荐",
                "create_url": "https://github.com/settings/personal-access-tokens/new",
                "manage_url": "https://github.com/settings/personal-access-tokens",
                "usages": ["拉取私有仓库（HTTPS 地址）"],
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
                    "令牌不能用于 SSH 登录远程主机；需要部署到远程服务器请改用 SSH 私钥。",
                    "组织仓库若限制令牌，需要组织管理员批准；也可改用 Deploy Key。",
                    "仓库地址保持 https://github.com/owner/repo.git 形式，无需修改。",
                ],
            },
            {
                "kind": "ssh_key",
                "label": KIND_LABELS["ssh_key"],
                "best_for": "只给单个仓库授权、或用于登录远程主机时推荐",
                "create_url": "https://github.com/REPO_OWNER/REPO_NAME/settings/keys",
                "manage_url": "",
                "usages": [
                    "拉取私有仓库（git@host:owner/repo.git）",
                    "登录远程主机并部署（在「主机」页绑定）",
                ],
                "steps": [
                    "在本页新建凭据时选「SSH 私钥」，点「自动生成密钥对」，系统直接生成密钥。",
                    "复制页面上显示的公钥（以 ssh-ed25519 或 ssh-rsa 开头的一整行）。",
                    "打开仓库的 Settings → Deploy Keys → Add deploy key，把公钥粘贴到 Key 输入框。",
                    "「Allow write access」保持不勾选——部署只需要读取权限。",
                    "保存凭据，并把任务的仓库地址改成 git@github.com:owner/repo.git 形式。",
                ],
                "host_steps": [
                    "同样先在本页新建「SSH 私钥」凭据并点「自动生成密钥对」，保存后点该行的「公钥」按钮随时可再复制。",
                    "用密码登录一次目标机，把公钥追加到部署用户（例如 deploy）的 ~/.ssh/authorized_keys："
                    "ssh-copy-id -i ~/.ssh/autodeploy.pub deploy@目标地址。",
                    "确认 /etc/ssh/sshd_config 里 PubkeyAuthentication 为 yes，并已 reload sshd。",
                    "在「主机」页添加主机：填地址、端口、SSH 用户名，SSH 私钥凭据选刚建的这一条。",
                    "点主机行的「测试连接」：成功后会钉住主机指纹，之后指纹变化一律拒绝连接。",
                ],
                "notes": [
                    "私钥只保存在服务端，保存后不再回传浏览器；请自行备份，丢失后只能重新生成。",
                    "仓库用途：必须先到 GitHub 添加公钥，再点「测试」验证，否则会提示密钥不被接受。",
                    "主机用途：公钥要加到目标机 authorized_keys，而不是 GitHub；"
                    "验证请在凭据行点「测试」并选「主机登录」。",
                    "远程主机登录只支持免口令私钥：容器内无法交互输入口令，带口令的密钥会被明确拒绝。",
                    "同一个公钥对同一个仓库只能添加一次，GitHub 会拒绝重复添加；同一把钥匙加到多台主机的 authorized_keys 则完全没问题。",
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


@router.post("/{credential_id}/test-host")
def test_credential_host_login(
    credential_id: int,
    payload: dict[str, Any],
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """用该 SSH 私钥直接登录一台主机，验证「主机用途」是否配好。

    与 ``/test`` 是两件事：那个测的是仓库（Deploy Key 登记），这个测的是
    目标机 ``authorized_keys``。私钥只用于登录服务器时，用户此前无处验证。

    测试不读也不写 hosts 表的指纹：主机尚未添加时也要能测，而指纹钉住
    属于主机自己的生命周期。这里用一次性临时目录，凭据本身不被改动。
    """
    import shutil

    row = service.store.credentials.get(credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    if str(row.get("kind")) != "ssh_key":
        raise HTTPException(status_code=422, detail="只有「SSH 私钥」类型的凭据才能用于主机登录")

    address = str(payload.get("address") or "").strip()
    if not address:
        raise HTTPException(status_code=422, detail="请提供要登录的主机地址")
    try:
        host_check = validate_host_payload(
            {
                "name": "probe",
                "address": address,
                "port": payload.get("port") or 22,
                "username": payload.get("username") or "",
            }
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    tmp_root = Path(tempfile.mkdtemp(prefix="autodeploy-hosttest-", dir=str(config.TMP_DIR)))
    try:
        transport = SshTransport(
            address=host_check["address"],
            port=int(host_check["port"]),
            username=str(host_check["username"]),
            private_key=str(row.get("secret") or ""),
            passphrase=str(row.get("passphrase") or ""),
            known_hosts=tmp_root / "known_hosts",
            work_dir=tmp_root,
        )
        try:
            with transport:
                probe = transport.probe(timeout=min(30, int(service.settings.git_timeout_seconds)))
        except RemoteError as exc:
            ok, message, probe_data = False, str(exc), {}
        else:
            ok = probe.ok
            message = probe.message if probe.ok else (probe.message or "SSH 登录失败")
            probe_data = probe.as_dict()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    service.store.credentials.record_host_test(credential_id, ok=ok, error="" if ok else message)
    audit(
        service, "credential_host_tested", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"{'成功' if ok else '失败'}: {host_check['username']}@{host_check['address']} {message}"[:300],
        ip=client_ip(request),
    )
    return {
        "ok": ok,
        "message": message,
        "target": f"{host_check['username']}@{host_check['address']}:{host_check['port']}",
        "probe": probe_data,
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
    _attach_usage(item, service)
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
    # 凭据变更后旧的测试结论不再可信，清掉让用户重新测试。口令参与私钥
    # 解锁，同样会使结论失效；仅改名/改备注不影响连接行为，不算变更。
    # 仓库与主机两侧的结论都要清：一把钥匙换了内容，两边都得重测。
    if "secret" in body or "kind" in body or "passphrase" in body:
        service.store.credentials.clear_tests(credential_id)
    audit(
        service, "credential_updated", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"字段: {', '.join(sorted(body.keys()))}", ip=client_ip(request),
    )
    item = service.store.credentials.get_decoded(credential_id) or {}
    item["kind_label"] = KIND_LABELS.get(str(item.get("kind")), "")
    _attach_usage(item, service)
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
    host_rows = service.store.credentials.hosts_using(credential_id)
    if (used_by or host_rows) and not force:
        # 直接删除会让引用它的任务/主机静默失去凭据（外键置空后主机无法登录、
        # 任务回退任务内令牌），必须让用户先确认。两类引用都要算进来。
        parts = []
        if used_by:
            parts.append(f"{used_by} 个任务")
        if host_rows:
            names = "、".join(str(row.get("name") or row.get("address")) for row in host_rows[:3])
            if len(host_rows) > 3:
                names += f" 等 {len(host_rows)} 台"
            parts.append(f"{len(host_rows)} 台主机（{names}）")
        raise HTTPException(
            status_code=409,
            detail=f"仍有 {' 和 '.join(parts)} 在使用该凭据，请先改绑或使用强制删除",
        )
    service.store.credentials.delete(credential_id)
    affected = []
    if used_by:
        affected.append(f"{used_by} 个任务")
    if host_rows:
        affected.append(f"{len(host_rows)} 台主机")
    audit(
        service, "credential_deleted", actor=user["username"],
        target=f"credential:{credential_id}",
        detail=f"{existing.get('name', '')}（影响 {'、'.join(affected) or '无引用'}）",
        ip=client_ip(request),
    )
    return {
        "ok": True,
        "affected_tasks": used_by,
        "affected_hosts": len(host_rows),
    }


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
