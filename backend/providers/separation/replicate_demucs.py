"""Cloud Replicate Demucs and optional Local Demucs vocal/BGM separation adapters."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

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
from backend.core.subprocess_utils import run_subprocess_exec
from backend.models.provider import SeparationResult
from backend.providers.base.separation import BaseSeparationProvider

logger = get_logger(__name__)

_REPLICATE_PREDICTIONS_URL = "https://api.replicate.com/v1/predictions"
_REPLICATE_FILES_URL = "https://api.replicate.com/v1/files"
_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus"}
)
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 300.0
_INLINE_BASE64_LIMIT_BYTES = 8 * 1024 * 1024


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_replicate_status(provider: str, response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ProviderAuthError(provider, "Invalid or unauthorized Replicate API token")
    if status == 429:
        raise ProviderRateLimitError(provider, "Replicate API rate limit exceeded")
    if status >= 500:
        raise ProviderError(provider, f"Replicate server error ({status})", retryable=True)
    raise ProviderError(
        provider,
        f"Replicate request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


async def _ensure_source_audio(
    media_path: Path,
    work_dir: Path,
    ffmpeg_bin: str,
    provider_name: str,
) -> Path:
    """Return an audio path directly if already audio, or extract WAV via FFmpeg."""
    if media_path.suffix.lower() in _AUDIO_EXTENSIONS:
        if media_path.stat().st_size == 0:
            raise ProviderError(provider_name, f"Input audio file is empty: {media_path}")
        return media_path

    output_wav = work_dir / "source_audio.wav"
    try:
        returncode, _, stderr = await run_subprocess_exec(
            ffmpeg_bin,
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(output_wav),
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"FFmpeg binary '{ffmpeg_bin}' not found; install FFmpeg or set FFMPEG_BIN"
        ) from exc

    if returncode != 0 or not output_wav.exists() or output_wav.stat().st_size == 0:
        err_msg = stderr.decode("utf-8", errors="ignore").strip()[-400:]
        raise ProviderError(
            provider_name,
            f"Failed to extract audio from {media_path.name}: {err_msg}",
        )
    return output_wav


class ReplicateDemucsProvider(BaseSeparationProvider):
    """Separate vocals and accompaniment tracks using Replicate's Demucs API."""

    provider_name: str = "replicate"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._api_token = (self._settings.REPLICATE_API_TOKEN or "").strip()
        self._model = (self._settings.REPLICATE_DEMUCS_MODEL or "").strip()
        self._ffmpeg_bin = self._settings.FFMPEG_BIN or "ffmpeg"

    def _require_token(self) -> str:
        if not self._api_token:
            raise ConfigurationError("REPLICATE_API_TOKEN is required for ReplicateDemucsProvider")
        return self._api_token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _upload_file_to_replicate(self, audio_path: Path) -> str:
        headers = {"Authorization": f"Bearer {self._require_token()}"}
        mime_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
        async with httpx.AsyncClient(timeout=120.0) as client:
            with audio_path.open("rb") as fh:
                response = await client.post(
                    _REPLICATE_FILES_URL,
                    headers=headers,
                    files={"content": (audio_path.name, fh, mime_type)},
                )
        _raise_for_replicate_status(self.provider_name, response)
        data = response.json()
        file_url = (data.get("urls") or {}).get("get") or data.get("url")
        if not file_url:
            raise ProviderError(self.provider_name, "Replicate file upload response missing URL")
        return str(file_url)

    async def _prepare_audio_input(self, audio_path: Path) -> str:
        if audio_path.stat().st_size <= _INLINE_BASE64_LIMIT_BYTES:
            mime_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
            encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
        return await self._upload_file_to_replicate(audio_path)

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _create_prediction(self, audio_input: str) -> dict[str, Any]:
        version = self._model.split(":", 1)[1] if ":" in self._model else self._model
        payload: dict[str, Any] = {
            "version": version,
            "input": {
                "audio": audio_input,
                "stem": "vocals",
            },
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                _REPLICATE_PREDICTIONS_URL,
                headers=self._auth_headers(),
                json=payload,
            )
        _raise_for_replicate_status(self.provider_name, response)
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _get_prediction(self, poll_url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(poll_url, headers=self._auth_headers())
        _raise_for_replicate_status(self.provider_name, response)
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _download_track(self, url: str, dest_path: Path) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                _raise_for_replicate_status(self.provider_name, response)
                with dest_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
        return dest_path

    async def _poll_until_complete(self, prediction: dict[str, Any]) -> dict[str, Any]:
        status = str(prediction.get("status") or "").lower()
        poll_url = str((prediction.get("urls") or {}).get("get") or "")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POLL_TIMEOUT_SECONDS

        current = prediction
        while status in ("starting", "processing", "queued") and loop.time() < deadline:
            if not poll_url:
                raise ProviderError(self.provider_name, "Replicate prediction missing polling URL")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            current = await self._get_prediction(poll_url)
            status = str(current.get("status") or "").lower()

        if status == "succeeded":
            return current
        if status in ("failed", "canceled", "cancelled"):
            err = current.get("error") or "Demucs prediction failed"
            raise ProviderError(self.provider_name, f"Replicate prediction {status}: {err}")
        raise ProviderError(
            self.provider_name,
            f"Replicate Demucs timed out after {_POLL_TIMEOUT_SECONDS}s (status={status})",
            retryable=True,
        )

    async def separate(self, media_path: Path, work_dir: Path) -> SeparationResult:
        """Separate vocals and BGM via Replicate Demucs and download stems into ``work_dir``."""
        self._require_token()
        src_path = Path(media_path)
        if not src_path.exists():
            raise ProviderError(self.provider_name, f"Input media file not found: {src_path}")

        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Starting Replicate Demucs separation for %s", src_path.name)

        try:
            source_audio_path = await _ensure_source_audio(
                src_path, dest_dir, self._ffmpeg_bin, self.provider_name
            )
            audio_input = await self._prepare_audio_input(source_audio_path)
            prediction = await self._create_prediction(audio_input)
            completed = await self._poll_until_complete(prediction)

            output = completed.get("output")
            if not isinstance(output, dict):
                raise ProviderError(
                    self.provider_name,
                    f"Unexpected Replicate Demucs output format: {type(output).__name__}",
                )

            vocals_url = output.get("vocals")
            bgm_url = (
                output.get("no_vocals")
                or output.get("accompaniment")
                or output.get("other")
            )
            if not vocals_url:
                raise ProviderError(self.provider_name, "Replicate Demucs output missing 'vocals' stem")

            vocals_ext = Path(str(vocals_url).split("?")[0]).suffix or ".wav"
            vocals_path = await self._download_track(
                str(vocals_url),
                dest_dir / f"vocals{vocals_ext}",
            )

            bgm_path: Path | None = None
            if bgm_url:
                bgm_ext = Path(str(bgm_url).split("?")[0]).suffix or ".wav"
                bgm_path = await self._download_track(
                    str(bgm_url),
                    dest_dir / f"bgm{bgm_ext}",
                )

            return SeparationResult(
                vocals_path=vocals_path,
                bgm_path=bgm_path,
                source_audio_path=source_audio_path,
                mode=self.provider_name,
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(self.provider_name, f"Replicate timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider_name, f"Replicate HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in ReplicateDemucsProvider.separate")
            raise ProviderError(self.provider_name, f"Unexpected Replicate error: {exc}") from exc


class LocalDemucsProvider(BaseSeparationProvider):
    """Optional local Demucs vocal/BGM separation adapter (requires ``demucs`` + ``torch``)."""

    provider_name: str = "local_demucs"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._ffmpeg_bin = self._settings.FFMPEG_BIN or "ffmpeg"

    @staticmethod
    def _import_demucs() -> Any:
        try:
            import demucs.separate  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigurationError(
                "LocalDemucsProvider requires the optional 'demucs' and 'torch' packages. "
                "Install them with `pip install demucs torch` or set SEPARATION_PROVIDER=replicate / fast_mode."
            ) from exc
        return demucs.separate

    async def separate(self, media_path: Path, work_dir: Path) -> SeparationResult:
        """Run local Demucs two-stem separation (`vocals` / `no_vocals`) in a worker thread."""
        demucs_separate = self._import_demucs()

        src_path = Path(media_path)
        if not src_path.exists():
            raise ProviderError(self.provider_name, f"Input media file not found: {src_path}")

        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        demucs_out = dest_dir / "demucs_out"
        demucs_out.mkdir(parents=True, exist_ok=True)

        logger.info("Starting LocalDemucsProvider separation for %s", src_path.name)

        try:
            source_audio_path = await _ensure_source_audio(
                src_path, dest_dir, self._ffmpeg_bin, self.provider_name
            )
            args = [
                "--two-stems",
                "vocals",
                "-n",
                "htdemucs",
                "-o",
                str(demucs_out),
                str(source_audio_path),
            ]
            await asyncio.to_thread(demucs_separate.main, args)

            stem_dir = demucs_out / "htdemucs" / source_audio_path.stem
            vocals_path = stem_dir / "vocals.wav"
            bgm_path = stem_dir / "no_vocals.wav"

            if not vocals_path.exists():
                found_vocals = list(demucs_out.rglob("vocals.*"))
                if not found_vocals:
                    raise ProviderError(
                        self.provider_name,
                        "Local Demucs completed without producing a vocals track",
                    )
                vocals_path = found_vocals[0]

            if not bgm_path.exists():
                found_bgm = list(demucs_out.rglob("no_vocals.*"))
                bgm_path = found_bgm[0] if found_bgm else None

            return SeparationResult(
                vocals_path=vocals_path,
                bgm_path=bgm_path,
                source_audio_path=source_audio_path,
                mode=self.provider_name,
            )
        except (ConfigurationError, ProviderError):
            raise
        except Exception as exc:
            logger.exception("Unexpected error in LocalDemucsProvider.separate")
            raise ProviderError(
                self.provider_name,
                f"Local Demucs separation failed: {exc}",
            ) from exc
