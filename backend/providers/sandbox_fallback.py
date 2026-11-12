"""Zero-key Local Sandbox fallback providers for LLM, Visual, and STT pipelines."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shutil
import struct
import wave
import zlib
from pathlib import Path
from typing import Literal
from uuid import uuid4

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.core.subprocess_utils import run_subprocess_exec
from backend.models.enums import AspectRatio, VisualSource
from backend.models.provider import (
    StoryboardResult,
    StoryboardScene,
    TranscriptResult,
    TranscriptSegment,
    TranslatedSegment,
    VisualAsset,
    WordTimestamp,
)
from backend.providers.base.llm import BaseLLMProvider
from backend.providers.base.stt import BaseSTTProvider
from backend.providers.base.visual import BaseVisualProvider

logger = get_logger(__name__)

_SANDBOX_PALETTES: tuple[tuple[int, int, int], ...] = (
    (20, 27, 45),   # Midnight slate
    (29, 23, 51),   # Deep violet
    (15, 41, 46),   # Ocean teal
    (42, 24, 41),   # Plum charcoal
    (24, 41, 30),   # Emerald night
    (39, 28, 25),   # Warm espresso
)

_FALLBACK_KEYWORDS: tuple[str, ...] = (
    "modern workspace laptop",
    "digital technology abstract",
    "creative studio lighting",
    "minimalist productivity desk",
    "futuristic data network",
    "cinematic city night",
)

_SANDBOX_VI_TRANSLATIONS: dict[str, str] = {
    "Welcome to AI Video Studio local sandbox mode.": (
        "Chào mừng bạn đến với chế độ Local Sandbox của AI Video Studio."
    ),
    "You can edit every subtitle line, voice, and visual clip here.": (
        "Bạn có thể chỉnh sửa từng câu thoại, giọng đọc và cảnh quay ngay tại đây."
    ),
    "Click Confirm and Render when you are ready to export your video.": (
        "Nhấn Xác nhận và Render khi bạn đã sẵn sàng xuất video hoàn chỉnh."
    ),
}

_RATIO_DIMENSIONS: dict[AspectRatio, tuple[int, int]] = {
    AspectRatio.PORTRAIT_9_16: (720, 1280),
    AspectRatio.LANDSCAPE_16_9: (1280, 720),
    AspectRatio.SQUARE_1_1: (720, 720),
}


def is_configured_key(value: str | None) -> bool:
    """Return True only when ``value`` is non-empty and not an .env.example placeholder."""
    cleaned = (value or "").strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if (
        "your-" in lowered
        or "your_" in lowered
        or "change-me" in lowered
        or lowered.endswith("_here")
        or cleaned == "0123456789"
    ):
        return False
    return True


def _write_gradient_png(
    dest_path: Path,
    width: int,
    height: int,
    base_rgb: tuple[int, int, int],
) -> None:
    """Write a valid RGB PNG gradient image using pure Python stdlib (no Pillow required)."""
    r0, g0, b0 = base_rgb
    r1 = min(255, r0 + 42)
    g1 = min(255, g0 + 54)
    b1 = min(255, b0 + 78)

    # Build raw scanlines with filter byte 0 per row
    rows = bytearray()
    denom_y = max(1, height - 1)
    for y in range(height):
        t = y / denom_y
        r = int(r0 + (r1 - r0) * t)
        g = int(g0 + (g1 - g0) * t)
        b = int(b0 + (b1 - b0) * t)
        rows.append(0)
        rows.extend(bytes((r, g, b)) * width)

    compressed = zlib.compress(bytes(rows), level=6)

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(png_bytes)


def _extract_ascii_keywords(text: str, fallback_idx: int) -> str:
    words = re.findall(r"[A-Za-z0-9]{3,}", text or "")
    stop = {"the", "and", "for", "with", "that", "this", "from", "your", "into", "about"}
    filtered = [w.lower() for w in words if w.lower() not in stop]
    if len(filtered) >= 2:
        return " ".join(filtered[:4])
    return _FALLBACK_KEYWORDS[fallback_idx % len(_FALLBACK_KEYWORDS)]


class SandboxLLMProvider(BaseLLMProvider):
    """Deterministic zero-key LLM provider for Local Sandbox storyboarding and translation."""

    provider_name = "sandbox_llm"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def generate_storyboard(
        self,
        source_text: str,
        aspect_ratio: AspectRatio,
        scene_duration_target: tuple[int, int] = (3, 5),
        tone: str = "viral",
    ) -> StoryboardResult:
        """Split or expand ``source_text`` into 4-6 structured storyboard scenes offline."""
        del aspect_ratio  # Storyboard scenes are aspect-agnostic; visual engine sizes clips.

        raw = (source_text or "").strip()
        cleaned = re.sub(
            r"\s*\[Pacing constraint:.*?\]\s*",
            " ",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()

        title = "Local Sandbox Video"
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if lines and lines[0].lower().startswith("title:"):
            candidate_title = lines[0].split(":", 1)[1].strip()
            if candidate_title:
                title = candidate_title[:80]
            cleaned = "\n".join(lines[1:]).strip() or candidate_title

        if title == "Local Sandbox Video" and cleaned:
            first_clause = re.split(r"[.!?\n]", cleaned, maxsplit=1)[0].strip()
            if first_clause:
                title = first_clause[:72]

        # Split into candidate sentences/clauses
        fragments = [
            frag.strip(" -\t")
            for frag in re.split(r"(?<=[.!?])\s+|\n+|;\s*", cleaned)
            if frag.strip(" -\t")
        ]

        if len(fragments) >= 4:
            if len(fragments) > 6:
                # Merge trailing fragments evenly into 6 scenes
                grouped: list[str] = []
                step = math.ceil(len(fragments) / 6)
                for idx in range(0, len(fragments), step):
                    grouped.append(" ".join(fragments[idx : idx + step]))
                narration_lines = grouped[:6]
            else:
                narration_lines = fragments[:6]
        else:
            topic = cleaned or title
            topic_short = topic[:90].rstrip(".!? ")
            narration_lines = [
                f"Bạn đã từng tự hỏi {topic_short} hoạt động hiệu quả nhất như thế nào chưa?",
                f"Hôm nay chúng ta sẽ cùng khám phá nhanh những điểm cốt lõi nhất về {topic_short}.",
                f"Bước đầu tiên khi tiếp cận {topic_short} là xác định rõ mục tiêu và cấu trúc nội dung.",
                f"Tiếp theo, hãy tinh chỉnh từng phân cảnh và giọng đọc để thông điệp về {topic_short} thật cuốn hút.",
                f"Áp dụng ngay quy trình này với {topic_short} và nhấn xuất bản video của bạn ngay hôm nay!",
            ]

        min_dur = max(1, int(scene_duration_target[0]))
        max_dur = max(min_dur, int(scene_duration_target[1]))
        tone_clean = (tone or "viral").strip().lower()
        tone_palette = [tone_clean, "curious", "focused", "confident", "inspiring", "energetic"]

        scenes: list[StoryboardScene] = []
        for idx, narration in enumerate(narration_lines):
            word_count = max(1, len(narration.split()))
            estimated = round(max(float(min_dur), min(float(max_dur), word_count / 3.2)), 1)
            keywords = _extract_ascii_keywords(narration, idx)
            scenes.append(
                StoryboardScene(
                    scene_index=idx,
                    duration_seconds=estimated,
                    narration_text=narration,
                    search_keywords=keywords,
                    image_prompt=(
                        f"Editorial cinematic frame illustrating {keywords}, "
                        f"mood {tone_palette[idx % len(tone_palette)]}, clean lighting"
                    ),
                    emotion_tone=tone_palette[idx % len(tone_palette)],
                )
            )

        total_duration = round(sum(s.duration_seconds for s in scenes), 2)
        logger.info(
            "SandboxLLMProvider generated %d scenes (total=%.1fs) for '%s'",
            len(scenes),
            total_duration,
            title,
        )
        return StoryboardResult(
            title=title,
            total_duration_seconds=total_duration,
            scenes=scenes,
        )

    async def translate_time_synced(
        self,
        segments: list[TranscriptSegment],
        target_lang: str,
        style_tone: str = "casual",
    ) -> list[TranslatedSegment]:
        """Produce readable time-synced segments preserving timestamps and speaker labels."""
        del style_tone
        lang = (target_lang or "vi").strip().lower()
        translated: list[TranslatedSegment] = []

        for seg in segments:
            original = (seg.text or "").strip()
            if lang == "vi" and original in _SANDBOX_VI_TRANSLATIONS:
                text_out = _SANDBOX_VI_TRANSLATIONS[original]
            elif original:
                text_out = original
            else:
                text_out = f"[{lang}] ..."

            translated.append(
                TranslatedSegment(
                    index=seg.index,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                    original_text=original,
                    translated_text=text_out,
                    target_syllables=max(1, len(text_out.split())),
                    speaker_label=seg.speaker_label,
                )
            )

        logger.info(
            "SandboxLLMProvider translated %d segments (target_lang=%s)",
            len(translated),
            lang,
        )
        return translated


class SandboxVisualProvider(BaseVisualProvider):
    """Zero-key visual generator creating local MP4 clips (or PNG fallback) inside TEMP_DIR."""

    provider_name = "sandbox_visual"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def _render_ffmpeg_clip(
        self,
        dest_mp4: Path,
        width: int,
        height: int,
        base_rgb: tuple[int, int, int],
    ) -> bool:
        ffmpeg_bin = (self.settings.FFMPEG_BIN or "ffmpeg").strip()
        resolved_bin = shutil.which(ffmpeg_bin) or (
            ffmpeg_bin if Path(ffmpeg_bin).is_file() else None
        )
        if not resolved_bin:
            return False

        hex_color = f"{base_rgb[0]:02x}{base_rgb[1]:02x}{base_rgb[2]:02x}"
        vf = (
            "drawbox=x=iw*0.08:y=ih*0.08:w=iw*0.84:h=ih*0.84:color=white@0.07:t=2,"
            "drawbox=x=iw*0.16:y=ih*0.42:w=iw*0.68:h=ih*0.16:color=white@0.05:t=fill"
        )
        cmd = [
            resolved_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{hex_color}:s={width}x{height}:r=24:d=5",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest_mp4),
        ]
        try:
            returncode, _, stderr = await run_subprocess_exec(*cmd, timeout=20.0)
            if returncode == 0 and dest_mp4.is_file() and dest_mp4.stat().st_size > 0:
                return True
            logger.debug(
                "SandboxVisualProvider ffmpeg exited %s: %s",
                returncode,
                stderr.decode("utf-8", errors="replace")[:200],
            )
        except Exception as exc:
            logger.debug("SandboxVisualProvider ffmpeg fallback to PNG: %s", exc)
        dest_mp4.unlink(missing_ok=True)
        return False

    async def fetch_visual(
        self,
        query: str,
        prompt: str | None,
        aspect_ratio: AspectRatio,
        media_type: Literal["video", "image"] = "video",
        output_dir: Path | None = None,
    ) -> VisualAsset:
        """Generate a local synthetic video clip (or PNG fallback) playable via /local-media."""
        ratio = (
            aspect_ratio
            if isinstance(aspect_ratio, AspectRatio)
            else AspectRatio(str(aspect_ratio))
        )
        width, height = _RATIO_DIMENSIONS.get(ratio, (720, 1280))

        seed_text = f"{query or ''}|{prompt or ''}|{ratio.value}"
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        palette = _SANDBOX_PALETTES[digest[0] % len(_SANDBOX_PALETTES)]

        storage_dir = (self.settings.temp_path / "local_storage" / "sandbox_visuals").resolve()
        storage_dir.mkdir(parents=True, exist_ok=True)

        stem = f"sbx_{uuid4().hex[:12]}"
        actual_media_type: Literal["video", "image"] = "image"
        saved_path = storage_dir / f"{stem}.png"

        if media_type == "video":
            mp4_path = storage_dir / f"{stem}.mp4"
            if await self._render_ffmpeg_clip(mp4_path, width, height, palette):
                actual_media_type = "video"
                saved_path = mp4_path

        if actual_media_type == "image":
            await asyncio.to_thread(_write_gradient_png, saved_path, width, height, palette)

        local_path = saved_path
        if output_dir is not None:
            dest_dir = Path(output_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            copied_path = dest_dir / saved_path.name
            if copied_path.resolve() != saved_path.resolve():
                await asyncio.to_thread(shutil.copy2, saved_path, copied_path)
            local_path = copied_path

        url = f"/local-media/storage/sandbox_visuals/{saved_path.name}"
        logger.info(
            "SandboxVisualProvider created %s visual '%s' (%dx%d)",
            actual_media_type,
            url,
            width,
            height,
        )
        return VisualAsset(
            url=url,
            local_path=local_path,
            media_type=actual_media_type,
            source=VisualSource.AI_GEN,
            width=width,
            height=height,
            duration_seconds=5.0 if actual_media_type == "video" else None,
            attribution="Local Sandbox Visual Generator",
        )


class SandboxSTTProvider(BaseSTTProvider):
    """Zero-key STT provider generating deterministic timed segments for Local Sandbox mode."""

    provider_name = "sandbox_stt"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def _probe_duration_ms(self, audio_path: Path) -> int:
        if audio_path.is_file() and audio_path.suffix.lower() == ".wav":
            try:
                with wave.open(str(audio_path), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0 and frames > 0:
                        return max(3000, int((frames / rate) * 1000))
            except Exception:
                pass

        ffprobe_bin = (self.settings.FFPROBE_BIN or "ffprobe").strip()
        resolved_probe = shutil.which(ffprobe_bin) or (
            ffprobe_bin if Path(ffprobe_bin).is_file() else None
        )
        if resolved_probe and audio_path.is_file():
            try:
                returncode, stdout, _ = await run_subprocess_exec(
                    resolved_probe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(audio_path),
                    timeout=5.0,
                )
                raw_sec = stdout.decode("utf-8", errors="replace").strip()
                if returncode == 0 and raw_sec:
                    sec = float(raw_sec)
                    if sec > 0:
                        return max(3000, int(sec * 1000))
            except Exception as exc:
                logger.debug("SandboxSTTProvider ffprobe fallback to 12000ms: %s", exc)

        return 12000

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult:
        """Probe audio duration and return 3 timed demo segments with optional word timings."""
        duration_ms = await self._probe_duration_ms(Path(audio_path))
        lines = [
            "Welcome to AI Video Studio local sandbox mode.",
            "You can edit every subtitle line, voice, and visual clip here.",
            "Click Confirm and Render when you are ready to export your video.",
        ]

        seg_count = len(lines)
        step_ms = max(1000, duration_ms // seg_count)
        segments: list[TranscriptSegment] = []

        for idx, text in enumerate(lines):
            start_ms = idx * step_ms
            end_ms = duration_ms if idx == seg_count - 1 else (idx + 1) * step_ms
            words_list: list[WordTimestamp] = []
            if word_timestamps:
                tokens = text.split()
                word_span = max(80, (end_ms - start_ms) // max(1, len(tokens)))
                for w_idx, tok in enumerate(tokens):
                    w_start = start_ms + w_idx * word_span
                    w_end = end_ms if w_idx == len(tokens) - 1 else min(end_ms, w_start + word_span)
                    words_list.append(
                        WordTimestamp(
                            word=tok,
                            start_ms=w_start,
                            end_ms=max(w_start + 1, w_end),
                        )
                    )

            segments.append(
                TranscriptSegment(
                    index=idx,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    speaker_label="SPEAKER_00",
                    words=words_list,
                )
            )

        resolved_lang = (language or "en").strip().lower() or "en"
        logger.info(
            "SandboxSTTProvider generated %d segments (duration_ms=%d, lang=%s)",
            len(segments),
            duration_ms,
            resolved_lang,
        )
        return TranscriptResult(
            language=resolved_lang,
            duration_ms=duration_ms,
            full_text=" ".join(lines),
            segments=segments,
        )
