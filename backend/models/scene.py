"""Pydantic v2 schemas matching public.project_scenes and HITL review edits."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.enums import TTSProviderName, VisualSource
from backend.models.provider import WordTimestamp


class SceneBase(BaseModel):
    """Shared timeline fields for translator lines and faceless storyboard scenes."""

    scene_index: int = Field(..., ge=0)
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    original_text: str | None = None
    edited_text: str | None = None
    narration_text: str | None = None
    word_timestamps: list[WordTimestamp] = Field(default_factory=list)
    search_keywords: str | None = None
    image_prompt: str | None = None
    emotion_tone: str | None = None
    speaker_label: str | None = None
    voice_id: str | None = None
    tts_rate_pct: int = Field(default=0, ge=-50, le=100)
    audio_url: str | None = None
    visual_url: str | None = None
    visual_source: VisualSource | None = None
    subtitle_style: dict[str, Any] = Field(default_factory=dict)
    is_locked: bool = False

    @model_validator(mode="after")
    def _validate_time_range(self) -> SceneBase:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class SceneCreate(SceneBase):
    """Payload for inserting or upserting a scene in public.project_scenes."""

    project_id: UUID | None = None


class SceneUpdateRequest(BaseModel):
    """Partial update payload for HITL review editing of a scene/subtitle."""

    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    edited_text: str | None = None
    narration_text: str | None = None
    search_keywords: str | None = None
    image_prompt: str | None = None
    emotion_tone: str | None = None
    speaker_label: str | None = None
    voice_id: str | None = None
    tts_rate_pct: int | None = Field(default=None, ge=-50, le=100)
    audio_url: str | None = None
    visual_url: str | None = None
    visual_source: VisualSource | None = None
    subtitle_style: dict[str, Any] | None = None
    is_locked: bool | None = None

    @model_validator(mode="after")
    def _validate_time_range(self) -> SceneUpdateRequest:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class PreviewTTSRequest(BaseModel):
    """Request payload for HITL single-line/scene TTS preview."""

    text: str | None = Field(default=None, min_length=1, max_length=2000)
    voice_id: str | None = None
    tts_provider: TTSProviderName | None = None
    tts_rate_pct: int = Field(default=0, ge=-50, le=100)


class SceneAppendRequest(BaseModel):
    """Payload for appending a new scene/subtitle line during AWAITING_REVIEW."""

    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=3000, ge=0)
    edited_text: str | None = None
    narration_text: str | None = None
    original_text: str | None = None
    search_keywords: str | None = None
    speaker_label: str | None = None
    voice_id: str | None = None
    tts_rate_pct: int = Field(default=0, ge=-50, le=100)

    @model_validator(mode="after")
    def _validate_time_range(self) -> SceneAppendRequest:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class SceneResponse(SceneBase):
    """Database representation of a row in public.project_scenes."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_at: datetime
    updated_at: datetime
