"""The scheduler: decides when tasks run, and keeps the disk tidy.

One background thread owns all timing.  Every ``poll_interval_seconds`` it looks
for tasks whose ``next_run_at`` has passed and hands them to the runner.  The
next fire time is written **before** the run is dispatched, so a crash in
between cannot cause the same tick to fire twice on restart, and an interval
task advances from "now" rather than from the scheduled time so a slow deploy
does not produce a burst of catch-up runs.

Retention runs on a slower cadence (hourly) since it does bulk deletes.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from . import config, gitops
from .deployer import cleanup_path, directory_size
from .runner import DeployRunner
from .schedule import (
    ScheduleError,
    iso,
    iter_cron_minutes,
    next_run_time,
    parse_interval,
    utcnow,
)
from .store import Store

logger = logging.getLogger("autodeploy.scheduler")

# How often maintenance (retention, session cleanup) runs.
MAINTENANCE_INTERVAL_SECONDS = 3600


class Scheduler:
    """Background timing loop for scheduled deploys."""

    def __init__(self, store: Store, runner: DeployRunner) -> None:
        self.store = store
        self.runner = runner
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.RLock()
        self._last_maintenance = 0.0
        # Populated by the API layer; called after a run is dispatched so the
        # UI can be pushed an update.
        self.on_dispatch: Callable[[int, int], None] | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="autodeploy-scheduler", daemon=True
            )
            self._thread.start()
            logger.info("调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("调度器已停止")

    def poke(self) -> None:
        """Wake the loop immediately (after a task change, or a manual run)."""
        self._wake_event.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- main loop ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any error
                logger.exception("调度循环出现异常")
            interval = max(1, config.load_settings().poll_interval_seconds)
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def tick(self) -> int:
        """Dispatch one round of due tasks; returns how many were started."""
        settings = config.load_settings()
        now = utcnow()
        due = self.store.tasks.due(iso(now))
        started = 0
        for task in due:
            if self._stop_event.is_set():
                break
            if self.store.runs.count_active() >= settings.max_global_workers:
                # No worker slots left.  next_run_at stays in the past, so the
                # task is picked up by a later tick once capacity frees up.
                logger.info("达到并发上限 %s，暂缓调度", settings.max_global_workers)
                break
            if self._dispatch(task, trigger="schedule"):
                started += 1

        self._maybe_maintain(now)
        return started

    def _dispatch(self, task: dict[str, Any], *, trigger: str) -> bool:
        task_id = int(task["id"])
        if self.store.runs.has_active_for_task(task_id):
            settings = config.load_settings()
            if not settings.queue_while_running:
                # Skip this tick and reschedule, so a long deploy does not
                # stack up runs behind itself.
                self._advance(task)
                logger.info("任务 %s 仍在运行，跳过本次触发", task_id)
                return False

        # Record where the checkout sits now, so the runner can report the
        # before/after commits even if it is restarted mid-run.
        baseline = ""
        try:
            baseline = gitops.current_commit(
                config.workspace_dir(task_id),
                token=str(task.get("git_token") or ""),
                username=str(task.get("git_username") or ""),
            )
        except Exception:  # noqa: BLE001 - a missing workspace is normal
            baseline = ""

        self._advance(task)
        run_id = self.store.runs.create(task, trigger=trigger, commit_before=baseline)
        self.store.audit.record(
            "run_dispatched",
            actor="scheduler" if trigger == "schedule" else "system",
            target=f"task:{task_id}",
            detail=f"run:{run_id} trigger:{trigger}",
        )
        threading.Thread(
            target=self.runner.execute, args=(run_id,), name=f"deploy-{run_id}", daemon=True
        ).start()
        if self.on_dispatch is not None:
            try:
                self.on_dispatch(task_id, run_id)
            except Exception:  # noqa: BLE001
                pass
        return True

    def _advance(self, task: dict[str, Any]) -> None:
        """Recompute the task's next fire time after this tick."""
        task_id = int(task["id"])
        try:
            following = next_run_time(
                task.get("schedule_type") or "manual",
                task.get("schedule_expression") or "",
                reference=utcnow(),
                enabled=bool(task.get("enabled", 1)),
            )
        except ScheduleError as exc:
            logger.warning("任务 %s 的调度表达式无效: %s", task_id, exc)
            following = None
        self.store.tasks.set_next_run(task_id, iso(following))

    # -- public helpers ----------------------------------------------------
    def reschedule(self, task_id: int) -> str | None:
        """Recompute one task's next_run_at; used after create/update/toggle."""
        task = self.store.tasks.get(task_id)
        if task is None:
            return None
        try:
            following = next_run_time(
                task.get("schedule_type") or "manual",
                task.get("schedule_expression") or "",
                reference=utcnow(),
                enabled=bool(task.get("enabled", 1)),
            )
        except ScheduleError as exc:
            logger.warning("任务 %s 的调度表达式无效: %s", task_id, exc)
            following = None
        self.store.tasks.set_next_run(task_id, iso(following))
        self.poke()
        return iso(following)

    def run_now(self, task_id: int, *, trigger: str = "manual") -> int | None:
        """Queue an immediate run of a task, bypassing its schedule."""
        task = self.store.tasks.get(task_id)
        if task is None:
            return None
        run_id = self.store.runs.create(task, trigger=trigger)
        threading.Thread(
            target=self.runner.execute, args=(run_id,), name=f"deploy-{run_id}", daemon=True
        ).start()
        if self.on_dispatch is not None:
            try:
                self.on_dispatch(task_id, run_id)
            except Exception:  # noqa: BLE001
                pass
        return run_id

    def preview(self, schedule_type: str, expression: str, count: int = 5) -> list[str]:
        """Next fire times for the UI's schedule preview."""
        kind = (schedule_type or "").strip().lower()
        now = utcnow()
        if kind == "interval":
            try:
                seconds = parse_interval(expression)
            except ScheduleError:
                return []
            return [iso(now + timedelta(seconds=seconds * (index + 1))) or "" for index in range(count)]
        if kind == "cron":
            try:
                return [iso(moment) or "" for moment in iter_cron_minutes(expression, now, count)]
            except ScheduleError:
                return []
        return []

    # -- maintenance -------------------------------------------------------
    def _maybe_maintain(self, now) -> None:
        if time.monotonic() - self._last_maintenance < MAINTENANCE_INTERVAL_SECONDS:
            return
        self._last_maintenance = time.monotonic()
        try:
            self.maintenance()
        except Exception:  # noqa: BLE001
            logger.exception("维护任务失败")

    def maintenance(self) -> dict[str, int]:
        """Expire sessions, prune run history and old files."""
        settings = config.load_settings()
        report = {"sessions": 0, "runs": 0, "logs": 0, "releases": 0, "audit": 0}

        report["sessions"] = self.store.sessions.purge_expired()

        cutoff = None
        if settings.run_retention_days > 0:
            cutoff = iso(utcnow() - timedelta(days=settings.run_retention_days))
        log_paths_result = self.store.runs.purge_old(
            older_than_iso=cutoff, keep_count=max(0, settings.run_retention_count)
        )
        deleted_runs, log_paths = log_paths_result
        report["runs"] = deleted_runs
        for path in log_paths:
            try:
                Path(path).unlink(missing_ok=True)
                report["logs"] += 1
            except OSError:
                continue

        report["releases"] = self._prune_release_dirs(settings)
        report["audit"] = self.store.audit.purge_old(keep=2000)
        self._prune_orphan_workspaces()
        logger.info("维护完成: %s", report)
        return report

    def _prune_release_dirs(self, settings) -> int:
        """Trim release and artifact directories for every task."""
        removed = 0
        keep_releases = max(0, settings.release_retention_count)
        keep_artifacts = max(0, settings.artifact_retention_count)
        for task in self.store.tasks.list_all():
            task_id = int(task["id"])
            if keep_releases > 0:
                from .deployer import current_release, list_releases, resolve_release_paths

                root, link = resolve_release_paths(task)
                releases = list_releases(root)
                active = current_release(root, link)
                keepers = set(releases[:keep_releases])
                if active is not None:
                    keepers.add(active)
                for release in releases:
                    if release in keepers:
                        continue
                    cleanup_path(release)
                    removed += 1
            if keep_artifacts > 0:
                artifacts = sorted(
                    config.artifacts_dir(task_id).glob("*"),
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )
                for artifact in artifacts[keep_artifacts:]:
                    try:
                        artifact.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        continue
        return removed

    def _prune_orphan_workspaces(self) -> None:
        """Remove workspaces whose task no longer exists."""
        known = {int(task["id"]) for task in self.store.tasks.list_all()}
        root = config.WORKSPACES_DIR
        if not root.exists():
            return
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.startswith("task-"):
                continue
            try:
                task_id = int(entry.name.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if task_id not in known:
                cleanup_path(entry)
                logger.info("已清理孤立工作目录 %s", entry.name)

    # -- status ------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        settings = config.load_settings()
        active = self.store.runs.count_active()
        return {
            "running": self.running,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "max_global_workers": settings.max_global_workers,
            "active_runs": active,
            "capacity": max(0, settings.max_global_workers - active),
            "last_maintenance_seconds_ago": (
                round(time.monotonic() - self._last_maintenance, 1)
                if self._last_maintenance
                else None
            ),
        }


def disk_report(store: Store) -> dict[str, Any]:
    """Storage footprint of the service, for the statistics page."""
    settings = config.load_settings()
    workspaces = directory_size(config.WORKSPACES_DIR)
    releases = directory_size(config.RELEASES_DIR)
    artifacts = directory_size(config.ARTIFACTS_DIR)
    logs = directory_size(config.LOGS_DIR)
    usage = {}
    try:
        import shutil

        total, used, free = shutil.disk_usage(str(config.DATA_DIR))
        usage = {"total": total, "used": used, "free": free}
    except OSError:
        usage = {"total": 0, "used": 0, "free": 0}
    return {
        "workspaces_bytes": workspaces,
        "releases_bytes": releases,
        "artifacts_bytes": artifacts,
        "logs_bytes": logs,
        "total_bytes": workspaces + releases + artifacts + logs,
        "disk": usage,
        "run_retention_days": settings.run_retention_days,
        "release_retention_count": settings.release_retention_count,
    }


def workspace_summary(store: Store) -> list[dict[str, Any]]:
    """Per-task on-disk footprint, used by the tasks table."""
    rows: list[dict[str, Any]] = []
    for task in store.tasks.list_all():
        task_id = int(task["id"])
        from .deployer import current_release, list_releases, resolve_release_paths

        root, link = resolve_release_paths(task)
        try:
            releases = list_releases(root)
        except OSError:
            releases = []
        active = current_release(root, link)
        rows.append(
            {
                "task_id": task_id,
                "name": task.get("name"),
                "workspace": str(config.workspace_dir(task_id)),
                "releases_root": str(root),
                "releases": len(releases),
                "current": active.name if active else "",
                "artifacts": len(list(config.artifacts_dir(task_id).glob("*"))),
                "workspace_bytes": directory_size(config.workspace_dir(task_id)),
                "artifacts_bytes": directory_size(config.artifacts_dir(task_id)),
            }
        )
    return rows
