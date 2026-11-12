"""Stage 1 of the AI Video Translator pipeline: ingest, probe, credit charge, and separation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger
from backend.models.enums import ProjectMode
from backend.models.project import PipelineConfig, ProjectResponse
from backend.providers.factory import ProviderFactory
from backend.services.billing.credit_service import CreditService
from backend.services.media.downloader import MediaDownloader
from backend.services.media.ffmpeg_engine import FFmpegEngine
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]

STATE_FILENAME = "translator_state.json"


class Stage1Result(BaseModel):
    """Artifacts and media metadata produced by Translator Stage 1."""

    source_video_path: Path
    audio_stt_path: Path
    audio_orig_path: Path
    vocals_path: Path
    bgm_path: Path | None = None
    separation_mode: str
    duration_ms: int
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    credits_estimated: int = 0


def save_translator_state(work_dir: Path, state: Stage1Result) -> Path:
    """Persist Stage 1 local artifact paths so Phase 'render' can resume cleanly."""
    work_dir.mkdir(parents=True, exist_ok=True)
    state_path = work_dir / STATE_FILENAME
    payload = {
        "source_video_path": str(state.source_video_path),
        "audio_stt_path": str(state.audio_stt_path),
        "audio_orig_path": str(state.audio_orig_path),
        "vocals_path": str(state.vocals_path),
        "bgm_path": str(state.bgm_path) if state.bgm_path is not None else None,
        "separation_mode": state.separation_mode,
        "duration_ms": state.duration_ms,
        "duration_seconds": state.duration_seconds,
        "width": state.width,
        "height": state.height,
        "has_audio": state.has_audio,
        "credits_estimated": state.credits_estimated,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return state_path


def load_translator_state(work_dir: Path) -> dict[str, Any] | None:
    """Load persisted Stage 1 paths from ``work_dir/translator_state.json`` if present."""
    state_path = work_dir / STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", state_path, exc)
        return None


async def run_stage1_separate(
    project: ProjectResponse,
    cfg: PipelineConfig,
    work_dir: Path,
    settings: Settings | None = None,
    repo: SupabaseRepository | None = None,
    emit: ProgressEmitter | None = None,
) -> Stage1Result:
    """Stage source video, probe metadata, charge credits, and separate vocals/BGM."""
    cfg_settings = settings or get_settings()
    repository = repo or SupabaseRepository()
    work_dir.mkdir(parents=True, exist_ok=True)

    async def _emit(stage: str, pct: int, message: str) -> None:
        if emit is not None:
            await emit(stage, pct, message)

    await _emit("ingest", 10, "Downloading and staging source media...")

    source_url = project.source_inputs.get("source_url")
    source_file_key = project.source_inputs.get("source_file_key")
    storage = ProviderFactory.get_storage(override=cfg.storage_provider, settings=cfg_settings)
    downloader = MediaDownloader(settings=cfg_settings)
    downloaded = await downloader.stage_source_media(
        source_url=source_url,
        source_file_key=source_file_key,
        work_dir=work_dir,
        storage=storage,
    )
    source_video_path = Path(downloaded.local_path)

    ffmpeg = FFmpegEngine(settings=cfg_settings)
    probe = await ffmpeg.probe(source_video_path)

    duration_ms = probe.duration_ms
    if duration_ms <= 0 and downloaded.duration_seconds and downloaded.duration_seconds > 0:
        duration_ms = int(round(downloaded.duration_seconds * 1000.0))
    duration_seconds = round(max(0.0, duration_ms / 1000.0), 2)

    if duration_ms <= 0 or duration_seconds <= 0:
        raise ValidationError(
            "Source media has zero or unreadable duration",
            details={"project_id": str(project.id), "source": str(source_video_path.name)},
        )
    if not probe.has_audio:
        raise ValidationError(
            "Source media does not contain an audio stream to translate",
            details={"project_id": str(project.id), "source": str(source_video_path.name)},
        )

    await _emit(
        "probe",
        15,
        f"Probed media ({duration_seconds:.1f}s, {probe.width}x{probe.height})",
    )

    credit_service = CreditService(billing_repo=repository, settings=cfg_settings)
    credits_estimated = credit_service.estimate_project_credits(
        ProjectMode.TRANSLATOR,
        duration_seconds,
        cfg,
    )

    await repository.update_project(
        project.id,
        {
            "duration_seconds": duration_seconds,
            "credits_estimated": credits_estimated,
        },
    )

    if credits_estimated > 0 and project.credits_charged == 0:
        await credit_service.charge_for_project(
            user_id=project.user_id,
            project_id=project.id,
            amount=credits_estimated,
            description=f"AI Video Translator ({duration_seconds:.1f}s)",
            metadata={
                "mode": ProjectMode.TRANSLATOR.value,
                "duration_seconds": duration_seconds,
                "separation_provider": (
                    cfg.separation_provider.value
                    if cfg.separation_provider is not None
                    else cfg_settings.SEPARATION_PROVIDER
                ),
                "tts_provider": (
                    cfg.tts_provider.value
                    if cfg.tts_provider is not None
                    else cfg_settings.TTS_PROVIDER
                ),
            },
        )

    await _emit("extract_audio", 18, "Extracting STT and stereo audio tracks...")
    audio_stt_path = await ffmpeg.extract_audio(
        source_video_path,
        work_dir / "audio_stt.wav",
        sample_rate=16000,
        channels=1,
    )
    audio_orig_path = await ffmpeg.extract_audio(
        source_video_path,
        work_dir / "audio_orig.wav",
        sample_rate=44100,
        channels=2,
    )

    await _emit("separate", 22, "Separating vocals and background audio...")
    separation_provider = ProviderFactory.get_separation(
        override=cfg.separation_provider,
        settings=cfg_settings,
    )
    sep_result = await separation_provider.separate(audio_orig_path, work_dir)

    if (
        sep_result.mode == "fast_mode"
        or sep_result.vocals_path is None
        or sep_result.vocals_path == audio_orig_path
    ):
        vocals_for_stt = audio_stt_path
    else:
        vocals_for_stt = Path(sep_result.vocals_path)

    bgm_path = Path(sep_result.bgm_path) if sep_result.bgm_path is not None else None

    result = Stage1Result(
        source_video_path=source_video_path,
        audio_stt_path=audio_stt_path,
        audio_orig_path=audio_orig_path,
        vocals_path=vocals_for_stt,
        bgm_path=bgm_path,
        separation_mode=sep_result.mode,
        duration_ms=duration_ms,
        duration_seconds=duration_seconds,
        width=probe.width,
        height=probe.height,
        has_audio=probe.has_audio,
        credits_estimated=credits_estimated,
    )
    save_translator_state(work_dir, result)

    await _emit(
        "separate",
        25,
        f"Audio separation complete (mode={sep_result.mode})",
    )
    logger.info(
        "Stage 1 complete for project=%s (duration=%.2fs, credits=%d, sep_mode=%s)",
        project.id,
        duration_seconds,
        credits_estimated,
        sep_result.mode,
    )
    return result
