"""Authentication routes: login, logout, session info, password change."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..schedule import iso, utcnow
from ..security import (
    hash_password,
    new_token,
    needs_rehash,
    password_problem,
    public_user,
    token_fingerprint,
    verify_password,
)
from ..service import Service
from ..validation import ValidationError, validate_login, validate_password_change
from .deps import (
    audit,
    clear_session_cookie,
    client_ip,
    current_user,
    get_service,
    session_max_age,
    set_session_cookie,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(
    payload: dict[str, Any],
    request: Request,
    response: Response,
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    """Verify credentials and issue a session cookie."""
    ip = client_ip(request)
    try:
        credentials = validate_login(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    username = credentials["username"]
    throttle_key = f"{username.lower()}|{ip}"
    locked = service.throttle.locked_for(throttle_key)
    if locked:
        audit(service, "login_locked", actor=username, ip=ip, detail=f"剩余 {locked}s")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过于频繁，请在 {locked} 秒后重试",
            headers={"Retry-After": str(locked)},
        )

    user = service.store.users.get_by_username(username)
    if user is None or not verify_password(credentials["password"], user["password_hash"]):
        service.throttle.record_failure(throttle_key)
        remaining = service.throttle.remaining_attempts(throttle_key)
        audit(service, "login_failed", actor=username, ip=ip)
        detail = "用户名或密码错误"
        if remaining:
            detail += f"（剩余尝试次数 {remaining}）"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    service.throttle.reset(throttle_key)

    # Transparently upgrade a hash that predates the current parameters.
    if needs_rehash(user["password_hash"]):
        service.store.users.update_password(user["id"], hash_password(credentials["password"]))

    token = new_token()
    expires = utcnow() + timedelta(hours=service.settings.session_ttl_hours)
    service.store.sessions.create(
        token_fingerprint(token),
        int(user["id"]),
        iso(expires) or "",
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    service.store.users.touch_login(int(user["id"]))
    set_session_cookie(
        response, token, session_max_age(service), secure=service.settings.secure_cookies
    )
    audit(service, "login_success", actor=user["username"], ip=ip)

    return {
        "ok": True,
        "user": public_user(user),
        "expires_at": iso(expires),
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(current_user),
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    token = request.cookies.get("autodeploy_session") or ""
    if token:
        service.store.sessions.delete(token_fingerprint(token))
    clear_session_cookie(response)
    audit(service, "logout", actor=user["username"], ip=client_ip(request))
    return {"ok": True}


@router.get("/me")
def me(
    user: dict[str, Any] = Depends(current_user),
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    row = service.store.users.get(int(user["id"]))
    if row is None:
        raise HTTPException(status_code=401, detail="账号不存在")
    sessions = [
        entry
        for entry in service.store.sessions.list_active()
        if int(entry["user_id"]) == int(user["id"])
    ]
    return {
        "user": public_user(row),
        "session_count": len(sessions),
        "bootstrap": {
            "created_admin": service.bootstrap_report.created_admin,
            "banner": service.bootstrap_report.banner,
        },
    }


@router.post("/password")
def change_password(
    payload: dict[str, Any],
    request: Request,
    response: Response,
    user: dict[str, Any] = Depends(current_user),
    service: Service = Depends(get_service),
) -> dict[str, Any]:
    """Change the password and invalidate every other session."""
    ip = client_ip(request)
    try:
        body = validate_password_change(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc

    row = service.store.users.get(int(user["id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not verify_password(body["current_password"], row["password_hash"]):
        audit(service, "password_change_failed", actor=user["username"], ip=ip)
        raise HTTPException(status_code=400, detail="当前密码不正确")

    problem = password_problem(body["new_password"])
    if problem:
        raise HTTPException(status_code=422, detail={"errors": {"new_password": problem}})

    service.store.users.update_password(int(user["id"]), hash_password(body["new_password"]))
    # Keep the caller signed in but drop every other session, which is the
    # behaviour an operator expects after rotating a leaked password.
    token = request.cookies.get("autodeploy_session") or ""
    service.store.sessions.delete_for_user(
        int(user["id"]), keep_token_hash=token_fingerprint(token) if token else None
    )
    service.throttle.reset(f"{user['username'].lower()}|{ip}")
    audit(service, "password_changed", actor=user["username"], ip=ip)

    new_token_value = new_token()
    expires = utcnow() + timedelta(hours=service.settings.session_ttl_hours)
    service.store.sessions.create(
        token_fingerprint(new_token_value),
        int(user["id"]),
        iso(expires) or "",
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    set_session_cookie(
        response, new_token_value, session_max_age(service),
        secure=service.settings.secure_cookies,
    )
    return {"ok": True, "message": "密码已更新，其他登录会话已失效"}
