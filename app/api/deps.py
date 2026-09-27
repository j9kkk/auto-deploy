"""Shared FastAPI dependencies: session authentication and request context."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status

from .. import config
from ..schedule import iso, utcnow
from ..security import token_fingerprint
from ..service import Service

SESSION_COOKIE = "autodeploy_session"


def get_service(request: Request) -> Service:
    """The Service instance attached to the app at startup."""
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - means the app was misconfigured
        raise HTTPException(status_code=500, detail="服务尚未初始化")
    return service


def client_ip(request: Request) -> str:
    """Best-effort client address, honouring forwarded headers when trusted."""
    settings = config.load_settings()
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return real_ip.strip()[:64]
    if request.client is not None:
        return (request.client.host or "")[:64]
    return ""


def _resolve_session(request: Request, service: Service) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        return None
    row = service.store.sessions.find_valid(token_fingerprint(token))
    if row is None:
        return None
    # A password change invalidates every session issued before it.
    password_changed = row.get("password_changed_at")
    if password_changed and row.get("session_created_at") and password_changed > row["session_created_at"]:
        service.store.sessions.delete(token_fingerprint(token))
        return None
    return row


def current_session(
    request: Request, service: Service = Depends(get_service)
) -> dict[str, Any]:
    """Require a valid session; raise 401 otherwise."""
    session = _resolve_session(request, service)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或会话已过期",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return session


def optional_session(
    request: Request, service: Service = Depends(get_service)
) -> dict[str, Any] | None:
    return _resolve_session(request, service)


def current_user(session: dict[str, Any] = Depends(current_session)) -> dict[str, Any]:
    return {
        "id": session["user_id"],
        "username": session["username"],
        "display_name": session.get("display_name", ""),
        "is_admin": bool(session.get("is_admin")),
        "session_id": session["session_id"],
    }


def require_admin(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def set_session_cookie(response: Response, token: str, max_age: int, *, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")


def session_max_age(service: Service) -> int:
    return max(60, int(service.settings.session_ttl_hours) * 3600)


def audit(
    service: Service,
    action: str,
    *,
    actor: str = "",
    target: str = "",
    detail: str = "",
    ip: str = "",
) -> None:
    service.store.audit.record(action, actor=actor, target=target, detail=detail, ip=ip)


def now_iso() -> str:
    return iso(utcnow()) or ""
