"""Fast Mode audio separation provider using lightweight FFmpeg extraction."""

from __future__ import annotations

from pathlib import Path

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError, ProviderError
from backend.core.logging import get_logger
from backend.core.subprocess_utils import run_subprocess_exec
from backend.models.provider import SeparationResult
from backend.providers.base.separation import BaseSeparationProvider

logger = get_logger(__name__)

_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".wma"}
)


class FastFFmpegSeparationProvider(BaseSeparationProvider):
    """Fast Mode adapter that skips Demucs and extracts/passes through source audio."""

    provider_name: str = "fast_mode"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._ffmpeg_bin = self._settings.FFMPEG_BIN or "ffmpeg"
        self._ffprobe_bin = self._settings.FFPROBE_BIN or "ffprobe"

    async def _verify_audio_stream(self, media_path: Path) -> bool:
        """Return True if media_path has an audio stream (or if ffprobe is unavailable)."""
        try:
            returncode, stdout, _ = await run_subprocess_exec(
                self._ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(media_path),
            )
            if returncode != 0:
                return False
            return "audio" in stdout.decode("utf-8", errors="ignore").lower()
        except FileNotFoundError:
            # Allow passthrough if ffprobe binary is not present on host yet.
            return True

    async def _extract_audio_wav(self, video_path: Path, output_wav: Path) -> Path:
        """Extract stereo 44.1kHz WAV audio track from video via FFmpeg."""
        try:
            returncode, _, stderr = await run_subprocess_exec(
                self._ffmpeg_bin,
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(output_wav),
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"FFmpeg binary '{self._ffmpeg_bin}' not found; install FFmpeg or set FFMPEG_BIN"
            ) from exc

        if returncode != 0:
            err_msg = stderr.decode("utf-8", errors="ignore").strip()[-400:]
            raise ProviderError(
                self.provider_name,
                f"FFmpeg audio extraction failed (exit {returncode}): {err_msg}",
            )

        if not output_wav.exists() or output_wav.stat().st_size == 0:
            raise ProviderError(
                self.provider_name,
                "FFmpeg extracted an empty audio file",
            )
        return output_wav

    async def separate(self, media_path: Path, work_dir: Path) -> SeparationResult:
        """Extract/verify source audio track and reuse it as both vocals and BGM."""
        src_path = Path(media_path)
        if not src_path.exists():
            raise ProviderError(self.provider_name, f"Input media file not found: {src_path}")

        dest_dir = Path(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Running FastFFmpegSeparationProvider on %s", src_path.name)

        try:
            if src_path.suffix.lower() in _AUDIO_EXTENSIONS:
                if src_path.stat().st_size == 0:
                    raise ProviderError(self.provider_name, f"Audio file is empty: {src_path}")
                has_audio = await self._verify_audio_stream(src_path)
                if not has_audio:
                    raise ProviderError(
                        self.provider_name,
                        f"No valid audio stream found in {src_path.name}",
                    )
                audio_path = src_path
            else:
                has_audio = await self._verify_audio_stream(src_path)
                if not has_audio:
                    raise ProviderError(
                        self.provider_name,
                        f"Input video contains no audio track: {src_path.name}",
                    )
                audio_path = await self._extract_audio_wav(
                    src_path,
                    dest_dir / "source_audio.wav",
                )

            return SeparationResult(
                vocals_path=audio_path,
                bgm_path=audio_path,
                source_audio_path=audio_path,
                mode=self.provider_name,
            )
        except (ConfigurationError, ProviderError):
            raise
        except Exception as exc:
            logger.exception("Unexpected error in FastFFmpegSeparationProvider.separate")
            raise ProviderError(
                self.provider_name,
                f"Fast audio separation failed: {exc}",
            ) from exc
