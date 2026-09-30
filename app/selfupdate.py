"""受控自更新：只读验证 systemd，备份代码，启动后确认版本。"""

from __future__ import annotations

import ast
import uuid
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import config
from .executor import run_command

UPDATE_REPO_DEFAULT = "https://github.com/j9kkk/auto-deploy.git"
STATE_FILE_NAME = "self-update-state.json"
HISTORY_FILE_NAME = "self-update-history.json"
BACKUP_DIR_NAME = "self-update-backups"
CHECK_TTL_SECONDS = 600
RESTART_TIMEOUT = 120
STATE_SCHEMA_VERSION = 2
HISTORY_LIMIT = 20
PROCESS_BOOT_ID = uuid.uuid4().hex

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


def _tmp_root() -> Path:
    """A writable directory for throwaway files, even before the first deploy."""
    try:
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        return config.TMP_DIR
    except OSError:
        return Path(tempfile.gettempdir())


def _dir_size(path: Path) -> int:
    """Total bytes of a directory tree, ignoring anything unreadable."""
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _proxy_bypassed(host: str, no_proxy: str) -> bool:
    """``no_proxy`` 是否覆盖该主机。

    ``urllib`` 的 ``proxy_bypass`` 只读进程环境变量，而这里的代理来自应用设置，
    因此必须自己按 ``no_proxy`` 判断，否则内网更新源会被强行推过外网代理。
    """
    return config.no_proxy_bypassed(host, no_proxy)


def urlopen_with_proxy(
    request: urllib.request.Request,
    proxy: dict[str, str] | None,
    *,
    timeout: float,
) -> Any:
    """按应用设置打开 URL：显式使用配置的代理，并尊重 no_proxy。

    默认的 ``urllib.request.urlopen`` 只看进程环境变量，因此「设置页里配好的
    代理」对更新检查完全不起作用——在必须走代理才能访问 GitHub 的服务器上，
    Releases API 这条主路径会直接失败。这里把代理显式装进 opener。
    """
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    url = proxy.get("https_proxy") or proxy.get("http_proxy") or ""
    no_proxy = proxy.get("no_proxy") or proxy.get("NO_PROXY") or ""
    host = urllib.parse.urlsplit(request.full_url).hostname or ""
    if not url or _proxy_bypassed(host, no_proxy):
        return urllib.request.urlopen(request, timeout=timeout)
    # socks5 代理 urllib 原生不支持，会直接报错并回退到 git 协议查询；
    # 这里不做特殊处理，避免引入标准库之外的依赖。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    return opener.open(request, timeout=timeout)


