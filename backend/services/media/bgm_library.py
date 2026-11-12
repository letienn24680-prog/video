"""Curated BGM Mood Library with offline deterministic WAV synthesis & path resolution."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import struct
import wave
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx
from pydantic import BaseModel, Field

from backend.core.config import Settings, get_settings
from backend.core.exceptions import NotFoundError
from backend.core.logging import get_logger
from backend.providers.base.storage import BaseStorageProvider

logger = get_logger(__name__)

_AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"})

# Curated mood synthesis recipes:
# Each entry maps preset_id -> (chords_hz, lfo_hz, pulse_depth, brightness, detune_ratio)
_MOOD_RECIPES: dict[str, dict[str, Any]] = {
    "lofi_chill": {
        "chords": [
            (174.61, 220.00, 261.63, 329.63),  # Fmaj7
            (164.81, 196.00, 246.94, 293.66),  # Em7
            (146.83, 174.61, 220.00, 329.63),  # Dm9
            (130.81, 164.81, 196.00, 246.94),  # Cmaj7
        ],
        "lfo_hz": 0.35,
        "pulse_depth": 0.12,
        "harmonics": (1.0, 0.25, 0.06),
        "detune": 0.0025,
    },
    "cinematic_epic": {
        "chords": [
            (73.42, 146.83, 220.00, 293.66),   # Dm power/fifth
            (58.27, 116.54, 174.61, 233.08),   # Bb5
            (87.31, 174.61, 261.63, 349.23),   # F5
            (65.41, 130.81, 196.00, 261.63),   # C5
        ],
        "lfo_hz": 0.25,
        "pulse_depth": 0.18,
        "harmonics": (1.0, 0.42, 0.18),
        "detune": 0.0035,
    },
    "upbeat_tech": {
        "chords": [
            (130.81, 196.00, 261.63, 293.66),  # Cadd9
            (98.00, 196.00, 246.94, 293.66),   # G
            (110.00, 164.81, 220.00, 261.63),  # Am7
            (87.31, 174.61, 220.00, 329.63),   # Fmaj7
        ],
        "lfo_hz": 2.0,
        "pulse_depth": 0.24,
        "harmonics": (1.0, 0.35, 0.15),
        "detune": 0.0020,
    },
    "corporate_clean": {
        "chords": [
            (98.00, 196.00, 246.94, 293.66),   # G
            (146.83, 185.00, 220.00, 293.66),  # D
            (164.81, 196.00, 246.94, 329.63),  # Em7
            (130.81, 164.81, 196.00, 293.66),  # Cadd9
        ],
        "lfo_hz": 0.85,
        "pulse_depth": 0.10,
        "harmonics": (1.0, 0.30, 0.08),
        "detune": 0.0015,
    },
    "horror_suspense": {
        "chords": [
            (69.30, 138.59, 196.00, 207.65),   # C# dark tritone/m2
            (65.41, 130.81, 185.00, 196.00),   # C diminished tension
            (61.74, 123.47, 174.61, 185.00),   # B tension drone
            (65.41, 130.81, 155.56, 207.65),   # Cm(b6)
        ],
        "lfo_hz": 0.55,
        "pulse_depth": 0.28,
        "harmonics": (1.0, 0.38, 0.22),
        "detune": 0.0055,
    },
    "emotional_piano": {
        "chords": [
            (110.00, 220.00, 261.63, 329.63),  # Am
            (87.31, 174.61, 220.00, 261.63),   # F
            (130.81, 196.00, 261.63, 329.63),  # C
            (98.00, 196.00, 246.94, 293.66),   # G
        ],
        "lfo_hz": 0.40,
        "pulse_depth": 0.15,
        "harmonics": (1.0, 0.28, 0.07),
        "detune": 0.0018,
    },
}


class BGMPreset(BaseModel):
    """Metadata describing a curated background music mood preset."""

    id: str
    name: str
    mood: str
    bpm: int
    duration_seconds: int
    description: str
    tags: list[str] = Field(default_factory=list)


_PRESETS: tuple[BGMPreset, ...] = (
    BGMPreset(
        id="lofi_chill",
        name="Lo-Fi Chill",
        mood="Chill & Nostalgic",
        bpm=75,
        duration_seconds=15,
        description="Warm jazzy electric-piano chords with a relaxed lo-fi atmosphere for storytelling.",
        tags=["lofi", "chill", "storytelling", "study", "vlog"],
    ),
    BGMPreset(
        id="cinematic_epic",
        name="Cinematic Epic",
        mood="Dramatic & Inspiring",
        bpm=90,
        duration_seconds=15,
        description="Deep atmospheric sub-pads and wide fifth harmonies built for trailers and documentaries.",
        tags=["cinematic", "epic", "documentary", "trailer", "dramatic"],
    ),
    BGMPreset(
        id="upbeat_tech",
        name="Upbeat Tech",
        mood="Energetic & Modern",
        bpm=120,
        duration_seconds=15,
        description="Bright rhythmic synth pulse and crisp modern chords for tech reviews and product shorts.",
        tags=["tech", "upbeat", "modern", "startup", "product"],
    ),
    BGMPreset(
        id="corporate_clean",
        name="Corporate Clean",
        mood="Confident & Clear",
        bpm=105,
        duration_seconds=15,
        description="Clean, unobtrusive uplifting pad designed to sit smoothly beneath explainer voiceovers.",
        tags=["corporate", "explainer", "business", "clean", "tutorial"],
    ),
    BGMPreset(
        id="horror_suspense",
        name="Horror Suspense",
        mood="Dark & Tense",
        bpm=65,
        duration_seconds=15,
        description="Eerie detuned drones and minor tension intervals for mystery, crime, and thriller shorts.",
        tags=["horror", "mystery", "suspense", "dark", "thriller"],
    ),
    BGMPreset(
        id="emotional_piano",
        name="Emotional Piano",
        mood="Warm & Heartfelt",
        bpm=72,
        duration_seconds=15,
        description="Gentle harmonic progressions with expressive warmth for inspirational and emotional stories.",
        tags=["emotional", "piano", "warm", "inspirational", "drama"],
    ),
)

_PRESET_MAP: dict[str, BGMPreset] = {preset.id: preset for preset in _PRESETS}


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw_path = f"//{parsed.netloc}{raw_path}"
    return Path(raw_path)


def _synthesize_ambient_wav(preset_id: str, output_path: Path, duration_seconds: int) -> Path:
    """Synthesize a warm, deterministic multi-harmonic ambient pad WAV using pure Python."""
    recipe = _MOOD_RECIPES[preset_id]
    chords: list[tuple[float, ...]] = recipe["chords"]
    lfo_hz: float = float(recipe["lfo_hz"])
    pulse_depth: float = float(recipe["pulse_depth"])
    h1, h2, h3 = recipe["harmonics"]
    detune: float = float(recipe["detune"])

    sample_rate = 22050
    dur = max(1, int(duration_seconds))
    total_frames = sample_rate * dur
    num_chords = len(chords)
    chord_frames = max(1, total_frames // num_chords)
    fade_edge_frames = min(total_frames // 10, int(sample_rate * 0.25))
    crossfade_frames = min(chord_frames // 4, int(sample_rate * 0.35))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp.wav")

    two_pi = 2.0 * math.pi
    inv_sr = 1.0 / sample_rate
    norm_chord = 1.0 / max(1, len(chords[0]))
    norm_harm = 1.0 / max(0.01, h1 + h2 + h3)
    master_amp = 13500.0  # Comfortable headroom (~-7.7 dBFS) to avoid clipping

    # Precompute stereo phase increments for each chord's voices
    chord_inc_l: list[list[float]] = []
    chord_inc_r: list[list[float]] = []
    for chord in chords:
        chord_inc_l.append([two_pi * (f * (1.0 - detune)) * inv_sr for f in chord])
        chord_inc_r.append([two_pi * (f * (1.0 + detune)) * inv_sr for f in chord])

    sin = math.sin
    pack_into = struct.pack_into
    buf = bytearray(total_frames * 4)  # 2 channels * 2 bytes (int16)

    lfo_inc = two_pi * lfo_hz * inv_sr

    for i in range(total_frames):
        chord_idx = min(num_chords - 1, i // chord_frames)
        pos_in_chord = i - chord_idx * chord_frames
        inc_l = chord_inc_l[chord_idx]
        inc_r = chord_inc_r[chord_idx]

        # Base chord sample (multi-harmonic soft pad)
        val_l = 0.0
        val_r = 0.0
        for fl, fr in zip(inc_l, inc_r):
            pl = fl * i
            pr = fr * i
            val_l += h1 * sin(pl) + h2 * sin(2.0 * pl) + h3 * sin(3.0 * pl)
            val_r += h1 * sin(pr) + h2 * sin(2.0 * pr) + h3 * sin(3.0 * pr)

        val_l *= norm_chord * norm_harm
        val_r *= norm_chord * norm_harm

        # Smooth crossfade from previous chord at chord boundary
        if crossfade_frames > 0 and pos_in_chord < crossfade_frames and chord_idx > 0:
            alpha = pos_in_chord / crossfade_frames
            prev_l = 0.0
            prev_r = 0.0
            for fl, fr in zip(chord_inc_l[chord_idx - 1], chord_inc_r[chord_idx - 1]):
                pl = fl * i
                pr = fr * i
                prev_l += h1 * sin(pl) + h2 * sin(2.0 * pl) + h3 * sin(3.0 * pl)
                prev_r += h1 * sin(pr) + h2 * sin(2.0 * pr) + h3 * sin(3.0 * pr)
            prev_l *= norm_chord * norm_harm
            prev_r *= norm_chord * norm_harm
            val_l = prev_l * (1.0 - alpha) + val_l * alpha
            val_r = prev_r * (1.0 - alpha) + val_r * alpha

        # Gentle LFO breathing + loop-safe fade-in/fade-out
        lfo = 1.0 - pulse_depth * 0.5 * (1.0 - sin(lfo_inc * i))
        env = lfo
        if fade_edge_frames > 0:
            if i < fade_edge_frames:
                env *= i / fade_edge_frames
            elif i >= total_frames - fade_edge_frames:
                env *= (total_frames - 1 - i) / fade_edge_frames

        s_l = int(max(-32767, min(32767, val_l * env * master_amp)))
        s_r = int(max(-32767, min(32767, val_r * env * master_amp)))
        pack_into("<hh", buf, i * 4, s_l, s_r)

    with wave.open(str(tmp_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(buf))

    os.replace(tmp_path, output_path)
    return output_path


class BGMLibraryService:
    """Provides curated ambient BGM presets and resolves custom/preset BGM tracks."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def list_presets(self) -> list[BGMPreset]:
        """Return all curated BGM mood presets."""
        return [preset.model_copy() for preset in _PRESETS]

    def get_preset(self, preset_id: str) -> BGMPreset:
        """Look up a curated BGM preset by ID or raise ``NotFoundError``."""
        key = (preset_id or "").strip().lower()
        preset = _PRESET_MAP.get(key)
        if preset is None:
            raise NotFoundError(
                f"Unknown BGM preset '{preset_id}'",
                details={"preset_id": preset_id, "available": [p.id for p in _PRESETS]},
            )
        return preset.model_copy()

    def ensure_preset_audio(self, preset_id: str, duration_seconds: int = 15) -> Path:
        """Generate (if not cached) and return a deterministic WAV file for ``preset_id``."""
        preset = self.get_preset(preset_id)
        dur = max(1, int(duration_seconds))
        presets_dir = self.settings.temp_path / "bgm_presets"
        target_path = presets_dir / f"{preset.id}.wav"

        if target_path.is_file() and target_path.stat().st_size > 44:
            try:
                with wave.open(str(target_path), "rb") as wf:
                    rate = wf.getframerate() or 22050
                    cached_dur = wf.getnframes() / float(rate)
                if abs(cached_dur - dur) < 0.25:
                    return target_path
            except Exception:
                pass

        logger.info("Synthesizing offline BGM preset '%s' (%ds) -> %s", preset.id, dur, target_path)
        return _synthesize_ambient_wav(preset.id, target_path, dur)

    def _resolve_local_media_url(self, raw_ref: str) -> Path | None:
        """Resolve ``/local-media/...`` URLs into local paths under ``temp_path`` or ``local_storage``."""
        clean = raw_ref.strip()
        if not clean.startswith("/local-media/"):
            return None
        rel = clean[len("/local-media/") :].lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return None

        temp_root = self.settings.temp_path.resolve()
        candidate = (temp_root / rel).resolve()
        try:
            candidate.relative_to(temp_root)
            if candidate.is_file():
                return candidate
        except ValueError:
            pass

        # Support LocalStorageProvider URL prefix (/local-media/storage/<remote_key>)
        if rel.startswith("storage/"):
            storage_rel = rel[len("storage/") :]
            storage_root = (self.settings.temp_path.parent / "local_storage").resolve()
            storage_candidate = (storage_root / storage_rel).resolve()
            try:
                storage_candidate.relative_to(storage_root)
                if storage_candidate.is_file():
                    return storage_candidate
            except ValueError:
                pass

        return None

    async def _download_bgm_http(self, url: str, work_dir: Path) -> Path:
        parsed_ext = Path(urlparse(url).path).suffix.lower()
        ext = parsed_ext if parsed_ext in _AUDIO_EXTENSIONS else ".mp3"
        dest = work_dir / f"bgm_custom{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)

        if not dest.is_file() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded empty BGM file from '{url}'")
        return dest

    async def _stage_custom_ref(
        self,
        raw_ref: str,
        work_dir: Path,
        storage: BaseStorageProvider | None,
    ) -> Path | None:
        ref = raw_ref.strip()
        if not ref:
            return None

        work_dir.mkdir(parents=True, exist_ok=True)

        if ref.startswith("/local-media/"):
            local_candidate = self._resolve_local_media_url(ref)
            if local_candidate is not None and local_candidate.is_file():
                ext = local_candidate.suffix.lower() or ".wav"
                dest = work_dir / f"bgm_custom{ext}"
                if local_candidate.resolve() != dest.resolve():
                    await asyncio.to_thread(shutil.copy2, local_candidate, dest)
                return dest
            # Fallback: try stripping leading prefix and resolving via storage
            ref = ref[len("/local-media/") :].lstrip("/")
            if ref.startswith("storage/"):
                ref = ref[len("storage/") :]

        if ref.startswith("file://"):
            local_path = _file_uri_to_path(ref)
            if local_path.is_file():
                ext = local_path.suffix.lower() or ".wav"
                dest = work_dir / f"bgm_custom{ext}"
                if local_path.resolve() != dest.resolve():
                    await asyncio.to_thread(shutil.copy2, local_path, dest)
                return dest
            return None

        if ref.startswith(("http://", "https://")):
            return await self._download_bgm_http(ref, work_dir)

        direct = Path(ref)
        if direct.is_file():
            ext = direct.suffix.lower() or ".wav"
            dest = work_dir / f"bgm_custom{ext}"
            if direct.resolve() != dest.resolve():
                await asyncio.to_thread(shutil.copy2, direct, dest)
            return dest

        # Check TEMP_DIR/bgm_uploads or TEMP_DIR/<ref>
        for base in (self.settings.temp_path, self.settings.temp_path / "bgm_uploads"):
            base_resolved = base.resolve()
            candidate = (base_resolved / ref).resolve()
            try:
                candidate.relative_to(base_resolved)
                if candidate.is_file():
                    ext = candidate.suffix.lower() or ".wav"
                    dest = work_dir / f"bgm_custom{ext}"
                    if candidate.resolve() != dest.resolve():
                        await asyncio.to_thread(shutil.copy2, candidate, dest)
                    return dest
            except ValueError:
                pass

        if storage is not None:
            signed = await storage.get_signed_url(ref)
            if signed.startswith("/local-media/"):
                local_candidate = self._resolve_local_media_url(signed)
                if local_candidate is not None and local_candidate.is_file():
                    ext = local_candidate.suffix.lower() or ".wav"
                    dest = work_dir / f"bgm_custom{ext}"
                    if local_candidate.resolve() != dest.resolve():
                        await asyncio.to_thread(shutil.copy2, local_candidate, dest)
                    return dest
            elif signed.startswith("file://"):
                local_path = _file_uri_to_path(signed)
                if local_path.is_file():
                    ext = local_path.suffix.lower() or ".wav"
                    dest = work_dir / f"bgm_custom{ext}"
                    if local_path.resolve() != dest.resolve():
                        await asyncio.to_thread(shutil.copy2, local_path, dest)
                    return dest
            elif signed.startswith(("http://", "https://")):
                return await self._download_bgm_http(signed, work_dir)
            else:
                candidate = Path(signed)
                if candidate.is_file():
                    ext = candidate.suffix.lower() or ".wav"
                    dest = work_dir / f"bgm_custom{ext}"
                    if candidate.resolve() != dest.resolve():
                        await asyncio.to_thread(shutil.copy2, candidate, dest)
                    return dest

        return None

    async def resolve_bgm_path(
        self,
        pipeline_config: dict[str, Any],
        source_inputs: dict[str, Any],
        work_dir: Path,
        storage: BaseStorageProvider | None = None,
    ) -> Path | None:
        """Resolve ``bgm_file_key``, ``bgm_path``, ``bgm_url``, or ``bgm_preset_id`` into ``work_dir``."""
        p_cfg = pipeline_config or {}
        s_inp = source_inputs or {}
        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        custom_refs = [
            p_cfg.get("bgm_file_key"),
            s_inp.get("bgm_file_key"),
            p_cfg.get("bgm_path"),
            s_inp.get("bgm_path"),
            p_cfg.get("bgm_url"),
            s_inp.get("bgm_url"),
        ]
        for raw_ref in custom_refs:
            ref = str(raw_ref or "").strip()
            if not ref:
                continue
            try:
                resolved = await self._stage_custom_ref(ref, dest_dir, storage)
                if resolved is not None and resolved.is_file():
                    logger.info("Resolved custom BGM '%s' -> %s", ref, resolved)
                    return resolved
                logger.warning("Custom BGM '%s' could not be located", ref)
            except Exception as exc:
                logger.warning("Failed to resolve custom BGM '%s': %s", ref, exc)

        preset_id = str(
            p_cfg.get("bgm_preset_id") or s_inp.get("bgm_preset_id") or ""
        ).strip().lower()
        if preset_id and preset_id != "none":
            try:
                preset_wav = await asyncio.to_thread(self.ensure_preset_audio, preset_id, 15)
                staged_preset = dest_dir / f"bgm_preset_{preset_id}.wav"
                if preset_wav.resolve() != staged_preset.resolve():
                    await asyncio.to_thread(shutil.copy2, preset_wav, staged_preset)
                logger.info("Resolved BGM preset '%s' -> %s", preset_id, staged_preset)
                return staged_preset
            except Exception as exc:
                logger.warning("Failed to resolve BGM preset '%s': %s", preset_id, exc)

        return None
