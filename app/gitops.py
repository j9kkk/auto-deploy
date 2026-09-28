"""Git checkout management.

Credentials are supplied through a temporary ``GIT_ASKPASS`` helper rather than
being embedded in the remote URL.  That way a token never appears in the process
argument list, in ``.git/config``, or in an error message that could reach a log
file — the classic way a deploy tool leaks its own credentials.

``GIT_TERMINAL_PROMPT=0`` is always set so a misconfigured credential fails
fast instead of hanging on an interactive prompt until the task times out.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .executor import CommandResult, run_command

# Schemes a repository URL may use.  Plain paths and file:// are permitted: an
# admin deploying from a local mirror is a legitimate setup, and only an
# authenticated admin can create tasks.
_ALLOWED_SCHEMES = ("https://", "http://", "git://", "ssh://", "git@", "file://")

_SCP_LIKE = re.compile(r"^[\w.\-]+@[\w.\-]+:[\w./\-~]+$")


class GitError(Exception):
    """A git operation failed in a way the caller should report."""


@dataclass
class CheckoutInfo:
    """State of the working copy after a sync."""

    commit: str = ""
    short_commit: str = ""
    message: str = ""
    author: str = ""
    committed_at: str = ""
    branch: str = ""
    changed_files: int = 0
    previous_commit: str = ""
    is_first_clone: bool = False
    result: CommandResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def changes_detected(self) -> bool:
        """True when the checkout moved to a different commit."""
        if self.is_first_clone:
            return True
        if not self.previous_commit:
            return True
        return self.previous_commit != self.commit


def validate_repo_url(url: str) -> str | None:
    """Return an error message when the URL is unusable, else None."""
    text = (url or "").strip()
    if not text:
        return "仓库地址不能为空"
    # A leading dash would be parsed by git as an option, not a URL.
    if text.startswith("-"):
        return "仓库地址不能以 - 开头"
    if any(ch in text for ch in "\n\r\t "):
        return "仓库地址不能包含空白字符"
    if text.startswith(_ALLOWED_SCHEMES) or _SCP_LIKE.match(text):
        return None
    if re.fullmatch(r"[a-zA-Z]:[\\/].*", text):
        return None
    if text.startswith("/") or text.startswith("./") or text.startswith("../") or text.startswith("~"):
        return None
    return "仓库地址必须是 https/http/ssh/git 协议或以 / 开头的本地路径"


def validate_branch(branch: str) -> str | None:
    text = (branch or "").strip()
    if not text:
        return "分支不能为空"
    if text.startswith("-") or ".." in text or any(c in text for c in " ~^:?*[\\"):
        return "分支名包含非法字符"
    return None


def _askpass_env(token: str, username: str, tmp_dir: Path) -> dict[str, str]:
    """Create a throwaway askpass helper and return the git environment."""
    env: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
        # Never consult a system/global credential store: the service must not
        # pick up an operator's personal credentials by accident.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "",
    }
    if not token:
        return env

    tmp_dir.mkdir(parents=True, exist_ok=True)
    helper = tmp_dir / "git-askpass.sh"
    user = username or "x-access-token"
    # The script distinguishes the two prompts git can issue; both answers are
    # read from the environment so nothing is written into the script itself.
    script = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *sername*) printf "%s\\n" "$AUTODEPLOY_GIT_USER" ;;\n'
        '  *) printf "%s\\n" "$AUTODEPLOY_GIT_TOKEN" ;;\n'
        "esac\n"
    )
    helper.write_text(script, encoding="utf-8")
    helper.chmod(stat.S_IRWXU)  # 0700: readable only by the service user.
    env["GIT_ASKPASS"] = str(helper)
    env["AUTODEPLOY_GIT_USER"] = user
    env["AUTODEPLOY_GIT_TOKEN"] = token
    return env


def _git_env(
    *,
    token: str,
    username: str,
    tmp_dir: Path,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = _askpass_env(token, username, tmp_dir)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,  # ignore ~/.gitconfig
            "GIT_CONFIG_SYSTEM": os.devnull,  # and any system-wide config
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# Options applied to every git invocation.
#
# `http.version=HTTP/1.1` avoids the "HTTP2 framing layer" failures that some
# proxies and gateways produce against GitHub, and it is what makes a fetch
# over a flaky link succeed reliably. The low-speed settings turn a stalled
# transfer into a prompt error instead of a hang until the task timeout.
_GIT_RESILIENCE_CONFIG = (
    "-c", "http.version=HTTP/1.1",
    "-c", "http.lowSpeedLimit=1000",
    "-c", "http.lowSpeedTime=60",
)

# Substrings that indicate a transient network problem worth retrying, as
# opposed to a real failure such as a missing branch or a bad credential.
_TRANSIENT_MARKERS = (
    "http2 framing layer",
    "unable to access",
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "early eof",
    "rpc failed",
    "the remote end hung up",
    "tls connection was non-properly terminated",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "temporary failure in name resolution",
)


def _looks_transient(output: str) -> bool:
    lowered = (output or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _split_host_port(repo_url: str) -> tuple[str, int] | None:
    """Extract ``(host, port)`` from an http(s) URL for a reachability probe."""
    from urllib.parse import urlparse

    if not repo_url.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(repo_url)
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host, (parsed.port or (443 if parsed.scheme == "https" else 80))


def check_reachable(
    repo_url: str,
    *,
    timeout: float = 8.0,
) -> str | None:
    """Probe TCP reachability of an http(s) remote.

    Returns ``None`` when reachable (or when the URL is not http-based, where a
    probe is not meaningful), or a human-readable error.  Without this, an
    unreachable host costs three full TCP-connect timeouts before the run
    fails; the probe turns that into a fast, specific error.
    """
    target = _split_host_port(repo_url)
    if target is None:
        return None
    host, port = target
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return f"连接 {host}:{port} 超时，请检查网络或代理设置"
    except OSError as exc:
        return f"无法连接 {host}:{port}（{exc.strerror or exc}）"


def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout: int,
    log: Callable[[str], None] | None,
    label: str,
    check_cancelled: Callable[[], bool] | None = None,
    kill_grace_seconds: int = 10,
    attempts: int = 3,
    retry_budget_seconds: int | None = None,
) -> CommandResult:
    """Run a git command, retrying transient network failures.

    GitHub and intermediate proxies intermittently fail a fetch with errors
    like an HTTP/2 framing problem. Those are worth retrying; a bad credential
    or a missing branch is not, so only recognised transient markers retry.

    ``retry_budget_seconds`` caps the total wall-clock time spent across all
    attempts, so a slow failure cannot multiply the caller's timeout threefold.
    """
    result: CommandResult | None = None
    deadline = (
        time.monotonic() + retry_budget_seconds
        if retry_budget_seconds and retry_budget_seconds > 0
        else None
    )
    for attempt in range(1, max(1, attempts) + 1):
        result = run_command(
            ["git", *_GIT_RESILIENCE_CONFIG, *args],
            cwd=cwd,
            env=env,
            timeout=timeout,
            log=log,
            label=label,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        if result.ok or result.cancelled:
            return result
        if attempt >= attempts or not _looks_transient(result.output):
            return result
        if check_cancelled is not None and check_cancelled():
            return result
        if deadline is not None and time.monotonic() >= deadline:
            if log:
                log("! 已达到 git 重试时间预算，停止重试")
            return result
        if log:
            log(f"! git 操作失败（{result.error}），正在重试 {attempt}/{attempts - 1}…")
        # Back off a little so a momentarily unavailable remote can recover.
        time.sleep(min(30, 3 * attempt))
    assert result is not None
    return result


def sync_checkout(
    *,
    repo_url: str,
    branch: str,
    workspace: Path,
    depth: int = 1,
    token: str = "",
    username: str = "",
    tmp_dir: Path,
    timeout: int = 900,
    log: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    kill_grace_seconds: int = 10,
    reset_hard: bool = True,
) -> CheckoutInfo:
    """Clone or fetch the repository, returning what the checkout now holds.

    The workspace is reused between runs so only the delta is transferred.  On
    the first run a fresh clone is made; afterwards the existing checkout is
    fetched and hard-reset to the remote branch, which makes the result
    independent of any local edits a build step may have left behind.
    """
    problem = validate_repo_url(repo_url) or validate_branch(branch)
    if problem:
        raise GitError(problem)

    # Fail fast when the remote host is unreachable. Without this check, an
    # unreachable GitHub costs three ~75s TCP timeouts before the run fails.
    unreachable = check_reachable(repo_url)
    if unreachable:
        raise GitError(unreachable)

    workspace = Path(workspace)
    git_dir = workspace / ".git"
    env = _git_env(token=token, username=username, tmp_dir=tmp_dir)
    info = CheckoutInfo(branch=branch)

    if not git_dir.exists():
        info.is_first_clone = True
        workspace.parent.mkdir(parents=True, exist_ok=True)
        # 认领标记不属于仓库内容：非 git 目录被清空重克隆时也要保留，
        # 否则任务名到目录的归属关系会丢失。
        owner_file = workspace / ".autodeploy-owner"
        saved_owner = owner_file.read_text(encoding="utf-8") if owner_file.exists() else None
        if workspace.exists() and any(workspace.iterdir()):
            # A leftover directory that is not a checkout would make `git clone`
            # fail, so clear it before cloning.
            if log:
                log("! 工作目录存在但不是 git 仓库，正在清理后重新克隆")
            _remove_tree(workspace)
        clone_args = ["clone", "--branch", branch, "--single-branch"]
        if depth and depth > 0:
            clone_args += ["--depth", str(int(depth))]
        clone_args += ["--", repo_url, str(workspace)]
        result = _run_git(
            clone_args,
            cwd=workspace.parent,
            env=env,
            timeout=timeout,
            log=log,
            label="git",
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
            retry_budget_seconds=timeout,
        )
        if not result.ok:
            # A branch that does not exist yet is worth a clearer message than
            # git's raw output.
            if "Remote branch" in result.output and "not found" in result.output:
                raise GitError(f"远程分支 {branch!r} 不存在")
            raise GitError(result.error or "git clone 失败")
        if saved_owner is not None:
            # 恢复目录认领标记（重克隆把它随旧目录一起清掉了）。
            try:
                (workspace / ".autodeploy-owner").write_text(saved_owner, encoding="utf-8")
            except OSError:
                pass
        info.result = result
    else:
        info.previous_commit = rev_parse(workspace, "HEAD", env=env, timeout=60)
        fetch_args = ["fetch", "--prune", "origin", branch]
        if depth and depth > 0:
            fetch_args = ["fetch", "--prune", "--depth", str(int(depth)), "origin", branch]
        result = _run_git(
            fetch_args,
            cwd=workspace,
            env=env,
            timeout=timeout,
            log=log,
            label="git",
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
            retry_budget_seconds=timeout,
        )
        if not result.ok:
            raise GitError(result.error or "git fetch 失败")
        if reset_hard:
            reset = _run_git(
                ["reset", "--hard", "FETCH_HEAD"],
                cwd=workspace,
                env=env,
                timeout=120,
                log=log,
                label="git",
                check_cancelled=check_cancelled,
                kill_grace_seconds=kill_grace_seconds,
            )
            if not reset.ok:
                raise GitError(reset.error or "git reset 失败")
        # Clean untracked files so the next build starts from a known state.
        clean = _run_git(
            # 排除认领标记与缓存目录，clean 不得删除服务自身的管理文件。
            ["clean", "-fdx", "-e", ".autodeploy-owner", "-e", ".autodeploy-cache"],
            cwd=workspace,
            env=env,
            timeout=300,
            log=log,
            label="git",
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )
        info.result = result
        if not clean.ok and log:
            log("! git clean 未完全成功，构建产物可能残留")

    info.commit = rev_parse(workspace, "HEAD", env=env, timeout=60) or ""
    info.short_commit = (info.commit or "")[:8]
    details = commit_details(workspace, "HEAD", env=env, timeout=60)
    info.message = details.get("message", "")
    info.author = details.get("author", "")
    info.committed_at = details.get("committed_at", "")

    if info.previous_commit and info.commit and info.previous_commit != info.commit:
        info.changed_files = count_changed_files(
            workspace, info.previous_commit, info.commit, env=env, timeout=120
        )
    elif info.is_first_clone:
        info.changed_files = count_tracked_files(workspace, env=env, timeout=120)

    return info


def current_commit(workspace: Path, *, token: str = "", username: str = "", tmp_dir: Path | None = None) -> str:
    """Read HEAD without touching the network; used to seed a run's baseline."""
    workspace = Path(workspace)
    if not (workspace / ".git").exists():
        return ""
    env = _git_env(token=token, username=username, tmp_dir=tmp_dir or workspace)
    return rev_parse(workspace, "HEAD", env=env, timeout=30)


