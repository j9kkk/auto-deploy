"""Rclone CLI driver for direct integration with Baidu Netdisk, Aliyun Drive, 115, S3, etc."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..deployer import command_exists
from .base import CloudProvider

logger = logging.getLogger("autodeploy.cloud.rclone")


class RcloneProvider(CloudProvider):
    """Integrates with host or container rclone binary."""

    def __init__(self, remote_target: str, timeout: int = 120) -> None:
        self.remote_target = remote_target.strip()
        self.timeout = timeout

    def _is_available(self) -> bool:
        return command_exists("rclone")

    def test_connection(self) -> tuple[bool, str]:
        if not self._is_available():
            return False, "系统未安装 rclone 命令，请先安装 rclone 或使用 WebDAV 驱动"
        if not self.remote_target:
            return False, "未配置 rclone 目标（例如 mydrive:backups）"

        try:
            cmd = ["rclone", "about", self.remote_target]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                return True, "rclone 远端配置连通性测试成功"
            # Fallback to lsd if about is not supported on this provider
            cmd_ls = ["rclone", "lsf", "--max-depth", "1", self.remote_target]
            res_ls = subprocess.run(cmd_ls, capture_output=True, text=True, timeout=15)
            if res_ls.returncode == 0:
                return True, "rclone 远端连通性测试成功"
            err = res_ls.stderr.strip() or res.stderr.strip() or f"退出码 {res.returncode}"
            return False, f"rclone 连接失败: {err}"
        except Exception as e:
            return False, f"执行 rclone 测试出错: {e}"

    def upload_file(self, local_path: Path, remote_filename: str) -> tuple[bool, str]:
        if not self._is_available():
            return False, "未安装 rclone 命令"
        if not local_path.is_file():
            return False, f"本地文件不存在: {local_path}"

        target = f"{self.remote_target.rstrip('/')}/{remote_filename.lstrip('/')}"
        try:
            cmd = ["rclone", "copyto", str(local_path), target]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            if res.returncode == 0:
                return True, f"成功通过 rclone 同步至 {target}"
            return False, f"rclone 上传失败: {res.stderr.strip()}"
        except Exception as e:
            return False, f"rclone 执行异常: {e}"

    def delete_file(self, remote_filename: str) -> tuple[bool, str]:
        if not self._is_available():
            return False, "未安装 rclone 命令"
        target = f"{self.remote_target.rstrip('/')}/{remote_filename.lstrip('/')}"
        try:
            cmd = ["rclone", "deletefile", target]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                return True, "删除成功"
            return False, f"rclone 删除失败: {res.stderr.strip()}"
        except Exception as e:
            return False, f"rclone 执行异常: {e}"

    def list_files(self) -> list[dict[str, Any]]:
        if not self._is_available():
            return []
        try:
            cmd = ["rclone", "lsjson", self.remote_target]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return []
            items = json.loads(res.stdout)
            results = []
            for item in items:
                if not item.get("IsDir"):
                    results.append({
                        "name": item.get("Name"),
                        "size_bytes": item.get("Size", 0),
                        "modified_at": item.get("ModTime", ""),
                    })
            return results
        except Exception:
            return []
