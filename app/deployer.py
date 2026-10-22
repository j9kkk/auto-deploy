"""Release staging, packaging and the individual deploy methods.

The pipeline is deliberately capistrano-shaped:

* every run gets a **new immutable release directory** (``releases/<seq>``),
* the source tree is *staged* into it (optionally filtered to the paths the task
  cares about, e.g. ``dist/``),
* a ``.tar.gz`` bundle of that release is written to ``artifacts/`` so it can be
  downloaded from the UI,
* only then is the ``current`` symlink swapped, using ``os.replace`` on a
  temporary link so the swap is atomic and a running service never sees a
  half-populated directory.

If anything fails before the swap, the previous release keeps serving traffic.
"""

from __future__ import annotations

import base64
import fnmatch
import glob
import os
import re
import shlex
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import config
from .executor import CommandResult, command_exists, run_command, shell_command
from .target import TargetError

DEPLOY_METHODS = (
    "artifact",       # build + package only
    "script",         # run the task's deploy script
    "release",        # stage release + atomic symlink swap
    "systemd",        # release + systemctl restart
    "docker",         # docker build (+ optional run/update command)
    "docker_compose", # docker compose up -d --build
    "compose_inline", # compose 内容直填：不拉仓库，直接 compose up
    "rsync",          # push the release to a remote target
)

METHOD_LABELS = {
    "artifact": "仅打包",
    "script": "自定义脚本",
    "release": "发布目录 + 软链切换",
    "systemd": "发布 + systemd 重启",
    "docker": "Docker 构建/启动",
    "docker_compose": "Docker Compose",
    "compose_inline": "Compose 内容直填",
    "rsync": "rsync 同步到远程",
}

# 不需要仓库的部署方式：这些任务没有源码可拉，流水线跳过拉代码、暂存与打包。
REPO_FREE_METHODS = frozenset({"compose_inline"})

# Files never staged into a release or a bundle.
_STAGE_EXCLUDES = (
    ".git", ".git/*", ".svn", ".hg",
    "__pycache__", "*/__pycache__", "*.pyc",
    "node_modules", "*/node_modules",
    ".venv", "*/.venv", "venv", "*/venv",
    ".autodeploy-cache", "*/.autodeploy-cache",
    "*.log",
)

_DEFAULT_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".autodeploy-cache",
}


class DeployError(Exception):
    """A deploy stage failed; the message is shown to the operator."""


@dataclass
class DeployContext:
    """Everything a deploy stage needs to know about the current run."""

    task: dict[str, Any]
    run_id: int
    workspace: Path
    release_dir: Path
    releases_root: Path
    current_link: Path
    artifact_path: Path
    commit: str = ""
    previous_commit: str = ""
    branch: str = ""
    env: dict[str, str] = field(default_factory=dict)
    log: Callable[[str], None] = lambda _message: None
    # 批量日志回调（可选）：构建输出突发时整批递交，一次加锁一次落盘。
    log_batch: Callable[[list[str]], None] | None = None
    check_cancelled: Callable[[], bool] = lambda: False
    kill_grace_seconds: int = 10
    # Live process handles, so a cancellation request can kill what is running.
    handles: list[Any] = field(default_factory=list)
    last_exit_code: int | None = None
    # 代理环境变量：注入到所有脚本，让构建过程也能联网。
    proxy: dict[str, str] = field(default_factory=dict)
    # 命令与文件的执行位置。None 表示本机（保持既有调用方与测试的构造方式）。
    target: Any = None
    # 远程目标时的字符串路径；本机目标下为空，避免与 Path 语义混淆。
    remote_workspace: str = ""
    remote_release_dir: str = ""
    remote_releases_root: str = ""
    remote_current_link: str = ""
    remote_artifact_path: str = ""

    def _exec_target(self) -> Any:
        """惰性取得执行目标：未显式注入时即本机。"""
        if self.target is None:
            from .target import LocalTarget

            self.target = LocalTarget()
        return self.target

    def exec(
        self,
        args: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        label: str | None = None,
    ) -> CommandResult:
        """Run a command with this context's logging and cancellation wiring."""
        resolved_env = env if env is not None else self.script_env()
        # 代理附加在最后：即使调用方传了自己的 env，也仍然带上代理设置。
        if self.proxy:
            resolved_env = {**resolved_env, **self.proxy}
        result = self._exec_target().run(
            args,
            cwd=cwd,
            env=resolved_env,
            timeout=_timeout_for(self, timeout),
            log=self.log,
            log_batch=self.log_batch,
            label=label,
            handle_out=self.handles,
            check_cancelled=self.check_cancelled,
            kill_grace_seconds=self.kill_grace_seconds,
        )
        self.last_exit_code = result.exit_code
        return result

    def exec_script(
        self,
        script: str,
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        label: str | None = None,
    ) -> CommandResult:
        """在本上下文的目标上执行一段 shell 脚本。

        远程目标的 shell 选择属于目标机（固定 bash）；本机沿用设置里的 shell。
        """
        resolved_env = env if env is not None else self.script_env()
        if self.proxy:
            resolved_env = {**resolved_env, **self.proxy}
        result = self._exec_target().run_script(
            script,
            cwd=cwd,
            env=resolved_env,
            timeout=_timeout_for(self, timeout),
            log=self.log,
            log_batch=self.log_batch,
            label=label,
            handle_out=self.handles,
            check_cancelled=self.check_cancelled,
            kill_grace_seconds=self.kill_grace_seconds,
        )
        self.last_exit_code = result.exit_code
        return result

    @property
    def is_remote(self) -> bool:
        return bool(getattr(self._exec_target(), "is_remote", False))

    @property
    def work_dir(self) -> Any:
        """工作目录：本机返回 Path，远程返回远端路径字符串。"""
        return self.remote_workspace if self.is_remote else self.workspace

    @property
    def work_source_dir(self) -> Any:
        """源码目录（含 repo_subdir）：本机 Path，远程字符串。"""
        if not self.is_remote:
            return self.source_root
        subdir = (self.task.get("repo_subdir") or "").strip().strip("/")
        if not subdir:
            return self.remote_workspace
        if ".." in Path(subdir).parts:
            raise DeployError(f"仓库子目录越界: {subdir}")
        return f"{self.remote_workspace.rstrip('/')}/{subdir}"

    @property
    def release_path(self) -> Any:
        return self.remote_release_dir if self.is_remote else self.release_dir

    @property
    def releases_root_path(self) -> Any:
        return self.remote_releases_root if self.is_remote else self.releases_root

    @property
    def current_link_path(self) -> Any:
        return self.remote_current_link if self.is_remote else self.current_link

    @property
    def artifact_path_value(self) -> Any:
        return self.remote_artifact_path if self.is_remote else self.artifact_path

    @property
    def source_root(self) -> Path:
        """The directory inside the checkout that the task builds from."""
        subdir = (self.task.get("repo_subdir") or "").strip().strip("/")
        if subdir:
            candidate = (self.workspace / subdir).resolve()
            # Refuse to escape the workspace via `..` or an absolute path.
            try:
                candidate.relative_to(self.workspace.resolve())
            except ValueError as exc:
                raise DeployError(f"仓库子目录越界: {subdir}") from exc
            return candidate
        return self.workspace

    @property
    def deploy_method(self) -> str:
        return (self.task.get("deploy_method") or "script").strip().lower()

    def script_env(self) -> dict[str, str]:
        """Environment variables exported to every task script.

        路径类变量的取值取决于执行目标：远程部署时它们必须是目标机上的路径，
        否则用户在 deploy 脚本里拿到的 ``AUTODEPLOY_RELEASE_DIR`` 会指向控制机
        上一个目标机上并不存在的位置。
        """
        base = dict(self.env)
        if self.is_remote:
            workspace = self.remote_workspace
            source_dir = self.work_source_dir
            release_dir = self.remote_release_dir
            current_link = self.remote_current_link
            releases_root = self.remote_releases_root
            artifact = self.remote_artifact_path
        else:
            workspace = str(self.workspace)
            source_dir = str(self.source_root)
            release_dir = str(self.release_dir)
            current_link = str(self.current_link)
            releases_root = str(self.releases_root)
            artifact = str(self.artifact_path)
        base.update(
            {
                "AUTODEPLOY_TASK_ID": str(self.task.get("id", "")),
                "AUTODEPLOY_TASK_NAME": str(self.task.get("name", "")),
                "AUTODEPLOY_RUN_ID": str(self.run_id),
                "AUTODEPLOY_DEPLOY_METHOD": self.deploy_method,
                "AUTODEPLOY_WORKSPACE": workspace,
                "AUTODEPLOY_SOURCE_DIR": source_dir,
                "AUTODEPLOY_RELEASE_DIR": release_dir,
                "AUTODEPLOY_CURRENT_LINK": current_link,
                "AUTODEPLOY_RELEASES_ROOT": releases_root,
                "AUTODEPLOY_ARTIFACT": artifact,
                "AUTODEPLOY_COMMIT": self.commit,
                "AUTODEPLOY_SHORT_COMMIT": self.commit[:8],
                "AUTODEPLOY_PREVIOUS_COMMIT": self.previous_commit,
                "AUTODEPLOY_BRANCH": self.branch,
                "AUTODEPLOY_TARGET_DIR": str(self.task.get("target_dir") or ""),
                "AUTODEPLOY_TIMEOUT": str(self.task.get("timeout_seconds") or 1800),
            }
        )
        return {k: (v if v is not None else "") for k, v in base.items()}


