"""Local backup and restore engine for AutoDeploy.

Safely creates consistent tar.gz backups of the SQLite database (using online WAL backup),
configuration files, and optional logs/artifacts, with full checksum verification,
retention pruning, and restore preflight integrity checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__ as VERSION
from . import config
from .schedule import iso, utcnow
from .security import new_token
from .store import Store

logger = logging.getLogger("autodeploy.backup")

BACKUP_DIR_NAME = "backups"
MANIFEST_NAME = "manifest.json"


class BackupError(Exception):
    """Raised when backup or restore operation fails validation or execution."""


@dataclass
class BackupManifest:
    manifest_version: int
    app_version: str
    created_at: str
    contains_logs: bool
    contains_artifacts: bool
    files: list[str]


def get_backup_dir() -> Path:
    """Resolve and ensure the dedicated local backup directory."""
    backup_dir = config.DATA_DIR / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _calculate_sha256(filepath: Path) -> str:
    """Compute sha256 checksum of a file in chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_backup(
    store: Store,
    *,
    trigger: str = "manual",
    include_logs: bool = False,
    include_artifacts: bool = False,
    retention_count: int = 10,
) -> dict[str, Any]:
    """Perform a consistent snapshot backup into a .tar.gz archive."""
    backup_dir = get_backup_dir()
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"autodeploy-backup-{timestamp_str}.tar.gz"
    target_path = backup_dir / filename

    # 同一秒内的第二次备份会撞 filename 唯一约束；追加短随机后缀绕开。
    if store.backups.get_by_filename(filename) is not None or target_path.exists():
        suffix = new_token().lower()[:4]
        filename = f"autodeploy-backup-{timestamp_str}-{suffix}.tar.gz"
        target_path = backup_dir / filename

    # 0. 快照在前、记录在后：先落库再拍快照会让备份包里永远留一条
    #    in_progress 幽灵记录，恢复后还会被误当作有效备份。
    try:
        with tempfile.TemporaryDirectory(prefix="autodeploy-backup-") as tmp:
            stage = Path(tmp)
            files_manifest: list[str] = []

            # 1. Hot online backup of SQLite database to ensure WAL consistency
            db_snap_path = stage / "autodeploy.db"
            try:
                target_conn = sqlite3.connect(str(db_snap_path))
                with store.db._lock:
                    store.db._conn.backup(target_conn)
                target_conn.close()
                files_manifest.append("autodeploy.db")
            except Exception as e:
                logger.error("在线热备 SQLite 数据库失败: %s", e)
                raise BackupError(f"数据库热备失败: {e}") from e

            # 2. Backup config.json if present
            cfg_path = config.settings_path()
            if cfg_path.exists():
                try:
                    shutil.copy2(cfg_path, stage / "config.json")
                    files_manifest.append("config.json")
                except Exception as e:
                    logger.warning("复制配置文件失败: %s", e)

            # 3. Optional: include logs
            if include_logs and config.LOGS_DIR.exists():
                dest_logs = stage / "logs"
                shutil.copytree(config.LOGS_DIR, dest_logs, dirs_exist_ok=True)
                files_manifest.append("logs/")

            # 4. Optional: include artifacts
            if include_artifacts and config.ARTIFACTS_DIR.exists():
                dest_artifacts = stage / "artifacts"
                shutil.copytree(config.ARTIFACTS_DIR, dest_artifacts, dirs_exist_ok=True)
                files_manifest.append("artifacts/")

            # 5. Write manifest.json
            manifest = BackupManifest(
                manifest_version=1,
                app_version=VERSION,
                created_at=iso(utcnow()),
                contains_logs=include_logs,
                contains_artifacts=include_artifacts,
                files=files_manifest,
            )
            (stage / MANIFEST_NAME).write_text(json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8")

            # 6. Compress stage folder into .tar.gz
            try:
                with tarfile.open(target_path, "w:gz") as tar:
                    for item in stage.iterdir():
                        tar.add(item, arcname=item.name)
            except Exception as e:
                if target_path.exists():
                    target_path.unlink()
                raise BackupError(f"打包备份文件失败: {e}") from e

        size_bytes = target_path.stat().st_size
        sha256_hash = _calculate_sha256(target_path)

        # 7. 快照成功后才登记记录；失败路径不留任何半截状态
        backup_id = store.backups.create(
            filename=filename,
            filepath=str(target_path),
            size_bytes=size_bytes,
            sha256=sha256_hash,
            trigger=trigger,
            contains_logs=include_logs,
            contains_artifacts=include_artifacts,
            status="success",
        )
    except Exception:
        if target_path.exists():
            target_path.unlink()
        raise

    # 8. Retention pruning
    if retention_count > 0:
        pruned = store.backups.prune_older_than(retention_count)
        for old_item in pruned:
            old_file = Path(old_item["filepath"])
            if old_file.exists():
                try:
                    old_file.unlink()
                except OSError as e:
                    logger.warning("清理超期备份文件失败 %s: %s", old_file, e)

    # 9. Trigger cloud sync in background if configured
    try:
        from .cloud import sync_backup_to_cloud
        import threading
        threading.Thread(
            target=sync_backup_to_cloud,
            args=(target_path,),
            name=f"cloud-sync-{backup_id}",
            daemon=True,
        ).start()
    except Exception as e:
        logger.debug("触发云端同步后台任务失败: %s", e)

    return store.backups.get(backup_id) or {}


def inspect_backup_file(archive_path: Path) -> dict[str, Any]:
    """Inspect an existing backup tar.gz file and verify integrity without modifying state."""
    if not archive_path.is_file():
        raise BackupError("备份文件不存在或不是普通文件")

    if not tarfile.is_tarfile(str(archive_path)):
        raise BackupError("不是有效的 tar.gz 归档文件")

    with tempfile.TemporaryDirectory(prefix="autodeploy-inspect-") as tmp:
        stage = Path(tmp)
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                # Security: prevent directory traversal in archive
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        raise BackupError(f"备份文件包含非法路径: {member.name}")
                tar.extractall(stage)
        except Exception as e:
            raise BackupError(f"解压备份文件失败: {e}") from e

        manifest_file = stage / MANIFEST_NAME
        manifest_data = {}
        if manifest_file.exists():
            try:
                manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        db_file = stage / "autodeploy.db"
        if not db_file.exists():
            raise BackupError("备份文件中未找到 autodeploy.db 数据库文件")

        # Verify database integrity
        try:
            test_conn = sqlite3.connect(str(db_file))
            cursor = test_conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            integrity_result = cursor.fetchone()[0]
            test_conn.close()
            if integrity_result != "ok":
                raise BackupError(f"数据库完整性校验失败: {integrity_result}")
        except Exception as e:
            raise BackupError(f"无法校验备份中的数据库完整性: {e}") from e

        size_bytes = archive_path.stat().st_size
        sha256_hash = _calculate_sha256(archive_path)

        return {
            "valid": True,
            "filename": archive_path.name,
            "size_bytes": size_bytes,
            "sha256": sha256_hash,
            "manifest": manifest_data,
        }


def restore_backup(
    store: Store,
    backup_id: int,
    *,
    is_blocked: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Restore database and configurations from a verified backup archive."""
    backup = store.backups.get(backup_id)
    if not backup:
        raise BackupError(f"找不到 ID 为 {backup_id} 的备份记录")

    # 恢复会把 live 数据库整体覆盖：存在活跃运行时其 run 记录会凭空消失，
    # 状态机与前端都会错乱；系统更新/回滚期间同样禁止并发改动。
    active_runs = store.runs.count_active()
    if active_runs > 0:
        raise BackupError(f"当前有 {active_runs} 个部署正在运行或排队，恢复会丢失其记录；请等待结束或取消后再试")
    if is_blocked is not None and is_blocked():
        raise BackupError("系统正在更新或回滚，暂不能执行恢复")

    archive_path = Path(backup["filepath"])
    inspection = inspect_backup_file(archive_path)

    # 记住恢复前系统已存在的备份记录（包含本次正在恢复的这份备份），
    # 恢复后补录回库，保证磁盘上已有备份资产索引不丢失。
    pre_existing_backups = store.backups.list_all(limit=2000)

    with tempfile.TemporaryDirectory(prefix="autodeploy-restore-") as tmp:
        stage = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(stage)

        restored_db_path = stage / "autodeploy.db"
        # 1. Restore SQLite database into live connection safely using SQLite backup API
        try:
            source_conn = sqlite3.connect(str(restored_db_path))
            with store.db._lock:
                source_conn.backup(store.db._conn)
            source_conn.close()
        except Exception as e:
            logger.error("恢复数据库失败: %s", e)
            raise BackupError(f"恢复数据库失败: {e}") from e

        # 2. 补录宿主机现存备份资产：数据库虽回滚到了快照时点，但宿主机上物理存在的
        #    备份文件仍需在备份列表保留，避免用户误以为恢复操作删除了磁盘备份。
        #    created_at 沿用原值，列表时间线不因恢复操作而失真。
        for prev in pre_existing_backups:
            prev_file = Path(prev["filepath"])
            if prev_file.is_file() and store.backups.get_by_filename(prev["filename"]) is None:
                store.backups.create(
                    filename=prev["filename"],
                    filepath=str(prev_file),
                    size_bytes=prev.get("size_bytes", 0),
                    sha256=prev.get("sha256", ""),
                    trigger=prev.get("trigger", "manual"),
                    contains_logs=bool(prev.get("contains_logs")),
                    contains_artifacts=bool(prev.get("contains_artifacts")),
                    status="success",
                    created_at=prev.get("created_at"),
                )

        # 3. Restore config.json if present
        restored_config = stage / "config.json"
        if restored_config.exists():
            try:
                shutil.copy2(restored_config, config.settings_path())
            except Exception as e:
                logger.warning("恢复 config.json 失败: %s", e)

        # 4. Restore logs if present
        restored_logs = stage / "logs"
        if restored_logs.exists() and restored_logs.is_dir():
            try:
                shutil.copytree(restored_logs, config.LOGS_DIR, dirs_exist_ok=True)
            except Exception as e:
                logger.warning("恢复日志失败: %s", e)

    return {
        "ok": True,
        "message": f"已成功从备份 {backup['filename']} 恢复系统数据与配置",
        "backup": backup,
        "manifest": inspection.get("manifest"),
    }
