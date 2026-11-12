"""Dynamic VietQR URL generation and top-up intent creation service."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from urllib.parse import quote, urlencode
from uuid import UUID

from pydantic import BaseModel

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError, StudioError, ValidationError
from backend.core.logging import get_logger
from backend.models.enums import PaymentGateway, PaymentStatus
from backend.services.supabase.billing_repo import SupabaseBillingRepository

logger = get_logger(__name__)

_VIETQR_BASE_URL = "https://img.vietqr.io/image"
_ALLOWED_TOPUP_GATEWAYS: frozenset[PaymentGateway] = frozenset(
    {PaymentGateway.SEPAY, PaymentGateway.CASSO, PaymentGateway.VIETQR}
)


class TopUpIntentResponse(BaseModel):
    """Response payload returned to the client for displaying a VietQR payment modal."""

    transaction_id: UUID
    reference_code: str
    gateway: PaymentGateway
    status: PaymentStatus
    amount_vnd: int
    credits_amount: int
    vnd_per_credit: int
    qr_image_url: str
    bank_bin: str
    account_no: str
    account_name: str
    template: str
    add_info: str
    created_at: datetime | None = None


class VietQRService:
    """Generates unique bank transfer memos, VietQR image URLs, and pending top-up rows."""

    def __init__(
        self,
        billing_repo: SupabaseBillingRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.billing_repo = billing_repo or SupabaseBillingRepository()

    def _memo_prefix(self) -> str:
        cleaned = re.sub(r"[^A-Z0-9]", "", (self.settings.PAYMENT_MEMO_PREFIX or "AVS").upper())
        return cleaned or "AVS"

    def generate_reference_code(self, user_id: str | UUID) -> str:
        """Create a unique uppercase alphanumeric bank transfer memo prefixed with PAYMENT_MEMO_PREFIX."""
        prefix = self._memo_prefix()
        user_hex = re.sub(r"[^A-Z0-9]", "", str(user_id).upper())
        if len(user_hex) >= 2:
            suffix = f"{user_hex[:2]}{secrets.token_hex(3).upper()}"
        else:
            suffix = secrets.token_hex(4).upper()
        return f"{prefix}{suffix}"

    def build_vietqr_image_url(self, amount_vnd: int, reference_code: str) -> str:
        """Build a dynamic VietQR Quick Link PNG URL with amount, addInfo, and accountName."""
        if int(amount_vnd) <= 0:
            raise ValidationError("amount_vnd must be greater than 0")

        clean_ref = (reference_code or "").strip()
        if not clean_ref:
            raise ValidationError("reference_code is required to build VietQR URL")

        bank_bin = (self.settings.VIETQR_BANK_BIN or "").strip()
        account_no = (self.settings.VIETQR_ACCOUNT_NO or "").strip()
        template = (self.settings.VIETQR_TEMPLATE or "compact2").strip()
        account_name = (self.settings.VIETQR_ACCOUNT_NAME or "").strip()

        if not bank_bin or not account_no:
            raise ConfigurationError(
                "VIETQR_BANK_BIN and VIETQR_ACCOUNT_NO must be configured for VietQR generation"
            )

        base = (
            f"{_VIETQR_BASE_URL}/"
            f"{quote(bank_bin, safe='')}-{quote(account_no, safe='')}-{quote(template, safe='')}.png"
        )
        params: dict[str, str] = {
            "amount": str(int(amount_vnd)),
            "addInfo": clean_ref,
        }
        if account_name:
            params["accountName"] = account_name

        return f"{base}?{urlencode(params)}"

    async def create_topup_intent(
        self,
        user_id: str | UUID,
        amount_vnd: int,
        gateway: PaymentGateway | str | None = None,
    ) -> TopUpIntentResponse:
        """Validate VND amount, compute credits, persist a pending transaction, and return QR data."""
        vnd_amount = int(amount_vnd)
        vnd_per_credit = max(1, int(self.settings.VND_PER_CREDIT))
        if vnd_amount < vnd_per_credit:
            raise ValidationError(
                f"Minimum top-up amount is {vnd_per_credit} VND (got {vnd_amount} VND)",
                details={"amount_vnd": vnd_amount, "min_amount_vnd": vnd_per_credit},
            )

        raw_gateway = gateway if gateway is not None else self.settings.PAYMENT_GATEWAY
        try:
            resolved_gateway = (
                raw_gateway
                if isinstance(raw_gateway, PaymentGateway)
                else PaymentGateway(str(raw_gateway).strip().lower())
            )
        except ValueError as exc:
            raise ValidationError(f"Unsupported payment gateway '{raw_gateway}'") from exc

        if resolved_gateway not in _ALLOWED_TOPUP_GATEWAYS:
            raise ValidationError(
                f"Gateway '{resolved_gateway.value}' cannot be used for VietQR top-up intents"
            )

        credits_amount = vnd_amount // vnd_per_credit
        reference_code = self.generate_reference_code(user_id)
        qr_image_url = self.build_vietqr_image_url(vnd_amount, reference_code)

        try:
            tx = await self.billing_repo.create_pending_transaction(
                user_id,
                gateway=resolved_gateway,
                reference_code=reference_code,
                amount_vnd=vnd_amount,
                credits_amount=credits_amount,
                raw_payload={
                    "intent": {
                        "bank_bin": self.settings.VIETQR_BANK_BIN,
                        "account_no": self.settings.VIETQR_ACCOUNT_NO,
                        "template": self.settings.VIETQR_TEMPLATE,
                        "vnd_per_credit": vnd_per_credit,
                    }
                },
            )
        except StudioError:
            raise
        except Exception as exc:
            logger.exception("Failed to create top-up intent for user=%s: %s", user_id, exc)
            raise

        logger.info(
            "Created VietQR top-up intent tx=%s user=%s ref=%s vnd=%d credits=%d gateway=%s",
            tx.get("id"),
            user_id,
            reference_code,
            vnd_amount,
            credits_amount,
            resolved_gateway.value,
        )

        return TopUpIntentResponse(
            transaction_id=tx["id"],
            reference_code=reference_code,
            gateway=resolved_gateway,
            status=PaymentStatus(str(tx.get("status", PaymentStatus.PENDING.value))),
            amount_vnd=vnd_amount,
            credits_amount=credits_amount,
            vnd_per_credit=vnd_per_credit,
            qr_image_url=qr_image_url,
            bank_bin=self.settings.VIETQR_BANK_BIN,
            account_no=self.settings.VIETQR_ACCOUNT_NO,
            account_name=self.settings.VIETQR_ACCOUNT_NAME,
            template=self.settings.VIETQR_TEMPLATE,
            add_info=reference_code,
            created_at=tx.get("created_at"),
        )
