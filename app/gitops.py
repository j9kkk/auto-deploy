"""Git checkout management.

Credentials are supplied through a temporary ``GIT_ASKPASS`` helper rather than
being embedded in the remote URL.  That way a token never appears in the process
argument list, in ``.git/config``, or in an error message that could reach a log
file — the classic way a deploy tool leaks its own credentials.

``GIT_TERMINAL_PROMPT=0`` is always set so a misconfigured credential fails
fast instead of hanging on an interactive prompt until the task times out.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import config
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


def _askpass_env(token: str, username: str, tmp_dir: Path, passphrase: str = "") -> dict[str, str]:
    """Create a throwaway askpass helper and return the git environment.

    ``passphrase`` 用于带密码的 SSH 私钥：git 在解锁密钥时会以交互方式询问，
    此时通过 askpass 提供。
    """
    env: dict[str, str] = {
        "GIT_TERMINAL_PROMPT": "0",
        # Never consult a system/global credential store: the service must not
        # pick up an operator's personal credentials by accident.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "",
    }
    if not token and not passphrase:
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
        '  *) printf "%s\\n" "$AUTODEPLOY_GIT_SECRET" ;;\n'
        "esac\n"
    )
    helper.write_text(script, encoding="utf-8")
    helper.chmod(stat.S_IRWXU)  # 0700: readable only by the service user.
    env["GIT_ASKPASS"] = str(helper)
    env["AUTODEPLOY_GIT_USER"] = user
    # 密码提示既可能是 HTTPS 令牌，也可能是 SSH 私钥口令，用同一个变量承载。
    env["AUTODEPLOY_GIT_SECRET"] = token or passphrase
    return env


def _ssh_env(private_key: str, passphrase: str, tmp_dir: Path, known_hosts: Path) -> dict[str, str]:
    """把 SSH 私钥写成临时文件并返回指向它的 git 环境变量。

    私钥以 0600 落盘，且只在该次运行的临时目录内，运行结束后随之清理
    （由 staging 目录的清理逻辑负责）。
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    key_path = tmp_dir / "id_deploy"
    key_path.write_text(private_key if private_key.endswith("\n") else private_key + "\n", encoding="utf-8")
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600

    env = {
        # GIT_SSH_COMMAND 里不内联任何秘密：私钥通过 -i 指向文件，口令走 askpass。
        "GIT_SSH_COMMAND": (
            f"ssh -i {shlex_quote(str(key_path))} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={shlex_quote(str(known_hosts))} "
            f"-o BatchMode={'no' if passphrase else 'yes'}"
        ),
    }
    return env


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


# 凭据类型的中文标签（前后端共用的单一来源）。
KIND_LABELS = {
    "https_token": "HTTPS 访问令牌",
    "ssh_key": "SSH 私钥",
    "none": "无",
}


class GitCredential:
    """一次运行所需的 Git 凭证（HTTPS 令牌或 SSH 私钥）。"""

    def __init__(
        self,
        *,
        kind: str = "none",
        username: str = "",
        token: str = "",
        private_key: str = "",
        passphrase: str = "",
        name: str = "",
    ) -> None:
        self.kind = kind or "none"
        self.username = username
        self.token = token
        self.private_key = private_key
        self.passphrase = passphrase
        self.name = name

    @property
    def is_ssh(self) -> bool:
        return self.kind == "ssh_key" and bool(self.private_key)

    @property
    def has_secret(self) -> bool:
        return bool(self.token or self.private_key)

    @classmethod
    def from_task(cls, task: Mapping[str, Any]) -> "GitCredential":
        """兼容旧行为：任务自带的 git_username/git_token。"""
        return cls(
            kind="https_token" if task.get("git_token") else "none",
            username=str(task.get("git_username") or ""),
            token=str(task.get("git_token") or ""),
        )

    def __repr__(self) -> str:  # pragma: no cover - 避免日志泄露
        return f"GitCredential(kind={self.kind!r}, name={self.name!r}, has_secret={self.has_secret})"


