"""Credit estimation, atomic deduction, and refund service for AI Video Studio."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any
from uuid import UUID

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    InsufficientCreditsError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.models.enums import (
    ProjectMode,
    SeparationProviderName,
    TTSProviderName,
    VisualProviderName,
)
from backend.models.project import PipelineConfig
from backend.services.supabase.billing_repo import SupabaseBillingRepository
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

_PREMIUM_TTS_PROVIDERS: frozenset[str] = frozenset(
    {
        TTSProviderName.ELEVENLABS.value,
        TTSProviderName.FPT_AI.value,
        TTSProviderName.VIETTEL_AI.value,
    }
)
_AI_VISUAL_PROVIDERS: frozenset[str] = frozenset(
    {
        VisualProviderName.FLUX.value,
        VisualProviderName.STABLE_DIFFUSION.value,
    }
)


def _normalize_provider_name(value: str | Enum | None, fallback: str) -> str:
    if value is None:
        return fallback.strip().lower()
    raw = value.value if isinstance(value, Enum) else str(value)
    cleaned = raw.strip().lower()
    return cleaned or fallback.strip().lower()


def _coerce_pipeline_config(pipeline_config: PipelineConfig | dict[str, Any] | None) -> PipelineConfig:
    if pipeline_config is None:
        return PipelineConfig()
    if isinstance(pipeline_config, PipelineConfig):
        return pipeline_config
    return PipelineConfig.model_validate(pipeline_config)


class CreditService:
    """Computes credit estimates and performs atomic project credit charges and refunds."""

    def __init__(
        self,
        billing_repo: SupabaseBillingRepository | SupabaseRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.billing_repo = billing_repo or SupabaseBillingRepository()

    def estimate_project_credits(
        self,
        mode: ProjectMode | str,
        duration_seconds: float,
        pipeline_config: PipelineConfig | dict[str, Any] | None = None,
    ) -> int:
        """Estimate required credits from project mode, duration, and provider surcharges."""
        try:
            resolved_mode = mode if isinstance(mode, ProjectMode) else ProjectMode(str(mode).strip().lower())
        except ValueError as exc:
            raise ValidationError(f"Unsupported project mode '{mode}'") from exc

        safe_duration = max(0.0, float(duration_seconds or 0.0))
        billable_seconds = math.ceil(safe_duration)

        per_second_rate = (
            self.settings.CREDITS_PER_SECOND_TRANSLATOR
            if resolved_mode == ProjectMode.TRANSLATOR
            else self.settings.CREDITS_PER_SECOND_FACELESS
        )
        base_credits = billable_seconds * max(0, int(per_second_rate))

        cfg = _coerce_pipeline_config(pipeline_config)

        separation_provider = _normalize_provider_name(
            cfg.separation_provider,
            self.settings.SEPARATION_PROVIDER,
        )
        tts_provider = _normalize_provider_name(
            cfg.tts_provider,
            self.settings.TTS_PROVIDER,
        )
        visual_provider = _normalize_provider_name(
            cfg.visual_provider,
            self.settings.VISUAL_PROVIDER,
        )

        surcharges = 0
        if (
            resolved_mode == ProjectMode.TRANSLATOR or cfg.separation_provider is not None
        ) and separation_provider == SeparationProviderName.REPLICATE.value:
            surcharges += max(0, int(self.settings.CREDITS_SURCHARGE_SEPARATION))

        if tts_provider in _PREMIUM_TTS_PROVIDERS:
            surcharges += max(0, int(self.settings.CREDITS_SURCHARGE_PREMIUM_TTS))

        if (
            resolved_mode == ProjectMode.FACELESS or cfg.visual_provider is not None
        ) and visual_provider in _AI_VISUAL_PROVIDERS:
            surcharges += max(0, int(self.settings.CREDITS_SURCHARGE_AI_VISUAL))

        total_credits = max(0, base_credits + surcharges)
        logger.debug(
            "Estimated %d credits (mode=%s, duration=%.2fs, base=%d, surcharges=%d)",
            total_credits,
            resolved_mode.value,
            safe_duration,
            base_credits,
            surcharges,
        )
        return total_credits

    async def charge_for_project(
        self,
        user_id: str | UUID,
        project_id: str | UUID,
        amount: int,
        description: str = "Video processing credit charge",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify wallet balance and atomically deduct credits for a project."""
        charge_amount = int(amount)
        if charge_amount < 0:
            raise ValidationError(
                f"Credit charge amount must be non-negative, got {charge_amount}",
                details={"amount": charge_amount},
            )
        if charge_amount == 0:
            logger.info(
                "Skipping zero-credit charge for user=%s project=%s",
                user_id,
                project_id,
            )
            return {
                "status": "skipped",
                "user_id": str(user_id),
                "project_id": str(project_id),
                "deducted": 0,
            }

        try:
            wallet = await self.billing_repo.get_user_credits(user_id)
            balance = int(wallet.get("balance", 0))
            if balance < charge_amount:
                logger.warning(
                    "Insufficient credits for user=%s project=%s (balance=%d, required=%d)",
                    user_id,
                    project_id,
                    balance,
                    charge_amount,
                )
                raise InsufficientCreditsError(
                    f"Insufficient credits: balance={balance}, required={charge_amount}",
                    details={
                        "user_id": str(user_id),
                        "project_id": str(project_id),
                        "balance": balance,
                        "required": charge_amount,
                    },
                )

            result = await self.billing_repo.deduct_user_credits(
                user_id,
                charge_amount,
                project_id=project_id,
                description=description,
                metadata=metadata or {},
            )
            logger.info(
                "Charged %d credits for user=%s project=%s (new_balance=%s)",
                charge_amount,
                user_id,
                project_id,
                result.get("new_balance"),
            )
            return result
        except StudioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error charging %d credits for user=%s project=%s: %s",
                charge_amount,
                user_id,
                project_id,
                exc,
            )
            raise

    async def refund_for_project(
        self,
        user_id: str | UUID,
        project_id: str | UUID,
        amount: int,
        description: str = "Project credit refund",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically refund credits for a cancelled or failed project."""
        refund_amount = int(amount)
        if refund_amount < 0:
            raise ValidationError(
                f"Credit refund amount must be non-negative, got {refund_amount}",
                details={"amount": refund_amount},
            )
        if refund_amount == 0:
            logger.info(
                "Skipping zero-credit refund for user=%s project=%s",
                user_id,
                project_id,
            )
            return {
                "status": "skipped",
                "user_id": str(user_id),
                "project_id": str(project_id),
                "refunded": 0,
            }

        try:
            result = await self.billing_repo.refund_user_credits(
                user_id,
                refund_amount,
                project_id=project_id,
                description=description,
                metadata=metadata or {},
            )
            logger.info(
                "Refunded %d credits for user=%s project=%s (new_balance=%s)",
                refund_amount,
                user_id,
                project_id,
                result.get("new_balance"),
            )
            return result
        except StudioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error refunding %d credits for user=%s project=%s: %s",
                refund_amount,
                user_id,
                project_id,
                exc,
            )
            raise
