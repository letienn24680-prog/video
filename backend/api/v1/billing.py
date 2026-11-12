"""Billing, credit wallet, credit ledger, and VietQR top-up API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.exceptions import NotFoundError, StudioError
from backend.core.logging import get_logger
from backend.core.security import CurrentUser, get_current_user
from backend.models.enums import CreditEntryType, PaymentGateway, PaymentStatus
from backend.services.billing.vietqr import TopUpIntentResponse, VietQRService
from backend.services.supabase.billing_repo import SupabaseBillingRepository

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])


class CreditLedgerEntryResponse(BaseModel):
    """Immutable audit entry from public.credit_ledger."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: UUID
    user_id: UUID
    entry_type: CreditEntryType
    amount: int
    balance_after: int
    project_id: UUID | None = None
    transaction_id: UUID | None = None
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class BillingCreditsResponse(BaseModel):
    """User credit wallet summary with recent ledger entries."""

    user_id: UUID
    balance: int
    lifetime_purchased: int
    lifetime_spent: int
    updated_at: datetime | None = None
    credit_ledger: list[CreditLedgerEntryResponse] = Field(default_factory=list)


class BillingHistoryResponse(BaseModel):
    """Combined billing history including wallet balance, tier, transactions, and ledger."""

    balance: int
    tier: str
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    credit_ledger: list[dict[str, Any]] = Field(default_factory=list)


class TopUpIntentRequest(BaseModel):
    """Request payload to create a VietQR top-up intent."""

    amount_vnd: int = Field(..., ge=1)
    gateway: Literal["sepay", "casso"] = "sepay"


class TransactionStatusResponse(BaseModel):
    """Payment transaction status returned for top-up polling."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: UUID
    user_id: UUID
    gateway: PaymentGateway
    reference_code: str
    external_tx_id: str | None = None
    amount_vnd: int
    credits_amount: int
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


def get_billing_repository(request: Request) -> SupabaseBillingRepository:
    """Resolve the shared SupabaseBillingRepository from app.state or construct one."""
    repo = getattr(request.app.state, "billing_repository", None)
    if repo is None:
        base_repo = getattr(request.app.state, "repository", None)
        repo = SupabaseBillingRepository(repository=base_repo)
        request.app.state.billing_repository = repo
    return repo


def get_vietqr_service(
    settings: Settings = Depends(get_settings),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> VietQRService:
    """Construct VietQRService wired with Settings and SupabaseBillingRepository."""
    return VietQRService(billing_repo=billing_repo, settings=settings)


@router.get(
    "/credits",
    response_model=BillingCreditsResponse,
    summary="Get current user's credit wallet balance and recent ledger entries",
)
async def get_billing_credits(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> BillingCreditsResponse:
    """Return user_credits wallet totals and recent credit_ledger entries."""
    try:
        wallet = await billing_repo.get_user_credits(user.id)
        ledger_rows = await billing_repo.list_credit_ledger(
            user.id,
            limit=limit,
            offset=offset,
        )
        entries = [CreditLedgerEntryResponse.model_validate(row) for row in ledger_rows]
        return BillingCreditsResponse(
            user_id=UUID(str(wallet.get("user_id", user.id))),
            balance=int(wallet.get("balance", 0)),
            lifetime_purchased=int(wallet.get("lifetime_purchased", 0)),
            lifetime_spent=int(wallet.get("lifetime_spent", 0)),
            updated_at=wallet.get("updated_at"),
            credit_ledger=entries,
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch billing credits for user=%s: %s", user.id, exc)
        raise


@router.get(
    "/history",
    response_model=BillingHistoryResponse,
    summary="Get current user's credit balance, subscription tier, transactions, and credit ledger",
)
async def get_billing_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> BillingHistoryResponse:
    """Return wallet balance, subscription tier, VietQR transactions, and credit ledger."""
    repo = getattr(request.app.state, "repository", None) or billing_repo.repository
    try:
        wallet = await repo.get_user_credits(user.id)
        try:
            profile = await repo.get_profile(user.id)
        except NotFoundError:
            profile = {}
        transactions = await billing_repo.list_transactions(user.id, limit=limit)
        credit_ledger = await billing_repo.list_credit_ledger(user.id, limit=limit)
        tier = str(profile.get("subscription_tier") or profile.get("tier") or "free")
        return BillingHistoryResponse(
            balance=int(wallet.get("balance", 0)),
            tier=tier,
            transactions=transactions,
            credit_ledger=credit_ledger,
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch billing history for user=%s: %s", user.id, exc)
        raise


@router.post(
    "/topup-intent",
    response_model=TopUpIntentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pending VietQR top-up transaction and dynamic QR code URL",
)
async def create_topup_intent(
    payload: TopUpIntentRequest,
    user: CurrentUser = Depends(get_current_user),
    vietqr_service: VietQRService = Depends(get_vietqr_service),
) -> TopUpIntentResponse:
    """Create a pending payment_transactions record and return VietQR bank transfer info."""
    try:
        return await vietqr_service.create_topup_intent(
            user_id=user.id,
            amount_vnd=payload.amount_vnd,
            gateway=payload.gateway,
        )
    except StudioError:
        raise
    except Exception as exc:
        logger.exception("Failed to create top-up intent for user=%s: %s", user.id, exc)
        raise


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionStatusResponse,
    summary="Poll status of a user's top-up payment transaction",
)
async def get_transaction_status(
    transaction_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    billing_repo: SupabaseBillingRepository = Depends(get_billing_repository),
) -> TransactionStatusResponse:
    """Return a single payment_transactions row owned by the authenticated user."""
    try:
        tx = await billing_repo.get_transaction_by_id(transaction_id, user_id=user.id)
        return TransactionStatusResponse.model_validate(tx)
    except StudioError:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to fetch transaction %s for user=%s: %s",
            transaction_id,
            user.id,
            exc,
        )
        raise
