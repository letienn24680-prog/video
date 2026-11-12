"""Vietnamese premium TTS provider adapters (FPT.AI and Viettel AI)."""

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

_FPT_POLL_INTERVAL_SECONDS = 1.0
_FPT_POLL_TIMEOUT_SECONDS = 45.0

_FPT_VOICES: tuple[tuple[str, str, str], ...] = (
    ("banmai", "Ban Mai (Nữ miền Bắc)", "female"),
    ("lannhi", "Lan Nhi (Nữ miền Nam)", "female"),
    ("leminh", "Lê Minh (Nam miền Bắc)", "male"),
    ("myan", "Mỹ An (Nữ miền Trung)", "female"),
    ("thuminh", "Thu Minh (Nữ miền Bắc)", "female"),
    ("giahuy", "Gia Huy (Nam miền Trung)", "male"),
    ("ngoclam", "Ngọc Lam (Nữ miền Trung)", "female"),
    ("linhsan", "Linh San (Nữ miền Nam)", "female"),
    ("minhquang", "Minh Quang (Nam miền Nam)", "male"),
)

_VIETTEL_VOICES: tuple[tuple[str, str, str], ...] = (
    ("hn-quynhanh", "Quỳnh Anh (Nữ Hà Nội)", "female"),
    ("hn-phuongtrang", "Phương Trang (Nữ Hà Nội)", "female"),
    ("hn-thaochi", "Thảo Chi (Nữ Hà Nội)", "female"),
    ("hn-thanhha", "Thanh Hà (Nữ Hà Nội)", "female"),
    ("hn-thanhphuong", "Thanh Phương (Nữ Hà Nội)", "female"),
    ("hn-tienquan", "Tiến Quân (Nam Hà Nội)", "male"),
    ("hn-namkhanh", "Nam Khánh (Nam Hà Nội)", "male"),
    ("hue-maingoc", "Mai Ngọc (Nữ Huế)", "female"),
    ("hue-baoquoc", "Bảo Quốc (Nam Huế)", "male"),
    ("hcm-diemmy", "Diễm My (Nữ Sài Gòn)", "female"),
    ("hcm-minhquan", "Minh Quân (Nam Sài Gòn)", "male"),
    ("hcm-phuongly", "Phương Ly (Nữ Sài Gòn)", "female"),
    ("hcm-thuyduyen", "Thùy Duyên (Nữ Sài Gòn)", "female"),
)


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, ProviderError) and exc.retryable


