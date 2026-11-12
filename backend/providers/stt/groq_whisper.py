"""Groq Whisper cloud Speech-to-Text provider adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    NotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from backend.core.logging import get_logger
from backend.models.provider import TranscriptResult, TranscriptSegment, WordTimestamp
from backend.providers.base.stt import BaseSTTProvider

logger = get_logger(__name__)

_PLACEHOLDER_KEYS = {"", "gsk_your_groq_api_key_here"}


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ProviderRateLimitError) or (
        isinstance(exc, ProviderError) and exc.retryable
    )


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sec_to_ms(value: Any) -> int:
    try:
        return max(0, int(round(float(value) * 1000.0)))
    except (TypeError, ValueError):
        return 0


class GroqWhisperProvider(BaseSTTProvider):
    """Cloud STT adapter backed by Groq's Whisper API (``whisper-large-v3``)."""

    provider_name = "groq"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _validate_api_key(self) -> str:
        key = (self.settings.GROQ_API_KEY or "").strip()
        if key in _PLACEHOLDER_KEYS:
            raise ConfigurationError(
                "GROQ_API_KEY is missing or uses the default placeholder.",
                details={"provider": self.provider_name},
            )
        return key

    def _create_client(self, api_key: str) -> Any:
        try:
            from groq import AsyncGroq
        except ImportError as exc:
            raise ConfigurationError(
                "The 'groq' package is not installed. Run: pip install groq>=0.13.0",
                details={"provider": self.provider_name},
            ) from exc
        return AsyncGroq(api_key=api_key)

    def _map_exception(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        status_code = getattr(exc, "status_code", None)
        exc_name = type(exc).__name__.lower()
        message = str(exc) or type(exc).__name__

        if status_code in (401, 403) or "auth" in exc_name or "api_key" in message.lower():
            return ProviderAuthError(self.provider_name, message)
        if status_code == 429 or "ratelimit" in exc_name or "rate limit" in message.lower():
            return ProviderRateLimitError(self.provider_name, message)

        retryable = (
            status_code in (408, 500, 502, 503, 504)
            or "timeout" in exc_name
            or "connection" in exc_name
            or "temporar" in message.lower()
        )
        return ProviderError(self.provider_name, message, retryable=retryable)

    def _parse_response(self, raw: Any, word_timestamps: bool) -> TranscriptResult:
        full_text = str(_get_field(raw, "text", "") or "").strip()
        language = _get_field(raw, "language", None)
        duration_sec = _get_field(raw, "duration", None)
        duration_ms = _sec_to_ms(duration_sec) if duration_sec is not None else None

        raw_words = _get_field(raw, "words", None) or []
        top_words: list[WordTimestamp] = []
        if word_timestamps:
            for item in raw_words:
                word_str = str(_get_field(item, "word", "") or "").strip()
                if not word_str:
                    continue
                w_start = _sec_to_ms(_get_field(item, "start", 0.0))
                w_end = max(w_start, _sec_to_ms(_get_field(item, "end", 0.0)))
                conf = _get_field(item, "confidence", None)
                top_words.append(
                    WordTimestamp(
                        word=word_str,
                        start_ms=w_start,
                        end_ms=w_end,
                        confidence=float(conf) if conf is not None else None,
                    )
                )

        raw_segments = _get_field(raw, "segments", None) or []
        segments: list[TranscriptSegment] = []
        seg_has_nested_words = False

        for idx, seg in enumerate(raw_segments):
            s_start = _sec_to_ms(_get_field(seg, "start", 0.0))
            s_end = max(s_start, _sec_to_ms(_get_field(seg, "end", 0.0)))
            s_text = str(_get_field(seg, "text", "") or "").strip()
            s_speaker = _get_field(seg, "speaker", None) or _get_field(seg, "speaker_label", None)

            seg_words: list[WordTimestamp] = []
            nested_words = _get_field(seg, "words", None) or []
            if word_timestamps and nested_words:
                seg_has_nested_words = True
                for item in nested_words:
                    w_text = str(_get_field(item, "word", "") or "").strip()
                    if not w_text:
                        continue
                    w_start = _sec_to_ms(_get_field(item, "start", 0.0))
                    w_end = max(w_start, _sec_to_ms(_get_field(item, "end", 0.0)))
                    conf = _get_field(item, "confidence", None)
                    seg_words.append(
                        WordTimestamp(
                            word=w_text,
                            start_ms=w_start,
                            end_ms=w_end,
                            confidence=float(conf) if conf is not None else None,
                        )
                    )

            segments.append(
                TranscriptSegment(
                    index=idx,
                    start_ms=s_start,
                    end_ms=s_end,
                    text=s_text,
                    speaker_label=str(s_speaker) if s_speaker is not None else None,
                    words=seg_words,
                )
            )

        # Groq verbose_json typically places words at the top level rather than inside segments.
        if word_timestamps and top_words and not seg_has_nested_words and segments:
            for word in top_words:
                mid = (word.start_ms + word.end_ms) // 2
                matched = next(
                    (s for s in segments if s.start_ms <= mid <= s.end_ms),
                    None,
                )
                if matched is None:
                    matched = min(
                        segments,
                        key=lambda s: min(abs(mid - s.start_ms), abs(mid - s.end_ms)),
                    )
                matched.words.append(word)

        if not segments and full_text:
            segments.append(
                TranscriptSegment(
                    index=0,
                    start_ms=0,
                    end_ms=duration_ms or (top_words[-1].end_ms if top_words else 0),
                    text=full_text,
                    words=top_words if word_timestamps else [],
                )
            )

        if duration_ms is None and segments:
            duration_ms = max(s.end_ms for s in segments)

        return TranscriptResult(
            language=str(language) if language else None,
            duration_ms=duration_ms,
            full_text=full_text or " ".join(s.text for s in segments).strip(),
            segments=segments,
        )

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _call_api(
        self,
        api_key: str,
        audio_name: str,
        audio_bytes: bytes,
        language: str | None,
        word_timestamps: bool,
    ) -> Any:
        client = self._create_client(api_key)
        granularities = ["word", "segment"] if word_timestamps else ["segment"]
        request_kwargs: dict[str, Any] = {
            "file": (audio_name, audio_bytes),
            "model": self.settings.GROQ_STT_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": granularities,
        }
        if language:
            request_kwargs["language"] = language

        try:
            return await client.audio.transcriptions.create(**request_kwargs)
        except Exception as exc:
            mapped = self._map_exception(exc)
            logger.warning(
                "Groq STT request failed (retryable=%s): %s",
                mapped.retryable,
                mapped.message,
            )
            raise mapped from exc
        finally:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult:
        """Transcribe ``audio_path`` using Groq Whisper ``verbose_json``."""
        if not audio_path.is_file():
            raise NotFoundError(
                f"Audio file not found for transcription: {audio_path}",
                details={"audio_path": str(audio_path)},
            )

        api_key = self._validate_api_key()
        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)

        logger.info(
            "Starting Groq Whisper transcription (model=%s, file=%s, language=%s)",
            self.settings.GROQ_STT_MODEL,
            audio_path.name,
            language or "auto",
        )
        raw_response = await self._call_api(
            api_key=api_key,
            audio_name=audio_path.name,
            audio_bytes=audio_bytes,
            language=language,
            word_timestamps=word_timestamps,
        )
        result = self._parse_response(raw_response, word_timestamps=word_timestamps)
        logger.info(
            "Groq Whisper transcription complete: %d segments, duration_ms=%s",
            len(result.segments),
            result.duration_ms,
        )
        return result
