"""The deploy runner: a thread pool that executes one run at a time.

``DeployRunner`` owns everything mutable about an in-flight run — the log file
handle, the running process handles, the cancellation flag — so the rest of the
service never has to.  Runs are executed on worker threads with a bounded pool,
which is what enforces ``max_global_workers``.

Log lines are appended to a per-run file and simultaneously kept in a small
in-memory ring buffer, so the web UI can tail a run without reading the whole
file on every poll.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from pathlib import Path

from . import config, gitops
from .deployer import (
    DeployContext,
    DeployError,
    REPO_FREE_METHODS,
    _prune_remote_releases,
    compose_inline_dir,
    create_bundle,
    migrate_legacy_compose_inline,
    next_release_dir,
    preflight,
    prune_releases,
    resolve_release_paths,
    run_deploy,
    run_script_stage,
    stage_release,
    parse_path_list,
    rollback_task,
)
from .executor import ProcessHandle, redact
from .schedule import format_local, iso, utcnow
from .store import Store
from .target import (
    STAGE_EXCLUDES,
    UPLOAD_EXCLUDES,
    TargetError,
    build_target,
    resolve_remote_layout,
)
from .validation import describe_host

# Cap on how much of a log is kept in memory for the live tail view. This must
# stay at least as large as the window the /api/runs/{id}/tail endpoint reports,
# otherwise the client's line offset would run past what the server can serve
# and force a needless resynchronisation.
LOG_TAIL_LINES = 2000
LOG_TAIL_BYTES = 1_000_000

# 日志文件周期性 flush 的行距。文件句柄是块缓冲的，崩溃（kill -9 / 容器硬重启）
# 时未落盘的尾部会丢失；默认缓冲下最多可丢约 8KB 已展示的输出。每写若干行
# 强制 flush 一次（flush 只推到 OS 页缓存，不开销 fsync），把丢失窗口压到几十行。
LOG_FLUSH_EVERY_LINES = 64


@dataclass
class ActiveRun:
    """Mutable state for a run currently held by a worker."""

    run_id: int
    task_id: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    handles: list[ProcessHandle] = field(default_factory=list)
    tail: deque[str] = field(default_factory=deque)
    tail_bytes: int = 0
    # 累计写入行数（不随环形缓冲裁剪而减少），用作增量轮询的快速游标比对。
    tail_count: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)
    log_file: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Exit code of the most recent command, reported as the run's exit code.
    last_exit_code: int | None = None
    # 在流水线开始时一次性捕获，避免每写一行日志都读配置。
    log_max_bytes: int = 1_000_000
    # 距上次 flush 累计写入的行数（周期落盘用）。
    lines_since_flush: int = 0
    # 文件因超过大小上限而提前关闭后置位；运行结束时据此用内存尾部重写文件。
    log_truncated: bool = False

    def append_tail(self, line: str) -> None:
        with self.lock:
            self.tail.append(line)
            self.tail_bytes += len(line) + 1
            self.tail_count += 1
            self._trim_locked()

    def append_tail_batch(self, lines: list[str]) -> None:
        """批量入缓冲：一次加锁处理一批行，高频输出时摊薄锁竞争。"""
        if not lines:
            return
        with self.lock:
            for line in lines:
                self.tail.append(line)
                self.tail_bytes += len(line) + 1
                self.tail_count += 1
            self._trim_locked()

    def _trim_locked(self) -> None:
        # 调用方必须已持有 self.lock。
        while self.tail and (
            len(self.tail) > LOG_TAIL_LINES or self.tail_bytes > LOG_TAIL_BYTES
        ):
            removed = self.tail.popleft()
            self.tail_bytes -= len(removed) + 1

    def snapshot_tail(self, limit: int = LOG_TAIL_LINES) -> list[str]:
        with self.lock:
            lines = list(self.tail)
        return lines[-limit:]

    def tail_total(self) -> int:
        """累计输出行数（含已被环形缓冲裁掉的），供增量轮询快速比对。"""
        with self.lock:
            return self.tail_count


def resolve_git_depth(task: Mapping[str, Any]) -> int:
    """任务的拉取深度；0 表示完整历史，缺失时回退 1（浅克隆）。

    不能用 ``task.get("git_depth") or 1``：那会把界面上的 0（完整历史）
    当成缺省值吞掉，使完整历史永远退化为浅克隆。
    """
    raw = task.get("git_depth")
    if raw is None or raw == "":
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


class DeployRunner:
    """Owns run execution, cancellation and the worker pool."""

    def __init__(self, store: Store, *, on_finished: Callable[[int, str], None] | None = None) -> None:
        self.store = store
        self._active: dict[int, ActiveRun] = {}
        self._active_lock = threading.RLock()
        # Called after a run finishes, so the API can push an update.
        self.on_finished = on_finished

    # -- lifecycle ---------------------------------------------------------
    def recover_interrupted_runs(self) -> int:
        """Mark runs left active by a previous process as failed."""
        stale = self.store.runs.active()
        for run in stale:
            self.store.runs.mark_finished(
                int(run["id"]),
                status="failed",
                exit_code=None,
                error="服务重启导致运行中断",
            )
            self.store.audit.record(
                "run_interrupted", target=f"run:{run['id']}", detail="服务重启"
            )
        return len(stale)

    def active_run_ids(self) -> list[int]:
        with self._active_lock:
            return list(self._active.keys())

    def is_active(self, run_id: int) -> bool:
        with self._active_lock:
            return run_id in self._active

    def tail_meta(self, run_id: int) -> tuple[bool, int | None]:
        """活跃运行轻量探测：返回 (是否活跃, 累计日志行数或 None)。

        供增量轮询端点在客户端已追平输出时跳过整个 tail 快照的拷贝。
        """
        with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            return False, None
        return True, active.tail_total()

    def cancel(self, run_id: int) -> bool:
        """Request cancellation; the worker stops at the next safe point."""
        with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            # Still queued: mark the request so the worker skips it on pickup.
            return self.store.runs.request_cancel(run_id)
        active.cancel_event.set()
        for handle in list(active.handles):
            handle.mark_cancelled()
        return True

    def execute(self, run_id: int) -> None:
        """Run a single deploy pipeline.  Never raises."""
        run = self.store.runs.get(run_id)
        if run is None:
            return
        task_row = self.store.tasks.get_internal(int(run["task_id"]))
        if task_row is None:
            self.store.runs.mark_finished(
                run_id, status="failed", exit_code=None, error="任务已被删除"
            )
            return

        if run["cancel_requested"]:
            self.store.runs.mark_finished(
                run_id, status="cancelled", exit_code=None, error="任务在开始前已被取消"
            )
            return

        active = ActiveRun(
            run_id=run_id,
            task_id=int(run["task_id"]),
            log_max_bytes=config.load_settings().log_max_bytes,
        )
        log_path = config.run_log_path(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            active.log_file = log_path.open("a", encoding="utf-8", errors="replace")
        except OSError:
            active.log_file = None

        with self._active_lock:
            self._active[run_id] = active
        self.store.runs.mark_running(run_id)
        self.store.tasks.record_run_started(int(run["task_id"]))

        status = "failed"
        error = ""
        duration_ms = 0
        exit_code: int | None = None
        started = time.monotonic()

        def log(line: str) -> None:
            self._write_log(active, line)

        try:
            log("=" * 72)
            log(f"任务: {task_row.get('name', '')} (id={task_row['id']})  运行: #{run_id}")
            log(f"触发方式: {run.get('trigger', 'manual')}   开始时间: {format_local(utcnow())}")
            log("=" * 72)
            result = self._pipeline(
                task_row, run_id, active, log, trigger=str(run.get("trigger") or "manual")
            )
            status = result[0]
            exit_code = result[1]
            error = ""
        except DeployError as exc:
            status = "cancelled" if active.cancel_event.is_set() else "failed"
            error = str(exc)
            exit_code = active.last_exit_code
            log(f"! {error}")
        except gitops.GitError as exc:
            error = str(exc)
            if active.cancel_event.is_set():
                # A cancelled git command also exits non-zero; report it as a
                # cancellation rather than as a repository problem.
                status = "cancelled"
                error = "任务已被取消"
                log("! 任务已取消")
            else:
                status = "failed"
                log(f"! git 操作失败: {error}")
        except Exception as exc:  # noqa: BLE001 - a run must never kill the worker
            status = "failed"
            error = f"未预期的错误: {exc.__class__.__name__}: {exc}"
            log(f"! {error}")
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if active.cancel_event.is_set() and status == "failed":
                status = "cancelled"
            if exit_code is None:
                exit_code = active.last_exit_code
            if status == "success":
                exit_code = active.last_exit_code if active.last_exit_code is not None else 0
            log("-" * 72)
            log(f"结果: {STATUS_LABELS.get(status, status)}   耗时: {duration_ms / 1000:.1f}s")
            if error:
                log(f"错误: {error}")
            log("=" * 72)

            log_bytes = self._close_log(active, log_path)

            self.store.runs.mark_finished(
                run_id, status=status, exit_code=exit_code,
                error=error, duration_ms=duration_ms,
            )
            self.store.runs.set_log(run_id, str(log_path), log_bytes)
            self.store.tasks.record_run_finished(
                int(task_row["id"]), run_id=run_id, status=status, duration_ms=duration_ms
            )
            with self._active_lock:
                self._active.pop(run_id, None)

            self._notify(task_row, run_id, status, duration_ms, error)
            if self.on_finished is not None:
                try:
                    self.on_finished(run_id, status)
                except Exception:  # noqa: BLE001 - notification must not break cleanup
                    pass

    # -- pipeline ----------------------------------------------------------
    def _pipeline(
        self,
        task: dict[str, Any],
        run_id: int,
        active: ActiveRun,
        log: Callable[[str], None],
        *,
        trigger: str,
    ) -> tuple[str, int | None]:
        task_id = int(task["id"])
        settings = config.load_settings()
        check = active.cancel_event.is_set

        # 目标主机：为空即本机，走原有整条路径（远程能力是并列分支）。
        host_row = None
        if task.get("host_id"):
            host_row = self.store.hosts.get(int(task["host_id"]))
            if host_row is None:
                raise DeployError(
                    f"任务指定的目标主机 #{task['host_id']} 不存在，可能已被删除；"
                    "请在任务配置里重新选择目标主机"
                )
        if host_row is not None:
            if str(task.get("deploy_method") or "").lower() in REPO_FREE_METHODS:
                return self._pipeline_repo_free(
                    task, run_id, active, log, trigger=trigger, host_row=host_row,
                    settings=settings, check=check,
                )
            return self._pipeline_remote(
                task, run_id, active, log, trigger=trigger, host_row=host_row,
                settings=settings, check=check,
            )

        # Bundled per-task environment variables, then the task's own.
        env = self._task_env(task)

        if str(task.get("deploy_method") or "").lower() in REPO_FREE_METHODS:
            return self._pipeline_repo_free(
                task, run_id, active, log, trigger=trigger, host_row=None,
                settings=settings, check=check,
            )

        log("--- 阶段: 环境检查 ---")
        for check_result in preflight(task):
            mark = "✓" if check_result["ok"] else "✗"
            log(f"{mark} {check_result['name']}")
            if not check_result["ok"]:
                log(f"! {check_result['message']}")
                raise DeployError(check_result["message"])

        # 工作目录以任务名命名；老目录 task-<id> 首次运行时自动改名迁移。
        workspace = config.migrate_workspace_to_name(task, log=log)
        releases_root, current_link = resolve_release_paths(task)
        artifacts_root = config.artifacts_dir(task_id)
        creds_dir = gitops.temp_credential_dir(config.TMP_DIR / f"task-{task_id}")
        # 代理以环境变量下发（不走 git -c，避免密码出现在进程列表里）。
        proxy = config.proxy_env(settings)

        # 凭据：优先用任务引用的全局凭据，否则回退到任务自带的令牌。
        credential_row = None
        credential_id = task.get("credential_id")
        if credential_id:
            credential_row = self.store.credentials.get(int(credential_id))
            if credential_row is None:
                log(f"! 引用的凭据 #{credential_id} 不存在，将尝试任务自带的令牌")
        credential = gitops.resolve_credential(task, credential_row)
        if credential_row is not None:
            log(f"凭据: {credential_row.get('name', '')}（{gitops.KIND_LABELS.get(str(credential_row.get('kind')), '')}）")
        elif credential.has_secret:
            log("凭据: 使用任务内配置的令牌")

        log("--- 阶段: 拉取代码 ---")
        log(f"仓库: {redact(task['repo_url'])}  分支: {task['repo_branch']}")
        if proxy:
            log(f"代理: {config.describe_proxy(settings)}")
        before = (task.get("_last_commit") or "").strip()
        if not before:
            before = gitops.current_commit(
                workspace,
                token=credential.token,
                username=credential.username,
                proxy=proxy,
            )
            if before:
                log(f"当前工作副本: {before[:8]}")

        info = gitops.sync_checkout(
            repo_url=task["repo_url"],
            branch=task["repo_branch"],
            workspace=workspace,
            depth=resolve_git_depth(task),
            token=credential.token,
            username=credential.username,
            credential=credential,
            proxy=proxy,
            home=creds_dir,
            tmp_dir=creds_dir,
            timeout=int(settings.git_timeout_seconds),
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
        )
        if check():
            raise DeployError("任务已被取消")

        log(f"提交: {info.short_commit}  {info.message}")
        if info.author:
            log(f"作者: {info.author}   时间: {info.committed_at}")
        if info.previous_commit:
            log(f"上一个提交: {info.previous_commit[:8]}")
        if info.changed_files:
            log(f"本次变更文件数: {info.changed_files}")

        self.store.runs.update_checkout_info(
            run_id,
            commit_after=info.commit,
            commit_message=info.message,
            commit_author=info.author,
            commit_time=info.committed_at,
            branch=info.branch,
            changed_files=max(0, info.changed_files),
            changes_detected=info.changes_detected,
        )

        if task.get("skip_if_no_changes", True) and not info.changes_detected:
            log("代码无变化，跳过本次部署。")
            return "skipped", 0

        patterns = parse_path_list(task.get("artifact_paths"))
        release_dir = next_release_dir(releases_root, info.commit)
        artifact_name = f"{_safe_name(task.get('name') or 'task')}-{info.short_commit or 'nogit'}-{run_id}.tar.gz"
        artifact_path = artifacts_root / artifact_name

        ctx = DeployContext(
            task=task,
            run_id=run_id,
            workspace=workspace,
            release_dir=release_dir,
            releases_root=releases_root,
            current_link=current_link,
            artifact_path=artifact_path,
            commit=info.commit,
            previous_commit=info.previous_commit,
            branch=info.branch,
            env=env,
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
            # Share the live handle list so a cancel request reaches the
            # process a deploy stage is currently running.
            handles=active.handles,
            # 构建脚本（npm/pip 等）也可能需要联网，代理按开关注入。
            proxy=config.proxy_env(settings, for_scripts=True),
        )

        method = ctx.deploy_method
        needs_release = method != "script" or bool(patterns)

        log("--- 阶段: 准备脚本 ---")
        prepare = (task.get("prepare_script") or "").strip()
        if prepare:
            run_script_stage(ctx, prepare, stage="prepare")
        else:
            log("(未配置准备脚本，跳过)")

        if check():
            raise DeployError("任务已被取消")

        if needs_release:
            log("--- 阶段: 暂存发布目录 ---")
            log(f"发布目录: {release_dir}")
            stage_release(ctx, patterns=patterns, full_copy=not patterns)
        else:
            log("--- 阶段: 暂存发布目录 (纯脚本任务，跳过) ---")
            release_dir.mkdir(parents=True, exist_ok=True)

        log("--- 阶段: 打包 ---")
        size = create_bundle(release_dir, artifact_path)
        log(f"产物: {artifact_path} ({_human_size(size)})")
        self.store.runs.update_outputs(
            run_id, release_dir=str(release_dir), artifact_path=str(artifact_path)
        )

        if check():
            raise DeployError("任务已被取消")

        run_deploy(ctx)

        keep = int(task.get("keep_releases") or settings.release_retention_count)
        if keep > 0:
            prune_releases(releases_root, current_link, keep=keep, log=log)

        active.last_exit_code = ctx.last_exit_code
        return "success", ctx.last_exit_code

    # -- 不依赖仓库的流水线 ------------------------------------------------
    def _pipeline_repo_free(
        self,
        task: dict[str, Any],
        run_id: int,
        active: ActiveRun,
        log: Callable[[str], None],
        *,
        trigger: str,
        host_row: dict[str, Any] | None,
        settings: config.Settings,
        check: Callable[[], bool],
    ) -> tuple[str, int | None]:
        """执行「Compose 内容直填」：无仓库、无源码、无打包。

        与正常流水线的差异只有「没有输入可拉」这一件事：
        * 不做 git（任务本就没有仓库地址），也 skipped 不适用于无提交的场景；
        * 不暂存发布目录、不打包产物，发布目录仍按惯例占位，供 compose 文件
          落在固定子目录里（见 deployer.COMPOSE_INLINE_DIR）；
        * 部署、清理与运行记录写入完全复用既有实现。
        """
        task_id = int(task["id"])
        proxy = config.proxy_env(settings, for_scripts=True)
        env = self._task_env(task)

        log("--- 阶段: 环境检查 ---")
        inspect_target = None
        if host_row is not None:
            credential_row = None
            if host_row.get("credential_id"):
                credential_row = self.store.credentials.get(int(host_row["credential_id"]))
            try:
                inspect_target = build_target(
                    host_row, credential_row,
                    work_dir=config.TMP_DIR / f"task-{task_id}-host",
                )
            except TargetError as exc:
                raise DeployError(str(exc)) from exc
            log(f"目标主机: {describe_host(host_row)}")
        for check_result in preflight(task, target=inspect_target):
            mark = "✓" if check_result["ok"] else "✗"
            log(f"{mark} {check_result['name']}")
            if not check_result["ok"]:
                log(f"! {check_result['message']}")
                raise DeployError(check_result["message"])

        # 两种执行位置各自解析路径：本机用 Path，远程用目标机上的字符串。
        artifact_name = f"{_safe_name(task.get('name') or 'task')}-{run_id}.tar.gz"
        if host_row is not None:
            layout = resolve_remote_layout(host_row, task)
            inspect_target.make_dirs(layout.base_dir)
            inspect_target.make_dirs(layout.releases_root)
            releases_root: Any = layout.releases_root
            current_link: Any = layout.current_link
            artifact: Any = f"{layout.base_dir.rstrip('/')}/artifacts/{artifact_name}"
            # 远程的持久化目录同样在 releases 之外，按任务 slug 独立存放。
            work_dir: Any = f"{layout.base_dir.rstrip('/')}/persistent/{_safe_name(task.get('name') or 'task')}"
            inspect_target.make_dirs(str(work_dir))
        else:
            local_releases_root, local_current_link = resolve_release_paths(task)
            local_releases_root.mkdir(parents=True, exist_ok=True)
            releases_root = local_releases_root
            current_link = local_current_link
            artifact = config.artifacts_dir(task_id) / artifact_name
            work_dir = compose_inline_dir(task_id)
            # 迁移必须在建目录之前：旧数据要落到一个还不存在的目标上。
            # 先 mkdir 会让迁移判定「目标已存在」而跳过，用户升级后看到的
            # 就是一个空目录，旧数据仍在 releases 下「消失」。
            migrate_legacy_compose_inline(task, local_releases_root, log=log)
            work_dir.mkdir(parents=True, exist_ok=True)
        log(f"工作目录: {work_dir}（compose 文件与容器相对挂载都以它为基准）")

        remote = host_row is not None
        ctx = DeployContext(
            task=task,
            run_id=run_id,
            # 没有源码可引用：工作目录就是 compose 文件所在的固定目录，
            # 这样准备脚本里的相对路径也有确定落点。
            workspace=Path(str(work_dir)) if not remote else Path(),
            release_dir=Path(str(work_dir)) if not remote else Path(),
            releases_root=Path(str(releases_root)) if not remote else Path(),
            current_link=Path(str(current_link)) if not remote else Path(),
            artifact_path=Path(str(artifact)) if not remote else Path(),
            persistent_dir=work_dir,
            env=env,
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
            handles=active.handles,
            proxy=proxy,
            target=inspect_target,
            remote_workspace=str(work_dir) if remote else "",
            remote_release_dir=str(work_dir) if remote else "",
            remote_releases_root=str(releases_root) if remote else "",
            remote_current_link=str(current_link) if remote else "",
            remote_artifact_path=str(artifact) if remote else "",
        )
        self.store.runs.update_outputs(run_id, release_dir=str(work_dir), artifact_path="")

        log("--- 阶段: 准备脚本 ---")
        prepare = (task.get("prepare_script") or "").strip()
        if prepare:
            run_script_stage(ctx, prepare, stage="prepare")
        else:
            log("(未配置准备脚本，跳过)")

        if check():
            raise DeployError("任务已被取消")

        log("--- 阶段: 部署（Compose 内容直填） ---")
        run_deploy(ctx)
        # 该方式没有版本产物，不触发历史清理：compose-inline 目录必须长期保留。

        active.last_exit_code = ctx.last_exit_code
        return "success", ctx.last_exit_code

    # -- 远程流水线 --------------------------------------------------------
    def _pipeline_remote(
        self,
        task: dict[str, Any],
        run_id: int,
        active: ActiveRun,
        log: Callable[[str], None],
        *,
        trigger: str,
        host_row: dict[str, Any],
        settings: config.Settings,
        check: Callable[[], bool],
    ) -> tuple[str, int | None]:
        """把同一套流水线落到目标主机上执行。

        与本地路径的关键差异：
        * git 仍只在控制器执行（凭据不出控制器），源码经 rsync 镜像到目标机；
        * 环境检查分两段：控制器查 git/tar，目标机查 rsync/方法与基础目录可写；
        * 发布目录、打包、软链、清理全部在目标机上完成；
        * 产物默认留在目标机，仅 ``artifact`` 方式回传控制器供下载。
        """
        task_id = int(task["id"])
        host_label = describe_host(host_row)

        credential_row = None
        if host_row.get("credential_id"):
            credential_row = self.store.credentials.get(int(host_row["credential_id"]))
        try:
            target = build_target(
                host_row, credential_row,
                work_dir=config.TMP_DIR / f"task-{task_id}-host",
            )
        except TargetError as exc:
            raise DeployError(str(exc)) from exc

        layout = resolve_remote_layout(host_row, task)
        log(f"目标主机: {host_label}（{layout.base_dir}）")
        log(f"远端工作目录: {layout.workspace}")

        # 1) 环境检查：控制器与目标机分开报，用户能一眼看出缺在哪一侧。
        log("--- 阶段: 环境检查（控制器） ---")
        for check_result in preflight(task, target=None):
            mark = "✓" if check_result["ok"] else "✗"
            log(f"{mark} {check_result['name']}")
            if not check_result["ok"]:
                log(f"! {check_result['message']}")
                raise DeployError(check_result["message"])

        log("--- 阶段: 环境检查（目标主机） ---")
        for check_result in preflight(task, target=target):
            mark = "✓" if check_result["ok"] else "✗"
            log(f"{mark} {check_result['name']}")
            if not check_result["ok"]:
                log(f"! {check_result['message']}")
                raise DeployError(check_result["message"])
        # 基础目录必须可写，否则后续所有步骤都会以难懂的方式失败。
        probe_write = target.run(["test", "-w", layout.base_dir], timeout=20)
        if not probe_write.ok:
            target.make_dirs(layout.base_dir)
        log(f"✓ 目标目录可写: {layout.base_dir}")

        # 2) 控制器侧 git：与本地路径完全一致，只是不用于最终部署位置。
        env = self._task_env(task)
        workspace = config.migrate_workspace_to_name(task, log=log)
        creds_dir = gitops.temp_credential_dir(config.TMP_DIR / f"task-{task_id}")
        proxy = config.proxy_env(settings)

        credential_id = task.get("credential_id")
        git_credential_row = None
        if credential_id:
            git_credential_row = self.store.credentials.get(int(credential_id))
            if git_credential_row is None:
                log(f"! 引用的凭据 #{credential_id} 不存在，将尝试任务自带的令牌")
        credential = gitops.resolve_credential(task, git_credential_row)

        log("--- 阶段: 拉取代码（控制器） ---")
        log(f"仓库: {redact(task['repo_url'])}  分支: {task['repo_branch']}")
        if proxy:
            log(f"代理: {config.describe_proxy(settings)}")
        before = gitops.current_commit(
            workspace, token=credential.token, username=credential.username, proxy=proxy,
        )
        if before:
            log(f"当前工作副本: {before[:8]}")

        info = gitops.sync_checkout(
            repo_url=task["repo_url"],
            branch=task["repo_branch"],
            workspace=workspace,
            depth=resolve_git_depth(task),
            token=credential.token,
            username=credential.username,
            credential=credential,
            proxy=proxy,
            home=creds_dir,
            tmp_dir=creds_dir,
            timeout=int(settings.git_timeout_seconds),
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
        )
        if check():
            raise DeployError("任务已被取消")

        log(f"提交: {info.short_commit}  {info.message}")
        if info.author:
            log(f"作者: {info.author}   时间: {info.committed_at}")
        self.store.runs.update_checkout_info(
            run_id,
            commit_after=info.commit,
            commit_message=info.message,
            commit_author=info.author,
            commit_time=info.committed_at,
            branch=info.branch,
            changed_files=max(0, info.changed_files),
            changes_detected=info.changes_detected,
        )

        if task.get("skip_if_no_changes", True) and not info.changes_detected:
            log("代码无变化，跳过本次部署。")
            return "skipped", 0

        # 3) 源码镜像到目标机（增量、排除 .git）。
        log("--- 阶段: 同步源码到目标主机 ---")
        upload = target.upload(
            workspace,
            layout.workspace,
            delete=True,
            excludes=list(UPLOAD_EXCLUDES),
            timeout=int(settings.git_timeout_seconds) * 2,
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            handle_out=active.handles,
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
        )
        if not upload.ok:
            reason = "已取消" if upload.cancelled else (upload.error or f"退出码 {upload.exit_code}")
            raise DeployError(f"同步源码到目标主机失败: {reason}")
        if check():
            raise DeployError("任务已被取消")

        # 4) 发布目录命名与上下文。
        patterns = parse_path_list(task.get("artifact_paths"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        short = (info.commit or "nogit")[:8]
        release_name = f"{stamp}-{short}"
        remote_release_dir = f"{layout.releases_root.rstrip('/')}/{release_name}"
        target.make_dirs(layout.releases_root)
        # 同秒同提交重名时追加序号，避免覆盖上一份发布。
        suffix = 1
        while target.exists(remote_release_dir):
            suffix += 1
            release_name = f"{stamp}-{short}-{suffix}"
            remote_release_dir = f"{layout.releases_root.rstrip('/')}/{release_name}"

        artifact_name = f"{_safe_name(task.get('name') or 'task')}-{short}-{run_id}.tar.gz"
        remote_artifact = f"{layout.base_dir.rstrip('/')}/artifacts/{artifact_name}"

        ctx = DeployContext(
            task=task,
            run_id=run_id,
            workspace=workspace,
            release_dir=Path(remote_release_dir),
            releases_root=Path(layout.releases_root),
            current_link=Path(layout.current_link),
            artifact_path=Path(remote_artifact),
            commit=info.commit,
            previous_commit=info.previous_commit,
            branch=info.branch,
            env=env,
            log=log,
            log_batch=lambda lines: self._write_log_batch(active, lines),
            check_cancelled=check,
            kill_grace_seconds=settings.kill_grace_seconds,
            handles=active.handles,
            proxy=config.proxy_env(settings, for_scripts=True),
            target=target,
            remote_workspace=layout.workspace,
            remote_release_dir=remote_release_dir,
            remote_releases_root=layout.releases_root,
            remote_current_link=layout.current_link,
            remote_artifact_path=remote_artifact,
        )

        method = ctx.deploy_method
        needs_release = method != "script" or bool(patterns)

        log("--- 阶段: 准备脚本（目标主机） ---")
        prepare = (task.get("prepare_script") or "").strip()
        if prepare:
            run_script_stage(ctx, prepare, stage="prepare")
        else:
            log("(未配置准备脚本，跳过)")

        if check():
            raise DeployError("任务已被取消")

        log("--- 阶段: 暂存发布目录（目标主机） ---")
        log(f"发布目录: {remote_release_dir}")
        if needs_release:
            target.make_dirs(remote_release_dir)
            source_dir = ctx.work_source_dir
            copy_patterns = list(STAGE_EXCLUDES) if not patterns else []
            target.copy_tree(source_dir, remote_release_dir, excludes=copy_patterns)
        else:
            target.make_dirs(remote_release_dir)

        log("--- 阶段: 打包（目标主机） ---")
        size = target.bundle(remote_release_dir, remote_artifact)
        log(f"产物: {remote_artifact} ({_human_size(size)})")
        # artifact 方式的产物是交付物，回传到控制器供下载；其它方式留在目标机。
        local_artifact_path = ""
        if method == "artifact":
            local_dir = config.artifacts_dir(task_id)
            local_dir.mkdir(parents=True, exist_ok=True)
            local_file = local_dir / artifact_name
            log("正在把产物回传到控制机…")
            download = target.download(
                remote_artifact,
                local_file,
                timeout=int(settings.git_timeout_seconds),
                log=log,
                log_batch=lambda lines: self._write_log_batch(active, lines),
                handle_out=active.handles,
                check_cancelled=check,
                kill_grace_seconds=settings.kill_grace_seconds,
            )
            if download.ok:
                local_artifact_path = str(local_file)
            else:
                log(f"! 产物回传失败（产物仍保留在目标主机 {remote_artifact}）")
        self.store.runs.update_outputs(
            run_id,
            release_dir=remote_release_dir,
            artifact_path=local_artifact_path or remote_artifact,
        )

        if check():
            raise DeployError("任务已被取消")

        run_deploy(ctx)

        keep = int(task.get("keep_releases") or settings.release_retention_count)
        if keep > 0:
            _prune_remote_releases(target, layout, keep=keep, log=log)

        active.last_exit_code = ctx.last_exit_code
        return "success", ctx.last_exit_code

    # -- rollback ----------------------------------------------------------
    def execute_rollback(
        self,
        run_id: int,
        task_row: dict[str, Any],
        selected_run: dict[str, Any] | None,
        *,
        timeout: int,
        kill_grace_seconds: int,
    ) -> None:
        """后台执行一次回滚，作为 run 记录呈现。调用方已完成准入校验。"""
        active = ActiveRun(
            run_id=run_id,
            task_id=int(task_row["id"]),
            log_max_bytes=config.load_settings().log_max_bytes,
        )
        log_path = config.run_log_path(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            active.log_file = log_path.open("a", encoding="utf-8", errors="replace")
        except OSError:
            active.log_file = None

        with self._active_lock:
            self._active[run_id] = active
        self.store.runs.mark_running(run_id)

        status = "failed"
        error = ""
        exit_code: int | None = None
        started = time.monotonic()

        def log(line: str) -> None:
            self._write_log(active, line)

        try:
            log("=" * 72)
            log(f"任务: {task_row.get('name', '')} (id={task_row['id']})  运行: #{run_id}")
            log("触发方式: rollback   开始时间: " + format_local(utcnow()))
            log("=" * 72)
            # 远程任务的回滚要在目标主机上换链与执行脚本。
            rollback_target = None
            rollback_layout = None
            if task_row.get("host_id"):
                host_row = self.store.hosts.get(int(task_row["host_id"]))
                if host_row is None:
                    raise DeployError(
                        f"任务指定的目标主机 #{task_row['host_id']} 不存在，无法回滚"
                    )
                rollback_layout = resolve_remote_layout(host_row, task_row)
                credential_row = None
                if host_row.get("credential_id"):
                    credential_row = self.store.credentials.get(int(host_row["credential_id"]))
                try:
                    rollback_target = build_target(
                        host_row, credential_row,
                        work_dir=config.TMP_DIR / f"task-{task_row['id']}-host",
                    )
                except TargetError as exc:
                    raise DeployError(str(exc)) from exc
                log(f"目标主机: {describe_host(host_row)}")
            ok, message = rollback_task(
                task_row,
                log=log,
                log_batch=lambda lines: self._write_log_batch(active, lines),
                timeout=timeout,
                kill_grace_seconds=kill_grace_seconds,
                selected_run=selected_run,
                handles=active.handles,
                check_cancelled=active.cancel_event.is_set,
                target=rollback_target,
                layout=rollback_layout,
            )
            if ok:
                status = "success"
                error = ""
            elif active.cancel_event.is_set():
                status = "cancelled"
                error = "回滚已被取消"
            else:
                status = "failed"
                error = message
            log(message)
        except Exception as exc:  # noqa: BLE001 - a rollback must never kill the worker
            status = "failed"
            error = f"未预期的错误: {exc.__class__.__name__}: {exc}"
            log(f"! {error}")
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if active.cancel_event.is_set() and status == "failed":
                status = "cancelled"
            log("-" * 72)
            log(f"结果: {STATUS_LABELS.get(status, status)}   耗时: {duration_ms / 1000:.1f}s")
            if error:
                log(f"错误: {error}")
            log("=" * 72)

            log_bytes = self._close_log(active, log_path)

            self.store.runs.mark_finished(
                run_id, status=status, exit_code=exit_code,
                error=error, duration_ms=duration_ms,
            )
            self.store.runs.set_log(run_id, str(log_path), log_bytes)
            self.store.tasks.record_run_finished(
                int(task_row["id"]), run_id=run_id, status=status, duration_ms=duration_ms
            )
            with self._active_lock:
                self._active.pop(run_id, None)

            if self.on_finished is not None:
                try:
                    self.on_finished(run_id, status)
                except Exception:  # noqa: BLE001 - notification must not break cleanup
                    pass

    # -- helpers -----------------------------------------------------------
    def _task_env(self, task: dict[str, Any]) -> dict[str, str]:
        raw = task.get("env_vars") or "{}"
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = raw
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    def _write_log(self, active: ActiveRun, line: str) -> None:
        cleaned = redact(str(line))
        self._write_log_batch(active, [cleaned])

    def _write_log_batch(self, active: ActiveRun, lines: list[str]) -> None:
        """批量写入：一次加锁入环形缓冲，一次写盘循环。

        高频输出（如 make -j、docker build）时按行调用会每行各付一次锁与
        脱敏的成本，合并后显著摊薄。"""
        if not lines:
            return
        cleaned = [redact(str(line)) for line in lines]
        active.append_tail_batch(cleaned)
        if active.log_file is None:
            return
        try:
            for line in cleaned:
                active.log_file.write(line + "\n")
            active.lines_since_flush += len(cleaned)
            # 周期 flush：默认块缓冲下崩溃可丢约 8KB；按行距 flush 把
            # 丢失窗口压到几十行，flush 只推到 OS 页缓存，成本可忽略。
            if active.lines_since_flush >= LOG_FLUSH_EVERY_LINES:
                active.log_file.flush()
                active.lines_since_flush = 0
            if active.log_file.tell() > active.log_max_bytes:
                # 触顶即关，保住头部；尾部由 _close_log 在运行结束时回填，
                # 排障要看的失败原因几乎总在结尾。
                active.log_file.write("! 日志超过大小上限，后续输出被截断\n")
                active.log_file.flush()
                active.log_file.close()
                active.log_file = None
                active.log_truncated = True
        except (OSError, ValueError):
            active.log_file = None

    def _close_log(self, active: ActiveRun, log_path) -> int:
        """运行结束收口日志文件，返回落盘字节数供 runs 记录。

        文件因触顶提前关闭时，磁盘上只有日志开头——而实时视图保的是结尾，
        结束后内存缓冲一释放，失败原因就再也找不回来了。这里用内存环形
        缓冲（最近 2000 行 / 1MB，与实时视图同一窗口）整体重写文件，并在
        头部标注前段已被截断。"""
        if active.log_file is None:
            if active.log_truncated:
                return self._rewrite_log_tail(active, log_path)
            return 0
        try:
            active.log_file.flush()
            size = active.log_file.tell()
            active.log_file.close()
            active.log_file = None
            if active.log_truncated:
                self._rewrite_log_tail(active, log_path)
                size = log_path.stat().st_size
            return size
        except OSError:
            return 0

    def _rewrite_log_tail(self, active: ActiveRun, log_path) -> int:
        try:
            lines = active.snapshot_tail(LOG_TAIL_LINES)
            # 重写同样要尊重大小上限：从窗口末尾往前取，装得下多少留多少
            # （结尾优先）。预算按 UTF-8 字节算——len() 数的是字符，中日文
            # 提示头一个就占 ~70 字节，按字符算会超出上限。
            header = "! 日志前段因超过大小上限被截断，以下为结尾部分\n"
            budget = max(0, active.log_max_bytes - len(header.encode("utf-8")))
            kept: list[str] = []
            used = 0
            for line in reversed(lines):
                cost = len(line.encode("utf-8")) + 1
                if used + cost > budget and kept:
                    break
                kept.append(line)
                used += cost
            kept.reverse()
            with log_path.open("w", encoding="utf-8", errors="replace") as handle:
                handle.write(header)
                for line in kept:
                    handle.write(line + "\n")
            return log_path.stat().st_size
        except OSError:
            return 0

    def _notify(
        self, task: dict[str, Any], run_id: int, status: str, duration_ms: int, error: str
    ) -> None:
        """POST a short JSON summary to the task's webhook, if configured."""
        url = (task.get("notify_webhook") or config.load_settings().notify_webhook or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return
        payload = {
            "task_id": task.get("id"),
            "task_name": task.get("name"),
            "run_id": run_id,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "duration_ms": duration_ms,
            "error": error,
            "finished_at": iso(utcnow()),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AutoDeploy/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except (urllib.error.URLError, OSError, ValueError):
            # A failed notification must never affect the run's outcome.
            pass

    # -- live log ----------------------------------------------------------
    def tail(self, run_id: int, *, limit: int = 200, offset: int = 0) -> list[str] | None:
        """In-memory tail for an active run, or None when it is not running."""
        with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            return None
        lines = active.snapshot_tail(LOG_TAIL_LINES)
        if offset:
            lines = lines[offset:]
        return lines[-limit:]


STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
}


def _token(task: dict[str, Any]) -> str:
    return str(task.get("git_token") or "")


def _safe_name(name: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "task")).strip("-")
    return cleaned[:48] or "task"


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"
