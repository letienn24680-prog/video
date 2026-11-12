"""HITL media review upload and visual swap endpoints for AI Video Studio."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from pydantic import BaseModel

from backend.api.v1.projects import get_repository
from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    InvalidStateTransitionError,
    ProviderError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.core.security import CurrentUser, get_current_user
from backend.models.enums import ProjectStatus, VisualProviderName, VisualSource
from backend.models.scene import SceneResponse, SceneUpdateRequest
from backend.providers.factory import ProviderFactory
from backend.providers.sandbox_fallback import SandboxVisualProvider, is_configured_key
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["Media Review"])

_CHUNK_SIZE_BYTES = 1024 * 1024  # 1 MiB streaming chunks

_ALLOWED_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".flac",
    }
)

_ALLOWED_VISUAL_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }
)


class SourceUploadResponse(BaseModel):
    """Response returned after uploading a source video or audio file."""

    source_file_key: str
    filename: str
    content_type: str
    size_bytes: int
    storage_provider: str
    signed_url: str | None = None


class SwapVisualRequest(BaseModel):
    """Payload to fetch/generate a replacement visual asset for a scene during review."""

    query: str | None = None
    prompt: str | None = None
    visual_provider: VisualProviderName | None = None
    media_type: Literal["video", "image"] = "video"


def _validate_extension(filename: str | None, allowed: frozenset[str], kind: str) -> tuple[str, str]:
    clean_name = Path(filename or "").name.strip()
    if not clean_name:
        raise ValidationError(f"Uploaded {kind} file must include a valid filename")
    ext = Path(clean_name).suffix.lower()
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported {kind} file extension '{ext or '(none)'}'. Allowed: {sorted(allowed)}",
            details={"filename": clean_name, "allowed_extensions": sorted(allowed)},
        )
    return clean_name, ext


def _resolve_content_type(upload: UploadFile, filename: str) -> str:
    if upload.content_type and upload.content_type != "application/octet-stream":
        return upload.content_type
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


async def _stream_upload_to_path(
    upload: UploadFile,
    dest_path: Path,
    *,
    max_upload_mb: int,
) -> int:
    """Stream an UploadFile to disk while enforcing MAX_UPLOAD_MB without buffering all in RAM."""
    max_bytes = max(1, int(max_upload_mb)) * 1024 * 1024
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0

    try:
        with dest_path.open("wb") as fh:
            while True:
                chunk = await upload.read(_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValidationError(
                        f"Uploaded file exceeds MAX_UPLOAD_MB limit ({max_upload_mb} MB)",
                        code="FILE_TOO_LARGE",
                        details={"max_upload_mb": max_upload_mb},
                    )
                await asyncio.to_thread(fh.write, chunk)
    except Exception:
        try:
            if dest_path.exists():
                dest_path.unlink()
        except OSError:
            pass
        raise

    if total_bytes <= 0:
        try:
            if dest_path.exists():
                dest_path.unlink()
        except OSError:
            pass
        raise ValidationError("Uploaded file is empty")

    return total_bytes


async def _store_uploaded_file(
    local_path: Path,
    remote_key: str,
    content_type: str,
    settings: Settings,
    *,
    cleanup_local_on_remote_upload: bool = False,
) -> tuple[str, str | None, str]:
    """Upload staged file via BaseStorageProvider, falling back to local path if unconfigured."""
    try:
        storage = ProviderFactory.get_storage(settings=settings)
    except ConfigurationError as exc:
        logger.warning(
            "Storage provider '%s' unavailable (%s); keeping staged local file %s",
            settings.STORAGE_PROVIDER,
            exc.message,
            local_path,
        )
        return str(local_path), local_path.as_uri(), "local_temp"

    try:
        stored = await storage.upload_file(
            local_path=local_path,
            remote_key=remote_key,
            content_type=content_type,
        )
    except ConfigurationError as exc:
        logger.warning(
            "Storage upload skipped due to configuration (%s); keeping staged local file %s",
            exc.message,
            local_path,
        )
        return str(local_path), local_path.as_uri(), "local_temp"

    if cleanup_local_on_remote_upload and storage.provider_name != "local":
        try:
            if local_path.exists():
                local_path.unlink()
        except OSError:
            pass

    accessible_url = stored.signed_url or stored.public_url or stored.remote_key
    return stored.remote_key, accessible_url, storage.provider_name


@router.post(
    "/upload-source",
    response_model=SourceUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a source video or audio file for a Translator project",
)
async def upload_source_media(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> SourceUploadResponse:
    """Validate, stream, and store an uploaded source video/audio file returning source_file_key."""
    try:
        clean_name, ext = _validate_extension(file.filename, _ALLOWED_SOURCE_EXTENSIONS, "source")
        content_type = _resolve_content_type(file, clean_name)

        upload_id = uuid4().hex
        staged_path = settings.temp_path / "uploads" / str(user.id) / f"{upload_id}{ext}"
        size_bytes = await _stream_upload_to_path(
            file,
            staged_path,
            max_upload_mb=settings.MAX_UPLOAD_MB,
        )

        remote_key = f"{user.id}/uploads/{upload_id}{ext}"
        source_file_key, signed_url, provider_name = await _store_uploaded_file(
            staged_path,
            remote_key,
            content_type,
            settings,
            cleanup_local_on_remote_upload=True,
        )

        logger.info(
            "Uploaded source media user=%s key=%s size=%d provider=%s",
            user.id,
            source_file_key,
            size_bytes,
            provider_name,
        )
        return SourceUploadResponse(
            source_file_key=source_file_key,
            filename=clean_name,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_provider=provider_name,
            signed_url=signed_url,
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to upload source media for user=%s: %s", user.id, exc)
        raise ProviderError("storage", f"Source media upload failed: {exc}", retryable=True) from exc
    finally:
        await file.close()


def _visual_provider_missing_key(provider_key: str, settings: Settings) -> bool:
    """Return True when a configured cloud visual provider lacks a valid API key."""
    key_map = {
        "pexels": settings.PEXELS_API_KEY,
        "pixabay": settings.PIXABAY_API_KEY,
        "flux": settings.FLUX_API_KEY,
        "stable_diffusion": settings.SD_API_KEY,
    }
    if provider_key not in key_map:
        return False
    return not is_configured_key(key_map[provider_key])


def _resolve_alt_stock_provider(provider_key: str, settings: Settings) -> str | None:
    """Return a configured alternate stock provider (pixabay/pexels) when available."""
    for alt_key, raw_secret in (
        ("pixabay", settings.PIXABAY_API_KEY),
        ("pexels", settings.PEXELS_API_KEY),
    ):
        if alt_key != provider_key and is_configured_key(raw_secret):
            return alt_key
    return None


@router.post(
    "/{project_id}/scenes/{scene_id}/swap-visual",
    response_model=SceneResponse,
    summary="Swap a scene visual via stock search or AI generation during AWAITING_REVIEW",
)
async def swap_scene_visual(
    project_id: UUID,
    scene_id: UUID,
    payload: SwapVisualRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SceneResponse:
    """Fetch or generate a new visual asset for a scene while project is AWAITING_REVIEW."""
    cfg = getattr(request.app.state, "settings", None) or settings
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status != ProjectStatus.AWAITING_REVIEW:
        raise InvalidStateTransitionError(
            f"Scene visuals can only be swapped when project is AWAITING_REVIEW (current: {project.status.value})",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    scene = await repo.get_scene(project_id, scene_id)
    effective_query = (
        payload.query
        or scene.search_keywords
        or scene.narration_text
        or scene.edited_text
        or ""
    ).strip()
    effective_prompt = (payload.prompt or scene.image_prompt or effective_query or "").strip() or None

    if not effective_query and not effective_prompt:
        raise ValidationError("Either query or prompt must be provided to swap scene visual")

    provider_override: str | None = (
        payload.visual_provider.value
        if payload.visual_provider is not None
        else (project.pipeline_config or {}).get("visual_provider")
    )
    resolved_provider_key = (provider_override or cfg.VISUAL_PROVIDER).strip().lower()
    scenes_dir = cfg.temp_path / str(project.id) / "scenes"

    missing_primary_key = _visual_provider_missing_key(resolved_provider_key, cfg)
    alt_stock_provider = (
        _resolve_alt_stock_provider(resolved_provider_key, cfg)
        if missing_primary_key and (resolved_provider_key in {"pexels", "pixabay"} or cfg.LOCAL_SANDBOX_MODE)
        else None
    )

    if alt_stock_provider is not None:
        logger.info(
            "Using alternate stock provider '%s' for swap_scene_visual (requested '%s' missing API key)",
            alt_stock_provider,
            resolved_provider_key,
        )
        visual_provider = ProviderFactory.get_visual(override=alt_stock_provider, settings=cfg)
    elif cfg.LOCAL_SANDBOX_MODE and missing_primary_key:
        logger.info(
            "Using SandboxVisualProvider fallback for swap_scene_visual (provider=%s missing API key)",
            resolved_provider_key,
        )
        visual_provider = SandboxVisualProvider(settings=cfg)
    else:
        try:
            visual_provider = ProviderFactory.get_visual(override=provider_override, settings=cfg)
        except ConfigurationError:
            if not cfg.LOCAL_SANDBOX_MODE:
                raise
            logger.info(
                "Using SandboxVisualProvider fallback after ConfigurationError initializing provider=%s",
                resolved_provider_key,
            )
            visual_provider = SandboxVisualProvider(settings=cfg)
        except StudioError:
            raise
        except Exception as exc:
            raise ProviderError(
                provider=resolved_provider_key,
                message=f"Failed to initialize visual provider: {exc}",
            ) from exc

    try:
        asset = await visual_provider.fetch_visual(
            query=effective_query or (effective_prompt or ""),
            prompt=effective_prompt,
            aspect_ratio=project.aspect_ratio,
            media_type=payload.media_type,
            output_dir=scenes_dir,
        )
    except ConfigurationError:
        if not cfg.LOCAL_SANDBOX_MODE or isinstance(visual_provider, SandboxVisualProvider):
            raise
        logger.info(
            "Using SandboxVisualProvider fallback after ConfigurationError from provider=%s",
            visual_provider.provider_name,
        )
        fallback_provider = SandboxVisualProvider(settings=cfg)
        try:
            asset = await fallback_provider.fetch_visual(
                query=effective_query or (effective_prompt or ""),
                prompt=effective_prompt,
                aspect_ratio=project.aspect_ratio,
                media_type=payload.media_type,
                output_dir=scenes_dir,
            )
        except StudioError:
            raise
        except Exception as exc:
            logger.exception(
                "Sandbox fallback visual swap failed for project=%s scene=%s: %s",
                project_id,
                scene_id,
                exc,
            )
            raise ProviderError(
                provider=fallback_provider.provider_name,
                message=f"Visual swap failed: {exc}",
                retryable=True,
            ) from exc
    except StudioError:
        raise
    except Exception as exc:
        logger.exception(
            "Visual swap failed for project=%s scene=%s provider=%s: %s",
            project_id,
            scene_id,
            visual_provider.provider_name,
            exc,
        )
        raise ProviderError(
            provider=visual_provider.provider_name,
            message=f"Visual swap failed: {exc}",
            retryable=True,
        ) from exc

    updates = SceneUpdateRequest(
        visual_url=asset.url,
        visual_source=asset.source,
    )
    updated_scene = await repo.update_scene(project_id, scene_id, updates)
    logger.info(
        "Swapped visual for project=%s scene=%s source=%s url=%s",
        project_id,
        scene_id,
        asset.source.value,
        asset.url,
    )
    return updated_scene


@router.post(
    "/{project_id}/scenes/{scene_id}/upload-visual",
    response_model=SceneResponse,
    summary="Upload a custom clip or image for a scene during AWAITING_REVIEW",
)
async def upload_scene_visual(
    project_id: UUID,
    scene_id: UUID,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SceneResponse:
    """Upload a custom video/image asset for a scene and set visual_source='custom_upload'."""
    try:
        project = await repo.get_project(project_id, user_id=user.id)
        if project.status != ProjectStatus.AWAITING_REVIEW:
            raise InvalidStateTransitionError(
                f"Custom scene visuals can only be uploaded when project is AWAITING_REVIEW (current: {project.status.value})",
                details={"project_id": str(project_id), "status": project.status.value},
            )

        scene = await repo.get_scene(project_id, scene_id)
        clean_name, ext = _validate_extension(file.filename, _ALLOWED_VISUAL_EXTENSIONS, "visual")
        content_type = _resolve_content_type(file, clean_name)

        file_token = f"custom_{scene.id}_{uuid4().hex[:8]}{ext}"
        local_path = settings.temp_path / str(project.id) / "scenes" / file_token
        size_bytes = await _stream_upload_to_path(
            file,
            local_path,
            max_upload_mb=settings.MAX_UPLOAD_MB,
        )

        remote_key = f"{user.id}/{project.id}/scenes/{file_token}"
        stored_key, accessible_url, provider_name = await _store_uploaded_file(
            local_path,
            remote_key,
            content_type,
            settings,
            cleanup_local_on_remote_upload=False,
        )
        visual_url = accessible_url or stored_key

        updated_scene = await repo.update_scene(
            project_id,
            scene_id,
            SceneUpdateRequest(
                visual_url=visual_url,
                visual_source=VisualSource.CUSTOM_UPLOAD,
            ),
        )
        logger.info(
            "Uploaded custom visual for project=%s scene=%s size=%d provider=%s",
            project_id,
            scene_id,
            size_bytes,
            provider_name,
        )
        return updated_scene
    except StudioError:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to upload custom visual for project=%s scene=%s: %s",
            project_id,
            scene_id,
            exc,
        )
        raise ProviderError("storage", f"Custom visual upload failed: {exc}", retryable=True) from exc
    finally:
        await file.close()
