"""ProviderFactory registry mapping domain keys to lazy-loaded provider adapters."""

from __future__ import annotations

import importlib
import inspect
from enum import Enum
from typing import TypeVar

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError
from backend.core.logging import get_logger
from backend.models.enums import (
    LLMProviderName,
    SeparationProviderName,
    StorageProviderName,
    STTProviderName,
    TTSProviderName,
    VisualProviderName,
)
from backend.providers.base.llm import BaseLLMProvider
from backend.providers.base.separation import BaseSeparationProvider
from backend.providers.base.storage import BaseStorageProvider
from backend.providers.base.stt import BaseSTTProvider
from backend.providers.base.tts import BaseTTSProvider
from backend.providers.base.visual import BaseVisualProvider

logger = get_logger(__name__)

TProvider = TypeVar("TProvider")
RegistryMap = dict[str, tuple[str, str]]


class ProviderFactory:
    """Factory & registry for resolving pluggable AI and storage providers.

    Concrete adapter modules are imported lazily on demand so that importing
    the factory never triggers circular imports or fails on optional SDKs.
    """

    _STT_REGISTRY: RegistryMap = {
        STTProviderName.GROQ.value: (
            "backend.providers.stt.groq_whisper",
            "GroqWhisperProvider",
        ),
        STTProviderName.FASTER_WHISPER.value: (
            "backend.providers.stt.faster_whisper",
            "FasterWhisperProvider",
        ),
    }

    _TTS_REGISTRY: RegistryMap = {
        TTSProviderName.EDGE_TTS.value: (
            "backend.providers.tts.edge_tts",
            "EdgeTTSProvider",
        ),
        TTSProviderName.ELEVENLABS.value: (
            "backend.providers.tts.elevenlabs",
            "ElevenLabsProvider",
        ),
        TTSProviderName.FPT_AI.value: (
            "backend.providers.tts.vietnam_tts",
            "FptAiTTSProvider",
        ),
        TTSProviderName.VIETTEL_AI.value: (
            "backend.providers.tts.vietnam_tts",
            "ViettelAiTTSProvider",
        ),
    }

    _LLM_REGISTRY: RegistryMap = {
        LLMProviderName.GEMINI.value: (
            "backend.providers.llm.gemini",
            "GeminiProvider",
        ),
    }

    _VISUAL_REGISTRY: RegistryMap = {
        VisualProviderName.PEXELS.value: (
            "backend.providers.visual.pexels",
            "PexelsProvider",
        ),
        VisualProviderName.PIXABAY.value: (
            "backend.providers.visual.pixabay",
            "PixabayProvider",
        ),
        VisualProviderName.FLUX.value: (
            "backend.providers.visual.flux_sd",
            "FluxVisualProvider",
        ),
        VisualProviderName.STABLE_DIFFUSION.value: (
            "backend.providers.visual.flux_sd",
            "StableDiffusionProvider",
        ),
    }

    _SEPARATION_REGISTRY: RegistryMap = {
        SeparationProviderName.FAST_MODE.value: (
            "backend.providers.separation.fast_ffmpeg",
            "FastFFmpegSeparationProvider",
        ),
        SeparationProviderName.REPLICATE.value: (
            "backend.providers.separation.replicate_demucs",
            "ReplicateDemucsProvider",
        ),
        SeparationProviderName.LOCAL_DEMUCS.value: (
            "backend.providers.separation.replicate_demucs",
            "LocalDemucsProvider",
        ),
    }

    _STORAGE_REGISTRY: RegistryMap = {
        StorageProviderName.SUPABASE.value: (
            "backend.providers.storage.supabase_storage",
            "SupabaseStorageProvider",
        ),
        StorageProviderName.LOCAL.value: (
            "backend.providers.storage.local_storage",
            "LocalStorageProvider",
        ),
    }

    _PLACEHOLDER_SUPABASE_URLS: frozenset[str] = frozenset(
        {"", "https://your-project-ref.supabase.co"}
    )

    @classmethod
    def _normalize_key(cls, raw_key: str | Enum | None, fallback: str) -> str:
        if raw_key is None:
            candidate = fallback
        elif isinstance(raw_key, Enum):
            candidate = str(raw_key.value)
        else:
            candidate = str(raw_key)
        normalized = candidate.strip().lower()
        return normalized or fallback.strip().lower()

    @classmethod
    def _resolve_provider(
        cls,
        domain: str,
        registry: RegistryMap,
        base_cls: type[TProvider],
        override: str | Enum | None,
        default_key: str,
        settings: Settings | None = None,
    ) -> TProvider:
        cfg = settings or get_settings()
        key = cls._normalize_key(override, default_key)
        entry = registry.get(key)
        if entry is None:
            valid = ", ".join(sorted(registry.keys()))
            raise ConfigurationError(
                f"Unknown {domain} provider '{key}'. Registered providers: [{valid}]",
                details={"domain": domain, "provider": key, "valid": sorted(registry.keys())},
            )

        module_path, class_name = entry
        try:
            module = importlib.import_module(module_path)
            provider_cls = getattr(module, class_name)
        except ImportError as exc:
            raise ConfigurationError(
                f"Failed to import {domain} provider '{key}' from {module_path}: {exc}",
                details={"domain": domain, "provider": key, "module": module_path},
            ) from exc
        except AttributeError as exc:
            raise ConfigurationError(
                f"Provider class '{class_name}' not found in {module_path}",
                details={"domain": domain, "provider": key, "class_name": class_name},
            ) from exc

        sig = inspect.signature(provider_cls.__init__)
        accepts_settings = "settings" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        instance = provider_cls(settings=cfg) if accepts_settings else provider_cls()

        if not isinstance(instance, base_cls):
            raise ConfigurationError(
                f"Provider '{class_name}' must inherit from {base_cls.__name__}",
                details={"domain": domain, "provider": key, "class_name": class_name},
            )

        logger.debug("Resolved %s provider '%s' (%s)", domain, key, class_name)
        return instance

    @classmethod
    def get_stt(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseSTTProvider:
        """Instantiate and return the configured or overridden STT provider."""
        cfg = settings or get_settings()
        return cls._resolve_provider(
            domain="stt",
            registry=cls._STT_REGISTRY,
            base_cls=BaseSTTProvider,
            override=override,
            default_key=cfg.STT_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def get_tts(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseTTSProvider:
        """Instantiate and return the configured or overridden TTS provider."""
        cfg = settings or get_settings()
        return cls._resolve_provider(
            domain="tts",
            registry=cls._TTS_REGISTRY,
            base_cls=BaseTTSProvider,
            override=override,
            default_key=cfg.TTS_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def get_llm(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseLLMProvider:
        """Instantiate and return the configured or overridden LLM provider."""
        cfg = settings or get_settings()
        return cls._resolve_provider(
            domain="llm",
            registry=cls._LLM_REGISTRY,
            base_cls=BaseLLMProvider,
            override=override,
            default_key=cfg.LLM_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def get_visual(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseVisualProvider:
        """Instantiate and return the configured or overridden Visual provider."""
        cfg = settings or get_settings()
        return cls._resolve_provider(
            domain="visual",
            registry=cls._VISUAL_REGISTRY,
            base_cls=BaseVisualProvider,
            override=override,
            default_key=cfg.VISUAL_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def get_separation(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseSeparationProvider:
        """Instantiate and return the configured or overridden Separation provider."""
        cfg = settings or get_settings()
        return cls._resolve_provider(
            domain="separation",
            registry=cls._SEPARATION_REGISTRY,
            base_cls=BaseSeparationProvider,
            override=override,
            default_key=cfg.SEPARATION_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def get_storage(
        cls,
        override: str | Enum | None = None,
        settings: Settings | None = None,
    ) -> BaseStorageProvider:
        """Instantiate and return the configured or overridden Storage provider."""
        cfg = settings or get_settings()
        effective_override = override
        if (
            effective_override is None
            and cfg.LOCAL_SANDBOX_MODE
            and (cfg.SUPABASE_URL or "").strip() in cls._PLACEHOLDER_SUPABASE_URLS
        ):
            effective_override = StorageProviderName.LOCAL.value
        return cls._resolve_provider(
            domain="storage",
            registry=cls._STORAGE_REGISTRY,
            base_cls=BaseStorageProvider,
            override=effective_override,
            default_key=cfg.STORAGE_PROVIDER,
            settings=cfg,
        )

    @classmethod
    def list_registered_providers(cls) -> dict[str, list[str]]:
        """Return all registered provider keys grouped by domain."""
        return {
            "stt": list(cls._STT_REGISTRY.keys()),
            "tts": list(cls._TTS_REGISTRY.keys()),
            "llm": list(cls._LLM_REGISTRY.keys()),
            "visual": list(cls._VISUAL_REGISTRY.keys()),
            "separation": list(cls._SEPARATION_REGISTRY.keys()),
            "storage": list(cls._STORAGE_REGISTRY.keys()),
        }

    @classmethod
    def get_default_providers(cls, settings: Settings | None = None) -> dict[str, str]:
        """Return the active default provider key per domain from Settings."""
        cfg = settings or get_settings()
        return {
            "stt": cfg.STT_PROVIDER,
            "tts": cfg.TTS_PROVIDER,
            "llm": cfg.LLM_PROVIDER,
            "visual": cfg.VISUAL_PROVIDER,
            "separation": cfg.SEPARATION_PROVIDER,
            "storage": cfg.STORAGE_PROVIDER,
        }
