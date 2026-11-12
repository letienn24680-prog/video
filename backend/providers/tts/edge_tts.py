"""Microsoft Edge-TTS free default Text-to-Speech provider adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError, ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.core.subprocess_utils import run_subprocess_exec
from backend.models.provider import AudioSynthesisResult, VoiceOption
from backend.providers.base.tts import BaseTTSProvider

logger = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable


class EdgeTTSProvider(BaseTTSProvider):
    """Free cloud TTS adapter using ``edge-tts`` with adaptive speed clamping."""

    provider_name = "edge_tts"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _import_edge_tts(self) -> Any:
        try:
            import edge_tts
        except ImportError as exc:
            raise ConfigurationError(
                "The 'edge-tts' package is not installed. Run: pip install edge-tts>=6.1.12",
                details={"provider": self.provider_name},
            ) from exc
        return edge_tts

    def _clamp_rate(self, rate_pct: int) -> tuple[int, str]:
        max_speed = max(0, int(self.settings.EDGE_TTS_MAX_SPEED_PCT))
        clamped = max(-25, min(max_speed, int(rate_pct)))
        rate_str = f"+{clamped}%" if clamped >= 0 else f"{clamped}%"
        return clamped, rate_str

    async def _probe_duration_ms(self, audio_path: Path) -> int:
        """Compute audio duration in ms using ffprobe, with mutagen/bitrate fallback."""
        try:
            returncode, stdout, _ = await run_subprocess_exec(
                self.settings.FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            )
            if returncode == 0:
                raw = stdout.decode("utf-8", errors="ignore").strip()
                if raw:
                    return max(0, int(round(float(raw) * 1000.0)))
        except (FileNotFoundError, OSError, ValueError):
            pass

        try:
            from mutagen.mp3 import MP3

            info = await asyncio.to_thread(lambda: MP3(str(audio_path)).info)
            if info and getattr(info, "length", None):
                return max(0, int(round(float(info.length) * 1000.0)))
        except Exception:
            pass

        # Edge-TTS outputs 48kbps MP3 (~6000 bytes/sec) by default.
        try:
            size_bytes = audio_path.stat().st_size
            return max(0, int(round((size_bytes * 8.0) / 48.0)))
        except OSError:
            return 0

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
        reraise=True,
    )
    async def _run_synthesize(
        self,
        text: str,
        voice: str,
        rate_str: str,
        output_path: Path,
    ) -> None:
        edge_tts = self._import_edge_tts()
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_str)
            await communicate.save(str(output_path))
        except ConfigurationError:
            raise
        except Exception as exc:
            logger.warning("Edge-TTS synthesis attempt failed: %s", exc)
            raise ProviderError(
                self.provider_name,
                f"Edge-TTS synthesis failed: {exc}",
                retryable=True,
            ) from exc

        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ProviderError(
                self.provider_name,
                f"Edge-TTS produced no audio output at {output_path}",
                retryable=True,
            )

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        rate_pct: int = 0,
    ) -> AudioSynthesisResult:
        """Synthesize ``text`` to ``output_path`` via Edge-TTS and return audio metadata."""
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValidationError(
                "TTS synthesis text cannot be empty.",
                details={"provider": self.provider_name},
            )

        selected_voice = (voice_id or "").strip() or self.settings.EDGE_TTS_DEFAULT_VOICE
        clamped_rate, rate_str = self._clamp_rate(rate_pct)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Synthesizing Edge-TTS (voice=%s, rate=%s, chars=%d)",
            selected_voice,
            rate_str,
            len(clean_text),
        )
        await self._run_synthesize(
            text=clean_text,
            voice=selected_voice,
            rate_str=rate_str,
            output_path=output_path,
        )
        duration_ms = await self._probe_duration_ms(output_path)

        return AudioSynthesisResult(
            audio_path=output_path,
            duration_ms=duration_ms,
            voice_id=selected_voice,
            rate_pct=clamped_rate,
            provider=self.provider_name,
        )

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
        reraise=True,
    )
    async def list_voices(self, language: str | None = None) -> list[VoiceOption]:
        """List available Microsoft Edge neural voices, optionally filtered by language."""
        edge_tts = self._import_edge_tts()
        try:
            raw_voices = await edge_tts.list_voices()
        except Exception as exc:
            logger.warning("Edge-TTS list_voices failed: %s", exc)
            raise ProviderError(
                self.provider_name,
                f"Failed to fetch Edge-TTS voices: {exc}",
                retryable=True,
            ) from exc

        lang_filter = (language or "").strip().lower()
        options: list[VoiceOption] = []
        for item in raw_voices:
            locale = str(item.get("Locale") or "")
            if lang_filter and not locale.lower().startswith(lang_filter):
                continue
            short_name = str(item.get("ShortName") or item.get("Name") or "")
            if not short_name:
                continue
            friendly = str(item.get("FriendlyName") or short_name)
            gender = item.get("Gender")
            options.append(
                VoiceOption(
                    voice_id=short_name,
                    name=friendly,
                    language=locale or "unknown",
                    gender=str(gender) if gender else None,
                    provider=self.provider_name,
                )
            )
        return options
