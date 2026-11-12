"""Orchestrator for the 2-phase AI Faceless Video Generator pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.events import SSEEventBus, get_event_bus
from backend.core.exceptions import ConfigurationError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.models.enums import ProjectMode, SubscriptionTier
from backend.models.project import PipelineConfig
from backend.models.scene import SceneResponse
from backend.pipelines.faceless.stage1_storyboard import run_stage1_storyboard
from backend.pipelines.faceless.stage2_visuals import run_stage2_visuals
from backend.pipelines.faceless.stage3_captions_tts import run_stage3_captions_tts
from backend.pipelines.faceless.stage4_assemble import run_stage4_assemble
from backend.providers.base.llm import BaseLLMProvider
from backend.providers.base.stt import BaseSTTProvider
from backend.providers.base.visual import BaseVisualProvider
from backend.providers.factory import ProviderFactory
from backend.providers.sandbox_fallback import (
    SandboxLLMProvider,
    SandboxVisualProvider,
    is_configured_key,
)
from backend.services.billing.credit_service import CreditService
from backend.services.media.ass_karaoke import AssKaraokeGenerator
from backend.services.media.ffmpeg_engine import FFmpegEngine
from backend.services.scraper.article_scraper import ArticleScraper
from backend.services.supabase.repository import SupabaseRepository
from backend.workers.cleanup_worker import cleanup_job_dir
from backend.workers.task_queue import AsyncJobQueue

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


def _build_progress_emitter(
    project_id: str,
    *,
    queue: AsyncJobQueue | None,
    repo: SupabaseRepository,
    event_bus: SSEEventBus,
) -> ProgressEmitter:
    async def _emit(
        stage: str,
        pct: int,
        message: str = "",
        *,
        event_type: str = "progress",
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        if queue is not None:
            await queue.emit_progress(
                project_id,
                stage,
                pct,
                message,
                event_type=event_type,
                extra_data=extra_data,
            )
            return

        clamped_pct = max(0, min(100, int(pct)))
        try:
            await repo.update_project_progress(
                project_id,
                progress_pct=clamped_pct,
                current_stage=stage,
            )
        except Exception as exc:
            logger.warning("Failed to persist progress for project %s: %s", project_id, exc)

        payload: dict[str, Any] = {
            "project_id": str(project_id),
            "stage": stage,
            "progress_pct": clamped_pct,
            "message": message,
        }
        if extra_data:
            payload.update(extra_data)
        await event_bus.publish(str(project_id), event_type, payload)

    return _emit


_VISUAL_KEY_ATTRS: dict[str, str] = {
    "pexels": "PEXELS_API_KEY",
    "pixabay": "PIXABAY_API_KEY",
    "flux": "FLUX_API_KEY",
    "stable_diffusion": "SD_API_KEY",
}
_STOCK_FALLBACK_ORDER: tuple[tuple[str, str], ...] = (
    ("pixabay", "PIXABAY_API_KEY"),
    ("pexels", "PEXELS_API_KEY"),
)


async def _resolve_faceless_llm_provider(
    cfg: PipelineConfig,
    settings: Settings,
    context: dict[str, Any],
    emit_progress: ProgressEmitter,
) -> BaseLLMProvider:
    injected = context.get("llm_provider")
    if injected is not None:
        return injected

    if settings.LOCAL_SANDBOX_MODE and not is_configured_key(settings.GEMINI_API_KEY):
        await emit_progress(
            "storyboard",
            8,
            "Chưa có GEMINI_API_KEY — dùng Local Sandbox Storyboard Generator",
        )
        return SandboxLLMProvider(settings=settings)

    return ProviderFactory.get_llm(override=cfg.llm_provider, settings=settings)


async def _resolve_faceless_visual_provider(
    cfg: PipelineConfig,
    settings: Settings,
    context: dict[str, Any],
    emit_progress: ProgressEmitter,
    *,
    stage: str,
    pct: int,
) -> BaseVisualProvider:
    injected = context.get("visual_provider")
    if injected is not None:
        return injected

    chosen_key = (
        cfg.visual_provider.value
        if cfg.visual_provider is not None
        else settings.VISUAL_PROVIDER
    ).strip().lower()
    key_attr = _VISUAL_KEY_ATTRS.get(chosen_key)
    if key_attr is not None and not is_configured_key(getattr(settings, key_attr, "")):
        for alt_key, alt_attr in _STOCK_FALLBACK_ORDER:
            if alt_key != chosen_key and is_configured_key(getattr(settings, alt_attr, "")):
                await emit_progress(
                    stage,
                    pct,
                    f"Chưa có {key_attr} — tự động chuyển sang {alt_key}",
                )
                return ProviderFactory.get_visual(override=alt_key, settings=settings)

        if settings.LOCAL_SANDBOX_MODE:
            await emit_progress(
                stage,
                pct,
                f"Chưa có {key_attr} — dùng Local Sandbox Visual Generator",
            )
            return SandboxVisualProvider(settings=settings)

    return ProviderFactory.get_visual(override=cfg.visual_provider, settings=settings)


def _resolve_optional_stt(
    cfg: PipelineConfig,
    raw_config: dict[str, Any],
    settings: Settings,
    context: dict[str, Any],
) -> BaseSTTProvider | None:
    injected = context.get("stt_provider")
    if injected is not None:
        return injected

    if not raw_config.get("align_captions_with_stt") and not context.get("use_stt_alignment"):
        return None

    try:
        return ProviderFactory.get_stt(override=cfg.stt_provider, settings=settings)
    except ConfigurationError as exc:
        logger.warning("Optional STT alignment requested but unavailable: %s", exc.message)
        return None


async def run_faceless_prepare(
    project_id: str,
    context: dict[str, Any] | None = None,
) -> list[SceneResponse]:
    """Phase 1 ('prepare'): resolve content -> storyboard -> credits -> visuals -> AWAITING_REVIEW."""
    ctx = context or {}
    queue: AsyncJobQueue | None = ctx.get("queue")
    settings: Settings = ctx.get("settings") or (queue.settings if queue else get_settings())
    repo: SupabaseRepository = ctx.get("repository") or (
        queue.repository if queue else SupabaseRepository()
    )
    event_bus: SSEEventBus = ctx.get("event_bus") or (
        queue.event_bus if queue else get_event_bus()
    )

    emit_progress = _build_progress_emitter(
        project_id,
        queue=queue,
        repo=repo,
        event_bus=event_bus,
    )

    project = await repo.get_project(project_id)
    work_dir: Path = settings.temp_path / str(project.id)
    work_dir.mkdir(parents=True, exist_ok=True)

    cfg = PipelineConfig.model_validate(project.pipeline_config or {})

    llm_provider = await _resolve_faceless_llm_provider(cfg, settings, ctx, emit_progress)
    visual_provider = await _resolve_faceless_visual_provider(
        cfg,
        settings,
        ctx,
        emit_progress,
        stage="visuals",
        pct=35,
    )
    article_scraper: ArticleScraper = ctx.get("article_scraper") or ArticleScraper()
    credit_service: CreditService = ctx.get("credit_service") or CreditService(
        billing_repo=ctx.get("billing_repo") or repo,
        settings=settings,
    )

    logger.info(
        "Starting Faceless prepare phase for project=%s (llm=%s, visual=%s)",
        project.id,
        llm_provider.provider_name,
        visual_provider.provider_name,
    )

    storyboard = await run_stage1_storyboard(
        project=project,
        cfg=cfg,
        llm_provider=llm_provider,
        repo=repo,
        credit_service=credit_service,
        article_scraper=article_scraper,
        emit_progress=emit_progress,
    )

    refreshed_project = await repo.get_project(project.id)
    scenes = await run_stage2_visuals(
        project=refreshed_project,
        storyboard=storyboard,
        cfg=cfg,
        work_dir=work_dir,
        visual_provider=visual_provider,
        repo=repo,
        emit_progress=emit_progress,
    )
    return scenes


async def run_faceless_render(
    project_id: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Phase 2 ('render'): TTS + .ass karaoke -> clip assembly + BGM + watermark -> upload."""
    ctx = context or {}
    queue: AsyncJobQueue | None = ctx.get("queue")
    settings: Settings = ctx.get("settings") or (queue.settings if queue else get_settings())
    repo: SupabaseRepository = ctx.get("repository") or (
        queue.repository if queue else SupabaseRepository()
    )
    event_bus: SSEEventBus = ctx.get("event_bus") or (
        queue.event_bus if queue else get_event_bus()
    )

    emit_progress = _build_progress_emitter(
        project_id,
        queue=queue,
        repo=repo,
        event_bus=event_bus,
    )

    project = await repo.get_project(project_id)
    work_dir: Path = settings.temp_path / str(project.id)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            profile = await repo.get_profile(project.user_id)
            subscription_tier: str | SubscriptionTier = profile.get(
                "subscription_tier",
                SubscriptionTier.FREE.value,
            )
        except NotFoundError:
            logger.warning(
                "Profile not found for user=%s during render; defaulting subscription_tier='free'",
                project.user_id,
            )
            subscription_tier = SubscriptionTier.FREE.value

        scenes = await repo.list_scenes(project.id)
        if not scenes:
            raise ValidationError(
                f"Project {project.id} has no scenes to render",
                details={"project_id": str(project.id)},
            )

        raw_config = project.pipeline_config or {}
        cfg = PipelineConfig.model_validate(raw_config)

        tts_provider = ctx.get("tts_provider") or ProviderFactory.get_tts(
            override=cfg.tts_provider,
            settings=settings,
        )
        visual_provider = await _resolve_faceless_visual_provider(
            cfg,
            settings,
            ctx,
            emit_progress,
            stage="assemble_clips",
            pct=81,
        )
        storage_provider = ctx.get("storage_provider") or ProviderFactory.get_storage(
            override=cfg.storage_provider,
            settings=settings,
        )
        stt_provider = _resolve_optional_stt(cfg, raw_config, settings, ctx)
        ffmpeg_engine: FFmpegEngine = ctx.get("ffmpeg_engine") or FFmpegEngine(settings=settings)
        ass_generator: AssKaraokeGenerator = ctx.get("ass_generator") or AssKaraokeGenerator()

        logger.info(
            "Starting Faceless render phase for project=%s (scenes=%d, tts=%s, storage=%s, tier=%s)",
            project.id,
            len(scenes),
            tts_provider.provider_name,
            storage_provider.provider_name,
            subscription_tier,
        )

        stage3_result = await run_stage3_captions_tts(
            project=project,
            scenes=scenes,
            cfg=cfg,
            settings=settings,
            work_dir=work_dir,
            tts_provider=tts_provider,
            ffmpeg_engine=ffmpeg_engine,
            repo=repo,
            stt_provider=stt_provider,
            ass_generator=ass_generator,
            emit_progress=emit_progress,
        )

        output_video_url = await run_stage4_assemble(
            project=project,
            stage3_result=stage3_result,
            cfg=cfg,
            settings=settings,
            subscription_tier=subscription_tier,
            work_dir=work_dir,
            ffmpeg_engine=ffmpeg_engine,
            visual_provider=visual_provider,
            storage_provider=storage_provider,
            repo=repo,
            emit_progress=emit_progress,
        )
        return output_video_url
    finally:
        if settings.CLEANUP_AFTER_RENDER:
            try:
                await cleanup_job_dir(
                    project.id,
                    temp_dir=settings.temp_path,
                    repository=repo,
                )
            except Exception as exc:
                logger.warning(
                    "Post-render cleanup failed for project=%s: %s",
                    project.id,
                    exc,
                )


def register_faceless_handlers(queue: AsyncJobQueue) -> None:
    """Register AI Faceless Video prepare and render handlers on ``AsyncJobQueue``."""
    queue.register_handler(ProjectMode.FACELESS, "prepare", run_faceless_prepare)
    queue.register_handler(ProjectMode.FACELESS, "render", run_faceless_render)
