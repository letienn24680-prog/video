"""Optional local Faster-Whisper Speech-to-Text fallback provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError, NotFoundError, ProviderError
from backend.core.logging import get_logger
from backend.models.provider import TranscriptResult, TranscriptSegment, WordTimestamp
from backend.providers.base.stt import BaseSTTProvider

logger = get_logger(__name__)


def _sec_to_ms(value: float | None) -> int:
    if value is None:
        return 0
    return max(0, int(round(float(value) * 1000.0)))


class FasterWhisperProvider(BaseSTTProvider):
    """Local STT fallback powered by ``faster-whisper`` (lazy-imported)."""

    provider_name = "faster_whisper"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ConfigurationError(
                "Optional package 'faster-whisper' is not installed. "
                "Install it with: pip install faster-whisper>=1.1.0",
                details={"provider": self.provider_name},
            ) from exc

        logger.info(
            "Loading Faster-Whisper model '%s' (device=%s, compute_type=%s)",
            self.settings.FASTER_WHISPER_MODEL,
            self.settings.FASTER_WHISPER_DEVICE,
            self.settings.FASTER_WHISPER_COMPUTE_TYPE,
        )
        self._model = WhisperModel(
            self.settings.FASTER_WHISPER_MODEL,
            device=self.settings.FASTER_WHISPER_DEVICE,
            compute_type=self.settings.FASTER_WHISPER_COMPUTE_TYPE,
        )
        return self._model

    def _transcribe_sync(
        self,
        audio_path: Path,
        language: str | None,
        word_timestamps: bool,
    ) -> TranscriptResult:
        model = self._load_model()
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=word_timestamps,
        )

        segments: list[TranscriptSegment] = []
        full_text_parts: list[str] = []

        for idx, seg in enumerate(segments_iter):
            start_ms = _sec_to_ms(getattr(seg, "start", 0.0))
            end_ms = max(start_ms, _sec_to_ms(getattr(seg, "end", 0.0)))
            text = str(getattr(seg, "text", "") or "").strip()
            if text:
                full_text_parts.append(text)

            words: list[WordTimestamp] = []
            if word_timestamps:
                for raw_word in getattr(seg, "words", None) or []:
                    w_text = str(getattr(raw_word, "word", "") or "").strip()
                    if not w_text:
                        continue
                    w_start = _sec_to_ms(getattr(raw_word, "start", 0.0))
                    w_end = max(w_start, _sec_to_ms(getattr(raw_word, "end", 0.0)))
                    prob = getattr(raw_word, "probability", None)
                    words.append(
                        WordTimestamp(
                            word=w_text,
                            start_ms=w_start,
                            end_ms=w_end,
                            confidence=float(prob) if prob is not None else None,
                        )
                    )

            segments.append(
                TranscriptSegment(
                    index=idx,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    words=words,
                )
            )

        duration_sec = getattr(info, "duration", None)
        duration_ms = (
            _sec_to_ms(duration_sec)
            if duration_sec is not None
            else (max((s.end_ms for s in segments), default=0))
        )
        detected_lang = getattr(info, "language", language)

        return TranscriptResult(
            language=str(detected_lang) if detected_lang else None,
            duration_ms=duration_ms,
            full_text=" ".join(full_text_parts).strip(),
            segments=segments,
        )

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult:
        """Transcribe ``audio_path`` locally in a worker thread via ``faster-whisper``."""
        if not audio_path.is_file():
            raise NotFoundError(
                f"Audio file not found for transcription: {audio_path}",
                details={"audio_path": str(audio_path)},
            )

        try:
            result = await asyncio.to_thread(
                self._transcribe_sync,
                audio_path,
                language,
                word_timestamps,
            )
        except (ConfigurationError, NotFoundError):
            raise
        except Exception as exc:
            logger.exception("Faster-Whisper transcription failed: %s", exc)
            raise ProviderError(
                self.provider_name,
                f"Local transcription failed: {exc}",
                retryable=False,
            ) from exc

        logger.info(
            "Faster-Whisper transcription complete: %d segments, duration_ms=%s",
            len(result.segments),
            result.duration_ms,
        )
        return result
