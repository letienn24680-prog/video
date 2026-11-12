"""Abstract Base Class for Visual (stock media & AI image generation) providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from backend.models.enums import AspectRatio
from backend.models.provider import VisualAsset


class BaseVisualProvider(ABC):
    """Contract that all stock footage and AI image provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def fetch_visual(
        self,
        query: str,
        prompt: str | None,
        aspect_ratio: AspectRatio,
        media_type: Literal["video", "image"] = "video",
        output_dir: Path | None = None,
    ) -> VisualAsset:
        """Fetch or generate a visual asset matching the requested aspect ratio."""
        raise NotImplementedError
