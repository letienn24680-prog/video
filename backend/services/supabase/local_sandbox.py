"""Local JSON-backed sandbox repositories mirroring SupabaseRepository and SupabaseBillingRepository."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    AuthenticationError,
    DatabaseError,
    InsufficientCreditsError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.models.enums import (
    AspectRatio,
    CreditEntryType,
    PaymentGateway,
    PaymentStatus,
    ProjectMode,
    ProjectStatus,
    SubscriptionTier,
    can_transition,
)
from backend.models.project import ProjectDetailResponse, ProjectResponse
from backend.models.scene import SceneCreate, SceneResponse, SceneUpdateRequest

logger = get_logger(__name__)

SANDBOX_DEMO_USER_ID = "00000000-0000-0000-0000-000000000001"
SANDBOX_DEMO_EMAIL = "demo@local.studio"

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _get_path_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _normalize_json_dict(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data, default=_json_default))


def _empty_db() -> dict[str, Any]:
    return {
        "profiles": {},
        "user_credits": {},
        "projects": {},
        "project_scenes": {},
        "payment_transactions": {},
        "credit_ledger": [],
        "sandbox_users": {},
    }


def _ensure_db_shape(db: dict[str, Any]) -> bool:
    mutated = False
    for key, default in (
        ("profiles", {}),
        ("user_credits", {}),
        ("projects", {}),
        ("project_scenes", {}),
        ("payment_transactions", {}),
        ("credit_ledger", []),
        ("sandbox_users", {}),
    ):
        if key not in db or not isinstance(db[key], type(default)):
            db[key] = copy.deepcopy(default)
            mutated = True
    return mutated


class LocalSandboxRepository:
    """Thread-safe JSON-persisted local repository implementing the SupabaseRepository interface."""

    def __init__(
        self,
        settings: Settings | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = (
            Path(db_path)
            if db_path is not None
            else (self.settings.temp_path / "sandbox_db.json")
        )
        self._lock = asyncio.Lock()
        self._thread_lock = _get_path_lock(self.db_path)

    def _ensure_seeded(self, db: dict[str, Any]) -> bool:
        mutated = _ensure_db_shape(db)

        now_iso = _utc_now_iso()
        if SANDBOX_DEMO_USER_ID not in db["profiles"]:
            db["profiles"][SANDBOX_DEMO_USER_ID] = {
                "id": SANDBOX_DEMO_USER_ID,
                "email": SANDBOX_DEMO_EMAIL,
                "display_name": "Local Demo Creator",
                "avatar_url": None,
                "subscription_tier": SubscriptionTier.FREE.value,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            mutated = True

        if SANDBOX_DEMO_USER_ID not in db["user_credits"]:
            initial_credits = max(0, int(self.settings.SANDBOX_INITIAL_CREDITS))
            db["user_credits"][SANDBOX_DEMO_USER_ID] = {
                "user_id": SANDBOX_DEMO_USER_ID,
                "balance": initial_credits,
                "lifetime_purchased": 0,
                "lifetime_spent": 0,
                "updated_at": now_iso,
            }
            if initial_credits > 0:
                db["credit_ledger"].append(
                    {
                        "id": str(uuid4()),
                        "user_id": SANDBOX_DEMO_USER_ID,
                        "entry_type": CreditEntryType.SIGNUP_BONUS.value,
                        "amount": initial_credits,
                        "balance_after": initial_credits,
                        "project_id": None,
                        "transaction_id": None,
                        "description": "Initial local sandbox demo credits",
                        "metadata": {"source": "local_sandbox_seed"},
                        "created_at": now_iso,
                    }
                )
            mutated = True

        return mutated

    def _load_db_sync(self) -> dict[str, Any]:
        with self._thread_lock:
            if self.db_path.is_file():
                try:
                    raw = self.db_path.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else _empty_db()
                    if not isinstance(data, dict):
                        data = _empty_db()
                except Exception as exc:
                    logger.exception("Failed to load sandbox DB from %s: %s", self.db_path, exc)
                    raise DatabaseError(f"Failed to read local sandbox DB: {exc}") from exc
            else:
                data = _empty_db()

            if self._ensure_seeded(data) or not self.db_path.is_file():
                self._save_db_sync(data)
            return data

    def _save_db_sync(self, db: dict[str, Any]) -> None:
        with self._thread_lock:
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self.db_path.with_name(f"{self.db_path.name}.{uuid4().hex}.tmp")
                serialized = json.dumps(db, indent=2, ensure_ascii=False, default=_json_default)
                tmp_path.write_text(serialized, encoding="utf-8")
                os.replace(tmp_path, self.db_path)
            except Exception as exc:
                logger.exception("Failed to save sandbox DB to %s: %s", self.db_path, exc)
                raise DatabaseError(f"Failed to write local sandbox DB: {exc}") from exc

    async def _load_db(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._load_db_sync)

    async def _save_db(self, db: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_db_sync, db)

    @staticmethod
    def _deduct_in_db(
        db: dict[str, Any],
        user_id: str | UUID,
        amount: int,
        *,
        project_id: str | UUID | None = None,
        description: str = "Video processing credit deduction",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        uid = str(user_id)
        p_amount = int(amount)
        if p_amount <= 0:
            raise ValidationError(
                f"amount must be a positive integer, got {p_amount}",
                details={"amount": p_amount},
            )

        wallet = db["user_credits"].get(uid)
        if wallet is None:
            raise NotFoundError(f"Credit wallet not found for user {user_id}")

        prev_balance = int(wallet.get("balance", 0))
        if prev_balance < p_amount:
            raise InsufficientCreditsError(
                "Insufficient credits to start or render this project",
                details={"user_id": uid, "required": p_amount, "balance": prev_balance},
            )

        now_iso = _utc_now_iso()
        new_balance = prev_balance - p_amount
        wallet["balance"] = new_balance
        wallet["lifetime_spent"] = int(wallet.get("lifetime_spent", 0)) + p_amount
        wallet["updated_at"] = now_iso

        if project_id is not None:
            proj = db["projects"].get(str(project_id))
            if proj is not None and proj.get("user_id") == uid:
                proj["credits_charged"] = int(proj.get("credits_charged", 0)) + p_amount
                proj["updated_at"] = now_iso

        ledger_id = str(uuid4())
        db["credit_ledger"].append(
            {
                "id": ledger_id,
                "user_id": uid,
                "entry_type": CreditEntryType.DEDUCTION.value,
                "amount": -p_amount,
                "balance_after": new_balance,
                "project_id": str(project_id) if project_id is not None else None,
                "transaction_id": None,
                "description": description or "Video processing credit deduction",
                "metadata": _normalize_json_dict(metadata or {}),
                "created_at": now_iso,
            }
        )

        return {
            "status": "deducted",
            "user_id": uid,
            "deducted": p_amount,
            "previous_balance": prev_balance,
            "new_balance": new_balance,
            "ledger_id": ledger_id,
        }

    # ------------------------------------------------------------------
    # Projects
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
        now_iso = _utc_now_iso()
        project_id = str(uuid4())
        row: dict[str, Any] = {
            "id": project_id,
            "user_id": str(user_id),
            "title": title,
            "mode": mode_val,
            "status": ProjectStatus.PENDING.value,
            "aspect_ratio": ratio_val,
            "source_mode": source_mode,
            "source_inputs": _normalize_json_dict(source_inputs or {}),
            "pipeline_config": _normalize_json_dict(pipeline_config or {}),
            "progress_pct": 0,
            "current_stage": None,
            "output_video_url": None,
            "duration_seconds": None,
            "credits_estimated": max(0, int(credits_estimated)),
            "credits_charged": 0,
            "error_message": None,
            "batch_id": str(batch_id) if batch_id is not None else None,
            "temp_cleaned_at": None,
            "expires_at": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        async with self._lock:
            db = await self._load_db()
            db["projects"][project_id] = row
            await self._save_db(db)

        return ProjectResponse.model_validate(row)

    async def get_project(
        self,
        project_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Fetch a single project by id, optionally scoped to user_id."""
        pid = str(project_id)
        async with self._lock:
            db = await self._load_db()
            row = db["projects"].get(pid)
            if row is None or (user_id is not None and row.get("user_id") != str(user_id)):
                raise NotFoundError(f"Project {project_id} not found")
            return ProjectResponse.model_validate(copy.deepcopy(row))

    async def get_project_detail(
        self,
        project_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> ProjectDetailResponse:
        """Fetch a project along with all ordered project_scenes."""
        pid = str(project_id)
        async with self._lock:
            db = await self._load_db()
            row = db["projects"].get(pid)
            if row is None or (user_id is not None and row.get("user_id") != str(user_id)):
                raise NotFoundError(f"Project {project_id} not found")
            project = ProjectResponse.model_validate(copy.deepcopy(row))
            scene_rows = [
                copy.deepcopy(s)
                for s in db["project_scenes"].values()
                if s.get("project_id") == pid
            ]
            scene_rows.sort(key=lambda s: int(s.get("scene_index", 0)))
            scenes = [SceneResponse.model_validate(s) for s in scene_rows]
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
        uid = str(user_id)
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        status_val = (
            (status.value if isinstance(status, ProjectStatus) else str(status))
            if status is not None
            else None
        )
        mode_val = (
            (mode.value if isinstance(mode, ProjectMode) else str(mode))
            if mode is not None
            else None
        )

        async with self._lock:
            db = await self._load_db()
            rows = [
                copy.deepcopy(r)
                for r in db["projects"].values()
                if r.get("user_id") == uid
                and (status_val is None or r.get("status") == status_val)
                and (mode_val is None or r.get("mode") == mode_val)
            ]

        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        sliced = rows[safe_offset : safe_offset + safe_limit]
        return [ProjectResponse.model_validate(row) for row in sliced]

    async def update_project(
        self,
        project_id: str | UUID,
        updates: dict[str, Any],
        user_id: str | UUID | None = None,
    ) -> ProjectResponse:
        """Update arbitrary columns on a project row and return the updated model."""
        if not updates:
            return await self.get_project(project_id, user_id=user_id)

        pid = str(project_id)
        clean_updates = _normalize_json_dict(updates)
        clean_updates.setdefault("updated_at", _utc_now_iso())

        async with self._lock:
            db = await self._load_db()
            row = db["projects"].get(pid)
            if row is None or (user_id is not None and row.get("user_id") != str(user_id)):
                raise NotFoundError(f"Project {project_id} not found")
            row.update(clean_updates)
            row["updated_at"] = _utc_now_iso()
            await self._save_db(db)
            return ProjectResponse.model_validate(copy.deepcopy(row))

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
        pid = str(project_id)

        async with self._lock:
            db = await self._load_db()
            row = db["projects"].get(pid)
            if row is None or (user_id is not None and row.get("user_id") != str(user_id)):
                raise NotFoundError(f"Project {project_id} not found")

            if enforce_fsm:
                current_status = ProjectStatus(str(row["status"]))
                if not can_transition(current_status, target_status):
                    raise InvalidStateTransitionError(
                        f"Cannot transition project {project_id} from {current_status.value} to {target_status.value}",
                        details={
                            "project_id": pid,
                            "current_status": current_status.value,
                            "target_status": target_status.value,
                        },
                    )

            row["status"] = target_status.value
            if current_stage is not None:
                row["current_stage"] = current_stage
            if progress_pct is not None:
                row["progress_pct"] = max(0, min(100, int(progress_pct)))
            if error_message is not None:
                row["error_message"] = error_message
            if output_video_url is not None:
                row["output_video_url"] = output_video_url
            if duration_seconds is not None:
                row["duration_seconds"] = round(float(duration_seconds), 2)
            row["updated_at"] = _utc_now_iso()

            await self._save_db(db)
            return ProjectResponse.model_validate(copy.deepcopy(row))

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
        now_iso = _utc_now_iso()
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
        pid = str(project_id)
        async with self._lock:
            db = await self._load_db()
            row = db["projects"].get(pid)
            if row is None or (user_id is not None and row.get("user_id") != str(user_id)):
                raise NotFoundError(f"Project {project_id} not found")
            del db["projects"][pid]
            scene_ids = [
                sid for sid, s in db["project_scenes"].items() if s.get("project_id") == pid
            ]
            for sid in scene_ids:
                del db["project_scenes"][sid]
            await self._save_db(db)

    # ------------------------------------------------------------------
    # Scenes / Subtitles
    # ------------------------------------------------------------------

    async def list_scenes(self, project_id: str | UUID) -> list[SceneResponse]:
        """Return all scenes for ``project_id`` ordered by scene_index."""
        pid = str(project_id)
        async with self._lock:
            db = await self._load_db()
            rows = [
                copy.deepcopy(s)
                for s in db["project_scenes"].values()
                if s.get("project_id") == pid
            ]
        rows.sort(key=lambda s: int(s.get("scene_index", 0)))
        return [SceneResponse.model_validate(row) for row in rows]

    async def get_scene(
        self,
        project_id: str | UUID,
        scene_id: str | UUID,
    ) -> SceneResponse:
        """Fetch a single scene belonging to ``project_id``."""
        pid = str(project_id)
        sid = str(scene_id)
        async with self._lock:
            db = await self._load_db()
            row = db["project_scenes"].get(sid)
            if row is None or row.get("project_id") != pid:
                raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")
            return SceneResponse.model_validate(copy.deepcopy(row))

    async def upsert_scenes(
        self,
        project_id: str | UUID,
        scenes: list[SceneCreate | dict[str, Any]],
    ) -> list[SceneResponse]:
        """Batch upsert scenes keyed by (project_id, scene_index)."""
        if not scenes:
            return []

        pid = str(project_id)
        now_iso = _utc_now_iso()
        parsed_inputs: list[dict[str, Any]] = []
        for item in scenes:
            if isinstance(item, SceneCreate):
                row = item.model_dump(mode="json")
            else:
                row = SceneCreate(**item).model_dump(mode="json")
            row["project_id"] = pid
            parsed_inputs.append(row)

        async with self._lock:
            db = await self._load_db()
            existing_by_index: dict[int, str] = {
                int(s["scene_index"]): sid
                for sid, s in db["project_scenes"].items()
                if s.get("project_id") == pid
            }

            result_rows: list[dict[str, Any]] = []
            for row in parsed_inputs:
                idx = int(row["scene_index"])
                existing_id = existing_by_index.get(idx)
                if existing_id is not None:
                    existing_row = db["project_scenes"][existing_id]
                    existing_row.update(row)
                    existing_row["updated_at"] = now_iso
                    result_rows.append(copy.deepcopy(existing_row))
                else:
                    new_id = str(uuid4())
                    new_row = {
                        **row,
                        "id": new_id,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    db["project_scenes"][new_id] = new_row
                    existing_by_index[idx] = new_id
                    result_rows.append(copy.deepcopy(new_row))

            await self._save_db(db)

        parsed = [SceneResponse.model_validate(r) for r in result_rows]
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

        pid = str(project_id)
        sid = str(scene_id)
        async with self._lock:
            db = await self._load_db()
            row = db["project_scenes"].get(sid)
            if row is None or row.get("project_id") != pid:
                raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")
            row.update(payload)
            row["updated_at"] = _utc_now_iso()
            await self._save_db(db)
            return SceneResponse.model_validate(copy.deepcopy(row))

    async def delete_project_scenes(self, project_id: str | UUID) -> None:
        """Delete all scenes belonging to ``project_id``."""
        pid = str(project_id)
        async with self._lock:
            db = await self._load_db()
            scene_ids = [
                sid for sid, s in db["project_scenes"].items() if s.get("project_id") == pid
            ]
            if scene_ids:
                for sid in scene_ids:
                    del db["project_scenes"][sid]
                await self._save_db(db)

    async def delete_scene(
        self,
        project_id: str | UUID,
        scene_id: str | UUID,
    ) -> None:
        """Delete a single scene belonging to ``project_id``."""
        pid = str(project_id)
        sid = str(scene_id)
        async with self._lock:
            db = await self._load_db()
            row = db["project_scenes"].get(sid)
            if row is None or row.get("project_id") != pid:
                raise NotFoundError(f"Scene {scene_id} not found for project {project_id}")
            del db["project_scenes"][sid]
            await self._save_db(db)

    # ------------------------------------------------------------------
    # Profiles, Credits & Billing Operations
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_email(email: str) -> str:
        clean_email = (email or "").strip().lower()
        if (
            not clean_email
            or " " in clean_email
            or "@" not in clean_email
            or "." not in clean_email.split("@", 1)[1]
        ):
            raise ValidationError("Invalid email address", details={"email": email})
        return clean_email

    @staticmethod
    def _hash_password(password: str, salt_hex: str, iterations: int = 100_000) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
        ).hex()

    async def register_local_account(self, email: str, password: str) -> dict[str, Any]:
        """Register a new local sandbox account with PBKDF2-hashed password and initial credits."""
        clean_email = self._normalize_email(email)
        if not isinstance(password, str) or len(password) < 6:
            raise ValidationError("Password must be at least 6 characters")

        now_iso = _utc_now_iso()
        user_id = str(uuid4())
        display_name = clean_email.split("@", 1)[0] or "Creator"
        salt_hex = secrets.token_hex(16)
        iterations = 100_000
        password_hash = self._hash_password(password, salt_hex, iterations)

        async with self._lock:
            db = await self._load_db()
            if clean_email in db["sandbox_users"]:
                raise ValidationError(
                    f"An account with email '{clean_email}' already exists",
                    code="EMAIL_ALREADY_EXISTS",
                    status_code=409,
                    details={"email": clean_email},
                )

            db["sandbox_users"][clean_email] = {
                "user_id": user_id,
                "email": clean_email,
                "password_salt": salt_hex,
                "password_hash": password_hash,
                "iterations": iterations,
                "created_at": now_iso,
            }
            db["profiles"][user_id] = {
                "id": user_id,
                "email": clean_email,
                "display_name": display_name,
                "avatar_url": None,
                "subscription_tier": SubscriptionTier.FREE.value,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            initial_credits = max(0, int(self.settings.SANDBOX_INITIAL_CREDITS))
            db["user_credits"][user_id] = {
                "user_id": user_id,
                "balance": initial_credits,
                "lifetime_purchased": 0,
                "lifetime_spent": 0,
                "updated_at": now_iso,
            }
            if initial_credits > 0:
                db["credit_ledger"].append(
                    {
                        "id": str(uuid4()),
                        "user_id": user_id,
                        "entry_type": CreditEntryType.SIGNUP_BONUS.value,
                        "amount": initial_credits,
                        "balance_after": initial_credits,
                        "project_id": None,
                        "transaction_id": None,
                        "description": "Initial local sandbox account credits",
                        "metadata": {"source": "register_local_account"},
                        "created_at": now_iso,
                    }
                )
            await self._save_db(db)

        return {
            "id": user_id,
            "user_id": user_id,
            "email": clean_email,
            "display_name": display_name,
            "subscription_tier": SubscriptionTier.FREE.value,
            "created_at": now_iso,
        }

    async def login_local_account(self, email: str, password: str) -> dict[str, Any]:
        """Verify local sandbox email/password credentials and return the user profile summary."""
        clean_email = (email or "").strip().lower()
        if not clean_email or not isinstance(password, str) or not password:
            raise AuthenticationError("Invalid email or password")

        async with self._lock:
            db = await self._load_db()
            acct = db["sandbox_users"].get(clean_email)
            if acct is None:
                raise AuthenticationError("Invalid email or password")

            salt_hex = str(acct.get("password_salt") or "")
            stored_hash = str(acct.get("password_hash") or "")
            iterations = int(acct.get("iterations") or 100_000)
            if not salt_hex or not stored_hash:
                raise AuthenticationError("Invalid email or password")

            candidate_hash = self._hash_password(password, salt_hex, iterations)
            if not hmac.compare_digest(candidate_hash, stored_hash):
                raise AuthenticationError("Invalid email or password")

            user_id = str(acct["user_id"])
            profile = db["profiles"].get(user_id) or {}

        return {
            "id": user_id,
            "user_id": user_id,
            "email": clean_email,
            "display_name": profile.get("display_name") or clean_email.split("@", 1)[0],
            "subscription_tier": profile.get("subscription_tier", SubscriptionTier.FREE.value),
            "created_at": profile.get("created_at") or acct.get("created_at"),
        }

    async def get_profile(self, user_id: str | UUID) -> dict[str, Any]:
        """Fetch user profile including subscription_tier."""
        uid = str(user_id)
        async with self._lock:
            db = await self._load_db()
            profile = db["profiles"].get(uid)
            if profile is None:
                raise NotFoundError(f"Profile not found for user {user_id}")
            return copy.deepcopy(profile)

    async def update_profile(
        self,
        user_id: str | UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Update mutable fields on a user's profile row and return the updated dict."""
        if not updates:
            return await self.get_profile(user_id)

        uid = str(user_id)
        clean_updates = _normalize_json_dict(updates)
        async with self._lock:
            db = await self._load_db()
            profile = db["profiles"].get(uid)
            if profile is None:
                raise NotFoundError(f"Profile not found for user {user_id}")
            profile.update(clean_updates)
            profile["updated_at"] = _utc_now_iso()
            await self._save_db(db)
            return copy.deepcopy(profile)

    async def get_user_credits(self, user_id: str | UUID) -> dict[str, Any]:
        """Fetch user_credits wallet record."""
        uid = str(user_id)
        async with self._lock:
            db = await self._load_db()
            wallet = db["user_credits"].get(uid)
            if wallet is None:
                raise NotFoundError(f"Credit wallet not found for user {user_id}")
            return copy.deepcopy(wallet)

    async def deduct_user_credits(
        self,
        user_id: str | UUID,
        amount: int,
        *,
        project_id: str | UUID | None = None,
        description: str = "Video processing credit deduction",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically deduct credits and append an immutable credit_ledger entry."""
        async with self._lock:
            db = await self._load_db()
            result = self._deduct_in_db(
                db,
                user_id,
                amount,
                project_id=project_id,
                description=description,
                metadata=metadata,
            )
            await self._save_db(db)
            return result

    async def refund_user_credits(
        self,
        user_id: str | UUID,
        amount: int,
        *,
        project_id: str | UUID | None = None,
        description: str = "Credit refund",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically refund credits and append a credit_ledger refund entry."""
        uid = str(user_id)
        p_amount = int(amount)
        if p_amount <= 0:
            raise ValidationError(
                f"amount must be a positive integer, got {p_amount}",
                details={"amount": p_amount},
            )

        now_iso = _utc_now_iso()
        async with self._lock:
            db = await self._load_db()
            wallet = db["user_credits"].get(uid)
            if wallet is None:
                wallet = {
                    "user_id": uid,
                    "balance": 0,
                    "lifetime_purchased": 0,
                    "lifetime_spent": 0,
                    "updated_at": now_iso,
                }
                db["user_credits"][uid] = wallet

            prev_balance = int(wallet.get("balance", 0))
            new_balance = prev_balance + p_amount
            wallet["balance"] = new_balance
            wallet["lifetime_spent"] = max(0, int(wallet.get("lifetime_spent", 0)) - p_amount)
            wallet["updated_at"] = now_iso

            if project_id is not None:
                proj = db["projects"].get(str(project_id))
                if proj is not None and proj.get("user_id") == uid:
                    proj["credits_charged"] = max(
                        0, int(proj.get("credits_charged", 0)) - p_amount
                    )
                    proj["updated_at"] = now_iso

            ledger_id = str(uuid4())
            db["credit_ledger"].append(
                {
                    "id": ledger_id,
                    "user_id": uid,
                    "entry_type": CreditEntryType.REFUND.value,
                    "amount": p_amount,
                    "balance_after": new_balance,
                    "project_id": str(project_id) if project_id is not None else None,
                    "transaction_id": None,
                    "description": description or "Credit refund",
                    "metadata": _normalize_json_dict(metadata or {}),
                    "created_at": now_iso,
                }
            )
            await self._save_db(db)

        return {
            "status": "refunded",
            "user_id": uid,
            "refunded": p_amount,
            "previous_balance": prev_balance,
            "new_balance": new_balance,
            "ledger_id": ledger_id,
        }

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
        """Atomically and idempotently settle a bank transfer webhook in sandbox storage."""
        clean_ref = (reference_code or "").strip().upper()
        clean_ext = (external_tx_id or "").strip()
        vnd = int(amount_vnd)

        if not clean_ref:
            raise ValidationError("reference_code is required")
        if not clean_ext:
            raise ValidationError("external_tx_id is required for webhook idempotency")
        if vnd <= 0:
            raise ValidationError(f"amount_vnd must be positive, got {vnd}")

        now_iso = _utc_now_iso()
        resolved_gateway = (gateway or "").strip() or PaymentGateway.SEPAY.value

        async with self._lock:
            db = await self._load_db()
            txs: dict[str, dict[str, Any]] = db["payment_transactions"]

            # 1) Idempotency check by external_tx_id
            for existing in txs.values():
                if existing.get("external_tx_id") == clean_ext:
                    if existing.get("status") == PaymentStatus.COMPLETED.value:
                        return {
                            "status": "already_processed",
                            "idempotent": True,
                            "transaction_id": existing["id"],
                            "user_id": existing["user_id"],
                            "credits_added": 0,
                        }
                    if existing.get("reference_code") != clean_ref:
                        raise DatabaseError(
                            f"external_tx_id {clean_ext} is already bound to a different reference_code"
                        )

            # 2) Lookup by reference_code
            matched_tx: dict[str, Any] | None = None
            for existing in txs.values():
                if existing.get("reference_code") == clean_ref:
                    matched_tx = existing
                    break

            if matched_tx is not None:
                if matched_tx.get("status") == PaymentStatus.COMPLETED.value:
                    return {
                        "status": "already_processed",
                        "idempotent": True,
                        "transaction_id": matched_tx["id"],
                        "user_id": matched_tx["user_id"],
                        "credits_added": 0,
                    }

                expected_vnd = int(matched_tx.get("amount_vnd", 0))
                if vnd < expected_vnd:
                    raise ValidationError(
                        f"Transferred amount ({vnd} VND) is less than expected ({expected_vnd} VND)"
                    )

                tx_id = matched_tx["id"]
                target_user = str(user_id) if user_id is not None else str(matched_tx["user_id"])
                credits = (
                    int(credits_amount)
                    if credits_amount is not None
                    else int(matched_tx["credits_amount"])
                )
                matched_tx["external_tx_id"] = clean_ext
                matched_tx["gateway"] = resolved_gateway or matched_tx.get("gateway", "sepay")
                matched_tx["amount_vnd"] = vnd
                matched_tx["credits_amount"] = credits
                matched_tx["status"] = PaymentStatus.COMPLETED.value
                if raw_payload is not None:
                    matched_tx["raw_payload"] = _normalize_json_dict(raw_payload)
                matched_tx["completed_at"] = now_iso
                matched_tx["updated_at"] = now_iso
            else:
                if user_id is None or credits_amount is None or int(credits_amount) <= 0:
                    raise NotFoundError(
                        f"No pending transaction for reference_code {clean_ref}, and user_id/credits_amount were not provided"
                    )
                tx_id = str(uuid4())
                target_user = str(user_id)
                credits = int(credits_amount)
                txs[tx_id] = {
                    "id": tx_id,
                    "user_id": target_user,
                    "gateway": resolved_gateway,
                    "reference_code": clean_ref,
                    "external_tx_id": clean_ext,
                    "amount_vnd": vnd,
                    "credits_amount": credits,
                    "status": PaymentStatus.COMPLETED.value,
                    "raw_payload": _normalize_json_dict(raw_payload or {}),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "completed_at": now_iso,
                }

            # 3) Atomically credit user wallet
            wallet = db["user_credits"].get(target_user)
            if wallet is None:
                wallet = {
                    "user_id": target_user,
                    "balance": 0,
                    "lifetime_purchased": 0,
                    "lifetime_spent": 0,
                    "updated_at": now_iso,
                }
                db["user_credits"][target_user] = wallet

            prev_balance = int(wallet.get("balance", 0))
            new_balance = prev_balance + credits
            wallet["balance"] = new_balance
            wallet["lifetime_purchased"] = int(wallet.get("lifetime_purchased", 0)) + credits
            wallet["updated_at"] = now_iso

            # 4) Append immutable credit_ledger entry
            ledger_id = str(uuid4())
            db["credit_ledger"].append(
                {
                    "id": ledger_id,
                    "user_id": target_user,
                    "entry_type": CreditEntryType.TOPUP.value,
                    "amount": credits,
                    "balance_after": new_balance,
                    "project_id": None,
                    "transaction_id": tx_id,
                    "description": f"Top-up via {resolved_gateway} ({clean_ref})",
                    "metadata": {
                        "gateway": resolved_gateway,
                        "reference_code": clean_ref,
                        "external_tx_id": clean_ext,
                        "amount_vnd": vnd,
                    },
                    "created_at": now_iso,
                }
            )
            await self._save_db(db)

        return {
            "status": "completed",
            "idempotent": False,
            "transaction_id": tx_id,
            "user_id": target_user,
            "credits_added": credits,
            "previous_balance": prev_balance,
            "new_balance": new_balance,
            "ledger_id": ledger_id,
        }

    async def upgrade_subscription_tier(
        self,
        user_id: str | UUID,
        target_tier: SubscriptionTier | str,
        credit_cost: int,
    ) -> dict[str, Any]:
        """Validate target tier, atomically deduct credits, and update profile subscription_tier."""
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

        uid = str(user_id)
        async with self._lock:
            db = await self._load_db()
            profile = db["profiles"].get(uid)
            if profile is None:
                raise NotFoundError(f"Profile not found for user {user_id}")

            deduct_result = self._deduct_in_db(
                db,
                uid,
                cost,
                description=f"Upgrade subscription tier to {raw_tier}",
                metadata={"action": "tier_upgrade", "target_tier": raw_tier},
            )
            profile["subscription_tier"] = raw_tier
            profile["updated_at"] = _utc_now_iso()
            await self._save_db(db)

        return {
            "user_id": uid,
            "subscription_tier": raw_tier,
            "credits_deducted": int(deduct_result["deducted"]),
            "new_balance": int(deduct_result["new_balance"]),
        }


class LocalSandboxBillingRepository:
    """Local JSON-backed billing repository implementing the SupabaseBillingRepository interface."""

    def __init__(
        self,
        repository: LocalSandboxRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        if repository is not None:
            self._repository = repository
            self.settings = settings or repository.settings
        else:
            self.settings = settings or get_settings()
            self._repository = LocalSandboxRepository(settings=self.settings)

    @property
    def repository(self) -> LocalSandboxRepository:
        """Return the shared LocalSandboxRepository instance."""
        return self._repository

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
        """Insert a new pending top-up transaction into sandbox payment_transactions."""
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

        now_iso = _utc_now_iso()
        tx_id = str(uuid4())
        tx_row: dict[str, Any] = {
            "id": tx_id,
            "user_id": str(user_id),
            "gateway": gateway_val,
            "reference_code": clean_ref,
            "external_tx_id": None,
            "amount_vnd": int(amount_vnd),
            "credits_amount": int(credits_amount),
            "status": PaymentStatus.PENDING.value,
            "raw_payload": _normalize_json_dict(raw_payload or {}),
            "created_at": now_iso,
            "updated_at": now_iso,
            "completed_at": None,
        }

        async with self.repository._lock:
            db = await self.repository._load_db()
            for existing in db["payment_transactions"].values():
                if existing.get("reference_code") == clean_ref:
                    raise DatabaseError(
                        f"Duplicate payment reference_code '{clean_ref}'"
                    )
            db["payment_transactions"][tx_id] = tx_row
            await self.repository._save_db(db)

        return copy.deepcopy(tx_row)

    async def get_transaction_by_id(
        self,
        transaction_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Fetch a single payment_transactions row by id, optionally scoped to user_id."""
        tid = str(transaction_id)
        async with self.repository._lock:
            db = await self.repository._load_db()
            tx = db["payment_transactions"].get(tid)
            if tx is None or (user_id is not None and tx.get("user_id") != str(user_id)):
                raise NotFoundError(f"Payment transaction {transaction_id} not found")
            return copy.deepcopy(tx)

    async def get_transaction_by_reference(
        self,
        reference_code: str,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a payment_transactions row by reference_code, or None if not found."""
        clean_ref = (reference_code or "").strip().upper()
        if not clean_ref:
            return None

        async with self.repository._lock:
            db = await self.repository._load_db()
            for tx in db["payment_transactions"].values():
                if tx.get("reference_code") == clean_ref:
                    if user_id is not None and tx.get("user_id") != str(user_id):
                        return None
                    return copy.deepcopy(tx)
            return None

    async def list_transactions(
        self,
        user_id: str | UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List payment_transactions rows for user_id ordered newest created_at first."""
        uid = str(user_id)
        safe_limit = max(1, min(int(limit), 200))

        async with self.repository._lock:
            db = await self.repository._load_db()
            rows = [
                copy.deepcopy(tx)
                for tx in db["payment_transactions"].values()
                if tx.get("user_id") == uid
            ]

        rows.sort(key=lambda tx: str(tx.get("created_at", "")), reverse=True)
        return rows[:safe_limit]

    async def list_credit_ledger(
        self,
        user_id: str | UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List immutable credit_ledger audit entries for user_id ordered newest first."""
        uid = str(user_id)
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))

        async with self.repository._lock:
            db = await self.repository._load_db()
            entries = [
                copy.deepcopy(entry)
                for entry in db["credit_ledger"]
                if entry.get("user_id") == uid
            ]

        entries.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
        return entries[safe_offset : safe_offset + safe_limit]

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
        """Atomically deduct credits and record a credit_ledger entry."""
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
        """Atomically refund credits and record a credit_ledger entry."""
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
        """Settle a bank transfer idempotently in sandbox storage."""
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
