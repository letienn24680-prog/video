"""Stage 7 tests: placeholder stripping, isolated sandbox auth, hot .env config, and zero-key AI fallbacks."""

from __future__ import annotations

import asyncio
import json
import struct
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings, get_settings
from backend.core.events import SSEEventBus
from backend.main import create_app
from backend.models.enums import AspectRatio, ProjectMode, ProjectStatus, VisualProviderName
from backend.models.project import PipelineConfig
from backend.pipelines.faceless.orchestrator import (
    _resolve_faceless_visual_provider,
    run_faceless_prepare,
)
from backend.pipelines.faceless.stage4_assemble import _ensure_scene_local_asset
from backend.pipelines.translator.stage2_stt import run_stage2_stt
from backend.pipelines.translator.stage3_translate import run_stage3_translate
from backend.providers.storage.local_storage import LocalStorageProvider
from backend.providers.visual.pixabay import PixabayProvider
from backend.services.media.ffmpeg_engine import FFmpegEngine


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
        "STORAGE_PROVIDER": "local",
        "STT_PROVIDER": "groq",
        "LLM_PROVIDER": "gemini",
        "VISUAL_PROVIDER": "pexels",
        "GEMINI_API_KEY": "",
        "GROQ_API_KEY": "",
        "PEXELS_API_KEY": "",
        "PIXABAY_API_KEY": "",
        "FLUX_API_KEY": "",
        "SD_API_KEY": "",
        "SECRET_KEY": "a" * 64,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _write_tiny_wav(dest_path: Path, duration_sec: float = 1.0, sample_rate: int = 16000) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(duration_sec * sample_rate)
    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + ("h" * num_frames), *([0] * num_frames)))
    return dest_path


def test_public_config_strips_placeholder_supabase_values(tmp_path: Path) -> None:
    """GET /api/v1/public-config strips .env.example placeholders to empty strings."""
    placeholder_cfg = _sandbox_settings(
        tmp_path / "ph",
        SUPABASE_URL="https://your-project-ref.supabase.co",
        SUPABASE_ANON_KEY="your_supabase_anon_key_here",
    )
    app_ph = create_app(placeholder_cfg)
    with TestClient(app_ph) as client:
        data = client.get("/api/v1/public-config").json()
        assert data["supabase_url"] == ""
        assert data["supabase_anon_key"] == ""

    real_cfg = _sandbox_settings(
        tmp_path / "real",
        SUPABASE_URL="https://realproj123.supabase.co",
        SUPABASE_ANON_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.real",
    )
    app_real = create_app(real_cfg)
    with TestClient(app_real) as client:
        data = client.get("/api/v1/public-config").json()
        assert data["supabase_url"] == "https://realproj123.supabase.co"
        assert data["supabase_anon_key"] == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.real"


