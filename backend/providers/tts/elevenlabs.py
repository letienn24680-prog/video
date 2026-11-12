"""ElevenLabs premium multilingual TTS provider adapter."""

from __future__ import annotations

import asyncio
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
from backend.models.provider import AudioSynthesisResult, VoiceOption
from backend.providers.base.tts import BaseTTSProvider

logger = get_logger(__name__)


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_elevenlabs_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ProviderAuthError("elevenlabs", "Invalid or unauthorized ElevenLabs API key")
    if status == 429:
        raise ProviderRateLimitError("elevenlabs", "ElevenLabs API rate limit exceeded")
    if status >= 500:
        raise ProviderError(
            "elevenlabs",
            f"ElevenLabs server error ({status})",
            retryable=True,
        )
    raise ProviderError(
        "elevenlabs",
        f"ElevenLabs request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


async def _probe_duration_ms(audio_path: Path, ffprobe_bin: str) -> int:
    """Probe audio duration in milliseconds via ffprobe with size-based MP3 fallback."""
    try:
        returncode, stdout, _ = await run_subprocess_exec(
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        )
        if returncode == 0 and stdout:
            seconds = float(stdout.decode("utf-8", errors="ignore").strip())
            if seconds > 0:
                return int(round(seconds * 1000))
    except (FileNotFoundError, OSError, ValueError):
        pass

    # Fallback estimate for 128 kbps MP3 (16,000 bytes/sec)
    size_bytes = audio_path.stat().st_size if audio_path.exists() else 0
    return int(round((size_bytes / 16000.0) * 1000)) if size_bytes > 0 else 0


class ElevenLabsProvider(BaseTTSProvider):
    """Synthesize speech and list voices via the ElevenLabs REST API."""

    provider_name: str = "elevenlabs"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._api_key = (self._settings.ELEVENLABS_API_KEY or "").strip()
        self._base_url = (self._settings.ELEVENLABS_BASE_URL or "https://api.elevenlabs.io/v1").rstrip("/")
        self._model_id = (self._settings.ELEVENLABS_MODEL_ID or "eleven_multilingual_v2").strip()
        self._default_voice_id = (
            self._settings.ELEVENLABS_DEFAULT_VOICE_ID or "21m00Tcm4TlvDq8ikWAM"
        ).strip()
        self._ffprobe_bin = self._settings.FFPROBE_BIN or "ffprobe"

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ConfigurationError("ELEVENLABS_API_KEY is required for ElevenLabsProvider")
        return self._api_key

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _request_tts(
        self,
        voice_id: str,
        payload: dict[str, Any],
    ) -> bytes:
        api_key = self._require_api_key()
        url = f"{self._base_url}/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"output_format": "mp3_44100_128"},
                json=payload,
            )
        _raise_for_elevenlabs_status(response)
        return response.content

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _request_voices(self) -> dict[str, Any]:
        api_key = self._require_api_key()
        url = f"{self._base_url}/voices"
        headers = {
            "xi-api-key": api_key,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
        _raise_for_elevenlabs_status(response)
        return response.json()

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        rate_pct: int = 0,
    ) -> AudioSynthesisResult:
        """Synthesize text to an MP3 file at ``output_path`` and probe its duration."""
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ProviderError("elevenlabs", "Text for synthesis must not be empty")

        resolved_voice = (voice_id or "").strip() or self._default_voice_id
        clamped_rate = max(-25, min(25, int(rate_pct)))
        # ElevenLabs speed range is 0.7..1.2
        speed = round(max(0.7, min(1.2, 1.0 + clamped_rate / 100.0)), 2)

        payload: dict[str, Any] = {
            "text": cleaned_text,
            "model_id": self._model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": speed,
            },
        }

        logger.info(
            "Synthesizing ElevenLabs TTS voice=%s rate_pct=%d model=%s",
            resolved_voice,
            clamped_rate,
            self._model_id,
        )

        try:
            audio_bytes = await self._request_tts(resolved_voice, payload)
            if not audio_bytes:
                raise ProviderError("elevenlabs", "ElevenLabs returned empty audio payload")

            dest_path = Path(output_path)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(audio_bytes)

            duration_ms = await _probe_duration_ms(dest_path, self._ffprobe_bin)
            return AudioSynthesisResult(
                audio_path=dest_path,
                duration_ms=duration_ms,
                voice_id=resolved_voice,
                rate_pct=clamped_rate,
                provider=self.provider_name,
            )
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("elevenlabs", f"ElevenLabs request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("elevenlabs", f"ElevenLabs HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in ElevenLabsProvider.synthesize")
            raise ProviderError("elevenlabs", f"Unexpected ElevenLabs error: {exc}") from exc

    async def list_voices(self, language: str | None = None) -> list[VoiceOption]:
        """List available voices from ElevenLabs ``GET /voices``."""
        try:
            data = await self._request_voices()
            raw_voices = data.get("voices") or []
            lang_filter = (language or "").strip().lower()
            lang_prefix = lang_filter.split("-")[0] if lang_filter else ""

            options: list[VoiceOption] = []
            for item in raw_voices:
                vid = str(item.get("voice_id") or "").strip()
                if not vid:
                    continue
                name = str(item.get("name") or vid)
                labels = item.get("labels") or {}
                gender = labels.get("gender")
                voice_lang = str(
                    labels.get("language") or labels.get("accent") or "multilingual"
                )
                verified_langs = [
                    str(v.get("locale") or v.get("language") or "").lower()
                    for v in (item.get("verified_languages") or [])
                    if isinstance(v, dict)
                ]

                if lang_filter:
                    matches_label = lang_filter in voice_lang.lower() or (
                        lang_prefix and voice_lang.lower().startswith(lang_prefix)
                    )
                    matches_verified = any(
                        lang_filter in vl or (lang_prefix and vl.startswith(lang_prefix))
                        for vl in verified_langs
                    )
                    if not (matches_label or matches_verified or voice_lang == "multilingual"):
                        continue

                options.append(
                    VoiceOption(
                        voice_id=vid,
                        name=name,
                        language=voice_lang,
                        gender=str(gender) if gender else None,
                        provider=self.provider_name,
                    )
                )
            return options
        except (ConfigurationError, ProviderError):
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("elevenlabs", f"ElevenLabs voices timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("elevenlabs", f"ElevenLabs voices HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in ElevenLabsProvider.list_voices")
            raise ProviderError("elevenlabs", f"Unexpected ElevenLabs error: {exc}") from exc
