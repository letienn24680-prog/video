"""Unit tests for Stage 2 FastAPI core, job queue, cleanup worker, providers, and routes."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.core.events import SSEEventBus
from backend.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    DatabaseError,
    InsufficientCreditsError,
    InvalidStateTransitionError,
    NotFoundError,
    ProviderError,
    StudioError,
    ValidationError,
)
from backend.core.security import CurrentUser, get_current_user
from backend.main import create_app
from backend.models.enums import (
    AspectRatio,
    ProjectMode,
    ProjectStatus,
    can_transition,
)
from backend.models.project import ProjectDetailResponse, ProjectResponse
from backend.models.provider import AudioSynthesisResult, VoiceOption
from backend.models.scene import SceneResponse
from backend.providers.factory import ProviderFactory
from backend.workers.cleanup_worker import AutoCleanupWorker, cleanup_job_dir
from backend.workers.task_queue import AsyncJobQueue


def _make_project(
    *,
    project_id: UUID | None = None,
    user_id: UUID | None = None,
    mode: ProjectMode = ProjectMode.TRANSLATOR,
    status: ProjectStatus = ProjectStatus.PENDING,
    credits_charged: int = 0,
    pipeline_config: dict | None = None,
) -> ProjectResponse:
    now = datetime.now(timezone.utc)
    return ProjectResponse(
        id=project_id or uuid4(),
        user_id=user_id or uuid4(),
        title="Test Project",
        mode=mode,
        status=status,
        aspect_ratio=AspectRatio.PORTRAIT_9_16,
        source_mode="url",
        source_inputs={"source_url": "https://example.com/video.mp4"},
        pipeline_config=pipeline_config or {},
        progress_pct=0,
        current_stage=None,
        credits_estimated=10,
        credits_charged=credits_charged,
        created_at=now,
        updated_at=now,
    )


def _make_scene(
    *,
    scene_id: UUID | None = None,
    project_id: UUID | None = None,
    scene_index: int = 0,
    start_ms: int = 0,
    end_ms: int = 3000,
    edited_text: str = "Xin chao cac ban",
) -> SceneResponse:
    now = datetime.now(timezone.utc)
    return SceneResponse(
        id=scene_id or uuid4(),
        project_id=project_id or uuid4(),
        scene_index=scene_index,
        start_ms=start_ms,
        end_ms=end_ms,
        original_text="Hello everyone",
        edited_text=edited_text,
        narration_text=edited_text,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 1. Settings & FSM Tests
# ---------------------------------------------------------------------------


def test_settings_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "5")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "work"))

    cfg = Settings(_env_file=None)
    assert cfg.MAX_CONCURRENT_JOBS == 5
    assert cfg.TTS_PROVIDER == "edge_tts"
    assert cfg.STT_PROVIDER == "groq"
    assert cfg.cors_origins_list == ["https://a.example", "https://b.example"]
    assert cfg.temp_path == tmp_path / "work"


def test_project_status_fsm_transitions() -> None:
    assert can_transition(ProjectStatus.PENDING, ProjectStatus.PROCESSING)
    assert can_transition(ProjectStatus.PROCESSING, ProjectStatus.AWAITING_REVIEW)
    assert can_transition(ProjectStatus.AWAITING_REVIEW, ProjectStatus.RENDERING)
    assert can_transition(ProjectStatus.RENDERING, ProjectStatus.COMPLETED)
    assert can_transition(ProjectStatus.AWAITING_REVIEW, ProjectStatus.CANCELLED)
    assert can_transition(ProjectStatus.FAILED, ProjectStatus.PROCESSING)

    assert not can_transition(ProjectStatus.COMPLETED, ProjectStatus.PROCESSING)
    assert not can_transition(ProjectStatus.CANCELLED, ProjectStatus.PENDING)
    assert not can_transition(ProjectStatus.PENDING, ProjectStatus.COMPLETED)
    assert not can_transition(ProjectStatus.PENDING, ProjectStatus.RENDERING)
    assert not can_transition("UNKNOWN_STATUS", ProjectStatus.PROCESSING)


# ---------------------------------------------------------------------------
# 2. ProviderFactory Defaults & Overrides
# ---------------------------------------------------------------------------


def test_provider_factory_registry_and_overrides() -> None:
    registered = ProviderFactory.list_registered_providers()
    assert set(registered.keys()) == {"stt", "tts", "llm", "visual", "separation", "storage"}
    assert "groq" in registered["stt"]
    assert "edge_tts" in registered["tts"]
    assert "gemini" in registered["llm"]
    assert "pexels" in registered["visual"]
    assert "fast_mode" in registered["separation"]
    assert "supabase" in registered["storage"]

    cfg = Settings(_env_file=None, STORAGE_PROVIDER="local", TTS_PROVIDER="edge_tts")
    defaults = ProviderFactory.get_default_providers(cfg)
    assert defaults["storage"] == "local"
    assert defaults["tts"] == "edge_tts"

    local_storage = ProviderFactory.get_storage(settings=cfg)
    assert local_storage.provider_name == "local"

    eleven = ProviderFactory.get_tts(override="elevenlabs", settings=cfg)
    assert eleven.provider_name == "elevenlabs"

    fast_sep = ProviderFactory.get_separation(override="fast_mode", settings=cfg)
    assert fast_sep.provider_name == "fast_mode"

    with pytest.raises(ConfigurationError):
        ProviderFactory.get_tts(override="nonexistent_tts", settings=cfg)


# ---------------------------------------------------------------------------
# 3. AsyncJobQueue Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_queue_concurrency_limit() -> None:
    cfg = Settings(_env_file=None, MAX_CONCURRENT_JOBS=2, JOB_TIMEOUT_SECONDS=30)
    repo = MagicMock()
    repo.get_project = AsyncMock(side_effect=NotFoundError("skip"))
    bus = SSEEventBus()
    queue = AsyncJobQueue(settings=cfg, repository=repo, event_bus=bus)

    in_flight = 0
    max_seen = 0

    async def handler(pid: str, ctx: dict) -> str:
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return pid

    queue.register_handler(ProjectMode.TRANSLATOR, "prepare", handler)

    tasks = [
        await queue.enqueue(str(uuid4()), ProjectMode.TRANSLATOR, "prepare")
        for _ in range(4)
    ]
    await asyncio.gather(*tasks)
    assert max_seen <= 2
    assert queue.active_count() == 0


@pytest.mark.asyncio
async def test_job_queue_timeout_marks_failed_and_refunds() -> None:
    cfg = Settings(_env_file=None, MAX_CONCURRENT_JOBS=2, JOB_TIMEOUT_SECONDS=30)
    pid = uuid4()
    uid = uuid4()
    project = _make_project(
        project_id=pid,
        user_id=uid,
        status=ProjectStatus.PENDING,
        credits_charged=25,
    )

    repo = MagicMock()
    repo.get_project = AsyncMock(return_value=project)
    repo.update_project_status = AsyncMock(return_value=project)
    repo.refund_user_credits = AsyncMock(return_value={"status": "refunded"})
    bus = SSEEventBus()
    queue = AsyncJobQueue(settings=cfg, repository=repo, event_bus=bus)

    async def slow_handler(_pid: str, _ctx: dict) -> None:
        await asyncio.sleep(5)

    queue.register_handler(ProjectMode.FACELESS, "prepare", slow_handler)

    # Patch wait_for timeout down for fast unit testing
    with patch.object(queue.settings, "JOB_TIMEOUT_SECONDS", 0.05):
        async with bus.subscribe(str(pid)) as sub_q:
            task = await queue.enqueue(pid, ProjectMode.FACELESS, "prepare")
            await task

            events = []
            while not sub_q.empty():
                item = sub_q.get_nowait()
                if item is not None:
                    events.append(item)

    assert any(ev.event == "error" and ev.data.get("code") == "JOB_TIMEOUT" and ev.data.get("refunded") is True for ev in events)
    repo.update_project_status.assert_any_await(
        str(pid),
        ProjectStatus.FAILED,
        current_stage="failed",
        error_message="Job timed out after 0.05 seconds",
        enforce_fsm=False,
    )
    repo.refund_user_credits.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_queue_unhandled_error_refunds_and_cancel() -> None:
    cfg = Settings(_env_file=None, MAX_CONCURRENT_JOBS=2, JOB_TIMEOUT_SECONDS=30)
    pid_err = uuid4()
    uid = uuid4()
    project_err = _make_project(
        project_id=pid_err,
        user_id=uid,
        status=ProjectStatus.PROCESSING,
        credits_charged=15,
    )

    repo = MagicMock()
    repo.get_project = AsyncMock(return_value=project_err)
    repo.update_project_status = AsyncMock(return_value=project_err)
    repo.update_project_progress = AsyncMock(return_value=project_err)
    repo.refund_user_credits = AsyncMock(return_value={"status": "refunded"})
    bus = SSEEventBus()
    queue = AsyncJobQueue(settings=cfg, repository=repo, event_bus=bus)

    async def failing_handler(pid: str, _ctx: dict) -> None:
        await queue.emit_progress(pid, "stt", 25, "Running STT")
        raise RuntimeError("boom")

    queue.register_handler(ProjectMode.TRANSLATOR, "prepare", failing_handler)

    async with bus.subscribe(str(pid_err)) as sub_q:
        task = await queue.enqueue(pid_err, ProjectMode.TRANSLATOR, "prepare")
        await task

        events = []
        while not sub_q.empty():
            item = sub_q.get_nowait()
            if item is not None:
                events.append(item)

    assert any(ev.event == "progress" and ev.data.get("progress_pct") == 25 for ev in events)
    assert any(ev.event == "error" and ev.data.get("refunded") is True for ev in events)
    repo.refund_user_credits.assert_awaited_once()

    # Cancellation test
    pid_cancel = uuid4()
    started = asyncio.Event()

    async def long_handler(_pid: str, _ctx: dict) -> None:
        started.set()
        await asyncio.sleep(10)

    queue.register_handler(ProjectMode.TRANSLATOR, "render", long_handler)
    await queue.enqueue(pid_cancel, ProjectMode.TRANSLATOR, "render")
    await started.wait()
    assert queue.active_count() == 1
    cancelled = await queue.cancel(pid_cancel)
    assert cancelled is True
    assert queue.active_count() == 0


# ---------------------------------------------------------------------------
# 4. AutoCleanupWorker Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_cleanup_worker_removes_only_stale_dirs(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    gitkeep = temp_root / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    stale_id = str(uuid4())
    stale_dir = temp_root / stale_id
    stale_dir.mkdir()
    (stale_dir / "video.mp4").write_bytes(b"123")

    fresh_id = str(uuid4())
    fresh_dir = temp_root / fresh_id
    fresh_dir.mkdir()
    (fresh_dir / "audio.wav").write_bytes(b"456")

    now = time.time()
    old_mtime = now - (25 * 3600)
    os.utime(stale_dir, (old_mtime, old_mtime))

    cfg = Settings(_env_file=None, TEMP_DIR=temp_root, TEMP_TTL_HOURS=24)
    repo = MagicMock()
    repo.mark_temp_cleaned = AsyncMock()

    worker = AutoCleanupWorker(settings=cfg, repository=repo)
    cleaned = await worker.run_once(now=now)

    assert cleaned == [stale_id]
    assert not stale_dir.exists()
    assert fresh_dir.exists()
    assert gitkeep.exists()
    repo.mark_temp_cleaned.assert_awaited_once_with(stale_id)

    # Direct helper + traversal guard
    assert await cleanup_job_dir("../escape", temp_dir=temp_root, repository=repo) is False
    assert await cleanup_job_dir(fresh_id, temp_dir=temp_root, repository=repo) is True
    assert not fresh_dir.exists()


# ---------------------------------------------------------------------------
# 5. FastAPI Routes & Global Exception Mapping Tests
# ---------------------------------------------------------------------------


def _build_test_client(tmp_path: Path, user_id: UUID | None = None):
    uid = user_id or uuid4()
    cfg = Settings(
        _env_file=None,
        TEMP_DIR=tmp_path / "temp",
        AUTO_CLEANUP_ENABLED=False,
        MAX_BATCH_SIZE=2,
    )
    app = create_app(cfg)

    mock_repo = MagicMock()
    app.state.repository = mock_repo
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(uid),
        email="tester@example.com",
        role="authenticated",
    )
    return app, mock_repo, uid, cfg


def test_health_and_providers_routes(tmp_path: Path) -> None:
    app, _repo, _uid, _cfg = _build_test_client(tmp_path)

    with TestClient(app) as client:
        health_resp = client.get("/api/v1/health")
        assert health_resp.status_code == 200
        body = health_resp.json()
        assert body["status"] == "ok"
        assert body["active_jobs"] == 0
        assert body["default_providers"]["tts"] == "edge_tts"

        prov_resp = client.get("/api/v1/providers")
        assert prov_resp.status_code == 200
        assert "edge_tts" in prov_resp.json()["providers"]["tts"]

        fake_tts = SimpleNamespace(
            provider_name="edge_tts",
            list_voices=AsyncMock(
                return_value=[
                    VoiceOption(
                        voice_id="vi-VN-HoaiMyNeural",
                        name="HoaiMy",
                        language="vi-VN",
                        gender="Female",
                        provider="edge_tts",
                    )
                ]
            ),
        )
        with patch.object(ProviderFactory, "get_tts", return_value=fake_tts):
            voices_resp = client.get("/api/v1/providers/tts/voices?provider=edge_tts&language=vi")
            assert voices_resp.status_code == 200
            assert voices_resp.json()["count"] == 1
            assert voices_resp.json()["voices"][0]["voice_id"] == "vi-VN-HoaiMyNeural"


def test_projects_crud_hitl_render_and_cancel_routes(tmp_path: Path) -> None:
    app, repo, uid, _cfg = _build_test_client(tmp_path)
    pid = uuid4()
    sid = uuid4()

    pending_project = _make_project(project_id=pid, user_id=uid, status=ProjectStatus.PENDING)
    review_project = _make_project(
        project_id=pid,
        user_id=uid,
        status=ProjectStatus.AWAITING_REVIEW,
        credits_charged=20,
    )
    rendering_project = _make_project(
        project_id=pid,
        user_id=uid,
        status=ProjectStatus.RENDERING,
        credits_charged=20,
    )
    cancelled_project = _make_project(
        project_id=pid,
        user_id=uid,
        status=ProjectStatus.CANCELLED,
        credits_charged=0,
    )
    scene = _make_scene(scene_id=sid, project_id=pid)

    repo.create_project = AsyncMock(return_value=pending_project)
    repo.list_projects = AsyncMock(return_value=[pending_project])
    repo.get_project_detail = AsyncMock(
        return_value=ProjectDetailResponse(**review_project.model_dump(), scenes=[scene])
    )
    repo.get_project = AsyncMock(return_value=review_project)
    repo.get_scene = AsyncMock(return_value=scene)
    repo.update_scene = AsyncMock(return_value=scene)
    repo.update_project = AsyncMock(return_value=review_project)
    repo.update_project_status = AsyncMock(return_value=rendering_project)
    repo.refund_user_credits = AsyncMock(return_value={"status": "refunded"})

    with TestClient(app) as client:
        # Register no-op handlers so background prepare/render tasks complete cleanly
        app.state.job_queue.register_handler(ProjectMode.TRANSLATOR, "prepare", AsyncMock())
        app.state.job_queue.register_handler(ProjectMode.TRANSLATOR, "render", AsyncMock())

        # Create single project
        create_resp = client.post(
            "/api/v1/projects",
            json={
                "mode": "translator",
                "title": "Demo Dub",
                "source_url": "https://youtube.com/watch?v=123",
            },
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["id"] == str(pid)

        # Batch validation (MAX_BATCH_SIZE=2 -> 3 items fails with 422)
        bad_batch = client.post(
            "/api/v1/projects/batch",
            json={
                "items": [
                    {"mode": "faceless", "source_mode": "topic", "topic": "A"},
                    {"mode": "faceless", "source_mode": "topic", "topic": "B"},
                    {"mode": "faceless", "source_mode": "topic", "topic": "C"},
                ]
            },
        )
        assert bad_batch.status_code == 422
        assert bad_batch.json()["error"]["code"] == "VALIDATION_ERROR"

        # List & Detail
        assert client.get("/api/v1/projects").status_code == 200
        detail_resp = client.get(f"/api/v1/projects/{pid}")
        assert detail_resp.status_code == 200
        assert len(detail_resp.json()["scenes"]) == 1

        # HITL scene patch while AWAITING_REVIEW
        patch_resp = client.patch(
            f"/api/v1/projects/{pid}/scenes/{sid}",
            json={"edited_text": "Ban cap nhat", "tts_rate_pct": 10},
        )
        assert patch_resp.status_code == 200

        # HITL single-line TTS preview
        async def fake_synthesize(text: str, voice_id: str, output_path: Path, rate_pct: int = 0):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ID3-FAKE-MP3")
            return AudioSynthesisResult(
                audio_path=output_path,
                duration_ms=1250,
                voice_id=voice_id,
                rate_pct=rate_pct,
                provider="edge_tts",
            )

        fake_tts = SimpleNamespace(provider_name="edge_tts", synthesize=fake_synthesize)
        with patch.object(ProviderFactory, "get_tts", return_value=fake_tts):
            tts_resp = client.post(
                f"/api/v1/projects/{pid}/scenes/{sid}/preview-tts",
                json={"text": "Xin chao", "tts_rate_pct": 15},
            )
            assert tts_resp.status_code == 200
            assert tts_resp.content == b"ID3-FAKE-MP3"
            assert tts_resp.headers["X-Audio-Duration-Ms"] == "1250"

        # Render transition
        render_resp = client.post(f"/api/v1/projects/{pid}/render", json={})
        assert render_resp.status_code == 200
        assert render_resp.json()["status"] == "RENDERING"

        # Cancel & refund
        current_state = {"project": review_project}

        async def _get_project_dynamic(*_args, **_kwargs):
            return current_state["project"]

        async def _refund_and_zero(*_args, **_kwargs):
            current_state["project"] = cancelled_project
            return {"status": "refunded"}

        repo.get_project = AsyncMock(side_effect=_get_project_dynamic)
        repo.update_project_status = AsyncMock(return_value=cancelled_project)
        repo.refund_user_credits = AsyncMock(side_effect=_refund_and_zero)
        cancel_resp = client.post(f"/api/v1/projects/{pid}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "CANCELLED"
        assert cancel_resp.json()["credits_charged"] == 0
        repo.refund_user_credits.assert_awaited_once()


def test_global_exception_status_mapping(tmp_path: Path) -> None:
    app, _repo, _uid, _cfg = _build_test_client(tmp_path)

    cases = [
        ("/test/400", StudioError("bad", code="BAD_REQUEST", status_code=400), 400),
        ("/test/401", AuthenticationError("unauth"), 401),
        ("/test/402", InsufficientCreditsError("no credits"), 402),
        ("/test/403", AuthorizationError("forbidden"), 403),
        ("/test/404", NotFoundError("missing"), 404),
        ("/test/409", InvalidStateTransitionError("conflict"), 409),
        ("/test/422", ValidationError("invalid"), 422),
        ("/test/502", ProviderError("groq", "down"), 502),
        ("/test/500", DatabaseError("db fail"), 500),
    ]

    for route_path, exc, _expected in cases:
        def _make_endpoint(error_obj=exc):
            async def _ep():
                raise error_obj

            return _ep

        app.add_api_route(route_path, _make_endpoint(), methods=["GET"])

    with TestClient(app) as client:
        for route_path, _exc, expected_status in cases:
            resp = client.get(route_path)
            assert resp.status_code == expected_status
            assert "error" in resp.json()
