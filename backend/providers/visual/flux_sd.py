"""AI image generation visual provider adapters (Flux and Stable Diffusion)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

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

_ASPECT_SPECS: dict[AspectRatio, tuple[str, int, int]] = {
    AspectRatio.PORTRAIT_9_16: ("9:16", 768, 1344),
    AspectRatio.LANDSCAPE_16_9: ("16:9", 1344, 768),
    AspectRatio.SQUARE_1_1: ("1:1", 1024, 1024),
}

_POLL_INTERVAL_SECONDS = 1.5
_POLL_TIMEOUT_SECONDS = 120.0


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_status(provider: str, response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ProviderAuthError(provider, f"Invalid or unauthorized {provider} API key")
    if status == 429:
        raise ProviderRateLimitError(provider, f"{provider} API rate limit exceeded")
    if status >= 500:
        raise ProviderError(provider, f"{provider} server error ({status})", retryable=True)
    raise ProviderError(
        provider,
        f"{provider} request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


def _extract_image_url_or_b64(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract either an output URL or a base64 image string from diverse API schemas."""
    output = payload.get("output")
    if isinstance(output, str) and output.startswith("http"):
        return output, None
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str):
            if first.startswith("http"):
                return first, None
            return None, first
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"]), None

    for key in ("images", "data", "artifacts"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                if first.get("url"):
                    return str(first["url"]), None
                for b64_key in ("b64_json", "base64", "image"):
                    if first.get(b64_key):
                        return None, str(first[b64_key])

    if isinstance(payload.get("image"), str):
        val = payload["image"]
        if val.startswith("http"):
            return val, None
        return None, val

    return None, None


class _BaseAIImageProvider(BaseVisualProvider):
    """Shared HTTP, polling, and file-saving logic for AI image generation providers."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any],
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=json_body)
        _raise_for_status(self.provider_name, response)
        return response

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _post_multipart(
        self,
        url: str,
        headers: dict[str, str],
        files: dict[str, tuple[str | None, str]],
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, files=files)
        _raise_for_status(self.provider_name, response)
        return response

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
        _raise_for_status(self.provider_name, response)
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _download_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            response = await client.get(url)
        _raise_for_status(self.provider_name, response)
        return response.content

    async def _poll_prediction(
        self,
        poll_url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POLL_TIMEOUT_SECONDS
        while loop.time() < deadline:
            data = await self._get_json(poll_url, headers)
            status = str(data.get("status") or "").lower()
            if status in ("succeeded", "completed", "done"):
                return data
            if status in ("failed", "canceled", "cancelled", "error"):
                err = data.get("error") or data.get("detail") or "Generation failed"
                raise ProviderError(self.provider_name, f"Prediction {status}: {err}")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        raise ProviderError(
            self.provider_name,
            f"Prediction polling timed out after {_POLL_TIMEOUT_SECONDS}s",
            retryable=True,
        )

    async def _finalize_image_asset(
        self,
        image_url: str | None,
        raw_bytes: bytes | None,
        output_dir: Path | None,
        width: int,
        height: int,
        model_name: str,
    ) -> VisualAsset:
        local_path: Path | None = None
        if output_dir is not None:
            dest_dir = Path(output_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            local_path = dest_dir / f"{self.provider_name}_{uuid4().hex[:12]}.png"
            if raw_bytes is None and image_url:
                raw_bytes = await self._download_bytes(image_url)
            if raw_bytes is not None:
                local_path.write_bytes(raw_bytes)
        elif raw_bytes is not None and not image_url:
            fallback_dir = self._settings.temp_path / self.provider_name
            fallback_dir.mkdir(parents=True, exist_ok=True)
            local_path = fallback_dir / f"{self.provider_name}_{uuid4().hex[:12]}.png"
            local_path.write_bytes(raw_bytes)

        final_url = image_url or (local_path.as_uri() if local_path else "")
        if not final_url:
            raise ProviderError(self.provider_name, "Provider returned no image URL or binary payload")

        return VisualAsset(
            url=final_url,
            local_path=local_path,
            media_type="image",
            source=VisualSource.AI_GEN,
            width=width,
            height=height,
            duration_seconds=None,
            attribution=f"AI Generated ({model_name})",
        )


class FluxVisualProvider(_BaseAIImageProvider):
    """Generate AI images via Flux (Replicate or compatible REST API)."""

    provider_name: str = "flux"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings)
        self._api_key = (self._settings.FLUX_API_KEY or "").strip()
        self._api_url = (self._settings.FLUX_API_URL or "").strip()
        self._model = (self._settings.FLUX_MODEL or "").strip()

    def _require_config(self) -> None:
        if not self._api_key:
            raise ConfigurationError("FLUX_API_KEY is required for FluxVisualProvider")
        if not self._api_url:
            raise ConfigurationError("FLUX_API_URL is required for FluxVisualProvider")

    async def fetch_visual(
        self,
        query: str,
        prompt: str | None = None,
        aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16,
        media_type: Literal["video", "image"] = "image",
        output_dir: Path | None = None,
    ) -> VisualAsset:
        """Generate an image matching ``aspect_ratio`` using Flux."""
        self._require_config()
        prompt_text = (prompt or query or "").strip()
        if not prompt_text:
            raise ProviderError("flux", "Prompt or query must not be empty")

        aspect_str, width, height = _ASPECT_SPECS[aspect_ratio]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        is_replicate = "replicate.com" in self._api_url.lower()
        target_url = self._api_url
        if is_replicate:
            headers["Prefer"] = "wait"
            if ":" in self._model:
                _, version = self._model.split(":", 1)
                payload: dict[str, Any] = {
                    "version": version,
                    "input": {
                        "prompt": prompt_text,
                        "aspect_ratio": aspect_str,
                        "output_format": "png",
                    },
                }
            else:
                if self._api_url.rstrip("/") == "https://api.replicate.com/v1/predictions" and "/" in self._model:
                    target_url = f"https://api.replicate.com/v1/models/{self._model}/predictions"
                payload = {
                    "input": {
                        "prompt": prompt_text,
                        "aspect_ratio": aspect_str,
                        "output_format": "png",
                    }
                }
        else:
            payload = {
                "model": self._model,
                "prompt": prompt_text,
                "aspect_ratio": aspect_str,
                "width": width,
                "height": height,
            }

        logger.info("Generating Flux image model=%s aspect=%s", self._model, aspect_str)

        try:
            response = await self._post_json(target_url, headers, payload)
            data = response.json()

            status = str(data.get("status") or "").lower()
            poll_url = (data.get("urls") or {}).get("get") or data.get("status_url")
            if status in ("starting", "processing", "queued", "in_progress") and poll_url:
                data = await self._poll_prediction(str(poll_url), headers)
            elif status in ("failed", "canceled", "cancelled", "error"):
                raise ProviderError("flux", f"Flux generation {status}: {data.get('error')}")

            image_url, b64_data = _extract_image_url_or_b64(data)
            raw_bytes = base64.b64decode(b64_data) if b64_data else None
            return await self._finalize_image_asset(
                image_url=image_url,
                raw_bytes=raw_bytes,
                output_dir=output_dir,
                width=width,
                height=height,
                model_name=self._model or "flux",
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("flux", f"Flux request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("flux", f"Flux HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in FluxVisualProvider.fetch_visual")
            raise ProviderError("flux", f"Unexpected Flux error: {exc}") from exc


class StableDiffusionProvider(_BaseAIImageProvider):
    """Generate AI images via Stable Diffusion (Stability AI v2beta or compatible API)."""

    provider_name: str = "stable_diffusion"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings)
        self._api_key = (self._settings.SD_API_KEY or "").strip()
        self._api_url = (self._settings.SD_API_URL or "").strip()
        self._model = (self._settings.SD_MODEL or "").strip()

    def _require_config(self) -> None:
        if not self._api_key:
            raise ConfigurationError("SD_API_KEY is required for StableDiffusionProvider")
        if not self._api_url:
            raise ConfigurationError("SD_API_URL is required for StableDiffusionProvider")

    async def fetch_visual(
        self,
        query: str,
        prompt: str | None = None,
        aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16,
        media_type: Literal["video", "image"] = "image",
        output_dir: Path | None = None,
    ) -> VisualAsset:
        """Generate an image matching ``aspect_ratio`` using Stable Diffusion."""
        self._require_config()
        prompt_text = (prompt or query or "").strip()
        if not prompt_text:
            raise ProviderError("stable_diffusion", "Prompt or query must not be empty")

        aspect_str, width, height = _ASPECT_SPECS[aspect_ratio]
        logger.info("Generating Stable Diffusion image model=%s aspect=%s", self._model, aspect_str)

        try:
            if "stability.ai" in self._api_url.lower():
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                }
                files: dict[str, tuple[str | None, str]] = {
                    "prompt": (None, prompt_text),
                    "aspect_ratio": (None, aspect_str),
                    "output_format": (None, "png"),
                }
                if self._model:
                    files["model"] = (None, self._model)
                response = await self._post_multipart(self._api_url, headers, files)
            else:
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                }
                payload: dict[str, Any] = {
                    "model": self._model,
                    "prompt": prompt_text,
                    "aspect_ratio": aspect_str,
                    "width": width,
                    "height": height,
                }
                response = await self._post_json(self._api_url, headers, payload)

            content_type = response.headers.get("content-type", "").lower()
            if content_type.startswith("image/"):
                return await self._finalize_image_asset(
                    image_url=None,
                    raw_bytes=response.content,
                    output_dir=output_dir,
                    width=width,
                    height=height,
                    model_name=self._model or "stable-diffusion",
                )

            data = response.json()
            status = str(data.get("status") or "").lower()
            poll_url = (data.get("urls") or {}).get("get") or data.get("status_url")
            if status in ("starting", "processing", "queued", "in_progress") and poll_url:
                data = await self._poll_prediction(str(poll_url), {"Authorization": f"Bearer {self._api_key}"})
            elif status in ("failed", "canceled", "cancelled", "error"):
                raise ProviderError(
                    "stable_diffusion",
                    f"Stable Diffusion generation {status}: {data.get('error')}",
                )

            image_url, b64_data = _extract_image_url_or_b64(data)
            raw_bytes = base64.b64decode(b64_data) if b64_data else None
            return await self._finalize_image_asset(
                image_url=image_url,
                raw_bytes=raw_bytes,
                output_dir=output_dir,
                width=width,
                height=height,
                model_name=self._model or "stable-diffusion",
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "stable_diffusion",
                f"Stable Diffusion request timed out: {exc}",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "stable_diffusion",
                f"Stable Diffusion HTTP error: {exc}",
                retryable=True,
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error in StableDiffusionProvider.fetch_visual")
            raise ProviderError("stable_diffusion", f"Unexpected Stable Diffusion error: {exc}") from exc
