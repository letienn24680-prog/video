"""Pydantic v2 schemas matching public.projects and API project workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.enums import (
    AspectRatio,
    LLMProviderName,
    ProjectMode,
    ProjectStatus,
    SeparationProviderName,
    StorageProviderName,
    STTProviderName,
    TTSProviderName,
    VisualProviderName,
)
from backend.models.scene import SceneResponse


class SubtitleStyleConfig(BaseModel):
    """Karaoke / subtitle styling overrides stored inside pipeline_config."""

    model_config = ConfigDict(extra="allow")

    font_name: str = "Arial"
    font_size: int = Field(default=56, ge=12, le=160)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FACC15"
    outline_color: str = "#000000"
    outline_width: int = Field(default=3, ge=0, le=12)
    position: Literal["top", "middle", "bottom"] = "bottom"
    burn_subtitles: bool = True


class PipelineConfig(BaseModel):
    """Per-project provider overrides and pipeline rendering settings."""

    model_config = ConfigDict(extra="allow")

    # Provider Factory overrides (None => use .env defaults)
    separation_provider: SeparationProviderName | None = None
    stt_provider: STTProviderName | None = None
    llm_provider: LLMProviderName | None = None
    tts_provider: TTSProviderName | None = None
    visual_provider: VisualProviderName | None = None
    storage_provider: StorageProviderName | None = None

    # Language, translation, voice & audio mixing options
    source_lang: str | None = None
    target_lang: str = "vi"
    translation_style: str = "natural"
    tone: str = "viral"
    voice_id: str | None = None
    speaker_voices: dict[str, str] = Field(default_factory=dict)
    tts_rate_pct: int = Field(default=0, ge=-50, le=100)
    bgm_volume: float = Field(default=0.25, ge=0.0, le=1.0)
    burn_subtitles: bool = True
    subtitle_style: SubtitleStyleConfig = Field(default_factory=SubtitleStyleConfig)

    # Faceless storyboard & assembly options
    scene_duration_min: int = Field(default=3, ge=1, le=15)
    scene_duration_max: int = Field(default=5, ge=2, le=30)
    ken_burns_enabled: bool = True
    auto_ducking: bool = True

    @model_validator(mode="after")
    def _validate_scene_duration_bounds(self) -> PipelineConfig:
        if self.scene_duration_max < self.scene_duration_min:
            raise ValueError("scene_duration_max must be >= scene_duration_min")
        return self


class CreateTranslatorProjectRequest(BaseModel):
    """Request to create an AI Video Translator & Auto-Dubber project."""

    mode: Literal[ProjectMode.TRANSLATOR] = ProjectMode.TRANSLATOR
    title: str = Field(default="Untitled Translator Project", min_length=1, max_length=200)
    source_url: str | None = None
    source_file_key: str | None = None
    aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16
    pipeline_config: PipelineConfig = Field(default_factory=PipelineConfig)

    @model_validator(mode="after")
    def _validate_source_present(self) -> CreateTranslatorProjectRequest:
        has_url = bool(self.source_url and self.source_url.strip())
        has_file = bool(self.source_file_key and self.source_file_key.strip())
        if not has_url and not has_file:
            raise ValueError("Either source_url or source_file_key must be provided")
        return self


class CreateFacelessProjectRequest(BaseModel):
    """Request to create an AI Faceless Video Generator project."""

    mode: Literal[ProjectMode.FACELESS] = ProjectMode.FACELESS
    title: str = Field(default="Untitled Faceless Project", min_length=1, max_length=200)
    source_mode: Literal["topic", "script", "article_url"] = "topic"
    topic: str | None = None
    script: str | None = None
    article_url: str | None = None
    aspect_ratio: AspectRatio = AspectRatio.PORTRAIT_9_16
    target_duration_seconds: int | None = Field(default=None, ge=5, le=600)
    pipeline_config: PipelineConfig = Field(default_factory=PipelineConfig)

    @model_validator(mode="after")
    def _validate_content_present(self) -> CreateFacelessProjectRequest:
        mapping = {
            "topic": self.topic,
            "script": self.script,
            "article_url": self.article_url,
        }
        chosen = mapping.get(self.source_mode)
        if chosen and chosen.strip():
            return self
        if any(val and val.strip() for val in mapping.values()):
            return self
        raise ValueError(
            f"Content for source_mode='{self.source_mode}' (topic, script, or article_url) is required"
        )


class BatchProjectCreateRequest(BaseModel):
    """Batch creation payload validated against MAX_BATCH_SIZE at the API layer."""

    batch_title: str | None = Field(default=None, max_length=200)
    items: list[CreateTranslatorProjectRequest | CreateFacelessProjectRequest] = Field(
        ..., min_length=1
    )


class RenderConfirmRequest(BaseModel):
    """Payload sent when confirming AWAITING_REVIEW -> RENDERING."""

    pipeline_config: PipelineConfig | None = None
    burn_subtitles: bool | None = None
    voice_id: str | None = None
    speaker_voices: dict[str, str] | None = None


class ProjectUpdateRequest(BaseModel):
    """Partial update payload for renaming a project or updating its aspect ratio."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    aspect_ratio: AspectRatio | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class DuplicateProjectRequest(BaseModel):
    """Optional override payload when duplicating an existing project."""

    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_optional_title(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class ProjectResponse(BaseModel):
    """Database representation of a row in public.projects."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str
    mode: ProjectMode
    status: ProjectStatus
    aspect_ratio: AspectRatio
    source_mode: str | None = None
    source_inputs: dict[str, Any] = Field(default_factory=dict)
    pipeline_config: dict[str, Any] = Field(default_factory=dict)
    progress_pct: int = Field(default=0, ge=0, le=100)
    current_stage: str | None = None
    output_video_url: str | None = None
    duration_seconds: float | None = None
    credits_estimated: int = 0
    credits_charged: int = 0
    error_message: str | None = None
    batch_id: UUID | None = None
    temp_cleaned_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProjectDetailResponse(ProjectResponse):
    """Project response enriched with ordered project_scenes."""

    scenes: list[SceneResponse] = Field(default_factory=list)
