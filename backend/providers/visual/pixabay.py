"""Pixabay stock video and image visual provider adapter."""

from __future__ import annotations

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

_PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"
_PIXABAY_IMAGE_SEARCH_URL = "https://pixabay.com/api/"

_ORIENTATION_MAP: dict[AspectRatio, str] = {
    AspectRatio.PORTRAIT_9_16: "vertical",
    AspectRatio.LANDSCAPE_16_9: "horizontal",
    AspectRatio.SQUARE_1_1: "all",
}


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_pixabay_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    body_lower = response.text.lower()
    if status in (401, 403) or (status == 400 and "api key" in body_lower):
        raise ProviderAuthError("pixabay", "Invalid or unauthorized Pixabay API key")
    if status == 429:
        raise ProviderRateLimitError("pixabay", "Pixabay API rate limit exceeded")
    if status >= 500:
        raise ProviderError(
            "pixabay",
            f"Pixabay server error ({status})",
            retryable=True,
        )
    raise ProviderError(
        "pixabay",
        f"Pixabay request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


def _orientation_score(width: int | None, height: int | None, aspect_ratio: AspectRatio) -> int:
    if not width or not height:
        return 0
    if aspect_ratio == AspectRatio.PORTRAIT_9_16:
        return 2 if height > width else (1 if height == width else 0)
    if aspect_ratio == AspectRatio.LANDSCAPE_16_9:
        return 2 if width > height else (1 if height == width else 0)
    ratio = min(width, height) / max(width, height)
    if ratio >= 0.85:
        return 2
    return 1 if ratio >= 0.65 else 0


def _select_best_video_variant(hit: dict[str, Any]) -> dict[str, Any] | None:
    videos = hit.get("videos") or {}
    for quality in ("large", "medium", "small", "tiny"):
        variant = videos.get(quality)
        if isinstance(variant, dict) and variant.get("url"):
            return variant
    return None


def _select_best_video_hit(
    hits: list[dict[str, Any]],
    aspect_ratio: AspectRatio,
) -> tuple[dict[str, Any], dict[str, Any]]:
    best_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_rank = (-1, -1)
    for hit in hits:
        variant = _select_best_video_variant(hit)
        if not variant:
            continue
        w = int(variant.get("width") or 0)
        h = int(variant.get("height") or 0)
        rank = (_orientation_score(w, h, aspect_ratio), min(max(w, h), 1920))
        if rank > best_rank:
            best_rank = rank
            best_pair = (hit, variant)

    if best_pair is None:
        raise ProviderError("pixabay", "Pixabay returned videos without downloadable URLs")
    return best_pair


def _select_best_image_hit(
    hits: list[dict[str, Any]],
    aspect_ratio: AspectRatio,
) -> dict[str, Any]:
    return max(
        hits,
        key=lambda h: (
            _orientation_score(h.get("imageWidth"), h.get("imageHeight"), aspect_ratio),
            min(int(h.get("imageWidth") or 0), 2048),
        ),
    )


class PixabayProvider(BaseVisualProvider):
    """Fetch stock videos and images from the Pixabay API."""

    provider_name: str = "pixabay"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._api_key = (self._settings.PIXABAY_API_KEY or "").strip()

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ConfigurationError("PIXABAY_API_KEY is required for PixabayProvider")
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
        _raise_for_pixabay_status(response)
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
                        "pixabay",
                        f"Pixabay media download failed ({response.status_code})",
                        retryable=True,
                    )
                if response.status_code >= 400:
                    raise ProviderError(
                        "pixabay",
                        f"Pixabay media download failed ({response.status_code})",
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
        """Search Pixabay for stock video or image and optionally download it."""
        search_query = (query or prompt or "").strip()
        if not search_query:
            raise ProviderError("pixabay", "Search query must not be empty")

        api_key = self._require_api_key()
        orientation = _ORIENTATION_MAP[aspect_ratio]
        endpoint = _PIXABAY_VIDEO_SEARCH_URL if media_type == "video" else _PIXABAY_IMAGE_SEARCH_URL
        params: dict[str, Any] = {
            "key": api_key,
            "q": search_query,
            "orientation": orientation,
            "per_page": 10,
            "safesearch": "true",
        }
        if media_type == "image":
            params["image_type"] = "photo"

        logger.info(
            "Searching Pixabay media_type=%s orientation=%s query=%r",
            media_type,
            orientation,
            search_query,
        )

        try:
            payload = await self._search_api(endpoint, params)
            hits = payload.get("hits") or []
            if not hits:
                raise ProviderError(
                    "pixabay",
                    f"No Pixabay {media_type}s found for query '{search_query}'",
                )

            if media_type == "video":
                hit, variant = _select_best_video_hit(hits, aspect_ratio)
                media_url = str(variant["url"])
                width = variant.get("width")
                height = variant.get("height")
                duration = hit.get("duration")
                creator = hit.get("user") or "Unknown"
                asset_id = hit.get("id") or "video"
                ext = "mp4"
            else:
                hit = _select_best_image_hit(hits, aspect_ratio)
                media_url = str(
                    hit.get("largeImageURL")
                    or hit.get("fullHDURL")
                    or hit.get("webformatURL")
                    or ""
                )
                if not media_url:
                    raise ProviderError("pixabay", "Pixabay image result missing URL")
                width = hit.get("imageWidth")
                height = hit.get("imageHeight")
                duration = None
                creator = hit.get("user") or "Unknown"
                asset_id = hit.get("id") or "image"
                ext = "png" if ".png" in media_url.lower() else "jpg"

            local_path: Path | None = None
            if output_dir is not None:
                dest_path = Path(output_dir) / f"pixabay_{media_type}_{asset_id}.{ext}"
                local_path = await self._download_asset(media_url, dest_path)

            return VisualAsset(
                url=media_url,
                local_path=local_path,
                media_type=media_type,
                source=VisualSource.PIXABAY,
                width=int(width) if width is not None else None,
                height=int(height) if height is not None else None,
                duration_seconds=float(duration) if duration is not None else None,
                attribution=f"Pixabay / {creator}",
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("pixabay", f"Pixabay request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("pixabay", f"Pixabay HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in PixabayProvider.fetch_visual")
            raise ProviderError("pixabay", f"Unexpected Pixabay error: {exc}") from exc
