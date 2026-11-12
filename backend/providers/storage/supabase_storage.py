"""Supabase Storage provider adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError, NotFoundError, ProviderError
from backend.core.logging import get_logger
from backend.models.provider import StoredObject
from backend.providers.base.storage import BaseStorageProvider
from backend.services.supabase.client import get_supabase_client

logger = get_logger(__name__)


class SupabaseStorageProvider(BaseStorageProvider):
    """Object storage adapter backed by Supabase Storage buckets."""

    provider_name = "supabase"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @staticmethod
    def _clean_key(remote_key: str) -> str:
        return remote_key.strip().lstrip("/")

    def _get_bucket_client(self) -> tuple[str, Any]:
        client = get_supabase_client(self.settings)
        bucket = self.settings.SUPABASE_STORAGE_BUCKET
        return bucket, client.storage.from_(bucket)

    async def upload_file(
        self,
        local_path: Path,
        remote_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Upload ``local_path`` to Supabase Storage and return ``StoredObject`` metadata."""
        if not local_path.is_file():
            raise NotFoundError(
                f"Local file not found for upload: {local_path}",
                details={"local_path": str(local_path)},
            )

        clean_key = self._clean_key(remote_key)
        bucket, storage_bucket = self._get_bucket_client()
        data = await asyncio.to_thread(local_path.read_bytes)

        logger.info(
            "Uploading %d bytes to Supabase Storage (%s/%s)",
            len(data),
            bucket,
            clean_key,
        )
        try:
            await asyncio.to_thread(
                lambda: storage_bucket.upload(
                    path=clean_key,
                    file=data,
                    file_options={"content-type": content_type, "upsert": "true"},
                )
            )
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.exception("Supabase Storage upload failed for %s: %s", clean_key, exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to upload '{clean_key}': {exc}",
                retryable=True,
            ) from exc

        signed_url: str | None = None
        try:
            signed_url = await self.get_signed_url(
                clean_key,
                expires_in=self.settings.SUPABASE_SIGNED_URL_TTL_SECONDS,
            )
        except ProviderError as exc:
            logger.warning("Uploaded '%s' but failed to create signed URL: %s", clean_key, exc)

        public_url: str | None = None
        try:
            public_url = await asyncio.to_thread(lambda: storage_bucket.get_public_url(clean_key))
        except Exception:
            public_url = None

        return StoredObject(
            bucket=bucket,
            remote_key=clean_key,
            public_url=public_url if isinstance(public_url, str) else None,
            signed_url=signed_url,
            size_bytes=len(data),
        )

    async def get_signed_url(self, remote_key: str, expires_in: int = 86400) -> str:
        """Create a time-limited signed URL for ``remote_key``."""
        clean_key = self._clean_key(remote_key)
        _, storage_bucket = self._get_bucket_client()

        try:
            res = await asyncio.to_thread(
                lambda: storage_bucket.create_signed_url(clean_key, expires_in)
            )
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.exception("Supabase signed URL generation failed for %s: %s", clean_key, exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to create signed URL for '{clean_key}': {exc}",
                retryable=True,
            ) from exc

        if isinstance(res, dict):
            url = res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")
            if url:
                return str(url)
        elif isinstance(res, str) and res:
            return res

        raise ProviderError(
            self.provider_name,
            f"Supabase returned an invalid signed URL payload for '{clean_key}'",
            retryable=False,
        )

    async def delete_object(self, remote_key: str) -> None:
        """Delete ``remote_key`` from the configured Supabase Storage bucket."""
        clean_key = self._clean_key(remote_key)
        bucket, storage_bucket = self._get_bucket_client()

        logger.info("Deleting object from Supabase Storage (%s/%s)", bucket, clean_key)
        try:
            await asyncio.to_thread(lambda: storage_bucket.remove([clean_key]))
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.exception("Supabase Storage delete failed for %s: %s", clean_key, exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to delete '{clean_key}': {exc}",
                retryable=True,
            ) from exc
