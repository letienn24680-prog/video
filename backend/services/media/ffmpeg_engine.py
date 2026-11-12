"""Async FFmpeg media engine with hardware encoder auto-detection and CPU fallback."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    NotFoundError,
    ProviderError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.core.subprocess_utils import run_subprocess_exec
from backend.models.enums import AspectRatio
from backend.services.media.ass_karaoke import RESOLUTION_MAP
from backend.services.media.watermark import WatermarkOptions, build_watermark_filter

logger = get_logger(__name__)

_HW_ENCODERS: tuple[str, ...] = ("h264_nvenc", "h264_qsv", "h264_videotoolbox")


class MediaProbe(BaseModel):
    """Normalized media stream metadata extracted via ``ffprobe``."""

    duration_ms: int = Field(..., ge=0)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    fps: float = Field(default=0.0, ge=0.0)
    has_audio: bool = False
    video_codec: str | None = None


def _parse_fraction_fps(raw_fps: str | None) -> float:
    if not raw_fps or raw_fps in {"0/0", "0"}:
        return 0.0
    if "/" in raw_fps:
        num_s, den_s = raw_fps.split("/", 1)
        try:
            num = float(num_s)
            den = float(den_s)
            return round(num / den, 4) if den > 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(raw_fps), 4)
    except ValueError:
        return 0.0


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for FFmpeg filter arguments (e.g. ``ass='...'``)."""
    resolved = str(path.resolve()).replace("\\", "/")
    resolved = resolved.replace(":", "\\:")
    resolved = resolved.replace("'", "\\'")
    return resolved


def _build_atempo_filters(factor: float) -> list[str]:
    """Chain ``atempo`` filters so each stage remains strictly within ``[0.5, 2.0]``."""
    filters: list[str] = []
    remaining = float(factor)
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    return filters


