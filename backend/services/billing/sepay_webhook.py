"""SePay and Casso VietQR payment webhook verification, parsing, and idempotent settlement."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.core.security import verify_webhook_secret
from backend.models.enums import PaymentGateway
from backend.services.supabase.billing_repo import SupabaseBillingRepository

logger = get_logger(__name__)

_SUPPORTED_WEBHOOK_GATEWAYS: frozenset[str] = frozenset(
    {PaymentGateway.SEPAY.value, PaymentGateway.CASSO.value}
)


def _normalize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in headers.items() if v is not None}


def _parse_positive_amount(raw_value: Any) -> int:
    if raw_value is None or isinstance(raw_value, bool):
        return 0
    try:
        amount = int(float(raw_value))
    except (TypeError, ValueError):
        return 0
    return amount


class PaymentWebhookService:
    """Verifies SePay / Casso webhooks, parses transfer memos, and settles credits idempotently."""

    def __init__(
        self,
        billing_repo: SupabaseBillingRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.billing_repo = billing_repo or SupabaseBillingRepository()

    def _resolve_gateway(self, gateway: str | None) -> str:
        raw = (gateway or self.settings.PAYMENT_GATEWAY or PaymentGateway.SEPAY.value).strip().lower()
        if raw not in _SUPPORTED_WEBHOOK_GATEWAYS:
            raise ValidationError(
                f"Unsupported payment webhook gateway '{raw}'",
                details={"gateway": raw, "supported": sorted(_SUPPORTED_WEBHOOK_GATEWAYS)},
            )
        return raw

    def _expected_secret(self, gateway: str) -> str:
        if gateway == PaymentGateway.CASSO.value:
            return self.settings.CASSO_WEBHOOK_SECRET
        return self.settings.SEPAY_WEBHOOK_SECRET

    def verify_headers(
        self,
        headers: Mapping[str, str] | None,
        gateway: str | None = None,
    ) -> str:
        """Verify incoming webhook secret from Authorization, X-Webhook-Secret, or secure-token."""
        resolved_gateway = self._resolve_gateway(gateway)
        expected_secret = (self._expected_secret(resolved_gateway) or "").strip()

        if not expected_secret or expected_secret.startswith("your_"):
            raise ConfigurationError(
                f"Webhook secret for '{resolved_gateway}' is not configured"
            )

        normalized = _normalize_headers(headers)
        candidates = [
            normalized.get("authorization"),
            normalized.get("x-webhook-secret"),
            normalized.get("secure-token"),
        ]
        for candidate in candidates:
            if candidate and verify_webhook_secret(candidate, expected_secret):
                return resolved_gateway

        logger.warning(
            "Rejected payment webhook for gateway=%s due to missing or invalid secret",
            resolved_gateway,
        )
        raise AuthenticationError(f"Invalid webhook secret for '{resolved_gateway}'")

    def extract_reference_code(self, *text_fields: Any) -> str | None:
        """Extract PAYMENT_MEMO_PREFIX + 6..16 uppercase alphanumeric code from memo fields."""
        prefix = re.sub(r"[^A-Z0-9]", "", (self.settings.PAYMENT_MEMO_PREFIX or "AVS").upper()) or "AVS"
        pattern = re.compile(rf"({re.escape(prefix)}[A-Z0-9]{{6,16}})")

        for field in text_fields:
            if not field:
                continue
            upper_text = str(field).upper()
            match = pattern.search(upper_text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_transactions(payload: dict[str, Any], gateway: str) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ValidationError("Webhook payload must be a JSON object")

        if "data" in payload:
            data = payload["data"]
            if data is None:
                return []
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict):
                return [data]
            raise ValidationError("Webhook 'data' field must be an object or list of objects")

        # Flat SePay payload or single-object Casso payload
        if gateway == PaymentGateway.CASSO.value and payload.get("error") is not None and not any(
            k in payload for k in ("id", "tid", "description", "amount")
        ):
            return []

        return [payload]

    @staticmethod
    def _extract_external_tx_id(item: dict[str, Any], gateway: str) -> str:
        if gateway == PaymentGateway.CASSO.value:
            raw_id = item.get("tid") or item.get("id") or item.get("referenceCode")
        else:
            raw_id = item.get("id") or item.get("referenceCode") or item.get("tid")

        if raw_id is None or str(raw_id).strip() == "":
            raise ValidationError(
                "Webhook transaction is missing an external transaction id",
                details={"gateway": gateway},
            )
        return f"{gateway}:{str(raw_id).strip()}"

    async def process_payload(
        self,
        payload: dict[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        gateway: str | None = None,
        skip_auth: bool = False,
    ) -> list[dict[str, Any]]:
        """Authenticate and process a SePay or Casso webhook payload idempotently."""
        if skip_auth:
            resolved_gateway = self._resolve_gateway(gateway)
        else:
            resolved_gateway = self.verify_headers(headers, gateway=gateway)

        transactions = self._extract_transactions(payload, resolved_gateway)
        if not transactions:
            logger.info("Received empty payment webhook batch for gateway=%s", resolved_gateway)
            return []

        results: list[dict[str, Any]] = []
        for item in transactions:
            transfer_type = item.get("transferType")
            if transfer_type is not None and str(transfer_type).strip().lower() != "in":
                logger.info(
                    "Ignoring outgoing/non-in transfer on gateway=%s transferType=%s",
                    resolved_gateway,
                    transfer_type,
                )
                results.append(
                    {
                        "status": "ignored",
                        "reason": f"transfer_type_{str(transfer_type).strip().lower()}",
                        "gateway": resolved_gateway,
                    }
                )
                continue

            raw_amount = (
                item.get("transferAmount")
                if item.get("transferAmount") is not None
                else item.get("amount")
            )
            amount_vnd = _parse_positive_amount(raw_amount)
            if amount_vnd <= 0:
                logger.info(
                    "Ignoring non-positive transfer amount=%s on gateway=%s",
                    raw_amount,
                    resolved_gateway,
                )
                results.append(
                    {
                        "status": "ignored",
                        "reason": "non_positive_amount",
                        "gateway": resolved_gateway,
                    }
                )
                continue

            external_tx_id = self._extract_external_tx_id(item, resolved_gateway)
            reference_code = self.extract_reference_code(
                item.get("code"),
                item.get("content"),
                item.get("description"),
            )
            if not reference_code:
                logger.info(
                    "Ignoring transfer without matching memo prefix (external_tx_id=%s, gateway=%s)",
                    external_tx_id,
                    resolved_gateway,
                )
                results.append(
                    {
                        "status": "ignored",
                        "reason": "reference_code_not_found",
                        "external_tx_id": external_tx_id,
                        "amount_vnd": amount_vnd,
                        "gateway": resolved_gateway,
                    }
                )
                continue

            try:
                rpc_result = await self.billing_repo.process_payment_webhook(
                    reference_code=reference_code,
                    external_tx_id=external_tx_id,
                    amount_vnd=amount_vnd,
                    gateway=resolved_gateway,
                    raw_payload=item,
                )
            except StudioError:
                raise
            except Exception as exc:
                logger.exception(
                    "Unexpected failure settling payment webhook ref=%s ext_tx=%s: %s",
                    reference_code,
                    external_tx_id,
                    exc,
                )
                raise

            logger.info(
                "Processed payment webhook gateway=%s ref=%s ext_tx=%s status=%s credits_added=%s",
                resolved_gateway,
                reference_code,
                external_tx_id,
                rpc_result.get("status"),
                rpc_result.get("credits_added"),
            )
            results.append(
                {
                    **rpc_result,
                    "reference_code": reference_code,
                    "external_tx_id": external_tx_id,
                    "amount_vnd": amount_vnd,
                    "gateway": resolved_gateway,
                }
            )

        return results
