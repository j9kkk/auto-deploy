"""Abstract Base Class for Cloud Storage Providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class CloudProvider(ABC):
    """Abstract interface for cloud storage backup providers."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Test authentication and connectivity to the remote storage.

        Returns: (success: bool, message: str)
        """
        raise NotImplementedError

    @abstractmethod
    def upload_file(self, local_path: Path, remote_filename: str) -> tuple[bool, str]:
        """Upload a local file to the cloud destination folder.

        Returns: (success: bool, message: str)
        """
        raise NotImplementedError

    @abstractmethod
    def delete_file(self, remote_filename: str) -> tuple[bool, str]:
        """Delete a remote backup file.

        Returns: (success: bool, message: str)
        """
        raise NotImplementedError

    @abstractmethod
    def list_files(self) -> list[dict[str, Any]]:
        """List backup files stored in the cloud destination folder.

        Returns: list of dicts with 'name', 'size_bytes', 'modified_at'
        """
        raise NotImplementedError
