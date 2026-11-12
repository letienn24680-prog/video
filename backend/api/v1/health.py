"""Health check endpoint reporting service status, active queue load, and default providers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from backend.core.config import Settings, get_settings
from backend.providers.factory import ProviderFactory

router = APIRouter(tags=["Health"])

APP_VERSION = "0.1.0"


def _clean_public_value(raw_value: str | None) -> str:
    """Return empty string for untouched .env.example placeholder strings."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if (
        "your-project-ref" in lowered
        or "your_" in lowered
        or lowered.endswith("_here")
        or "change-me" in lowered
    ):
        return ""
    return cleaned


@router.get("/health", summary="Service health and provider status")
async def health_check(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return application liveness, active job queue count, and default provider configuration."""
    job_queue = getattr(request.app.state, "job_queue", None)
    cleanup_worker = getattr(request.app.state, "cleanup_worker", None)

    active_jobs = job_queue.active_count() if job_queue is not None else 0
    cleanup_running = bool(cleanup_worker.is_running) if cleanup_worker is not None else False

    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": APP_VERSION,
        "environment": settings.APP_ENV,
        "active_jobs": active_jobs,
        "max_concurrent_jobs": settings.MAX_CONCURRENT_JOBS,
        "auto_cleanup_running": cleanup_running,
        "default_providers": ProviderFactory.get_default_providers(settings),
    }


@router.get("/public-config", summary="Safe non-secret frontend SPA configuration")
async def get_public_config(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return strictly non-secret public configuration values for the frontend SPA."""
    cfg: Settings = getattr(request.app.state, "settings", None) or settings
    return {
        "app_name": cfg.APP_NAME,
        "supabase_url": _clean_public_value(cfg.SUPABASE_URL),
        "supabase_anon_key": _clean_public_value(cfg.SUPABASE_ANON_KEY),
        "max_batch_size": cfg.MAX_BATCH_SIZE,
        "max_upload_mb": cfg.MAX_UPLOAD_MB,
        "vnd_per_credit": cfg.VND_PER_CREDIT,
        "default_providers": ProviderFactory.get_default_providers(cfg),
        "local_sandbox_mode": cfg.LOCAL_SANDBOX_MODE,
        "pro_upgrade_credits": cfg.PRO_UPGRADE_CREDITS,
        "enterprise_upgrade_credits": cfg.ENTERPRISE_UPGRADE_CREDITS,
    }
