"""Cloud storage backup subsystem for AutoDeploy."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from .base import CloudProvider
from .rclone import RcloneProvider
from .webdav import WebDAVProvider

logger = logging.getLogger("autodeploy.cloud")


def get_cloud_provider() -> CloudProvider | None:
    """Build the configured cloud provider instance from global settings."""
    settings = config.load_settings()
    if not settings.cloud_backup_enabled:
        return None

    provider_type = (settings.cloud_provider or "webdav").lower()
    if provider_type == "webdav":
        if not settings.cloud_webdav_url:
            return None
        return WebDAVProvider(
            endpoint_url=settings.cloud_webdav_url,
            username=settings.cloud_webdav_username,
            password=settings.cloud_webdav_password,
            root_dir=settings.cloud_webdav_root or "autodeploy-backups",
        )
    elif provider_type == "rclone":
        if not settings.cloud_rclone_remote:
            return None
        return RcloneProvider(remote_target=settings.cloud_rclone_remote)

    return None


def sync_backup_to_cloud(local_path: Path) -> tuple[bool, str]:
    """Upload local backup archive to the configured cloud storage."""
    provider = get_cloud_provider()
    if provider is None:
        return False, "未启用云端备份或配置不完整"

    filename = local_path.name
    ok, msg = provider.upload_file(local_path, filename)
    if ok:
        settings = config.load_settings()
        retention = max(1, settings.cloud_retention_count)
        prune_cloud_backups(provider, retention)
    return ok, msg


def prune_cloud_backups(provider: CloudProvider, keep_count: int) -> int:
    """Retain only the latest N backup files in cloud storage."""
    try:
        files = provider.list_files()
        backup_files = [f for f in files if f.get("name", "").startswith("autodeploy-backup-")]
        # Sort by filename descending (filenames contain timestamp YYYYMMDD-HHMMSS)
        backup_files.sort(key=lambda x: x.get("name", ""), reverse=True)

        to_delete = backup_files[keep_count:]
        deleted_count = 0
        for item in to_delete:
            fname = item.get("name")
            if fname:
                del_ok, _ = provider.delete_file(fname)
                if del_ok:
                    deleted_count += 1
        return deleted_count
    except Exception as e:
        logger.warning("清理云端历史备份失败: %s", e)
        return 0
