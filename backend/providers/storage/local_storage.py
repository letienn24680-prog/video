"""Local filesystem Object Storage adapter for offline development & tests."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from backend.core.config import Settings, get_settings
from backend.core.exceptions import NotFoundError, ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.models.provider import StoredObject
from backend.providers.base.storage import BaseStorageProvider

logger = get_logger(__name__)


class LocalStorageProvider(BaseStorageProvider):
    """Local filesystem storage adapter mirroring Supabase Storage semantics."""

    provider_name = "local"

    def __init__(
        self,
        settings: Settings | None = None,
        base_dir: Path | None = None,
        base_url_prefix: str = "/local-media",
    ) -> None:
        self.settings = settings or get_settings()
        root = base_dir if base_dir is not None else (self.settings.temp_path / "local_storage")
        self.base_dir = Path(root).resolve()
        self.base_url_prefix = (base_url_prefix or "").strip().rstrip("/")

    def _build_url(self, clean_key: str, target_path: Path) -> str:
        """Build browser-playable /local-media/storage/... URL when inside TEMP_DIR/local_storage."""
        if self.base_url_prefix:
            temp_root = self.settings.temp_path.resolve()
            default_storage_root = (temp_root / "local_storage").resolve()
            if self.base_dir == default_storage_root:
                return f"{self.base_url_prefix}/storage/{clean_key}"
            try:
                rel = target_path.relative_to(temp_root)
                return f"{self.base_url_prefix}/{rel.as_posix()}"
            except ValueError:
                pass
        return target_path.as_uri()

    def _resolve_target(self, remote_key: str) -> tuple[str, Path]:
        clean_key = remote_key.strip().replace("\\", "/").lstrip("/")
        if not clean_key:
            raise ValidationError("remote_key cannot be empty")

        candidate = (self.base_dir / clean_key).resolve()
        try:
            candidate.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid remote_key '{remote_key}': path traversal is not allowed",
                details={"remote_key": remote_key},
            ) from exc
        return clean_key, candidate

    async def upload_file(
        self,
        local_path: Path,
        remote_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Copy ``local_path`` into local storage root under ``remote_key``."""
        del content_type  # Unused by filesystem backend; kept for ABC signature parity.
        if not local_path.is_file():
            raise NotFoundError(
                f"Local file not found for upload: {local_path}",
                details={"local_path": str(local_path)},
            )

        clean_key, target_path = self._resolve_target(remote_key)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if local_path.resolve() != target_path:
                await asyncio.to_thread(shutil.copy2, local_path, target_path)
            size_bytes = target_path.stat().st_size
        except OSError as exc:
            logger.exception("Local storage upload failed for %s: %s", clean_key, exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to store file locally at '{clean_key}': {exc}",
            ) from exc

        uri = self._build_url(clean_key, target_path)
        logger.info("Stored %d bytes locally at %s", size_bytes, target_path)
        return StoredObject(
            bucket="local",
            remote_key=clean_key,
            public_url=uri,
            signed_url=uri,
            size_bytes=size_bytes,
        )

    async def get_signed_url(self, remote_key: str, expires_in: int = 86400) -> str:
        """Return a browser-playable URL or ``file://`` URI for a stored local object."""
        del expires_in
        clean_key, target_path = self._resolve_target(remote_key)
        if not target_path.is_file():
            raise NotFoundError(
                f"Local storage object not found: {clean_key}",
                details={"remote_key": clean_key},
            )
        return self._build_url(clean_key, target_path)

    async def delete_object(self, remote_key: str) -> None:
        """Delete ``remote_key`` from local storage if it exists."""
        clean_key, target_path = self._resolve_target(remote_key)
        try:
            await asyncio.to_thread(target_path.unlink, missing_ok=True)
            logger.info("Deleted local storage object %s", clean_key)
        except OSError as exc:
            logger.exception("Local storage delete failed for %s: %s", clean_key, exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to delete local object '{clean_key}': {exc}",
            ) from exc
