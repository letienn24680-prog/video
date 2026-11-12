"""Stage 5 Studio Feature Pack routes: Sandbox Auth, BGM, Marketing Kit, Thumbnails, Subtitles, Batch ZIP, and Tier Upgrade."""

from __future__ import annotations

import asyncio
import re
import zipfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Body, Depends, File, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

from backend.api.v1.billing import get_billing_repository
from backend.api.v1.media_review import (
    _resolve_content_type,
    _store_uploaded_file,
    _stream_upload_to_path,
    _validate_extension,
)
from backend.api.v1.projects import get_repository
from backend.core.config import Settings, get_settings
from backend.core.exceptions import AuthorizationError, NotFoundError, ProviderError, StudioError
from backend.core.logging import get_logger
from backend.core.security import CurrentUser, get_current_user, issue_sandbox_account_token
from backend.models.enums import AspectRatio, ProjectStatus
from backend.models.project import ProjectResponse, SubtitleStyleConfig
from backend.providers.factory import ProviderFactory
from backend.services.ai_extras.marketing_kit import MarketingKit, MarketingKitService
from backend.services.ai_extras.thumbnail_gen import ThumbnailResult, ThumbnailService
from backend.services.media.bgm_library import BGMLibraryService, BGMPreset
from backend.services.media.subtitle_exporter import SubtitleExporter
from backend.services.supabase.billing_repo import SupabaseBillingRepository
from backend.services.supabase.local_sandbox import (
    SANDBOX_DEMO_EMAIL,
    SANDBOX_DEMO_USER_ID,
    LocalSandboxRepository,
)
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

router = APIRouter(tags=["Studio Features"])

_ALLOWED_BGM_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg"})
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SandboxUserInfo(BaseModel):
    """Minimal user metadata returned alongside a sandbox demo bearer token."""

    id: str
    email: str


class SandboxCredentialsRequest(BaseModel):
    """Email and password payload for local sandbox account registration or login."""

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=6, max_length=200)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        cleaned = value.strip().lower()
        local_part, sep, domain = cleaned.partition("@")
        if not sep or not local_part or "." not in domain or any(ch.isspace() for ch in cleaned):
            raise ValueError("Please provide a valid email address")
        return cleaned


class SandboxTokenResponse(BaseModel):
    """Response payload for local sandbox auth endpoints."""

    access_token: str
    token_type: str = "bearer"
    user: SandboxUserInfo


class BgmUploadResponse(BaseModel):
    """Response payload after uploading a custom BGM track."""

    bgm_file_key: str
    filename: str
    url: str


class ThumbnailGenerateRequest(BaseModel):
    """Optional custom prompt override for AI thumbnail generation."""

    prompt: str | None = Field(default=None, max_length=1000)


class UpgradeTierRequest(BaseModel):
    """Request payload to upgrade subscription tier using credits."""

    target_tier: Literal["pro", "enterprise"]


class UpgradeTierResponse(BaseModel):
    """Response payload returned after a successful credit-based tier upgrade."""

    user_id: str
    subscription_tier: Literal["pro", "enterprise"]
    credits_deducted: int
    new_balance: int


def _resolve_settings(request: Request, settings: Settings) -> Settings:
    return getattr(request.app.state, "settings", None) or settings


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.debug("Could not delete temporary file %s", path)


