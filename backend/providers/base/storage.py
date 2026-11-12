"""Abstract Base Class for Object Storage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from backend.models.provider import StoredObject


class BaseStorageProvider(ABC):
    """Contract that all object storage provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def upload_file(
        self,
        local_path: Path,
        remote_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Upload a local file to remote object storage under ``remote_key``."""
        raise NotImplementedError

    @abstractmethod
    async def get_signed_url(self, remote_key: str, expires_in: int = 86400) -> str:
        """Return a time-limited signed URL (or local download path) for ``remote_key``."""
        raise NotImplementedError

    @abstractmethod
    async def delete_object(self, remote_key: str) -> None:
        """Delete an object from storage by ``remote_key``."""
        raise NotImplementedError
