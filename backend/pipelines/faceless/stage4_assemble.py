"""Stage 4 of the AI Faceless Video pipeline: clip assembly, BGM ducking, mastering, and upload."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx

from backend.core.config import Settings
from backend.core.exceptions import NotFoundError, ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.models.enums import ProjectStatus, SubscriptionTier
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.scene import SceneResponse
from backend.pipelines.faceless.stage3_captions_tts import Stage3CaptionsTTSResult
from backend.providers.base.storage import BaseStorageProvider
from backend.providers.base.visual import BaseVisualProvider
from backend.services.media.bgm_library import BGMLibraryService
from backend.services.media.downloader import resolve_local_media_url
from backend.services.media.ffmpeg_engine import FFmpegEngine
from backend.services.media.watermark import resolve_watermark_options
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]

_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})


async def _noop_progress(*_args: Any, **_kwargs: Any) -> None:
    return None


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = f"//{parsed.netloc}{raw_path}"
    return Path(raw_path)


def _load_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _infer_extension(url: str, content_type: str | None, default: str = ".mp4") -> str:
    parsed_path = urlparse(url).path
    suffix = Path(parsed_path).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS or suffix in _VIDEO_EXTENSIONS:
        return suffix
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(ct)
        if guessed == ".jpe":
            return ".jpg"
        if guessed:
            return guessed
    return default


async def _download_http_file(url: str, dest_base: Path, *, default_ext: str = ".mp4") -> Path:
    dest_base.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise ProviderError(
                        "visual_download",
                        f"Failed to download asset '{url}' (HTTP {resp.status_code})",
                        retryable=resp.status_code >= 500,
                    )
                ext = _infer_extension(url, resp.headers.get("content-type"), default=default_ext)
                target = dest_base.with_suffix(ext)
                with target.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
    except ProviderError:
        raise
    except httpx.HTTPError as exc:
        raise ProviderError(
            "visual_download",
            f"HTTP error downloading asset '{url}': {exc}",
            retryable=True,
        ) from exc

    if not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise ProviderError("visual_download", f"Downloaded empty asset from '{url}'")
    return target


async def _resolve_media_type(
    local_path: Path,
    ffmpeg_engine: FFmpegEngine,
    hint: str | None = None,
) -> Literal["video", "image"]:
    ext = local_path.suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if hint in ("video", "image"):
        return hint  # type: ignore[return-value]
    probe = await ffmpeg_engine.probe(local_path)
    return "video" if probe.duration_ms > 0 and probe.fps > 1.0 else "image"


async def _ensure_scene_local_asset(
    scene: SceneResponse,
    project: ProjectResponse,
    scenes_dir: Path,
    manifest: dict[str, dict[str, Any]],
    visual_provider: BaseVisualProvider,
    storage_provider: BaseStorageProvider,
    ffmpeg_engine: FFmpegEngine,
    settings: Settings | None = None,
) -> tuple[Path, Literal["video", "image"]]:
    """Ensure a valid local visual file exists for ``scene`` (reusing, downloading, or re-fetching)."""
    entry = manifest.get(str(scene.scene_index))
    if (
        isinstance(entry, dict)
        and entry.get("visual_url") == scene.visual_url
        and entry.get("local_path")
    ):
        cached = Path(str(entry["local_path"]))
        if cached.is_file() and cached.stat().st_size > 0:
            mtype = await _resolve_media_type(cached, ffmpeg_engine, entry.get("media_type"))
            return cached, mtype

    raw_url = (scene.visual_url or "").strip()
    dest_base = scenes_dir / f"scene_{scene.scene_index:04d}"

    if raw_url:
        try:
            if raw_url.startswith("file://"):
                candidate = _file_uri_to_path(raw_url)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate, await _resolve_media_type(candidate, ffmpeg_engine)
            elif raw_url.startswith("/local-media/"):
                candidate = resolve_local_media_url(
                    raw_url,
                    settings,
                    base_dir=getattr(storage_provider, "base_dir", None),
                )
                if candidate is not None and candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate, await _resolve_media_type(candidate, ffmpeg_engine)
            elif raw_url.startswith(("http://", "https://")):
                downloaded = await _download_http_file(raw_url, dest_base)
                return downloaded, await _resolve_media_type(downloaded, ffmpeg_engine)
            else:
                candidate = Path(raw_url)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate, await _resolve_media_type(candidate, ffmpeg_engine)

                signed = await storage_provider.get_signed_url(raw_url)
                if signed.startswith("file://"):
                    local_src = _file_uri_to_path(signed)
                    if local_src.is_file() and local_src.stat().st_size > 0:
                        return local_src, await _resolve_media_type(local_src, ffmpeg_engine)
                elif signed.startswith("/local-media/"):
                    local_src = resolve_local_media_url(
                        signed,
                        settings,
                        base_dir=getattr(storage_provider, "base_dir", None),
                    )
                    if local_src is not None and local_src.is_file() and local_src.stat().st_size > 0:
                        return local_src, await _resolve_media_type(local_src, ffmpeg_engine)
                elif signed.startswith(("http://", "https://")):
                    downloaded = await _download_http_file(signed, dest_base)
                    return downloaded, await _resolve_media_type(downloaded, ffmpeg_engine)
        except Exception as exc:
            logger.warning(
                "Could not resolve visual_url '%s' for scene_index=%d (%s); re-fetching visual",
                raw_url,
                scene.scene_index,
                exc,
            )

    query = (
        scene.search_keywords
        or scene.narration_text
        or scene.edited_text
        or "abstract cinematic background"
    ).strip()
    asset = await visual_provider.fetch_visual(
        query=query,
        prompt=scene.image_prompt,
        aspect_ratio=project.aspect_ratio,
        media_type="image" if visual_provider.provider_name in ("flux", "stable_diffusion") else "video",
        output_dir=scenes_dir,
    )
    if asset.local_path is not None and Path(asset.local_path).is_file():
        return Path(asset.local_path), asset.media_type
    if asset.url.startswith(("http://", "https://")):
        downloaded = await _download_http_file(asset.url, dest_base)
        return downloaded, asset.media_type
    raise NotFoundError(
        f"Unable to locate or fetch visual asset for scene {scene.scene_index}",
        details={"project_id": str(project.id), "scene_index": scene.scene_index},
    )


async def _resolve_optional_bgm(
    project: ProjectResponse,
    work_dir: Path,
    storage_provider: BaseStorageProvider,
    cfg: PipelineConfig | None = None,
    settings: Settings | None = None,
) -> Path | None:
    effective_pipeline_cfg = {
        **(project.pipeline_config or {}),
        **(cfg.model_dump(exclude_none=True) if cfg is not None else {}),
    }
    try:
        bgm_service = BGMLibraryService(settings=settings)
        return await bgm_service.resolve_bgm_path(
            pipeline_config=effective_pipeline_cfg,
            source_inputs=project.source_inputs or {},
            work_dir=work_dir,
            storage=storage_provider,
        )
    except Exception as exc:
        logger.warning(
            "Failed to stage optional BGM for project=%s (%s); continuing without BGM",
            project.id,
            exc,
        )
        return None


async def run_stage4_assemble(
    project: ProjectResponse,
    stage3_result: Stage3CaptionsTTSResult,
    cfg: PipelineConfig,
    settings: Settings,
    subscription_tier: SubscriptionTier | str,
    work_dir: Path,
    ffmpeg_engine: FFmpegEngine,
    visual_provider: BaseVisualProvider,
    storage_provider: BaseStorageProvider,
    repo: SupabaseRepository,
    *,
    emit_progress: ProgressEmitter | None = None,
) -> str:
    """Prepare clips, concat, duck/mix audio, burn captions/watermark, upload, and complete."""
    notify = emit_progress or _noop_progress
    ordered_scenes = sorted(stage3_result.scenes, key=lambda s: s.scene_index)
    if not ordered_scenes:
        raise ValidationError("No scenes available for Stage 4 assembly")

    scenes_dir = work_dir / "scenes"
    clips_dir = work_dir / "clips"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(scenes_dir / "manifest.json")

    spoken_durations_sec: list[float] = []
    for scene in ordered_scenes:
        dur_ms = stage3_result.scene_durations_ms.get(
            scene.scene_index,
            max(500, scene.end_ms - scene.start_ms),
        )
        spoken_durations_sec.append(max(0.5, dur_ms / 1000.0))

    # Pad all clips except the last by the exact xfade overlap so video duration == voiceover duration.
    use_xfade = len(ordered_scenes) > 1
    transition_sec = min(0.35, min(spoken_durations_sec) * 0.35) if use_xfade else 0.0
    if transition_sec < 0.05:
        use_xfade = False
        transition_sec = 0.0

    await notify(
        "assemble_clips",
        82,
        f"Rendering {len(ordered_scenes)} scene clips ({project.aspect_ratio.value})...",
    )

    clip_paths: list[Path] = []
    total_scenes = len(ordered_scenes)
    for pos, scene in enumerate(ordered_scenes):
        local_asset, media_type = await _ensure_scene_local_asset(
            scene=scene,
            project=project,
            scenes_dir=scenes_dir,
            manifest=manifest,
            visual_provider=visual_provider,
            storage_provider=storage_provider,
            ffmpeg_engine=ffmpeg_engine,
            settings=settings,
        )

        clip_duration = spoken_durations_sec[pos] + (
            transition_sec if (use_xfade and pos < total_scenes - 1) else 0.0
        )
        clip_out = clips_dir / f"clip_{scene.scene_index:04d}.mp4"
        prepared = await ffmpeg_engine.prepare_scene_clip(
            asset_path=local_asset,
            media_type=media_type,
            duration_seconds=clip_duration,
            output_path=clip_out,
            aspect_ratio=project.aspect_ratio,
            ken_burns=cfg.ken_burns_enabled,
            scene_index=scene.scene_index,
        )
        clip_paths.append(prepared)

        pct = 82 + int(round(((pos + 1) / max(1, total_scenes)) * 8))
        await notify(
            "assemble_clips",
            min(90, pct),
            f"Prepared scene clip {pos + 1}/{total_scenes}",
            extra_data={"completed_items": pos + 1, "total_items": total_scenes},
        )

    concat_video = work_dir / "concat.mp4"
    await ffmpeg_engine.concat_video_clips(
        clip_paths,
        concat_video,
        transition_duration=transition_sec,
        use_xfade=use_xfade,
    )

    await notify("mixing_audio", 92, "Mixing voiceover and background music...")
    bgm_path = await _resolve_optional_bgm(
        project,
        work_dir,
        storage_provider,
        cfg=cfg,
        settings=settings,
    )
    mixed_audio = work_dir / "mixed_audio.wav"
    await ffmpeg_engine.mix_voice_and_bgm(
        stage3_result.voiceover_path,
        bgm_path,
        mixed_audio,
        bgm_volume=cfg.bgm_volume,
        auto_ducking=cfg.auto_ducking,
        is_fast_mode=False,
    )

    await notify("mastering", 94, "Burning karaoke subtitles, watermark, and mastering final MP4...")
    watermark_opts = resolve_watermark_options(subscription_tier, settings)
    final_mp4 = work_dir / "final.mp4"
    await ffmpeg_engine.master_final_video(
        concat_video,
        mixed_audio,
        final_mp4,
        aspect_ratio=project.aspect_ratio,
        ass_subtitle_path=stage3_result.ass_path,
        watermark=watermark_opts,
    )

    await notify("uploading", 97, "Uploading final video to storage...")
    remote_key = f"{project.user_id}/{project.id}/final.mp4"
    stored = await storage_provider.upload_file(
        final_mp4,
        remote_key,
        content_type="video/mp4",
    )
    output_video_url = stored.signed_url
    if not output_video_url:
        output_video_url = await storage_provider.get_signed_url(
            stored.remote_key,
            expires_in=settings.SUPABASE_SIGNED_URL_TTL_SECONDS,
        )
    if not output_video_url:
        output_video_url = stored.public_url or stored.remote_key

    total_duration_s = round(stage3_result.total_duration_ms / 1000.0, 2)
    await repo.update_project_status(
        project.id,
        ProjectStatus.COMPLETED,
        current_stage="completed",
        progress_pct=100,
        output_video_url=output_video_url,
        duration_seconds=total_duration_s,
        enforce_fsm=True,
    )

    await notify(
        "completed",
        100,
        "Faceless video completed!",
        event_type="completed",
        extra_data={"output_video_url": output_video_url},
    )
    logger.info(
        "Stage 4 complete for project=%s: output_video_url=%s duration=%.2fs",
        project.id,
        output_video_url,
        total_duration_s,
    )
    return output_video_url
