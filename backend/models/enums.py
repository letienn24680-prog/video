"""Domain enums and project state-machine transitions for AI Video Studio."""

from __future__ import annotations

from enum import Enum


class ProjectMode(str, Enum):
    """Supported AI video production pipelines."""

    TRANSLATOR = "translator"
    FACELESS = "faceless"


class ProjectStatus(str, Enum):
    """Finite state machine statuses matching public.projects.status CHECK constraint."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[ProjectStatus] = frozenset(
    {
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    }
)

VALID_STATUS_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.PENDING: {
        ProjectStatus.PROCESSING,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.PROCESSING: {
        ProjectStatus.AWAITING_REVIEW,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.AWAITING_REVIEW: {
        ProjectStatus.RENDERING,
        ProjectStatus.PROCESSING,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.RENDERING: {
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.COMPLETED: set(),
    ProjectStatus.FAILED: {
        ProjectStatus.PROCESSING,
        ProjectStatus.PENDING,
    },
    ProjectStatus.CANCELLED: set(),
}


def can_transition(src: ProjectStatus | str, dst: ProjectStatus | str) -> bool:
    """Return True if transitioning from ``src`` to ``dst`` is permitted by the FSM."""
    try:
        src_status = src if isinstance(src, ProjectStatus) else ProjectStatus(str(src))
        dst_status = dst if isinstance(dst, ProjectStatus) else ProjectStatus(str(dst))
    except ValueError:
        return False
    return dst_status in VALID_STATUS_TRANSITIONS.get(src_status, set())


class AspectRatio(str, Enum):
    """Supported output video aspect ratios."""

    PORTRAIT_9_16 = "9:16"
    LANDSCAPE_16_9 = "16:9"
    SQUARE_1_1 = "1:1"


class VisualSource(str, Enum):
    """Supported visual asset sources matching public.project_scenes.visual_source."""

    PEXELS = "pexels"
    PIXABAY = "pixabay"
    AI_GEN = "ai_gen"
    CUSTOM_UPLOAD = "custom_upload"


class SubscriptionTier(str, Enum):
    """User subscription tiers controlling watermark and quota policies."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class PaymentGateway(str, Enum):
    """Supported payment gateways matching public.payment_transactions.gateway."""

    SEPAY = "sepay"
    CASSO = "casso"
    VIETQR = "vietqr"
    MANUAL = "manual"
    ADMIN = "admin"


class PaymentStatus(str, Enum):
    """Payment transaction lifecycle states."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class CreditEntryType(str, Enum):
    """Immutable credit ledger entry types."""

    SIGNUP_BONUS = "signup_bonus"
    TOPUP = "topup"
    DEDUCTION = "deduction"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class STTProviderName(str, Enum):
    """Speech-to-text provider identifiers."""

    GROQ = "groq"
    FASTER_WHISPER = "faster_whisper"


class TTSProviderName(str, Enum):
    """Text-to-speech provider identifiers."""

    EDGE_TTS = "edge_tts"
    ELEVENLABS = "elevenlabs"
    FPT_AI = "fpt_ai"
    VIETTEL_AI = "viettel_ai"


class LLMProviderName(str, Enum):
    """Large language model provider identifiers."""

    GEMINI = "gemini"


class VisualProviderName(str, Enum):
    """Visual stock/generation provider identifiers."""

    PEXELS = "pexels"
    PIXABAY = "pixabay"
    FLUX = "flux"
    STABLE_DIFFUSION = "stable_diffusion"


class SeparationProviderName(str, Enum):
    """Vocal/BGM separation provider identifiers."""

    FAST_MODE = "fast_mode"
    REPLICATE = "replicate"
    LOCAL_DEMUCS = "local_demucs"


class StorageProviderName(str, Enum):
    """Object storage provider identifiers."""

    SUPABASE = "supabase"
    LOCAL = "local"
