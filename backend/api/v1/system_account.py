"""Account profile, system diagnostics, and temporary storage cleanup endpoints."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from backend.api.v1.projects import get_repository
from backend.core.config import Settings, get_settings
from backend.core.exceptions import AuthorizationError, NotFoundError, StudioError, ValidationError
from backend.core.logging import get_logger
from backend.core.security import CurrentUser, get_current_user
from backend.models.enums import ProjectStatus
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

router = APIRouter(prefix="", tags=["System & Account"])

_PROTECTED_TEMP_ENTRIES: frozenset[str] = frozenset(
    {
        "local_storage",
        "sandbox_db.json",
        "bgm_presets",
        "bgm_uploads",
        "thumbnails",
        "batch_zips",
        ".gitkeep",
    }
)

_CONFIG_UPDATE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "PEXELS_API_KEY",
        "PIXABAY_API_KEY",
        "REPLICATE_API_TOKEN",
        "ELEVENLABS_API_KEY",
        "FPT_AI_API_KEY",
        "VIETTEL_AI_TOKEN",
        "FLUX_API_KEY",
        "SD_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_JWT_SECRET",
        "VIETQR_BANK_BIN",
        "VIETQR_ACCOUNT_NO",
        "VIETQR_ACCOUNT_NAME",
        "SEPAY_API_KEY",
        "SEPAY_WEBHOOK_SECRET",
        "FFMPEG_BIN",
        "FFPROBE_BIN",
        "LOCAL_SANDBOX_MODE",
        "STORAGE_PROVIDER",
        "SEPARATION_PROVIDER",
        "STT_PROVIDER",
        "LLM_PROVIDER",
        "TTS_PROVIDER",
        "VISUAL_PROVIDER",
    }
)

_NON_SECRET_TOGGLE_KEYS: frozenset[str] = frozenset(
    {
        "LOCAL_SANDBOX_MODE",
        "STORAGE_PROVIDER",
        "SEPARATION_PROVIDER",
        "STT_PROVIDER",
        "LLM_PROVIDER",
        "TTS_PROVIDER",
        "VISUAL_PROVIDER",
        "FFMPEG_BIN",
        "FFPROBE_BIN",
        "VIETQR_BANK_BIN",
        "VIETQR_ACCOUNT_NO",
        "VIETQR_ACCOUNT_NAME",
    }
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AccountProfileResponse(BaseModel):
    """Normalized account profile and wallet summary for the current user."""

    id: str
    email: str | None = None
    display_name: str | None = None
    tier: str = "free"
    credit_balance: int = 0
    is_sandbox: bool = False
    created_at: Any | None = None


class AccountProfileUpdateRequest(BaseModel):
    """Payload to update editable user profile attributes."""

    display_name: str = Field(..., min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def _strip_display_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class SystemCleanupResponse(BaseModel):
    """Summary of stale UUID job directories removed from TEMP_DIR."""

    cleaned_dirs: list[str] = Field(default_factory=list)
    cleaned_count: int = 0


class SystemConfigUpdateRequest(BaseModel):
    """Payload to update allowlisted .env configuration keys at runtime."""

    updates: dict[str, str | bool] = Field(default_factory=dict)


class SystemConfigUpdateResponse(BaseModel):
    """Summary of updated configuration keys without echoing secret values."""

    updated_keys: list[str] = Field(default_factory=list)
    requires_restart: bool = False


def _resolve_settings(request: Request, settings: Settings) -> Settings:
    return getattr(request.app.state, "settings", None) or settings


def _is_valid_uuid_name(name: str) -> bool:
    if not name or name.startswith(".") or name in _PROTECTED_TEMP_ENTRIES:
        return False
    try:
        parsed = UUID(name)
    except ValueError:
        return False
    return str(parsed) == name.lower()


def _has_configured_secret(raw_value: str | None) -> bool:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if (
        "your-" in lowered
        or "your_" in lowered
        or "change-me" in lowered
        or lowered.endswith("_here")
        or cleaned == "0123456789"
        or lowered == "nguyen van a"
    ):
        return False
    return True


def _format_env_value(value: str) -> str:
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or "#" in value or '"' in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    existing_lines: list[str] = []
    if env_path.is_file():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key_part = line.split("=", 1)[0].strip()
        if key_part in remaining:
            new_lines.append(f"{key_part}={_format_env_value(remaining.pop(key_part))}")
        else:
            new_lines.append(line)

    for key, val in remaining.items():
        new_lines.append(f"{key}={_format_env_value(val)}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _resolve_binary(bin_name: str) -> str | None:
    cleaned = (bin_name or "").strip()
    if not cleaned:
        return None
    found = shutil.which(cleaned)
    if found:
        return found
    candidate = Path(cleaned)
    if candidate.is_file():
        return str(candidate)
    return None


def _probe_tool_version_sync(bin_path: str) -> str | None:
    try:
        proc = subprocess.run(
            [bin_path, "-version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    if not output:
        return None
    first_line = output.splitlines()[0].strip()
    return first_line[:160] if first_line else None


def _scan_temp_storage_sync(temp_root: Path) -> tuple[int, float]:
    if not temp_root.exists() or not temp_root.is_dir():
        return 0, 0.0

    job_dirs = 0
    total_bytes = 0
    for entry in temp_root.iterdir():
        if entry.is_dir() and _is_valid_uuid_name(entry.name):
            job_dirs += 1
        try:
            if entry.is_file():
                total_bytes += entry.stat().st_size
            elif entry.is_dir():
                for child in entry.rglob("*"):
                    if child.is_file():
                        total_bytes += child.stat().st_size
        except OSError:
            continue

    size_mb = round(total_bytes / (1024 * 1024), 2)
    return job_dirs, size_mb


def _build_profile_payload(
    user: CurrentUser,
    profile: dict[str, Any],
    credits: dict[str, Any],
    cfg: Settings,
) -> AccountProfileResponse:
    tier = str(profile.get("subscription_tier") or profile.get("tier") or "free")
    return AccountProfileResponse(
        id=str(user.id),
        email=profile.get("email") or user.email,
        display_name=profile.get("display_name"),
        tier=tier,
        credit_balance=int(credits.get("balance", 0)),
        is_sandbox=bool(getattr(cfg, "LOCAL_SANDBOX_MODE", False)),
        created_at=profile.get("created_at"),
    )


@router.get(
    "/account/profile",
    response_model=AccountProfileResponse,
    summary="Get the authenticated user's profile, tier, and credit balance",
)
async def get_account_profile(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> AccountProfileResponse:
    """Return profile details, subscription tier, sandbox flag, and wallet balance."""
    cfg = _resolve_settings(request, settings)
    try:
        try:
            profile = await repo.get_profile(user.id)
        except NotFoundError:
            profile = {}
        try:
            credits = await repo.get_user_credits(user.id)
        except NotFoundError:
            credits = {}
        return _build_profile_payload(user, profile, credits, cfg)
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to load account profile for user=%s: %s", user.id, exc)
        raise


@router.patch(
    "/account/profile",
    response_model=AccountProfileResponse,
    summary="Update the authenticated user's profile display_name",
)
async def update_account_profile(
    payload: AccountProfileUpdateRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> AccountProfileResponse:
    """Persist profile edits (display_name) and return the updated account summary."""
    cfg = _resolve_settings(request, settings)
    try:
        updated_profile = await repo.update_profile(
            user.id,
            {"display_name": payload.display_name},
        )
        try:
            credits = await repo.get_user_credits(user.id)
        except NotFoundError:
            credits = {}
        return _build_profile_payload(user, updated_profile, credits, cfg)
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to update account profile for user=%s: %s", user.id, exc)
        raise


@router.get(
    "/system/diagnostics",
    summary="Inspect local media tools, provider readiness, temp storage, and queue status",
)
async def get_system_diagnostics(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return non-secret system setup and runtime health diagnostics."""
    cfg = _resolve_settings(request, settings)

    ffmpeg_path = _resolve_binary(cfg.FFMPEG_BIN or "ffmpeg")
    ffprobe_path = _resolve_binary(cfg.FFPROBE_BIN or "ffprobe")
    ffmpeg_version = (
        await asyncio.to_thread(_probe_tool_version_sync, ffmpeg_path)
        if ffmpeg_path
        else None
    )

    yt_dlp_available = (
        importlib.util.find_spec("yt_dlp") is not None or shutil.which("yt-dlp") is not None
    )
    edge_tts_available = (
        importlib.util.find_spec("edge_tts") is not None or shutil.which("edge-tts") is not None
    )

    temp_job_dirs, temp_size_mb = await asyncio.to_thread(
        _scan_temp_storage_sync,
        cfg.temp_path,
    )

    job_queue = getattr(request.app.state, "job_queue", None)
    cleanup_worker = getattr(request.app.state, "cleanup_worker", None)

    return {
        "tools": {
            "ffmpeg": {
                "available": ffmpeg_path is not None,
                "path": ffmpeg_path,
                "version": ffmpeg_version,
            },
            "ffprobe": {
                "available": ffprobe_path is not None,
                "path": ffprobe_path,
            },
            "yt_dlp": {"available": yt_dlp_available},
            "edge_tts": {"available": edge_tts_available},
        },
        "providers": {
            "groq": _has_configured_secret(cfg.GROQ_API_KEY),
            "gemini": _has_configured_secret(cfg.GEMINI_API_KEY),
            "pexels": _has_configured_secret(cfg.PEXELS_API_KEY),
            "pixabay": _has_configured_secret(cfg.PIXABAY_API_KEY),
            "replicate": _has_configured_secret(cfg.REPLICATE_API_TOKEN),
            "elevenlabs": _has_configured_secret(cfg.ELEVENLABS_API_KEY),
            "fpt_ai": _has_configured_secret(cfg.FPT_AI_API_KEY),
            "viettel_ai": _has_configured_secret(cfg.VIETTEL_AI_TOKEN),
            "supabase_configured": (
                _has_configured_secret(cfg.SUPABASE_URL)
                and _has_configured_secret(cfg.SUPABASE_ANON_KEY)
                and _has_configured_secret(cfg.SUPABASE_SERVICE_ROLE_KEY)
            ),
            "sepay_configured": (
                _has_configured_secret(cfg.SEPAY_API_KEY)
                or _has_configured_secret(cfg.SEPAY_WEBHOOK_SECRET)
            ),
            "local_sandbox_mode": bool(getattr(cfg, "LOCAL_SANDBOX_MODE", False)),
        },
        "storage": {
            "provider": cfg.STORAGE_PROVIDER,
            "temp_dir": str(cfg.temp_path),
            "temp_job_dirs": temp_job_dirs,
            "temp_size_mb": temp_size_mb,
        },
        "queue": {
            "active_jobs": job_queue.active_count() if job_queue is not None else 0,
            "max_concurrent_jobs": int(cfg.MAX_CONCURRENT_JOBS),
            "auto_cleanup_running": (
                bool(cleanup_worker.is_running) if cleanup_worker is not None else False
            ),
        },
    }


