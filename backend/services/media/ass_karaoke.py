"""ASS (.ass) dynamic word-level Karaoke subtitle generator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio
from backend.models.project import SubtitleStyleConfig
from backend.models.provider import WordTimestamp
from backend.models.scene import SceneResponse

logger = get_logger(__name__)

RESOLUTION_MAP: dict[AspectRatio, tuple[int, int]] = {
    AspectRatio.PORTRAIT_9_16: (1080, 1920),
    AspectRatio.LANDSCAPE_16_9: (1920, 1080),
    AspectRatio.SQUARE_1_1: (1080, 1080),
}

_ALIGNMENT_MAP: dict[str, int] = {
    "bottom": 2,
    "middle": 5,
    "top": 8,
}

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB`` (or ``#RGB``) and 0-255 alpha into ASS ``&HAABBGGRR``."""
    if not isinstance(hex_color, str):
        raise ValidationError(f"Color must be a hex string, got {type(hex_color).__name__}")

    match = _HEX_COLOR_RE.fullmatch(hex_color.strip())
    if not match:
        raise ValidationError(
            f"Invalid hex color '{hex_color}'. Expected format '#RRGGBB'.",
            details={"hex_color": hex_color},
        )

    raw = match.group(1)
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)

    rr = int(raw[0:2], 16)
    gg = int(raw[2:4], 16)
    bb = int(raw[4:6], 16)
    aa = max(0, min(255, int(alpha)))
    return f"&H{aa:02X}{bb:02X}{gg:02X}{rr:02X}"


def _format_ass_timestamp(ms: int) -> str:
    """Format non-negative milliseconds as ``H:MM:SS.cc`` ASS timestamp."""
    clamped = max(0, int(ms))
    total_cs = round(clamped / 10)
    cs = total_cs % 100
    total_seconds = total_cs // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Escape ASS special braces and newlines safely for Dialogue text."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    # Preserve explicit \N line breaks while converting raw newlines into \N
    normalized = normalized.replace("\n", r"\N")
    return normalized


def _resolve_safe_margins(
    aspect_ratio: AspectRatio,
    width: int,
    height: int,
    position: str,
) -> tuple[int, int, int]:
    """Return ``(margin_l, margin_r, margin_v)`` tailored to aspect ratio safe zones."""
    margin_lr = max(40, int(round(width * 0.06)))
    if position == "middle":
        return margin_lr, margin_lr, 0

    if aspect_ratio == AspectRatio.PORTRAIT_9_16:
        # Leave generous room for TikTok/Reels/Shorts top header & bottom UI overlays
        margin_v = int(round(height * (0.16 if position == "bottom" else 0.13)))
    elif aspect_ratio == AspectRatio.SQUARE_1_1:
        margin_v = int(round(height * 0.10))
    else:
        margin_v = int(round(height * 0.08))

    return margin_lr, margin_lr, margin_v


