"""远程主机执行层：通过 SSH 在另一台机器上运行命令与传输文件。

设计要点
--------
* **只支持密钥认证**。密码认证需要 ``sshpass``（不在依赖内），且密码会出现在
  命令行里，违反「秘密不进命令行」的红线。
* **私钥不落持久盘**：每次连接把私钥写入 0600 的临时文件，连接结束即删除。
  这与 ``gitops._ssh_env`` 对 git 凭据的处理方式一致。
* **主机指纹钉住**：``StrictHostKeyChecking=accept-new`` 只信任首次连接，
  首次成功后把指纹存回 hosts.host_key；之后指纹不符直接拒绝，避免中间人。
* **命令经本机 ``run_command`` 执行**，因此超时、取消、进程组终止、日志脱敏
  等全部复用既有实现，不另造一套。

调用方拿到的永远是 ``CommandResult``，与本地执行的结果结构一致，这样
``DeployContext.exec`` 可以在本机/远程之间无缝切换。
"""

from __future__ import annotations

import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .executor import CommandResult, ProcessHandle, run_command

# SSH 连接超时（秒）：建连本身失败要快速暴露，而不是等命令超时。
SSH_CONNECT_TIMEOUT = 15
# 主机指纹探测使用的算法。ed25519 为主，rsa 兜底旧机器。
HOST_KEY_TYPES = ("ed25519", "rsa", "ecdsa")


class RemoteError(Exception):
    """远程操作失败；消息面向用户（中文）。"""


def quote_remote(value: str) -> str:
    """把值转义成可安全嵌入远端 shell 的单参数。

    远端命令最终通过 ``ssh host -- <command string>`` 交给远端 shell 解释，
    因此每个参数都必须单独转义，不能直接拼接。
    """
    return shlex.quote(str(value))


def build_remote_command(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """把 argv + cwd + env 组装成远端要执行的 shell 命令串。

    用 ``cd`` + 导出的形式而不是 ``env -i``：目标机可能依赖 sshd 提供的
    基础环境（PATH 等），全清空会让 docker/node 找不到。
    """
    parts: list[str] = []
    if env:
        for key, value in env.items():
            parts.append(f"export {key}={quote_remote(value)}")
    if cwd:
        parts.append(f"cd {quote_remote(cwd)}")
    parts.append(" ".join(quote_remote(a) for a in args))
    return " && ".join(parts)


@dataclass
class RemoteProbe:
    """一次连通性探测的结果，直接喂给前端「测试连接」。"""

    ok: bool
    message: str = ""
    host_fingerprint: str = ""
    ssh_version: str = ""
    rsync_version: str = ""
    docker_version: str = ""
    compose_version: str = ""
    uname: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "host_fingerprint": self.host_fingerprint,
            "ssh_version": self.ssh_version,
            "rsync_version": self.rsync_version,
            "docker_version": self.docker_version,
            "compose_version": self.compose_version,
            "uname": self.uname,
        }