class SelfUpdateManager:
    """内置自我更新的状态机与执行器。"""

    def __init__(self, store: Any, gate=None) -> None:
        self.store = store
        self._lock = gate or threading.RLock()
        self._thread: threading.Thread | None = None
        self._check_cache: tuple[float, UpdateCheck] | None = None

    def _urlopen(self, request: urllib.request.Request, proxy: dict[str, str] | None, *, timeout: float) -> Any:
        return urlopen_with_proxy(request, proxy, timeout=timeout)

    # ------------------------------------------------------------- 状态
    @property
    def state_path(self) -> Path:
        return config.DATA_DIR / STATE_FILE_NAME

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._read_state()

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("stage", "idle")
                data.setdefault("log", [])
                changed = False
                if "schema_version" not in data:
                    data["schema_version"] = STATE_SCHEMA_VERSION
                    changed = True
                    # v1.3.6 已有进程确认字段，但尚未写入格式版本。
                    modern = any(key in data for key in ("operation_id", "before_boot_id", "expected_version"))
                    if not modern and data["stage"] != "idle":
                        data["legacy_stage"] = data["stage"]
                        data["notice"] = "此记录来自旧版更新机制，缺少进程确认信息；当前运行版本不代表该次操作已确认成功。可继续检查更新，原日志已保留。"
                        if data["stage"] in (*ACTIVE_STAGES, "done"):
                            data["stage"] = "unverified"
                if data.get("schema_version") != STATE_SCHEMA_VERSION:
                    return {**data, "stage": "unverified", "notice": "无法识别更新记录格式，未确认操作成功；原记录保持不变。"}
                if data.get("stage") == "restarting":
                    deadline = data.get("restart_deadline")
                    valid_deadline = (type(deadline) in (int, float) and math.isfinite(deadline) and deadline > 0)
                    error = ""
                    if not valid_deadline:
                        error = "重启确认记录缺少有效期限，未确认操作成功；请检查服务"
                    elif time.time() > deadline:
                        error = "重启确认超时；未确认目标版本运行，请检查服务"
                    if error:
                        data.update(stage="failed", error=error)
                        self._log(data, error)
                        changed = True
                if changed:
                    self._save_state(data)
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"stage": "idle", "log": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._write_state(state)

    def _write_state(self, state: dict[str, Any]) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _log(self, state: dict[str, Any], message: str) -> None:
        state.setdefault("log", []).append(f"[{time.strftime('%H:%M:%S')}] {message}")
        state["log"] = state["log"][-200:]

    # ------------------------------------------------------------- 历史
    @property
    def history_path(self) -> Path:
        return config.DATA_DIR / HISTORY_FILE_NAME

    def history(self) -> list[dict[str, Any]]:
        """历次更新/回滚记录（新→旧）。

        每次操作结束（成功、失败或确认）时落一条，供界面展示「每次升级的日志」
        并按版本回滚。记录只保留可展示字段与日志，不含路径或代币。
        """
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [item for item in entries if isinstance(item, dict)]

    def _append_history(self, state: dict[str, Any]) -> None:
        """把一次操作的结果追加到历史；同一 operation_id 只保留最新一条。"""
        operation_id = str(state.get("operation_id") or "")
        if not operation_id:
            return
        entry = {
            "operation_id": operation_id,
            "operation": str(state.get("operation") or "update"),
            "stage": str(state.get("stage") or ""),
            "target_version": normalize_tag(str(state.get("target_version") or "")),
            "previous_version": normalize_tag(str(state.get("previous_version") or "")),
            "current_version": normalize_tag(str(state.get("version") or __version__)),
            "backup_version": normalize_tag(str(state.get("backup_version") or state.get("previous_version") or "")),
            "started_at": state.get("started_at") or 0,
            "finished_at": time.time(),
            "error": str(state.get("error") or ""),
            "log": list(state.get("log") or [])[-200:],
        }
        entries = [item for item in self.history() if item.get("operation_id") != operation_id]
        entries.insert(0, entry)
        payload = {"entries": entries[:HISTORY_LIMIT]}
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.history_path)

    @property
    def backups_dir(self) -> Path:
        return config.DATA_DIR / BACKUP_DIR_NAME

    def _check_backup_root(self) -> None:
        if self.backups_dir.is_symlink() or (self.backups_dir.exists() and not self.backups_dir.is_dir()):
            raise RuntimeError("备份目录不安全或类型错误，尚未修改程序文件")

    def _validate_backup(self, backup: Path) -> str:
        self._check_backup_root()
        if (backup.is_symlink() or not backup.is_dir()
                or backup.resolve().parent != self.backups_dir.resolve()):
            raise RuntimeError("备份路径不安全")
        marker = backup / "complete.json"
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError("备份缺少有效完成标记，无法确认备份完整性")
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            version = metadata.get("version") if isinstance(metadata, dict) else None
            if not isinstance(version, str) or not version.strip():
                raise ValueError("version")
            return self._validate_tree(backup, version)
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            raise RuntimeError("备份完成标记或程序内容无效") from exc

    def _available_backup(self) -> tuple[Path | None, str]:
        if self.backups_dir.is_symlink():
            return None, "备份目录是软链，已禁用自动回滚；请人工核查备份。"
        if not self.backups_dir.exists():
            return None, "尚无可用的代码备份。"
        try:
            entries = sorted(self.backups_dir.iterdir(), key=lambda p: p.lstat().st_mtime_ns, reverse=True)
        except OSError:
            return None, "无法读取备份目录，请检查访问权限。"
        available = None
        legacy = invalid = 0
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    invalid += 1
                    continue
                marker = entry / "complete.json"
                if not marker.exists() and not marker.is_symlink():
                    legacy += 1
                    continue
                self._validate_backup(entry)
                if available is None:
                    available = entry
            except (RuntimeError, OSError):
                invalid += 1
        notices = []
        if legacy:
            notices.append(f"发现 {legacy} 份旧版或未完成备份，缺少完成标记，无法确认完整性；已保留但不用于自动回滚，请人工核查后恢复。")
        if invalid:
            notices.append(f"另有 {invalid} 份备份未通过校验，已跳过并保留。")
        if available is not None:
            notices.append("可回滚到最近通过结构及版本校验的代码备份；数据库和 Python 依赖不会回滚。")
        elif not notices:
            notices.append("尚无可用的代码备份。")
        return available, " ".join(notices)

    def latest_backup(self) -> Path | None:
        return self._available_backup()[0]

    def backup_status(self) -> dict[str, Any]:
        backup, notice = self._available_backup()
        return {"can_rollback": backup is not None, "backup_notice": notice}

    def _valid_backups(self) -> list[tuple[Path, str, float]]:
        """所有通过校验的备份，按时间从新到旧：``(路径, 版本, mtime)``。

        与 ``_available_backup`` 用同一套校验（完成标记 + 结构 + 版本），
        因此历史列表里的回滚目标和「回滚上一版本」可信度一致。
        """
        if self.backups_dir.is_symlink() or not self.backups_dir.is_dir():
            return []
        try:
            entries = sorted(
                self.backups_dir.iterdir(), key=lambda p: p.lstat().st_mtime_ns, reverse=True
            )
        except OSError:
            return []
        found: list[tuple[Path, str, float]] = []
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                marker = entry / "complete.json"
                if not marker.exists() or marker.is_symlink():
                    continue
                version = self._validate_backup(entry)
                found.append((entry, version, entry.lstat().st_mtime))
            except (RuntimeError, OSError):
                continue
        return found

    def backup_for_version(self, version: str) -> Path | None:
        """按版本号查一个可回滚的备份；查不到返回 None。"""
        wanted = normalize_tag(version)
        for path, backed_up, _ in self._valid_backups():
            if normalize_tag(backed_up) == wanted:
                return path
        return None

    def backups(self) -> list[dict[str, Any]]:
        """界面用的备份清单（不含路径，避免把服务器目录结构暴露成接口契约）。"""
        return [
            {"version": normalize_tag(version), "created_at": created, "size": _dir_size(path)}
            for path, version, created in self._valid_backups()
        ]

    # ------------------------------------------------------------- 检查
    def check(self, *, force: bool = False) -> UpdateCheck:
        """查询最新版本。成功结果缓存 10 分钟，避免频繁请求 GitHub API。

        **失败结果不缓存**：把一次网络抖动缓存十分钟，会让界面持续显示
        「检查失败」，用户点多少次都没用，只能等缓存过期。失败必须即时可见。
        """
        with self._lock:
            if not force and self._check_cache is not None:
                cached_at, cached = self._check_cache
                if time.monotonic() - cached_at < CHECK_TTL_SECONDS:
                    return cached

        result = self._fetch_latest()
        self._check_cache = None if result.error else (time.monotonic(), result)
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
            return "j9kkk", "auto-deploy"

    def _fetch_latest(self) -> UpdateCheck:
        from .schedule import iso, utcnow

        checked_at = iso(utcnow()) or ""
        current = __version__
        settings = config.load_settings()
        repo = (settings.update_repo or UPDATE_REPO_DEFAULT).strip()
        proxy = config.proxy_env(settings)

        # GitHub 仓库优先用 Releases API；失败（限流/内网镜像）则回退到
        # git ls-remote --tags，两者都不通才报错。
        if "github.com" in repo:
            owner, name = self._repo_slug()
            api = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
            request = urllib.request.Request(
                api, headers={"Accept": "application/vnd.github+json", "User-Agent": "AutoDeploy"}
            )
            try:
                with self._urlopen(request, proxy, timeout=15) as response:
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
        # 必须复用 gitops 的环境构造，才能带上 HTTP/1.1 等抗抖配置与代理。
        from . import gitops

        with tempfile.TemporaryDirectory(prefix="update-check-", dir=_tmp_root()) as tmp:
            result = gitops.ls_remote_tags(
                repo_url=repo, tmp_dir=Path(tmp), timeout=30, proxy=proxy
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

    # 更新与部署共用一把准入锁；活动状态持久化后才放行 HTTP 请求。
    def deployment_blocked(self) -> bool:
        return self.state().get("stage") in ACTIVE_STAGES

    def _preflight(self) -> str:
        strategy, service = self._restart_plan()
        if strategy != "self-exit":
            raise RuntimeError("不支持自动重启：必须在真实 systemd 单元主进程中运行，且有效 Restart=always；尚未修改程序文件")
        if self.store.runs.count_active():
            raise RuntimeError("仍有部署任务在运行，请稍后再更新或回滚")
        root = Path(config.ROOT_DIR)
        if not os.access(root, os.W_OK):
            raise RuntimeError("安装目录不可写")
        if (root / ".git").exists():
            result = run_command(["git", "status", "--porcelain"], cwd=root, timeout=30)
            if not result.ok or result.output.strip():
                raise RuntimeError("源码目录存在未提交改动或无法确认状态，已中止")
        self._check_backup_root()
        self._validate_tree(root)
        return service

    def _new_state(self, operation: str, target: str, service: str) -> dict[str, Any]:
        return dict(schema_version=STATE_SCHEMA_VERSION, stage="queued", operation=operation, operation_id=uuid.uuid4().hex,
                    target_version=target, expected_version=normalize_tag(target),
                    previous_version=__version__, before_boot_id=PROCESS_BOOT_ID,
                    before_pid=os.getpid(), service=service, started_at=time.time(),
                    log=[], error="", backup="")

    def start(self, target_version: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self.deployment_blocked():
                raise RuntimeError("已有更新或回滚正在进行")
            service = self._preflight()
            if not isinstance(target_version, str) or not re.fullmatch(r"[vV]?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.-]+)?", target_version):
                raise RuntimeError("请先检查更新并确认有效目标版本")
            checked = self.check(force=True)
            if checked.error or checked.latest != target_version or not checked.update_available:
                raise RuntimeError("更新目标已变化或不可用，请重新检查并确认")
            state = self._new_state("update", target_version, service)
            self._save_state(state)
            self._thread = threading.Thread(target=self._run, args=(state,), daemon=True)
            self._thread.start()
            return dict(state)

    def _set_stage(self, state: dict[str, Any], stage: str) -> None:
        with self._lock:
            state["stage"] = stage
            self._save_state(state)

    @staticmethod
    def _validate_tree(root: Path, expected: str = "") -> str:
        for item in UPDATE_ITEMS:
            path = root / item
            if path.is_symlink() or not path.exists():
                raise RuntimeError(f"程序内容缺失或包含软链：{item}")
            if path.is_dir() != (item in ("app", "web")):
                raise RuntimeError(f"程序路径类型错误：{item}")
            for child in [path, *path.rglob("*")] if path.is_dir() else [path]:
                if child.is_symlink() or not (child.is_file() or child.is_dir()):
                    raise RuntimeError("程序内容含不安全链接或特殊文件")
        for item in ("app/__init__.py", "app/main.py", "web/index.html"):
            if not (root / item).is_file():
                raise RuntimeError(f"程序缺少关键文件：{item}")
        version = ""
        for node in ast.parse((root / "app/__init__.py").read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    version = normalize_tag(node.value.value)
        if not version or (expected and version != normalize_tag(expected)):
            raise RuntimeError("下载或备份版本与预期不符")
        return version

    def _replace(self, source: Path, root: Path) -> None:
        # 每项先完整复制至同盘临时位置，避免复制失败先删除旧代码。
        for item in UPDATE_ITEMS:
            src, dst = source / item, root / item
            staged = root / (".update-" + uuid.uuid4().hex)
            old = root / (".update-old-" + uuid.uuid4().hex)
            try:
                if src.is_dir():
                    shutil.copytree(src, staged)
                else:
                    shutil.copy2(src, staged)
                if dst.exists():
                    os.replace(dst, old)
                try:
                    os.replace(staged, dst)
                except Exception:
                    if old.exists():
                        os.replace(old, dst)
                    raise
            finally:
                for path in (staged, old):
                    if path == old and not dst.exists():
                        continue  # 恢复失败时保留原文件，绝不继续删除。
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()

    def _backup(self, root, state, log):
        self._check_backup_root()
        backup = self.backups_dir / (str(time.time_ns()) + "-" + state["operation_id"])
        backup.mkdir(parents=True)
        for item in UPDATE_ITEMS:
            src = root / item
            if src.is_dir():
                shutil.copytree(src, backup / item)
            else:
                shutil.copy2(src, backup / item)
        version = self._validate_tree(backup)
        (backup / "complete.json").write_text(json.dumps({"version": version}))
        self._validate_backup(backup)
        state["backup"] = str(backup)
        state["backup_version"] = version
        log("已备份程序代码；不包含数据库和 Python 依赖，不能保证降级兼容")
        return backup

    def _restore_backup(self, root, state):
        if not state.get("backup"):
            return
        backup = Path(state["backup"])
        self._validate_backup(backup)
        self._replace(backup, root)
        self._log(state, "已恢复程序代码；数据库和依赖未回滚")

    def _run(self, state):
        import tempfile
        root = Path(config.ROOT_DIR)
        changed = False
        def log(message):
            self._log(state, message)
            self._save_state(state)
        try:
            settings = config.load_settings()
            self._set_stage(state, "downloading")
            config.TMP_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="selfupdate-", dir=config.TMP_DIR) as tmp:
                self._download(state["target_version"], Path(tmp), settings, log, state)
                source = Path(tmp) / "src"
                self._validate_tree(source, state["expected_version"])
                self._set_stage(state, "backing_up")
                self._backup(root, state, log)
                self._set_stage(state, "applying")
                changed = True
                self._replace(source, root)
                self._set_stage(state, "dependencies")
                pip = run_command([sys_executable(), "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                                  timeout=600, env={**os.environ, **config.proxy_env(settings, for_scripts=True)})
                if not pip.ok:
                    raise RuntimeError("依赖安装失败；依赖可能已部分改变，请检查服务环境")
                self._finish(state, log)
        except Exception as exc:
            self._fail(state, exc, changed)

    def _fail(self, state, exc, changed):
        error = str(exc)
        if changed:
            try:
                self._restore_backup(Path(config.ROOT_DIR), state)
            except Exception as restore_error:
                error += f"；代码恢复失败：{restore_error}；请从保留的备份人工恢复"
        state.update(stage="failed", error=error)
        self._log(state, error)
        self._save_state(state)
        self._append_history(state)

    def _download(self, target, tmp_root, settings, log, state):
        from . import gitops
        gitops.sync_checkout(repo_url=settings.update_repo or UPDATE_REPO_DEFAULT,
                             branch=target, workspace=tmp_root / "src", depth=1,
                             tmp_dir=tmp_root / "creds", timeout=int(settings.git_timeout_seconds),
                             log=log, proxy=config.proxy_env(settings), home=tmp_root / "creds")
        source = tmp_root / "src"
        head = run_command(["git", "rev-parse", "HEAD"], cwd=source, timeout=10)
        tag = run_command(["git", "rev-parse", "--verify", "refs/tags/" + target + "^{commit}"], cwd=source, timeout=10)
        if not head.ok or not tag.ok or head.output.strip() != tag.output.strip():
            raise RuntimeError("下载结果不是已确认的目标标签")
        log("已检出目标标签")

    def _cgroup_path(self) -> str:
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                _, controllers, path = line.split(":", 2)
                if controllers in ("", "name=systemd"):
                    return path
        except (OSError, ValueError):
            pass
        return ""

    def _restart_plan(self) -> tuple[str, str]:
        path = self._cgroup_path()
        service = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9@_.-]+\.service", service):
            return "unsupported", ""
        result = run_command(["systemctl", "show", service, "--no-pager",
                              "--property=Id,LoadState,ActiveState,MainPID,ControlGroup,Restart,RestartPreventExitStatus"], timeout=10)
        props = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
        if (result.ok and props.get("Id") == service and props.get("LoadState") == "loaded"
                and props.get("ActiveState") == "active" and props.get("MainPID") == str(os.getpid())
                and props.get("ControlGroup") == path and props.get("Restart") == "always"
                and not props.get("RestartPreventExitStatus")):
            return "self-exit", service
        return "unsupported", service

    def _finish(self, state, log):
        if self._restart_plan() != ("self-exit", state["service"]):
            raise RuntimeError("重启能力已变化，已中止更新")
        state.update(restart="self-exit", restart_deadline=time.time() + RESTART_TIMEOUT)
        self._set_stage(state, "restarting")
        log("即将退出，等待 systemd 拉起后确认版本")
        self._exit_for_restart(state)

    def _exit_for_restart(self, state):
        def exit_later():
            time.sleep(1.5)
            os._exit(0)
        threading.Thread(target=exit_later, daemon=True).start()

    def rollback(self, target_version: str | None = None):
        """回滚到指定版本的备份；不指定则回滚最近一份可用备份。

        指定版本时只在**通过校验的备份**里按版本号查找（清单来自
        ``_valid_backups``，不接受任何路径），因此界面传入的值无法越界或
        指向备份目录之外的代码。
        """
        with self._lock:
            if self.deployment_blocked():
                raise RuntimeError("更新或回滚正在进行，无法重复操作")
            service = self._preflight()
            if target_version:
                wanted = str(target_version).strip()
                if not re.fullmatch(r"[vV]?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.-]+)?", wanted):
                    raise RuntimeError("回滚目标版本号无效")
                backup = self.backup_for_version(wanted)
                if backup is None:
                    raise RuntimeError(f"没有版本 {normalize_tag(wanted)} 的可用代码备份")
            else:
                backup = self.latest_backup()
                if backup is None:
                    raise RuntimeError("没有可用的代码备份")
            version = self._validate_backup(backup)
            state = self._new_state("rollback", version, service)
            self._save_state(state)
            def run():
                changed = False
                try:
                    self._set_stage(state, "backing_up")
                    self._backup(Path(config.ROOT_DIR), state, lambda m: self._log(state, m))
                    self._set_stage(state, "applying")
                    changed = True
                    self._replace(backup, Path(config.ROOT_DIR))
                    self._finish(state, lambda m: self._log(state, m))
                except Exception as exc:
                    self._fail(state, exc, changed)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            return {"ok": True, "state": dict(state)}

    def reconcile_on_startup(self):
        with self._lock:
            state = self.state()
            if state.get("stage") not in ACTIVE_STAGES:
                return
            valid = (state.get("stage") == "restarting" and state.get("operation") in ("update", "rollback")
                     and isinstance(state.get("operation_id"), str) and bool(state["operation_id"])
                     and isinstance(state.get("before_boot_id"), str) and bool(state["before_boot_id"])
                     and state["before_boot_id"] != PROCESS_BOOT_ID
                     and type(state.get("before_pid")) is int and state["before_pid"] > 0
                     and state["before_pid"] != os.getpid()
                     and state.get("expected_version") == normalize_tag(__version__)
                     and normalize_tag(str(state.get("target_version", ""))) == normalize_tag(__version__))
            if valid:
                state.update(stage="done", version=__version__, confirmed_operation=state["operation"],
                             confirmed_operation_id=state["operation_id"], boot_id=PROCESS_BOOT_ID, pid=os.getpid())
                self._log(state, "启动后已确认操作及目标版本运行")
            else:
                state.update(stage="failed", error="更新或回滚中断，或启动版本/进程与预期不符；未确认成功")
                self._log(state, state["error"])
            self._save_state(state)
            # 只有这里（重启后确认/落定为失败）才是操作的终态，历史在此写入，
            # 使「升级日志」里的每条记录都带最终结果而不是中途状态。
            self._append_history(state)


def sys_executable() -> str:
    import sys
    return sys.executable