# ---------------------------------------------------------------------------
# Path / config helpers
# ---------------------------------------------------------------------------

def parse_path_list(raw: Any) -> list[str]:
    """Split the artifact-path field into individual patterns.

    Accepts newlines, commas and semicolons so the UI can use a plain textarea.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw)
        for separator in ("\r\n", "\n", ",", ";"):
            text = text.replace(separator, "\n")
        parts = text.split("\n")
    cleaned: list[str] = []
    for part in parts:
        item = part.strip().strip('"').strip("'")
        # Drop only a single literal "./" prefix. Using str.lstrip here would
        # also eat the dots in "../x", silently turning a traversal attempt into
        # a valid path instead of letting validation reject it.
        while item.startswith("./") and len(item) > 2:
            item = item[2:]
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def validate_relative_path(pattern: str, *, field_name: str) -> str | None:
    """Reject absolute paths and parent traversal in a user-supplied path."""
    text = (pattern or "").strip()
    if not text:
        return f"{field_name} 不能为空"
    if text.startswith("/") or text.startswith("~"):
        return f"{field_name} 必须是相对路径: {pattern}"
    normalized = text.replace("\\", "/")
    if normalized.startswith("../") or "/../" in normalized or normalized == "..":
        return f"{field_name} 不能包含 .. 上级引用: {pattern}"
    return None


def resolve_release_paths(task: dict[str, Any]) -> tuple[Path, Path]:
    """Return ``(releases_root, current_link)`` for a task.

    When the task sets ``target_dir`` the standard layout is used there, so
    ``<target_dir>/current`` can be pointed at by nginx or systemd.  Otherwise
    releases stay inside the service data directory.
    """
    target = (task.get("target_dir") or "").strip()
    if target:
        root = Path(os.path.expanduser(target)).resolve()
        return root / "releases", root / "current"
    base = config.releases_dir(int(task.get("id") or 0))
    return base / "releases", base / "current"


def next_release_dir(releases_root: Path, commit: str) -> Path:
    """A monotonically numbered release directory, unique per run."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    short = (commit or "nogit")[:8]
    releases_root.mkdir(parents=True, exist_ok=True)
    candidate = releases_root / f"{stamp}-{short}"
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = releases_root / f"{stamp}-{short}-{suffix}"
    return candidate


def current_release(releases_root: Path, current_link: Path) -> Path | None:
    """Resolve which release ``current`` points at, if any."""
    for link in (current_link, releases_root / "current"):
        if link.is_symlink():
            try:
                resolved = link.resolve()
            except OSError:
                continue
            if resolved.exists():
                return resolved
    return None


# 「Compose 内容直填」在发布根下的固定子目录名。相对路径挂载（``./data``）
# 以 compose 文件的所在目录为基准，因此文件必须写在这个跨运行不变的目录里，
# 而不是每次新建的 releases/<时间戳> 里——后者会让数据目录每次部署都变成
# 「空的新目录」，看起来就是数据被清空。
COMPOSE_INLINE_DIR = "compose-inline"
COMPOSE_INLINE_FILE = "docker-compose.yml"
# 用户内容里出现这些写法时，日志里会提示它们相对于这个固定目录解析。
_COMPOSE_REPO_FREE_NOTE = (
    "相对路径（如 ./data 或 build: .）以 {dir} 为基准解析；"
    "该目录不随发布版本变化，升级不会清空挂载的数据"
)


def compose_inline_dir(releases_root: Path) -> Path:
    """「Compose 内容直填」写入 compose 文件的固定目录。"""
    return releases_root / COMPOSE_INLINE_DIR


def compose_inline_path(releases_root: Path) -> Path:
    return compose_inline_dir(releases_root) / COMPOSE_INLINE_FILE


def previous_release(releases_root: Path, current_link: Path) -> Path | None:
    """The newest release older than the one ``current`` points at."""
    active = current_release(releases_root, current_link)
    candidates = list_releases(releases_root)
    if not candidates:
        return None
    if active is None:
        return candidates[0]
    for candidate in candidates:
        if candidate != active:
            return candidate
    return None


def list_releases(releases_root: Path) -> list[Path]:
    """Existing release directories, newest first."""
    if not releases_root.exists():
        return []
    entries = [
        entry
        for entry in releases_root.iterdir()
        if entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith(".")
        # 「Compose 内容直填」的固定工作目录不是发布版本：它跨运行复用，
        # 若被当作历史发布清理，挂载在其中的数据会被一并删除。
        and entry.name != COMPOSE_INLINE_DIR
    ]
    # The timestamp prefix sorts chronologically; mtime is the tiebreaker.
    entries.sort(key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _matches_any(rel_path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if rel_path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def stage_release(
    ctx: DeployContext,
    *,
    patterns: Sequence[str],
    full_copy: bool = False,
) -> int:
    """Copy the wanted parts of the checkout into the release directory.

    Returns the number of files staged.  When ``patterns`` is empty the whole
    source tree is copied, minus build artefacts and version-control metadata.
    """
    source = ctx.source_root
    if not source.exists():
        raise DeployError(f"源码目录不存在: {source}")

    release = ctx.release_dir
    if release.exists():
        shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True, exist_ok=True)

    staged = 0
    if full_copy or not patterns:
        if source.is_dir():
            for entry in sorted(source.iterdir()):
                if entry.name in _DEFAULT_IGNORE_DIRS:
                    continue
                target = release / entry.name
                if entry.is_dir() and not entry.is_symlink():
                    shutil.copytree(
                        entry, target, symlinks=True,
                        ignore=shutil.ignore_patterns(*_DEFAULT_IGNORE_DIRS, "*.log", "*.pyc"),
                        dirs_exist_ok=True,
                    )
                else:
                    shutil.copy2(entry, target, follow_symlinks=False)
                staged += 1
        else:
            shutil.copy2(source, release / source.name)
            staged += 1
        ctx.log(f"已暂存完整源码树 ({staged} 个顶层条目)")
        return staged

    missing: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(str(source / pattern), recursive=True))
        if not matches:
            missing.append(pattern)
            continue
        for match in matches:
            src = Path(match)
            rel = src.relative_to(source)
            dst = release / rel
            if src.is_dir():
                shutil.copytree(
                    src, dst, symlinks=True,
                    ignore=shutil.ignore_patterns(*_DEFAULT_IGNORE_DIRS, "*.log", "*.pyc"),
                    dirs_exist_ok=True,
                )
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst, follow_symlinks=False)
            staged += 1

    if missing and staged == 0:
        raise DeployError(
            "打包路径未匹配到任何文件: " + ", ".join(missing)
        )
    if missing:
        ctx.log("! 以下打包路径未匹配到文件: " + ", ".join(missing))
    ctx.log(f"已暂存 {staged} 个打包路径匹配项")
    return staged


