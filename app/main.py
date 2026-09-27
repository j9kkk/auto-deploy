"""FastAPI application factory and process entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .api import auth as auth_routes
from .api import runs as run_routes
from .api import settings as settings_routes
from .api import stats as stats_routes
from .api import tasks as task_routes
from .service import Service

logger = logging.getLogger("autodeploy")

VERSION = "1.0.0"


def create_app(service: Service | None = None) -> FastAPI:
    """Build the ASGI application around a ``Service`` instance."""
    config.ensure_dirs()
    service = service or Service()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Start the scheduler with the app and stop it on shutdown."""
        service.start()
        banner = service.initial_password_message()
        if banner:
            # Written to stderr so it is visible even when stdout is captured.
            print(banner, file=sys.stderr, flush=True)
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(
        title="AutoDeploy",
        version=VERSION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.service = service

    # --- API routes -----------------------------------------------------
    app.include_router(auth_routes.router)
    app.include_router(task_routes.router)
    app.include_router(run_routes.router)
    app.include_router(stats_routes.router)
    app.include_router(settings_routes.router)

    # --- error handling -------------------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        """Return JSON for API calls, and let the SPA handle page routes."""
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=exc.status_code,
                content={"ok": False, "status": exc.status_code, "detail": exc.detail},
                headers=getattr(exc, "headers", None),
            )
        if exc.status_code == 404:
            return _index_response()
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "status": exc.status_code, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"ok": False, "detail": "请求参数无效", "errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("未处理的异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "detail": f"服务器内部错误: {exc.__class__.__name__}"},
        )

    # --- security headers ------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # The UI is fully self-hosted, so nothing may load from a CDN.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'self'",
        )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # --- static UI -------------------------------------------------------
    web_dir = config.WEB_DIR
    if web_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(web_dir / "assets")), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return _index_response()

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            icon = web_dir / "favicon.svg"
            if icon.exists():
                return FileResponse(icon, media_type="image/svg+xml")
            return JSONResponse(status_code=204, content=None)

        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str):
            """Serve index.html for client-side routes, but never for /api."""
            if path.startswith(("api/", "assets/")):
                return JSONResponse(status_code=404, content={"ok": False, "detail": "未找到"})
            candidate = (web_dir / path).resolve()
            try:
                candidate.relative_to(web_dir.resolve())
            except ValueError:
                return JSONResponse(status_code=400, content={"ok": False, "detail": "非法路径"})
            if candidate.is_file():
                return FileResponse(candidate)
            return _index_response()
    else:
        @app.get("/", include_in_schema=False)
        async def missing_ui() -> JSONResponse:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "detail": f"Web 目录不存在: {web_dir}"},
            )

    # --- lifecycle -------------------------------------------------------
    # Handled by the lifespan context defined above so startup and shutdown
    # share one code path (and the deprecated on_event hooks stay unused).

    return app


def _index_response() -> FileResponse:
    index = config.WEB_DIR / "index.html"
    if not index.exists():  # pragma: no cover - guarded by create_app
        return JSONResponse(status_code=500, content={"ok": False, "detail": "缺少 index.html"})
    return FileResponse(index, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autodeploy",
        description="AutoDeploy — 从 GitHub 自动拉取代码并部署到 Linux 服务器",
    )
    parser.add_argument("--host", default=None, help="监听地址（默认取配置或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8770）")
    parser.add_argument(
        "--initial-password",
        default=None,
        help="首次启动时设置管理员密码；不提供则随机生成并打印一次",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ./data）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    parser.add_argument("--version", action="version", version=f"AutoDeploy {VERSION}")
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    if args.data_dir:
        os.environ["AUTODEPLOY_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
        # config.DATA_DIR is resolved at import time, so reload it now.
        import importlib

        importlib.reload(config)

    config.ensure_dirs()
    service = Service(initial_password=args.initial_password)

    host = args.host or service.settings.host
    port = args.port or service.settings.port

    try:
        import uvicorn
    except ImportError:
        print(
            "缺少依赖 uvicorn，请先安装: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    app = create_app(service)

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print("AutoDeploy 正在启动…", file=sys.stderr, flush=True)
    # The credential banner itself is printed by the app's lifespan handler,
    # which runs for every entry point (including `uvicorn app.main:app`).
    print(f"  访问地址: {url}", file=sys.stderr, flush=True)
    print(f"  数据目录: {config.DATA_DIR}", file=sys.stderr, flush=True)
    print("  按 Ctrl+C 停止服务", file=sys.stderr, flush=True)

    if not args.no_browser and not args.reload:
        threading.Timer(1.2, lambda: _open_browser(url)).start()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=str(args.log_level).lower(),
        access_log=False,
        reload=False,
        timeout_keep_alive=65,
    )
    return 0


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a headless server has no browser
        pass


if __name__ == "__main__":
    raise SystemExit(main())
