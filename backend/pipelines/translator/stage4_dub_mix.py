"""Stage 4 of the AI Video Translator pipeline: adaptive TTS, BGM mix, subtitles, and master."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger
from backend.models.enums import ProjectStatus
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.scene import SceneResponse
from backend.pipelines.translator.stage1_separate import load_translator_state
from backend.providers.base.tts import BaseTTSProvider
from backend.providers.factory import ProviderFactory
from backend.services.media.ass_karaoke import AssKaraokeGenerator, KaraokeSegmentInput
from backend.services.media.bgm_library import BGMLibraryService
from backend.services.media.downloader import MediaDownloader
from backend.services.media.ffmpeg_engine import FFmpegEngine
from backend.services.media.watermark import resolve_watermark_options
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


def _resolve_default_voice(provider_name: str, settings: Settings) -> str:
    defaults = {
        "edge_tts": settings.EDGE_TTS_DEFAULT_VOICE,
        "elevenlabs": settings.ELEVENLABS_DEFAULT_VOICE_ID,
        "fpt_ai": settings.FPT_AI_DEFAULT_VOICE,
        "viettel_ai": settings.VIETTEL_AI_DEFAULT_VOICE,
    }
    return defaults.get(provider_name, settings.EDGE_TTS_DEFAULT_VOICE)


def _resolve_scene_voice(
    scene: SceneResponse,
    cfg: PipelineConfig,
    tts_provider: BaseTTSProvider,
    settings: Settings,
) -> str:
    if scene.voice_id and scene.voice_id.strip():
        return scene.voice_id.strip()
    if scene.speaker_label and scene.speaker_label in cfg.speaker_voices:
        speaker_voice = (cfg.speaker_voices.get(scene.speaker_label) or "").strip()
        if speaker_voice:
            return speaker_voice
    if cfg.voice_id and cfg.voice_id.strip():
        return cfg.voice_id.strip()
    return _resolve_default_voice(tts_provider.provider_name, settings)


async def _ensure_stage_assets(
    project: ProjectResponse,
    cfg: PipelineConfig,
    work_dir: Path,
    settings: Settings,
    ffmpeg: FFmpegEngine,
) -> tuple[Path, Path | None, str, int]:
    """Recover or re-stage source video and BGM track if temp state was lost."""
    state = load_translator_state(work_dir) or {}
    source_raw = state.get("source_video_path")
    source_path = Path(source_raw) if source_raw else (work_dir / "source.mp4")

    if not source_path.is_file():
        storage = ProviderFactory.get_storage(override=cfg.storage_provider, settings=settings)
        downloader = MediaDownloader(settings=settings)
        downloaded = await downloader.stage_source_media(
            source_url=project.source_inputs.get("source_url"),
            source_file_key=project.source_inputs.get("source_file_key"),
            work_dir=work_dir,
            storage=storage,
        )
        source_path = Path(downloaded.local_path)

    probe = await ffmpeg.probe(source_path)
    total_duration_ms = probe.duration_ms
    if total_duration_ms <= 0 and project.duration_seconds:
        total_duration_ms = int(round(project.duration_seconds * 1000.0))

    bgm_raw = state.get("bgm_path")
    bgm_path = Path(bgm_raw) if bgm_raw else None
    separation_mode = str(
        state.get("separation_mode")
        or (
            cfg.separation_provider.value
            if cfg.separation_provider is not None
            else settings.SEPARATION_PROVIDER
        )
    )

    if (bgm_path is None or not bgm_path.is_file()) and probe.has_audio:
        audio_orig_path = work_dir / "audio_orig.wav"
        if not audio_orig_path.is_file():
            await ffmpeg.extract_audio(
                source_path,
                audio_orig_path,
                sample_rate=44100,
                channels=2,
            )
        sep_provider = ProviderFactory.get_separation(
            override=cfg.separation_provider,
            settings=settings,
        )
        sep_result = await sep_provider.separate(audio_orig_path, work_dir)
        separation_mode = sep_result.mode
        bgm_path = Path(sep_result.bgm_path) if sep_result.bgm_path is not None else None

    if bgm_path is not None and not bgm_path.is_file():
        bgm_path = None

    return source_path, bgm_path, separation_mode, max(1, total_duration_ms)


async def _synthesize_adaptive_scene(
    scene: SceneResponse,
    text: str,
    voice_id: str,
    tts_dir: Path,
    tts_provider: BaseTTSProvider,
    ffmpeg: FFmpegEngine,
    settings: Settings,
) -> tuple[Path, int]:
    """Synthesize a single dialogue line and apply adaptive speed sync to fit its slot."""
    slot_ms = max(200, scene.end_ms - scene.start_ms)
    raw_path = tts_dir / f"scene_{scene.scene_index:04d}.mp3"

    synth = await tts_provider.synthesize(
        text=text,
        voice_id=voice_id,
        output_path=raw_path,
        rate_pct=scene.tts_rate_pct,
    )
    final_path = Path(synth.audio_path)
    duration_ms = synth.duration_ms
    if duration_ms <= 0:
        probed = await ffmpeg.probe(final_path)
        duration_ms = probed.duration_ms

    if duration_ms > slot_ms and scene.tts_rate_pct == 0:
        max_speed = max(0, int(settings.EDGE_TTS_MAX_SPEED_PCT))
        ratio = duration_ms / float(slot_ms)
        adaptive_rate = min(max_speed, max(1, int(math.ceil((ratio - 1.0) * 100.0))))
        if adaptive_rate > 0:
            fast_path = tts_dir / f"scene_{scene.scene_index:04d}_r{adaptive_rate}.mp3"
            resynth = await tts_provider.synthesize(
                text=text,
                voice_id=voice_id,
                output_path=fast_path,
                rate_pct=adaptive_rate,
            )
            final_path = Path(resynth.audio_path)
            duration_ms = resynth.duration_ms
            if duration_ms <= 0:
                probed = await ffmpeg.probe(final_path)
                duration_ms = probed.duration_ms

    if duration_ms > slot_ms:
        synced_path = tts_dir / f"scene_{scene.scene_index:04d}_synced.wav"
        adjusted_path, adjusted_ms = await ffmpeg.adjust_audio_speed(
            final_path,
            target_duration_ms=slot_ms,
            output_path=synced_path,
            max_atempo=1.25,
        )
        final_path = adjusted_path
        duration_ms = adjusted_ms

    return final_path, max(1, duration_ms)


async def run_stage4_dub_mix(
    project: ProjectResponse,
    scenes: list[SceneResponse],
    cfg: PipelineConfig,
    subscription_tier: str,
    work_dir: Path,
    settings: Settings | None = None,
    repo: SupabaseRepository | None = None,
    emit: ProgressEmitter | None = None,
) -> str:
    """Synthesize adaptive dubbed audio, mix with BGM, burn optional subtitles, and master."""
    if not scenes:
        raise ValidationError(
            "No scenes available to render for this project",
            details={"project_id": str(project.id)},
        )

    cfg_settings = settings or get_settings()
    repository = repo or SupabaseRepository()
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = FFmpegEngine(settings=cfg_settings)

    async def _emit(
        stage: str,
        pct: int,
        message: str,
        *,
        event_type: str = "progress",
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        if emit is not None:
            await emit(stage, pct, message, event_type=event_type, extra_data=extra_data)

    await _emit("prepare_render", 62, "Preparing video and background audio tracks...")
    source_video_path, bgm_path, separation_mode, total_duration_ms = await _ensure_stage_assets(
        project=project,
        cfg=cfg,
        work_dir=work_dir,
        settings=cfg_settings,
        ffmpeg=ffmpeg,
    )

    storage = ProviderFactory.get_storage(override=cfg.storage_provider, settings=cfg_settings)
    effective_pipeline_cfg = {
        **(project.pipeline_config or {}),
        **cfg.model_dump(exclude_none=True),
    }
    bgm_service = BGMLibraryService(settings=cfg_settings)
    custom_bgm_path = await bgm_service.resolve_bgm_path(
        pipeline_config=effective_pipeline_cfg,
        source_inputs=project.source_inputs or {},
        work_dir=work_dir,
        storage=storage,
    )
    using_custom_bgm = custom_bgm_path is not None and custom_bgm_path.is_file()
    if using_custom_bgm:
        bgm_path = custom_bgm_path
        logger.info("Translator project=%s using custom/preset BGM -> %s", project.id, bgm_path)

    tts_provider = ProviderFactory.get_tts(override=cfg.tts_provider, settings=cfg_settings)
    tts_dir = work_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    dubbed_segments: list[tuple[int, int, Path]] = []
    karaoke_segments: list[KaraokeSegmentInput] = []
    total_scenes = len(scenes)

    for idx, scene in enumerate(scenes):
        text = (scene.edited_text or scene.narration_text or scene.original_text or "").strip()
        if not text:
            continue

        voice_id = _resolve_scene_voice(scene, cfg, tts_provider, cfg_settings)
        audio_path, actual_ms = await _synthesize_adaptive_scene(
            scene=scene,
            text=text,
            voice_id=voice_id,
            tts_dir=tts_dir,
            tts_provider=tts_provider,
            ffmpeg=ffmpeg,
            settings=cfg_settings,
        )

        seg_start = max(0, scene.start_ms)
        seg_end = seg_start + actual_ms
        dubbed_segments.append((seg_start, seg_end, audio_path))
        karaoke_segments.append(
            KaraokeSegmentInput(
                start_ms=seg_start,
                end_ms=max(seg_start + 200, scene.end_ms, seg_end),
                text=text,
                words=[],
            )
        )

        pct = 63 + int(round(((idx + 1) / max(1, total_scenes)) * 18))
        await _emit(
            "tts",
            pct,
            f"Synthesized line {idx + 1}/{total_scenes}",
            extra_data={"completed_items": idx + 1, "total_items": total_scenes},
        )

    if not dubbed_segments:
        raise ValidationError(
            "All subtitle segments are empty; cannot render dubbed audio",
            details={"project_id": str(project.id)},
        )

    await _emit("build_dub_track", 83, "Aligning dubbed speech segments on timeline...")
    dubbed_wav = await ffmpeg.build_dubbed_audio_track(
        segments=dubbed_segments,
        total_duration_ms=total_duration_ms,
        output_wav=work_dir / "dubbed_track.wav",
    )

    await _emit("mix_audio", 86, "Mixing dubbed voice track with background music...")
    mixed_audio = await ffmpeg.mix_voice_and_bgm(
        voice_path=dubbed_wav,
        bgm_path=bgm_path,
        output_audio=work_dir / "mixed_audio.wav",
        bgm_volume=cfg.bgm_volume,
        auto_ducking=cfg.auto_ducking,
        fast_mode_duck_db=float(cfg_settings.SEPARATION_FAST_MODE_DUCK_DB),
        is_fast_mode=(separation_mode == "fast_mode" and not using_custom_bgm),
    )

    ass_path: Path | None = None
    if cfg.burn_subtitles:
        await _emit("subtitles", 89, "Generating dynamic .ass subtitles...")
        ass_generator = AssKaraokeGenerator()
        ass_path = ass_generator.generate(
            karaoke_segments,
            output_path=work_dir / "subtitles.ass",
            aspect_ratio=project.aspect_ratio,
            style=cfg.subtitle_style,
        )

    watermark = resolve_watermark_options(subscription_tier, cfg_settings)
    await _emit("master", 92, "Mastering final dubbed video...")
    final_video_path = await ffmpeg.master_final_video(
        video_path=source_video_path,
        audio_path=mixed_audio,
        output_path=work_dir / "final.mp4",
        aspect_ratio=project.aspect_ratio,
        ass_subtitle_path=ass_path,
        watermark=watermark,
    )

    await _emit("upload", 96, "Uploading final video to storage...")
    remote_key = f"{project.user_id}/{project.id}/final.mp4"
    stored = await storage.upload_file(final_video_path, remote_key, content_type="video/mp4")
    signed_url = stored.signed_url or await storage.get_signed_url(
        remote_key,
        expires_in=cfg_settings.SUPABASE_SIGNED_URL_TTL_SECONDS,
    )

    await repository.update_project_status(
        project.id,
        ProjectStatus.COMPLETED,
        current_stage="completed",
        progress_pct=100,
        output_video_url=signed_url,
        enforce_fsm=True,
    )

    await _emit(
        "completed",
        100,
        "Final dubbed video ready!",
        event_type="completed",
        extra_data={"output_video_url": signed_url},
    )

    logger.info(
        "Stage 4 render complete for project=%s (remote_key=%s)",
        project.id,
        remote_key,
    )
    return signed_url