@router.post(
    "/system/cleanup",
    response_model=SystemCleanupResponse,
    summary="Clean stale or completed UUID job directories under TEMP_DIR",
)
async def run_system_cleanup(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    repo: SupabaseRepository = Depends(get_repository),
) -> SystemCleanupResponse:
    """Delete inactive UUID job subdirectories in TEMP_DIR while preserving active jobs and system folders."""
    del user  # Authentication required; user identity is enforced by Depends(get_current_user).
    cfg = _resolve_settings(request, settings)
    temp_root = cfg.temp_path.resolve()
    if not temp_root.exists() or not temp_root.is_dir():
        return SystemCleanupResponse(cleaned_dirs=[], cleaned_count=0)

    job_queue = getattr(request.app.state, "job_queue", None)
    cleaned_dirs: list[str] = []

    for entry in sorted(temp_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not _is_valid_uuid_name(entry.name):
            continue

        resolved = entry.resolve()
        if resolved.parent != temp_root:
            continue

        if job_queue is not None and job_queue.is_running(entry.name):
            continue

        # Also avoid deleting directories for projects actively PROCESSING or RENDERING
        try:
            proj = await repo.get_project(entry.name)
            if proj.status in (ProjectStatus.PROCESSING, ProjectStatus.RENDERING):
                continue
        except NotFoundError:
            pass
        except Exception as exc:
            logger.debug("Skipping project status check during cleanup for %s: %s", entry.name, exc)

        try:
            await asyncio.to_thread(shutil.rmtree, resolved)
            cleaned_dirs.append(entry.name)
            try:
                await repo.mark_temp_cleaned(entry.name)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Failed to remove temp job dir '%s': %s", entry.name, exc)

    logger.info("Manual system cleanup removed %d temp job dir(s)", len(cleaned_dirs))
    return SystemCleanupResponse(cleaned_dirs=cleaned_dirs, cleaned_count=len(cleaned_dirs))


@router.post(
    "/system/config",
    response_model=SystemConfigUpdateResponse,
    summary="Update allowlisted .env configuration keys and reload Settings in development/sandbox mode",
)
async def update_system_config(
    payload: SystemConfigUpdateRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> SystemConfigUpdateResponse:
    """Persist allowlisted API keys and studio settings to .env and refresh runtime Settings."""
    del user
    cfg = _resolve_settings(request, settings)
    is_dev = cfg.APP_ENV == "development"
    is_sandbox = bool(getattr(cfg, "LOCAL_SANDBOX_MODE", False))
    if not (is_dev or is_sandbox):
        raise AuthorizationError(
            "Runtime configuration updates are only allowed in development or local sandbox mode",
            code="CONFIG_UPDATE_FORBIDDEN",
        )

    sanitized_updates: dict[str, str] = {}
    for raw_key, raw_val in (payload.updates or {}).items():
        key = str(raw_key or "").strip().upper()
        if key not in _CONFIG_UPDATE_ALLOWLIST:
            continue

        if isinstance(raw_val, bool):
            str_val = "true" if raw_val else "false"
        else:
            str_val = str(raw_val or "").replace("\r", "").replace("\n", "").strip()

        if str_val == "__CLEAR__":
            sanitized_updates[key] = ""
        elif str_val:
            sanitized_updates[key] = str_val
        elif key in _NON_SECRET_TOGGLE_KEYS and isinstance(raw_val, bool):
            sanitized_updates[key] = "false"

    if not sanitized_updates:
        return SystemConfigUpdateResponse(updated_keys=[], requires_restart=False)

    effective_pexels = (
        sanitized_updates["PEXELS_API_KEY"]
        if "PEXELS_API_KEY" in sanitized_updates
        else cfg.PEXELS_API_KEY
    )
    if (
        _has_configured_secret(sanitized_updates.get("PIXABAY_API_KEY"))
        and not _has_configured_secret(effective_pexels)
        and "VISUAL_PROVIDER" not in sanitized_updates
    ):
        sanitized_updates["VISUAL_PROVIDER"] = "pixabay"

    env_override = getattr(request.app.state, "env_file_path", None)
    env_path = Path(env_override) if env_override is not None else (_PROJECT_ROOT / ".env")

    try:
        await asyncio.to_thread(_upsert_env_file, env_path, sanitized_updates)
    except OSError as exc:
        logger.exception("Failed updating .env file at %s: %s", env_path, exc)
        raise ValidationError(f"Failed to update configuration file: {exc}") from exc

    get_settings.cache_clear()
    try:
        if env_override is not None:
            new_settings = Settings(_env_file=env_path)
        else:
            new_settings = Settings()
        request.app.state.settings = new_settings
        job_queue = getattr(request.app.state, "job_queue", None)
        if job_queue is not None and hasattr(job_queue, "settings"):
            job_queue.settings = new_settings
        cleanup_worker = getattr(request.app.state, "cleanup_worker", None)
        if cleanup_worker is not None and hasattr(cleanup_worker, "settings"):
            cleanup_worker.settings = new_settings
    except Exception as exc:
        logger.warning("Rebuilding Settings after .env update failed: %s", exc)

    updated_keys = list(sanitized_updates.keys())
    logger.info("Updated runtime .env config keys (%d): %s", len(updated_keys), ", ".join(updated_keys))
    return SystemConfigUpdateResponse(updated_keys=updated_keys, requires_restart=False)