class FFmpegEngine:
    """High-level async FFmpeg/FFprobe wrapper for audio, scene clips, and mastering."""

    _encoder_cache: dict[tuple[str, str, str], str] = {}
    _encoder_lock: asyncio.Lock | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.ffmpeg_bin = self.settings.FFMPEG_BIN or "ffmpeg"
        self.ffprobe_bin = self.settings.FFPROBE_BIN or "ffprobe"

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._encoder_lock is None:
            cls._encoder_lock = asyncio.Lock()
        return cls._encoder_lock

    async def _exec(self, args: list[str], op_name: str) -> tuple[bytes, bytes]:
        try:
            returncode, stdout, stderr = await run_subprocess_exec(*args)
        except FileNotFoundError as exc:
            binary = args[0] if args else self.ffmpeg_bin
            raise ConfigurationError(
                f"Binary '{binary}' not found; install FFmpeg or configure FFMPEG_BIN/FFPROBE_BIN"
            ) from exc

        if returncode != 0:
            err_tail = stderr.decode("utf-8", errors="ignore").strip()[-600:]
            raise ProviderError(
                "ffmpeg",
                f"{op_name} failed (exit {returncode}): {err_tail}",
            )
        return stdout, stderr

    async def _probe_encoder_usable(self, encoder: str) -> bool:
        try:
            returncode, _, _ = await run_subprocess_exec(
                self.ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.1",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            )
            return returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    async def resolve_video_encoder(self) -> str:
        """Auto-detect a working hardware H.264 encoder with cached fallback to ``libx264``."""
        hwaccel = (self.settings.FFMPEG_HWACCEL or "auto").strip().lower()
        preferred = (self.settings.FFMPEG_VIDEO_CODEC or "h264_nvenc").strip()
        fallback = (self.settings.FFMPEG_FALLBACK_CODEC or "libx264").strip()
        cache_key = (self.ffmpeg_bin, hwaccel, preferred)

        if cache_key in self._encoder_cache:
            return self._encoder_cache[cache_key]

        async with self._get_lock():
            if cache_key in self._encoder_cache:
                return self._encoder_cache[cache_key]

            if hwaccel in {"none", "cpu", "off", "false"}:
                self._encoder_cache[cache_key] = fallback
                logger.info("FFmpeg hardware acceleration disabled; using %s", fallback)
                return fallback

            explicit_map = {
                "nvenc": ["h264_nvenc"],
                "h264_nvenc": ["h264_nvenc"],
                "qsv": ["h264_qsv"],
                "h264_qsv": ["h264_qsv"],
                "videotoolbox": ["h264_videotoolbox"],
                "h264_videotoolbox": ["h264_videotoolbox"],
            }
            if hwaccel in explicit_map:
                candidates = explicit_map[hwaccel]
            else:
                ordered = [preferred, *_HW_ENCODERS]
                candidates = list(dict.fromkeys(c for c in ordered if c and c != fallback))

            listed_encoders = ""
            try:
                stdout, _ = await self._exec(
                    [self.ffmpeg_bin, "-hide_banner", "-encoders"],
                    "list encoders",
                )
                listed_encoders = stdout.decode("utf-8", errors="ignore")
            except (ConfigurationError, ProviderError):
                listed_encoders = ""

            selected = fallback
            for candidate in candidates:
                if listed_encoders and candidate not in listed_encoders:
                    continue
                if await self._probe_encoder_usable(candidate):
                    selected = candidate
                    break

            self._encoder_cache[cache_key] = selected
            if selected == fallback:
                logger.info("[FFmpeg] No usable hwaccel encoder found; using %s", fallback)
            else:
                logger.info("[FFmpeg] Detected hardware encoder: %s", selected)
            return selected

    def _video_encode_args(self, encoder: str) -> list[str]:
        preset = self.settings.FFMPEG_PRESET or "fast"
        crf = str(self.settings.FFMPEG_CRF)
        threads_args = (
            ["-threads", str(self.settings.FFMPEG_THREADS)]
            if self.settings.FFMPEG_THREADS > 0
            else []
        )

        if encoder == "h264_nvenc":
            return [*threads_args, "-c:v", "h264_nvenc", "-preset", "fast", "-cq", crf, "-pix_fmt", "yuv420p"]
        if encoder == "h264_qsv":
            return [*threads_args, "-c:v", "h264_qsv", "-preset", preset, "-global_quality", crf, "-pix_fmt", "yuv420p"]
        if encoder == "h264_videotoolbox":
            return [*threads_args, "-c:v", "h264_videotoolbox", "-q:v", "65", "-pix_fmt", "yuv420p"]

        return [*threads_args, "-c:v", encoder, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]

    async def _run_encode_with_fallback(
        self,
        build_cmd: Callable[[str], list[str]],
        op_name: str,
    ) -> None:
        encoder = await self.resolve_video_encoder()
        fallback = self.settings.FFMPEG_FALLBACK_CODEC or "libx264"

        try:
            await self._exec(build_cmd(encoder), op_name)
        except ProviderError as exc:
            if encoder == fallback:
                raise
            logger.warning(
                "[FFmpeg] Encoder '%s' failed during %s (%s); fallback to %s preset %s",
                encoder,
                op_name,
                exc.message,
                fallback,
                self.settings.FFMPEG_PRESET,
            )
            await self._exec(build_cmd(fallback), f"{op_name} (fallback {fallback})")

    async def probe(self, media_path: Path) -> MediaProbe:
        """Inspect media duration, resolution, frame rate, and audio presence via ``ffprobe``."""
        src = Path(media_path)
        if not src.is_file():
            raise NotFoundError(f"Media file not found for probing: {src}")

        stdout, _ = await self._exec(
            [
                self.ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(src),
            ],
            f"probe {src.name}",
        )

        try:
            payload = json.loads(stdout.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError as exc:
            raise ProviderError("ffprobe", f"Invalid JSON returned by ffprobe: {exc}") from exc

        streams = payload.get("streams") or []
        fmt = payload.get("format") or {}

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        raw_duration = fmt.get("duration")
        if raw_duration is None and video_stream is not None:
            raw_duration = video_stream.get("duration")
        if raw_duration is None and streams:
            raw_duration = streams[0].get("duration")

        try:
            duration_ms = max(0, int(round(float(raw_duration) * 1000))) if raw_duration is not None else 0
        except (TypeError, ValueError):
            duration_ms = 0

        width = int(video_stream.get("width") or 0) if video_stream else 0
        height = int(video_stream.get("height") or 0) if video_stream else 0
        fps = 0.0
        video_codec: str | None = None
        if video_stream:
            fps = _parse_fraction_fps(video_stream.get("avg_frame_rate")) or _parse_fraction_fps(
                video_stream.get("r_frame_rate")
            )
            video_codec = video_stream.get("codec_name")

        return MediaProbe(
            duration_ms=duration_ms,
            width=width,
            height=height,
            fps=fps,
            has_audio=has_audio,
            video_codec=video_codec,
        )

    async def extract_audio(
        self,
        media_path: Path,
        output_wav: Path,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> Path:
        """Extract PCM 16-bit WAV audio from ``media_path``."""
        src = Path(media_path)
        if not src.is_file():
            raise NotFoundError(f"Media file not found for audio extraction: {src}")

        dest = Path(output_wav)
        dest.parent.mkdir(parents=True, exist_ok=True)

        await self._exec(
            [
                self.ffmpeg_bin,
                "-y",
                "-i",
                str(src),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                str(dest),
            ],
            f"extract_audio {src.name}",
        )
        return dest

    async def adjust_audio_speed(
        self,
        audio_path: Path,
        target_duration_ms: int,
        output_path: Path,
        max_atempo: float = 1.25,
    ) -> tuple[Path, int]:
        """Time-fit audio to ``target_duration_ms`` using ``atempo`` (capped at ``max_atempo``) and pad/trim."""
        src = Path(audio_path)
        if not src.is_file():
            raise NotFoundError(f"Audio file not found for speed adjustment: {src}")
        if target_duration_ms <= 0:
            raise ValidationError("target_duration_ms must be greater than 0")

        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        info = await self.probe(src)
        actual_ms = info.duration_ms
        if actual_ms <= 0:
            raise ProviderError("ffmpeg", f"Cannot adjust speed of 0ms audio file: {src.name}")

        target_sec = target_duration_ms / 1000.0
        filters: list[str] = []

        if actual_ms > target_duration_ms:
            ratio = actual_ms / float(target_duration_ms)
            clamped_max = max(1.0, float(max_atempo))
            applied_tempo = min(clamped_max, ratio)
            if applied_tempo > 1.005:
                filters.extend(_build_atempo_filters(applied_tempo))
            # Only trim/pad to exact target if speedup fit within max_atempo
            if ratio <= clamped_max:
                filters.append(f"apad=whole_dur={target_sec:.3f}")
                filters.append(f"atrim=0:{target_sec:.3f}")
        elif actual_ms < target_duration_ms:
            filters.append(f"apad=whole_dur={target_sec:.3f}")
            filters.append(f"atrim=0:{target_sec:.3f}")

        if not filters:
            if src.resolve() != dest.resolve():
                await asyncio.to_thread(shutil.copy2, src, dest)
            return dest, actual_ms

        codec_args = (
            ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2"]
            if dest.suffix.lower() == ".wav"
            else ["-c:a", self.settings.FFMPEG_AUDIO_CODEC, "-b:a", self.settings.FFMPEG_AUDIO_BITRATE]
        )

        await self._exec(
            [
                self.ffmpeg_bin,
                "-y",
                "-i",
                str(src),
                "-af",
                ",".join(filters),
                *codec_args,
                str(dest),
            ],
            f"adjust_audio_speed {src.name}",
        )

        out_probe = await self.probe(dest)
        return dest, out_probe.duration_ms

    async def _mix_dub_batch(
        self,
        batch: list[tuple[int, int, Path]],
        total_duration_ms: int,
        output_wav: Path,
    ) -> Path:
        total_sec = max(0.1, total_duration_ms / 1000.0)
        cmd: list[str] = [
            self.ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{total_sec:.3f}",
            "-i",
            "anullsrc=r=44100:cl=stereo",
        ]
        filter_parts: list[str] = []
        mix_labels: list[str] = ["[0:a]"]

        for idx, (start_ms, _end_ms, seg_path) in enumerate(batch, start=1):
            cmd.extend(["-i", str(seg_path)])
            delay_ms = max(0, int(start_ms))
            label = f"[s{idx}]"
            filter_parts.append(
                f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms}{label}"
            )
            mix_labels.append(label)

        filter_parts.append(
            f"{''.join(mix_labels)}amix=inputs={len(batch) + 1}:duration=first:normalize=0[out]"
        )

        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[out]",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(output_wav),
            ]
        )
        await self._exec(cmd, "build_dubbed_audio_track batch")
        return output_wav

    async def build_dubbed_audio_track(
        self,
        segments: list[tuple[int, int, Path]],
        total_duration_ms: int,
        output_wav: Path,
    ) -> Path:
        """Assemble dubbed voice segments onto a single timeline WAV of ``total_duration_ms``."""
        dest = Path(output_wav)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if total_duration_ms <= 0:
            if segments:
                total_duration_ms = max(end_ms for _s, end_ms, _p in segments)
            if total_duration_ms <= 0:
                raise ValidationError("total_duration_ms must be greater than 0")

        if not segments:
            total_sec = max(0.1, total_duration_ms / 1000.0)
            await self._exec(
                [
                    self.ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-t",
                    f"{total_sec:.3f}",
                    "-i",
                    "anullsrc=r=44100:cl=stereo",
                    "-acodec",
                    "pcm_s16le",
                    str(dest),
                ],
                "generate silent dubbed track",
            )
            return dest

        for _start_ms, _end_ms, seg_path in segments:
            if not Path(seg_path).is_file():
                raise NotFoundError(f"Dubbed audio segment not found: {seg_path}")

        batch_size = 25
        if len(segments) <= batch_size:
            return await self._mix_dub_batch(segments, total_duration_ms, dest)

        chunk_dir = dest.parent / f"_{dest.stem}_dub_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        try:
            chunk_paths: list[Path] = []
            for b_idx in range(0, len(segments), batch_size):
                batch = segments[b_idx : b_idx + batch_size]
                chunk_out = chunk_dir / f"chunk_{b_idx // batch_size:03d}.wav"
                await self._mix_dub_batch(batch, total_duration_ms, chunk_out)
                chunk_paths.append(chunk_out)

            final_inputs = [(0, total_duration_ms, p) for p in chunk_paths]
            return await self._mix_dub_batch(final_inputs, total_duration_ms, dest)
        finally:
            shutil.rmtree(chunk_dir, ignore_errors=True)

    async def mix_voice_and_bgm(
        self,
        voice_path: Path,
        bgm_path: Path | None,
        output_audio: Path,
        *,
        bgm_volume: float = 0.25,
        auto_ducking: bool = True,
        fast_mode_duck_db: float = -14.0,
        is_fast_mode: bool = False,
    ) -> Path:
        """Mix voiceover with BGM using FFmpeg ``sidechaincompress`` ducking or fast-mode attenuation."""
        voice = Path(voice_path)
        if not voice.is_file():
            raise NotFoundError(f"Voice track not found: {voice}")

        dest = Path(output_audio)
        dest.parent.mkdir(parents=True, exist_ok=True)

        codec_args = (
            ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2"]
            if dest.suffix.lower() == ".wav"
            else ["-c:a", self.settings.FFMPEG_AUDIO_CODEC, "-b:a", self.settings.FFMPEG_AUDIO_BITRATE]
        )

        if bgm_path is None or not Path(bgm_path).is_file() or (not is_fast_mode and bgm_volume <= 0.0):
            await self._exec(
                [
                    self.ffmpeg_bin,
                    "-y",
                    "-i",
                    str(voice),
                    "-af",
                    "aformat=sample_rates=44100:channel_layouts=stereo",
                    *codec_args,
                    str(dest),
                ],
                "normalize voice audio",
            )
            return dest

        voice_probe = await self.probe(voice)
        voice_dur_sec = max(0.1, voice_probe.duration_ms / 1000.0) if voice_probe.duration_ms > 0 else None

        bgm_gain = (
            f"volume={float(fast_mode_duck_db):.1f}dB"
            if is_fast_mode
            else f"volume={max(0.0, float(bgm_volume)):.3f}"
        )

        if auto_ducking:
            filter_complex = (
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[v_sc][v_mix];"
                f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,{bgm_gain}[bgm_scaled];"
                f"[bgm_scaled][v_sc]sidechaincompress="
                f"threshold={self.settings.FFMPEG_DUCKING_THRESHOLD}:"
                f"ratio={self.settings.FFMPEG_DUCKING_RATIO}:"
                f"attack={self.settings.FFMPEG_DUCKING_ATTACK}:"
                f"release={self.settings.FFMPEG_DUCKING_RELEASE}[bgm_ducked];"
                f"[v_mix][bgm_ducked]amix=inputs=2:duration=first:normalize=0[out]"
            )
        else:
            filter_complex = (
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[v_mix];"
                f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,{bgm_gain}[bgm_scaled];"
                f"[v_mix][bgm_scaled]amix=inputs=2:duration=first:normalize=0[out]"
            )

        cmd: list[str] = [
            self.ffmpeg_bin,
            "-y",
            "-i",
            str(voice),
            "-stream_loop",
            "-1",
            "-i",
            str(bgm_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
        ]
        if voice_dur_sec is not None:
            cmd.extend(["-t", f"{voice_dur_sec:.3f}"])
        cmd.extend([*codec_args, str(dest)])

        await self._exec(cmd, "mix_voice_and_bgm")
        return dest

    async def prepare_scene_clip(
        self,
        asset_path: Path,
        media_type: Literal["video", "image"],
        duration_seconds: float,
        output_path: Path,
        aspect_ratio: AspectRatio,
        ken_burns: bool = True,
        scene_index: int = 0,
    ) -> Path:
        """Normalize a video clip or animate a static image with Ken Burns ``zoompan``."""
        src = Path(asset_path)
        if not src.is_file():
            raise NotFoundError(f"Scene visual asset not found: {src}")

        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        width, height = RESOLUTION_MAP[aspect_ratio]
        fps = 30
        dur = max(0.5, float(duration_seconds))

        if media_type == "video":
            info = await self.probe(src)
            loop_args = (
                ["-stream_loop", "-1"]
                if info.duration_ms > 0 and info.duration_ms < int(dur * 1000)
                else []
            )
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
            )

            def build_cmd(enc: str) -> list[str]:
                return [
                    self.ffmpeg_bin,
                    "-y",
                    *loop_args,
                    "-i",
                    str(src),
                    "-t",
                    f"{dur:.3f}",
                    "-vf",
                    vf,
                    "-an",
                    *self._video_encode_args(enc),
                    str(dest),
                ]

            await self._run_encode_with_fallback(build_cmd, f"prepare_scene_clip video #{scene_index}")
            return dest

        total_frames = max(1, int(round(dur * fps)))
        if ken_burns:
            pre_w, pre_h = width * 2, height * 2
            pattern = scene_index % 4
            if pattern == 0:
                zp = "z='min(zoom+0.0015,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif pattern == 1:
                zp = "z='if(lte(on,1),1.18,max(1.0,zoom-0.0015))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            elif pattern == 2:
                zp = f"z='1.12':x='(iw-iw/zoom)*(on/{total_frames})':y='ih/2-(ih/zoom/2)'"
            else:
                zp = f"z='1.12':x='(iw-iw/zoom)*(1-on/{total_frames})':y='ih/2-(ih/zoom/2)'"

            vf = (
                f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase,"
                f"crop={pre_w}:{pre_h},"
                f"zoompan={zp}:d={total_frames}:s={width}x{height}:fps={fps},"
                f"setsar=1,format=yuv420p"
            )

            def build_kb_cmd(enc: str) -> list[str]:
                return [
                    self.ffmpeg_bin,
                    "-y",
                    "-i",
                    str(src),
                    "-t",
                    f"{dur:.3f}",
                    "-vf",
                    vf,
                    "-an",
                    *self._video_encode_args(enc),
                    str(dest),
                ]

            await self._run_encode_with_fallback(build_kb_cmd, f"prepare_scene_clip ken_burns #{scene_index}")
            return dest

        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
        )

        def build_static_cmd(enc: str) -> list[str]:
            return [
                self.ffmpeg_bin,
                "-y",
                "-loop",
                "1",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(src),
                "-vf",
                vf,
                "-an",
                *self._video_encode_args(enc),
                str(dest),
            ]

        await self._run_encode_with_fallback(build_static_cmd, f"prepare_scene_clip image #{scene_index}")
        return dest

    async def _concat_demuxer(self, clip_paths: list[Path], output_path: Path) -> Path:
        list_file = output_path.parent / f"_{output_path.stem}_concat.txt"
        lines = []
        for p in clip_paths:
            escaped = str(p.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            try:
                await self._exec(
                    [
                        self.ffmpeg_bin,
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(list_file),
                        "-c",
                        "copy",
                        str(output_path),
                    ],
                    "concat_demuxer copy",
                )
            except ProviderError:
                def build_reencode_cmd(enc: str) -> list[str]:
                    return [
                        self.ffmpeg_bin,
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(list_file),
                        "-an",
                        *self._video_encode_args(enc),
                        str(output_path),
                    ]

                await self._run_encode_with_fallback(build_reencode_cmd, "concat_demuxer reencode")
            return output_path
        finally:
            list_file.unlink(missing_ok=True)

    async def _xfade_batch(
        self,
        clip_paths: list[Path],
        output_path: Path,
        transition_duration: float,
    ) -> Path:
        probes = await asyncio.gather(*(self.probe(p) for p in clip_paths))
        durations = [max(0.2, p.duration_ms / 1000.0) for p in probes]
        min_dur = min(durations)
        td = min(float(transition_duration), min_dur * 0.35)
        if td < 0.05:
            return await self._concat_demuxer(clip_paths, output_path)

        inputs: list[str] = []
        for p in clip_paths:
            inputs.extend(["-i", str(p)])

        filter_parts: list[str] = []
        offset = max(0.05, durations[0] - td)
        prev_label = "[0:v]"
        for idx in range(1, len(clip_paths)):
            out_label = f"[vx{idx}]"
            filter_parts.append(
                f"{prev_label}[{idx}:v]xfade=transition=fade:duration={td:.3f}:offset={offset:.3f}{out_label}"
            )
            prev_label = out_label
            if idx < len(clip_paths) - 1:
                offset = max(offset + 0.05, offset + durations[idx] - td)

        filter_parts.append(f"{prev_label}format=yuv420p[vout]")
        filter_complex = ";".join(filter_parts)

        def build_cmd(enc: str) -> list[str]:
            return [
                self.ffmpeg_bin,
                "-y",
                *inputs,
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-an",
                *self._video_encode_args(enc),
                str(output_path),
            ]

        await self._run_encode_with_fallback(build_cmd, "concat_video_clips xfade")
        return output_path

    async def concat_video_clips(
        self,
        clip_paths: list[Path],
        output_path: Path,
        transition_duration: float = 0.35,
        use_xfade: bool = True,
    ) -> Path:
        """Concatenate normalized scene clips with optional ``xfade`` crossfade transitions."""
        if not clip_paths:
            raise ValidationError("clip_paths cannot be empty")

        for clip in clip_paths:
            if not Path(clip).is_file():
                raise NotFoundError(f"Scene clip not found for concatenation: {clip}")

        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if len(clip_paths) == 1:
            if Path(clip_paths[0]).resolve() != dest.resolve():
                await asyncio.to_thread(shutil.copy2, clip_paths[0], dest)
            return dest

        if not use_xfade or transition_duration <= 0:
            return await self._concat_demuxer(clip_paths, dest)

        batch_size = 12
        if len(clip_paths) <= batch_size:
            return await self._xfade_batch(clip_paths, dest, transition_duration)

        batch_dir = dest.parent / f"_{dest.stem}_xfade_batches"
        batch_dir.mkdir(parents=True, exist_ok=True)
        try:
            current_paths = list(clip_paths)
            round_num = 0
            while len(current_paths) > batch_size:
                next_paths: list[Path] = []
                for idx in range(0, len(current_paths), batch_size):
                    chunk = current_paths[idx : idx + batch_size]
                    if len(chunk) == 1:
                        next_paths.append(chunk[0])
                    else:
                        chunk_out = batch_dir / f"r{round_num}_b{idx // batch_size:03d}.mp4"
                        await self._xfade_batch(chunk, chunk_out, transition_duration)
                        next_paths.append(chunk_out)
                current_paths = next_paths
                round_num += 1
            return await self._xfade_batch(current_paths, dest, transition_duration)
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)

    async def master_final_video(
        self,
        video_path: Path,
        audio_path: Path | None,
        output_path: Path,
        *,
        aspect_ratio: AspectRatio,
        ass_subtitle_path: Path | None = None,
        watermark: WatermarkOptions | None = None,
    ) -> Path:
        """Burn ``.ass`` subtitles, overlay watermark, mux audio, and encode final MP4."""
        src_video = Path(video_path)
        if not src_video.is_file():
            raise NotFoundError(f"Master input video not found: {src_video}")

        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        width, height = RESOLUTION_MAP[aspect_ratio]
        inputs: list[str] = ["-i", str(src_video)]

        has_audio_input = audio_path is not None and Path(audio_path).is_file()
        if audio_path is not None and not has_audio_input:
            raise NotFoundError(f"Master audio file not found: {audio_path}")

        if has_audio_input:
            inputs.extend(["-i", str(audio_path)])
            audio_map = "1:a"
            wm_input_idx = 2
        else:
            audio_map = "0:a?"
            wm_input_idx = 1

        wm_filter: str | None = None
        wm_extra_args: list[str] = []
        if watermark is not None:
            wm_filter, wm_extra_args = build_watermark_filter(watermark, width, height)
            if wm_extra_args:
                inputs.extend(wm_extra_args)

        base_filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
        ]
        if ass_subtitle_path is not None:
            ass_path = Path(ass_subtitle_path)
            if not ass_path.is_file():
                raise NotFoundError(f"ASS subtitle file not found: {ass_path}")
            base_filters.append(f"ass='{_escape_ffmpeg_filter_path(ass_path)}'")

        if wm_filter and wm_extra_args:
            rewritten_wm = (
                wm_filter.replace("[1:v]", f"[{wm_input_idx}:v]")
                .replace("[0:v]", "[v_base]")
            )
            filter_complex = f"[0:v]{','.join(base_filters)}[v_base];{rewritten_wm}[v_out]"
        else:
            if wm_filter:
                base_filters.append(wm_filter)
            filter_complex = f"[0:v]{','.join(base_filters)}[v_out]"

        def build_cmd(enc: str) -> list[str]:
            cmd = [
                self.ffmpeg_bin,
                "-y",
                *inputs,
                "-filter_complex",
                filter_complex,
                "-map",
                "[v_out]",
                "-map",
                audio_map,
                *self._video_encode_args(enc),
                "-c:a",
                self.settings.FFMPEG_AUDIO_CODEC,
                "-b:a",
                self.settings.FFMPEG_AUDIO_BITRATE,
                "-movflags",
                "+faststart",
            ]
            if has_audio_input:
                cmd.append("-shortest")
            cmd.append(str(dest))
            return cmd

        await self._run_encode_with_fallback(build_cmd, "master_final_video")
        logger.info("Mastered final video (%dx%d) -> %s", width, height, dest)
        return dest
