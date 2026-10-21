"""Local and Cloud Backup API endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import config
from ..backup import (
    BackupError,
    create_backup,
    get_backup_dir,
    inspect_backup_file,
    restore_backup,
)
from ..cloud import sync_backup_to_cloud
from ..cloud.rclone import RcloneProvider
from ..cloud.webdav import WebDAVProvider
from ..service import Service
from .deps import audit, client_ip, current_user, get_service, require_admin

router = APIRouter(prefix="/api/backups", tags=["backups"])


class CreateBackupRequest(BaseModel):
    include_logs: bool = False
    include_artifacts: bool = False
    retention_count: int = Field(10, ge=1, le=100)


@router.get("")
def list_backups(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """List recorded backups with size and configuration summary."""
    backups = service.store.backups.list_all(limit=limit, offset=offset)
    total_count = service.store.backups.count()

    total_bytes = 0
    valid_backups = []
    for b in backups:
        path = Path(b["filepath"])
        exists = path.exists()
        b["file_exists"] = exists
        if exists:
            total_bytes += b["size_bytes"]
        valid_backups.append(b)

    return {
        "backups": valid_backups,
        "total": total_count,
        "total_bytes": total_bytes,
        "backup_dir": str(get_backup_dir()),
    }


@router.post("")
def trigger_backup(
    req: CreateBackupRequest,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new manual backup archive."""
    try:
        backup = create_backup(
            service.store,
            trigger="manual",
            include_logs=req.include_logs,
            include_artifacts=req.include_artifacts,
            retention_count=req.retention_count,
        )
        audit(
            service,
            action="backup_created",
            actor=user["username"],
            target=backup.get("filename", ""),
            detail=f"大小: {backup.get('size_bytes', 0)} 字节",
            ip=client_ip(request),
        )
        return {"ok": True, "message": "备份创建成功", "backup": backup}
    except BackupError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{backup_id}/download")
def download_backup(
    backup_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> FileResponse:
    """Download a backup tar.gz file."""
    backup = service.store.backups.get(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    path = Path(backup["filepath"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="备份文件在磁盘上不存在")

    return FileResponse(
        path=str(path),
        filename=backup["filename"],
        media_type="application/gzip",
    )


@router.delete("/{backup_id}")
def delete_backup(
    backup_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a backup archive and record."""
    backup = service.store.backups.get(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")

    path = Path(backup["filepath"])
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除磁盘文件失败: {e}")

    service.store.backups.delete(backup_id)
    audit(
        service,
        action="backup_deleted",
        actor=user["username"],
        target=backup["filename"],
        ip=client_ip(request),
    )
    return {"ok": True, "message": "备份已删除"}


@router.post("/{backup_id}/inspect")
def inspect_backup(
    backup_id: int,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Inspect backup archive integrity and read manifest metadata."""
    backup = service.store.backups.get(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    path = Path(backup["filepath"])
    try:
        info = inspect_backup_file(path)
        return {"ok": True, "inspection": info}
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{backup_id}/restore")
def restore_from_backup(
    backup_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Restore system database and configurations from a backup."""
    try:
        result = restore_backup(
            service.store,
            backup_id,
            is_blocked=service.scheduler.deployment_blocked,
        )
        audit(
            service,
            action="backup_restored",
            actor=user["username"],
            target=str(backup_id),
            detail=result["message"],
            ip=client_ip(request),
        )
        return result
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload")
async def upload_backup(
    request: Request,
    file: UploadFile = File(...),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Upload an external backup archive into the system."""
    filename = Path(file.filename or "upload.tar.gz").name
    if not filename.endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="只支持上传 .tar.gz 格式的备份文件")

    backup_dir = get_backup_dir()
    target_path = backup_dir / filename
    if target_path.exists():
        raise HTTPException(status_code=409, detail="同名备份文件已存在")

    # 落盘前限额：误传/恶意大文件不该先写满磁盘才被发现。
    max_bytes = 8 * 1024 * 1024 * 1024  # 8 GB
    try:
        size = 0
        with open(target_path, "wb") as buffer:
            while chunk := await file.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    buffer.close()
                    target_path.unlink()
                    raise HTTPException(status_code=413, detail="备份文件超过 8 GB 上限")
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if target_path.exists():
            target_path.unlink()
        raise HTTPException(status_code=500, detail=f"保存上传文件失败: {e}")

    try:
        inspection = inspect_backup_file(target_path)
    except BackupError as e:
        if target_path.exists():
            target_path.unlink()
        raise HTTPException(status_code=400, detail=f"上传的文件不是有效的备份包: {e}")

    manifest = inspection.get("manifest") or {}
    backup_id = service.store.backups.create(
        filename=filename,
        filepath=str(target_path),
        size_bytes=inspection["size_bytes"],
        sha256=inspection["sha256"],
        trigger="upload",
        contains_logs=bool(manifest.get("contains_logs")),
        contains_artifacts=bool(manifest.get("contains_artifacts")),
        status="success",
    )
    audit(
        service,
        action="backup_uploaded",
        actor=user["username"],
        target=filename,
        ip=client_ip(request),
    )
    return {"ok": True, "message": "备份上传并校验成功", "backup": service.store.backups.get(backup_id)}


class CloudTestRequest(BaseModel):
    provider: str = "webdav"
    webdav_url: str = ""
    webdav_username: str = ""
    webdav_password: str = ""
    webdav_root: str = "autodeploy-backups"
    rclone_remote: str = ""


@router.post("/cloud/test")
def test_cloud_storage(
    req: CloudTestRequest,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Test remote cloud storage connectivity and authorization."""
    if req.provider == "webdav":
        # 密码不回显：留空且已保存过时用存储中的凭据测试。
        password = req.webdav_password
        if password == "__SAVED__":
            password = config.load_settings().cloud_webdav_password
        client = WebDAVProvider(
            endpoint_url=req.webdav_url or config.load_settings().cloud_webdav_url,
            username=req.webdav_username or config.load_settings().cloud_webdav_username,
            password=password,
            root_dir=req.webdav_root,
        )
    elif req.provider == "rclone":
        client = RcloneProvider(remote_target=req.rclone_remote)
    else:
        raise HTTPException(status_code=400, detail="不支持的存储驱动类型")

    ok, msg = client.test_connection()
    return {"ok": ok, "message": msg}


@router.post("/{backup_id}/upload-cloud")
def upload_to_cloud(
    backup_id: int,
    request: Request,
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Upload a specific local backup to the configured cloud storage."""
    backup = service.store.backups.get(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    path = Path(backup["filepath"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="本地备份文件不存在")

    ok, msg = sync_backup_to_cloud(path)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)

    audit(
        service,
        action="backup_synced_cloud",
        actor=user["username"],
        target=backup["filename"],
        detail=msg,
        ip=client_ip(request),
    )
    return {"ok": True, "message": msg}

