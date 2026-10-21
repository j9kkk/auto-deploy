"""执行目标抽象：命令在哪跑、文件在哪。

部署链路原先直接假设「本机」。引入远程主机后，同一个流水线需要能落到另一台
机器上。这里把两件事收敛到一个对象：

* ``run`` / ``run_script`` —— 命令在哪里执行
* ``exists`` / ``swap_symlink`` / ``copy_tree`` / ``bundle`` … —— 文件在哪里

设计取舍
--------
**本机路径一行不改。** ``LocalTarget`` 全部委托既有的
``executor.run_command`` 与 ``deployer`` 里的实现，不复制逻辑；``RemoteTarget``
是并列实现。这样本地部署的行为与回归风险都保持原样——远程能力是新增分支，
不是把安全校验密集的本地代码改成双态。

**git 永远在控制器执行。** 远端 workspace 是 rsync 镜像（不含 ``.git``），
所以仓库凭据不需要出现在目标机上，这也是「零安装」的前提。

**远端 shell 命令逐个参数转义**（``shlex.quote``），拼接前不接受任何未转义
片段——这些值里可能带用户提交的路径。
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import config
from .executor import CommandResult, ProcessHandle, run_command
from .remote import SshTransport

# 远端 staged 目录与 bundle 的排除项：与本地 stage_release 保持一致，
# 避免同一个任务在两种目标下产出不同的发布内容。
STAGE_EXCLUDES = (
    ".git", ".git/*", ".svn", ".hg",
    "__pycache__", "*/__pycache__", "*.pyc",
    "node_modules", "*/node_modules",
    ".venv", "*/.venv", "venv", "*/venv",
    ".autodeploy-cache", "*/.autodeploy-cache",
    "*.log",
)

# 远端 workspace 不同步的信息：.git 由控制器持有，用户不需要在目标机上
# 拿到完整仓库历史（体积可能是几十倍）。
UPLOAD_EXCLUDES = (".git", ".autodeploy-owner", ".autodeploy-cache")


class TargetError(Exception):
    """目标操作失败；消息面向用户（中文）。"""


def q(value: Any) -> str:
    """转义一个值用于远端 shell。"""
    return shlex.quote(str(value))


def _safe_name(value: str) -> str:
    return str(value or "").strip() or "task"


@dataclass
class RemoteLayout:
    """远程主机上的目录布局。"""

    base_dir: str
    workspace: str
    releases_root: str
    current_link: str

    @property
    def releases_parent(self) -> str:
        return str(Path(self.releases_root).parent)


def resolve_remote_layout(host: Mapping[str, Any], task: Mapping[str, Any]) -> RemoteLayout:
    """计算任务在目标主机上的 workspace / releases / current。

    规则与本地一致：任务设置了 ``target_dir`` 时，发布目录放在那里
    （通常是站点根，便于 nginx/systemd 引用 ``current``）；否则落在主机的
    ``base_dir`` 下，按任务 slug 分目录，不同任务的发布历史互不干扰。
    """
    base_dir = str(host.get("base_dir") or "/opt/autodeploy").rstrip("/") or "/opt/autodeploy"
    if not base_dir.startswith("/"):
        raise TargetError(f"主机「{host.get('name') or host.get('address')}」的基础目录不是绝对路径")
    slug = _safe_name(str(task.get("name") or "task"))
    workspace = f"{base_dir}/workspaces/{slug}"

    target_dir = str(task.get("target_dir") or "").strip()
    if target_dir:
        if not target_dir.startswith("/"):
            raise TargetError("任务的目标目录必须是目标主机上的绝对路径")
        root = target_dir.rstrip("/") or "/"
        return RemoteLayout(
            base_dir=base_dir,
            workspace=workspace,
            releases_root=f"{root}/releases" if root != "/" else "/releases",
            current_link=f"{root}/current" if root != "/" else "/current",
        )
    releases_root = f"{base_dir}/releases/{slug}/releases"
    return RemoteLayout(
        base_dir=base_dir,
        workspace=workspace,
        releases_root=releases_root,
        current_link=f"{base_dir}/releases/{slug}/current",
    )


class ExecutionTarget:
    """命令与文件系统的执行位置。"""

    is_remote: bool = False
    label: str = "本机"

    # -- 命令 -------------------------------------------------------------
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Any = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 600,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        label: str | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        raise NotImplementedError

    def run_script(
        self,
        script: str,
        *,
        cwd: Any = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 600,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        label: str | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        raise NotImplementedError

    def which(self, binary: str) -> bool:
        raise NotImplementedError

    def shell(self) -> str:
        raise NotImplementedError

    # -- 文件原语 ---------------------------------------------------------
    def path(self, value: str) -> Any:
        """把字符串路径变成该目标可用的形式（本地为 Path，远端为 str）。"""
        raise NotImplementedError

    def exists(self, target: Any) -> bool:
        raise NotImplementedError

    def is_dir(self, target: Any) -> bool:
        raise NotImplementedError

    def is_symlink(self, target: Any) -> bool:
        raise NotImplementedError

    def readlink(self, target: Any) -> str | None:
        raise NotImplementedError

    def list_dirs(self, root: Any) -> list[str]:
        raise NotImplementedError

    def make_dirs(self, target: Any) -> None:
        raise NotImplementedError

    def remove_tree(self, target: Any) -> None:
        raise NotImplementedError

    def swap_symlink(self, link: Any, destination: Any) -> None:
        raise NotImplementedError

    def copy_tree(self, source: Any, destination: Any, *, excludes: Sequence[str] = ()) -> None:
        raise NotImplementedError

    def bundle(self, source_dir: Any, out_file: Any) -> int:
        raise NotImplementedError

    def size_of_file(self, target: Any) -> int:
        raise NotImplementedError

    def upload(
        self,
        local_dir: Path,
        remote_dir: Any,
        *,
        delete: bool = False,
        excludes: Sequence[str] = (),
        **kwargs: Any,
    ) -> CommandResult:
        raise NotImplementedError

    def download(self, remote_path: Any, local_path: Path, **kwargs: Any) -> CommandResult:
        raise NotImplementedError


class LocalTarget(ExecutionTarget):
    """本机执行：全部委托既有实现，不复制任何逻辑。"""

    is_remote = False
    label = "本机"

    def __init__(self) -> None:
        self._shell: str | None = None

    def shell(self) -> str:
        if self._shell is None:
            self._shell = config.load_settings().shell
        return self._shell

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Any = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 600,
        log: Callable[[str], None] | None = None,
        log_batch: Callable[[list[str]], None] | None = None,
        label: str | None = None,
        handle_out: list[ProcessHandle] | None = None,
        check_cancelled: Callable[[], bool] | None = None,
        kill_grace_seconds: int = 10,
    ) -> CommandResult:
        return run_command(
            args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            log=log,
            log_batch=log_batch,
            label=label,
            handle_out=handle_out,
            check_cancelled=check_cancelled,
            kill_grace_seconds=kill_grace_seconds,
        )

    def run_script(self, script: str, **kwargs: Any) -> CommandResult:
        from .executor import shell_command

        kwargs.setdefault("label", "script")
        cwd = kwargs.pop("cwd", None)
        return self.run(
            shell_command(script, shell=self.shell()),
            cwd=cwd,
            **kwargs,
        )

    def which(self, binary: str) -> bool:
        from .deployer import command_exists

        return command_exists(binary)

    def path(self, value: str) -> Path:
        return Path(value)

    def exists(self, target: Any) -> bool:
        return Path(target).exists()

    def is_dir(self, target: Any) -> bool:
        return Path(target).is_dir()

    def is_symlink(self, target: Any) -> bool:
        return Path(target).is_symlink()

    def readlink(self, target: Any) -> str | None:
        path = Path(target)
        if not path.is_symlink():
            return None
        try:
            return str(path.resolve())
        except OSError:
            return None

    def list_dirs(self, root: Any) -> list[str]:
        base = Path(root)
        if not base.is_dir():
            return []
        entries = [
            entry.name for entry in base.iterdir()
            if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
        ]
        # 时间戳前缀按名字倒序即为最新在前；mtime 作为同秒兜底。
        entries.sort(
            key=lambda name: (name, (base / name).stat().st_mtime),
            reverse=True,
        )
        return entries

    def make_dirs(self, target: Any) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)

    def remove_tree(self, target: Any) -> None:
        from .deployer import cleanup_path

        cleanup_path(Path(target))

    def swap_symlink(self, link: Any, destination: Any) -> None:
        from .deployer import swap_symlink

        swap_symlink(Path(link), Path(destination))

    def copy_tree(self, source: Any, destination: Any, *, excludes: Sequence[str] = ()) -> None:
        """本机目录复制：沿用 deployer 的 rsync 风格排他规则。

        这里用 ``shutil`` 而非 rsync，避免为本机路径新增外部二进制依赖。
        """
        src = Path(source)
        dst = Path(destination)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(*_ignore_tokens(excludes)) if excludes else None
        for entry in src.iterdir():
            target = dst / entry.name
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=True, ignore=ignore, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)

    def bundle(self, source_dir: Any, out_file: Any) -> int:
        from .deployer import create_bundle

        return create_bundle(Path(source_dir), Path(out_file))

    def size_of_file(self, target: Any) -> int:
        return Path(target).stat().st_size

    def upload(self, local_dir: Path, remote_dir: Any, **_kwargs: Any) -> CommandResult:
        # 本机目标无需上传；语义上是「已经在位」。返回成功以保持调用方统一。
        return CommandResult(f"local copy {local_dir}", 0, "", 0)

    def download(self, remote_path: Any, local_path: Path, **_kwargs: Any) -> CommandResult:
        src = Path(remote_path)
        if not src.is_file():
            return CommandResult(str(src), 1, "", 0, error="源文件不存在")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)
        return CommandResult(f"local copy {src}", 0, "", 0)


def _ignore_tokens(patterns: Sequence[str]) -> list[str]:
    """把 rsync 风格排除项转成 shutil 能吃的模式（去掉 */ 前缀与整段 .git/*）。"""
    tokens: list[str] = []
    for pattern in patterns:
        token = pattern.strip()
        if not token:
            continue
        if token.startswith("*/"):
            token = token[2:]
        if token.endswith("/*"):
            token = token[:-2]
        if token and token not in tokens:
            tokens.append(token)
    return tokens


