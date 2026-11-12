"""Abstract Base Class for Text-to-Speech (TTS) providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from backend.models.provider import AudioSynthesisResult, VoiceOption


class BaseTTSProvider(ABC):
    """Contract that all TTS provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_path: Path,
        rate_pct: int = 0,
    ) -> AudioSynthesisResult:
        """Synthesize speech audio to ``output_path`` with optional speed adjustment."""
        raise NotImplementedError

    @abstractmethod
    async def list_voices(self, language: str | None = None) -> list[VoiceOption]:
        """Return available voices, optionally filtered by BCP-47/ISO language prefix."""
        raise NotImplementedError
