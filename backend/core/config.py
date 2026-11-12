"""Application configuration loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings matching .env.example."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 1. Application, Server & Logging
    APP_NAME: str = "AI Video Studio"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "change-me-to-a-long-random-hex-string"
    FRONTEND_URL: str = "http://localhost:8000"
    CORS_ORIGINS: str = "http://localhost:8000,http://127.0.0.1:8000"

    # 2. Concurrency, Batching & Auto-Cleanup TTL
    MAX_CONCURRENT_JOBS: int = Field(default=3, ge=1)
    MAX_BATCH_SIZE: int = Field(default=10, ge=1)
    JOB_TIMEOUT_SECONDS: int = Field(default=900, ge=30)
    MAX_UPLOAD_MB: int = Field(default=500, ge=1)

    TEMP_DIR: Path = Path("./temp")
    CLEANUP_AFTER_RENDER: bool = True
    AUTO_CLEANUP_ENABLED: bool = True
    AUTO_CLEANUP_INTERVAL_MINUTES: int = Field(default=30, ge=1)
    TEMP_TTL_HOURS: int = Field(default=24, ge=1)

    # 3. Provider Factory Switches
    SEPARATION_PROVIDER: str = "fast_mode"
    STT_PROVIDER: str = "groq"
    LLM_PROVIDER: str = "gemini"
    TTS_PROVIDER: str = "edge_tts"
    VISUAL_PROVIDER: str = "pexels"
    STORAGE_PROVIDER: str = "supabase"

    # 4. AI & Media Provider API Keys / Endpoints
    GROQ_API_KEY: str = ""
    GROQ_STT_MODEL: str = "whisper-large-v3"
    FASTER_WHISPER_MODEL: str = "small"
    FASTER_WHISPER_DEVICE: str = "cpu"
    FASTER_WHISPER_COMPUTE_TYPE: str = "int8"

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    REPLICATE_API_TOKEN: str = ""
    REPLICATE_DEMUCS_MODEL: str = (
        "cjwbw/demucs:25a173108cff36ef9f80f854c162d01df9e6528be175794b81158fa03836d953"
    )
    SEPARATION_FAST_MODE_DUCK_DB: int = -14

    EDGE_TTS_DEFAULT_VOICE: str = "vi-VN-HoaiMyNeural"
    EDGE_TTS_MAX_SPEED_PCT: int = Field(default=25, ge=0, le=100)

    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_BASE_URL: str = "https://api.elevenlabs.io/v1"
    ELEVENLABS_MODEL_ID: str = "eleven_multilingual_v2"
    ELEVENLABS_DEFAULT_VOICE_ID: str = "21m00Tcm4TlvDq8ikWAM"

    FPT_AI_API_KEY: str = ""
    FPT_AI_BASE_URL: str = "https://api.fpt.ai/hmi/tts/v5"
    FPT_AI_DEFAULT_VOICE: str = "banmai"

    VIETTEL_AI_TOKEN: str = ""
    VIETTEL_AI_BASE_URL: str = "https://viettelai.vn/tts/speech_synthesis"
    VIETTEL_AI_DEFAULT_VOICE: str = "hn-quynhanh"

    PEXELS_API_KEY: str = ""
    PIXABAY_API_KEY: str = ""

    FLUX_API_KEY: str = ""
    FLUX_API_URL: str = "https://api.replicate.com/v1/predictions"
    FLUX_MODEL: str = "black-forest-labs/flux-schnell"

    SD_API_KEY: str = ""
    SD_API_URL: str = "https://api.stability.ai/v2beta/stable-image/generate/core"
    SD_MODEL: str = "sd3.5-large-turbo"

    # 5. FFmpeg Engine, Hardware Acceleration & Audio Ducking
    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    FFMPEG_HWACCEL: str = "auto"
    FFMPEG_VIDEO_CODEC: str = "h264_nvenc"
    FFMPEG_FALLBACK_CODEC: str = "libx264"
    FFMPEG_PRESET: str = "fast"
    FFMPEG_CRF: int = Field(default=23, ge=0, le=51)
    FFMPEG_AUDIO_CODEC: str = "aac"
    FFMPEG_AUDIO_BITRATE: str = "192k"
    FFMPEG_THREADS: int = Field(default=0, ge=0)

    FFMPEG_DUCKING_THRESHOLD: float = 0.03
    FFMPEG_DUCKING_RATIO: float = 6.0
    FFMPEG_DUCKING_ATTACK: int = 20
    FFMPEG_DUCKING_RELEASE: int = 250

    # 6. Supabase (PostgreSQL, Auth & Storage Bucket)
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_JWT_SECRET: str = ""
    SUPABASE_STORAGE_BUCKET: str = "video-studio-assets"
    SUPABASE_SIGNED_URL_TTL_SECONDS: int = Field(default=86400, ge=60)

    # 7. Payment Gateway (SePay / Casso) & VietQR Configuration
    PAYMENT_GATEWAY: str = "sepay"
    SEPAY_API_KEY: str = ""
    SEPAY_WEBHOOK_SECRET: str = ""
    CASSO_API_KEY: str = ""
    CASSO_WEBHOOK_SECRET: str = ""

    VIETQR_BANK_BIN: str = "970422"
    VIETQR_ACCOUNT_NO: str = "0123456789"
    VIETQR_ACCOUNT_NAME: str = "NGUYEN VAN A"
    VIETQR_TEMPLATE: str = "compact2"
    PAYMENT_MEMO_PREFIX: str = "AVS"
    VND_PER_CREDIT: int = Field(default=100, ge=1)

    # 8. Credit Pricing & Free-Tier Watermark Settings
    INITIAL_FREE_CREDITS: int = Field(default=50, ge=0)
    CREDITS_PER_SECOND_TRANSLATOR: int = Field(default=1, ge=0)
    CREDITS_PER_SECOND_FACELESS: int = Field(default=1, ge=0)
    CREDITS_SURCHARGE_SEPARATION: int = Field(default=10, ge=0)
    CREDITS_SURCHARGE_PREMIUM_TTS: int = Field(default=15, ge=0)
    CREDITS_SURCHARGE_AI_VISUAL: int = Field(default=20, ge=0)

    FREE_TIER_WATERMARK_ENABLED: bool = True
    WATERMARK_TEXT: str = "AI Video Studio"
    WATERMARK_IMAGE_PATH: str = ""
    WATERMARK_POSITION: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = (
        "bottom_right"
    )
    WATERMARK_OPACITY: float = Field(default=0.65, ge=0.0, le=1.0)

    # 9. Local Sandbox & Tier Upgrade
    LOCAL_SANDBOX_MODE: bool = False
    SANDBOX_INITIAL_CREDITS: int = Field(default=500, ge=0)
    PRO_UPGRADE_CREDITS: int = Field(default=500, ge=1)
    ENTERPRISE_UPGRADE_CREDITS: int = Field(default=1500, ge=1)

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS_ORIGINS into a clean list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def temp_path(self) -> Path:
        """Return TEMP_DIR as a resolved Path instance."""
        return Path(self.TEMP_DIR)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application Settings instance."""
    return Settings()
