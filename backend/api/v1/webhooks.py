"""Payment gateway webhook endpoints for SePay and Casso VietQR settlement."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from backend.api.v1.billing import get_billing_repository
from backend.core.config import Settings, get_settings
from backend.core.exceptions import StudioError, ValidationError
from backend.core.logging import get_logger
from backend.services.billing.sepay_webhook import PaymentWebhookService
from backend.services.supabase.billing_repo import SupabaseBillingRepository

logger = get_logger(__name__)

router = APIRouter(tags=["Webhooks"])


def get_webhook_service(
    settings: Settings = Depends(get_settings),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> PaymentWebhookService:
    """Construct PaymentWebhookService with Settings and SupabaseBillingRepository."""
    return PaymentWebhookService(billing_repo=billing_repo, settings=settings)


@router.post(
    "/api/webhooks/payment",
    summary="Process SePay or Casso bank transfer webhook idempotently",
)
async def handle_payment_webhook(
    request: Request,
    gateway: str | None = Query(
        default=None,
        description="Payment gateway ('sepay' or 'casso'); defaults to settings.PAYMENT_GATEWAY",
    ),
    settings: Settings = Depends(get_settings),
    webhook_service: PaymentWebhookService = Depends(get_webhook_service),
) -> dict[str, Any]:
    """Authenticate webhook secret and atomically credit matching top-up transactions."""
    resolved_gateway = (gateway or settings.PAYMENT_GATEWAY).strip().lower()

    # Verify webhook secret before parsing body to reject unauthenticated callers early.
    verified_gateway = webhook_service.verify_headers(request.headers, gateway=resolved_gateway)

    try:
        payload = await request.json()
    except Exception as exc:
        raise ValidationError("Invalid JSON webhook payload") from exc

    if not isinstance(payload, dict):
        raise ValidationError("Webhook payload must be a JSON object")

    try:
        results = await webhook_service.process_payload(
            payload,
            headers=request.headers,
            gateway=verified_gateway,
            skip_auth=True,
        )
        return {
            "success": True,
            "gateway": verified_gateway,
            "results": results,
        }
    except StudioError:
        raise
    except Exception as exc:
        logger.exception(
            "Unhandled error processing payment webhook for gateway=%s: %s",
            verified_gateway,
            exc,
        )
        raise
