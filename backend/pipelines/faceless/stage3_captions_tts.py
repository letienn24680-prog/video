"""Stage 3 of the AI Faceless Video pipeline: TTS synthesis, word alignment, and .ass karaoke."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.config import Settings
from backend.core.exceptions import ConfigurationError, ProviderError, ValidationError
from backend.core.logging import get_logger
from backend.models.project import PipelineConfig, ProjectResponse
from backend.models.provider import WordTimestamp
from backend.models.scene import SceneCreate, SceneResponse
from backend.providers.base.stt import BaseSTTProvider
from backend.providers.base.tts import BaseTTSProvider
from backend.services.media.ass_karaoke import AssKaraokeGenerator, KaraokeSegmentInput
from backend.services.media.ffmpeg_engine import FFmpegEngine
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

ProgressEmitter = Callable[..., Awaitable[None]]

_ALNUM_RE = re.compile(r"[^\w]", re.UNICODE)


@dataclass(slots=True)
class Stage3CaptionsTTSResult:
    """Outputs from Stage 3 needed by Stage 4 assembly."""

    scenes: list[SceneResponse]
    scene_durations_ms: dict[int, int]
    scene_audio_paths: dict[int, Path]
    voiceover_path: Path
    ass_path: Path | None
    total_duration_ms: int


async def _noop_progress(*_args: Any, **_kwargs: Any) -> None:
    return None


def resolve_default_tts_voice(provider_name: str, settings: Settings) -> str:
    """Resolve the default voice_id for a TTS provider from Settings."""
    defaults = {
        "edge_tts": settings.EDGE_TTS_DEFAULT_VOICE,
        "elevenlabs": settings.ELEVENLABS_DEFAULT_VOICE_ID,
        "fpt_ai": settings.FPT_AI_DEFAULT_VOICE,
        "viettel_ai": settings.VIETTEL_AI_DEFAULT_VOICE,
    }
    return defaults.get(provider_name, settings.EDGE_TTS_DEFAULT_VOICE)


def compute_proportional_word_timestamps(
    text: str,
    start_ms: int,
    end_ms: int,
) -> list[WordTimestamp]:
    """Compute monotonic proportional word timestamps across ``[start_ms, end_ms]``."""
    tokens = [tok for tok in (text or "").strip().split() if tok]
    if not tokens or end_ms <= start_ms:
        return []

    weights: list[float] = []
    for tok in tokens:
        core_len = len(_ALNUM_RE.sub("", tok))
        weight = max(1.0, float(core_len) + 0.5)
        if tok.endswith((".", "!", "?", "…")):
            weight += 1.5
        elif tok.endswith((",", ";", ":")):
            weight += 0.8
        weights.append(weight)

    total_weight = sum(weights) or float(len(tokens))
    total_ms = end_ms - start_ms
    results: list[WordTimestamp] = []
    cursor_ms = start_ms

    for idx, (tok, weight) in enumerate(zip(tokens, weights)):
        if idx == len(tokens) - 1:
            word_end = end_ms
        else:
            remaining_words = len(tokens) - idx - 1
            raw_dur = round(total_ms * (weight / total_weight))
            max_allowed = max(0, end_ms - cursor_ms - remaining_words)
            dur = max(1 if (end_ms - cursor_ms) > remaining_words else 0, min(raw_dur, max_allowed))
            word_end = cursor_ms + dur

        word_end = max(cursor_ms, min(end_ms, word_end))
        results.append(
            WordTimestamp(
                word=tok,
                start_ms=cursor_ms,
                end_ms=word_end,
            )
        )
        cursor_ms = word_end

    return results


def _shift_and_clamp_words(
    words: list[WordTimestamp],
    *,
    offset_ms: int,
    scene_start_ms: int,
    scene_end_ms: int,
) -> list[WordTimestamp]:
    """Shift local/relative word timings by ``offset_ms`` and clamp inside scene bounds."""
    shifted: list[WordTimestamp] = []
    cursor = scene_start_ms
    for w in words:
        if not w.word or not w.word.strip():
            continue
        w_start = max(cursor, min(scene_end_ms, w.start_ms + offset_ms))
        w_end = max(w_start, min(scene_end_ms, w.end_ms + offset_ms))
        shifted.append(
            WordTimestamp(
                word=w.word.strip(),
                start_ms=w_start,
                end_ms=w_end,
                confidence=w.confidence,
            )
        )
        cursor = w_end
    if shifted:
        shifted[-1] = WordTimestamp(
            word=shifted[-1].word,
            start_ms=shifted[-1].start_ms,
            end_ms=scene_end_ms,
            confidence=shifted[-1].confidence,
        )
    return shifted


async def _align_words_for_scene(
    *,
    scene: SceneResponse,
    text: str,
    audio_path: Path,
    scene_start_ms: int,
    scene_end_ms: int,
    target_lang: str,
    stt_provider: BaseSTTProvider | None,
) -> list[WordTimestamp]:
    """Resolve word timestamps via optional STT alignment or proportional distribution."""
    actual_dur_ms = scene_end_ms - scene_start_ms
    orig_base = (scene.narration_text or scene.original_text or "").strip()
    edited = (scene.edited_text or "").strip()
    text_was_edited = bool(edited and edited != orig_base)

    if scene.word_timestamps and not text_was_edited:
        # Re-map existing timestamps if scene shifted on cumulative timeline
        orig_start = scene.start_ms
        orig_dur = max(1, scene.end_ms - scene.start_ms)
        if abs(orig_dur - actual_dur_ms) <= 150:
            return _shift_and_clamp_words(
                scene.word_timestamps,
                offset_ms=scene_start_ms - orig_start,
                scene_start_ms=scene_start_ms,
                scene_end_ms=scene_end_ms,
            )

    if stt_provider is not None:
        try:
            transcript = await stt_provider.transcribe(
                audio_path,
                language=target_lang or None,
                word_timestamps=True,
            )
            aligned: list[WordTimestamp] = []
            for seg in transcript.segments:
                aligned.extend(seg.words)
            if aligned:
                return _shift_and_clamp_words(
                    aligned,
                    offset_ms=scene_start_ms,
                    scene_start_ms=scene_start_ms,
                    scene_end_ms=scene_end_ms,
                )
        except (ConfigurationError, ProviderError) as exc:
            logger.warning(
                "Optional STT alignment failed for scene_index=%d (%s); using proportional timings",
                scene.scene_index,
                exc.message,
            )
        except Exception as exc:
            logger.warning(
                "Unexpected error during STT alignment for scene_index=%d (%s); using proportional timings",
                scene.scene_index,
                exc,
            )

    return compute_proportional_word_timestamps(text, scene_start_ms, scene_end_ms)


async def run_stage3_captions_tts(
    project: ProjectResponse,
    scenes: list[SceneResponse],
    cfg: PipelineConfig,
    settings: Settings,
    work_dir: Path,
    tts_provider: BaseTTSProvider,
    ffmpeg_engine: FFmpegEngine,
    repo: SupabaseRepository,
    *,
    stt_provider: BaseSTTProvider | None = None,
    ass_generator: AssKaraokeGenerator | None = None,
    emit_progress: ProgressEmitter | None = None,
) -> Stage3CaptionsTTSResult:
    """Synthesize per-scene TTS, align word timestamps, build voiceover track, and generate .ass."""
    if not scenes:
        raise ValidationError(
            "Cannot render Faceless project with zero scenes",
            details={"project_id": str(project.id)},
        )

    notify = emit_progress or _noop_progress
    tts_dir = work_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    ordered_scenes = sorted(scenes, key=lambda s: s.scene_index)
    total_scenes = len(ordered_scenes)
    default_voice = resolve_default_tts_voice(tts_provider.provider_name, settings)

    await notify(
        "tts_captions",
        65,
        f"Synthesizing voiceover for {total_scenes} scenes via {tts_provider.provider_name}...",
    )

    cumulative_ms = 0
    scene_audio_paths: dict[int, Path] = {}
    scene_durations_ms: dict[int, int] = {}
    dub_segments: list[tuple[int, int, Path]] = []
    updated_creates: list[SceneCreate] = []
    karaoke_inputs: list[KaraokeSegmentInput] = []

    for pos, scene in enumerate(ordered_scenes):
        text = (scene.edited_text or scene.narration_text or scene.original_text or "").strip()
        if not text:
            raise ValidationError(
                f"Scene {scene.scene_index} has empty narration text",
                details={"project_id": str(project.id), "scene_id": str(scene.id)},
            )

        speaker_voice = (
            cfg.speaker_voices.get(scene.speaker_label)
            if scene.speaker_label and cfg.speaker_voices
            else None
        )
        voice_id = scene.voice_id or speaker_voice or cfg.voice_id or default_voice
        rate_pct = scene.tts_rate_pct if scene.tts_rate_pct != 0 else cfg.tts_rate_pct

        raw_audio_out = tts_dir / f"scene_{scene.scene_index:04d}.mp3"
        synth = await tts_provider.synthesize(
            text=text,
            voice_id=voice_id,
            output_path=raw_audio_out,
            rate_pct=rate_pct,
        )
        audio_path = Path(synth.audio_path)

        duration_ms = int(synth.duration_ms or 0)
        if duration_ms <= 0:
            probed = await ffmpeg_engine.probe(audio_path)
            duration_ms = probed.duration_ms
        duration_ms = max(500, duration_ms)

        scene_start_ms = cumulative_ms
        scene_end_ms = scene_start_ms + duration_ms
        cumulative_ms = scene_end_ms

        word_timestamps = await _align_words_for_scene(
            scene=scene,
            text=text,
            audio_path=audio_path,
            scene_start_ms=scene_start_ms,
            scene_end_ms=scene_end_ms,
            target_lang=cfg.target_lang,
            stt_provider=stt_provider,
        )

        scene_audio_paths[scene.scene_index] = audio_path
        scene_durations_ms[scene.scene_index] = duration_ms
        dub_segments.append((scene_start_ms, scene_end_ms, audio_path))
        karaoke_inputs.append(
            KaraokeSegmentInput(
                start_ms=scene_start_ms,
                end_ms=scene_end_ms,
                text=text,
                words=word_timestamps,
            )
        )

        updated_creates.append(
            SceneCreate(
                project_id=project.id,
                scene_index=scene.scene_index,
                start_ms=scene_start_ms,
                end_ms=scene_end_ms,
                original_text=scene.original_text,
                edited_text=scene.edited_text,
                narration_text=scene.narration_text,
                word_timestamps=word_timestamps,
                search_keywords=scene.search_keywords,
                image_prompt=scene.image_prompt,
                emotion_tone=scene.emotion_tone,
                speaker_label=scene.speaker_label,
                voice_id=voice_id,
                tts_rate_pct=rate_pct,
                audio_url=scene.audio_url,
                visual_url=scene.visual_url,
                visual_source=scene.visual_source,
                subtitle_style=scene.subtitle_style,
                is_locked=scene.is_locked,
            )
        )

        pct = 65 + int(round(((pos + 1) / max(1, total_scenes)) * 10))
        await notify(
            "tts_captions",
            min(75, pct),
            f"Synthesized voiceover {pos + 1}/{total_scenes}",
            extra_data={"completed_items": pos + 1, "total_items": total_scenes},
        )

    persisted_scenes = await repo.upsert_scenes(project.id, updated_creates)

    voiceover_path = work_dir / "voiceover.wav"
    await ffmpeg_engine.build_dubbed_audio_track(
        dub_segments,
        total_duration_ms=cumulative_ms,
        output_wav=voiceover_path,
    )

    ass_path: Path | None = None
    should_burn = bool(cfg.burn_subtitles and cfg.subtitle_style.burn_subtitles)
    if should_burn:
        generator = ass_generator or AssKaraokeGenerator()
        ass_path = generator.generate(
            karaoke_inputs,
            work_dir / "subtitles.ass",
            aspect_ratio=project.aspect_ratio,
            style=cfg.subtitle_style,
        )

    await notify(
        "tts_captions",
        80,
        f"Voiceover track & karaoke captions ready ({cumulative_ms / 1000.0:.1f}s)",
    )
    logger.info(
        "Stage 3 complete for project=%s: scenes=%d total_ms=%d ass=%s",
        project.id,
        len(persisted_scenes),
        cumulative_ms,
        ass_path,
    )

    return Stage3CaptionsTTSResult(
        scenes=persisted_scenes,
        scene_durations_ms=scene_durations_ms,
        scene_audio_paths=scene_audio_paths,
        voiceover_path=voiceover_path,
        ass_path=ass_path,
        total_duration_ms=cumulative_ms,
    )
