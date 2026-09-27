"""The service container: one object that owns every long-lived component.

Both the HTTP API and the CLI entry point construct a ``Service``.  Keeping the
wiring in one place means the routes never reach for module-level globals, which
also makes the whole thing straightforward to construct inside a test.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config
from .db import Database
from .runner import DeployRunner
from .scheduler import Scheduler
from .security import LoginThrottle, generate_password, hash_password, password_problem
from .store import Store

logger = logging.getLogger("autodeploy.service")

DEFAULT_ADMIN_USERNAME = "admin"


@dataclass
class BootstrapResult:
    """What happened while preparing the service for first use."""

    created_admin: bool = False
    generated_password: str = ""
    username: str = ""
    recovered_runs: int = 0
    banner: str = ""


class Service:
    """Owns the database, the runner and the scheduler."""

    def __init__(self, *, data_dir: Path | None = None, initial_password: str | None = None) -> None:
        config.ensure_dirs()
        self.settings_path = config.settings_path()
        self.settings = config.load_settings()

        db_path = (data_dir / "autodeploy.db") if data_dir else config.DB_PATH
        self.db = Database(db_path)
        self.store = Store(self.db)
        self.runner = DeployRunner(self.store)
        self.scheduler = Scheduler(self.store, self.runner)
        self.throttle = LoginThrottle(
            self.settings.login_max_attempts, self.settings.login_lockout_seconds
        )
        self.bootstrap_report = BootstrapResult()
        # Listeners notified when a run changes state (used for SSE / polling hints).
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._listener_lock = threading.Lock()
        self._bootstrap(initial_password)

    # -- bootstrap ---------------------------------------------------------
    def _bootstrap(self, initial_password: str | None) -> None:
        """Create the first admin account and clear stale run state."""
        report = BootstrapResult()
        report.recovered_runs = self.runner.recover_interrupted_runs()
        # A restart invalidates any queued next-run time: interval tasks are
        # recomputed from now, so a paused service does not fire a burst of
        # catch-up deploys the moment it comes back up.
        self.store.tasks.clear_runtime_state()

        if self.store.users.count() == 0:
            password = initial_password or ""
            if not password:
                password = generate_password(20)
                report.generated_password = password
            else:
                problem = password_problem(password)
                if problem:
                    # An operator-supplied weak password is acceptable at
                    # bootstrap but must be reported, not silently accepted.
                    report.banner = (
                        f"警告: 初始密码不满足强度要求（{problem}），请登录后立即修改。"
                    )
            user_id = self.store.users.create(
                DEFAULT_ADMIN_USERNAME,
                hash_password(password),
                display_name="管理员",
                is_admin=True,
            )
            report.created_admin = True
            report.username = DEFAULT_ADMIN_USERNAME
            self.store.audit.record(
                "admin_bootstrapped", actor="system", target=f"user:{user_id}"
            )
            if not report.banner:
                report.banner = (
                    f"已创建管理员账号 {DEFAULT_ADMIN_USERNAME}，请立即登录并修改密码。"
                )
            logger.warning("已创建初始管理员账号 %s", DEFAULT_ADMIN_USERNAME)
        else:
            admin = self.store.users.get_by_username(DEFAULT_ADMIN_USERNAME)
            report.username = admin["username"] if admin else ""

        if report.recovered_runs:
            logger.warning("已将 %s 个中断的运行标记为失败", report.recovered_runs)
        self.bootstrap_report = report

    def initial_password_message(self) -> str:
        """One-time credential message printed to the console on first boot."""
        report = self.bootstrap_report
        if not report.created_admin:
            return ""
        lines = [
            "",
            "=" * 66,
            "  首次启动：已创建管理员账号",
            f"  用户名: {report.username}",
        ]
        if report.generated_password:
            lines.append(f"  初始密码: {report.generated_password}")
            lines.append("  （此密码仅显示这一次，请立即登录并修改）")
        else:
            lines.append("  初始密码: 由启动参数提供")
        lines.append("=" * 66),
        lines.append("")
        return "\n".join(lines)

    # -- listeners ---------------------------------------------------------
    def add_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with self._listener_lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with self._listener_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(event, payload)
            except Exception:  # noqa: BLE001 - a listener must not break a run
                logger.exception("事件监听器执行失败: %s", event)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.scheduler.on_dispatch = lambda task_id, run_id: self.notify(
            "run_dispatched", {"task_id": task_id, "run_id": run_id}
        )
        self.runner.on_finished = lambda run_id, status: self.notify(
            "run_finished", {"run_id": run_id, "status": status}
        )
        self.scheduler.start()
        # Give every enabled task a fresh next-run time so the schedule is
        # anchored to the moment the service came up.
        for task in self.store.tasks.list_enabled():
            self.scheduler.reschedule(int(task["id"]))
        logger.info("服务已启动")

    def stop(self) -> None:
        self.scheduler.stop()
        self.db.close()
        logger.info("服务已停止")

    # -- settings ----------------------------------------------------------
    def reload_settings(self) -> None:
        self.settings = config.load_settings()
        self.throttle.max_attempts = self.settings.login_max_attempts
        self.throttle.lockout_seconds = self.settings.login_lockout_seconds

    def save_settings(self, patch: dict[str, Any]) -> None:
        settings = config.load_settings()
        for key, value in patch.items():
            setattr(settings, key, value)
        config.save_settings(settings)
        self.settings = settings
        self.throttle.max_attempts = settings.login_max_attempts
        self.throttle.lockout_seconds = settings.login_lockout_seconds
        # A changed poll interval should take effect without a restart.
        self.scheduler.poke()

    def export_settings(self) -> dict[str, Any]:
        return config.load_settings().as_dict()

    # -- storage helpers ---------------------------------------------------
    def storage_report(self) -> dict[str, Any]:
        from .scheduler import disk_report

        return disk_report(self.store)

    def close(self) -> None:
        self.stop()