def test_sandbox_register_login_and_user_isolation(tmp_path: Path) -> None:
    """Verify sandbox-register/login, conflict/401 guards, HMAC tokens, and per-user data isolation."""
    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)

    with TestClient(app) as client:
        app.state.job_queue.enqueue = AsyncMock()

        reg_a = client.post(
            "/api/v1/auth/sandbox-register",
            json={"email": "Alice@Example.com", "password": "secret-password-1"},
        )
        assert reg_a.status_code == 201
        a_data = reg_a.json()
        assert a_data["access_token"].startswith("sandbox-acct:")
        assert a_data["user"]["email"] == "alice@example.com"

        # Duplicate registration -> 409
        dup_a = client.post(
            "/api/v1/auth/sandbox-register",
            json={"email": "alice@example.com", "password": "secret-password-1"},
        )
        assert dup_a.status_code == 409

        # Wrong password -> 401
        bad_login = client.post(
            "/api/v1/auth/sandbox-login",
            json={"email": "alice@example.com", "password": "wrong-password"},
        )
        assert bad_login.status_code == 401

        # Valid login -> 200
        ok_login = client.post(
            "/api/v1/auth/sandbox-login",
            json={"email": "alice@example.com", "password": "secret-password-1"},
        )
        assert ok_login.status_code == 200
        token_a = ok_login.json()["access_token"]
        assert token_a.startswith("sandbox-acct:")

        # Register second distinct user Bob
        reg_b = client.post(
            "/api/v1/auth/sandbox-register",
            json={"email": "bob@example.com", "password": "secret-password-2"},
        )
        assert reg_b.status_code == 201
        b_data = reg_b.json()
        assert b_data["user"]["id"] != a_data["user"]["id"]
        token_b = b_data["access_token"]

        # Create a project as Alice, verify Bob cannot list or fetch it
        proj_a = client.post(
            "/api/v1/projects",
            json={
                "mode": "faceless",
                "title": "Alice Private Project",
                "source_mode": "topic",
                "topic": "Private AI video",
                "aspect_ratio": "9:16",
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert proj_a.status_code == 201
        proj_a_id = proj_a.json()["id"]

        bob_list = client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert bob_list.status_code == 200
        assert all(p["id"] != proj_a_id for p in bob_list.json())

        bob_get = client.get(
            f"/api/v1/projects/{proj_a_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert bob_get.status_code == 404


def test_system_config_writes_isolated_env_and_reloads_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify POST /api/v1/system/config updates an isolated .env without leaking secrets."""
    isolated_env = tmp_path / ".env"
    isolated_env.write_text(
        f"APP_ENV=development\nLOCAL_SANDBOX_MODE=true\nTEMP_DIR={tmp_path / 'temp'}\nSECRET_KEY={'b' * 64}\n",
        encoding="utf-8",
    )

    cfg = _sandbox_settings(tmp_path)
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    try:
        with TestClient(app) as client:
            app.state.env_file_path = isolated_env
            # Ensure Settings() inside update_system_config reads our isolated temp env
            monkeypatch.delenv("GEMINI_API_KEY", raising=False)

            resp = client.post(
                "/api/v1/system/config",
                json={
                    "updates": {
                        "GEMINI_API_KEY": "AIzaHotReloadTestValue999",
                        "NON_ALLOWLISTED_KEY": "should_be_ignored",
                    }
                },
                headers=headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["updated_keys"] == ["GEMINI_API_KEY"]
            assert body["requires_restart"] is False
            assert "AIzaHotReloadTestValue999" not in json.dumps(body)

            env_text = isolated_env.read_text(encoding="utf-8")
            assert "GEMINI_API_KEY=AIzaHotReloadTestValue999" in env_text
            assert "NON_ALLOWLISTED_KEY" not in env_text

            assert app.state.settings.GEMINI_API_KEY == "AIzaHotReloadTestValue999"
            assert app.state.job_queue.settings.GEMINI_API_KEY == "AIzaHotReloadTestValue999"
            assert app.state.cleanup_worker.settings.GEMINI_API_KEY == "AIzaHotReloadTestValue999"
    finally:
        get_settings.cache_clear()


def test_zero_key_sandbox_faceless_and_translator_fallbacks(tmp_path: Path) -> None:
    """Verify full zero-key SandboxLLM, SandboxVisual, SandboxSTT, swap-visual & local asset resolution."""
    cfg = _sandbox_settings(
        tmp_path,
        FFMPEG_BIN="__no_ffmpeg_for_fast_png_fallback__",
        FFPROBE_BIN="__no_ffprobe_for_fast_fallback__",
    )
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    with TestClient(app) as client:
        app.state.job_queue.enqueue = AsyncMock()

        created = client.post(
            "/api/v1/projects",
            json={
                "mode": "faceless",
                "title": "Zero Key Sandbox Demo",
                "source_mode": "topic",
                "topic": "Cách dùng web",
                "aspect_ratio": "9:16",
            },
            headers=headers,
        )
        assert created.status_code == 201
        project_id = created.json()["id"]

        repo = app.state.repository
        bus = SSEEventBus()
        asyncio.run(repo.update_project_status(project_id, ProjectStatus.PROCESSING))

        scenes = asyncio.run(
            run_faceless_prepare(
                project_id,
                {
                    "settings": cfg,
                    "repository": repo,
                    "billing_repo": repo,
                    "event_bus": bus,
                },
            )
        )
        assert len(scenes) >= 3
        first_scene = scenes[0]
        assert first_scene.visual_url and first_scene.visual_url.startswith(
            "/local-media/storage/sandbox_visuals/"
        )

        refreshed = asyncio.run(repo.get_project(project_id))
        assert refreshed.status == ProjectStatus.AWAITING_REVIEW

        # Verify swap-visual falls back cleanly to SandboxVisualProvider without PEXELS_API_KEY
        swap_resp = client.post(
            f"/api/v1/projects/{project_id}/scenes/{first_scene.id}/swap-visual",
            json={
                "query": "creative workspace",
                "visual_provider": "pexels",
                "media_type": "video",
            },
            headers=headers,
        )
        assert swap_resp.status_code == 200
        swapped_scene = swap_resp.json()
        assert swapped_scene["visual_url"].startswith("/local-media/storage/sandbox_visuals/")
        assert swapped_scene["visual_source"] == "ai_gen"

        # Verify _ensure_scene_local_asset resolves /local-media/storage/... directly without storage.get_signed_url
        storage_spy = MagicMock(spec=LocalStorageProvider)
        storage_spy.get_signed_url = AsyncMock(side_effect=AssertionError("should resolve locally"))
        visual_spy = MagicMock()
        ffmpeg_engine = FFmpegEngine(settings=cfg)
        loaded_detail = asyncio.run(repo.get_project_detail(project_id))
        target_scene = loaded_detail.scenes[0]
        local_path, media_type = asyncio.run(
            _ensure_scene_local_asset(
                scene=target_scene,
                project=refreshed,
                scenes_dir=cfg.temp_path / project_id / "scenes_check",
                manifest={},
                visual_provider=visual_spy,
                storage_provider=storage_spy,
                ffmpeg_engine=ffmpeg_engine,
                settings=cfg,
            )
        )
        assert local_path.is_file()
        assert media_type == "image"
        storage_spy.get_signed_url.assert_not_called()

        # Verify Translator Stage 2 STT + Stage 3 Translation zero-key fallback
        tr_proj = asyncio.run(
            repo.create_project(
                user_id="00000000-0000-0000-0000-000000000001",
                title="Zero Key Translator",
                mode=ProjectMode.TRANSLATOR,
                aspect_ratio=AspectRatio.PORTRAIT_9_16,
                source_mode="url",
                source_inputs={"source_url": "https://example.com/demo.mp4"},
                pipeline_config=PipelineConfig(target_lang="vi").model_dump(mode="json"),
            )
        )
        tr_proj = asyncio.run(repo.update_project_status(tr_proj.id, ProjectStatus.PROCESSING))
        wav_path = _write_tiny_wav(cfg.temp_path / str(tr_proj.id) / "vocals.wav", duration_sec=1.2)
        tr_cfg = PipelineConfig(target_lang="vi")

        transcript = asyncio.run(
            run_stage2_stt(
                vocals_path=wav_path,
                cfg=tr_cfg,
                settings=cfg,
                duration_ms=1200,
            )
        )
        assert len(transcript.segments) == 3
        assert len(transcript.segments[0].words) >= 1

        tr_scenes = asyncio.run(
            run_stage3_translate(
                project=tr_proj,
                transcript=transcript,
                cfg=tr_cfg,
                settings=cfg,
                repo=repo,
            )
        )
        assert len(tr_scenes) == 3
        assert tr_scenes[0].edited_text
        tr_after = asyncio.run(repo.get_project(tr_proj.id))
        assert tr_after.status == ProjectStatus.AWAITING_REVIEW


def test_pixabay_auto_switch_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify saving PIXABAY_API_KEY auto-switches VISUAL_PROVIDER and routes pexels fallback."""
    isolated_env = tmp_path / ".env"
    isolated_env.write_text(
        f"APP_ENV=development\nLOCAL_SANDBOX_MODE=true\nTEMP_DIR={tmp_path / 'temp'}\nSECRET_KEY={'c' * 64}\n",
        encoding="utf-8",
    )

    cfg = _sandbox_settings(tmp_path, PEXELS_API_KEY="", PIXABAY_API_KEY="")
    app = create_app(cfg)
    headers = {"Authorization": "Bearer sandbox-demo-token"}

    try:
        with TestClient(app) as client:
            app.state.env_file_path = isolated_env
            monkeypatch.delenv("PEXELS_API_KEY", raising=False)
            monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
            monkeypatch.delenv("VISUAL_PROVIDER", raising=False)

            resp = client.post(
                "/api/v1/system/config",
                json={"updates": {"PIXABAY_API_KEY": "pixabay-test-key-123"}},
                headers=headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "PIXABAY_API_KEY" in body["updated_keys"]
            assert "VISUAL_PROVIDER" in body["updated_keys"]

            env_text = isolated_env.read_text(encoding="utf-8")
            assert "VISUAL_PROVIDER=pixabay" in env_text
            assert app.state.settings.VISUAL_PROVIDER == "pixabay"

        emitted: list[tuple[str, int, str]] = []

        async def _capture_progress(stage: str, pct: int, message: str) -> None:
            emitted.append((stage, pct, message))

        fb_settings = _sandbox_settings(
            tmp_path,
            PEXELS_API_KEY="",
            PIXABAY_API_KEY="pixabay-test-key-123",
            VISUAL_PROVIDER="pexels",
        )
        resolved = asyncio.run(
            _resolve_faceless_visual_provider(
                PipelineConfig(visual_provider=VisualProviderName.PEXELS),
                fb_settings,
                {},
                _capture_progress,
                stage="visuals",
                pct=35,
            )
        )
        assert isinstance(resolved, PixabayProvider)
        assert any("pixabay" in msg.lower() for _, _, msg in emitted)
    finally:
        get_settings.cache_clear()


def test_gemini_structured_schema_sanitizes_exclusive_minimum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Gemini response_schema sanitization strips exclusiveMinimum and passes SDK validation."""
    from types import SimpleNamespace

    from google import genai
    from google.genai import _transformers, types

    from backend.models.provider import StoryboardResult
    from backend.providers.llm.gemini import (
        GeminiProvider,
        _TranslationBatchSchema,
        _build_gemini_response_schema,
    )
    from backend.services.ai_extras.marketing_kit import _MarketingKitLLMSchema

    sanitized = _build_gemini_response_schema(StoryboardResult)
    serialized = json.dumps(sanitized)
    for forbidden in ('"exclusiveMinimum"', '"exclusiveMaximum"', '"additionalProperties"', '"$schema"'):
        assert forbidden not in serialized

    scene_def = sanitized.get("$defs", {}).get("StoryboardScene", {})
    duration_prop = scene_def.get("properties", {}).get("duration_seconds", {})
    assert duration_prop.get("minimum") == 0.1

    for schema_cls in (StoryboardResult, _TranslationBatchSchema, _MarketingKitLLMSchema):
        built = _build_gemini_response_schema(schema_cls)
        sdk_schema = _transformers.t_schema(None, built)
        assert isinstance(sdk_schema, types.Schema)

    captured_configs: list[types.GenerateContentConfig] = []

    def fake_generate_content(*, model: str, contents: str, config: types.GenerateContentConfig) -> SimpleNamespace:
        del model, contents
        captured_configs.append(config)
        payload = {
            "title": "Sanitized Schema Storyboard",
            "total_duration_seconds": 3.5,
            "scenes": [
                {
                    "scene_index": 0,
                    "duration_seconds": 3.5,
                    "narration_text": "Hello sanitized schema",
                    "search_keywords": "minimal studio desk",
                    "image_prompt": "vertical studio scene",
                    "emotion_tone": "calm",
                }
            ],
        }
        return SimpleNamespace(parsed=None, text=json.dumps(payload))

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    monkeypatch.setattr(genai, "Client", lambda api_key: fake_client)

    provider = GeminiProvider(
        settings=_sandbox_settings(tmp_path, GEMINI_API_KEY="AIzaTestSanitizedSchemaKey12345"),
    )
    result = asyncio.run(
        provider.generate_storyboard(
            source_text="Test topic for schema check",
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
        )
    )
    assert isinstance(result, StoryboardResult)
    assert result.title == "Sanitized Schema Storyboard"
    assert len(result.scenes) == 1
    assert len(captured_configs) == 1
    assert captured_configs[0].response_schema is not None
    assert isinstance(
        captured_configs[0].response_schema,
        (types.Schema, dict),
    )


def test_local_storage_signed_url_staging_in_translator_downloader(tmp_path: Path) -> None:
    """Verify MediaDownloader stages /local-media/storage/... signed URLs and raises NotFoundError on missing."""
    from backend.core.exceptions import NotFoundError
    from backend.services.media.downloader import MediaDownloader

    cfg = _sandbox_settings(tmp_path, STORAGE_PROVIDER="local", LOCAL_SANDBOX_MODE=True)
    storage = LocalStorageProvider(settings=cfg)

    payload = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_demo_payload"
    src_upload = tmp_path / "upload_source.mp4"
    src_upload.write_bytes(payload)

    remote_key = "00000000-0000-0000-0000-000000000001/uploads/demo.mp4"
    stored = asyncio.run(storage.upload_file(src_upload, remote_key, content_type="video/mp4"))
    signed_url = asyncio.run(storage.get_signed_url(remote_key))
    assert signed_url.startswith("/local-media/storage/")
    assert stored.signed_url == signed_url

    downloader = MediaDownloader(settings=cfg)

    res_via_key = asyncio.run(
        downloader.stage_source_media(
            source_url=None,
            source_file_key=remote_key,
            work_dir=tmp_path / "job1",
            storage=storage,
        )
    )
    assert res_via_key.local_path.is_file()
    assert res_via_key.local_path.read_bytes() == payload

    res_via_local_url = asyncio.run(
        downloader.stage_source_media(
            source_url=None,
            source_file_key=signed_url,
            work_dir=tmp_path / "job2",
            storage=storage,
        )
    )
    assert res_via_local_url.local_path.is_file()
    assert res_via_local_url.local_path.read_bytes() == payload

    with pytest.raises(NotFoundError):
        asyncio.run(
            downloader.stage_source_media(
                source_url=None,
                source_file_key="/local-media/storage/missing.mp4",
                work_dir=tmp_path / "job3",
                storage=storage,
            )
        )


def test_selector_event_loop_subprocess_fallback_and_error_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify SelectorEventLoop NotImplementedError fallback, queue error codes, and zero direct create_subprocess_exec."""
    import sys

    from backend.core.subprocess_utils import run_subprocess_exec
    from backend.services.supabase.local_sandbox import LocalSandboxRepository
    from backend.workers.task_queue import AsyncJobQueue

    async def _raise_not_implemented(*_args: object, **_kwargs: object) -> object:
        raise NotImplementedError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_not_implemented)
    rc, stdout, stderr = asyncio.run(
        run_subprocess_exec(sys.executable, "-c", "print('ok-fallback')")
    )
    assert rc == 0
    assert b"ok-fallback" in stdout
    assert stderr == b""

    # Verify AsyncJobQueue distinguishes handler NotImplementedError from missing handler
    cfg = _sandbox_settings(tmp_path)
    repo = LocalSandboxRepository(settings=cfg)
    bus = SSEEventBus()

    async def _exercise_queue() -> tuple[ProjectStatus, str | None, str | None, list[dict[str, object]]]:
        queue = AsyncJobQueue(settings=cfg, repository=repo, event_bus=bus)

        proj_missing = await repo.create_project(
            user_id="00000000-0000-0000-0000-000000000001",
            title="Missing Handler Check",
            mode=ProjectMode.FACELESS,
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
            source_mode="topic",
            source_inputs={"topic": "Test"},
            pipeline_config={},
        )
        proj_boom = await repo.create_project(
            user_id="00000000-0000-0000-0000-000000000001",
            title="Handler NotImplemented Check",
            mode=ProjectMode.FACELESS,
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
            source_mode="topic",
            source_inputs={"topic": "Test"},
            pipeline_config={},
        )

        # Subscribe before firing so we inspect emitted error codes cleanly
        events: list[dict[str, object]] = []
        async with (
            bus.subscribe(str(proj_missing.id)) as q_missing,
            bus.subscribe(str(proj_boom.id)) as q_boom,
        ):
            await queue._execute_job(str(proj_missing.id), ProjectMode.FACELESS, "prepare", {})

            async def _raising_handler(_pid: str, _ctx: dict[str, object]) -> None:
                raise NotImplementedError()

            queue.register_handler(ProjectMode.FACELESS, "prepare", _raising_handler)
            await queue._execute_job(str(proj_boom.id), ProjectMode.FACELESS, "prepare", {})

            for q in (q_missing, q_boom):
                while not q.empty():
                    ev = q.get_nowait()
                    if ev is not None and ev.event == "error":
                        events.append(ev.data)

        after_missing = await repo.get_project(proj_missing.id)
        after_boom = await repo.get_project(proj_boom.id)
        return after_boom.status, after_boom.error_message, after_missing.error_message, events

    boom_status, boom_msg, missing_msg, error_events = asyncio.run(_exercise_queue())
    assert boom_status == ProjectStatus.FAILED
    assert boom_msg == "NotImplementedError"
    assert missing_msg and "not registered" in missing_msg.lower()
    codes = {ev["code"] for ev in error_events}
    assert codes == {"PIPELINE_NOT_REGISTERED", "INTERNAL_ERROR"}

    # Ensure no production module outside subprocess_utils.py calls create_subprocess_exec directly
    backend_root = Path(__file__).resolve().parents[1]
    allowed = {backend_root / "core" / "subprocess_utils.py"}
    offenders: list[str] = []
    for py_file in backend_root.rglob("*.py"):
        if "tests" in py_file.relative_to(backend_root).parts:
            continue
        if py_file.resolve() in {p.resolve() for p in allowed}:
            continue
        if "create_subprocess_exec" in py_file.read_text(encoding="utf-8"):
            offenders.append(str(py_file.relative_to(backend_root)))
    assert offenders == []
