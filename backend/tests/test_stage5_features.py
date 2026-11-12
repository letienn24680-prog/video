"""Stage 5 Feature Pack unit & integration tests (Sandbox, BGM, Marketing, Thumbnails, Subtitles, Batch ZIP, Tier Upgrade)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.main import create_app
from backend.models.enums import AspectRatio, ProjectMode, ProjectStatus, SubscriptionTier
from backend.models.project import CreateFacelessProjectRequest, PipelineConfig
from backend.models.scene import SceneCreate
from backend.providers.storage.local_storage import LocalStorageProvider
from backend.services.supabase.local_sandbox import (
    SANDBOX_DEMO_EMAIL,
    SANDBOX_DEMO_USER_ID,
    LocalSandboxBillingRepository,
    LocalSandboxRepository,
)


def _sandbox_settings(tmp_path: Path, **overrides: object) -> Settings:
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, object] = {
        "_env_file": None,
        "APP_ENV": "development",
        "TEMP_DIR": str(temp_dir),
        "LOCAL_SANDBOX_MODE": True,
        "SANDBOX_INITIAL_CREDITS": 500,
        "PRO_UPGRADE_CREDITS": 500,
        "ENTERPRISE_UPGRADE_CREDITS": 1500,
        "STORAGE_PROVIDER": "local",
        "TTS_PROVIDER": "edge_tts",
        "LLM_PROVIDER": "gemini",
        "VISUAL_PROVIDER": "pexels",
        "GEMINI_API_KEY": "",
        "PEXELS_API_KEY": "",
        "MAX_UPLOAD_MB": 5,
    }
    defaults.update(overrides)
    return Settings(**defaults)


async def _seed_project_with_scenes(
    repo: LocalSandboxRepository,
    *,
    title: str = "AI Studio Test Project",
    batch_id: str | None = None,
    status: ProjectStatus = ProjectStatus.COMPLETED,
    output_video_url: str | None = None,
) -> tuple[str, list[str]]:
    project = await repo.create_project(
        user_id=SANDBOX_DEMO_USER_ID,
        mode=ProjectMode.FACELESS,
        title=title,
        source_mode="topic",
        source_inputs={"topic": "AI automation workflow"},
        aspect_ratio=AspectRatio.PORTRAIT_9_16,
        pipeline_config=PipelineConfig().model_dump(mode="json"),
        batch_id=batch_id,
    )
    await repo.upsert_scenes(
        project.id,
        [
            SceneCreate(
                scene_index=0,
                original_text="First hook line",
                edited_text="First hook line edited",
                narration_text="First hook line edited",
                search_keywords="futuristic studio",
                start_ms=0,
                end_ms=2500,
            ),
            SceneCreate(
                scene_index=1,
                original_text="Second explanation line",
                edited_text="Second explanation line",
                narration_text="Second explanation line",
                search_keywords="developer coding",
                start_ms=2500,
                end_ms=5200,
            ),
        ],
    )
    if status != ProjectStatus.PENDING:
        await repo.update_project_status(
            project.id,
            status,
            enforce_fsm=False,
            progress_pct=100 if status == ProjectStatus.COMPLETED else 40,
            output_video_url=output_video_url,
        )
    scenes = await repo.list_scenes(project.id)
    return str(project.id), [str(s.id) for s in scenes]


def test_public_config_and_sandbox_auth_toggle(tmp_path: Path) -> None:
    """Verify public-config exposes Stage 5 flags and /auth/sandbox-token honors LOCAL_SANDBOX_MODE."""
    disabled_cfg = _sandbox_settings(tmp_path / "disabled", LOCAL_SANDBOX_MODE=False)
    disabled_app = create_app(disabled_cfg)
    with TestClient(disabled_app) as client:
        pub = client.get("/api/v1/public-config")
        assert pub.status_code == 200
        pub_data = pub.json()
        assert pub_data["local_sandbox_mode"] is False
        assert pub_data["pro_upgrade_credits"] == 500
        assert pub_data["enterprise_upgrade_credits"] == 1500

        denied = client.post("/api/v1/auth/sandbox-token")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "SANDBOX_DISABLED"

    enabled_cfg = _sandbox_settings(tmp_path / "enabled", LOCAL_SANDBOX_MODE=True)
    enabled_app = create_app(enabled_cfg)
    with TestClient(enabled_app) as client:
        assert isinstance(enabled_app.state.repository, LocalSandboxRepository)
        assert isinstance(enabled_app.state.billing_repository, LocalSandboxBillingRepository)

        pub = client.get("/api/v1/public-config")
        assert pub.status_code == 200
        assert pub.json()["local_sandbox_mode"] is True

        tok = client.post("/api/v1/auth/sandbox-token")
        assert tok.status_code == 200
        tok_data = tok.json()
        assert tok_data["access_token"] == "sandbox-demo-token"
        assert tok_data["token_type"] == "bearer"
        assert tok_data["user"]["id"] == SANDBOX_DEMO_USER_ID
        assert tok_data["user"]["email"] == SANDBOX_DEMO_EMAIL


def test_sandbox_crud_billing_local_media_and_tier_upgrade(tmp_path: Path) -> None:
    """Verify sandbox token auth, credits, project CRUD, /local-media mounts, and tier upgrade."""
    cfg = _sandbox_settings(
        tmp_path,
        SANDBOX_INITIAL_CREDITS=500,
        PRO_UPGRADE_CREDITS=500,
        ENTERPRISE_UPGRADE_CREDITS=1500,
    )
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        # Prevent background pipeline from running external calls during CRUD test
        app.state.job_queue.enqueue = AsyncMock()

        credits_resp = client.get("/api/v1/billing/credits", headers=headers)
        assert credits_resp.status_code == 200
        credits_body = credits_resp.json()
        assert credits_body["balance"] == 500
        assert len(credits_body["credit_ledger"]) >= 1

        create_payload = CreateFacelessProjectRequest(
            title="Sandbox Demo Clip",
            source_mode="topic",
            topic="5 AI tricks",
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
        ).model_dump(mode="json")
        created = client.post("/api/v1/projects", json=create_payload, headers=headers)
        assert created.status_code == 201
        project_id = created.json()["id"]

        listed = client.get("/api/v1/projects", headers=headers)
        assert listed.status_code == 200
        assert any(p["id"] == project_id for p in listed.json())

        detail = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["title"] == "Sandbox Demo Clip"

        # Static mounts: /local-media and /local-media/storage
        temp_asset = cfg.temp_path / "preview_note.txt"
        temp_asset.write_text("hello-local-media", encoding="utf-8")
        res_temp = client.get("/local-media/preview_note.txt")
        assert res_temp.status_code == 200
        assert res_temp.text == "hello-local-media"

        src_storage_file = tmp_path / "sample.mp4"
        src_storage_file.write_bytes(b"FAKE_MP4_STREAM")
        storage = LocalStorageProvider(settings=cfg)
        import asyncio

        stored = asyncio.run(
            storage.upload_file(src_storage_file, "demo/output.mp4", "video/mp4")
        )
        assert stored.signed_url == "/local-media/storage/demo/output.mp4"
        res_storage = client.get(stored.signed_url)
        assert res_storage.status_code == 200
        assert res_storage.content == b"FAKE_MP4_STREAM"

        # Tier upgrade: Enterprise (1500) should fail with 402 when balance is 500
        ent_fail = client.post(
            "/api/v1/billing/upgrade-tier",
            json={"target_tier": "enterprise"},
            headers=headers,
        )
        assert ent_fail.status_code == 402

        # Pro (500) succeeds and deducts 500 credits
        pro_ok = client.post(
            "/api/v1/billing/upgrade-tier",
            json={"target_tier": "pro"},
            headers=headers,
        )
        assert pro_ok.status_code == 200
        pro_data = pro_ok.json()
        assert pro_data["subscription_tier"] == "pro"
        assert pro_data["credits_deducted"] == 500
        assert pro_data["new_balance"] == 0

        profile = asyncio.run(app.state.repository.get_profile(SANDBOX_DEMO_USER_ID))
        assert profile["subscription_tier"] == SubscriptionTier.PRO.value

        # Insufficient credits on repeat upgrade
        pro_empty = client.post(
            "/api/v1/billing/upgrade-tier",
            json={"target_tier": "pro"},
            headers=headers,
        )
        assert pro_empty.status_code == 402


def test_bgm_presets_preview_and_upload(tmp_path: Path) -> None:
    """Verify BGM preset listing, synthesized WAV preview, and custom BGM upload validation."""
    cfg = _sandbox_settings(tmp_path, MAX_UPLOAD_MB=1)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        presets_resp = client.get("/api/v1/bgm/presets")
        assert presets_resp.status_code == 200
        presets = presets_resp.json()
        assert len(presets) >= 5
        first_id = presets[0]["id"]

        preview_resp = client.get(f"/api/v1/bgm/presets/{first_id}/preview")
        assert preview_resp.status_code == 200
        assert "audio/wav" in preview_resp.headers["content-type"]
        assert preview_resp.content[:4] == b"RIFF"

        missing_resp = client.get("/api/v1/bgm/presets/unknown_mood/preview")
        assert missing_resp.status_code == 404

        bad_ext = client.post(
            "/api/v1/projects/upload-bgm",
            files={"file": ("notes.txt", b"not audio", "text/plain")},
            headers=headers,
        )
        assert bad_ext.status_code == 422

        too_large = client.post(
            "/api/v1/projects/upload-bgm",
            files={"file": ("big.mp3", b"x" * (1024 * 1024 + 64), "audio/mpeg")},
            headers=headers,
        )
        assert too_large.status_code == 422

        ok_upload = client.post(
            "/api/v1/projects/upload-bgm",
            files={"file": ("my_beat.mp3", b"ID3-DEMO-MP3-DATA", "audio/mpeg")},
            headers=headers,
        )
        assert ok_upload.status_code == 201
        up_data = ok_upload.json()
        assert up_data["filename"] == "my_beat.mp3"
        assert up_data["bgm_file_key"]
        assert up_data["url"].startswith("/local-media/")

        fetched = client.get(up_data["url"])
        assert fetched.status_code == 200
        assert fetched.content == b"ID3-DEMO-MP3-DATA"


def test_marketing_kit_thumbnail_and_subtitle_exports(tmp_path: Path) -> None:
    """Verify AI Marketing Kit, Thumbnail generation + persistence, and .srt/.ass subtitle exports."""
    import asyncio

    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        project_id, _ = asyncio.run(
            _seed_project_with_scenes(app.state.repository, title="Marketing & Subtitles Clip")
        )

        # Marketing kit
        mk_resp = client.post(f"/api/v1/projects/{project_id}/marketing-kit", headers=headers)
        assert mk_resp.status_code == 200
        mk_data = mk_resp.json()
        assert len(mk_data["hooks"]) >= 1
        assert len(mk_data["titles"]) >= 1
        assert mk_data["description"]
        assert len(mk_data["hashtags"]) >= 1

        # Thumbnail generation
        thumb_resp = client.post(
            f"/api/v1/projects/{project_id}/thumbnail",
            json={"prompt": "Dramatic neon cyberpunk creator setup"},
            headers=headers,
        )
        assert thumb_resp.status_code == 200
        thumb_data = thumb_resp.json()
        assert thumb_data["url"].startswith("/local-media/")
        assert Path(thumb_data["local_path"]).is_file()

        thumb_fetch = client.get(thumb_data["url"])
        assert thumb_fetch.status_code == 200

        # Confirm both persisted on project.pipeline_config
        proj_resp = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert proj_resp.status_code == 200
        pc = proj_resp.json()["pipeline_config"]
        assert pc["marketing_kit"]["description"] == mk_data["description"]
        assert pc["thumbnail_url"] == thumb_data["url"]

        # SRT export
        srt_resp = client.get(
            f"/api/v1/projects/{project_id}/subtitles/export?format=srt",
            headers=headers,
        )
        assert srt_resp.status_code == 200
        assert f'filename="subtitles_{project_id}.srt"' in srt_resp.headers["content-disposition"]
        assert "00:00:00,000 --> 00:00:02,500" in srt_resp.text
        assert "First hook line edited" in srt_resp.text

        # ASS export
        ass_resp = client.get(
            f"/api/v1/projects/{project_id}/subtitles/export?format=ass",
            headers=headers,
        )
        assert ass_resp.status_code == 200
        assert f'filename="subtitles_{project_id}.ass"' in ass_resp.headers["content-disposition"]
        assert "[Script Info]" in ass_resp.text
        assert "[V4+ Styles]" in ass_resp.text
        assert "Dialogue:" in ass_resp.text

        # Invalid format
        bad_fmt = client.get(
            f"/api/v1/projects/{project_id}/subtitles/export?format=vtt",
            headers=headers,
        )
        assert bad_fmt.status_code == 422


def test_batch_zip_download_bundles_completed_videos_and_srts(tmp_path: Path) -> None:
    """Verify batch ZIP bundles only COMPLETED projects in batch_id along with .srt files."""
    import asyncio

    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}
    batch_id = str(uuid4())
    other_batch_id = str(uuid4())

    with TestClient(app) as client:
        storage = LocalStorageProvider(settings=cfg)
        v1_src = tmp_path / "v1.mp4"
        v1_src.write_bytes(b"MP4_CLIP_ONE")
        v1_stored = asyncio.run(storage.upload_file(v1_src, "batch/clip1.mp4", "video/mp4"))

        v2_file = cfg.temp_path / "direct_clip2.mp4"
        v2_file.write_bytes(b"MP4_CLIP_TWO")

        asyncio.run(
            _seed_project_with_scenes(
                app.state.repository,
                title="Batch One",
                batch_id=batch_id,
                status=ProjectStatus.COMPLETED,
                output_video_url=v1_stored.signed_url,
            )
        )
        asyncio.run(
            _seed_project_with_scenes(
                app.state.repository,
                title="Batch Two",
                batch_id=batch_id,
                status=ProjectStatus.COMPLETED,
                output_video_url="/local-media/direct_clip2.mp4",
            )
        )
        # Should be excluded (FAILED status)
        asyncio.run(
            _seed_project_with_scenes(
                app.state.repository,
                title="Batch Failed",
                batch_id=batch_id,
                status=ProjectStatus.FAILED,
                output_video_url=None,
            )
        )
        # Should be excluded (different batch_id)
        asyncio.run(
            _seed_project_with_scenes(
                app.state.repository,
                title="Other Batch",
                batch_id=other_batch_id,
                status=ProjectStatus.COMPLETED,
                output_video_url=v1_stored.signed_url,
            )
        )

        zip_resp = client.get(
            f"/api/v1/projects/batch/{batch_id}/download-zip",
            headers=headers,
        )
        assert zip_resp.status_code == 200
        assert "application/zip" in zip_resp.headers["content-type"]
        assert f'filename="batch_{batch_id}.zip"' in zip_resp.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(zip_resp.content), "r") as zf:
            names = sorted(zf.namelist())
            mp4_names = [n for n in names if n.endswith(".mp4")]
            srt_names = [n for n in names if n.endswith(".srt")]
            assert len(mp4_names) == 2
            assert len(srt_names) == 2
            assert not any("Failed" in n or "Other" in n for n in names)

            payloads = {zf.read(n) for n in mp4_names}
            assert payloads == {b"MP4_CLIP_ONE", b"MP4_CLIP_TWO"}
            for srt_name in srt_names:
                srt_text = zf.read(srt_name).decode("utf-8")
                assert "First hook line edited" in srt_text

        missing_zip = client.get(
            f"/api/v1/projects/batch/{uuid4()}/download-zip",
            headers=headers,
        )
        assert missing_zip.status_code == 404
