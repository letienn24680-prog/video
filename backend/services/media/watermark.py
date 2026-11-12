"""Free-tier watermark policy resolution and FFmpeg filter builder."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.models.enums import SubscriptionTier

logger = get_logger(__name__)

WatermarkPosition = Literal["top_left", "top_right", "bottom_left", "bottom_right"]


class WatermarkOptions(BaseModel):
    """Resolved watermark configuration passed into FFmpeg mastering."""

    enabled: bool
    text: str
    image_path: Path | None = None
    position: WatermarkPosition = "bottom_right"
    opacity: float = Field(default=0.65, ge=0.0, le=1.0)


def resolve_watermark_options(
    subscription_tier: SubscriptionTier | str,
    settings: Settings | None = None,
) -> WatermarkOptions:
    """Resolve whether a watermark must be burned based on tier and settings."""
    cfg = settings or get_settings()

    if isinstance(subscription_tier, SubscriptionTier):
        tier_value = subscription_tier.value
    else:
        tier_value = str(subscription_tier).strip().lower()

    is_free_tier = tier_value == SubscriptionTier.FREE.value
    enabled = bool(cfg.FREE_TIER_WATERMARK_ENABLED and is_free_tier)

    raw_img = (cfg.WATERMARK_IMAGE_PATH or "").strip()
    image_path = Path(raw_img) if raw_img else None

    return WatermarkOptions(
        enabled=enabled,
        text=cfg.WATERMARK_TEXT or "AI Video Studio",
        image_path=image_path,
        position=cfg.WATERMARK_POSITION,
        opacity=cfg.WATERMARK_OPACITY,
    )


def _escape_drawtext(text: str) -> str:
    """Escape characters with special meaning inside FFmpeg drawtext filter args."""
    escaped = text.replace("\\", "\\\\\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\\\'")
    escaped = escaped.replace("%", "\\%")
    return escaped


def build_watermark_filter(
    options: WatermarkOptions,
    video_width: int,
    video_height: int,
) -> tuple[str | None, list[str]]:
    """Build an FFmpeg ``drawtext`` or PNG ``overlay`` filter with extra input args."""
    if not options.enabled:
        return None, []

    opacity = max(0.0, min(1.0, float(options.opacity)))
    if opacity <= 0.0:
        return None, []

    margin_x = max(16, int(round(video_width * 0.03)))
    margin_y = max(16, int(round(video_height * 0.03)))

    if options.image_path is not None and options.image_path.is_file():
        wm_width = max(96, int(round(video_width * 0.18)))
        pos_map = {
            "top_left": (f"{margin_x}", f"{margin_y}"),
            "top_right": (f"W-w-{margin_x}", f"{margin_y}"),
            "bottom_left": (f"{margin_x}", f"H-h-{margin_y}"),
            "bottom_right": (f"W-w-{margin_x}", f"H-h-{margin_y}"),
        }
        x_expr, y_expr = pos_map[options.position]
        filter_expr = (
            f"[1:v]scale={wm_width}:-1,format=rgba,"
            f"colorchannelmixer=aa={opacity:.2f}[wm];"
            f"[0:v][wm]overlay={x_expr}:{y_expr}"
        )
        return filter_expr, ["-i", str(options.image_path)]

    if options.image_path is not None and not options.image_path.is_file():
        logger.warning(
            "Watermark image '%s' not found; falling back to text watermark",
            options.image_path,
        )

    clean_text = (options.text or "").strip()
    if not clean_text:
        return None, []

    font_size = max(18, int(round(min(video_width, video_height) * 0.028)))
    pos_text_map = {
        "top_left": (f"{margin_x}", f"{margin_y}"),
        "top_right": (f"w-tw-{margin_x}", f"{margin_y}"),
        "bottom_left": (f"{margin_x}", f"h-th-{margin_y}"),
        "bottom_right": (f"w-tw-{margin_x}", f"h-th-{margin_y}"),
    }
    x_expr, y_expr = pos_text_map[options.position]
    border_opacity = min(1.0, opacity)
    escaped_text = _escape_drawtext(clean_text)

    filter_expr = (
        f"drawtext=text='{escaped_text}':"
        f"fontsize={font_size}:"
        f"fontcolor=white@{opacity:.2f}:"
        f"borderw=2:bordercolor=black@{border_opacity:.2f}:"
        f"shadowx=1:shadowy=1:shadowcolor=black@{border_opacity:.2f}:"
        f"x={x_expr}:y={y_expr}"
    )
    return filter_expr, []
