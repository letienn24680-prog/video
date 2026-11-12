"""Stage 6 Web App Essentials integration tests (Out-of-the-box config, Project/Scene CRUD, Billing History, Account Profile & System Diagnostics)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.main import create_app
from backend.models.enums import AspectRatio, ProjectMode, ProjectStatus
from backend.models.project import CreateFacelessProjectRequest, PipelineConfig
from backend.models.scene import SceneCreate

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _sandbox_settings(tmp_path: Path, **overrides: object) -> Settings:
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, object] = {
        "_env_file": None,
        "APP_ENV": "development",
        "TEMP_DIR": str(temp_dir),
        "AUTO_CLEANUP_ENABLED": False,
        "LOCAL_SANDBOX_MODE": True,
        "SANDBOX_INITIAL_CREDITS": 500,
        "PRO_UPGRADE_CREDITS": 500,
        "ENTERPRISE_UPGRADE_CREDITS": 1500,
        "STORAGE_PROVIDER": "local",
        "VIETQR_BANK_BIN": "970422",
        "VIETQR_ACCOUNT_NO": "0123456789",
        "VIETQR_ACCOUNT_NAME": "AI VIDEO STUDIO",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_out_of_box_files_and_stage6_static_serving(tmp_path: Path) -> None:
    """Verify .env, launcher scripts, index.html markers, and Stage 6 JS views are served."""
    assert (_PROJECT_ROOT / ".env").is_file()
    assert (_PROJECT_ROOT / "start_studio.ps1").is_file()
    assert (_PROJECT_ROOT / "start_studio.bat").is_file()

    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)

    with TestClient(app) as client:
        index_resp = client.get("/")
        assert index_resp.status_code == 200
        html = index_resp.text
        assert 'data-nav="billing"' in html
        assert 'data-nav="settings"' in html
        assert 'id="project-detail-modal-root"' in html
        assert 'id="studio-dialog-modal-root"' in html

        for js_path in (
            "/js/views/billing_view.js",
            "/js/views/settings_view.js",
        ):
            resp = client.get(js_path)
            assert resp.status_code == 200


def test_project_crud_lifecycle_in_local_sandbox(tmp_path: Path) -> None:
    """Verify create -> rename -> duplicate -> cancel -> retry -> delete + running delete guard."""
    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        app.state.job_queue.enqueue = AsyncMock()

        create_payload = CreateFacelessProjectRequest(
            title="Initial Studio Project",
            source_mode="topic",
            topic="Local sandbox lifecycle test",
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
        ).model_dump(mode="json")

        created = client.post("/api/v1/projects", json=create_payload, headers=headers)
        assert created.status_code == 201
        project_id = created.json()["id"]
        assert created.json()["title"] == "Initial Studio Project"

        # PATCH rename + aspect_ratio
        renamed = client.patch(
            f"/api/v1/projects/{project_id}",
            json={"title": "Renamed Studio Project", "aspect_ratio": "16:9"},
            headers=headers,
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Renamed Studio Project"
        assert renamed.json()["aspect_ratio"] == "16:9"

        # POST duplicate -> creates a new queued copy
        duplicated = client.post(
            f"/api/v1/projects/{project_id}/duplicate",
            json={},
            headers=headers,
        )
        assert duplicated.status_code == 201
        dup_id = duplicated.json()["id"]
        assert dup_id != project_id
        assert duplicated.json()["title"] == "Renamed Studio Project (Bản sao)"
        assert duplicated.json()["aspect_ratio"] == "16:9"
        assert duplicated.json()["status"] == ProjectStatus.PENDING.value

        # POST cancel -> CANCELLED
        cancelled = client.post(f"/api/v1/projects/{project_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == ProjectStatus.CANCELLED.value

        # POST retry -> resets back to PENDING and re-enqueues prepare
        retried = client.post(f"/api/v1/projects/{project_id}/retry", headers=headers)
        assert retried.status_code == 200
        assert retried.json()["status"] == ProjectStatus.PENDING.value
        assert retried.json()["progress_pct"] == 0
        assert retried.json()["current_stage"] == "queued"

        # Active PROCESSING / RENDERING delete must be rejected with 409
        asyncio.run(
            app.state.repository.update_project_status(
                project_id,
                ProjectStatus.PROCESSING,
                enforce_fsm=True,
            )
        )
        blocked_delete = client.delete(f"/api/v1/projects/{project_id}", headers=headers)
        assert blocked_delete.status_code == 409

        # Cancel again, verify temp dir removal on DELETE
        job_dir = cfg.temp_path / str(project_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "temp_clip.mp4").write_bytes(b"temp")

        cancelled_again = client.post(f"/api/v1/projects/{project_id}/cancel", headers=headers)
        assert cancelled_again.status_code == 200

        deleted = client.delete(f"/api/v1/projects/{project_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "project_id": project_id}
        assert not job_dir.exists()

        # Verify project no longer exists
        missing = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert missing.status_code == 404


def test_scene_crud_in_awaiting_review(tmp_path: Path) -> None:
    """Verify POST/DELETE scenes in AWAITING_REVIEW including locked and last-scene guards."""
    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        repo = app.state.repository
        project = asyncio.run(
            repo.create_project(
                user_id="00000000-0000-0000-0000-000000000001",
                title="Scene CRUD Review Project",
                mode=ProjectMode.FACELESS,
                aspect_ratio=AspectRatio.PORTRAIT_9_16,
                source_mode="topic",
                source_inputs={"topic": "Scene CRUD"},
                pipeline_config=PipelineConfig().model_dump(mode="json"),
            )
        )
        seeded_scenes = asyncio.run(
            repo.upsert_scenes(
                project.id,
                [
                    SceneCreate(
                        scene_index=0,
                        start_ms=0,
                        end_ms=3000,
                        original_text="Locked intro scene",
                        narration_text="Locked intro scene",
                        is_locked=True,
                    )
                ],
            )
        )
        locked_scene_id = str(seeded_scenes[0].id)

        asyncio.run(
            repo.update_project_status(
                project.id,
                ProjectStatus.AWAITING_REVIEW,
                enforce_fsm=False,
            )
        )

        # Cannot delete last remaining scene (also locked)
        del_locked = client.delete(
            f"/api/v1/projects/{project.id}/scenes/{locked_scene_id}",
            headers=headers,
        )
        assert del_locked.status_code == 409

        # Append unlocked scene
        appended = client.post(
            f"/api/v1/projects/{project.id}/scenes",
            json={
                "narration_text": "Second appended scene",
                "search_keywords": "neon desk",
            },
            headers=headers,
        )
        assert appended.status_code == 201
        appended_data = appended.json()
        appended_id = appended_data["id"]
        assert appended_data["scene_index"] == 1
        assert appended_data["start_ms"] == 3000
        assert appended_data["end_ms"] == 6000

        # Delete appended unlocked scene succeeds
        del_ok = client.delete(
            f"/api/v1/projects/{project.id}/scenes/{appended_id}",
            headers=headers,
        )
        assert del_ok.status_code == 200
        assert del_ok.json() == {"deleted": True, "scene_id": appended_id}

        # Unlock remaining sole scene, then verify deleting last remaining scene is blocked (422)
        unlock_resp = client.patch(
            f"/api/v1/projects/{project.id}/scenes/{locked_scene_id}",
            json={"is_locked": False},
            headers=headers,
        )
        assert unlock_resp.status_code == 200
        assert unlock_resp.json()["is_locked"] is False

        del_last = client.delete(
            f"/api/v1/projects/{project.id}/scenes/{locked_scene_id}",
            headers=headers,
        )
        assert del_last.status_code == 422


def test_billing_history_account_profile_and_system_diagnostics_cleanup(tmp_path: Path) -> None:
    """Verify billing history, account profile GET/PATCH, non-secret diagnostics, and cleanup."""
    cfg = _sandbox_settings(
        tmp_path,
        GROQ_API_KEY="gsk-secret-test-value",
        GEMINI_API_KEY="AIza-secret-test-value",
        SUPABASE_SERVICE_ROLE_KEY="super-secret-service-role-key",
        SUPABASE_JWT_SECRET="super-secret-jwt-key",
        SEPAY_WEBHOOK_SECRET="super-secret-sepay-key",
    )
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        # Create a top-up intent so billing history includes a VietQR transaction
        intent_resp = client.post(
            "/api/v1/billing/topup-intent",
            json={"amount_vnd": 100000, "gateway": "sepay"},
            headers=headers,
        )
        assert intent_resp.status_code == 201
        ref_code = intent_resp.json()["reference_code"]

        history_resp = client.get("/api/v1/billing/history", headers=headers)
        assert history_resp.status_code == 200
        history = history_resp.json()
        assert history["balance"] == 500
        assert history["tier"] == "free"
        assert isinstance(history["transactions"], list)
        assert any(tx.get("reference_code") == ref_code for tx in history["transactions"])
        assert isinstance(history["credit_ledger"], list)
        assert len(history["credit_ledger"]) >= 1

        # Account profile GET + PATCH persistence
        prof_get = client.get("/api/v1/account/profile", headers=headers)
        assert prof_get.status_code == 200
        assert prof_get.json()["credit_balance"] == 500
        assert prof_get.json()["is_sandbox"] is True

        prof_patch = client.patch(
            "/api/v1/account/profile",
            json={"display_name": "Studio Creator Pro"},
            headers=headers,
        )
        assert prof_patch.status_code == 200
        assert prof_patch.json()["display_name"] == "Studio Creator Pro"

        prof_after = client.get("/api/v1/account/profile", headers=headers)
        assert prof_after.status_code == 200
        assert prof_after.json()["display_name"] == "Studio Creator Pro"

        # System diagnostics exposes expected sections and ZERO secret values
        diag_resp = client.get("/api/v1/system/diagnostics")
        assert diag_resp.status_code == 200
        diag = diag_resp.json()
        for section in ("tools", "providers", "storage", "queue"):
            assert section in diag
        assert diag["providers"]["groq"] is True
        assert diag["providers"]["gemini"] is True
        assert diag["providers"]["local_sandbox_mode"] is True

        serialized_diag = json.dumps(diag)
        for secret in (
            "gsk-secret-test-value",
            "AIza-secret-test-value",
            "super-secret-service-role-key",
            "super-secret-jwt-key",
            "super-secret-sepay-key",
        ):
            assert secret not in serialized_diag

        # System cleanup deletes inactive UUID dir while preserving local_storage, sandbox_db.json, and active project dir
        stale_uuid = str(uuid4())
        stale_dir = cfg.temp_path / stale_uuid
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "old.mp4").write_bytes(b"stale")

        active_proj = asyncio.run(
            app.state.repository.create_project(
                user_id="00000000-0000-0000-0000-000000000001",
                title="Active Rendering Dir Guard",
                mode=ProjectMode.FACELESS,
                aspect_ratio=AspectRatio.PORTRAIT_9_16,
                source_mode="topic",
                source_inputs={"topic": "Active"},
                pipeline_config=PipelineConfig().model_dump(mode="json"),
            )
        )
        asyncio.run(
            app.state.repository.update_project_status(
                active_proj.id,
                ProjectStatus.RENDERING,
                enforce_fsm=False,
            )
        )
        active_dir = cfg.temp_path / str(active_proj.id)
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / "in_progress.mp4").write_bytes(b"active")

        local_storage_dir = cfg.temp_path / "local_storage"
        local_storage_dir.mkdir(parents=True, exist_ok=True)
        preserved_asset = local_storage_dir / "keep.mp4"
        preserved_asset.write_bytes(b"keep")
        sandbox_db_path = cfg.temp_path / "sandbox_db.json"
        assert sandbox_db_path.is_file()

        cleanup_resp = client.post("/api/v1/system/cleanup", headers=headers)
        assert cleanup_resp.status_code == 200
        cleanup_data = cleanup_resp.json()
        assert stale_uuid in cleanup_data["cleaned_dirs"]
        assert cleanup_data["cleaned_count"] >= 1
        assert not stale_dir.exists()
        assert active_dir.is_dir()
        assert preserved_asset.is_file()
        assert sandbox_db_path.is_file()