class KaraokeSegmentInput(BaseModel):
    """Normalized segment input accepted by ``AssKaraokeGenerator``."""

    model_config = ConfigDict(extra="ignore")

    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    text: str
    words: list[WordTimestamp] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_range(self) -> KaraokeSegmentInput:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class AssKaraokeGenerator:
    """Builds stylized ``.ass`` files with word-level ``\\kf`` karaoke highlight tags."""

    def _normalize_segment(
        self,
        segment: KaraokeSegmentInput | SceneResponse,
    ) -> KaraokeSegmentInput:
        if isinstance(segment, KaraokeSegmentInput):
            return segment
        if isinstance(segment, SceneResponse):
            text = (
                (segment.edited_text or "").strip()
                or (segment.narration_text or "").strip()
                or (segment.original_text or "").strip()
            )
            return KaraokeSegmentInput(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=text,
                words=segment.word_timestamps,
            )
        raise ValidationError(f"Unsupported karaoke segment type: {type(segment).__name__}")

    def _build_karaoke_dialogue_text(self, seg: KaraokeSegmentInput) -> str:
        """Build the ``{\\kf...}word`` sequence strictly clamped to ``[start_ms, end_ms]``."""
        seg_start = seg.start_ms
        seg_end = seg.end_ms
        total_ms = max(0, seg_end - seg_start)
        total_cs = max(1, round(total_ms / 10))

        valid_words = [w for w in seg.words if w.word and w.word.strip()]
        if valid_words:
            parts: list[str] = []
            cursor_cs = 0
            count = len(valid_words)

            for idx, word_ts in enumerate(valid_words):
                clamped_start = max(seg_start, min(seg_end, word_ts.start_ms))
                clamped_end = max(clamped_start, min(seg_end, word_ts.end_ms))

                rel_start_cs = max(cursor_cs, min(total_cs, round((clamped_start - seg_start) / 10)))
                rel_end_cs = max(rel_start_cs, min(total_cs, round((clamped_end - seg_start) / 10)))

                if rel_start_cs > cursor_cs:
                    gap_cs = rel_start_cs - cursor_cs
                    parts.append(f"{{\\k{gap_cs}}}")
                    cursor_cs = rel_start_cs

                if idx == count - 1:
                    dur_cs = max(0, total_cs - cursor_cs)
                else:
                    dur_cs = max(1 if cursor_cs < total_cs else 0, rel_end_cs - cursor_cs)
                    dur_cs = min(dur_cs, total_cs - cursor_cs)

                cursor_cs += dur_cs
                token = _escape_ass_text(word_ts.word.strip())
                suffix = " " if idx < count - 1 else ""
                parts.append(f"{{\\kf{dur_cs}}}{token}{suffix}")

            return "".join(parts)

        # Fallback: proportional split across whitespace-separated words
        tokens = seg.text.strip().split()
        if not tokens:
            return ""

        weights = [max(1, len(tok)) for tok in tokens]
        total_weight = sum(weights)
        parts = []
        allocated_cs = 0

        for idx, (tok, weight) in enumerate(zip(tokens, weights)):
            if idx == len(tokens) - 1:
                dur_cs = max(0, total_cs - allocated_cs)
            else:
                remaining_tokens = len(tokens) - idx - 1
                raw_cs = round(total_cs * (weight / total_weight))
                max_allowed = max(0, total_cs - allocated_cs - remaining_tokens)
                dur_cs = max(1 if total_cs - allocated_cs > remaining_tokens else 0, min(raw_cs, max_allowed))
            allocated_cs += dur_cs
            escaped = _escape_ass_text(tok)
            suffix = " " if idx < len(tokens) - 1 else ""
            parts.append(f"{{\\kf{dur_cs}}}{escaped}{suffix}")

        return "".join(parts)

    def generate(
        self,
        segments: Sequence[KaraokeSegmentInput | SceneResponse],
        output_path: Path,
        aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16,
        style: SubtitleStyleConfig | None = None,
    ) -> Path:
        """Write a complete ``.ass`` karaoke subtitle file and return ``output_path``."""
        cfg = style or SubtitleStyleConfig()
        width, height = RESOLUTION_MAP[aspect_ratio]
        alignment = _ALIGNMENT_MAP.get(cfg.position, 2)
        margin_l, margin_r, margin_v = _resolve_safe_margins(
            aspect_ratio=aspect_ratio,
            width=width,
            height=height,
            position=cfg.position,
        )

        # In ASS karaoke, SecondaryColour renders pre-highlight text and PrimaryColour fills on \kf
        highlight_ass = hex_to_ass_color(cfg.highlight_color, alpha=0)
        base_ass = hex_to_ass_color(cfg.primary_color, alpha=0)
        outline_ass = hex_to_ass_color(cfg.outline_color, alpha=0)
        shadow_ass = hex_to_ass_color("#000000", alpha=128)
        outline_width = max(0, int(cfg.outline_width))

        lines: list[str] = [
            "[Script Info]",
            "Title: AI Video Studio Karaoke Subtitles",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding"
            ),
            (
                f"Style: Default,{cfg.font_name},{cfg.font_size},"
                f"{highlight_ass},{base_ass},{outline_ass},{shadow_ass},"
                f"-1,0,0,0,100,100,0,0,1,{outline_width},2,"
                f"{alignment},{margin_l},{margin_r},{margin_v},1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        emitted = 0
        for raw_seg in segments:
            seg = self._normalize_segment(raw_seg)
            if seg.end_ms <= seg.start_ms:
                continue
            if not seg.text.strip() and not seg.words:
                continue

            karaoke_text = self._build_karaoke_dialogue_text(seg)
            if not karaoke_text:
                continue

            start_ts = _format_ass_timestamp(seg.start_ms)
            end_ts = _format_ass_timestamp(seg.end_ms)
            lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{karaoke_text}")
            emitted += 1

        dest = Path(output_path)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        except OSError as exc:
            logger.exception("Failed to write .ass subtitle file at %s", dest)
            raise ValidationError(
                f"Unable to write ASS subtitle file to '{dest}': {exc}",
                details={"output_path": str(dest)},
            ) from exc

        logger.info(
            "Generated .ass karaoke subtitles (%d events, %dx%d, pos=%s) -> %s",
            emitted,
            width,
            height,
            cfg.position,
            dest,
        )
        return dest