def copy_release_to(source: Path, destination: Path) -> int:
    """Materialise a release into a foreign filesystem location (e.g. rsync target)."""
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    return 1


def create_bundle(release_dir: Path, artifact_path: Path) -> int:
    """Write a ``.tar.gz`` of the release and return its size in bytes."""
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = artifact_path.with_suffix(artifact_path.suffix + ".part")
    tmp_path.unlink(missing_ok=True)
    try:
        # `dereference=False` keeps symlinks as links, matching the staged tree.
        with tarfile.open(tmp_path, "w:gz") as archive:
            archive.add(str(release_dir), arcname=release_dir.name, recursive=True)
        os.replace(tmp_path, artifact_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return artifact_path.stat().st_size


# ---------------------------------------------------------------------------
# Symlink swap
# ---------------------------------------------------------------------------

def swap_symlink(current_link: Path, release_dir: Path) -> None:
    """Atomically point ``current_link`` at ``release_dir``.

    The new link is created under a temporary name and moved into place with
    ``os.replace``, which is atomic on POSIX.  Readers therefore always observe
    either the old or the new target, never a missing link.
    """
    current_link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = current_link.parent / f".{current_link.name}.new-{os.getpid()}"
    tmp_link.unlink(missing_ok=True)
    os.symlink(str(release_dir), str(tmp_link))
    os.replace(str(tmp_link), str(current_link))


def prune_releases(
    releases_root: Path,
    current_link: Path,
    *,
    keep: int,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Delete old releases, never removing the active one."""
    if keep is None or keep <= 0:
        return []
    releases = list_releases(releases_root)
    active = current_release(releases_root, current_link)
    if len(releases) <= keep:
        return []

    keepers = set(releases[:keep])
    if active is not None:
        keepers.add(active)

    removed: list[str] = []
    for release in releases:
        if release in keepers:
            continue
        shutil.rmtree(release, ignore_errors=True)
        removed.append(release.name)
        if log:
            log(f"已清理历史发布: {release.name}")
    return removed


# ---------------------------------------------------------------------------
# Deploy methods
# ---------------------------------------------------------------------------

def _timeout_for(ctx: DeployContext, override: int | None = None) -> int:
    if override and override > 0:
        return override
    try:
        value = int(ctx.task.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 1800


def run_script_stage(
    ctx: DeployContext,
    script: str,
    *,
    stage: str,
    required: bool = True,
    timeout: int | None = None,
) -> CommandResult:
    """Run a user script with the deploy environment exported."""
    body = (script or "").strip()
    if not body:
        if required:
            raise DeployError(f"{stage} 脚本为空")
        return CommandResult(command="", exit_code=0, output="", duration_ms=0)

    env = ctx.script_env()
    env["AUTODEPLOY_STAGE"] = stage
    # Run from the source dir so relative paths in a script behave naturally.
    # 远程目标没有本地 Path 概念，直接使用目标机上的源码目录字符串。
    if ctx.is_remote:
        cwd: Any = ctx.work_source_dir
    else:
        cwd = ctx.source_root if ctx.source_root.exists() else ctx.workspace
    ctx.log(f"--- 阶段: {stage} ---")
    result = ctx.exec_script(
        body,
        cwd=cwd,
        env={**env, **ctx.proxy},
        timeout=_timeout_for(ctx, timeout),
        label=stage,
    )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"{stage} 阶段失败: {reason}")
    return result


def deploy_artifact(ctx: DeployContext) -> None:
    """Build output only: nothing is published, the bundle is the deliverable."""
    ctx.log("部署方式为「仅打包」：跳过发布步骤，产物已生成。")


def deploy_script(ctx: DeployContext) -> None:
    script = ctx.task.get("deploy_script") or ""
    if not script.strip():
        raise DeployError("部署方式为自定义脚本，但未填写部署脚本")
    run_script_stage(ctx, script, stage="deploy")


def deploy_release(ctx: DeployContext) -> None:
    """Stage the release and swap the symlink; no service action."""
    target = ctx._exec_target()
    if not target.exists(ctx.release_path):
        raise DeployError("发布目录不存在，暂存阶段可能失败")
    target.swap_symlink(ctx.current_link_path, ctx.release_path)
    ctx.log(f"已将 current 指向 {Path(str(ctx.release_path)).name}")
    script = ctx.task.get("deploy_script") or ""
    if script.strip():
        run_script_stage(ctx, script, stage="deploy")


def deploy_systemd(ctx: DeployContext) -> None:
    """Swap the symlink then restart and verify the configured unit."""
    target = ctx._exec_target()
    service = (ctx.task.get("service_name") or "").strip()
    if not service:
        raise DeployError("部署方式为 systemd，但未填写服务名称")
    if not target.which("systemctl"):
        raise DeployError("未找到 systemctl，无法使用 systemd 部署方式")
    target.swap_symlink(ctx.current_link_path, ctx.release_path)
    ctx.log(f"已将 current 指向 {Path(str(ctx.release_path)).name}")

    script = ctx.task.get("deploy_script") or ""
    if script.strip():
        run_script_stage(ctx, script, stage="deploy")

    ctx.log(f"--- 阶段: systemd 重启 ({service}) ---")
    result = ctx.exec(
        ["systemctl", "restart", service],
        timeout=min(300, _timeout_for(ctx)),
        label="systemd",
    )
    if not result.ok:
        raise DeployError(f"systemctl restart {service} 失败: {result.error}")

    # `is-active` failing means the unit restarted and immediately died, which
    # is exactly the case an operator needs to hear about.
    status = target.run(
        ["systemctl", "is-active", service],
        timeout=30,
        log=None,
    )
    state = status.output.strip().splitlines()[-1] if status.output.strip() else ""
    if state and state != "active":
        raise DeployError(f"服务 {service} 重启后状态为 {state}，请检查日志")
    ctx.log(f"服务 {service} 状态: active")


def deploy_docker(ctx: DeployContext) -> None:
    """Build the image, optionally refresh a container, and wait for health."""
    target = ctx._exec_target()
    if not target.which("docker"):
        raise DeployError("未找到 docker 命令，无法使用 Docker 部署方式")
    image = (ctx.task.get("docker_image") or "").strip()
    if not image:
        raise DeployError("部署方式为 Docker，但未填写镜像名称")

    release_dir = ctx.release_path
    dockerfile = f"{str(release_dir).rstrip('/')}/Dockerfile"
    if not target.exists(dockerfile):
        # Fall back to the checkout root for repos whose Dockerfile sits beside
        # sources that the artifact filter excluded.
        workspace_dockerfile = f"{str(ctx.work_dir).rstrip('/')}/Dockerfile"
        if target.exists(workspace_dockerfile):
            dockerfile = workspace_dockerfile
        else:
            raise DeployError("未在发布目录中找到 Dockerfile")

    build_context = str(Path(str(dockerfile)).parent)
    ctx.log(f"--- 阶段: docker build ({image}) ---")
    result = ctx.exec(
        ["docker", "build", "-t", image, "-f", str(dockerfile), build_context],
        timeout=_timeout_for(ctx),
        label="docker",
    )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"docker build 失败: {reason}")

    command = (ctx.task.get("docker_command") or "").strip()
    if command:
        run_script_stage(ctx, command, stage="docker-run", timeout=_timeout_for(ctx))
    else:
        ctx.log("未配置容器启动命令，仅完成镜像构建。")


def _conflicting_stale_containers(
    output: str, project_name: str, *, target: Any = None
) -> list[str]:
    """从 compose 报错中解析容器名冲突，且只认旧 autodeploy 项目的容器。

    安全边界：绝不碰任何不带 autodeploy 项目标签的容器——那可能是用户
    手工跑的业务容器。
    """
    import re as _re

    conflicts = _re.findall(r'The container name "/([^"]+)" is already in use', output or "")
    if not conflicts:
        return []
    runner = target.run if target is not None else run_command
    listing = runner(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Labels}}"],
        timeout=30,
    )
    if not listing.ok:
        return []
    stale: list[str] = []
    # 旧命名：项目名 = 发布目录名 = <日期>-<时间>-<短commit>[_default]。
    # 现行命名：autodeploy-<任务slug>；历史命名：autodeploy-<任务slug>-<id>。
    # 两种 autodeploy 形态都要识别（改名/升级前的存量容器靠它兜底清理）。
    legacy_re = _re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6,8}(_\w+)?$")
    new_re = _re.compile(r"^autodeploy-[a-z0-9-]+(-[0-9]+)?$")
    for line in listing.output.splitlines():
        name, _, label = line.partition("\t")
        labels = label or ""
        # compose 给每个容器打 com.docker.compose.project 标签；没有标签的
        # 容器（手工 docker run）一律不碰。
        if "com.docker.compose.project" not in labels:
            continue
        project = labels.split("com.docker.compose.project=")[-1].split(",")[0].strip()
        if project == project_name:
            continue  # 属于本项目（compose 自己会处理）
        if legacy_re.match(project) or new_re.match(project):
            stale.append(name.strip())
    # 只移除确实冲突的那几个名字。
    return [name for name in stale if name in conflicts]


def _remove_stale_containers(ctx: DeployContext, names: list[str]) -> bool:
    ok_all = True
    for name in names:
        result = ctx.exec(["docker", "rm", "-f", name], label="compose-cleanup")
        if not result.ok:
            ctx.log(f"! 无法移除遗留容器 {name}: {result.error}")
            ok_all = False
    return ok_all


def _safe_task_slug(name: str) -> str:
    """任务名 → compose 项目名安全段：小写字母数字与短横线。"""
    import re as _re
    import unicodedata as _unicodedata

    # 中文等非 ASCII 转拼音不可取（无依赖），转为 ASCII 音译太复杂；
    # 直接把非 [a-z0-9-] 全替换为短横线并折叠，保证 compose 项目名合法。
    text = _unicodedata.normalize("NFKD", str(name or ""))
    slug = _re.sub(r"[^a-zA-Z0-9-]+", "-", text).strip("-").lower()
    slug = _re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    return slug or "task"


def compose_project_name(task: dict[str, Any]) -> str:
    """该任务的 compose 项目名。

    任务名全局唯一（大小写不敏感校验），项目名不带 id 也唯一。
    改名会使项目名跟着变：改名被禁止在运行中，旧项目名的残留容器由
    _conflicting_stale_containers 的历史形态识别兜底，删除任务另有按
    compose 标签的回退清理，两者都不依赖项目名精确匹配。
    """
    return f"autodeploy-{_safe_task_slug(task.get('name') or '')}"


def deploy_docker_compose(ctx: DeployContext) -> None:
    """Bring up the compose project from the release directory."""
    target = ctx._exec_target()
    if not target.which("docker"):
        raise DeployError("未找到 docker 命令，无法使用 Docker Compose 部署方式")
    compose_file = (ctx.task.get("docker_compose_file") or "docker-compose.yml").strip()
    if validate_relative_path(compose_file, field_name="compose 文件"):
        raise DeployError(f"compose 文件必须是相对路径: {compose_file}")

    cwd = ctx.release_path
    candidate = f"{str(cwd).rstrip('/')}/{compose_file}"
    if not target.exists(candidate):
        raise DeployError(f"发布目录中不存在 compose 文件: {compose_file}")

    env = ctx.script_env()
    ctx.log(f"--- 阶段: docker compose up ({compose_file}) ---")
    # `docker compose` (v2) is preferred; fall back to `docker-compose` (v1).
    base = ["docker", "compose"] if _supports_compose_v2(target) else ["docker-compose"]

    # 固定 compose 项目名（按任务）。
    # 默认项目名取自目录名，而发布目录每次运行都不同（<时间戳>-<commit>），
    # 这会让每次部署都新建一个 compose 项目与一套网络——数百次运行后耗尽
    # Docker 默认地址池，报 "all predefined address pools have been fully
    # subnetted"。固定项目名让后续部署原地复用同一组网络并替换容器。
    project_name = compose_project_name(ctx.task)
    ctx.log(f"compose 项目名: {project_name}")

    result = ctx.exec(
        [*base, "-f", compose_file, "-p", project_name, "up", "-d", "--build", "--remove-orphans"],
        cwd=cwd,
        env=env,
        timeout=_timeout_for(ctx),
        label="compose",
    )
    if not result.ok and not result.cancelled:
        # 从旧命名时代（项目名=发布目录）迁移过来时，旧项目的容器还占着
        # 服务的容器名；自动识别并清理同项目遗留容器后重试一次。
        stale = _conflicting_stale_containers(result.output, project_name, target=target)
        if stale:
            ctx.log(f"! 检测到旧部署遗留容器占用容器名: {', '.join(stale)}")
            if _remove_stale_containers(ctx, stale):
                ctx.log("已清理遗留容器，重试部署…")
                result = ctx.exec(
                    [*base, "-f", compose_file, "-p", project_name,
                     "up", "-d", "--build", "--remove-orphans"],
                    cwd=cwd,
                    env=env,
                    timeout=_timeout_for(ctx),
                    label="compose",
                )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"docker compose 启动失败: {reason}")


def _supports_compose_v2(target: Any = None) -> bool:
    runner = target.run if target is not None else run_command
    probe = runner(["docker", "compose", "version"], timeout=20, log=None)
    return probe.ok


def _write_compose_file(ctx: DeployContext, path: Any, content: str) -> None:
    """把 compose 正文写到目标位置。

    本机直接写文件；远程目标没有本地文件系统，改用 base64 中转到远端再解码。
    先编码再传输，避免 YAML 里的引号、``$`` 与反斜杠被 shell 二次解释——
    用户写的 compose 文件不该因为 shell 规则变形。
    """
    target = ctx._exec_target()
    if not ctx.is_remote:
        directory = Path(str(path)).parent
        directory.mkdir(parents=True, exist_ok=True)
        Path(str(path)).write_text(content, encoding="utf-8")
        return

    target.make_dirs(str(Path(str(path)).parent))
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    # 用 heredoc 一次性投递整段 base64：分块 printf 会在超大文件上拼出长脚本，
    # 而 heredoc 的内容按行传给 bash，不受命令行长度限制。
    quoted = _quote(path)
    script = "\n".join([
        f"base64 -d > {quoted} <<'AUTODEPLOY_COMPOSE_EOF'",
        *[encoded[i:i + 76] for i in range(0, len(encoded), 76)],
        "AUTODEPLOY_COMPOSE_EOF",
    ])
    result = target.run_script(
        script,
        timeout=_timeout_for(ctx),
        log=ctx.log,
        log_batch=ctx.log_batch,
        label="compose-write",
        handle_out=ctx.handles,
        check_cancelled=ctx.check_cancelled,
        kill_grace_seconds=ctx.kill_grace_seconds,
    )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"写入 compose 文件失败: {reason}")


def _quote(value: Any) -> str:
    return shlex.quote(str(value))


def deploy_compose_inline(ctx: DeployContext) -> None:
    """用任务内直填的 compose 内容启动服务，全程不碰仓库。

    与 ``docker_compose`` 的差别：
    * 内容来自任务字段，不是发布目录里的文件，因此不拉代码、不暂存、不打包；
    * compose 文件写在发布根下跨运行固定的 ``compose-inline/`` 目录里，
      让 ``./data`` 这类相对挂载有稳定基准（见 ``COMPOSE_INLINE_DIR``）；
    * 先 ``pull`` 再 ``up``：该方式的「发布」语义就是换镜像，不显式拉取的话
      本地同名 tag 会一直复用旧镜像，改内容也不会生效。
    """
    target = ctx._exec_target()
    if not target.which("docker"):
        raise DeployError("未找到 docker 命令，无法使用 Compose 内容直填方式")
    content = str(ctx.task.get("compose_content") or "")
    if not content.strip():
        raise DeployError("部署方式为 Compose 内容直填，但 compose 内容为空")

    releases_root = ctx.releases_root_path
    work_dir = f"{str(releases_root).rstrip('/')}/{COMPOSE_INLINE_DIR}"
    compose_path = f"{work_dir}/{COMPOSE_INLINE_FILE}"

    ctx.log(f"--- 阶段: 写入 compose 文件 ({compose_path}) ---")
    _write_compose_file(ctx, compose_path, content)
    ctx.log(_COMPOSE_REPO_FREE_NOTE.format(dir=work_dir))

    env = ctx.script_env()
    base = ["docker", "compose"] if _supports_compose_v2(target) else ["docker-compose"]
    project_name = compose_project_name(ctx.task)

    # pull 放在 up 之前：该方式的「发布」就是换镜像，不显式拉取会让本地同名
    # tag 一直复用旧镜像。`--ignore-buildable` 跳过只有 build 段的服
    # （compose 会因这类服务没有可拉取的镜像而整体报错）；旧版 compose 不认识
    # 该参数，失败后再退回不带它的写法。
    ctx.log("--- 阶段: 拉取镜像 ---")
    pull_args = [*base, "-f", compose_path, "-p", project_name,
                 "--project-directory", work_dir, "pull"]
    pull = ctx.exec([*pull_args, "--ignore-buildable"], cwd=work_dir, env=env,
                    timeout=_timeout_for(ctx), label="compose-pull")
    if not pull.ok and not pull.cancelled:
        lowered = (pull.output or "") + (pull.error or "")
        if "unknown flag" in lowered or "unknown shorthand" in lowered:
            ctx.log("当前 compose 版本不支持 --ignore-buildable，改用普通 pull")
            pull = ctx.exec(pull_args, cwd=work_dir, env=env,
                            timeout=_timeout_for(ctx), label="compose-pull")
    if not pull.ok:
        if pull.cancelled:
            raise DeployError("任务已被取消")
        # 拉取失败不直接中止：本地可能已有可用镜像（离线或私有库未登录），
        # 继续 up 并让真正的错误在启动阶段暴露。
        ctx.log(f"! 镜像拉取未成功（{pull.error or '退出码 ' + str(pull.exit_code)}），"
                "将尝试用本地已有镜像启动")

    ctx.log(f"--- 阶段: docker compose up ({COMPOSE_INLINE_FILE}) ---")
    result = ctx.exec(
        [*base, "-f", compose_path, "-p", project_name, "--project-directory", work_dir,
         "up", "-d", "--remove-orphans"],
        cwd=work_dir,
        env=env,
        timeout=_timeout_for(ctx),
        label="compose",
    )
    if not result.ok and not result.cancelled:
        # 与 docker_compose 一致：旧命名的遗留容器占着容器名时清理后重试一次。
        stale = _conflicting_stale_containers(result.output, project_name, target=target)
        if stale:
            ctx.log(f"! 检测到旧部署遗留容器占用容器名: {', '.join(stale)}")
            if _remove_stale_containers(ctx, stale):
                ctx.log("已清理遗留容器，重试部署…")
                result = ctx.exec(
                    [*base, "-f", compose_path, "-p", project_name,
                     "--project-directory", work_dir, "up", "-d", "--remove-orphans"],
                    cwd=work_dir,
                    env=env,
                    timeout=_timeout_for(ctx),
                    label="compose",
                )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"docker compose 启动失败: {reason}")


def deploy_rsync(ctx: DeployContext) -> None:
    """Push the release to a remote (or local) target with rsync."""
    executor_target = ctx._exec_target()
    if not executor_target.which("rsync"):
        raise DeployError("未找到 rsync 命令")
    rsync_target = (ctx.task.get("rsync_target") or "").strip()
    if not rsync_target:
        raise DeployError("部署方式为 rsync，但未填写目标地址")
    if rsync_target.startswith("-"):
        raise DeployError("rsync 目标不能以 - 开头")

    options_raw = (ctx.task.get("rsync_options") or "-az --delete").strip()
    options = options_raw.split() if options_raw else []
    # `/./` marks where the source subtree starts, so `-R`-style relative
    # layouts land correctly on the far side.
    source = str(ctx.release_path) + "/./"

    ctx.log(f"--- 阶段: rsync → {rsync_target} ---")
    result = ctx.exec(
        ["rsync", *options, source, rsync_target],
        timeout=_timeout_for(ctx),
        label="rsync",
    )
    if not result.ok:
        reason = "已取消" if result.cancelled else (result.error or f"退出码 {result.exit_code}")
        raise DeployError(f"rsync 同步失败: {reason}")

    # Refresh the `current` link so the UI reflects what was published.
    if ctx.task.get("target_dir"):
        executor_target.swap_symlink(ctx.current_link_path, ctx.release_path)


_DEPLOY_DISPATCH: dict[str, Callable[[DeployContext], None]] = {
    "artifact": deploy_artifact,
    "script": deploy_script,
    "release": deploy_release,
    "systemd": deploy_systemd,
    "docker": deploy_docker,
    "docker_compose": deploy_docker_compose,
    "compose_inline": deploy_compose_inline,
    "rsync": deploy_rsync,
}


def run_deploy(ctx: DeployContext) -> None:
    """Execute the task's configured deploy method."""
    method = ctx.deploy_method
    handler = _DEPLOY_DISPATCH.get(method)
    if handler is None:
        raise DeployError(f"不支持的部署方式: {method!r}")
    ctx.log(f"--- 阶段: 部署 ({METHOD_LABELS.get(method, method)}) ---")
    handler(ctx)


def _rollback_target(
    task: dict[str, Any],
    selected_run: dict[str, Any] | None,
) -> tuple[Path, Path, Path | None, Path]:
    """Resolve only a stored run's direct release directory, never a client path."""
    if selected_run is not None:
        if not selected_run:
            raise DeployError("指定的运行记录不存在")
        if selected_run.get("task_id") != task.get("id"):
            raise DeployError("指定的运行记录不属于该任务")
        if selected_run.get("status") != "success":
            raise DeployError("只能回滚到部署成功的运行版本")
        if not selected_run.get("release_dir"):
            raise DeployError("该运行没有发布目录，无法回滚")
        recorded_method = str(selected_run.get("deploy_method") or "").strip().lower()
        current_method = str(task.get("deploy_method") or "script").strip().lower()
        if recorded_method and recorded_method != current_method:
            raise DeployError("任务部署方式已改变，不能使用当前配置回滚该运行版本")

    releases_root, current_link = resolve_release_paths(task)
    # Resolve trusted ancestors (e.g. macOS /tmp -> /private/tmp), not the
    # release-root leaf: replacing that leaf must still be rejected.
    if releases_root.is_symlink():
        raise DeployError("发布目录根路径存在软链或越界，无法回滚")
    if not releases_root.is_dir():
        raise DeployError("发布目录不存在或已删除，无法回滚")
    root = releases_root.resolve()
    if not (task.get("target_dir") or "").strip():
        expected_parent = config.RELEASES_DIR.resolve() / f"task-{int(task.get('id') or 0)}"
        if root.parent != expected_parent:
            raise DeployError("任务发布目录越界，无法回滚")
    current_link = current_link.parent.resolve() / current_link.name
    active = None
    for link in (current_link, root / "current"):
        if link.is_symlink():
            resolved = link.resolve()
            if resolved.parent != root or (resolved.exists() and not resolved.is_dir()):
                raise DeployError("当前版本软链越界或未指向发布目录，无法回滚")
            # A missing in-root current was ignored by current_release too.
            if active is None and resolved.is_dir():
                active = resolved
        elif link.exists():
            raise DeployError("current 不是软链，无法安全回滚")

    if selected_run is None:
        # Canonical root avoids comparing alias paths against a resolved current.
        target = previous_release(root, current_link)
        if target is None:
            raise DeployError("没有可回滚的历史版本")
    else:
        raw_path = selected_run["release_dir"]
        if not isinstance(raw_path, str) or "\\" in raw_path or "\x00" in raw_path:
            raise DeployError("运行记录中的发布目录路径无效")
        target = Path(raw_path)
    if not target.is_absolute() or ".." in target.parts or target.parent.resolve() != root:
        raise DeployError("运行记录中的发布目录越界或不属于该任务")
    if selected_run is not None and not re.fullmatch(
        r"\d{8}-\d{6}-(?:[0-9a-fA-F]{1,8}|nogit)(?:-[0-9]+)?", target.name,
    ):
        raise DeployError("运行记录未指向有效的版本发布目录")
    if target.is_symlink():
        raise DeployError("发布目录不能是软链或指向其他位置")
    target = target.resolve()
    if target.parent != root:
        raise DeployError("发布目录不能是软链或指向其他位置")
    if not target.is_dir():
        raise DeployError("该版本的发布目录不存在或已删除")
    if target == active:
        raise DeployError("指定版本已经是当前版本，无需回滚")
    return root, current_link, active, target


def _remote_rollback_target(
    task: dict[str, Any],
    selected_run: dict[str, Any] | None,
    target: Any,
    layout: Any,
) -> tuple[str, str, str | None, str]:
    """远程回滚目标校验：只接受确实位于发布根下的历史发布目录。

    与本地 ``_rollback_target`` 同等严格，只是用目标抽象代替本地 Path：
    发布根必须是目录而非软链、目标必须直接位于根下、名字必须符合发布命名、
    不能是软链，也不能等于当前版本。
    """
    if selected_run is not None:
        if not selected_run:
            raise DeployError("指定的运行记录不存在")
        if selected_run.get("task_id") != task.get("id"):
            raise DeployError("指定的运行记录不属于该任务")
        if selected_run.get("status") != "success":
            raise DeployError("只能回滚到部署成功的运行版本")
        if not selected_run.get("release_dir"):
            raise DeployError("该运行没有发布目录，无法回滚")
        recorded_method = str(selected_run.get("deploy_method") or "").strip().lower()
        current_method = str(task.get("deploy_method") or "script").strip().lower()
        if recorded_method and recorded_method != current_method:
            raise DeployError("任务部署方式已改变，不能使用当前配置回滚该运行版本")

    root = str(layout.releases_root).rstrip("/")
    current_link = str(layout.current_link)
    if target.is_symlink(root):
        raise DeployError("目标主机上的发布目录根路径是软链，无法安全回滚")
    if not target.is_dir(root):
        raise DeployError("目标主机上不存在发布目录，无法回滚")

    active = None
    link = target.readlink(current_link)
    if link:
        resolved = link.rstrip("/")
        if not resolved.startswith(root + "/") and resolved != root:
            raise DeployError("目标主机上的 current 指向发布目录之外，无法回滚")
        if resolved != root and target.is_dir(resolved):
            active = resolved

    if selected_run is None:
        names = target.list_dirs(root)
        candidates = [n for n in names if f"{root}/{n}" != active]
        if not candidates:
            raise DeployError("目标主机上没有可回滚的历史版本")
        chosen = f"{root}/{candidates[0]}"
    else:
        raw_path = str(selected_run.get("release_dir") or "")
        if "\\" in raw_path or "\x00" in raw_path or ".." in Path(raw_path).parts:
            raise DeployError("运行记录中的发布目录路径无效")
        if not raw_path.startswith(root + "/"):
            raise DeployError("运行记录中的发布目录不在该任务的发布根下，无法回滚")
        if not re.fullmatch(
            r"\d{8}-\d{6}-(?:[0-9a-fA-F]{1,8}|nogit)(?:-[0-9]+)?",
            raw_path.rstrip("/").rsplit("/", 1)[-1],
        ):
            raise DeployError("运行记录未指向有效的版本发布目录")
        chosen = raw_path.rstrip("/")

    if target.is_symlink(chosen):
        raise DeployError("发布目录不能是软链，无法回滚")
    if not target.is_dir(chosen):
        raise DeployError("该版本的发布目录在目标主机上不存在或已删除")
    if active and chosen == active:
        raise DeployError("指定版本已经是当前版本，无需回滚")
    return root, current_link, active, chosen


def rollback_task(
    task: dict[str, Any],
    *,
    log: Callable[[str], None],
    log_batch: Callable[[list[str]], None] | None = None,
    timeout: int = 300,
    kill_grace_seconds: int = 10,
    selected_run: dict[str, Any] | None = None,
    handles: list[Any] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    target: Any = None,
    layout: Any = None,
) -> tuple[bool, str]:
    """Switch to a validated stored run, or retain the legacy previous selection.

    The caller holds the deployment admission lock throughout this operation.
    Scripts and systemd retain the original rollback semantics; other methods
    do not automatically redeploy containers or synchronize remote machines.

    传入 ``target``（远程执行目标）与 ``layout`` 时走远程回滚：换链与脚本都在
    目标主机上完成，本地路径逻辑完全不变。
    """
    # 「Compose 内容直填」没有版本化的发布目录：容器状态由任务里那份 compose
    # 内容决定，历史运行也没有保存当时的正文，切换软链毫无意义。明确拒绝，
    # 避免用户以为回滚成功而实际服务没变。
    if str(task.get("deploy_method") or "").strip().lower() == "compose_inline":
        return False, (
            "Compose 内容直填方式没有历史版本可供回滚：请把 compose 内容改回旧内容后重新部署"
        )
    if target is not None and getattr(target, "is_remote", False) and layout is not None:
        return _remote_rollback(
            task, selected_run=selected_run, target=target, layout=layout,
            log=log, log_batch=log_batch, timeout=timeout,
            kill_grace_seconds=kill_grace_seconds, handles=handles,
            check_cancelled=check_cancelled,
        )

    try:
        releases_root, current_link, active, target_path = _rollback_target(task, selected_run)
    except DeployError as exc:
        return False, str(exc)
    except (OSError, RuntimeError, ValueError):
        return False, "发布目录或当前版本路径无效、已删除或无法访问"

    if selected_run is not None:
        log("回滚使用任务当前配置的回滚脚本和服务名称；历史运行未保存这些配置的快照。")
    try:
        swap_symlink(current_link, target_path)
    except OSError as exc:
        return False, f"切换软链失败: {exc}"
    log(f"已回滚 current → {target_path.name}"
        + (f"（原 {active.name}）" if active else ""))

    script = (task.get("rollback_script") or "").strip()
    ctx_env = {
        "AUTODEPLOY_TASK_ID": str(task.get("id", "")),
        "AUTODEPLOY_TASK_NAME": str(task.get("name", "")),
        "AUTODEPLOY_RUN_ID": str(selected_run.get("id", "")) if selected_run else "",
        "AUTODEPLOY_COMMIT": str(selected_run.get("commit_after") or "") if selected_run else "",
        "AUTODEPLOY_RELEASE_DIR": str(target_path),
        "AUTODEPLOY_CURRENT_LINK": str(current_link),
        "AUTODEPLOY_RELEASES_ROOT": str(releases_root),
        "AUTODEPLOY_TARGET_DIR": str(task.get("target_dir") or ""),
        "AUTODEPLOY_STAGE": "rollback",
    }
    if script:
        log("--- 阶段: 回滚脚本 ---")
        result = run_command(
            shell_command(script, shell=config.load_settings().shell),
            cwd=target_path,
            env=ctx_env,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label="rollback",
            handle_out=handles if handles is not None else None,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        if not result.ok:
            if check_cancelled is not None and check_cancelled():
                return False, "回滚已被取消"
            return False, f"回滚脚本执行失败: {result.error}"

    service = (task.get("service_name") or "").strip()
    if service and task.get("deploy_method") == "systemd":
        if not command_exists("systemctl"):
            return False, "未找到 systemctl，无法重启服务"
        log("--- 阶段: systemd 重启 ---")
        result = run_command(
            ["systemctl", "restart", service],
            env=ctx_env,
            timeout=timeout,
            log=log,
            label="systemd",
            handle_out=handles if handles is not None else None,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        if not result.ok:
            if check_cancelled is not None and check_cancelled():
                return False, "回滚已被取消"
            return False, f"重启服务失败: {result.error}"
    message = f"已回滚到 {target_path.name}"
    if task.get("deploy_method") in {"docker", "docker_compose", "rsync"}:
        message += "；仅切换本地 current 并执行已配置的回滚脚本，不会自动重新部署容器或同步远端"
        log(message)
    elif task.get("deploy_method") == "artifact":
        message += "；仅切换本地 current 并执行已配置的回滚脚本，不会自动重新打包、替换已有产物或执行部署"
        log(message)
    return True, message


def _remote_rollback(
    task: dict[str, Any],
    *,
    selected_run: dict[str, Any] | None,
    target: Any,
    layout: Any,
    log: Callable[[str], None],
    log_batch: Callable[[list[str]], None] | None,
    timeout: int,
    kill_grace_seconds: int,
    handles: list[Any] | None,
    check_cancelled: Callable[[], bool] | None,
) -> tuple[bool, str]:
    """在目标主机上执行回滚：校验 → 换链 → 回滚脚本 → (systemd) 重启。"""
    try:
        root, current_link, active, chosen = _remote_rollback_target(
            task, selected_run, target, layout
        )
    except (DeployError, TargetError) as exc:
        return False, str(exc)
    except (OSError, RuntimeError, ValueError):
        return False, "目标主机上的发布目录或当前版本无效、已删除或无法访问"

    if selected_run is not None:
        log("回滚使用任务当前配置的回滚脚本和服务名称；历史运行未保存这些配置的快照。")
    try:
        target.swap_symlink(current_link, chosen)
    except (TargetError, OSError) as exc:
        return False, f"切换软链失败: {exc}"
    log(f"已回滚 current → {Path(chosen).name}"
        + (f"（原 {Path(active).name}）" if active else ""))

    script = (task.get("rollback_script") or "").strip()
    ctx_env = {
        "AUTODEPLOY_TASK_ID": str(task.get("id", "")),
        "AUTODEPLOY_TASK_NAME": str(task.get("name", "")),
        "AUTODEPLOY_RUN_ID": str(selected_run.get("id", "")) if selected_run else "",
        "AUTODEPLOY_COMMIT": str(selected_run.get("commit_after") or "") if selected_run else "",
        "AUTODEPLOY_RELEASE_DIR": chosen,
        "AUTODEPLOY_CURRENT_LINK": current_link,
        "AUTODEPLOY_RELEASES_ROOT": root,
        "AUTODEPLOY_TARGET_DIR": str(task.get("target_dir") or ""),
        "AUTODEPLOY_STAGE": "rollback",
    }
    if script:
        log("--- 阶段: 回滚脚本（目标主机） ---")
        result = target.run_script(
            script,
            cwd=chosen,
            env=ctx_env,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label="rollback",
            handle_out=handles if handles is not None else None,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        if not result.ok:
            if check_cancelled is not None and check_cancelled():
                return False, "回滚已被取消"
            return False, f"回滚脚本执行失败: {result.error}"

    service = (task.get("service_name") or "").strip()
    if service and task.get("deploy_method") == "systemd":
        if not target.which("systemctl"):
            return False, "目标主机上未找到 systemctl，无法重启服务"
        log("--- 阶段: systemd 重启（目标主机） ---")
        result = target.run(
            ["systemctl", "restart", service],
            env=ctx_env,
            timeout=timeout,
            log=log,
            label="systemd",
            handle_out=handles if handles is not None else None,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        if not result.ok:
            if check_cancelled is not None and check_cancelled():
                return False, "回滚已被取消"
            return False, f"重启服务失败: {result.error}"

    message = f"已在目标主机回滚到 {Path(chosen).name}"
    if task.get("deploy_method") in {"docker", "docker_compose", "rsync"}:
        message += "；仅切换目标主机上的 current 并执行回滚脚本，不会自动重新部署容器或同步远端"
    log(message)
    return True, message


# ---------------------------------------------------------------------------
# Preflight checks shown in the UI
# ---------------------------------------------------------------------------

REQUIRED_BINARIES: dict[str, str] = {
    "git": "git 未安装，无法拉取代码",
    "tar": "tar 未安装，无法打包",
}

METHOD_BINARIES: dict[str, str] = {
    "systemd": "systemctl",
    "docker": "docker",
    "docker_compose": "docker",
    "compose_inline": "docker",
    "rsync": "rsync",
}


def preflight(task: dict[str, Any], *, target: Any = None) -> list[dict[str, Any]]:
    """Report missing tools for a task's method without failing the deploy.

    传入 ``target`` 时检查的是**目标主机**上的工具（远程部署的构建与部署都在
    目标机执行）；不传则检查控制器——控制器仍需 git 与 tar（拉取与打包源码）。

    远程目标下 ``git`` 不是必需的：远端 workspace 由 rsync 镜像而来，目标机
    不需要仓库凭据，也不需要 git 客户端。
    """
    checks: list[dict[str, Any]] = []
    remote = bool(target is not None and getattr(target, "is_remote", False))
    which = target.which if target is not None else command_exists
    # 不依赖仓库的方式既没有源码要拉、也没有产物要打包：git/tar 缺失不应
    # 拦住它们（用户完全可以在一个没装 git 的机器上跑 compose）。
    method = (task.get("deploy_method") or "script").strip().lower()
    repo_free = method in REPO_FREE_METHODS
    for binary, message in REQUIRED_BINARIES.items():
        if repo_free:
            continue
        if remote and binary == "git":
            continue
        checks.append({"name": binary, "ok": which(binary), "message": message})
    if remote and not repo_free:
        # rsync 是源码镜像的前提；缺了就没法把代码送过去。
        checks.append({
            "name": "rsync",
            "ok": which("rsync"),
            "message": "目标主机未安装 rsync，无法接收源码",
        })
    method = (task.get("deploy_method") or "script").strip().lower()
    needed = METHOD_BINARIES.get(method)
    if needed:
        checks.append(
            {
                "name": needed,
                "ok": which(needed),
                "message": f"部署方式 {METHOD_LABELS.get(method, method)} 需要 {needed} 命令",
            }
        )
    return checks


def _prune_remote_releases(target: Any, layout: Any, *, keep: int,
                           log: Callable[[str], None] | None = None) -> list[str]:
    """在目标主机上清理历史发布，永不删除 ``current`` 指向的那一份。

    与本地 ``prune_releases`` 同语义：保留最新 ``keep`` 个 + 当前活跃版本。
    """
    names = target.list_dirs(layout.releases_root)
    if len(names) <= keep:
        return []
    keepers = set(names[:keep])
    active = target.readlink(layout.current_link)
    if active:
        active_name = str(active).rstrip("/").rsplit("/", 1)[-1]
        if active_name:
            keepers.add(active_name)
    removed: list[str] = []
    for name in names:
        if name in keepers:
            continue
        target.remove_tree(f"{layout.releases_root.rstrip('/')}/{name}")
        removed.append(name)
        if log:
            log(f"已清理历史发布: {name}")
    return removed


def disk_usage(path: Path) -> dict[str, int]:
    try:
        usage = shutil.disk_usage(str(path))
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def directory_size(path: Path, *, limit_entries: int = 200_000) -> int:
    """Total size of a directory tree, bounded so a huge tree cannot stall the UI."""
    total = 0
    count = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE_DIRS]
        for name in files:
            count += 1
            if count > limit_entries:
                return total
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def cleanup_path(path: Path) -> None:
    """Remove a workspace or release directory, tolerating a missing path."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 删除任务：停止容器
# ---------------------------------------------------------------------------

# 删除任务时最多等待容器停止的秒数。docker 偶发卡住不应让删除请求一直挂着。
CONTAINER_STOP_TIMEOUT_SECONDS = 120


def _docker_available() -> bool:
    return command_exists("docker")


def _compose_base() -> list[str]:
    return ["docker", "compose"] if _supports_compose_v2() else ["docker-compose"]


def _running_compose_containers(project: str) -> list[str]:
    """列出属于该 compose 项目、且可能仍在运行的容器名。

    只认带 ``com.docker.compose.project`` 标签且项目名匹配的容器，
    绝不触碰手工 ``docker run`` 起来的业务容器。
    """
    listing = run_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Labels}}"],
        timeout=30,
        log=None,
    )
    if not listing.ok:
        return []
    names: list[str] = []
    for line in listing.output.splitlines():
        name, _, label = line.partition("\t")
        labels = label or ""
        if "com.docker.compose.project" not in labels:
            continue
        owner = labels.split("com.docker.compose.project=")[-1].split(",")[0].strip()
        if owner == project:
            names.append(name.strip())
    return [name for name in names if name]


def stop_task_containers(
    task: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
    log_batch: Callable[[list[str]], None] | None = None,
    timeout: int = CONTAINER_STOP_TIMEOUT_SECONDS,
) -> list[str]:
    """停止并移除该任务部署起来的容器，返回处理过的容器名。

    仅对 docker / docker_compose 生效：
    * ``docker_compose``：按任务固定的项目名执行 ``compose down``，覆盖
      该项目下的全部服务与网络；
    * ``docker``：容器由任务自定义的 ``docker_command`` 启动，无法可靠推断
      容器名，因此执行任务内配置的**回滚脚本**（约定为停止/清理动作的位置），
      但绝不自动猜测容器名去 ``docker rm -f``，避免误删他人容器。

    任何一步失败都只记录到 ``log`` 并继续，删除任务本身不应因 Docker 环境
    异常而失败。
    """
    def note(message: str) -> None:
        if log is not None:
            log(message)

    method = str(task.get("deploy_method") or "").strip().lower()
    if method not in {"docker", "docker_compose", "compose_inline"}:
        return []
    if not _docker_available():
        note("! 未找到 docker 命令，跳过容器清理")
        return []

    removed: list[str] = []
    if method == "compose_inline":
        # compose 文件是从任务字段生成的，任务记录删除后内容已无从取得，
        # 无法再让 compose 解析它；直接按项目标签逐个移除容器与网络。
        project = compose_project_name(task)
        before = _running_compose_containers(project)
        note(f"--- 按项目标签停止 compose 项目 {project} ---")
        for name in before:
            note(f"--- 强制移除容器 {name} ---")
            removal = run_command(["docker", "rm", "-f", name], timeout=60, log=log)
            if removal.ok:
                removed.append(name)
        network = f"{project}_default"
        run_command(["docker", "network", "rm", network], timeout=60, log=log)
    elif method == "docker_compose":
        project = compose_project_name(task)
        # 先取一次名单：compose down 成功后容器已消失，事后查询会得到空列表。
        before = _running_compose_containers(project)
        note(f"--- 停止 compose 项目 {project} ---")
        result = run_command(
            [*_compose_base(), "-p", project, "down", "--remove-orphans", "--timeout", "10"],
            timeout=max(30, timeout),
            log=log,
            log_batch=log_batch,
        )
        if not result.ok:
            # compose down 依赖 compose 文件；文件已被删或项目未创建时会失败，
            # 此时退回到按标签逐个 rm -f，保证容器不会残留。
            note(f"! compose down 未成功（{result.error}），改为按项目标签清理容器")
            for name in before:
                note(f"--- 强制移除容器 {name} ---")
                removal = run_command(["docker", "rm", "-f", name], timeout=60, log=log)
                if removal.ok:
                    removed.append(name)
        else:
            removed = before

    script = (task.get("rollback_script") or "").strip()
    if method == "docker" and script:
        # 容器由用户脚本启动，容器名只有脚本知道；用回滚脚本作为停止钩子。
        note("--- 执行回滚脚本以停止容器 ---")
        env = {
            "AUTODEPLOY_TASK_ID": str(task.get("id", "")),
            "AUTODEPLOY_TASK_NAME": str(task.get("name", "")),
            "AUTODEPLOY_DEPLOY_METHOD": method,
            "AUTODEPLOY_TARGET_DIR": str(task.get("target_dir") or ""),
            "AUTODEPLOY_STAGE": "cleanup",
        }
        result = run_command(
            shell_command(script, shell=config.load_settings().shell),
            cwd=config.DATA_DIR,
            env=env,
            timeout=max(30, timeout),
            log=log,
            log_batch=log_batch,
            label="cleanup",
            kill_grace_seconds=config.load_settings().kill_grace_seconds,
        )
        if not result.ok:
            note(f"! 回滚脚本执行失败: {result.error}")
    elif method == "docker":
        note("! 该 Docker 任务未配置回滚脚本，无法自动停止容器；请手动确认容器状态")
    return removed


def purge_target_release_history(
    task: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """删除配置了 ``target_dir`` 的任务在该目录下的发布历史。

    仅删除 ``<target_dir>/releases`` 与 ``<target_dir>/current`` 这两个由本
    服务创建的条目，**绝不删除 target_dir 本身**——它通常是站点根目录，
    里面还有业务自己的文件，超出「删除任务」的授权范围。

    安全边界：目标必须是绝对路径；``releases`` 若为软链则跳过（指向别处，
    删除会波及无关数据）；``current`` 只在确实是软链时才解除，普通目录不动。
    """
    target_raw = str(task.get("target_dir") or "").strip()
    if not target_raw:
        return []

    def note(message: str) -> None:
        if log is not None:
            log(message)

    try:
        target = Path(os.path.expanduser(target_raw))
    except (OSError, ValueError):
        note(f"! 目标目录路径无效，跳过清理: {target_raw}")
        return []
    # 必须绝对且不能是根目录：否则 releases 会成为 /releases 这类系统路径。
    if not target.is_absolute() or len(target.parts) < 2:
        note(f"! 目标目录不是可安全清理的绝对路径，跳过: {target_raw}")
        return []
    if target.is_symlink():
        note(f"! 目标目录是软链，跳过清理: {target_raw}")
        return []

    releases_root, current_link = resolve_release_paths(task)
    removed: list[str] = []

    if releases_root.is_symlink():
        note(f"! 发布目录是软链，跳过清理: {releases_root}")
    elif releases_root.is_dir():
        cleanup_path(releases_root)
        removed.append(str(releases_root))

    if current_link.is_symlink():
        try:
            current_link.unlink()
            removed.append(str(current_link))
        except OSError as exc:
            note(f"! 解除 current 软链失败: {exc}")

    if removed:
        note(f"已清理目标目录下的发布历史，保留 {target}（目标目录未删除）")
    return removed
