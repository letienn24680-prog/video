"""Project lifecycle, batch creation, SSE streaming, HITL review, render, and cancel routes."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from backend.core.config import Settings, get_settings
from backend.core.events import SSEEventBus, get_event_bus
from backend.core.exceptions import (
    InvalidStateTransitionError,
    ProviderError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.core.security import CurrentUser, get_current_user
from backend.models.enums import (
    TERMINAL_STATUSES,
    ProjectMode,
    ProjectStatus,
    can_transition,
)
from backend.models.project import (
    BatchProjectCreateRequest,
    CreateFacelessProjectRequest,
    CreateTranslatorProjectRequest,
    DuplicateProjectRequest,
    ProjectDetailResponse,
    ProjectResponse,
    ProjectUpdateRequest,
    RenderConfirmRequest,
)
from backend.models.scene import (
    PreviewTTSRequest,
    SceneAppendRequest,
    SceneCreate,
    SceneResponse,
    SceneUpdateRequest,
)
from backend.providers.factory import ProviderFactory
from backend.services.supabase.repository import SupabaseRepository
from backend.workers.task_queue import AsyncJobQueue

logger = get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["Projects"])

_STREAM_STOP_EVENTS = frozenset({"completed", "error", "awaiting_review", "cancelled"})


def get_repository(request: Request) -> SupabaseRepository:
    """Resolve the shared SupabaseRepository from app.state or construct a fallback."""
    repo = getattr(request.app.state, "repository", None)
    if repo is None:
        repo = SupabaseRepository()
        request.app.state.repository = repo
    return repo


def get_job_queue(request: Request) -> AsyncJobQueue:
    """Resolve the shared AsyncJobQueue from app.state or construct a fallback."""
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        queue = AsyncJobQueue(
            settings=get_settings(),
            repository=get_repository(request),
            event_bus=get_event_bus_dep(request),
        )
        request.app.state.job_queue = queue
    return queue


def get_event_bus_dep(request: Request) -> SSEEventBus:
    """Resolve the shared SSEEventBus from app.state or return the singleton."""
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        bus = get_event_bus()
        request.app.state.event_bus = bus
    return bus


def _extract_project_create_fields(
    payload: CreateTranslatorProjectRequest | CreateFacelessProjectRequest,
) -> tuple[str, dict[str, Any]]:
    if isinstance(payload, CreateTranslatorProjectRequest):
        source_mode = "url" if (payload.source_url and payload.source_url.strip()) else "upload"
        source_inputs: dict[str, Any] = {
            "source_url": payload.source_url,
            "source_file_key": payload.source_file_key,
        }
        return source_mode, source_inputs

    source_inputs = {
        "topic": payload.topic,
        "script": payload.script,
        "article_url": payload.article_url,
        "target_duration_seconds": payload.target_duration_seconds,
    }
    return payload.source_mode, source_inputs


async def _create_and_enqueue_project(
    *,
    user: CurrentUser,
    payload: CreateTranslatorProjectRequest | CreateFacelessProjectRequest,
    repo: SupabaseRepository,
    job_queue: AsyncJobQueue,
    batch_id: UUID | None = None,
) -> ProjectResponse:
    source_mode, source_inputs = _extract_project_create_fields(payload)
    project = await repo.create_project(
        user.id,
        title=payload.title,
        mode=payload.mode,
        aspect_ratio=payload.aspect_ratio,
        source_mode=source_mode,
        source_inputs=source_inputs,
        pipeline_config=payload.pipeline_config.model_dump(mode="json"),
        batch_id=batch_id,
    )
    await job_queue.enqueue(
        project.id,
        project.mode,
        "prepare",
        context={
            "user_id": user.id,
            "project": project.model_dump(mode="json"),
        },
    )
    return project


def _resolve_default_voice(provider_name: str, settings: Settings) -> str:
    defaults = {
        "edge_tts": settings.EDGE_TTS_DEFAULT_VOICE,
        "elevenlabs": settings.ELEVENLABS_DEFAULT_VOICE_ID,
        "fpt_ai": settings.FPT_AI_DEFAULT_VOICE,
        "viettel_ai": settings.VIETTEL_AI_DEFAULT_VOICE,
    }
    return defaults.get(provider_name, settings.EDGE_TTS_DEFAULT_VOICE)


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Translator or Faceless project and enqueue Stage 1-3 preparation",
)
async def create_project(
    payload: CreateTranslatorProjectRequest | CreateFacelessProjectRequest = Body(...),
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
) -> ProjectResponse:
    """Create a new project in Supabase and start its asynchronous prepare job."""
    return await _create_and_enqueue_project(
        user=user,
        payload=payload,
        repo=repo,
        job_queue=job_queue,
    )


@router.post(
    "/batch",
    status_code=status.HTTP_201_CREATED,
    summary="Batch create projects up to MAX_BATCH_SIZE and enqueue preparation jobs",
)
async def create_batch_projects(
    payload: BatchProjectCreateRequest,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
) -> dict[str, Any]:
    """Validate batch size against MAX_BATCH_SIZE, create projects, and enqueue jobs."""
    if len(payload.items) > settings.MAX_BATCH_SIZE:
        raise ValidationError(
            f"Batch size {len(payload.items)} exceeds MAX_BATCH_SIZE ({settings.MAX_BATCH_SIZE})",
            details={
                "max_batch_size": settings.MAX_BATCH_SIZE,
                "received": len(payload.items),
            },
        )

    batch_id = uuid4()
    created: list[ProjectResponse] = []
    for item in payload.items:
        project = await _create_and_enqueue_project(
            user=user,
            payload=item,
            repo=repo,
            job_queue=job_queue,
            batch_id=batch_id,
        )
        created.append(project)

    return {
        "batch_id": str(batch_id),
        "batch_title": payload.batch_title,
        "count": len(created),
        "projects": [proj.model_dump(mode="json") for proj in created],
    }


@router.get("", response_model=list[ProjectResponse], summary="List user projects")
async def list_projects(
    status_filter: ProjectStatus | None = Query(default=None, alias="status"),
    mode: ProjectMode | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> list[ProjectResponse]:
    """List projects owned by the authenticated user."""
    return await repo.list_projects(
        user.id,
        status=status_filter,
        mode=mode,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectDetailResponse,
    summary="Get project details and ordered scenes",
)
async def get_project_detail(
    project_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> ProjectDetailResponse:
    """Return a single project with its ordered project_scenes."""
    return await repo.get_project_detail(project_id, user_id=user.id)


@router.get("/{project_id}/events", summary="Stream real-time project progress via SSE")
async def stream_project_events(
    project_id: UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    event_bus: SSEEventBus = Depends(get_event_bus_dep),
) -> EventSourceResponse:
    """Stream an initial status snapshot followed by live SSEEventBus events."""
    project = await repo.get_project(project_id, user_id=user.id)
    pid = str(project.id)

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        snapshot = {
            "project_id": pid,
            "status": project.status.value,
            "progress_pct": project.progress_pct,
            "current_stage": project.current_stage,
            "output_video_url": project.output_video_url,
            "error_message": project.error_message,
        }
        yield {
            "event": "status",
            "id": f"snapshot-{pid}",
            "data": json.dumps(snapshot, default=str),
        }

        if project.status == ProjectStatus.AWAITING_REVIEW:
            yield {
                "event": "awaiting_review",
                "id": f"review-{pid}",
                "data": json.dumps(
                    {"project_id": pid, "progress_pct": project.progress_pct},
                    default=str,
                ),
            }
            return

        if project.status in TERMINAL_STATUSES:
            terminal_event = (
                "completed"
                if project.status == ProjectStatus.COMPLETED
                else "cancelled"
                if project.status == ProjectStatus.CANCELLED
                else "error"
            )
            yield {
                "event": terminal_event,
                "id": f"terminal-{pid}",
                "data": json.dumps(snapshot, default=str),
            }
            return

        async with event_bus.subscribe(pid) as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    continue

                if event is None:
                    break

                yield event.to_sse_dict()
                if event.event in _STREAM_STOP_EVENTS:
                    break

    return EventSourceResponse(event_generator())


@router.patch(
    "/{project_id}/scenes/{scene_id}",
    response_model=SceneResponse,
    summary="HITL edit of scene text, timing, voice, or lock state during AWAITING_REVIEW",
)
async def update_scene(
    project_id: UUID,
    scene_id: UUID,
    payload: SceneUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> SceneResponse:
    """Update a scene/subtitle segment while the project is in AWAITING_REVIEW."""
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status != ProjectStatus.AWAITING_REVIEW:
        raise InvalidStateTransitionError(
            f"Scenes can only be edited when project is AWAITING_REVIEW (current: {project.status.value})",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    existing_scene = await repo.get_scene(project_id, scene_id)
    new_start = payload.start_ms if payload.start_ms is not None else existing_scene.start_ms
    new_end = payload.end_ms if payload.end_ms is not None else existing_scene.end_ms
    if new_end < new_start:
        raise ValidationError(
            "end_ms must be greater than or equal to start_ms",
            details={"start_ms": new_start, "end_ms": new_end},
        )

    return await repo.update_scene(project_id, scene_id, payload)


@router.post(
    "/{project_id}/scenes/{scene_id}/preview-tts",
    summary="Generate and return a single-line TTS audio preview for HITL review",
)
async def preview_scene_tts(
    project_id: UUID,
    scene_id: UUID,
    payload: PreviewTTSRequest = Body(default_factory=PreviewTTSRequest),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> Response:
    """Synthesize a single scene's line via ProviderFactory.get_tts and return audio bytes."""
    project = await repo.get_project(project_id, user_id=user.id)
    scene = await repo.get_scene(project_id, scene_id)

    text = (
        payload.text
        or scene.edited_text
        or scene.narration_text
        or scene.original_text
        or ""
    ).strip()
    if not text:
        raise ValidationError("No text available for TTS preview on this scene")

    provider_override: str | None = (
        payload.tts_provider.value
        if payload.tts_provider is not None
        else project.pipeline_config.get("tts_provider")
    )
    resolved_provider_key = (provider_override or settings.TTS_PROVIDER).strip().lower()

    try:
        tts_provider = ProviderFactory.get_tts(override=provider_override, settings=settings)
    except StudioError:
        raise
    except Exception as exc:
        raise ProviderError(
            provider=resolved_provider_key,
            message=f"Failed to initialize TTS provider: {exc}",
        ) from exc

    voice_id = (
        payload.voice_id
        or scene.voice_id
        or project.pipeline_config.get("voice_id")
        or _resolve_default_voice(tts_provider.provider_name, settings)
    )
    rate_pct = payload.tts_rate_pct if "tts_rate_pct" in payload.model_fields_set else scene.tts_rate_pct

    preview_dir: Path = settings.temp_path / str(project.id) / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    output_path = preview_dir / f"preview_{scene.id}_{uuid4().hex[:8]}.mp3"
    synthesized_path: Path | None = None

    try:
        result = await tts_provider.synthesize(
            text=text,
            voice_id=voice_id,
            output_path=output_path,
            rate_pct=rate_pct,
        )
        synthesized_path = Path(result.audio_path)
        if not synthesized_path.exists():
            raise ProviderError(
                provider=tts_provider.provider_name,
                message="TTS provider did not produce an output audio file",
            )
        audio_bytes = await asyncio.to_thread(synthesized_path.read_bytes)
        media_type, _ = mimetypes.guess_type(str(synthesized_path))
        return Response(
            content=audio_bytes,
            media_type=media_type or "audio/mpeg",
            headers={
                "X-Audio-Duration-Ms": str(result.duration_ms),
                "X-TTS-Provider": result.provider,
                "X-Voice-Id": result.voice_id,
                "Cache-Control": "no-store",
            },
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception(
            "TTS preview synthesis failed for project=%s scene=%s: %s",
            project_id,
            scene_id,
            exc,
        )
        raise ProviderError(
            provider=tts_provider.provider_name,
            message=f"TTS preview synthesis failed: {exc}",
            retryable=True,
        ) from exc
    finally:
        for candidate in {p for p in (output_path, synthesized_path) if p is not None}:
            try:
                if candidate.exists():
                    candidate.unlink()
            except OSError:
                pass


@router.post(
    "/{project_id}/render",
    response_model=ProjectResponse,
    summary="Confirm HITL review and start final video rendering",
)
async def start_project_render(
    project_id: UUID,
    payload: RenderConfirmRequest | None = Body(default=None),
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
) -> ProjectResponse:
    """Transition AWAITING_REVIEW -> RENDERING and enqueue the final render job."""
    project = await repo.get_project(project_id, user_id=user.id)
    if not can_transition(project.status, ProjectStatus.RENDERING):
        raise InvalidStateTransitionError(
            f"Cannot start render from status {project.status.value}; project must be AWAITING_REVIEW",
            details={
                "project_id": str(project_id),
                "current_status": project.status.value,
                "target_status": ProjectStatus.RENDERING.value,
            },
        )

    if payload is not None:
        merged_config = dict(project.pipeline_config or {})
        if payload.pipeline_config is not None:
            merged_config.update(payload.pipeline_config.model_dump(mode="json"))
        if payload.burn_subtitles is not None:
            merged_config["burn_subtitles"] = payload.burn_subtitles
        if payload.voice_id is not None:
            merged_config["voice_id"] = payload.voice_id
        if payload.speaker_voices is not None:
            merged_config["speaker_voices"] = payload.speaker_voices
        project = await repo.update_project(
            project_id,
            {"pipeline_config": merged_config},
            user_id=user.id,
        )

    updated = await repo.update_project_status(
        project_id,
        ProjectStatus.RENDERING,
        current_stage="render_queued",
        enforce_fsm=True,
        user_id=user.id,
    )

    await job_queue.enqueue(
        updated.id,
        updated.mode,
        "render",
        context={
            "user_id": user.id,
            "project": updated.model_dump(mode="json"),
        },
    )
    return updated


@router.post(
    "/{project_id}/cancel",
    response_model=ProjectResponse,
    summary="Cancel an active or pending project and refund unused credits",
)
async def cancel_project(
    project_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
    event_bus: SSEEventBus = Depends(get_event_bus_dep),
) -> ProjectResponse:
    """Cancel active job execution, transition status to CANCELLED, and refund credits."""
    project = await repo.get_project(project_id, user_id=user.id)
    if not can_transition(project.status, ProjectStatus.CANCELLED):
        raise InvalidStateTransitionError(
            f"Cannot cancel project in status {project.status.value}",
            details={
                "project_id": str(project_id),
                "current_status": project.status.value,
                "target_status": ProjectStatus.CANCELLED.value,
            },
        )

    await job_queue.cancel(project.id)

    updated = await repo.update_project_status(
        project.id,
        ProjectStatus.CANCELLED,
        current_stage="cancelled",
        enforce_fsm=True,
        user_id=user.id,
    )

    refunded = False
    if project.credits_charged > 0:
        await repo.refund_user_credits(
            user.id,
            project.credits_charged,
            project_id=project.id,
            description="Credit refund on project cancellation",
            metadata={"previous_status": project.status.value},
        )
        refunded = True
        updated = await repo.get_project(project.id, user_id=user.id)

    await event_bus.publish(
        str(project.id),
        "cancelled",
        {
            "project_id": str(project.id),
            "status": ProjectStatus.CANCELLED.value,
            "refunded": refunded,
        },
    )
    await event_bus.close_project(str(project.id))
    return updated


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Rename a project or update its aspect ratio",
)
async def update_project_metadata(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> ProjectResponse:
    """Update editable top-level project metadata (title, aspect_ratio)."""
    project = await repo.get_project(project_id, user_id=user.id)
    updates: dict[str, Any] = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.aspect_ratio is not None:
        updates["aspect_ratio"] = payload.aspect_ratio.value

    if not updates:
        return project
    return await repo.update_project(project_id, updates, user_id=user.id)


@router.delete(
    "/{project_id}",
    summary="Delete a project and remove its temporary working directory",
)
async def delete_project(
    project_id: UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Delete a non-running project and clean up any local temporary files."""
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status in {ProjectStatus.PROCESSING, ProjectStatus.RENDERING}:
        raise InvalidStateTransitionError(
            "Vui lòng hủy dự án đang chạy trước khi xóa",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    await repo.delete_project(project_id, user_id=user.id)

    cfg: Settings = getattr(request.app.state, "settings", None) or settings
    job_dir = cfg.temp_path / str(project_id)
    if job_dir.exists():
        await asyncio.to_thread(shutil.rmtree, job_dir, True)

    return {"deleted": True, "project_id": str(project_id)}


@router.post(
    "/{project_id}/retry",
    response_model=ProjectResponse,
    summary="Retry a FAILED or CANCELLED project from preparation phase",
)
async def retry_project(
    project_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
) -> ProjectResponse:
    """Reset a FAILED or CANCELLED project to PENDING and re-enqueue its prepare job."""
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status not in {ProjectStatus.FAILED, ProjectStatus.CANCELLED}:
        raise InvalidStateTransitionError(
            f"Cannot retry project in status {project.status.value}; only FAILED or CANCELLED projects can be retried",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    updated = await repo.update_project(
        project_id,
        {
            "status": ProjectStatus.PENDING.value,
            "progress_pct": 0,
            "current_stage": "queued",
            "error_message": None,
            "output_video_url": None,
        },
        user_id=user.id,
    )

    await job_queue.enqueue(
        updated.id,
        updated.mode,
        "prepare",
        context={
            "user_id": user.id,
            "project": updated.model_dump(mode="json"),
        },
    )
    return updated


@router.post(
    "/{project_id}/duplicate",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate an existing project and enqueue preparation",
)
async def duplicate_project(
    project_id: UUID,
    payload: DuplicateProjectRequest | None = Body(default=None),
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
    job_queue: AsyncJobQueue = Depends(get_job_queue),
) -> ProjectResponse:
    """Clone an existing project's inputs and pipeline configuration into a new queued project."""
    source = await repo.get_project(project_id, user_id=user.id)
    custom_title = payload.title if payload is not None else None
    new_title = (custom_title or f"{source.title} (Bản sao)")[:200]

    cloned = await repo.create_project(
        user.id,
        title=new_title,
        mode=source.mode,
        aspect_ratio=source.aspect_ratio,
        source_mode=source.source_mode,
        source_inputs=dict(source.source_inputs or {}),
        pipeline_config=dict(source.pipeline_config or {}),
    )
    await job_queue.enqueue(
        cloned.id,
        cloned.mode,
        "prepare",
        context={
            "user_id": user.id,
            "project": cloned.model_dump(mode="json"),
        },
    )
    return cloned


@router.post(
    "/{project_id}/scenes",
    response_model=SceneResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a new storyboard scene or subtitle line during AWAITING_REVIEW",
)
async def append_project_scene(
    project_id: UUID,
    payload: SceneAppendRequest,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> SceneResponse:
    """Append a new scene to a project currently in AWAITING_REVIEW."""
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status != ProjectStatus.AWAITING_REVIEW:
        raise InvalidStateTransitionError(
            f"Scenes can only be added when project is AWAITING_REVIEW (current: {project.status.value})",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    existing = await repo.list_scenes(project_id)
    next_index = max((s.scene_index for s in existing), default=-1) + 1
    last_end_ms = max((s.end_ms for s in existing), default=0)

    fields_set = payload.model_fields_set
    if ("start_ms" not in fields_set and "end_ms" not in fields_set) or (
        payload.start_ms == 0 and payload.end_ms == 0
    ):
        start_ms = last_end_ms
        end_ms = last_end_ms + 3000
    else:
        start_ms = payload.start_ms if "start_ms" in fields_set else last_end_ms
        end_ms = payload.end_ms
        if end_ms < start_ms:
            raise ValidationError(
                "end_ms must be greater than or equal to start_ms",
                details={"start_ms": start_ms, "end_ms": end_ms},
            )

    fallback_text = payload.edited_text or payload.narration_text or payload.original_text
    scene_create = SceneCreate(
        project_id=project_id,
        scene_index=next_index,
        start_ms=start_ms,
        end_ms=end_ms,
        original_text=payload.original_text or fallback_text,
        edited_text=payload.edited_text,
        narration_text=payload.narration_text,
        search_keywords=payload.search_keywords,
        speaker_label=payload.speaker_label,
        voice_id=payload.voice_id,
        tts_rate_pct=payload.tts_rate_pct,
    )
    saved = await repo.upsert_scenes(project_id, [scene_create])
    for item in saved:
        if item.scene_index == next_index:
            return item
    return saved[-1]


@router.delete(
    "/{project_id}/scenes/{scene_id}",
    summary="Delete an unlocked scene during AWAITING_REVIEW",
)
async def delete_project_scene(
    project_id: UUID,
    scene_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    repo: SupabaseRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Delete an unlocked scene from a project in AWAITING_REVIEW, keeping at least 1 scene."""
    project = await repo.get_project(project_id, user_id=user.id)
    if project.status != ProjectStatus.AWAITING_REVIEW:
        raise InvalidStateTransitionError(
            f"Scenes can only be deleted when project is AWAITING_REVIEW (current: {project.status.value})",
            details={"project_id": str(project_id), "status": project.status.value},
        )

    scene = await repo.get_scene(project_id, scene_id)
    if scene.is_locked:
        raise InvalidStateTransitionError(
            "Cannot delete a locked scene; unlock it before deleting",
            details={"project_id": str(project_id), "scene_id": str(scene_id)},
        )

    existing = await repo.list_scenes(project_id)
    if len(existing) < 2:
        raise ValidationError(
            "Project must retain at least 1 scene",
            details={"project_id": str(project_id), "scenes_count": len(existing)},
        )

    await repo.delete_scene(project_id, scene_id)
    return {"deleted": True, "scene_id": str(scene_id)}
