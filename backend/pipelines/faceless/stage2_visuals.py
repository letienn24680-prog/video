"""Stage 2 of the AI Faceless Video pipeline: multi-source visual fetching & HITL pause."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from backend.core.exceptions import ConfigurationError, ProviderAuthError, ProviderError
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio, ProjectStatus
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.provider import StoryboardResult, VisualAsset
from backend.models.scene import SceneCreate, SceneResponse
from backend.providers.base.visual import BaseVisualProvider
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]

_AI_IMAGE_PROVIDERS = frozenset({"flux", "stable_diffusion"})


async def _noop_progress(*_args: Any, **_kwargs: Any) -> None:
    return None


def _load_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Ignoring corrupted scenes manifest at %s: %s", manifest_path, exc)
        return {}


def _save_manifest(manifest_path: Path, manifest: dict[str, dict[str, Any]]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


async def _fetch_scene_visual_with_fallback(
    visual_provider: BaseVisualProvider,
    *,
    query: str,
    prompt: str | None,
    aspect_ratio: AspectRatio,
    output_dir: Path,
) -> VisualAsset:
    """Fetch scene visual, falling back from video to image or simplified keywords on empty search."""
    preferred_type: Literal["video", "image"] = (
        "image" if visual_provider.provider_name in _AI_IMAGE_PROVIDERS else "video"
    )
    clean_query = (query or prompt or "abstract background").strip()

    try:
        return await visual_provider.fetch_visual(
            query=clean_query,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            media_type=preferred_type,
            output_dir=output_dir,
        )
    except (ConfigurationError, ProviderAuthError):
        raise
    except ProviderError as exc:
        if preferred_type == "video":
            logger.warning(
                "Video visual lookup failed for query=%r (%s); retrying with image lookup",
                clean_query,
                exc.message,
            )
            try:
                return await visual_provider.fetch_visual(
                    query=clean_query,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    media_type="image",
                    output_dir=output_dir,
                )
            except (ConfigurationError, ProviderAuthError):
                raise
            except ProviderError:
                pass

        words = [w for w in clean_query.split() if w.strip()]
        if len(words) > 2:
            short_query = " ".join(words[:2])
            logger.warning(
                "Retrying visual lookup with simplified query=%r (media_type=image)",
                short_query,
            )
            return await visual_provider.fetch_visual(
                query=short_query,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                media_type="image",
                output_dir=output_dir,
            )
        raise


async def run_stage2_visuals(
    project: ProjectResponse,
    storyboard: StoryboardResult,
    cfg: PipelineConfig,
    work_dir: Path,
    visual_provider: BaseVisualProvider,
    repo: SupabaseRepository,
    *,
    emit_progress: ProgressEmitter | None = None,
) -> list[SceneResponse]:
    """Fetch visuals per scene, persist project_scenes, and pause in AWAITING_REVIEW."""
    notify = emit_progress or _noop_progress
    scenes_dir = work_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = scenes_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)

    existing_scenes = await repo.list_scenes(project.id)
    existing_by_index = {scene.scene_index: scene for scene in existing_scenes}

    ordered_storyboard = sorted(storyboard.scenes, key=lambda s: s.scene_index)
    total_scenes = len(ordered_storyboard)
    scene_creates: list[SceneCreate] = []
    cursor_ms = 0

    await notify(
        "visuals",
        35,
        f"Fetching visual assets for {total_scenes} scenes via {visual_provider.provider_name}...",
    )

    for pos, sb_scene in enumerate(ordered_storyboard):
        existing = existing_by_index.get(sb_scene.scene_index)
        if existing is not None and existing.is_locked:
            existing_dur = existing.end_ms - existing.start_ms
            dur_ms = (
                existing_dur
                if existing_dur > 0
                else max(500, int(round(float(sb_scene.duration_seconds) * 1000)))
            )
            start_ms = cursor_ms
            end_ms = start_ms + dur_ms
            cursor_ms = end_ms

            scene_creates.append(
                SceneCreate(
                    project_id=project.id,
                    scene_index=sb_scene.scene_index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    original_text=existing.original_text or existing.narration_text,
                    edited_text=existing.edited_text,
                    narration_text=existing.narration_text,
                    word_timestamps=existing.word_timestamps,
                    search_keywords=existing.search_keywords,
                    image_prompt=existing.image_prompt,
                    emotion_tone=existing.emotion_tone,
                    speaker_label=existing.speaker_label,
                    voice_id=existing.voice_id,
                    tts_rate_pct=existing.tts_rate_pct,
                    audio_url=existing.audio_url,
                    visual_url=existing.visual_url,
                    visual_source=existing.visual_source,
                    subtitle_style=existing.subtitle_style,
                    is_locked=True,
                )
            )
            logger.info(
                "Preserved locked scene_index=%d for project=%s",
                sb_scene.scene_index,
                project.id,
            )
        else:
            dur_ms = max(500, int(round(float(sb_scene.duration_seconds) * 1000)))
            start_ms = cursor_ms
            end_ms = start_ms + dur_ms
            cursor_ms = end_ms

            asset = await _fetch_scene_visual_with_fallback(
                visual_provider,
                query=sb_scene.search_keywords,
                prompt=sb_scene.image_prompt,
                aspect_ratio=project.aspect_ratio,
                output_dir=scenes_dir,
            )

            if asset.local_path is not None:
                manifest[str(sb_scene.scene_index)] = {
                    "visual_url": asset.url,
                    "local_path": str(asset.local_path),
                    "media_type": asset.media_type,
                }

            scene_creates.append(
                SceneCreate(
                    project_id=project.id,
                    scene_index=sb_scene.scene_index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    original_text=sb_scene.narration_text,
                    narration_text=sb_scene.narration_text,
                    search_keywords=sb_scene.search_keywords,
                    image_prompt=sb_scene.image_prompt,
                    emotion_tone=sb_scene.emotion_tone,
                    voice_id=cfg.voice_id,
                    tts_rate_pct=cfg.tts_rate_pct,
                    visual_url=asset.url,
                    visual_source=asset.source,
                    subtitle_style=cfg.subtitle_style.model_dump(mode="json"),
                    is_locked=False,
                )
            )

        pct = 35 + int(round(((pos + 1) / max(1, total_scenes)) * 17))
        await notify(
            "visuals",
            min(52, pct),
            f"Prepared visual {pos + 1}/{total_scenes}",
            extra_data={"completed_items": pos + 1, "total_items": total_scenes},
        )

    _save_manifest(manifest_path, manifest)
    persisted_scenes = await repo.upsert_scenes(project.id, scene_creates)

    total_duration_s = round(cursor_ms / 1000.0, 2)
    await repo.update_project_status(
        project.id,
        ProjectStatus.AWAITING_REVIEW,
        current_stage="awaiting_review",
        progress_pct=55,
        duration_seconds=total_duration_s,
        enforce_fsm=True,
    )

    await notify(
        "awaiting_review",
        55,
        f"Storyboard & visuals ready for review ({len(persisted_scenes)} scenes)",
        event_type="awaiting_review",
        extra_data={"scenes_count": len(persisted_scenes)},
    )
    logger.info(
        "Stage 2 complete for project=%s: persisted %d scenes, transitioned to AWAITING_REVIEW",
        project.id,
        len(persisted_scenes),
    )
    return persisted_scenes