class SshTransport:
    """一条到远程主机的 SSH 通道。

    构造时只持有配置，不建立连接；每次 ``run`` 都是一次独立的 ssh 调用
    （无连接复用），换来的是崩溃后无残留状态。批量操作用 ``rsync`` 减少往返。
    """

    def __init__(
        self,
        *,
        address: str,
        port: int = 22,
        username: str,
        private_key: str = "",
        passphrase: str = "",
        known_hosts: Path | None = None,
        expected_host_key: str = "",
        work_dir: Path | None = None,
    ) -> None:
        if not address:
            raise RemoteError("主机地址不能为空")
        if not username:
            raise RemoteError("SSH 用户名不能为空")
        self.address = address
        self.port = int(port or 22)
        self.username = username
        self.private_key = private_key
        self.passphrase = passphrase
        # 每台主机的 known_hosts 独立存放；指纹钉住依赖它保持稳定。
        self._work_dir = work_dir
        self.known_hosts = known_hosts
        self.expected_host_key = expected_host_key
        self._key_path: Path | None = None
        self._tmp_dir: Path | None = None

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "SshTransport":
        self._materialize_key()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """删除临时私钥目录。私钥绝不留在磁盘上超过一次调用的生命期。"""
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
            self._key_path = None
        elif self._key_path is not None:
            try:
                self._key_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._key_path = None

    def _materialize_key(self) -> None:
        if self._key_path is not None or not self.private_key:
            return
        if self._work_dir is not None:
            base = Path(self._work_dir)
            base.mkdir(parents=True, exist_ok=True)
            self._tmp_dir = None
            key_path = base / "id_remote"
        else:
            self._tmp_dir = Path(tempfile.mkdtemp(prefix="autodeploy-ssh-"))
            key_path = self._tmp_dir / "id_remote"
        text = self.private_key if self.private_key.endswith("\n") else self.private_key + "\n"
        key_path.write_text(text, encoding="utf-8")
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self._key_path = key_path
        if self.known_hosts is None:
            parent = self._work_dir or self._tmp_dir or Path(tempfile.gettempdir())
            self.known_hosts = Path(parent) / "known_hosts"
        self.known_hosts.touch(exist_ok=True)

    # -- 命令构造 ---------------------------------------------------------
    def ssh_base(self) -> list[str]:
        """ssh 命令行前缀（不含目标与远端命令）。

        安全相关选项集中在这里：禁用交互提示、只用指定私钥、独立 known_hosts。
        口令保护（passphrase）在无 ssh-agent 的容器里无法交互输入，因此要求
        密钥必须免口令；否则明确报错而不是静默挂起。
        """
        self._materialize_key()
        if self.passphrase:
            raise RemoteError(
                "暂不支持带口令的 SSH 私钥：容器内无法交互输入口令。"
                "请使用免口令密钥（推荐），或改用 ssh-agent。"
            )
        options = [
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
        ]
        if self._key_path is not None:
            options += ["-i", str(self._key_path)]
        return ["ssh", *options, "-p", str(self.port), f"{self.username}@{self.address}"]

    def _wrap(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        remote_command = build_remote_command(args, cwd=cwd, env=env)
        return [*self.ssh_base(), "--", remote_command]

    def _wrap_script(self, script: str) -> list[str]:
        # 用非登录 shell：``-l`` 会 source profile，目标机的 banner/提示会混进
        # 被解析的 stdout。解析类调用另用哨兵包裹，双保险。
        return [*self.ssh_base(), "--", f"bash -c {quote_remote(script)}"]

    # -- 执行 -------------------------------------------------------------
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 600,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        label: str | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        """在远端执行一条命令，返回与本地一致的 CommandResult。

        展示用命令串会打上 ``<host>`` 前缀，让日志能区分本地与远端。
        """
        try:
            command = self._wrap(args, cwd=cwd, env=env)
        except RemoteError as exc:
            return CommandResult("", 1, "", 0, error=str(exc))
        display_label = f"{self.username}@{self.address}"
        return run_command(
            command,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label=f"{display_label} {label}" if label else display_label,
            handle_out=handle_out,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )

    def run_script(
        self,
        script: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 600,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        label: str | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        """在远端执行一段 shell 脚本（多行、含管道时用）。

        脚本体与普通命令不同，不能逐参数传给 ssh；这里把 ``env`` 与 ``cwd``
        以 export / cd 前缀拼到脚本前面，再用 ``bash -c`` 执行——与
        ``build_remote_command`` 对 argv 的处理保持同一套转义规则。
        """
        prefix: list[str] = []
        if env:
            for key, value in env.items():
                prefix.append(f"export {key}={quote_remote(value)}")
        if cwd:
            prefix.append(f"cd {quote_remote(cwd)}")
        body = "\n".join([*prefix, script]) if prefix else script
        try:
            command = self._wrap_script(body)
        except RemoteError as exc:
            return CommandResult("", 1, "", 0, error=str(exc))
        display_label = f"{self.username}@{self.address}"
        return run_command(
            command,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label=f"{display_label} {label}" if label else display_label,
            handle_out=handle_out,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )

    # -- 文件传输 ---------------------------------------------------------
    def rsync_up(
        self,
        local_dir: Path | str,
        remote_dir: str,
        *,
        delete: bool = False,
        excludes: Sequence[str] = (),
        timeout: int = 1800,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        """把本地目录增量同步到远端目录。

        用 ``rsync -e ssh`` 并显式传入与 ``ssh_base`` 相同的安全选项：
        rsync 默认用系统 ssh，不会带私钥与 known_hosts。
        """
        if not shutil.which("rsync"):
            return CommandResult("", 1, "", 0, error="本地未找到 rsync 命令")
        ssh_cmd = " ".join(shlex.quote(part) for part in self._ssh_options_only())
        args = ["rsync", "-az"]
        if delete:
            args.append("--delete")
        args += ["-e", ssh_cmd]
        for pattern in excludes:
            args += ["--exclude", pattern]
        source = str(local_dir).rstrip("/") + "/"
        args += [source, f"{self.username}@{self.address}:{_remote_path(remote_dir)}"]
        return run_command(
            args,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label=f"rsync→{self.address}",
            handle_out=handle_out,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )

    def rsync_down(
        self,
        remote_dir: str,
        local_dir: Path | str,
        *,
        timeout: int = 1800,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        """把远端文件/目录下载到本地。"""
        if not shutil.which("rsync"):
            return CommandResult("", 1, "", 0, error="本地未找到 rsync 命令")
        ssh_cmd = " ".join(shlex.quote(part) for part in self._ssh_options_only())
        source = f"{self.username}@{self.address}:{_remote_path(remote_dir)}"
        args = ["rsync", "-az", "-e", ssh_cmd, source, str(local_dir)]
        return run_command(
            args,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label=f"rsync←{self.address}",
            handle_out=handle_out,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )

    def _ssh_options_only(self) -> list[str]:
        """给 rsync -e 用的 ssh 片段：安全选项 + 端口，不含目标。"""
        base = self.ssh_base()
        # ssh_base 形如 ["ssh", opts..., "-p", port, "user@host"]，去掉末尾目标。
        return base[:-1]

    # -- 探测 -------------------------------------------------------------
    def probe(self, *, timeout: int = 30) -> RemoteProbe:
        """探测连通性、工具链版本与主机指纹。

        指纹通过 ``ssh-keyscan`` 获取（不需要私钥即可读到公钥），失败时留空，
        不影响连通性结论——用户仍可正常使用，只是少了指纹钉住这一层。
        """
        result = self.run(["true"], timeout=timeout)
        if not result.ok:
            return RemoteProbe(ok=False, message=_describe_ssh_failure(result))

        probe_script = (
            "echo SSH_READY; "
            "uname -sr 2>/dev/null || true; "
            "echo ---RSYNC---; rsync --version 2>/dev/null | head -1 || true; "
            "echo ---DOCKER---; docker --version 2>/dev/null || true; "
            "echo ---COMPOSE---; (docker compose version 2>/dev/null || docker-compose --version 2>/dev/null) || true"
        )
        info = self.run_script(probe_script, timeout=timeout)
        parsed = _parse_probe_output(info.output if info.ok else "")
        fingerprint = self.fetch_host_fingerprint(timeout=timeout)
        return RemoteProbe(
            ok=True,
            message="连接成功",
            host_fingerprint=fingerprint,
            ssh_version="",
            rsync_version=parsed.get("rsync", ""),
            docker_version=parsed.get("docker", ""),
            compose_version=parsed.get("compose", ""),
            uname=parsed.get("uname", ""),
        )

    def fetch_host_fingerprint(self, *, timeout: int = 20) -> str:
        """读取目标主机的公钥指纹（不需要登录）。

        返回形如 ``ssh-ed25519 SHA256:xxxx`` 的首行；探测失败返回空串。
        """
        if not shutil.which("ssh-keyscan"):
            return ""
        for key_type in HOST_KEY_TYPES:
            scan = run_command(
                ["ssh-keyscan", "-p", str(self.port), "-t", key_type, "-T", "8", self.address],
                timeout=timeout,
            )
            if not scan.ok or not scan.output.strip():
                continue
            for line in scan.output.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                # 用 ssh-keygen 计算指纹，避免自己实现 base64/hash 细节。
                fp = _fingerprint_of(parts[2])
                if fp:
                    return f"{parts[1]} {fp}"
        return ""


def _describe_ssh_failure(result: CommandResult) -> str:
    """把 ssh 的原始失败信息翻译成可操作的中文提示。

    ssh 在大多数连接失败场景只给退出码 255，原始输出里那句
    ``Permission denied`` / ``Connection refused`` 才是真正有用的信息。
    """
    text = f"{result.output}\n{result.error}"
    if "Host key verification failed" in text or "REMOTE HOST IDENTIFICATION HAS CHANGED" in text:
        return "目标主机指纹已变化，可能是重装了系统或存在中间人攻击；确认无误后请重置指纹再测试"
    if "Permission denied" in text:
        return "SSH 认证失败：请确认私钥已加入目标机的 ~/.ssh/authorized_keys，且用户名正确"
    if "Connection refused" in text:
        return "连接被拒绝：目标机的 SSH 端口未开放，请检查地址与端口"
    if "No route to host" in text:
        return "无法到达目标主机：请检查网络连通性与防火墙"
    if "timed out" in text.lower() or result.timed_out:
        return "连接超时：目标主机无响应，请检查地址、网络与 SSH 服务状态"
    if "Could not resolve hostname" in text:
        return "无法解析主机名：请检查地址拼写"
    if "Too many authentication failures" in text:
        return "SSH 认证尝试次数过多：目标机可能已拦截该来源，请稍后重试"
    # 兜底：保留 ssh 的最后一行原始输出，至少让用户看到真实原因。
    lines = [line.strip() for line in (result.output or "").splitlines() if line.strip()]
    tail = lines[-1] if lines else ""
    if tail and not tail.startswith("$ "):
        return f"SSH 连接失败：{tail[:200]}"
    return result.error or "SSH 连接失败"


def _fingerprint_of(public_key_b64: str) -> str:
    if not shutil.which("ssh-keygen"):
        return ""
    tmp = Path(tempfile.mkdtemp(prefix="autodeploy-fp-"))
    try:
        key_file = tmp / "host.pub"
        key_file.write_text(public_key_b64 + "\n", encoding="utf-8")
        result = run_command(["ssh-keygen", "-l", "-f", str(key_file)], timeout=15)
        if not result.ok:
            return ""
        # 形如 "2048 SHA256:abc... comment (RSA)"
        parts = result.output.split()
        for part in parts:
            if part.startswith("SHA256:"):
                return part
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parse_probe_output(text: str) -> dict[str, str]:
    """解析 probe 脚本的分段输出。缺失段返回空串，不报错。"""
    sections: dict[str, str] = {}
    current = "uname"
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line == "---RSYNC---":
            current = "rsync"
            continue
        if line == "---DOCKER---":
            current = "docker"
            continue
        if line == "---COMPOSE---":
            current = "compose"
            continue
        if not line or line == "SSH_READY":
            continue
        if not sections.get(current):
            sections[current] = line
    return sections


def _remote_path(value: str) -> str:
    """rsync 远端路径中的空格等字符必须转义，否则会被拆成多个参数。"""
    text = str(value)
    return shlex.quote(text) if any(ch in text for ch in " \t'\"\\$`") else text


def transport_for_host(host: Mapping[str, Any], *, credential: Mapping[str, Any] | None,
                       work_dir: Path | None = None) -> SshTransport:
    """按 hosts 行与凭据行构造传输通道。

    凭据缺失时抛 RemoteError，由调用方转成面向用户的提示。
    """
    if not credential or credential.get("kind") != "ssh_key":
        raise RemoteError(
            f"主机「{host.get('name') or host.get('address')}」未绑定 SSH 私钥凭据，"
            "请在主机配置中选择一个 ssh_key 类型的凭据"
        )
    secret = str(credential.get("secret") or "")
    if not secret:
        raise RemoteError("绑定的 SSH 凭据没有私钥内容，请重新填写")
    return SshTransport(
        address=str(host.get("address") or ""),
        port=int(host.get("port") or 22),
        username=str(host.get("username") or ""),
        private_key=secret,
        passphrase=str(credential.get("passphrase") or ""),
        known_hosts=(Path(work_dir) / "known_hosts") if work_dir else None,
        expected_host_key=str(host.get("host_key") or ""),
        work_dir=work_dir,
    )
