"""Stage 2 of the AI Video Translator pipeline: word-level Speech-to-Text."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger
from backend.models.project import PipelineConfig
from backend.models.provider import TranscriptResult, TranscriptSegment
from backend.providers.factory import ProviderFactory
from backend.providers.sandbox_fallback import SandboxSTTProvider, is_configured_key

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


async def run_stage2_stt(
    vocals_path: Path,
    cfg: PipelineConfig,
    settings: Settings | None = None,
    emit: ProgressEmitter | None = None,
    *,
    duration_ms: int = 0,
) -> TranscriptResult:
    """Transcribe separated vocals into timestamped segments and word timings."""
    cfg_settings = settings or get_settings()

    async def _emit(stage: str, pct: int, message: str) -> None:
        if emit is not None:
            await emit(stage, pct, message)

    await _emit("stt", 30, "Running word-level speech recognition...")

    chosen_stt = (
        cfg.stt_provider.value
        if cfg.stt_provider is not None
        else cfg_settings.STT_PROVIDER
    ).strip().lower()
    if (
        cfg_settings.LOCAL_SANDBOX_MODE
        and chosen_stt == "groq"
        and not is_configured_key(cfg_settings.GROQ_API_KEY)
    ):
        await _emit(
            "stt",
            32,
            "Chưa có GROQ_API_KEY — dùng Local Sandbox Speech Recognition",
        )
        stt_provider = SandboxSTTProvider(settings=cfg_settings)
    else:
        stt_provider = ProviderFactory.get_stt(override=cfg.stt_provider, settings=cfg_settings)

    transcript = await stt_provider.transcribe(
        vocals_path,
        language=cfg.source_lang,
        word_timestamps=True,
    )

    cleaned_segments: list[TranscriptSegment] = []
    for seg in transcript.segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        cleaned_segments.append(
            TranscriptSegment(
                index=len(cleaned_segments),
                start_ms=max(0, seg.start_ms),
                end_ms=max(seg.start_ms, seg.end_ms),
                text=text,
                speaker_label=seg.speaker_label,
                words=list(seg.words),
            )
        )

    if not cleaned_segments:
        fallback_text = (transcript.full_text or "").strip()
        if not fallback_text:
            raise ValidationError(
                "No speech detected in source audio; cannot generate subtitles or dubbing."
            )
        fallback_end = max(200, transcript.duration_ms or duration_ms or 1000)
        cleaned_segments.append(
            TranscriptSegment(
                index=0,
                start_ms=0,
                end_ms=fallback_end,
                text=fallback_text,
                words=[],
            )
        )

    normalized = TranscriptResult(
        language=transcript.language or cfg.source_lang,
        duration_ms=transcript.duration_ms or duration_ms or cleaned_segments[-1].end_ms,
        full_text=transcript.full_text or " ".join(s.text for s in cleaned_segments),
        segments=cleaned_segments,
    )

    await _emit(
        "stt",
        55,
        f"Speech recognition complete ({len(normalized.segments)} segments)",
    )
    logger.info(
        "Stage 2 STT complete: provider=%s segments=%d language=%s",
        stt_provider.provider_name,
        len(normalized.segments),
        normalized.language,
    )
    return normalized
