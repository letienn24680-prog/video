"""Google Gemini LLM provider adapter using structured JSON outputs."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from backend.core.logging import get_logger
from backend.models.enums import AspectRatio
from backend.models.provider import (
    StoryboardResult,
    TranscriptSegment,
    TranslatedSegment,
)
from backend.providers.base.llm import BaseLLMProvider

logger = get_logger(__name__)

_PLACEHOLDER_KEYS = {"", "your_gemini_api_key_here"}
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ProviderRateLimitError) or (
        isinstance(exc, ProviderError) and exc.retryable
    )


def _strip_json_fences(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = _CODE_FENCE_RE.sub("", cleaned).strip()
    return cleaned


def _sanitize_gemini_schema(node: Any) -> Any:
    """Recursively strip JSON Schema fields unsupported by google.genai.types.Schema."""
    if isinstance(node, list):
        return [_sanitize_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    sanitized: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$schema", "additionalProperties", "exclusiveMinimum", "exclusiveMaximum"):
            continue
        sanitized[key] = _sanitize_gemini_schema(value)

    if "exclusiveMinimum" in node:
        val = node["exclusiveMinimum"]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and "minimum" not in sanitized:
            sanitized["minimum"] = 0.1 if float(val) == 0.0 else float(val)

    if "exclusiveMaximum" in node:
        val = node["exclusiveMaximum"]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and "maximum" not in sanitized:
            sanitized["maximum"] = float(val)

    return sanitized


def _build_gemini_response_schema(schema_cls: type[BaseModel]) -> dict[str, Any]:
    """Build a google-genai-compatible JSON schema dict from a Pydantic BaseModel."""
    result = _sanitize_gemini_schema(schema_cls.model_json_schema())
    return result if isinstance(result, dict) else {}


class _TranslationBatchSchema(BaseModel):
    """Structured JSON wrapper enforced on Gemini time-synced translations."""

    segments: list[TranslatedSegment] = Field(default_factory=list)


class GeminiProvider(BaseLLMProvider):
    """LLM adapter powered by ``google-genai`` (``gemini-2.5-flash`` default)."""

    provider_name = "gemini"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _validate_api_key(self) -> str:
        key = (self.settings.GEMINI_API_KEY or "").strip()
        if key in _PLACEHOLDER_KEYS:
            raise ConfigurationError(
                "GEMINI_API_KEY is missing or uses the default placeholder.",
                details={"provider": self.provider_name},
            )
        return key

    def _import_sdk(self) -> tuple[Any, Any]:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ConfigurationError(
                "The 'google-genai' package is not installed. Run: pip install google-genai>=1.0.0",
                details={"provider": self.provider_name},
            ) from exc
        return genai, types

    def _map_exception(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        message = str(exc) or type(exc).__name__
        lowered = message.lower()

        if status_code in (401, 403) or "api key" in lowered or "permission" in lowered:
            return ProviderAuthError(self.provider_name, message)
        if status_code == 429 or "resource_exhausted" in lowered or "quota" in lowered or "rate" in lowered:
            return ProviderRateLimitError(self.provider_name, message)

        retryable = (
            status_code in (408, 500, 502, 503, 504)
            or "timeout" in lowered
            or "unavailable" in lowered
            or "connection" in lowered
        )
        return ProviderError(self.provider_name, message, retryable=retryable)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _generate_structured(
        self,
        prompt: str,
        schema_cls: type[BaseModel],
        system_instruction: str,
    ) -> BaseModel:
        api_key = self._validate_api_key()
        genai, types = self._import_sdk()
        client = genai.Client(api_key=api_key)

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=_build_gemini_response_schema(schema_cls),
            temperature=0.4,
        )

        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.settings.GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            mapped = self._map_exception(exc)
            logger.warning(
                "Gemini request failed (retryable=%s): %s",
                mapped.retryable,
                mapped.message,
            )
            raise mapped from exc

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema_cls):
            return parsed
        if isinstance(parsed, dict):
            try:
                return schema_cls.model_validate(parsed)
            except ValidationError as exc:
                raise ProviderError(
                    self.provider_name,
                    f"Structured output validation failed: {exc}",
                    retryable=True,
                ) from exc

        raw_text = getattr(response, "text", None) or ""
        if not raw_text.strip():
            raise ProviderError(
                self.provider_name,
                "Gemini returned an empty response payload.",
                retryable=True,
            )

        cleaned = _strip_json_fences(raw_text)
        try:
            return schema_cls.model_validate_json(cleaned)
        except (ValidationError, ValueError) as exc:
            logger.warning("Gemini JSON validation failed; retrying: %s", exc)
            raise ProviderError(
                self.provider_name,
                f"Invalid structured JSON output from Gemini: {exc}",
                retryable=True,
            ) from exc

    async def translate_time_synced(
        self,
        segments: list[TranscriptSegment],
        target_lang: str,
        style_tone: str = "casual",
    ) -> list[TranslatedSegment]:
        """Translate transcript segments with syllable-length awareness for dubbing sync."""
        if not segments:
            return []

        payload = []
        segment_by_idx: dict[int, TranscriptSegment] = {}
        syllable_targets: dict[int, int] = {}

        for seg in segments:
            duration_ms = max(200, seg.end_ms - seg.start_ms)
            est_syllables = max(2, round((duration_ms / 1000.0) * 4.5))
            segment_by_idx[seg.index] = seg
            syllable_targets[seg.index] = est_syllables
            payload.append(
                {
                    "index": seg.index,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "duration_ms": duration_ms,
                    "target_syllables": est_syllables,
                    "speaker_label": seg.speaker_label,
                    "text": seg.text,
                }
            )

        system_instruction = (
            "You are an expert audiovisual localization and dubbing translator. "
            "Translate dialogue naturally into the target language while strictly matching "
            "syllable budget and spoken duration so voiceover fits the original timing window "
            "without sounding rushed. Preserve all segment indices and timestamps exactly."
        )
        prompt = (
            f"Target language: {target_lang}\n"
            f"Style/Tone: {style_tone}\n\n"
            "Rules:\n"
            "1. Return one entry in `segments` for EVERY input segment with matching `index`, "
            "`start_ms`, `end_ms`, `original_text`, and `speaker_label`.\n"
            "2. Keep `translated_text` concise and speakable within ~`target_syllables` "
            "(within +-20% of target_syllables) so dubbing fits `duration_ms`.\n"
            "3. Maintain cross-segment narrative context, pronouns, and consistent terminology.\n\n"
            f"Input segments JSON:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        logger.info(
            "Requesting Gemini time-synced translation (%d segments -> %s, tone=%s)",
            len(segments),
            target_lang,
            style_tone,
        )
        validated = await self._generate_structured(
            prompt=prompt,
            schema_cls=_TranslationBatchSchema,
            system_instruction=system_instruction,
        )
        assert isinstance(validated, _TranslationBatchSchema)

        # Normalize against source segments to guarantee exact index/timestamp integrity.
        by_index = {item.index: item for item in validated.segments}
        results: list[TranslatedSegment] = []
        for pos, src in enumerate(segments):
            item = by_index.get(src.index)
            if item is None and pos < len(validated.segments):
                item = validated.segments[pos]
            if item is None:
                raise ProviderError(
                    self.provider_name,
                    f"Missing translated segment for index {src.index}",
                    retryable=True,
                )
            results.append(
                TranslatedSegment(
                    index=src.index,
                    start_ms=src.start_ms,
                    end_ms=src.end_ms,
                    original_text=src.text,
                    translated_text=item.translated_text.strip(),
                    target_syllables=item.target_syllables or syllable_targets[src.index],
                    speaker_label=src.speaker_label,
                )
            )
        return results

    async def generate_storyboard(
        self,
        source_text: str,
        aspect_ratio: AspectRatio,
        scene_duration_target: tuple[int, int] = (3, 5),
        tone: str = "viral",
    ) -> StoryboardResult:
        """Generate a 3-5s scene-by-scene structured storyboard for Faceless videos."""
        min_dur, max_dur = scene_duration_target
        system_instruction = (
            "You are an elite short-form & faceless video director. "
            "Break topics, scripts, or articles into punchy visual scenes with tight pacing, "
            "clear English stock-search keywords, and rich AI image prompts."
        )
        prompt = (
            f"Aspect ratio: {aspect_ratio.value}\n"
            f"Tone: {tone}\n"
            f"Target duration per scene: {min_dur} to {max_dur} seconds\n\n"
            "Requirements:\n"
            "1. Create a compelling `title` and sequential `scenes` starting at `scene_index=0`.\n"
            f"2. Each scene's `duration_seconds` should be between {min_dur}.0 and {max_dur}.0 "
            "and proportional to its `narration_text` length.\n"
            "3. `search_keywords` must be concise English keywords suitable for Pexels/Pixabay.\n"
            f"4. `image_prompt` must describe a vivid visual composition framed for {aspect_ratio.value}.\n"
            "5. `emotion_tone` should be a concise mood label (e.g., curious, dramatic, upbeat).\n\n"
            f"Source content:\n{source_text}"
        )

        logger.info(
            "Requesting Gemini storyboard generation (aspect_ratio=%s, tone=%s)",
            aspect_ratio.value,
            tone,
        )
        validated = await self._generate_structured(
            prompt=prompt,
            schema_cls=StoryboardResult,
            system_instruction=system_instruction,
        )
        assert isinstance(validated, StoryboardResult)

        if validated.scenes and validated.total_duration_seconds <= 0:
            validated.total_duration_seconds = round(
                sum(scene.duration_seconds for scene in validated.scenes),
                2,
            )
        return validated
