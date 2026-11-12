"""Provider discovery and TTS voice listing endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ProviderError, StudioError
from backend.core.logging import get_logger
from backend.models.provider import VoiceOption
from backend.providers.factory import ProviderFactory

logger = get_logger(__name__)

router = APIRouter(prefix="/providers", tags=["Providers"])


@router.get("", summary="List registered and default AI/storage providers")
async def list_providers(
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return all registered provider keys grouped by domain and current .env defaults."""
    return {
        "providers": ProviderFactory.list_registered_providers(),
        "defaults": ProviderFactory.get_default_providers(settings),
    }


@router.get("/tts/voices", summary="List available voices for a TTS provider")
async def list_tts_voices(
    provider: str | None = Query(
        default=None,
        description="Optional TTS provider override (e.g. edge_tts, elevenlabs, fpt_ai, viettel_ai)",
    ),
    language: str | None = Query(
        default=None,
        description="Optional BCP-47/ISO language filter (e.g. vi, vi-VN, en)",
    ),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Resolve the requested or default TTS provider and list available voices."""
    resolved_key = (provider or settings.TTS_PROVIDER).strip().lower()
    try:
        tts_provider = ProviderFactory.get_tts(override=provider, settings=settings)
        voices: list[VoiceOption] = await tts_provider.list_voices(language=language)
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to list TTS voices for provider '%s': %s", resolved_key, exc)
        raise ProviderError(
            provider=resolved_key,
            message=f"Failed to list TTS voices: {exc}",
            retryable=True,
        ) from exc

    return {
        "provider": tts_provider.provider_name,
        "count": len(voices),
        "voices": [voice.model_dump() for voice in voices],
    }