class RemoteTarget(ExecutionTarget):
    """远程执行：命令经 SSH，文件操作编译成远端 shell。"""

    is_remote = True

    def __init__(self, transport: SshTransport, *, label: str | None = None) -> None:
        self.transport = transport
        self.label = label or f"{transport.username}@{transport.address}"

    # -- 命令 -------------------------------------------------------------
    def shell(self) -> str:
        return "/bin/bash"

    def run(self, args: Sequence[str], *, cwd: Any = None, **kwargs: Any) -> CommandResult:
        return self.transport.run(args, cwd=_str_or_none(cwd), **kwargs)

    def run_script(self, script: str, *, cwd: Any = None, **kwargs: Any) -> CommandResult:
        # cwd 与 env 由 transport 统一以 cd / export 前缀拼进脚本，
        # 这里不再重复拼接，避免出现两层 cd。
        return self.transport.run_script(str(script), cwd=_str_or_none(cwd), **kwargs)

    def which(self, binary: str) -> bool:
        result = self.transport.run(
            ["sh", "-c", f"command -v {q(binary)} >/dev/null 2>&1"],
            timeout=20,
        )
        return result.ok

    # -- 文件原语 ---------------------------------------------------------
    def path(self, value: str) -> str:
        return str(value)

    def _test(self, expression: str) -> bool:
        # 表达式整体交给 shell 求值：按空格拆成 argv 会让含空格的路径出错。
        result = self.transport.run(["sh", "-c", f"test {expression}"], timeout=20)
        return result.ok

    def exists(self, target: Any) -> bool:
        return self._test(f"-e {q(target)}")

    def is_dir(self, target: Any) -> bool:
        return self._test(f"-d {q(target)}")

    def is_symlink(self, target: Any) -> bool:
        return self._test(f"-L {q(target)}")

    def readlink(self, target: Any) -> str | None:
        result = self.transport.run(["readlink", "-f", str(target)], timeout=20)
        if not result.ok:
            return None
        value = result.output.strip()
        return value or None

    def list_dirs(self, root: Any) -> list[str]:
        """列出发布目录下的子目录名，按名字倒序（时间戳前缀即最新在前）。

        不用 ``find -printf``：busybox（alpine 目标机常见）不支持它，会退化成
        把 find 的帮助文本当成目录名。输出用哨兵行包裹，这样 ssh 自身打印的
        提示（例如首次连接的主机告警）不会混进解析结果。
        """
        marker = "__AD_LS_DIRS__"
        script = (
            f"echo {marker}; "
            f"if [ -d {q(root)} ]; then cd {q(root)} && for entry in *; do "
            f"[ -d \"$entry\" ] && [ ! -L \"$entry\" ] && printf '%s\\n' \"$entry\"; "
            f"done; fi; echo {marker}"
        )
        result = self.transport.run_script(script, timeout=60)
        if not result.ok:
            return []
        lines = result.output.splitlines()
        try:
            start = lines.index(marker)
            end = lines.index(marker, start + 1)
            body = lines[start + 1:end]
        except ValueError:
            # 没找到哨兵（旧目标机或输出被截断）：退回逐行过滤，宁少不错。
            body = [line for line in lines if "/" not in line and not line.startswith(("-", "Warn", "bash"))]
        names = [line.strip() for line in body if line.strip() and "/" not in line]
        names.sort(reverse=True)
        return names

    def make_dirs(self, target: Any) -> None:
        result = self.transport.run(["mkdir", "-p", str(target)], timeout=60)
        if not result.ok:
            raise TargetError(f"在目标主机创建目录失败: {target}")

    def remove_tree(self, target: Any) -> None:
        self.transport.run(["rm", "-rf", "--", str(target)], timeout=300)

    def swap_symlink(self, link: Any, destination: Any) -> None:
        """原子换链：先建临时软链，再 mv -Tf 覆盖。

        ``mv -T`` 把目标当作普通文件替换而不是移进目录，否则当
        ``current`` 恰好是指向目录的软链时，mv 会把新链塞进目标目录里。
        """
        link_str = str(link)
        tmp = f"{link_str}.new-$$"
        script = (
            f"mkdir -p {q(str(Path(link_str).parent))} && "
            f"ln -sfn {q(str(destination))} {q(tmp)} && "
            f"mv -Tf {q(tmp)} {q(link_str)}"
        )
        result = self.transport.run_script(script, timeout=60)
        if not result.ok:
            raise TargetError(f"在目标主机切换软链失败: {result.error or result.output}".strip())

    def copy_tree(self, source: Any, destination: Any, *, excludes: Sequence[str] = ()) -> None:
        """同主机内复制（不过网络）：远端 rsync 或 cp -a。"""
        args = ["rsync", "-a", "--delete"]
        for pattern in excludes:
            args += ["--exclude", pattern]
        args += [str(source).rstrip("/") + "/", str(destination)]
        result = self.transport.run(args, timeout=1800)
        if result.ok:
            return
        # 目标机没装 rsync 时退回 cp：排除项会失效，但对发布目录通常无影响
        # （源码树里本来就不该有 .git/node_modules 之外的大件）。
        fallback = f"rm -rf {q(destination)} && mkdir -p {q(destination)} && cp -a {q(source)}/. {q(destination)}/"
        result = self.transport.run_script(fallback, timeout=1800)
        if not result.ok:
            raise TargetError(f"在目标主机暂存发布目录失败: {result.error or result.output}".strip())

    def bundle(self, source_dir: Any, out_file: Any) -> int:
        script = (
            f"mkdir -p {q(str(Path(str(out_file)).parent))} && "
            f"tar czf {q(str(out_file))}.part -C {q(str(Path(str(source_dir)).parent))} "
            f"{q(str(Path(str(source_dir)).name))} && "
            f"mv -f {q(str(out_file))}.part {q(str(out_file))} && "
            f"stat -c %s {q(str(out_file))}"
        )
        result = self.transport.run_script(script, timeout=1800)
        if not result.ok:
            raise TargetError(f"在目标主机打包失败: {result.error or result.output}".strip())
        try:
            return int(result.output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    def size_of_file(self, target: Any) -> int:
        result = self.transport.run(["stat", "-c", "%s", str(target)], timeout=30)
        if not result.ok:
            return 0
        try:
            return int(result.output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    def upload(
        self,
        local_dir: Path,
        remote_dir: Any,
        *,
        delete: bool = False,
        excludes: Sequence[str] = (),
        **kwargs: Any,
    ) -> CommandResult:
        # 远端目录先建好，rsync 不会替我们建多层父目录。
        self.transport.run(["mkdir", "-p", str(remote_dir)], timeout=60)
        return self.transport.rsync_up(
            local_dir, str(remote_dir), delete=delete, excludes=excludes, **kwargs
        )

    def download(self, remote_path: Any, local_path: Path, **kwargs: Any) -> CommandResult:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        return self.transport.rsync_down(str(remote_path), str(local_path), **kwargs)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def build_target(host: Mapping[str, Any] | None, credential: Mapping[str, Any] | None,
                 *, work_dir: Path | None = None) -> ExecutionTarget:
    """按任务的目标主机构造执行目标；无主机即本机。

    远程但凭据缺失/类型不对时抛 TargetError——宁可运行一开始就失败，
    也不要在中途某个步骤莫名退回本机执行。
    """
    if not host:
        return LocalTarget()
    if not credential or credential.get("kind") != "ssh_key":
        raise TargetError(
            f"主机「{host.get('name') or host.get('address')}」未绑定 SSH 私钥凭据；"
            "请先在「主机」页选择 ssh_key 类型的凭据"
        )
    secret = str(credential.get("secret") or "")
    if not secret:
        raise TargetError("主机绑定的 SSH 凭据没有私钥内容，请重新填写")
    transport = SshTransport(
        address=str(host.get("address") or ""),
        port=int(host.get("port") or 22),
        username=str(host.get("username") or ""),
        private_key=secret,
        passphrase=str(credential.get("passphrase") or ""),
        known_hosts=(Path(work_dir) / "known_hosts") if work_dir else None,
        expected_host_key=str(host.get("host_key") or ""),
        work_dir=work_dir,
    )
    return RemoteTarget(transport, label=f"{transport.username}@{transport.address}")
