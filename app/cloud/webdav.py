"""Pure standard library WebDAV client for cloud backups.

Compatible with Nextcloud, Jianguoyun (坚果云), Synology/QNAP NAS,
and universal AList / OpenList gateways (bridging Baidu Netdisk, Aliyun Drive, 115, etc.).
"""

from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .base import CloudProvider

logger = logging.getLogger("autodeploy.cloud.webdav")


class WebDAVProvider(CloudProvider):
    """Pure Python standard library WebDAV client."""

    def __init__(
        self,
        endpoint_url: str,
        username: str = "",
        password: str = "",
        root_dir: str = "autodeploy-backups",
        timeout: int = 30,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.root_dir = root_dir.strip("/")
        self.timeout = timeout

    def _headers(self, custom: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "AutoDeploy-Backup/1.0",
        }
        if self.username or self.password:
            auth_str = f"{self.username}:{self.password}"
            b64 = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {b64}"
        if custom:
            headers.update(custom)
        return headers

    def _make_url(self, path: str = "") -> str:
        clean = path.strip("/")
        if clean:
            return urllib.parse.urljoin(self.endpoint_url, clean)
        return self.endpoint_url

    def _target_url(self, filename: str = "") -> str:
        parts = []
        if self.root_dir:
            parts.append(self.root_dir)
        if filename:
            parts.append(filename.strip("/"))
        joined = "/".join(parts)
        return urllib.parse.urljoin(self.endpoint_url, joined)

    def test_connection(self) -> tuple[bool, str]:
        """Test connection using PROPFIND on the base WebDAV endpoint."""
        if not self.endpoint_url.startswith(("http://", "https://")):
            return False, "WebDAV 地址必须以 http:// 或 https:// 开头"

        req = urllib.request.Request(
            self.endpoint_url,
            headers=self._headers({"Depth": "0"}),
            method="PROPFIND",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 207):
                    self._ensure_root_dir()
                    return True, "WebDAV 连接与鉴权成功"
                return False, f"服务器返回 HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "WebDAV 用户名或密码错误 (HTTP 401)"
            return False, f"WebDAV 请求失败: HTTP {e.code} {e.reason}"
        except Exception as e:
            return False, f"无法连接到 WebDAV 服务器: {e}"

    def _ensure_root_dir(self) -> None:
        """Create the target backup directory on remote via MKCOL if missing.

        逐级创建：root 配成 a/b 且 a 不存在时，多数服务器会对缺失父集合
        返回 409，因此从第一级开始依次 MKCOL。
        """
        if not self.root_dir:
            return
        parts = [p for p in self.root_dir.split("/") if p]
        for depth in range(1, len(parts) + 1):
            target_dir_url = self._target_url("/".join(parts[:depth]))
            if not target_dir_url.endswith("/"):
                target_dir_url += "/"
            req = urllib.request.Request(
                target_dir_url,
                headers=self._headers(),
                method="MKCOL",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    pass
            except urllib.error.HTTPError as e:
                # 405 Method Not Allowed indicates the collection already exists
                if e.code not in (405, 301, 302):
                    logger.debug("MKCOL %s: %s", target_dir_url, e)
            except Exception as e:
                logger.debug("MKCOL exception %s: %s", target_dir_url, e)

    def upload_file(self, local_path: Path, remote_filename: str) -> tuple[bool, str]:
        """Upload a file to remote WebDAV directory using chunked streaming PUT.

        分块流式读取：备份包可能包含数 GB 的构建产物，整块 read_bytes()
        会把文件全部压进内存，容器形态下容易 OOM。文件对象作为请求体时
        urllib 按 8KB 逐块发送，全程内存占用恒定。
        """
        if not local_path.is_file():
            return False, f"本地文件不存在: {local_path}"

        self._ensure_root_dir()
        target_url = self._target_url(remote_filename)

        file_size = local_path.stat().st_size
        try:
            with open(local_path, "rb") as f:
                req = urllib.request.Request(
                    target_url,
                    data=f,
                    headers=self._headers({
                        "Content-Type": "application/gzip",
                        "Content-Length": str(file_size),
                    }),
                    method="PUT",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status in (200, 201, 204):
                        return True, f"成功上传至 {remote_filename}"
                    return False, f"上传失败，HTTP 状态码: {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"上传 HTTP 错误: {e.code} {e.reason}"
        except Exception as e:
            return False, f"上传文件失败: {e}"

    def delete_file(self, remote_filename: str) -> tuple[bool, str]:
        """Delete a remote file from WebDAV."""
        target_url = self._target_url(remote_filename)
        req = urllib.request.Request(
            target_url,
            headers=self._headers(),
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 204):
                    return True, "删除成功"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, "文件已不存在"
            return False, f"删除失败: HTTP {e.code}"
        except Exception as e:
            return False, f"删除操作异常: {e}"

    def list_files(self) -> list[dict[str, Any]]:
        """List files in the backup directory using PROPFIND."""
        target_url = self._target_url()
        if not target_url.endswith("/"):
            target_url += "/"
        req = urllib.request.Request(
            target_url,
            headers=self._headers({"Depth": "1"}),
            method="PROPFIND",
        )
        results: list[dict[str, Any]] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)
                # Find all D:response elements regardless of namespace prefix
                for elem in root.findall(".//{*}response"):
                    href_elem = elem.find("{*}href")
                    if href_elem is None or not href_elem.text:
                        continue
                    href = href_elem.text.rstrip("/")
                    name = href.split("/")[-1]
                    if not name or name == self.root_dir:
                        continue  # Skip root directory itself

                    size_elem = elem.find(".//{*}getcontentlength")
                    size = int(size_elem.text) if size_elem is not None and size_elem.text and size_elem.text.isdigit() else 0

                    date_elem = elem.find(".//{*}getlastmodified")
                    modified = date_elem.text if date_elem is not None else ""

                    results.append({
                        "name": name,
                        "size_bytes": size,
                        "modified_at": modified,
                    })
        except Exception as e:
            logger.debug("PROPFIND list_files failed: %s", e)

        return results