def resolve_credential(task: Mapping[str, Any], credential_row: Mapping[str, Any] | None) -> GitCredential:
    """决定任务该用哪份凭据。

    优先级：显式引用的全局凭据 > 任务自带的 git_token。
    这样既支持集中管理，又不会让已有的任务配置失效。
    """
    if credential_row:
        kind = str(credential_row.get("kind") or "https_token")
        secret = str(credential_row.get("secret") or "")
        if kind == "ssh_key":
            return GitCredential(
                kind="ssh_key",
                username=str(credential_row.get("username") or "git"),
                private_key=secret,
                passphrase=str(credential_row.get("passphrase") or ""),
                name=str(credential_row.get("name") or ""),
            )
        return GitCredential(
            kind="https_token",
            username=str(credential_row.get("username") or ""),
            token=secret,
            name=str(credential_row.get("name") or ""),
        )
    return GitCredential.from_task(task)


def _git_env(
    *,
    token: str = "",
    username: str = "",
    tmp_dir: Path,
    extra: Mapping[str, str] | None = None,
    credential: "GitCredential | None" = None,
    proxy: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """构造 git 运行环境。

    ``proxy`` 以环境变量形式传入（不是 ``-c http.proxy=``），这样代理密码
    不会出现在进程命令行里被其它用户看到。
    """
    cred = credential or GitCredential(kind="https_token" if token else "none",
                                       username=username, token=token)
    env = _askpass_env(cred.token, cred.username, tmp_dir, passphrase=cred.passphrase)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,  # ignore ~/.gitconfig
            "GIT_CONFIG_SYSTEM": os.devnull,  # and any system-wide config
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    if cred.is_ssh:
        known_hosts = (home or tmp_dir) / "known_hosts"
        if not known_hosts.exists():
            try:
                known_hosts.parent.mkdir(parents=True, exist_ok=True)
                known_hosts.touch()
            except OSError:
                pass
        env.update(_ssh_env(cred.private_key, cred.passphrase, tmp_dir, known_hosts))
    if proxy:
        env.update({str(k): str(v) for k, v in proxy.items()})
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


def _credential_hint(output: str) -> str:
    """把「需要认证但没有可用凭据」的 git 输出翻译成可操作的中文提示。

    私有仓库未配置凭据时，git 只会给出 ``could not read Username ...: terminal
    prompts disabled``，最终上报的却只是「命令退出码 128」，看不出该去配凭据。
    """
    lowered = (output or "").lower()
    if "could not read username" in lowered or "could not read password" in lowered:
        return "私有仓库需要认证，但任务未配置可用凭据：请在任务中选择凭据，或配置任务内访问令牌"
    if "authentication failed" in lowered or "invalid username or password" in lowered:
        return "认证失败：凭据无效或已过期，请在「凭据」页面测试并更新"
    if "permission denied" in lowered and "publickey" in lowered:
        return "认证失败：SSH 密钥不被接受，请确认公钥已登记到仓库的 Deploy Keys"
    return ""


def _network_hint(output: str) -> str:
    """把 git 的域名解析失败翻译成可操作提示。

    解析失败可能只是瞬时抖动（重试即可通），因此不给「不可达」的结论，
    只说明这次没解析成功以及该检查什么，避免运维按错误方向排查。
    """
    lowered = (output or "").lower()
    if "could not resolve host" in lowered or "name resolution" in lowered:
        return "域名解析失败：可能是 DNS 临时抖动（可重试）或服务器 DNS 配置有误"
    return ""


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


# 域名解析类错误：有些是「临时」失败（DNS 抖动、上游解析器过载），git 自己
# 会重试就能通；把它们当成确定不可达，会让一次解析抖动直接终止整个检查。
# 这些常量并非所有平台都有（Windows 缺 EAI_NODATA），故用 getattr 取值，
# 避免模块导入期就 AttributeError。
_TRANSIENT_DNS_ERRNOS = tuple(
    value
    for value in (
        getattr(socket, "EAI_AGAIN", None),
        getattr(socket, "EAI_FAIL", None),
        getattr(socket, "EAI_NODATA", None),
    )
    if value is not None
)


def _probe_is_conclusive(exc: OSError) -> bool:
    """该探测异常是否足以判定「不可达」。

    ``EAI_AGAIN`` 之类的临时解析失败按语义就是「稍后重试即可」，git 自己会重试；
    把这类失败当作确定不可达，一次 DNS 抖动就会终止整个更新检查或部署，
    而运维在服务器上手动访问却一切正常——这正是需要避免的误杀。
    """
    if isinstance(exc, socket.gaierror):
        return getattr(exc, "errno", None) not in _TRANSIENT_DNS_ERRNOS
    if isinstance(exc, socket.timeout):
        return True
    return getattr(exc, "errno", None) in (
        errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
    )


def _probe_failure_message(host: str, port: int, exc: OSError, *, via_proxy: bool = False) -> str | None:
    """把探测异常翻译成可读原因；``None`` 表示不应据此判定不可达。

    ``via_proxy`` 表示被探测的是代理而非目标主机，用于给出准确的措辞。
    """
    if not _probe_is_conclusive(exc):
        return None
    who = "代理" if via_proxy else "主机"
    if isinstance(exc, socket.gaierror):
        return f"无法解析{who}名 {host}（{exc.strerror or exc}）"
    if isinstance(exc, socket.timeout):
        return f"连接{who} {host}:{port} 超时，请检查网络或代理设置"
    tail = "，请检查代理设置" if via_proxy else ""
    return f"无法连接{who} {host}:{port}（{exc.strerror or exc}）{tail}"


def check_reachable(
    repo_url: str,
    *,
    timeout: float = 8.0,
    proxy_url: str = "",
    no_proxy: str = "",
) -> str | None:
    """探测远端是否可达。

    返回 ``None`` 表示可达、该地址不适用探测，或失败原因**不确定**
    （例如临时 DNS 抖动）——后两种都应交由 git 自己去尝试并重试，
    预检只负责拦下**确定**不可达的情况，避免误杀。

    **代理场景**：配置了代理时必须探测代理本身，而不是直连目标主机——
    否则内网环境里直连必然失败，会把「走代理能通」的仓库误判为不可达，
    导致部署在连通性预检阶段就被中止。``no_proxy`` 命中的主机则相反，
    必须直连探测，否则又会在代理不可用时误报。
    """
    target = _split_host_port(repo_url)
    if target is None:
        return None
    host, port = target

    # 应用设置里的代理优先；没配则回退到进程环境（git 同样会读它）。
    effective_proxy = (proxy_url or "").strip() or config.env_proxy_url()
    bypass = (no_proxy or "").strip() or config.env_no_proxy()
    if effective_proxy and not config.no_proxy_bypassed(host, bypass):
        from urllib.parse import urlparse

        try:
            parsed = urlparse(effective_proxy)
        except ValueError:
            return f"代理地址无法解析: {effective_proxy}"
        proxy_host = parsed.hostname
        if proxy_host:
            proxy_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                with socket.create_connection((proxy_host, proxy_port), timeout=timeout):
                    # 代理可达即认为可继续：目标主机由代理去连。
                    return None
            except OSError as exc:
                return _probe_failure_message(proxy_host, proxy_port, exc, via_proxy=True)

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return _probe_failure_message(host, port, exc)


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
    credential: "GitCredential | None" = None,
    proxy: Mapping[str, str] | None = None,
    home: Path | None = None,
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
    # 走代理时探测代理本身（见 check_reachable 的说明）。
    unreachable = check_reachable(
        repo_url, proxy_url=_proxy_url_of(proxy), no_proxy=_no_proxy_of(proxy)
    )
    if unreachable:
        raise GitError(unreachable)

    workspace = Path(workspace)
    git_dir = workspace / ".git"
    env = _git_env(
        token=token, username=username, tmp_dir=tmp_dir,
        credential=credential, proxy=proxy, home=home,
    )
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
            hint = _credential_hint(result.output) or _network_hint(result.output)
            if hint:
                raise GitError(hint)
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
            hint = _credential_hint(result.output) or _network_hint(result.output)
            if hint:
                raise GitError(hint)
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


def current_commit(
    workspace: Path,
    *,
    token: str = "",
    username: str = "",
    tmp_dir: Path | None = None,
    proxy: Mapping[str, str] | None = None,
) -> str:
    """Read HEAD without touching the network; used to seed a run's baseline."""
    workspace = Path(workspace)
    if not (workspace / ".git").exists():
        return ""
    env = _git_env(token=token, username=username, tmp_dir=tmp_dir or workspace, proxy=proxy)
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
    *,
    token: str = "",
    username: str = "",
    tmp_dir: Path,
    credential: "GitCredential | None" = None,
    proxy: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Public helper so callers can reuse the credential environment."""
    return _git_env(
        token=token, username=username, tmp_dir=tmp_dir,
        credential=credential, proxy=proxy,
    )


def _proxy_url_of(proxy: Mapping[str, str] | None) -> str:
    """从代理环境变量里取出 URL，供连通性探测使用。"""
    if not proxy:
        return ""
    return str(
        proxy.get("https_proxy") or proxy.get("http_proxy")
        or proxy.get("HTTPS_PROXY") or proxy.get("HTTP_PROXY") or ""
    )


def _no_proxy_of(proxy: Mapping[str, str] | None) -> str:
    """从代理环境变量里取出 no_proxy，探测必须与 git 用同一份。"""
    if not proxy:
        return ""
    return str(proxy.get("no_proxy") or proxy.get("NO_PROXY") or "")


def test_credentials(
    *,
    repo_url: str,
    credential: "GitCredential",
    tmp_dir: Path,
    timeout: int = 30,
    proxy: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[bool, str]:
    """用给定凭据试连仓库，返回 ``(是否成功, 说明)``。

    走 ``git ls-remote``：它只协商认证并列出引用，不会写入任何本地状态，
    因此可以在界面上安全地反复测试。凭据错误、网络不通都能给出具体原因。
    """
    problem = validate_repo_url(repo_url)
    if problem:
        return False, problem

    if credential.kind == "ssh_key" and not credential.private_key:
        return False, "SSH 凭据缺少私钥"
    if credential.kind == "https_token" and not credential.token:
        return False, "HTTPS 凭据缺少访问令牌"

    unreachable = check_reachable(
        repo_url, proxy_url=_proxy_url_of(proxy), no_proxy=_no_proxy_of(proxy)
    )
    if unreachable:
        return False, unreachable

    env = _git_env(
        credential=credential,
        proxy=proxy,
        home=home,
        tmp_dir=tmp_dir,
    )
    result = run_command(
        ["git", *_GIT_RESILIENCE_CONFIG, "ls-remote", "--heads", "--", repo_url],
        env=env,
        timeout=timeout,
    )
    if result.ok:
        branches = [line for line in result.output.splitlines() if line.strip()]
        return True, f"连接成功，远端有 {len(branches)} 个分支"

    output = (result.output or "").lower()
    if "authentication failed" in output or "invalid username or password" in output:
        return False, "认证失败：凭据无效或已过期"
    if "permission denied" in output or "publickey" in output:
        return False, "认证失败：密钥不被接受（可能未在仓库中登记公钥）"
    if "not found" in output and "repository" in output:
        return False, "仓库不存在或当前凭据无权访问"
    if "could not read username" in output:
        return False, "仓库需要认证，但未提供有效凭据"
    return False, result.error or "连接失败"


def ls_remote_tags(
    *,
    repo_url: str,
    tmp_dir: Path,
    timeout: int = 30,
    proxy: Mapping[str, str] | None = None,
    attempts: int = 2,
) -> CommandResult:
    """列出远端标签，复用与部署完全相同的 git 环境。

    更新检查的 git 回退路径必须走这里，而不是自己拼 ``git ls-remote``：
    只有这样才能拿到 ``http.version=HTTP/1.1`` 等抗抖配置、代理环境以及
    ``GIT_TERMINAL_PROMPT=0``（否则在缺少凭据时会挂在交互提示上直到超时）。
    """
    problem = validate_repo_url(repo_url)
    if problem:
        return CommandResult(
            command="git ls-remote --tags", exit_code=1, output="", duration_ms=0, error=problem
        )

    # 先探测可达性：不可达时几秒内给出「无法连接 <host>:<port>」，
    # 而不是让更新检查干等一次完整的 ls-remote 超时。
    unreachable = check_reachable(
        repo_url, proxy_url=_proxy_url_of(proxy), no_proxy=_no_proxy_of(proxy)
    )
    if unreachable:
        return CommandResult(
            command="git ls-remote --tags", exit_code=1, output="", duration_ms=0, error=unreachable
        )

    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = _git_env(proxy=proxy, tmp_dir=tmp_dir)
    result = _run_git(
        ["ls-remote", "--tags", "--", repo_url],
        cwd=None,
        env=env,
        timeout=timeout,
        log=None,
        label="更新检查",
        attempts=attempts,
        retry_budget_seconds=timeout,
    )
    if not result.ok:
        # DNS 抖动重试后仍失败时，git 只会留下 ``could not resolve host``；
        # 换成可操作的中文提示，避免用户拿着「命令退出码 128」无从下手。
        hint = _credential_hint(result.output) or _network_hint(result.output)
        if hint:
            result.error = hint
    return result


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