def rev_parse(workspace: Path, rev: str, *, env: Mapping[str, str], timeout: int = 60) -> str:
    result = run_command(
        ["git", "rev-parse", "--verify", "--quiet", rev],
        cwd=workspace,
        env=env,
        timeout=timeout,
        log=None,
    )
    return result.output.strip().splitlines()[-1] if result.ok and result.output.strip() else ""


def commit_details(
    workspace: Path, rev: str, *, env: Mapping[str, str], timeout: int = 60
) -> dict[str, str]:
    """Read the commit subject, author and timestamp in one call."""
    result = run_command(
        ["git", "log", "-1", "--pretty=format:%s%x1f%an%x1f%cI", rev],
        cwd=workspace,
        env=env,
        timeout=timeout,
        log=None,
    )
    if not result.ok or not result.output.strip():
        return {}
    parts = result.output.strip().split("\x1f")
    return {
        "message": parts[0].strip() if len(parts) > 0 else "",
        "author": parts[1].strip() if len(parts) > 1 else "",
        "committed_at": parts[2].strip() if len(parts) > 2 else "",
    }


def count_changed_files(
    workspace: Path, old_rev: str, new_rev: str, *, env: Mapping[str, str], timeout: int = 120
) -> int:
    result = run_command(
        ["git", "diff", "--name-only", f"{old_rev}..{new_rev}"],
        cwd=workspace,
        env=env,
        timeout=timeout,
        log=None,
    )
    if not result.ok:
        # A shallow clone may not contain the previous commit; the change count
        # is informational, so report 0 rather than failing the deploy.
        return 0
    return len([line for line in result.output.splitlines() if line.strip()])


