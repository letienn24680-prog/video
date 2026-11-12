"""Async-friendly Supabase repository for billing, payment transactions, and credit ledger."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from backend.core.exceptions import (
    DatabaseError,
    NotFoundError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.models.enums import PaymentGateway, PaymentStatus, SubscriptionTier
from backend.services.supabase.client import get_supabase_client
from backend.services.supabase.repository import SupabaseRepository, _extract_data

if TYPE_CHECKING:
    from supabase import Client

logger = get_logger(__name__)


class SupabaseBillingRepository:
    """Repository managing payment_transactions, credit_ledger, and billing RPC delegation."""

    def __init__(
        self,
        client: Client | None = None,
        repository: SupabaseRepository | None = None,
    ) -> None:
        self._client = client
        self._repository = repository

    @property
    def client(self) -> Client:
        """Lazily resolve the service_role Supabase client."""
        if self._client is None:
            self._client = get_supabase_client()
        return self._client

    @property
    def repository(self) -> SupabaseRepository:
        """Lazily resolve the shared SupabaseRepository using the same client."""
        if self._repository is None:
            self._repository = SupabaseRepository(client=self._client)
        return self._repository

    async def _run(self, label: str, fn: Any) -> Any:
        """Execute a synchronous supabase-py call inside a worker thread."""
        try:
            return await asyncio.to_thread(fn)
        except StudioError:
            raise
        except Exception as exc:
            logger.exception("Supabase billing operation '%s' failed: %s", label, exc)
            raise DatabaseError(f"Billing database operation '{label}' failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Payment Transactions (public.payment_transactions)
    # ------------------------------------------------------------------

    async def create_pending_transaction(
        self,
        user_id: str | UUID,
        *,
        gateway: PaymentGateway | str,
        reference_code: str,
        amount_vnd: int,
        credits_amount: int,
        raw_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a new pending top-up transaction row in public.payment_transactions."""
        if int(amount_vnd) <= 0:
            raise ValidationError("amount_vnd must be greater than 0")
        if int(credits_amount) <= 0:
            raise ValidationError("credits_amount must be greater than 0")

        clean_ref = (reference_code or "").strip().upper()
        if not clean_ref:
            raise ValidationError("reference_code is required")

        gateway_val = (
            gateway.value
            if isinstance(gateway, PaymentGateway)
            else PaymentGateway(str(gateway).strip().lower()).value
        )

        payload: dict[str, Any] = {
            "user_id": str(user_id),
            "gateway": gateway_val,
            "reference_code": clean_ref,
            "amount_vnd": int(amount_vnd),
            "credits_amount": int(credits_amount),
            "status": PaymentStatus.PENDING.value,
            "raw_payload": raw_payload or {},
        }

        response = await self._run(
            "create_pending_transaction",
            lambda: self.client.table("payment_transactions").insert(payload).execute(),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise DatabaseError("Supabase insert into payment_transactions returned no row")

        tx = dict(rows[0])
        logger.info(
            "Created pending payment transaction id=%s ref=%s user=%s amount_vnd=%d credits=%d",
            tx.get("id"),
            clean_ref,
            user_id,
            int(amount_vnd),
            int(credits_amount),
        )
        return tx

    async def get_transaction_by_id(
        self,
        transaction_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Fetch a single payment_transactions row by id, optionally scoped to user_id."""

        def _query() -> Any:
            q = (
                self.client.table("payment_transactions")
                .select("*")
                .eq("id", str(transaction_id))
            )
            if user_id is not None:
                q = q.eq("user_id", str(user_id))
            return q.limit(1).execute()

        response = await self._run("get_transaction_by_id", _query)
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Payment transaction {transaction_id} not found")
        return dict(rows[0])

    async def get_transaction_by_reference(
        self,
        reference_code: str,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a payment_transactions row by reference_code, or None if not found."""
        clean_ref = (reference_code or "").strip().upper()
        if not clean_ref:
            return None

        def _query() -> Any:
            q = (
                self.client.table("payment_transactions")
                .select("*")
                .eq("reference_code", clean_ref)
            )
            if user_id is not None:
                q = q.eq("user_id", str(user_id))
            return q.limit(1).execute()

        response = await self._run("get_transaction_by_reference", _query)
        rows = _extract_data(response) or []
        return dict(rows[0]) if rows else None

    async def list_transactions(
        self,
        user_id: str | UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List payment_transactions rows for user_id ordered newest created_at first."""
        safe_limit = max(1, min(int(limit), 200))

        def _query() -> Any:
            return (
                self.client.table("payment_transactions")
                .select("*")
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .limit(safe_limit)
                .execute()
            )

        response = await self._run("list_transactions", _query)
        rows = _extract_data(response) or []
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Credit Ledger (public.credit_ledger)
    # ------------------------------------------------------------------

    async def list_credit_ledger(
        self,
        user_id: str | UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List immutable credit_ledger audit entries for user_id ordered newest first."""
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))

        def _query() -> Any:
            return (
                self.client.table("credit_ledger")
                .select("*")
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .range(safe_offset, safe_offset + safe_limit - 1)
                .execute()
            )

        response = await self._run("list_credit_ledger", _query)
        rows = _extract_data(response) or []
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Delegated Wallet & Atomic RPC Operations
    # ------------------------------------------------------------------

    async def get_user_credits(self, user_id: str | UUID) -> dict[str, Any]:
        """Fetch the authoritative user_credits wallet record for user_id."""
        return await self.repository.get_user_credits(user_id)

    async def deduct_user_credits(
        self,
        user_id: str | UUID,
        amount: int,
        *,
        project_id: str | UUID | None = None,
        description: str = "Video processing credit deduction",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically deduct credits via public.deduct_user_credits RPC."""
        return await self.repository.deduct_user_credits(
            user_id,
            amount,
            project_id=project_id,
            description=description,
            metadata=metadata,
        )

    async def refund_user_credits(
        self,
        user_id: str | UUID,
        amount: int,
        *,
        project_id: str | UUID | None = None,
        description: str = "Credit refund",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically refund credits via public.refund_user_credits RPC."""
        return await self.repository.refund_user_credits(
            user_id,
            amount,
            project_id=project_id,
            description=description,
            metadata=metadata,
        )

    async def process_payment_webhook(
        self,
        *,
        reference_code: str,
        external_tx_id: str,
        amount_vnd: int,
        gateway: str = "sepay",
        credits_amount: int | None = None,
        user_id: str | UUID | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Settle a bank transfer idempotently via public.process_payment_webhook RPC."""
        return await self.repository.process_payment_webhook(
            reference_code=reference_code,
            external_tx_id=external_tx_id,
            amount_vnd=amount_vnd,
            gateway=gateway,
            credits_amount=credits_amount,
            user_id=user_id,
            raw_payload=raw_payload,
        )

    async def upgrade_subscription_tier(
        self,
        user_id: str | UUID,
        target_tier: SubscriptionTier | str,
        credit_cost: int,
    ) -> dict[str, Any]:
        """Validate target tier, atomically deduct credits, and upgrade profile tier."""
        return await self.repository.upgrade_subscription_tier(
            user_id,
            target_tier,
            credit_cost,
        )


BillingRepository = SupabaseBillingRepository
