"""Abstract Base Class for Large Language Model (LLM) providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.models.enums import AspectRatio
from backend.models.provider import (
    StoryboardResult,
    TranscriptSegment,
    TranslatedSegment,
)


class BaseLLMProvider(ABC):
    """Contract that all LLM provider adapters must implement."""

    provider_name: str

    @abstractmethod
    async def translate_time_synced(
        self,
        segments: list[TranscriptSegment],
        target_lang: str,
        style_tone: str = "casual",
    ) -> list[TranslatedSegment]:
        """Translate transcript segments while matching original timing & syllable budget."""
        raise NotImplementedError

    @abstractmethod
    async def generate_storyboard(
        self,
        source_text: str,
        aspect_ratio: AspectRatio,
        scene_duration_target: tuple[int, int] = (3, 5),
        tone: str = "viral",
    ) -> StoryboardResult:
        """Generate a structured multi-scene storyboard from a topic, script, or article."""
        raise NotImplementedError
