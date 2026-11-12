"""Async-friendly Supabase repository for projects, scenes, profiles, and credit RPCs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from backend.core.exceptions import (
    DatabaseError,
    InsufficientCreditsError,
    InvalidStateTransitionError,
    NotFoundError,
    StudioError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.models.enums import (
    AspectRatio,
    ProjectMode,
    ProjectStatus,
    SubscriptionTier,
    can_transition,
)
from backend.models.project import ProjectDetailResponse, ProjectResponse
from backend.models.scene import SceneCreate, SceneResponse, SceneUpdateRequest
from backend.services.supabase.client import get_supabase_client

if TYPE_CHECKING:
    from supabase import Client

logger = get_logger(__name__)


def _extract_data(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("data")
    return getattr(response, "data", None)


class SupabaseRepository:
    """Repository wrapping Supabase PostgREST queries and SECURITY DEFINER RPCs."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        """Lazily resolve the service_role Supabase client."""
        if self._client is None:
            self._client = get_supabase_client()
        return self._client

    async def _run(self, label: str, fn: Any) -> Any:
        """Execute a synchronous supabase-py call inside a worker thread."""
        try:
            return await asyncio.to_thread(fn)
        except StudioError:
            raise
        except Exception as exc:
            logger.exception("Supabase operation '%s' failed: %s", label, exc)
            raise DatabaseError(f"Database operation '{label}' failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Projects (public.projects)
    # ------------------------------------------------------------------

    async def create_project(
        self,
        user_id: str | UUID,
        *,
        title: str,
        mode: ProjectMode | str,
        aspect_ratio: AspectRatio | str = AspectRatio.PORTRAIT_9_16,
        source_mode: str | None = None,
        source_inputs: dict[str, Any] | None = None,
        pipeline_config: dict[str, Any] | None = None,
        credits_estimated: int = 0,
        batch_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Insert a new project row in PENDING status."""
        mode_val = mode.value if isinstance(mode, ProjectMode) else str(mode)
        ratio_val = aspect_ratio.value if isinstance(aspect_ratio, AspectRatio) else str(aspect_ratio)
        payload: dict[str, Any] = {
            "user_id": str(user_id),
            "title": title,
            "mode": mode_val,
            "status": ProjectStatus.PENDING.value,
            "aspect_ratio": ratio_val,
            "source_mode": source_mode,
            "source_inputs": source_inputs or {},
            "pipeline_config": pipeline_config or {},
            "credits_estimated": max(0, int(credits_estimated)),
        }
        if batch_id is not None:
            payload["batch_id"] = str(batch_id)

        response = await self._run(
            "create_project",
            lambda: self.client.table("projects").insert(payload).execute(),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise DatabaseError("Supabase insert into projects returned no row")
        return ProjectResponse.model_validate(rows[0])

    async def get_project(
        self,
        project_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Fetch a single project by id, optionally scoped to user_id."""

        def _query() -> Any:
            q = self.client.table("projects").select("*").eq("id", str(project_id))
            if user_id is not None:
                q = q.eq("user_id", str(user_id))
            return q.limit(1).execute()

        response = await self._run("get_project", _query)
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Project {project_id} not found")
        return ProjectResponse.model_validate(rows[0])

    async def get_project_detail(
        self,
        project_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> ProjectDetailResponse:
        """Fetch a project along with all ordered project_scenes."""
        project = await self.get_project(project_id, user_id=user_id)
        scenes = await self.list_scenes(project.id)
        return ProjectDetailResponse(**project.model_dump(), scenes=scenes)

    async def list_projects(
        self,
        user_id: str | UUID,
        *,
        status: ProjectStatus | str | None = None,
        mode: ProjectMode | str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProjectResponse]:
        """List projects owned by ``user_id`` ordered newest first."""
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))

        def _query() -> Any:
            q = (
                self.client.table("projects")
                .select("*")
                .eq("user_id", str(user_id))
                .order("created_at", desc=True)
                .range(safe_offset, safe_offset + safe_limit - 1)
            )
            if status is not None:
                status_val = status.value if isinstance(status, ProjectStatus) else str(status)
                q = q.eq("status", status_val)
            if mode is not None:
                mode_val = mode.value if isinstance(mode, ProjectMode) else str(mode)
                q = q.eq("mode", mode_val)
            return q.execute()

        response = await self._run("list_projects", _query)
        rows = _extract_data(response) or []
        return [ProjectResponse.model_validate(row) for row in rows]

    async def update_project(
        self,
        project_id: str | UUID,
        updates: dict[str, Any],
        user_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Update arbitrary columns on a project row and return the updated model."""
        if not updates:
            return await self.get_project(project_id, user_id=user_id)

        def _query() -> Any:
            q = self.client.table("projects").update(updates).eq("id", str(project_id))
            if user_id is not None:
                q = q.eq("user_id", str(user_id))
            return q.execute()

        response = await self._run("update_project", _query)
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Project {project_id} not found")
        return ProjectResponse.model_validate(rows[0])

    async def update_project_status(
        self,
        project_id: str | UUID,
        new_status: ProjectStatus | str,
        *,
        current_stage: str | None = None,
        progress_pct: int | None = None,
        error_message: str | None = None,
        output_video_url: str | None = None,
        duration_seconds: float | None = None,
        enforce_fsm: bool = True,
        user_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Transition a project status (enforcing VALID_STATUS_TRANSITIONS by default)."""
        target_status = (
            new_status if isinstance(new_status, ProjectStatus) else ProjectStatus(str(new_status))
        )

        if enforce_fsm:
            existing = await self.get_project(project_id, user_id=user_id)
            if not can_transition(existing.status, target_status):
                raise InvalidStateTransitionError(
                    f"Cannot transition project {project_id} from {existing.status.value} to {target_status.value}",
                    details={
                        "project_id": str(project_id),
                        "current_status": existing.status.value,
                        "target_status": target_status.value,
                    },
                )

        updates: dict[str, Any] = {"status": target_status.value}
        if current_stage is not None:
            updates["current_stage"] = current_stage
        if progress_pct is not None:
            updates["progress_pct"] = max(0, min(100, int(progress_pct)))
        if error_message is not None:
            updates["error_message"] = error_message
        if output_video_url is not None:
            updates["output_video_url"] = output_video_url
        if duration_seconds is not None:
            updates["duration_seconds"] = round(float(duration_seconds), 2)

        return await self.update_project(project_id, updates, user_id=user_id)

    async def update_project_progress(
        self,
        project_id: str | UUID,
        *,
        progress_pct: int,
        current_stage: str | None = None,
    ) -> ProjectResponse:
        """Update progress_pct and optional current_stage without changing status."""
        updates: dict[str, Any] = {"progress_pct": max(0, min(100, int(progress_pct)))}
        if current_stage is not None:
            updates["current_stage"] = current_stage
        return await self.update_project(project_id, updates)

    async def mark_temp_cleaned(self, project_id: str | UUID) -> None:
        """Record temp_cleaned_at timestamp on a project after temp/{job_id}/ removal."""
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await self.update_project(project_id, {"temp_cleaned_at": now_iso})
        except NotFoundError:
            logger.debug("Skipping temp_cleaned_at update; project %s not found", project_id)

    async def delete_project(
        self,
        project_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> None:
        """Delete a project (cascades to project_scenes)."""

        def _query() -> Any:
            q = self.client.table("projects").delete().eq("id", str(project_id))
            if user_id is not None:
                q = q.eq("user_id", str(user_id))
            return q.execute()

        response = await self._run("delete_project", _query)
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Project {project_id} not found")

    # ------------------------------------------------------------------
    # Scenes / Subtitles (public.project_scenes)
    # ------------------------------------------------------------------

    async def list_scenes(self, project_id: str | UUID) -> list[SceneResponse]:
        """Return all scenes for ``project_id`` ordered by scene_index."""
        response = await self._run(
            "list_scenes",
            lambda: (
                self.client.table("project_scenes")
                .select("*")
                .eq("project_id", str(project_id))
                .order("scene_index", desc=False)
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        return [SceneResponse.model_validate(row) for row in rows]

    async def get_scene(
        self,
        project_id: str | UUID,
        scene_id: str | UUID,
    ) -> SceneResponse:
        """Fetch a single scene belonging to ``project_id``."""
        response = await self._run(
            "get_scene",
            lambda: (
                self.client.table("project_scenes")
                .select("*")
                .eq("id", str(scene_id))
                .eq("project_id", str(project_id))
                .limit(1)
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")
        return SceneResponse.model_validate(rows[0])

    async def upsert_scenes(
        self,
        project_id: str | UUID,
        scenes: list[SceneCreate | dict[str, Any]],
    ) -> list[SceneResponse]:
        """Batch upsert scenes keyed by (project_id, scene_index)."""
        if not scenes:
            return []

        pid = str(project_id)
        payloads: list[dict[str, Any]] = []
        for item in scenes:
            if isinstance(item, SceneCreate):
                row = item.model_dump(mode="json")
            else:
                row = SceneCreate(**item).model_dump(mode="json")
            row["project_id"] = pid
            payloads.append(row)

        response = await self._run(
            "upsert_scenes",
            lambda: (
                self.client.table("project_scenes")
                .upsert(payloads, on_conflict="project_id,scene_index")
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        parsed = [SceneResponse.model_validate(row) for row in rows]
        parsed.sort(key=lambda s: s.scene_index)
        return parsed

    async def update_scene(
        self,
        project_id: str | UUID,
        scene_id: str | UUID,
        updates: SceneUpdateRequest | dict[str, Any],
    ) -> SceneResponse:
        """Apply partial HITL edits to a single scene."""
        if isinstance(updates, SceneUpdateRequest):
            payload = updates.model_dump(mode="json", exclude_unset=True)
        else:
            payload = SceneUpdateRequest(**updates).model_dump(mode="json", exclude_unset=True)

        if not payload:
            return await self.get_scene(project_id, scene_id)

        response = await self._run(
            "update_scene",
            lambda: (
                self.client.table("project_scenes")
                .update(payload)
                .eq("id", str(scene_id))
                .eq("project_id", str(project_id))
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")
        return SceneResponse.model_validate(rows[0])

    async def delete_project_scenes(self, project_id: str | UUID) -> None:
        """Delete all scenes belonging to ``project_id``."""
        await self._run(
            "delete_project_scenes",
            lambda: (
                self.client.table("project_scenes")
                .delete()
                .eq("project_id", str(project_id))
                .execute()
            ),
        )

    async def delete_scene(
        self,
        project_id: str | UUID,
        scene_id: str | UUID,
    ) -> None:
        """Delete a single scene belonging to ``project_id``."""
        response = await self._run(
            "delete_scene",
            lambda: (
                self.client.table("project_scenes")
                .delete()
                .eq("id", str(scene_id))
                .eq("project_id", str(project_id))
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")

    # ------------------------------------------------------------------
    # Profiles, Credits & Billing RPCs
    # ------------------------------------------------------------------

    async def get_profile(self, user_id: str | UUID) -> dict[str, Any]:
        """Fetch user profile including subscription_tier."""
        response = await self._run(
            "get_profile",
            lambda: (
                self.client.table("profiles")
                .select("*")
                .eq("id", str(user_id))
                .limit(1)
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Profile not found for user {user_id}")
        return dict(rows[0])

    async def update_profile(
        self,
        user_id: str | UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Update mutable fields on a user's profile row and return the updated dict."""
        if not updates:
            return await self.get_profile(user_id)

        response = await self._run(
            "update_profile",
            lambda: (
                self.client.table("profiles")
                .update(updates)
                .eq("id", str(user_id))
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Profile not found for user {user_id}")
        return dict(rows[0])

    async def get_user_credits(self, user_id: str | UUID) -> dict[str, Any]:
        """Fetch user_credits wallet record."""
        response = await self._run(
            "get_user_credits",
            lambda: (
                self.client.table("user_credits")
                .select("*")
                .eq("user_id", str(user_id))
                .limit(1)
                .execute()
            ),
        )
        rows = _extract_data(response) or []
        if not rows:
            raise NotFoundError(f"Credit wallet not found for user {user_id}")
        return dict(rows[0])

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
        params = {
            "p_user_id": str(user_id),
            "p_amount": int(amount),
            "p_project_id": str(project_id) if project_id is not None else None,
            "p_description": description,
            "p_metadata": metadata or {},
        }
        try:
            response = await asyncio.to_thread(
                lambda: self.client.rpc("deduct_user_credits", params).execute()
            )
        except Exception as exc:
            msg = str(exc)
            lowered = msg.lower()
            if "insufficient credits" in lowered or "p0001" in lowered:
                raise InsufficientCreditsError(
                    "Insufficient credits to start or render this project",
                    details={"user_id": str(user_id), "required": int(amount)},
                ) from exc
            if "credit wallet not found" in lowered:
                raise NotFoundError(f"Credit wallet not found for user {user_id}") from exc
            logger.exception("RPC deduct_user_credits failed for user=%s: %s", user_id, exc)
            raise DatabaseError(f"Failed to deduct user credits: {exc}") from exc

        data = _extract_data(response)
        if not isinstance(data, dict):
            raise DatabaseError("Unexpected RPC payload from deduct_user_credits")
        return data

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
        params = {
            "p_user_id": str(user_id),
            "p_amount": int(amount),
            "p_project_id": str(project_id) if project_id is not None else None,
            "p_description": description,
            "p_metadata": metadata or {},
        }
        try:
            response = await asyncio.to_thread(
                lambda: self.client.rpc("refund_user_credits", params).execute()
            )
        except Exception as exc:
            logger.exception("RPC refund_user_credits failed for user=%s: %s", user_id, exc)
            raise DatabaseError(f"Failed to refund user credits: {exc}") from exc

        data = _extract_data(response)
        if not isinstance(data, dict):
            raise DatabaseError("Unexpected RPC payload from refund_user_credits")
        return data

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
        """Invoke public.process_payment_webhook RPC for atomic, idempotent top-up."""
        params = {
            "p_reference_code": reference_code,
            "p_external_tx_id": external_tx_id,
            "p_amount_vnd": int(amount_vnd),
            "p_gateway": gateway,
            "p_credits_amount": int(credits_amount) if credits_amount is not None else None,
            "p_user_id": str(user_id) if user_id is not None else None,
            "p_raw_payload": raw_payload or {},
        }
        try:
            response = await asyncio.to_thread(
                lambda: self.client.rpc("process_payment_webhook", params).execute()
            )
        except Exception as exc:
            logger.exception(
                "RPC process_payment_webhook failed for ref=%s: %s", reference_code, exc
            )
            raise DatabaseError(f"Failed to process payment webhook: {exc}") from exc

        data = _extract_data(response)
        if not isinstance(data, dict):
            raise DatabaseError("Unexpected RPC payload from process_payment_webhook")
        return data

    async def upgrade_subscription_tier(
        self,
        user_id: str | UUID,
        target_tier: SubscriptionTier | str,
        credit_cost: int,
    ) -> dict[str, Any]:
        """Validate target tier, atomically deduct credits, and upgrade profiles.subscription_tier."""
        raw_tier = (
            target_tier.value
            if isinstance(target_tier, SubscriptionTier)
            else str(target_tier or "").strip().lower()
        )
        if raw_tier not in (SubscriptionTier.PRO.value, SubscriptionTier.ENTERPRISE.value):
            raise ValidationError(
                f"Invalid target subscription tier '{target_tier}'. Must be 'pro' or 'enterprise'.",
                details={"target_tier": str(target_tier)},
            )

        cost = int(credit_cost)
        if cost <= 0:
            raise ValidationError(
                f"credit_cost must be a positive integer, got {cost}",
                details={"credit_cost": cost},
            )

        deduct_result = await self.deduct_user_credits(
            user_id,
            cost,
            description=f"Upgrade subscription tier to {raw_tier}",
            metadata={"action": "tier_upgrade", "target_tier": raw_tier},
        )

        try:
            response = await self._run(
                "upgrade_subscription_tier",
                lambda: (
                    self.client.table("profiles")
                    .update({"subscription_tier": raw_tier})
                    .eq("id", str(user_id))
                    .execute()
                ),
            )
            rows = _extract_data(response) or []
            if not rows:
                raise NotFoundError(f"Profile not found for user {user_id}")
        except Exception:
            try:
                await self.refund_user_credits(
                    user_id,
                    cost,
                    description=f"Refund failed tier upgrade to {raw_tier}",
                    metadata={"action": "tier_upgrade_rollback", "target_tier": raw_tier},
                )
            except Exception:
                logger.exception(
                    "Failed compensating credit refund after tier upgrade failure for user=%s",
                    user_id,
                )
            raise

        logger.info(
            "Upgraded subscription tier for user=%s to %s (cost=%d, new_balance=%s)",
            user_id,
            raw_tier,
            cost,
            deduct_result.get("new_balance"),
        )
        return {
            "user_id": str(user_id),
            "subscription_tier": raw_tier,
            "credits_deducted": int(deduct_result.get("deducted", cost)),
            "new_balance": int(deduct_result.get("new_balance", 0)),
        }


ProjectRepository = SupabaseRepository
