"""Stage 1 of the AI Faceless Video pipeline: input resolution, storyboard, and credit billing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from backend.core.exceptions import ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.models.enums import ProjectMode
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.provider import StoryboardResult
from backend.providers.base.llm import BaseLLMProvider
from backend.services.billing.credit_service import CreditService
from backend.services.scraper.article_scraper import ArticleScraper
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]


async def _noop_progress(*_args: Any, **_kwargs: Any) -> None:
    return None


def _resolve_source_mode(project: ProjectResponse) -> str:
    mode = (project.source_mode or "").strip().lower()
    if mode:
        return mode
    inputs = project.source_inputs or {}
    if str(inputs.get("article_url") or inputs.get("url") or "").strip():
        return "article_url"
    if str(inputs.get("script") or "").strip():
        return "script"
    return "topic"


async def _resolve_source_text(
    project: ProjectResponse,
    scraper: ArticleScraper,
    emit_progress: ProgressEmitter,
) -> str:
    inputs = project.source_inputs or {}
    source_mode = _resolve_source_mode(project)

    if source_mode == "article_url":
        url = str(inputs.get("article_url") or inputs.get("url") or "").strip()
        if not url:
            raise ValidationError(
                "An article URL is required when source_mode='article_url'",
                details={"project_id": str(project.id)},
            )
        await emit_progress(
            "scrape_article",
            15,
            f"Extracting clean article content from {url}...",
        )
        article = await scraper.scrape(url)
        base_text = f"Title: {article.title}\n\n{article.text}" if article.title else article.text
    elif source_mode == "script":
        base_text = str(inputs.get("script") or "").strip()
        if not base_text:
            raise ValidationError(
                "Script text is required when source_mode='script'",
                details={"project_id": str(project.id)},
            )
    elif source_mode == "topic":
        base_text = str(inputs.get("topic") or inputs.get("script") or "").strip()
        if not base_text:
            raise ValidationError(
                "Topic is required when source_mode='topic'",
                details={"project_id": str(project.id)},
            )
    else:
        raise ValidationError(
            f"Unsupported faceless source_mode '{source_mode}'",
            details={"project_id": str(project.id), "source_mode": source_mode},
        )

    target_duration = inputs.get("target_duration_seconds")
    if target_duration is not None:
        try:
            target_sec = int(target_duration)
            if target_sec > 0:
                base_text = (
                    f"{base_text}\n\n"
                    f"[Pacing constraint: aim for approximately {target_sec} seconds total duration]"
                )
        except (TypeError, ValueError):
            pass

    return base_text


async def run_stage1_storyboard(
    project: ProjectResponse,
    cfg: PipelineConfig,
    llm_provider: BaseLLMProvider,
    repo: SupabaseRepository,
    credit_service: CreditService,
    *,
    article_scraper: ArticleScraper | None = None,
    emit_progress: ProgressEmitter | None = None,
) -> StoryboardResult:
    """Resolve input content, generate a 3-5s scene storyboard, and charge estimated credits."""
    notify = emit_progress or _noop_progress
    scraper = article_scraper or ArticleScraper()

    await notify("storyboard", 15, "Resolving source content for storyboard generation...")
    source_text = await _resolve_source_text(project, scraper, notify)

    await notify(
        "storyboard",
        20,
        f"Generating {cfg.scene_duration_min}-{cfg.scene_duration_max}s storyboard scenes...",
    )
    storyboard = await llm_provider.generate_storyboard(
        source_text,
        aspect_ratio=project.aspect_ratio,
        scene_duration_target=(cfg.scene_duration_min, cfg.scene_duration_max),
        tone=cfg.tone,
    )

    if not storyboard.scenes:
        raise ProviderError(
            llm_provider.provider_name,
            "Storyboard generation returned zero scenes",
            retryable=True,
        )

    total_duration = float(storyboard.total_duration_seconds or 0.0)
    if total_duration <= 0.0:
        total_duration = round(sum(max(0.5, float(s.duration_seconds)) for s in storyboard.scenes), 2)
        storyboard.total_duration_seconds = total_duration

    credits_estimated = credit_service.estimate_project_credits(
        ProjectMode.FACELESS,
        total_duration,
        cfg,
    )

    await repo.update_project(
        project.id,
        {
            "duration_seconds": round(total_duration, 2),
            "credits_estimated": credits_estimated,
        },
    )

    delta_credits = max(0, credits_estimated - int(project.credits_charged or 0))
    if delta_credits > 0:
        await credit_service.charge_for_project(
            project.user_id,
            project.id,
            delta_credits,
            description=f"AI Faceless Video storyboard ({len(storyboard.scenes)} scenes, {total_duration:.1f}s)",
            metadata={
                "mode": ProjectMode.FACELESS.value,
                "duration_seconds": round(total_duration, 2),
                "scenes_count": len(storyboard.scenes),
            },
        )

    await notify(
        "storyboard",
        30,
        f"Storyboard ready: {len(storyboard.scenes)} scenes (~{total_duration:.1f}s, {credits_estimated} credits)",
        extra_data={
            "scenes_count": len(storyboard.scenes),
            "duration_seconds": round(total_duration, 2),
            "credits_estimated": credits_estimated,
        },
    )
    logger.info(
        "Stage 1 complete for project=%s: scenes=%d duration=%.2fs credits_est=%d",
        project.id,
        len(storyboard.scenes),
        total_duration,
        credits_estimated,
    )
    return storyboard
