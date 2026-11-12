"""Pexels stock video and image visual provider adapter."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Literal

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio, VisualSource
from backend.models.provider import VisualAsset
from backend.providers.base.visual import BaseVisualProvider

logger = get_logger(__name__)

_PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
_PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"

_ORIENTATION_MAP: dict[AspectRatio, str] = {
    AspectRatio.PORTRAIT_9_16: "portrait",
    AspectRatio.LANDSCAPE_16_9: "landscape",
    AspectRatio.SQUARE_1_1: "square",
}


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_pexels_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ProviderAuthError("pexels", "Invalid or unauthorized Pexels API key")
    if status == 429:
        raise ProviderRateLimitError("pexels", "Pexels API rate limit exceeded")
    if status >= 500:
        raise ProviderError(
            "pexels",
            f"Pexels server error ({status})",
            retryable=True,
        )
    raise ProviderError(
        "pexels",
        f"Pexels request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


def _orientation_score(width: int | None, height: int | None, aspect_ratio: AspectRatio) -> int:
    if not width or not height:
        return 0
    if aspect_ratio == AspectRatio.PORTRAIT_9_16:
        return 2 if height > width else (1 if height == width else 0)
    if aspect_ratio == AspectRatio.LANDSCAPE_16_9:
        return 2 if width > height else (1 if height == width else 0)
    # Square: prefer near-1:1 ratio
    ratio = min(width, height) / max(width, height)
    if ratio >= 0.85:
        return 2
    return 1 if ratio >= 0.65 else 0


def _select_best_video_file(
    video: dict[str, Any],
    aspect_ratio: AspectRatio,
) -> dict[str, Any] | None:
    files = [f for f in video.get("video_files", []) if f.get("link")]
    if not files:
        return None

    def _score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        orient = _orientation_score(w, h, aspect_ratio)
        is_mp4 = 1 if item.get("file_type") == "video/mp4" else 0
        max_dim = max(w, h)
        # Prefer HD (720p-1920p) over tiny previews or overly massive 4K streams.
        is_ideal_hd = 1 if 720 <= max_dim <= 1920 else 0
        is_hd_tag = 1 if item.get("quality") == "hd" else 0
        return (orient, is_mp4, is_ideal_hd, is_hd_tag * 10_000 + min(max_dim, 1920))

    return max(files, key=_score)


def _select_best_video(
    videos: list[dict[str, Any]],
    aspect_ratio: AspectRatio,
) -> tuple[dict[str, Any], dict[str, Any]]:
    best_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_rank = (-1, -1)
    for video in videos:
        file_entry = _select_best_video_file(video, aspect_ratio)
        if not file_entry:
            continue
        w = int(file_entry.get("width") or video.get("width") or 0)
        h = int(file_entry.get("height") or video.get("height") or 0)
        rank = (_orientation_score(w, h, aspect_ratio), min(max(w, h), 1920))
        if rank > best_rank:
            best_rank = rank
            best_pair = (video, file_entry)

    if best_pair is None:
        raise ProviderError("pexels", "Pexels returned videos without downloadable media links")
    return best_pair


def _select_best_photo(
    photos: list[dict[str, Any]],
    aspect_ratio: AspectRatio,
) -> dict[str, Any]:
    return max(
        photos,
        key=lambda p: (
            _orientation_score(p.get("width"), p.get("height"), aspect_ratio),
            min(int(p.get("width") or 0), 2048),
        ),
    )


class PexelsProvider(BaseVisualProvider):
    """Fetch stock videos and photos from the Pexels API."""

    provider_name: str = "pexels"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._api_key = (self._settings.PEXELS_API_KEY or "").strip()

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ConfigurationError("PEXELS_API_KEY is required for PexelsProvider")
        return self._api_key

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _search_api(
        self,
        url: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        api_key = self._require_api_key()
        headers = {"Authorization": api_key}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
        _raise_for_pexels_status(response)
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _download_asset(self, url: str, dest_path: Path) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 500:
                    raise ProviderError(
                        "pexels",
                        f"Pexels media download failed ({response.status_code})",
                        retryable=True,
                    )
                if response.status_code >= 400:
                    raise ProviderError(
                        "pexels",
                        f"Pexels media download failed ({response.status_code})",
                        retryable=False,
                    )
                with dest_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
        return dest_path

    async def fetch_visual(
        self,
        query: str,
        prompt: str | None = None,
        aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16,
        media_type: Literal["video", "image"] = "video",
        output_dir: Path | None = None,
    ) -> VisualAsset:
        """Search Pexels for stock video or photo and optionally download it."""
        search_query = (query or prompt or "").strip()
        if not search_query:
            raise ProviderError("pexels", "Search query must not be empty")

        orientation = _ORIENTATION_MAP[aspect_ratio]
        endpoint = _PEXELS_VIDEO_SEARCH_URL if media_type == "video" else _PEXELS_PHOTO_SEARCH_URL
        params = {
            "query": search_query,
            "orientation": orientation,
            "per_page": 10,
        }

        logger.info(
            "Searching Pexels media_type=%s orientation=%s query=%r",
            media_type,
            orientation,
            search_query,
        )

        try:
            payload = await self._search_api(endpoint, params)
            if media_type == "video":
                videos = payload.get("videos") or []
                if not videos:
                    raise ProviderError(
                        "pexels",
                        f"No Pexels videos found for query '{search_query}'",
                    )
                video, file_entry = _select_best_video(videos, aspect_ratio)
                media_url = str(file_entry["link"])
                width = file_entry.get("width") or video.get("width")
                height = file_entry.get("height") or video.get("height")
                duration = video.get("duration")
                creator = (video.get("user") or {}).get("name") or "Unknown"
                asset_id = video.get("id") or "video"
                ext = "mp4"
            else:
                photos = payload.get("photos") or []
                if not photos:
                    raise ProviderError(
                        "pexels",
                        f"No Pexels images found for query '{search_query}'",
                    )
                photo = _select_best_photo(photos, aspect_ratio)
                src = photo.get("src") or {}
                media_url = str(
                    src.get("large2x") or src.get("original") or src.get("large") or ""
                )
                if not media_url:
                    raise ProviderError("pexels", "Pexels photo result missing image URL")
                width = photo.get("width")
                height = photo.get("height")
                duration = None
                creator = photo.get("photographer") or "Unknown"
                asset_id = photo.get("id") or "photo"
                guessed_ext = mimetypes.guess_extension("image/jpeg") or ".jpg"
                ext = "png" if ".png" in media_url.lower() else guessed_ext.lstrip(".")

            local_path: Path | None = None
            if output_dir is not None:
                dest_path = Path(output_dir) / f"pexels_{media_type}_{asset_id}.{ext}"
                local_path = await self._download_asset(media_url, dest_path)

            return VisualAsset(
                url=media_url,
                local_path=local_path,
                media_type=media_type,
                source=VisualSource.PEXELS,
                width=int(width) if width is not None else None,
                height=int(height) if height is not None else None,
                duration_seconds=float(duration) if duration is not None else None,
                attribution=f"Pexels / {creator}",
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("pexels", f"Pexels request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("pexels", f"Pexels HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in PexelsProvider.fetch_visual")
            raise ProviderError("pexels", f"Unexpected Pexels error: {exc}") from exc
