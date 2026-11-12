"""Abstract Base Class for Vocal / BGM audio separation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from backend.models.provider import SeparationResult


class BaseSeparationProvider(ABC):
    """Contract that all vocal/BGM separation provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def separate(self, media_path: Path, work_dir: Path) -> SeparationResult:
        """Extract or separate vocal and background music tracks into ``work_dir``."""
        raise NotImplementedError