def _sanitize_slug(raw: str, fallback: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", (raw or "").strip()).strip("._-")
    return cleaned[:48] or fallback


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = f"//{parsed.netloc}{raw_path}"
    return Path(raw_path)


def _resolve_local_media_candidate(raw_url: str, cfg: Settings) -> Path | None:
    cleaned = raw_url.strip()
    if cleaned.startswith("file://"):
        candidate = _file_uri_to_path(cleaned)
        return candidate if candidate.is_file() else None

    if cleaned.startswith("/local-media/"):
        rel = cleaned[len("/local-media/") :].lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return None

        temp_root = cfg.temp_path.resolve()
        if rel.startswith("storage/"):
            storage_rel = rel[len("storage/") :]
            for storage_root in (
                (temp_root / "local_storage").resolve(),
                (temp_root.parent / "local_storage").resolve(),
            ):
                candidate = (storage_root / storage_rel).resolve()
                try:
                    candidate.relative_to(storage_root)
                    if candidate.is_file():
                        return candidate
                except ValueError:
                    continue

        candidate = (temp_root / rel).resolve()
        try:
            candidate.relative_to(temp_root)
            if candidate.is_file():
                return candidate
        except ValueError:
            pass
        return None

    if not cleaned.startswith(("http://", "https://")):
        direct = Path(cleaned)
        return direct if direct.is_file() else None
    return None


async def _materialize_project_video(
    project: ProjectResponse,
    work_dir: Path,
    cfg: Settings,
) -> Path | None:
    """Resolve or download a completed project's video file into ``work_dir`` if available."""
    raw_url = (project.output_video_url or "").strip()
    if not raw_url:
        return None

    local_candidate = _resolve_local_media_candidate(raw_url, cfg)
    if local_candidate is not None:
        return local_candidate

    if raw_url.startswith(("http://", "https://")):
        suffix = Path(urlparse(raw_url).path).suffix.lower() or ".mp4"
        dest = work_dir / f"{project.id}{suffix}"
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                async with client.stream("GET", raw_url) as resp:
                    resp.raise_for_status()
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
            if dest.is_file() and dest.stat().st_size > 0:
                return dest
        except Exception as exc:
            logger.warning(
                "Failed downloading video for batch zip (project=%s): %s",
                project.id,
                exc,
            )
            _safe_unlink(dest)
        return None

    try:
        storage = ProviderFactory.get_storage(settings=cfg)
        dest = work_dir / f"{project.id}.mp4"
        await storage.download_file(raw_url, dest)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
    except Exception as exc:
        logger.debug("Storage download fallback skipped for project=%s: %s", project.id, exc)
    return None


def _build_zip_archive(zip_path: Path, entries: list[tuple[str, Path | bytes]]) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, payload in entries:
            if isinstance(payload, Path):
                zf.write(payload, arcname=arcname)
            else:
                zf.writestr(arcname, payload)
    return zip_path


@router.post(
    "/auth/sandbox-token",
    response_model=SandboxTokenResponse,
    summary="Issue the local sandbox demo bearer token when LOCAL_SANDBOX_MODE=true",
)
async def issue_sandbox_token(
    request: Request,
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SandboxTokenResponse:
    """Return the deterministic local sandbox demo token when sandbox mode is enabled."""
    cfg = _resolve_settings(request, settings)
    if not cfg.LOCAL_SANDBOX_MODE:
        raise AuthorizationError(
            "Local sandbox authentication is disabled",
            code="SANDBOX_DISABLED",
        )

    if isinstance(repo, LocalSandboxRepository):
        await repo.get_user_credits(SANDBOX_DEMO_USER_ID)

    return SandboxTokenResponse(
        access_token="sandbox-demo-token",
        token_type="bearer",
        user=SandboxUserInfo(
            id=SANDBOX_DEMO_USER_ID,
            email=SANDBOX_DEMO_EMAIL,
        ),
    )


def _require_local_sandbox_repo(
    request: Request,
    settings: Settings,
    repo: SupabaseRepository,
) -> tuple[Settings, LocalSandboxRepository]:
    """Ensure local sandbox mode is enabled and LocalSandboxRepository is active."""
    cfg = _resolve_settings(request, settings)
    if not cfg.LOCAL_SANDBOX_MODE:
        raise AuthorizationError(
            "Local sandbox authentication is disabled",
            code="SANDBOX_DISABLED",
        )
    if not isinstance(repo, LocalSandboxRepository):
        raise AuthorizationError(
            "Local sandbox repository is unavailable",
            code="SANDBOX_REPO_UNAVAILABLE",
        )
    return cfg, repo


@router.post(
    "/auth/sandbox-register",
    response_model=SandboxTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a local sandbox account and return an HMAC-signed bearer token",
)
async def register_sandbox_account(
    payload: SandboxCredentialsRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SandboxTokenResponse:
    """Create a local sandbox account and issue a signed sandbox account token."""
    cfg, sandbox_repo = _require_local_sandbox_repo(request, settings, repo)
    account = await sandbox_repo.register_local_account(payload.email, payload.password)
    uid = str(account.get("id") or account["user_id"])
    email = str(account["email"])
    token = issue_sandbox_account_token(uid, email, settings=cfg)
    return SandboxTokenResponse(
        access_token=token,
        token_type="bearer",
        user=SandboxUserInfo(id=uid, email=email),
    )


@router.post(
    "/auth/sandbox-login",
    response_model=SandboxTokenResponse,
    summary="Sign in to a local sandbox account and return an HMAC-signed bearer token",
)
async def login_sandbox_account(
    payload: SandboxCredentialsRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SandboxTokenResponse:
    """Authenticate a local sandbox account and issue a signed sandbox account token."""
    cfg, sandbox_repo = _require_local_sandbox_repo(request, settings, repo)
    account = await sandbox_repo.login_local_account(payload.email, payload.password)
    uid = str(account.get("id") or account["user_id"])
    email = str(account["email"])
    token = issue_sandbox_account_token(uid, email, settings=cfg)
    return SandboxTokenResponse(
        access_token=token,
        token_type="bearer",
        user=SandboxUserInfo(id=uid, email=email),
    )


@router.get(
    "/bgm/presets",
    response_model=list[BGMPreset],
    summary="List curated background music mood presets",
)
async def list_bgm_presets(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> list[BGMPreset]:
    """Return all curated BGM mood presets."""
    cfg = _resolve_settings(request, settings)
    return BGMLibraryService(settings=cfg).list_presets()


@router.get(
    "/bgm/presets/{preset_id}/preview",
    summary="Stream or download a synthesized WAV preview for a BGM mood preset",
)
async def preview_bgm_preset(
    preset_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Synthesize (if not cached) and return the WAV preview for ``preset_id``."""
    cfg = _resolve_settings(request, settings)
    service = BGMLibraryService(settings=cfg)
    wav_path = await asyncio.to_thread(service.ensure_preset_audio, preset_id, 15)
    return FileResponse(
        path=str(wav_path),
        media_type="audio/wav",
        filename=f"{preset_id}.wav",
    )


@router.post(
    "/projects/upload-bgm",
    response_model=BgmUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a custom background music track for projects",
)
async def upload_custom_bgm(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> BgmUploadResponse:
    """Validate and store a custom BGM audio file under MAX_UPLOAD_MB."""
    cfg = _resolve_settings(request, settings)
    try:
        clean_name, ext = _validate_extension(file.filename, _ALLOWED_BGM_EXTENSIONS, "BGM audio")
        content_type = _resolve_content_type(file, clean_name)

        file_token = f"{uuid4().hex[:12]}{ext}"
        local_path = cfg.temp_path / "bgm_uploads" / str(user.id) / file_token
        await _stream_upload_to_path(
            file,
            local_path,
            max_upload_mb=cfg.MAX_UPLOAD_MB,
        )

        remote_key = f"{user.id}/bgm/{file_token}"
        stored_key, accessible_url, provider_name = await _store_uploaded_file(
            local_path,
            remote_key,
            content_type,
            cfg,
            cleanup_local_on_remote_upload=False,
        )

        if provider_name == "local_temp":
            bgm_file_key = f"bgm_uploads/{user.id}/{file_token}"
            resolved_url = f"/local-media/{bgm_file_key}"
        else:
            bgm_file_key = stored_key
            resolved_url = accessible_url or f"/local-media/bgm_uploads/{user.id}/{file_token}"

        return BgmUploadResponse(
            bgm_file_key=bgm_file_key,
            filename=clean_name,
            url=resolved_url,
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Custom BGM upload failed for user=%s: %s", user.id, exc)
        raise ProviderError("storage", f"BGM upload failed: {exc}", retryable=True) from exc
    finally:
        await file.close()


@router.post(
    "/projects/{project_id}/marketing-kit",
    response_model=MarketingKit,
    summary="Generate and persist an AI Viral Marketing Kit for a project",
)
async def generate_project_marketing_kit(
    project_id: UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> MarketingKit:
    """Generate viral hooks, SEO titles, description, hashtags, and save to pipeline_config."""
    cfg = _resolve_settings(request, settings)
    project = await repo.get_project(project_id, user_id=user.id)
    scenes = await repo.list_scenes(project_id)

    kit = await MarketingKitService().generate(project, scenes, settings=cfg)
    updated_config = {
        **(project.pipeline_config or {}),
        "marketing_kit": kit.model_dump(mode="json"),
    }
    await repo.update_project(project_id, {"pipeline_config": updated_config}, user_id=user.id)
    return kit


@router.post(
    "/projects/{project_id}/thumbnail",
    response_model=ThumbnailResult,
    summary="Generate and persist a cover thumbnail for a project",
)
async def generate_project_thumbnail(
    project_id: UUID,
    request: Request,
    payload: ThumbnailGenerateRequest | None = Body(default=None),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> ThumbnailResult:
    """Generate an AI/video-frame/studio-SVG thumbnail and persist pipeline_config['thumbnail_url']."""
    cfg = _resolve_settings(request, settings)
    project = await repo.get_project(project_id, user_id=user.id)
    scenes = await repo.list_scenes(project_id)

    storage = None
    try:
        storage = ProviderFactory.get_storage(settings=cfg)
    except Exception:
        storage = None

    prompt = payload.prompt if payload is not None else None
    result = await ThumbnailService().generate_thumbnail(
        project,
        scenes,
        prompt=prompt,
        settings=cfg,
        storage=storage,
    )

    updated_config = {
        **(project.pipeline_config or {}),
        "thumbnail_url": result.url,
    }
    await repo.update_project(project_id, {"pipeline_config": updated_config}, user_id=user.id)
    return result


@router.get(
    "/projects/{project_id}/subtitles/export",
    summary="Download project subtitles as .srt or .ass",
)
async def export_project_subtitles(
    project_id: UUID,
    request: Request,
    format: Literal["srt", "ass"] = Query(default="srt"),
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> Response:
    """Export project scenes as a downloadable .srt or .ass subtitle file."""
    cfg = _resolve_settings(request, settings)
    project = await repo.get_project(project_id, user_id=user.id)
    scenes = await repo.list_scenes(project_id)

    exporter = SubtitleExporter()
    if format == "srt":
        content = exporter.export_srt(scenes)
        media_type = "application/x-subrip; charset=utf-8"
    else:
        raw_style = (project.pipeline_config or {}).get("subtitle_style")
        style = (
            SubtitleStyleConfig.model_validate(raw_style)
            if isinstance(raw_style, dict)
            else SubtitleStyleConfig()
        )
        aspect_ratio = (
            project.aspect_ratio
            if isinstance(project.aspect_ratio, AspectRatio)
            else AspectRatio(str(project.aspect_ratio))
        )
        temp_ass = cfg.temp_path / str(project.id) / f"export_{uuid4().hex[:8]}.ass"
        try:
            exporter.export_ass(scenes, aspect_ratio, style, temp_ass)
            content = temp_ass.read_text(encoding="utf-8")
        finally:
            _safe_unlink(temp_ass)
        media_type = "text/plain; charset=utf-8"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="subtitles_{project_id}.{format}"',
        },
    )


@router.get(
    "/projects/batch/{batch_id}/download-zip",
    summary="Download a ZIP archive containing completed batch videos and .srt subtitles",
)
async def download_batch_zip(
    batch_id: UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> FileResponse:
    """Bundle all completed projects in ``batch_id`` with .srt subtitles into a ZIP file."""
    cfg = _resolve_settings(request, settings)
    all_projects = await repo.list_projects(user_id=user.id, limit=500, offset=0)
    completed_batch = [
        p
        for p in all_projects
        if p.batch_id == batch_id and p.status == ProjectStatus.COMPLETED
    ]
    if not completed_batch:
        raise NotFoundError(
            f"No completed projects found for batch {batch_id}",
            details={"batch_id": str(batch_id)},
        )

    work_dir = cfg.temp_path / "batch_zips" / f"work_{batch_id}_{uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cfg.temp_path / "batch_zips" / f"batch_{batch_id}_{uuid4().hex[:8]}.zip"

    exporter = SubtitleExporter()
    entries: list[tuple[str, Path | bytes]] = []
    temp_downloads: list[Path] = []

    try:
        for idx, proj in enumerate(completed_batch, start=1):
            slug = _sanitize_slug(proj.title, f"project_{idx}")
            prefix = f"{idx:02d}_{slug}_{str(proj.id)[:8]}"

            scenes = await repo.list_scenes(proj.id)
            srt_content = exporter.export_srt(scenes).encode("utf-8")
            entries.append((f"{prefix}.srt", srt_content))

            video_path = await _materialize_project_video(proj, work_dir, cfg)
            if video_path is not None and video_path.is_file():
                ext = video_path.suffix.lower() or ".mp4"
                entries.append((f"{prefix}{ext}", video_path))
                if video_path.parent == work_dir:
                    temp_downloads.append(video_path)

        await asyncio.to_thread(_build_zip_archive, zip_path, entries)
    except Exception:
        _safe_unlink(zip_path)
        raise
    finally:
        for dl in temp_downloads:
            _safe_unlink(dl)
        try:
            work_dir.rmdir()
        except OSError:
            pass

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"batch_{batch_id}.zip",
        background=BackgroundTask(_safe_unlink, zip_path),
    )


@router.post(
    "/billing/upgrade-tier",
    response_model=UpgradeTierResponse,
    summary="Upgrade subscription tier (pro or enterprise) by spending credits",
)
async def upgrade_billing_tier(
    payload: UpgradeTierRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> UpgradeTierResponse:
    """Deduct PRO_UPGRADE_CREDITS or ENTERPRISE_UPGRADE_CREDITS and upgrade profiles.subscription_tier."""
    cfg = _resolve_settings(request, settings)
    cost = (
        cfg.PRO_UPGRADE_CREDITS
        if payload.target_tier == "pro"
        else cfg.ENTERPRISE_UPGRADE_CREDITS
    )
    result = await billing_repo.upgrade_subscription_tier(
        user.id,
        payload.target_tier,
        cost,
    )
    return UpgradeTierResponse(**result)
