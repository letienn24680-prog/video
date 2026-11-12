"""Orchestrator for the 4-stage AI Video Translator & Auto-Dubber pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.events import SSEEventBus, get_event_bus
from backend.core.exceptions import NotFoundError
from backend.core.logging import get_logger
from backend.models.enums import ProjectMode, ProjectStatus, SubscriptionTier
from backend.models.project import PipelineConfig
from backend.pipelines.translator.stage1_separate import run_stage1_separate
from backend.pipelines.translator.stage2_stt import run_stage2_stt
from backend.pipelines.translator.stage3_translate import run_stage3_translate
from backend.pipelines.translator.stage4_dub_mix import run_stage4_dub_mix
from backend.services.supabase.repository import SupabaseRepository
from backend.workers.cleanup_worker import cleanup_job_dir
from backend.workers.task_queue import AsyncJobQueue

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


def _resolve_runtime_deps(
    context: dict[str, Any],
) -> tuple[Settings, SupabaseRepository, SSEEventBus, AsyncJobQueue | None]:
    queue: AsyncJobQueue | None = context.get("queue")
    settings: Settings = context.get("settings") or (queue.settings if queue else get_settings())
    repo: SupabaseRepository = context.get("repository") or (
        queue.repository if queue else SupabaseRepository()
    )
    event_bus: SSEEventBus = context.get("event_bus") or (
        queue.event_bus if queue else get_event_bus()
    )
    return settings, repo, event_bus, queue


def _make_emitter(
    project_id: str,
    repo: SupabaseRepository,
    event_bus: SSEEventBus,
    queue: AsyncJobQueue | None,
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


async def run_translator_prepare(project_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Execute Translator Stages 1-3 and pause in ``AWAITING_REVIEW``."""
    settings, repo, event_bus, queue = _resolve_runtime_deps(context)
    emit = _make_emitter(project_id, repo, event_bus, queue)

    project = await repo.get_project(project_id)
    work_dir = settings.temp_path / str(project.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    cfg = PipelineConfig.model_validate(project.pipeline_config or {})

    logger.info("Starting Translator prepare phase for project=%s", project.id)

    stage1_result = await run_stage1_separate(
        project=project,
        cfg=cfg,
        work_dir=work_dir,
        settings=settings,
        repo=repo,
        emit=emit,
    )
    transcript = await run_stage2_stt(
        vocals_path=stage1_result.vocals_path,
        cfg=cfg,
        settings=settings,
        emit=emit,
        duration_ms=stage1_result.duration_ms,
    )
    saved_scenes = await run_stage3_translate(
        project=project,
        transcript=transcript,
        cfg=cfg,
        settings=settings,
        repo=repo,
        emit=emit,
    )

    return {
        "project_id": str(project.id),
        "status": ProjectStatus.AWAITING_REVIEW.value,
        "scenes_count": len(saved_scenes),
        "duration_seconds": stage1_result.duration_seconds,
    }


async def run_translator_render(project_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Execute Translator Stage 4 after HITL review, upload final video, and clean up temp."""
    settings, repo, event_bus, queue = _resolve_runtime_deps(context)
    emit = _make_emitter(project_id, repo, event_bus, queue)

    try:
        project = await repo.get_project(project_id)
        try:
            profile = await repo.get_profile(project.user_id)
            subscription_tier = str(
                profile.get("subscription_tier") or SubscriptionTier.FREE.value
            )
        except NotFoundError:
            logger.warning(
                "Profile not found for user=%s; defaulting subscription_tier to 'free'",
                project.user_id,
            )
            subscription_tier = SubscriptionTier.FREE.value

        scenes = await repo.list_scenes(project.id)
        cfg = PipelineConfig.model_validate(project.pipeline_config or {})
        work_dir = settings.temp_path / str(project.id)
        work_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Starting Translator render phase for project=%s (scenes=%d, tier=%s)",
            project.id,
            len(scenes),
            subscription_tier,
        )

        signed_url = await run_stage4_dub_mix(
            project=project,
            scenes=scenes,
            cfg=cfg,
            subscription_tier=subscription_tier,
            work_dir=work_dir,
            settings=settings,
            repo=repo,
            emit=emit,
        )
        await event_bus.close_project(str(project.id))

        return {
            "project_id": str(project.id),
            "status": ProjectStatus.COMPLETED.value,
            "output_video_url": signed_url,
        }
    finally:
        if settings.CLEANUP_AFTER_RENDER:
            try:
                await cleanup_job_dir(
                    project_id,
                    temp_dir=settings.temp_path,
                    repository=repo,
                )
            except Exception as exc:
                logger.warning(
                    "Post-render cleanup failed for project %s: %s",
                    project_id,
                    exc,
                )


def register_translator_handlers(queue: AsyncJobQueue) -> None:
    """Register Translator 'prepare' and 'render' handlers on ``AsyncJobQueue``."""
    queue.register_handler(ProjectMode.TRANSLATOR, "prepare", run_translator_prepare)
    queue.register_handler(ProjectMode.TRANSLATOR, "render", run_translator_render)
    logger.info("Registered Translator pipeline handlers on AsyncJobQueue")
