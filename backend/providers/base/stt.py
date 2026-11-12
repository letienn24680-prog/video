"""Abstract Base Class for Speech-to-Text (STT) providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from backend.models.provider import TranscriptResult


class BaseSTTProvider(ABC):
    """Contract that all STT provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        word_timestamps: bool = True,
    ) -> TranscriptResult:
        """Transcribe an audio file into normalized segments and word timestamps."""
        raise NotImplementedError
