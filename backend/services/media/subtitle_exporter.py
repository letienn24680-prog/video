"""Subtitle (.SRT and .ASS) exporter for AI Video Studio scenes."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from backend.core.exceptions import ValidationError
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio
from backend.models.project import SubtitleStyleConfig
from backend.models.scene import SceneResponse
from backend.services.media.ass_karaoke import AssKaraokeGenerator

logger = get_logger(__name__)


def _format_srt_timestamp(ms: int) -> str:
    """Format non-negative milliseconds as ``HH:MM:SS,mmm`` SubRip timestamp."""
    clamped = max(0, int(ms))
    milliseconds = clamped % 1000
    total_seconds = clamped // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _extract_scene_text(scene: SceneResponse) -> str:
    """Resolve display subtitle text preferring ``edited_text``, then ``narration_text``, then ``original_text``."""
    raw = (
        (scene.edited_text or "").strip()
        or (scene.narration_text or "").strip()
        or (scene.original_text or "").strip()
    )
    return raw.replace("\r\n", "\n").replace("\r", "\n")


class SubtitleExporter:
    """Exports project scenes to SubRip ``.srt`` or stylized Karaoke ``.ass`` formats."""

    def __init__(self, ass_generator: AssKaraokeGenerator | None = None) -> None:
        self._ass_generator = ass_generator or AssKaraokeGenerator()

    def export_srt(
        self,
        scenes: Sequence[SceneResponse],
        output_path: Path | None = None,
    ) -> str:
        """Format scenes as valid SubRip ``.srt`` text and optionally write to ``output_path``."""
        ordered = sorted(scenes, key=lambda s: (s.scene_index, s.start_ms))
        blocks: list[str] = []
        cue_index = 1

        for scene in ordered:
            text = _extract_scene_text(scene)
            if not text or scene.end_ms <= scene.start_ms:
                continue
            start_ts = _format_srt_timestamp(scene.start_ms)
            end_ts = _format_srt_timestamp(scene.end_ms)
            blocks.append(f"{cue_index}\n{start_ts} --> {end_ts}\n{text}\n")
            cue_index += 1

        srt_content = "\n".join(blocks) + ("\n" if blocks else "")

        if output_path is not None:
            dest = Path(output_path)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(srt_content, encoding="utf-8")
            except OSError as exc:
                logger.exception("Failed to write .srt file at %s", dest)
                raise ValidationError(
                    f"Unable to write SRT subtitle file to '{dest}': {exc}",
                    details={"output_path": str(dest)},
                ) from exc
            logger.info("Exported .srt subtitles (%d cues) -> %s", cue_index - 1, dest)

        return srt_content

    def export_ass(
        self,
        scenes: Sequence[SceneResponse],
        aspect_ratio: AspectRatio,
        style: SubtitleStyleConfig | None,
        output_path: Path,
    ) -> Path:
        """Delegate stylized karaoke ``.ass`` subtitle export to ``AssKaraokeGenerator``."""
        ordered = sorted(scenes, key=lambda s: (s.scene_index, s.start_ms))
        return self._ass_generator.generate(
            segments=ordered,
            output_path=Path(output_path),
            aspect_ratio=aspect_ratio,
            style=style,
        )
