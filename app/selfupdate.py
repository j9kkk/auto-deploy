"""内置自我更新：检查、下载、备份、替换与延迟重启。

设计要点：

* **后台执行**：更新由线程执行，HTTP 请求立刻返回。原因是最后一步会重启
  服务进程——同步等待的话，重启发生时 HTTP 响应根本来不及送达浏览器。
* **状态落盘**：进度写入 ``DATA_DIR/self-update-state.json``。重启发生时
  本进程随之消失，下次启动依据该文件把「重启中」收尾为「已完成」，
  界面才有可信的最终结果。
* **先备份后替换**：安装目录的 ``app/`` ``web/`` 等先复制到
  ``DATA_DIR/self-update-backups/<时间戳>/``，保留最近 3 份，支持一键回滚。
* **重启策略**：优先使用 sudoers 已放行的固定命令延迟重启（部署记录先
  落库，重启发生在本服务 cgroup 之外）；不可用时退回直接重启；两者都
  不可用（如开发环境无 systemd）则只更新文件并提示手动重启。
* **更新源**：默认为本项目的 GitHub 仓库，可通过设置 ``update_repo`` 指向
  镜像或私有副本；下载复用 gitops 的拉取通道，自动继承代理设置。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import config
from .executor import run_command

UPDATE_REPO_DEFAULT = "https://github.com/j9kkk/git-deploy.git"
STATE_FILE_NAME = "self-update-state.json"
BACKUP_DIR_NAME = "self-update-backups"
CHECK_TTL_SECONDS = 600
KEEP_BACKUPS = 3

# 与部署脚本同一套产物：这些路径构成一次完整的程序更新。
UPDATE_ITEMS = ("app", "web", "requirements.txt", "run.sh")

ACTIVE_STAGES = ("queued", "checking", "downloading", "backing_up", "applying", "dependencies", "restarting")


def compare_versions(a: str, b: str) -> int:
    """比较两个版本号，返回 -1/0/1。无法解析的段落按字符串比较。"""
    def parts(v: str) -> list[Any]:
        cleaned = (v or "").strip().lstrip("vV")
        chunks: list[Any] = []
        for chunk in cleaned.split("."):
            if chunk.isdigit():
                chunks.append(int(chunk))
            else:
                # 预发布后缀（如 1.2.0-rc1）排在同号正式版之前。
                match = re.match(r"(\d+)(.*)", chunk)
                if match:
                    chunks.append(int(match.group(1)))
                    if match.group(2):
                        chunks.append(match.group(2))
                else:
                    chunks.append(chunk)
        return chunks

    pa, pb = parts(a), parts(b)
    for index in range(max(len(pa), len(pb))):
        va = pa[index] if index < len(pa) else 0
        vb = pb[index] if index < len(pb) else 0
        if va == vb:
            continue
        # int 与 str 混合比较时统一转字符串，避免 TypeError。
        if isinstance(va, int) != isinstance(vb, int):
            va, vb = str(va), str(vb)
        return -1 if va < vb else 1
    return 0


def normalize_tag(tag: str) -> str:
    return (tag or "").strip().lstrip("vV")


@dataclass
class UpdateCheck:
    current: str
    latest: str
    update_available: bool
    checked_at: str
    published_at: str = ""
    notes_url: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "update_available": self.update_available,
            "checked_at": self.checked_at,
            "published_at": self.published_at,
            "notes_url": self.notes_url,
            "error": self.error,
            "ok": not self.error,
        }


class SelfUpdateManager:
    """内置自我更新的状态机与执行器。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._check_cache: tuple[float, UpdateCheck] | None = None

    # ------------------------------------------------------------- 状态
    @property
    def state_path(self) -> Path:
        return config.DATA_DIR / STATE_FILE_NAME

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("stage", "idle")
                data.setdefault("log", [])
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"stage": "idle", "log": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _log(self, state: dict[str, Any], message: str) -> None:
        state.setdefault("log", []).append(f"[{time.strftime('%H:%M:%S')}] {message}")
        state["log"] = state["log"][-200:]

    @property
    def backups_dir(self) -> Path:
        return config.DATA_DIR / BACKUP_DIR_NAME

    def latest_backup(self) -> Path | None:
        if not self.backups_dir.exists():
            return None
        entries = sorted(
            (p for p in self.backups_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        return entries[0] if entries else None

    # ------------------------------------------------------------- 检查
    def check(self, *, force: bool = False) -> UpdateCheck:
        """查询最新版本。结果缓存 10 分钟，避免频繁请求 GitHub API。"""
        with self._lock:
            if not force and self._check_cache is not None:
                cached_at, cached = self._check_cache
                if time.monotonic() - cached_at < CHECK_TTL_SECONDS:
                    return cached

        result = self._fetch_latest()
        self._check_cache = (time.monotonic(), result)
        return result

    def _repo_slug(self) -> tuple[str, str]:
        from urllib.parse import urlparse

        repo = (config.load_settings().update_repo or UPDATE_REPO_DEFAULT).strip().rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        try:
            path = urlparse(repo).path.strip("/")
            owner, name = path.split("/", 1)
            return owner, name
        except (ValueError, AttributeError):
            return "j9kkk", "git-deploy"

    def _fetch_latest(self) -> UpdateCheck:
        from .schedule import iso, utcnow

        checked_at = iso(utcnow()) or ""
        current = __version__
        settings = config.load_settings()
        repo = (settings.update_repo or UPDATE_REPO_DEFAULT).strip()

        # GitHub 仓库优先用 Releases API；失败（限流/内网镜像）则回退到
        # git ls-remote --tags，两者都不通才报错。
        if "github.com" in repo:
            owner, name = self._repo_slug()
            api = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
            request = urllib.request.Request(
                api, headers={"Accept": "application/vnd.github+json", "User-Agent": "AutoDeploy"}
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.load(response)
                latest = str(payload.get("tag_name") or "").strip()
                if latest:
                    return UpdateCheck(
                        current=current, latest=latest,
                        update_available=compare_versions(latest, current) > 0,
                        checked_at=checked_at,
                        published_at=str(payload.get("published_at") or ""),
                        notes_url=str(payload.get("html_url") or ""),
                    )
            except (urllib.error.URLError, json.JSONDecodeError, OSError):
                pass  # 回退到 git 协议查询

        # git ls-remote --tags：不依赖 API，且对镜像/私有副本同样有效。
        proxy = config.proxy_env(settings)
        env = {**os.environ, **proxy}
        result = run_command(
            ["git", "ls-remote", "--tags", "--", repo],
            env=env, timeout=30,
        )
        if not result.ok:
            return UpdateCheck(
                current=current, latest="", update_available=False,
                checked_at=checked_at,
                error=result.error or "无法查询最新版本，请检查网络或代理设置",
            )
        candidates = []
        for line in result.output.splitlines():
            ref = line.split("refs/tags/", 1)[-1].strip()
            if not ref or "^{}" in line:
                continue
            normalized = normalize_tag(ref)
            if re.fullmatch(r"\d+(\.\d+)*", normalized):
                candidates.append((normalized, ref))
        if not candidates:
            return UpdateCheck(current=current, latest="", update_available=False,
                               checked_at=checked_at, error="仓库中没有可识别的版本 tag")
        # 保留原始 tag 名（含 v 前缀）用于拉取；归一化值只用于比较。
        # 排序键必须是可全序比较的元组：compare_versions 只能两两比较，
        # 用它当 key 会让所有非零版本并列（实测曾因此选错目标版本）。
        def sort_key(pair: tuple[str, str]) -> tuple[int, ...]:
            return tuple(int(part) for part in pair[0].split("."))

        latest = max(candidates, key=sort_key)[1]
        return UpdateCheck(
            current=current, latest=latest,
            update_available=compare_versions(latest, current) > 0,
            checked_at=checked_at,
            notes_url=f"{repo}/releases/tag/{latest}",
        )

    # ------------------------------------------------------------- 执行
    def start(self, target_version: str | None = None) -> dict[str, Any]:
        """启动一次自我更新；立即返回，进度通过 :meth:`state` 轮询。"""
        with self._lock:
            state = self.state()
            if state.get("stage") in ACTIVE_STAGES:
                raise RuntimeError("已有一次更新正在进行")
            if self.store.runs.count_active() > 0:
                raise RuntimeError("仍有部署任务在运行，更新会重启服务将其打断，请稍后再试")

            root = config.ROOT_DIR
            if not os.access(root, os.W_OK):
                raise RuntimeError(f"安装目录不可写: {root}")
            # 源码目录（含 .git）里可能有未提交的开发工作，被覆盖就找不回来了；
            # systemd 安装（install.sh）不含 .git，不受影响。
            git_dir = root / ".git"
            if git_dir.exists():
                check = run_command(
                    ["git", "status", "--porcelain"], cwd=root, timeout=30,
                )
                if check.ok and check.output.strip():
                    raise RuntimeError(
                        "当前是源码目录且有未提交的改动，自更新会覆盖它们。"
                        "请先提交或暂存（git stash），或改用任务方式更新。"
                    )
            for item in UPDATE_ITEMS:
                if not (Path(root) / item).exists():
                    raise RuntimeError(f"安装目录缺少 {item}，不像一次完整安装，已中止")

            target = normalize_tag(target_version or "")
            if not target:
                check = self.check(force=True)
                if check.error:
                    raise RuntimeError(f"无法获取最新版本：{check.error}")
                if not check.update_available:
                    raise RuntimeError(f"已是最新版本（{check.current}）")
                target = check.latest

            state = {
                "stage": "queued",
                "target_version": target,
                "previous_version": __version__,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "log": [],
                "error": "",
                "backup": "",
            }
            self._save_state(state)
            thread = threading.Thread(
                target=self._run, args=(state,), name="self-update", daemon=True
            )
            self._thread = thread
            thread.start()
            return state

    def _set_stage(self, state: dict[str, Any], stage: str) -> None:
        state["stage"] = stage
        self._save_state(state)

    def _run(self, state: dict[str, Any]) -> None:
        """更新线程主体。任何一步失败都会自动回滚并记录。"""
        settings = config.load_settings()
        target = str(state.get("target_version") or "")
        root = Path(config.ROOT_DIR)
        def log(message: str) -> None:
            self._log(state, message)
            self._save_state(state)

        try:
            self._set_stage(state, "downloading")
            log(f"下载 {target}（源: {settings.update_repo}）")
            tmp_root = config.TMP_DIR / f"selfupdate-{int(time.time())}"
            tmp_root.mkdir(parents=True, exist_ok=True)
            try:
                self._download(target, tmp_root, settings, log, state)
                source = tmp_root / "src"

                # 下载内容自检：缺关键文件说明 tag 内容不对，绝不动安装目录。
                missing = [item for item in UPDATE_ITEMS if not (source / item).exists()]
                if missing:
                    raise RuntimeError(f"下载内容不完整，缺少: {', '.join(missing)}")

                self._set_stage(state, "backing_up")
                backup = self._backup(root, state, log)

                self._set_stage(state, "applying")
                for item in UPDATE_ITEMS:
                    src = source / item
                    dst = root / item
                    if src.is_dir():
                        if dst.exists():
                            shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst)
                    log(f"已更新 {item}")

                self._set_stage(state, "dependencies")
                log("更新 Python 依赖…")
                pip = run_command(
                    [sys_executable(), "-m", "pip", "install", "-q",
                     "-r", str(root / "requirements.txt")],
                    timeout=600,
                    env={**os.environ, **config.proxy_env(settings, for_scripts=True)},
                )
                if not pip.ok:
                    raise RuntimeError(f"依赖安装失败: {pip.error}（已回滚）")
                log("依赖更新完成")

                self._finish(state, root, backup, log)
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - 更新失败必须回滚并可见
            self._log(state, f"! 更新失败: {exc}")
            self._restore_backup(root, state)
            state["stage"] = "failed"
            state["error"] = str(exc)
            self._save_state(state)

    def _download(self, target: str, tmp_root: Path, settings: Any, log: Callable[[str], None], state: dict[str, Any]):
        """按 tag 拉取代码，复用任务同款拉取通道（自动继承代理）。"""
        from . import gitops

        source = tmp_root / "src"
        try:
            gitops.sync_checkout(
                repo_url=settings.update_repo,
                branch=target,  # git fetch 对 tag 名同样有效
                workspace=source,
                depth=1,
                tmp_dir=tmp_root / "creds",
                timeout=int(settings.git_timeout_seconds),
                log=lambda message: self._log(state, message),
                proxy=config.proxy_env(settings),
                home=tmp_root / "creds",
            )
        except gitops.GitError:
            # 用户可能填了不带 v 前缀的版本号（1.2.1），而仓库的 tag 是 v1.2.1。
            if not target.lower().startswith("v"):
                self._log(state, f"未找到 {target}，尝试 v{target} …")
                shutil.rmtree(source, ignore_errors=True)
                gitops.sync_checkout(
                    repo_url=settings.update_repo,
                    branch=f"v{target}",
                    workspace=source,
                    depth=1,
                    tmp_dir=tmp_root / "creds",
                    timeout=int(settings.git_timeout_seconds),
                    log=lambda message: self._log(state, message),
                    proxy=config.proxy_env(settings),
                    home=tmp_root / "creds",
                )
            else:
                raise
        log("已检出目标版本")

    def _backup(self, root: Path, state: dict[str, Any], log: Callable[[str], None]) -> Path:
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = self.backups_dir / stamp
        backup.mkdir(parents=True, exist_ok=True)
        for item in UPDATE_ITEMS:
            src = root / item
            if not src.exists():
                continue
            dst = backup / item
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        # 只保留最近 N 份备份。
        entries = sorted((p for p in self.backups_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        for old in entries[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        state["backup"] = str(backup)
        log(f"已备份当前版本到 {backup.name}")
        return backup

    def _finish(self, state: dict[str, Any], root: Path, backup: Path, log: Callable[[str], None]) -> None:
        """重启策略与收尾。"""
        strategy, service = self._restart_plan()
        # CI/容器里常有 systemd-run 但 sudo 无免密凭据；sudo -n 会立即失败，
        # 不会挂起。真正要防的是策略可用却卡住：给重启命令较短的超时。
        self._set_stage(state, "restarting")
        if strategy == "deferred":
            log(f"已调度延迟重启（{service}，5 秒后由 systemd 执行）")
            result = run_command(
                ["sudo", "-n", "/usr/bin/systemd-run", "--collect", "--on-active=5s",
                 "/usr/bin/systemctl", "restart", service],
                timeout=20,
            )
            if result.ok:
                state["restart"] = "deferred"
                log("服务即将自动重启；页面会自动重连")
                self._save_state(state)
                return
            self._log(state, "! 延迟重启调度失败，尝试直接重启")
        if strategy in ("deferred", "direct"):
            result = run_command(
                ["sudo", "-n", "/usr/bin/systemctl", "restart", service],
                timeout=30,
            )
            if result.ok:
                state["restart"] = "direct"
                self._log(state, "已请求重启；本次运行可能显示为中断，属预期现象")
                self._save_state(state)
                return
            self._log(state, "! 直接重启失败，需要手动重启")
        state["restart"] = "manual"
        self._log(state, "更新文件已就位：当前环境无 systemd，请手动重启服务以加载新版本")
        state["stage"] = "done"
        state["version"] = __version__
        self._save_state(state)

    def _restart_plan(self) -> tuple[str, str]:
        service = os.environ.get("AUTODEPLOY_SERVICE_NAME", "autodeploy")
        from .executor import command_exists

        if command_exists("systemd-run") and command_exists("systemctl") and command_exists("sudo"):
            return "deferred", service
        if command_exists("systemctl") and command_exists("sudo"):
            return "direct", service
        return "manual", service

    def _restore_backup(self, root: Path, state: dict[str, Any]) -> None:
        """更新失败时自动回滚到更新前状态。"""
        backup = state.get("backup")
        if not backup or not Path(backup).exists():
            return
        self._log(state, "正在回滚到更新前版本…")
        for item in UPDATE_ITEMS:
            src = Path(backup) / item
            dst = root / item
            if not src.exists():
                continue
            if dst.exists() and dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        self._log(state, "已回滚完成")

    def rollback(self) -> dict[str, Any]:
        """手动回滚到最近一次备份并重启。"""
        with self._lock:
            state = self.state()
            if state.get("stage") in ACTIVE_STAGES:
                raise RuntimeError("更新正在进行，无法回滚")
            backup = self.latest_backup()
            if backup is None:
                raise RuntimeError("没有可用的备份")
            root = Path(config.ROOT_DIR)
            state["backup"] = str(backup)
            state["stage"] = "backing_up"
            self._log(state, f"手动回滚：恢复备份 {backup.name}")
            self._restore_backup(root, state)
            state["stage"] = "restarting"
            strategy, service = self._restart_plan()
            if strategy in ("deferred", "direct"):
                args = (["sudo", "-n", "/usr/bin/systemd-run", "--collect", "--on-active=5s",
                         "/usr/bin/systemctl", "restart", service]
                        if strategy == "deferred"
                        else ["sudo", "-n", "/usr/bin/systemctl", "restart", service])
                result = run_command(args, timeout=30)
                if result.ok:
                    state["restart"] = strategy
                    self._save_state(state)
                    return {"ok": True, "message": f"已回滚到 {backup.name}，服务即将重启"}
            state["stage"] = "done"
            state["restart"] = "manual"
            self._log(state, "已回滚文件；当前环境无 systemd，请手动重启服务")
            self._save_state(state)
            return {"ok": True, "message": "已回滚文件，请手动重启服务"}

    def reconcile_on_startup(self) -> None:
        """启动收尾：把上次「重启中」的状态落定为完成/中断。"""
        state = self.state()
        if state.get("stage") == "restarting":
            state["stage"] = "done"
            state["version"] = __version__
            self._log(state, f"服务已在新版本 {__version__} 上运行，自我更新完成")
            self._save_state(state)


def sys_executable() -> str:
    """当前解释器：systemd 安装下即安装目录里的 .venv/bin/python。"""
    import sys

    return sys.executable
