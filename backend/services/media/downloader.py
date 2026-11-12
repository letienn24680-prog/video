"""Media downloader and source staging service (yt-dlp, local files, object storage)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx
from pydantic import BaseModel

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    NotFoundError,
    ProviderError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.providers.base.storage import BaseStorageProvider

logger = get_logger(__name__)


class DownloadedMedia(BaseModel):
    """Normalized metadata for a staged or downloaded source video/audio file."""

    local_path: Path
    title: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    source_url: str | None = None


def _validate_http_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValidationError("Media URL cannot be empty")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(
            f"Invalid media URL '{url}': expected http:// or https:// URL",
            details={"url": url},
        )
    return cleaned


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = f"//{parsed.netloc}{raw_path}"
    return Path(raw_path)


def _safe_existing_file(root: Path, rel_path: str) -> Path | None:
    root_resolved = root.resolve()
    candidate = (root_resolved / rel_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def resolve_local_media_url(
    raw_ref: str,
    settings: Settings | None = None,
    *,
    base_dir: Path | None = None,
) -> Path | None:
    """Resolve a ``/local-media/...`` URL path to a safe existing file on disk."""
    clean = (raw_ref or "").strip()
    if not clean.startswith("/local-media/"):
        return None

    rel = unquote(clean[len("/local-media/") :]).lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None

    temp_root = (settings or get_settings()).temp_path.resolve()

    if rel.startswith("storage/"):
        storage_rel = rel[len("storage/") :].lstrip("/")
        if storage_rel and ".." not in Path(storage_rel).parts:
            if base_dir is not None:
                found = _safe_existing_file(Path(base_dir), storage_rel)
                if found is not None:
                    return found
            found = _safe_existing_file(temp_root / "local_storage", storage_rel)
            if found is not None:
                return found

    return _safe_existing_file(temp_root, rel)


class MediaDownloader:
    """Downloads remote video URLs via ``yt-dlp`` and stages uploaded storage assets."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.max_bytes = int(self.settings.MAX_UPLOAD_MB) * 1024 * 1024

    def _enforce_size_limit(self, file_path: Path) -> None:
        if not file_path.is_file():
            raise ProviderError("media_downloader", f"Downloaded file missing: {file_path}")
        size_bytes = file_path.stat().st_size
        if size_bytes == 0:
            file_path.unlink(missing_ok=True)
            raise ValidationError("Source media file is empty (0 bytes)")
        if size_bytes > self.max_bytes:
            file_path.unlink(missing_ok=True)
            size_mb = round(size_bytes / (1024 * 1024), 2)
            raise ValidationError(
                f"Media size ({size_mb} MB) exceeds MAX_UPLOAD_MB ({self.settings.MAX_UPLOAD_MB} MB)",
                details={"size_bytes": size_bytes, "max_bytes": self.max_bytes},
            )

    def _run_yt_dlp_sync(
        self,
        url: str,
        work_dir: Path,
        filename: str,
    ) -> DownloadedMedia:
        try:
            import yt_dlp  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "yt-dlp is required to download remote video URLs. Install 'yt-dlp'."
            ) from exc

        target_name = Path(filename)
        stem = target_name.stem or "source"
        outtmpl = str(work_dir / f"{stem}.%(ext)s")

        ydl_opts: dict[str, Any] = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "max_filesize": self.max_bytes,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not isinstance(info, dict):
                    raise ProviderError("yt_dlp", f"No media metadata returned for URL: {url}")

                # Resolve the actual output path produced by yt-dlp / postprocessors
                candidates: list[Path] = []
                requested = info.get("requested_downloads") or []
                for entry in requested:
                    fp = entry.get("filepath") if isinstance(entry, dict) else None
                    if fp:
                        candidates.append(Path(fp))

                prepared = Path(ydl.prepare_filename(info))
                candidates.extend(
                    [
                        work_dir / filename,
                        prepared.with_suffix(".mp4"),
                        prepared,
                    ]
                )

                local_path: Path | None = next((p for p in candidates if p.is_file()), None)
                if local_path is None:
                    matched = sorted(
                        (p for p in work_dir.glob(f"{stem}.*") if p.is_file() and p.suffix != ".part"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if matched:
                        local_path = matched[0]

                if local_path is None:
                    raise ProviderError(
                        "yt_dlp",
                        f"yt-dlp finished without creating a media file in '{work_dir}'",
                    )

                desired_path = work_dir / filename
                if local_path.resolve() != desired_path.resolve() and local_path.suffix == desired_path.suffix:
                    shutil.move(str(local_path), str(desired_path))
                    local_path = desired_path

                self._enforce_size_limit(local_path)

                duration = info.get("duration")
                width = info.get("width")
                height = info.get("height")
                title = str(info.get("title") or stem).strip() or stem

                return DownloadedMedia(
                    local_path=local_path,
                    title=title,
                    duration_seconds=float(duration) if duration is not None else None,
                    width=int(width) if width is not None else None,
                    height=int(height) if height is not None else None,
                    source_url=url,
                )
        except (ValidationError, ConfigurationError, ProviderError):
            raise
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "File is larger than max-filesize" in msg or "max_filesize" in msg.lower():
                raise ValidationError(
                    f"Remote media exceeds MAX_UPLOAD_MB ({self.settings.MAX_UPLOAD_MB} MB)"
                ) from exc
            logger.exception("yt-dlp DownloadError for %s", url)
            raise ProviderError("yt_dlp", f"Failed to download media URL: {msg}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error during yt-dlp download for %s", url)
            raise ProviderError("yt_dlp", f"Media download failed: {exc}", retryable=True) from exc

    async def download_url(
        self,
        url: str,
        work_dir: Path,
        filename: str = "source.mp4",
    ) -> DownloadedMedia:
        """Download a remote video/audio URL into ``work_dir`` via ``yt-dlp``."""
        clean_url = _validate_http_url(url)
        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading source URL via yt-dlp -> %s", dest_dir / filename)
        media = await asyncio.to_thread(self._run_yt_dlp_sync, clean_url, dest_dir, filename)
        logger.info(
            "Downloaded media '%s' (%s, duration=%ss)",
            media.title,
            media.local_path.name,
            media.duration_seconds,
        )
        return media

    async def _download_http_stream(
        self,
        url: str,
        dest_path: Path,
    ) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise ProviderError(
                            "storage",
                            f"Failed to fetch staged media (HTTP {resp.status_code})",
                            retryable=resp.status_code >= 500,
                        )
                    content_length = resp.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
                        raise ValidationError(
                            f"Source file exceeds MAX_UPLOAD_MB ({self.settings.MAX_UPLOAD_MB} MB)"
                        )
                    with dest_path.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise ValidationError(
                                    f"Source file exceeds MAX_UPLOAD_MB ({self.settings.MAX_UPLOAD_MB} MB)"
                                )
                            fh.write(chunk)
        except (ValidationError, ProviderError):
            dest_path.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            dest_path.unlink(missing_ok=True)
            raise ProviderError(
                "storage",
                f"HTTP error while staging source file: {exc}",
                retryable=True,
            ) from exc

        self._enforce_size_limit(dest_path)
        return dest_path

    async def stage_source_media(
        self,
        source_url: str | None,
        source_file_key: str | None,
        work_dir: Path,
        storage: BaseStorageProvider | None = None,
    ) -> DownloadedMedia:
        """Stage source media from either a URL, local path, or object storage key."""
        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        clean_url = (source_url or "").strip()
        clean_key = (source_file_key or "").strip()

        if clean_url:
            return await self.download_url(clean_url, dest_dir, filename="source.mp4")

        if not clean_key:
            raise ValidationError("Either source_url or source_file_key must be provided")

        if clean_key.startswith("/local-media/"):
            local_src = resolve_local_media_url(
                clean_key,
                self.settings,
                base_dir=getattr(storage, "base_dir", None),
            )
            if local_src is None or not local_src.is_file():
                raise NotFoundError(
                    f"Local media file not found on disk: {clean_key}",
                    details={"source_file_key": clean_key},
                )
            ext = local_src.suffix or ".mp4"
            staged_path = dest_dir / f"source{ext}"
            if local_src.resolve() != staged_path.resolve():
                await asyncio.to_thread(shutil.copy2, local_src, staged_path)
            self._enforce_size_limit(staged_path)
            logger.info("Staged /local-media source %s -> %s", local_src, staged_path)
            return DownloadedMedia(
                local_path=staged_path,
                title=local_src.stem,
                source_url=None,
            )

        # Direct local filesystem path or file:// URI support
        direct_candidate = _file_uri_to_path(clean_key) if clean_key.startswith("file://") else Path(clean_key)
        if direct_candidate.is_file():
            ext = direct_candidate.suffix or ".mp4"
            staged_path = dest_dir / f"source{ext}"
            if direct_candidate.resolve() != staged_path.resolve():
                await asyncio.to_thread(shutil.copy2, direct_candidate, staged_path)
            self._enforce_size_limit(staged_path)
            logger.info("Staged local source file %s -> %s", direct_candidate, staged_path)
            return DownloadedMedia(
                local_path=staged_path,
                title=direct_candidate.stem,
                source_url=None,
            )

        if storage is None:
            raise NotFoundError(
                f"Source file not found locally and no storage provider was supplied: '{clean_key}'",
                details={"source_file_key": clean_key},
            )

        signed_url = await storage.get_signed_url(clean_key)
        ext = Path(clean_key).suffix or ".mp4"
        staged_path = dest_dir / f"source{ext}"

        if signed_url.startswith("file://"):
            local_src = _file_uri_to_path(signed_url)
            if not local_src.is_file():
                raise NotFoundError(
                    f"Staged storage object not found on disk: {local_src}",
                    details={"source_file_key": clean_key},
                )
            if local_src.resolve() != staged_path.resolve():
                await asyncio.to_thread(shutil.copy2, local_src, staged_path)
            self._enforce_size_limit(staged_path)
        elif signed_url.startswith("/local-media/"):
            local_src = resolve_local_media_url(
                signed_url,
                self.settings,
                base_dir=getattr(storage, "base_dir", None),
            )
            if local_src is None or not local_src.is_file():
                raise NotFoundError(
                    f"Staged storage object not found on disk: {signed_url}",
                    details={"source_file_key": clean_key},
                )
            if local_src.resolve() != staged_path.resolve():
                await asyncio.to_thread(shutil.copy2, local_src, staged_path)
            self._enforce_size_limit(staged_path)
        elif signed_url.startswith(("http://", "https://")):
            await self._download_http_stream(signed_url, staged_path)
        else:
            candidate = Path(signed_url)
            if not candidate.is_file():
                raise ProviderError(
                    storage.provider_name,
                    f"Unsupported signed URL format returned for '{clean_key}': {signed_url}",
                )
            await asyncio.to_thread(shutil.copy2, candidate, staged_path)
            self._enforce_size_limit(staged_path)

        logger.info("Staged source key '%s' via storage -> %s", clean_key, staged_path)
        return DownloadedMedia(
            local_path=staged_path,
            title=Path(clean_key).stem or "source",
            source_url=None,
        )
