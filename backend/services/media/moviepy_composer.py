"""MoviePy-based clip composer with crossfade transitions and lazy imports."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

from backend.core.exceptions import ConfigurationError, NotFoundError, ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio
from backend.services.media.ass_karaoke import RESOLUTION_MAP

logger = get_logger(__name__)


class MoviePyClipSpec(BaseModel):
    """Normalized clip specification accepted by ``MoviePyComposer``."""

    asset_path: Path
    media_type: Literal["video", "image"] = "video"
    duration_seconds: float = Field(..., gt=0)


def _normalize_clip_spec(raw: Any) -> MoviePyClipSpec:
    if isinstance(raw, MoviePyClipSpec):
        return raw
    if isinstance(raw, tuple) and len(raw) >= 2:
        path_val, dur_val = raw[0], raw[1]
        media_type = raw[2] if len(raw) > 2 else "video"
        return MoviePyClipSpec(
            asset_path=Path(path_val),
            media_type=media_type,
            duration_seconds=float(dur_val),
        )
    if isinstance(raw, Mapping):
        path_val = raw.get("asset_path") or raw.get("path") or raw.get("clip_path")
        if not path_val:
            raise ValidationError("Clip spec mapping must include 'asset_path' or 'path'")
        return MoviePyClipSpec(
            asset_path=Path(path_val),
            media_type=raw.get("media_type", "video"),
            duration_seconds=float(raw.get("duration_seconds") or raw.get("duration") or 3.0),
        )
    if hasattr(raw, "asset_path") and hasattr(raw, "duration_seconds"):
        return MoviePyClipSpec(
            asset_path=Path(getattr(raw, "asset_path")),
            media_type=getattr(raw, "media_type", "video"),
            duration_seconds=float(getattr(raw, "duration_seconds")),
        )
    raise ValidationError(f"Unsupported clip spec type: {type(raw).__name__}")


class MoviePyComposer:
    """Composes scene clips with smooth crossfade transitions using MoviePy (lazy-imported)."""

    @staticmethod
    def _import_moviepy() -> dict[str, Any]:
        try:
            import moviepy as mp  # noqa: PLC0415

            if hasattr(mp, "VideoFileClip") and hasattr(mp, "concatenate_videoclips"):
                return {
                    "version": 2,
                    "VideoFileClip": mp.VideoFileClip,
                    "ImageClip": mp.ImageClip,
                    "concatenate_videoclips": mp.concatenate_videoclips,
                    "vfx": getattr(mp, "vfx", None),
                }
        except ImportError:
            pass

        try:
            from moviepy import editor as mp_editor  # type: ignore[attr-defined]  # noqa: PLC0415

            return {
                "version": 1,
                "VideoFileClip": mp_editor.VideoFileClip,
                "ImageClip": mp_editor.ImageClip,
                "concatenate_videoclips": mp_editor.concatenate_videoclips,
                "vfx": getattr(mp_editor, "vfx", None),
            }
        except ImportError as exc:
            raise ConfigurationError(
                "MoviePy is not installed or failed to import. Install 'moviepy>=2.0.0'."
            ) from exc

    @staticmethod
    def _fit_clip_to_canvas(clip: Any, target_w: int, target_h: int) -> Any:
        cw, ch = clip.size
        if cw <= 0 or ch <= 0:
            return clip

        scale = max(target_w / cw, target_h / ch)
        new_w = max(target_w, int(round(cw * scale)))
        new_h = max(target_h, int(round(ch * scale)))

        if hasattr(clip, "resized"):
            resized = clip.resized((new_w, new_h))
        else:
            resized = clip.resize((new_w, new_h))

        x1 = max(0, (new_w - target_w) // 2)
        y1 = max(0, (new_h - target_h) // 2)
        if hasattr(resized, "cropped"):
            return resized.cropped(x1=x1, y1=y1, width=target_w, height=target_h)
        return resized.crop(x1=x1, y1=y1, width=target_w, height=target_h)

    def _compose_sync(
        self,
        specs: list[MoviePyClipSpec],
        output_path: Path,
        aspect_ratio: AspectRatio,
        crossfade_sec: float,
    ) -> Path:
        mp = self._import_moviepy()
        VideoFileClip = mp["VideoFileClip"]
        ImageClip = mp["ImageClip"]
        concatenate_videoclips = mp["concatenate_videoclips"]
        vfx = mp["vfx"]
        is_v2 = mp["version"] == 2

        target_w, target_h = RESOLUTION_MAP[aspect_ratio]
        opened_clips: list[Any] = []
        final_clip: Any = None

        try:
            for idx, spec in enumerate(specs):
                if not spec.asset_path.is_file():
                    raise NotFoundError(f"Clip asset not found: {spec.asset_path}")

                if spec.media_type == "image":
                    raw_clip = ImageClip(str(spec.asset_path))
                    raw_clip = (
                        raw_clip.with_duration(spec.duration_seconds)
                        if hasattr(raw_clip, "with_duration")
                        else raw_clip.set_duration(spec.duration_seconds)
                    )
                else:
                    raw_clip = VideoFileClip(str(spec.asset_path), audio=False)
                    if raw_clip.duration and raw_clip.duration > spec.duration_seconds:
                        raw_clip = (
                            raw_clip.subclipped(0, spec.duration_seconds)
                            if hasattr(raw_clip, "subclipped")
                            else raw_clip.subclip(0, spec.duration_seconds)
                        )
                    elif raw_clip.duration and raw_clip.duration < spec.duration_seconds and vfx is not None:
                        loop_fx = getattr(vfx, "Loop", None)
                        if loop_fx is not None and is_v2:
                            raw_clip = raw_clip.with_effects([loop_fx(duration=spec.duration_seconds)])

                fitted = self._fit_clip_to_canvas(raw_clip, target_w, target_h)
                if idx > 0 and crossfade_sec > 0 and vfx is not None:
                    fade_dur = min(crossfade_sec, max(0.05, spec.duration_seconds * 0.35))
                    crossfade_cls = getattr(vfx, "CrossFadeIn", None)
                    if is_v2 and crossfade_cls is not None and hasattr(fitted, "with_effects"):
                        fitted = fitted.with_effects([crossfade_cls(fade_dur)])
                    elif hasattr(fitted, "crossfadein"):
                        fitted = fitted.crossfadein(fade_dur)

                opened_clips.append(fitted)

            padding = -crossfade_sec if (crossfade_sec > 0 and len(opened_clips) > 1) else 0
            final_clip = concatenate_videoclips(opened_clips, method="compose", padding=padding)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            final_clip.write_videofile(
                str(output_path),
                fps=30,
                codec="libx264",
                audio=False,
                preset="fast",
                logger=None,
            )
            return output_path
        except (ConfigurationError, NotFoundError, ValidationError):
            raise
        except Exception as exc:
            logger.exception("MoviePy composition failed")
            raise ProviderError("moviepy", f"MoviePy composition failed: {exc}") from exc
        finally:
            for clip in opened_clips:
                try:
                    clip.close()
                except Exception:
                    pass
            if final_clip is not None:
                try:
                    final_clip.close()
                except Exception:
                    pass

    async def compose_clips_with_transitions(
        self,
        clip_specs: Sequence[MoviePyClipSpec | Mapping[str, Any] | tuple[Any, ...]],
        output_path: Path,
        aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16,
        crossfade_sec: float = 0.35,
    ) -> Path:
        """Compose a sequence of clips with crossfade transitions in a worker thread."""
        if not clip_specs:
            raise ValidationError("clip_specs cannot be empty")

        normalized = [_normalize_clip_spec(item) for item in clip_specs]
        dest = Path(output_path)

        logger.info(
            "Composing %d clips with MoviePy (aspect=%s, crossfade=%.2fs) -> %s",
            len(normalized),
            aspect_ratio.value,
            crossfade_sec,
            dest,
        )
        return await asyncio.to_thread(
            self._compose_sync,
            normalized,
            dest,
            aspect_ratio,
            max(0.0, float(crossfade_sec)),
        )