def _raise_for_status(provider: str, response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ProviderAuthError(provider, f"Invalid or unauthorized {provider} credentials")
    if status == 429:
        raise ProviderRateLimitError(provider, f"{provider} API rate limit exceeded")
    if status >= 500:
        raise ProviderError(provider, f"{provider} server error ({status})", retryable=True)
    raise ProviderError(
        provider,
        f"{provider} request failed ({status}): {response.text[:300]}",
        retryable=False,
    )


async def _probe_duration_ms(audio_path: Path, ffprobe_bin: str) -> int:
    """Probe audio duration in milliseconds via ffprobe with MP3 size fallback."""
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

    size_bytes = audio_path.stat().st_size if audio_path.exists() else 0
    return int(round((size_bytes / 16000.0) * 1000)) if size_bytes > 0 else 0


def _filter_vietnamese_voices(
    voices: tuple[tuple[str, str, str], ...],
    provider: str,
    language: str | None,
) -> list[VoiceOption]:
    if language:
        lang = language.strip().lower()
        if lang and not (lang.startswith("vi") or lang in ("vietnamese", "vn")):
            return []
    return [
        VoiceOption(
            voice_id=vid,
            name=name,
            language="vi-VN",
            gender=gender,
            provider=provider,
        )
        for vid, name, gender in voices
    ]


class FptAiTTSProvider(BaseTTSProvider):
    """Synthesize Vietnamese speech via the FPT.AI TTS v5 API (with async URL polling)."""

    provider_name: str = "fpt_ai"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._api_key = (self._settings.FPT_AI_API_KEY or "").strip()
        self._base_url = (
            self._settings.FPT_AI_BASE_URL or "https://api.fpt.ai/hmi/tts/v5"
        ).strip()
        self._default_voice = (self._settings.FPT_AI_DEFAULT_VOICE or "banmai").strip()
        self._ffprobe_bin = self._settings.FFPROBE_BIN or "ffprobe"

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise ConfigurationError("FPT_AI_API_KEY is required for FptAiTTSProvider")
        return self._api_key

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _submit_tts(
        self,
        text: str,
        voice: str,
        speed: str,
    ) -> httpx.Response:
        api_key = self._require_api_key()
        headers = {
            "api-key": api_key,
            "voice": voice,
            "speed": speed,
            "format": "mp3",
            "Content-Type": "text/plain; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._base_url,
                headers=headers,
                content=text.encode("utf-8"),
            )
        _raise_for_status(self.provider_name, response)
        return response

    async def _poll_async_audio(self, async_url: str) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FPT_POLL_TIMEOUT_SECONDS
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            while loop.time() < deadline:
                try:
                    resp = await client.get(async_url)
                    if resp.status_code == 200 and resp.content:
                        return resp.content
                    if resp.status_code >= 500:
                        raise ProviderError(
                            self.provider_name,
                            f"FPT.AI audio CDN error ({resp.status_code})",
                            retryable=True,
                        )
                except httpx.TransportError as exc:
                    logger.warning("Transient error polling FPT.AI audio URL: %s", exc)
                await asyncio.sleep(_FPT_POLL_INTERVAL_SECONDS)

        raise ProviderError(
            self.provider_name,
            f"FPT.AI async audio URL did not become ready within {_FPT_POLL_TIMEOUT_SECONDS}s",
            retryable=True,
        )

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        rate_pct: int = 0,
    ) -> AudioSynthesisResult:
        """Synthesize speech with FPT.AI and poll the returned async MP3 URL."""
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ProviderError("fpt_ai", "Text for synthesis must not be empty")

        resolved_voice = (voice_id or "").strip() or self._default_voice
        clamped_rate = max(-25, min(25, int(rate_pct)))
        # FPT.AI speed accepts integer values in [-3, +3]
        fpt_speed = max(-3, min(3, round(clamped_rate / 8)))
        speed_header = f"+{fpt_speed}" if fpt_speed > 0 else str(fpt_speed)

        logger.info(
            "Synthesizing FPT.AI TTS voice=%s rate_pct=%d fpt_speed=%s",
            resolved_voice,
            clamped_rate,
            speed_header,
        )

        try:
            response = await self._submit_tts(cleaned_text, resolved_voice, speed_header)
            content_type = response.headers.get("content-type", "").lower()

            if content_type.startswith("audio/"):
                audio_bytes = response.content
            else:
                data: dict[str, Any] = response.json()
                error_code = data.get("error")
                if error_code not in (0, "0", None):
                    msg = data.get("message") or str(data)
                    if "auth" in str(msg).lower() or "key" in str(msg).lower():
                        raise ProviderAuthError("fpt_ai", f"FPT.AI auth error: {msg}")
                    raise ProviderError("fpt_ai", f"FPT.AI returned error={error_code}: {msg}")

                async_url = str(data.get("async") or "").strip()
                if not async_url:
                    raise ProviderError("fpt_ai", "FPT.AI response missing 'async' audio URL")
                audio_bytes = await self._poll_async_audio(async_url)

            if not audio_bytes:
                raise ProviderError("fpt_ai", "FPT.AI downloaded empty audio file")

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
            raise ProviderError("fpt_ai", f"FPT.AI request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("fpt_ai", f"FPT.AI HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in FptAiTTSProvider.synthesize")
            raise ProviderError("fpt_ai", f"Unexpected FPT.AI error: {exc}") from exc

    async def list_voices(self, language: str | None = None) -> list[VoiceOption]:
        """Return official FPT.AI Vietnamese voices."""
        return _filter_vietnamese_voices(_FPT_VOICES, self.provider_name, language)


class ViettelAiTTSProvider(BaseTTSProvider):
    """Synthesize Vietnamese speech via the Viettel AI TTS API."""

    provider_name: str = "viettel_ai"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._token = (self._settings.VIETTEL_AI_TOKEN or "").strip()
        self._base_url = (
            self._settings.VIETTEL_AI_BASE_URL or "https://viettelai.vn/tts/speech_synthesis"
        ).strip()
        self._default_voice = (self._settings.VIETTEL_AI_DEFAULT_VOICE or "hn-quynhanh").strip()
        self._ffprobe_bin = self._settings.FFPROBE_BIN or "ffprobe"

    def _require_token(self) -> str:
        if not self._token:
            raise ConfigurationError("VIETTEL_AI_TOKEN is required for ViettelAiTTSProvider")
        return self._token

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _request_tts(
        self,
        text: str,
        voice: str,
        speed: float,
    ) -> httpx.Response:
        token = self._require_token()
        headers = {
            "accept": "*/*",
            "Content-Type": "application/json",
            "token": token,
        }
        payload = {
            "text": text,
            "voice": voice,
            "speed": speed,
            "tts_return_option": 3,  # MP3
            "token": token,
            "without_filter": False,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(self._base_url, headers=headers, json=payload)
        _raise_for_status(self.provider_name, response)
        return response

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        rate_pct: int = 0,
    ) -> AudioSynthesisResult:
        """Synthesize speech with Viettel AI and save audio to ``output_path``."""
        cleaned_text = text.strip()
        if not cleaned_text:
            raise ProviderError("viettel_ai", "Text for synthesis must not be empty")

        resolved_voice = (voice_id or "").strip() or self._default_voice
        clamped_rate = max(-25, min(25, int(rate_pct)))
        # Viettel AI speed factor typically sits around 0.8..1.2
        speed = round(max(0.8, min(1.2, 1.0 + clamped_rate / 100.0)), 2)

        logger.info(
            "Synthesizing Viettel AI TTS voice=%s rate_pct=%d speed=%.2f",
            resolved_voice,
            clamped_rate,
            speed,
        )

        try:
            response = await self._request_tts(cleaned_text, resolved_voice, speed)
            content_type = response.headers.get("content-type", "").lower()

            if "application/json" in content_type:
                err_data = response.json()
                status_val = err_data.get("status")
                msg = err_data.get("message") or str(err_data)
                if status_val in (401, 403) or "token" in str(msg).lower():
                    raise ProviderAuthError("viettel_ai", f"Viettel AI auth failed: {msg}")
                raise ProviderError("viettel_ai", f"Viettel AI error: {msg}")

            audio_bytes = response.content
            if not audio_bytes:
                raise ProviderError("viettel_ai", "Viettel AI returned empty audio payload")

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
            raise ProviderError("viettel_ai", f"Viettel AI timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("viettel_ai", f"Viettel AI HTTP error: {exc}", retryable=True) from exc
        except Exception as exc:
            logger.exception("Unexpected error in ViettelAiTTSProvider.synthesize")
            raise ProviderError("viettel_ai", f"Unexpected Viettel AI error: {exc}") from exc

    async def list_voices(self, language: str | None = None) -> list[VoiceOption]:
        """Return official Viettel AI Vietnamese regional voices."""
        return _filter_vietnamese_voices(_VIETTEL_VOICES, self.provider_name, language)