def count_tracked_files(workspace: Path, *, env: Mapping[str, str], timeout: int = 120) -> int:
    result = run_command(
        ["git", "ls-files"], cwd=workspace, env=env, timeout=timeout, log=None
    )
    if not result.ok:
        return 0
    return len([line for line in result.output.splitlines() if line.strip()])


def head_subject(workspace: Path, *, env: Mapping[str, str]) -> str:
    return commit_details(workspace, "HEAD", env=env).get("message", "")


def changed_files_between(
    workspace: Path, old_rev: str, new_rev: str, *, env: Mapping[str, str], limit: int = 200
) -> list[str]:
    result = run_command(
        ["git", "diff", "--name-only", f"{old_rev}..{new_rev}"],
        cwd=workspace,
        env=env,
        timeout=120,
        log=None,
    )
    if not result.ok:
        return []
    return [line for line in result.output.splitlines() if line.strip()][:limit]


def make_git_env(
    *, token: str = "", username: str = "", tmp_dir: Path
) -> dict[str, str]:
    """Public helper so callers can reuse the credential environment."""
    return _git_env(token=token, username=username, tmp_dir=tmp_dir)


def _remove_tree(path: Path) -> None:
    import shutil

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=True)


def repo_key(repo_url: str) -> str:
    """A filesystem-safe, collision-resistant key derived from a repo URL."""
    import hashlib

    cleaned = (repo_url or "").strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    tail = cleaned.rstrip("/").rsplit("/", 1)[-1] or "repo"
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", tail)[:40].strip("-") or "repo"
    # Hash the normalised URL so `repo.git` and `repo` (or a trailing slash)
    # map to the same key.
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def temp_credential_dir(base: Path) -> Path:
    """A private directory for askpass helpers, removed with the release."""
    path = base / ".git-credentials"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass
    return path


def make_temp_dir(prefix: str = "autodeploy-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
