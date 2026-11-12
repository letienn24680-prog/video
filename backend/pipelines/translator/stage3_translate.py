"""Stage 3 of the AI Video Translator pipeline: time-synced translation & AWAITING_REVIEW."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.models.enums import ProjectStatus
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.provider import TranscriptResult, TranscriptSegment
from backend.models.scene import SceneCreate, SceneResponse
from backend.providers.factory import ProviderFactory
from backend.providers.sandbox_fallback import SandboxLLMProvider, is_configured_key
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


def _resolve_scene_voice(
    speaker_label: str | None,
    cfg: PipelineConfig,
) -> str | None:
    if speaker_label and speaker_label in cfg.speaker_voices:
        chosen = (cfg.speaker_voices.get(speaker_label) or "").strip()
        if chosen:
            return chosen
    if cfg.voice_id and cfg.voice_id.strip():
        return cfg.voice_id.strip()
    return None


async def run_stage3_translate(
    project: ProjectResponse,
    transcript: TranscriptResult,
    cfg: PipelineConfig,
    settings: Settings | None = None,
    repo: SupabaseRepository | None = None,
    emit: ProgressEmitter | None = None,
) -> list[SceneResponse]:
    """Translate transcript segments with syllable budget, persist scenes, and pause for review."""
    cfg_settings = settings or get_settings()
    repository = repo or SupabaseRepository()

    async def _emit(
        stage: str,
        pct: int,
        message: str,
        *,
        event_type: str = "progress",
        extra_data: dict | None = None,
    ) -> None:
        if emit is not None:
            await emit(stage, pct, message, event_type=event_type, extra_data=extra_data)

    await _emit(
        "translate",
        57,
        f"Translating {len(transcript.segments)} segments to '{cfg.target_lang}'...",
    )

    if cfg_settings.LOCAL_SANDBOX_MODE and not is_configured_key(cfg_settings.GEMINI_API_KEY):
        await _emit(
            "translate",
            58,
            "Chưa có GEMINI_API_KEY — dùng Local Sandbox Translator",
        )
        llm_provider = SandboxLLMProvider(settings=cfg_settings)
    else:
        llm_provider = ProviderFactory.get_llm(override=cfg.llm_provider, settings=cfg_settings)

    translated_segments = await llm_provider.translate_time_synced(
        transcript.segments,
        target_lang=cfg.target_lang,
        style_tone=cfg.translation_style,
    )

    source_by_index: dict[int, TranscriptSegment] = {
        seg.index: seg for seg in transcript.segments
    }

    scene_payloads: list[SceneCreate] = []
    for pos, tr_seg in enumerate(translated_segments):
        src_seg = source_by_index.get(tr_seg.index)
        if src_seg is None and pos < len(transcript.segments):
            src_seg = transcript.segments[pos]

        speaker = tr_seg.speaker_label or (src_seg.speaker_label if src_seg else None)
        word_timings = list(src_seg.words) if src_seg else []
        start_ms = max(0, tr_seg.start_ms)
        end_ms = max(start_ms, tr_seg.end_ms)
        translated_text = (tr_seg.translated_text or tr_seg.original_text or "").strip()

        scene_payloads.append(
            SceneCreate(
                project_id=project.id,
                scene_index=pos,
                start_ms=start_ms,
                end_ms=end_ms,
                original_text=tr_seg.original_text,
                edited_text=translated_text,
                word_timestamps=word_timings,
                speaker_label=speaker,
                voice_id=_resolve_scene_voice(speaker, cfg),
                tts_rate_pct=cfg.tts_rate_pct,
                subtitle_style=cfg.subtitle_style.model_dump(mode="json"),
            )
        )

    await repository.delete_project_scenes(project.id)
    saved_scenes = await repository.upsert_scenes(project.id, scene_payloads)

    await repository.update_project_status(
        project.id,
        ProjectStatus.AWAITING_REVIEW,
        current_stage="awaiting_review",
        progress_pct=60,
        enforce_fsm=True,
    )

    await _emit(
        "awaiting_review",
        60,
        f"Translation ready for review ({len(saved_scenes)} segments)",
        event_type="awaiting_review",
        extra_data={"scenes_count": len(saved_scenes)},
    )

    logger.info(
        "Stage 3 translation complete for project=%s: %d scenes saved -> AWAITING_REVIEW",
        project.id,
        len(saved_scenes),
    )
    return saved_scenes
