"""Unit tests for Stage 3 media engines, scraper, billing/webhooks, and pipeline orchestrators."""

from __future__ import annotations

import json
import sys
import types
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
    InsufficientCreditsError,
    ProviderError,
    ValidationError,
)
from backend.core.security import CurrentUser, get_current_user
from backend.main import create_app
from backend.models.enums import (
    AspectRatio,
    CreditEntryType,
    PaymentGateway,
    PaymentStatus,
    ProjectMode,
    ProjectStatus,
    SubscriptionTier,
    VisualSource,
)
from backend.models.project import ProjectResponse, SubtitleStyleConfig
from backend.models.provider import (
    AudioSynthesisResult,
    SeparationResult,
    StoryboardResult,
    StoryboardScene,
    StoredObject,
    TranscriptResult,
    TranscriptSegment,
    TranslatedSegment,
    VisualAsset,
    WordTimestamp,
)
from backend.models.scene import SceneResponse
from backend.pipelines.faceless.orchestrator import run_faceless_prepare, run_faceless_render
from backend.pipelines.translator.orchestrator import (
    run_translator_prepare,
    run_translator_render,
)
from backend.providers.factory import ProviderFactory
from backend.services.billing.credit_service import CreditService
from backend.services.billing.sepay_webhook import PaymentWebhookService
from backend.services.billing.vietqr import VietQRService
from backend.services.media.ass_karaoke import (
    AssKaraokeGenerator,
    KaraokeSegmentInput,
    hex_to_ass_color,
)
from backend.services.media.ffmpeg_engine import FFmpegEngine, MediaProbe
from backend.services.media.watermark import build_watermark_filter, resolve_watermark_options
from backend.services.scraper.article_scraper import ArticleScraper


def _make_project(
    *,
    project_id: UUID | None = None,
    user_id: UUID | None = None,
    mode: ProjectMode = ProjectMode.FACELESS,
    status: ProjectStatus = ProjectStatus.PROCESSING,
    source_mode: str = "topic",
    source_inputs: dict | None = None,
    pipeline_config: dict | None = None,
) -> ProjectResponse:
    now = datetime.now(timezone.utc)
    return ProjectResponse(
        id=project_id or uuid4(),
        user_id=user_id or uuid4(),
        title="Stage 3 Test Project",
        mode=mode,
        status=status,
        aspect_ratio=AspectRatio.PORTRAIT_9_16,
        source_mode=source_mode,
        source_inputs=source_inputs or {"topic": "AI productivity tips"},
        pipeline_config=pipeline_config or {},
        progress_pct=0,
        current_stage=None,
        credits_estimated=0,
        credits_charged=0,
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
    text: str = "Xin chao cac ban",
    visual_url: str | None = None,
) -> SceneResponse:
    now = datetime.now(timezone.utc)
    return SceneResponse(
        id=scene_id or uuid4(),
        project_id=project_id or uuid4(),
        scene_index=scene_index,
        start_ms=start_ms,
        end_ms=end_ms,
        original_text=text,
        edited_text=text,
        narration_text=text,
        search_keywords="city night",
        image_prompt="neon city night",
        emotion_tone="exciting",
        visual_url=visual_url,
        visual_source=VisualSource.PEXELS,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 1. AssKaraokeGenerator Tests
# ---------------------------------------------------------------------------


def test_ass_karaoke_color_escaping_and_generation(tmp_path: Path) -> None:
    assert hex_to_ass_color("#FACC15", alpha=0) == "&H0015CCFA"
    assert hex_to_ass_color("#FFF", alpha=128) == "&H80FFFFFF"
    with pytest.raises(ValidationError):
        hex_to_ass_color("not-a-color")

    gen = AssKaraokeGenerator()
    out_ass = tmp_path / "captions.ass"
    segments = [
        KaraokeSegmentInput(
            start_ms=0,
            end_ms=2000,
            text="Hello {world}\nNow",
            words=[
                WordTimestamp(word="Hello", start_ms=0, end_ms=800),
                WordTimestamp(word="{world}\nNow", start_ms=900, end_ms=2000),
            ],
        ),
        KaraokeSegmentInput(
            start_ms=2100,
            end_ms=3600,
            text="Proportional fallback test",
            words=[],
        ),
    ]

    result_path = gen.generate(
        segments=segments,
        output_path=out_ass,
        aspect_ratio=AspectRatio.PORTRAIT_9_16,
        style=SubtitleStyleConfig(font_name="Inter", font_size=60, position="bottom"),
    )

    assert result_path.is_file()
    content = result_path.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert r"{\kf80}Hello {\k10}{\kf110}\{world\}\NNow" in content
    assert r"{\kf" in content
    assert "Proportional" in content


# ---------------------------------------------------------------------------
# 2. Watermark Tests
# ---------------------------------------------------------------------------


def test_watermark_free_vs_pro_bypass(tmp_path: Path) -> None:
    cfg = Settings(
        _env_file=None,
        FREE_TIER_WATERMARK_ENABLED=True,
        WATERMARK_TEXT="Studio:Demo",
        WATERMARK_POSITION="bottom_right",
        WATERMARK_OPACITY=0.7,
    )

    free_opts = resolve_watermark_options(SubscriptionTier.FREE, settings=cfg)
    assert free_opts.enabled is True
    filt, extra_args = build_watermark_filter(free_opts, 1080, 1920)
    assert filt is not None and "drawtext=" in filt
    assert r"Studio\:Demo" in filt
    assert extra_args == []

    # PNG image overlay branch
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"PNG")
    cfg_img = Settings(
        _env_file=None,
        FREE_TIER_WATERMARK_ENABLED=True,
        WATERMARK_IMAGE_PATH=str(logo),
    )
    img_opts = resolve_watermark_options("free", settings=cfg_img)
    img_filt, img_args = build_watermark_filter(img_opts, 1080, 1920)
    assert img_filt is not None and "overlay=" in img_filt
    assert img_args == ["-i", str(logo)]

    # Pro & Enterprise bypass
    for tier in (SubscriptionTier.PRO, SubscriptionTier.ENTERPRISE, "pro"):
        pro_opts = resolve_watermark_options(tier, settings=cfg)
        assert pro_opts.enabled is False
        assert build_watermark_filter(pro_opts, 1080, 1920) == (None, [])


# ---------------------------------------------------------------------------
# 3. FFmpegEngine Tests (mocked subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ffmpeg_engine_encoder_fallback_and_commands(tmp_path: Path) -> None:
    FFmpegEngine._encoder_cache.clear()
    cfg = Settings(
        _env_file=None,
        FFMPEG_HWACCEL="auto",
        FFMPEG_VIDEO_CODEC="h264_nvenc",
        FFMPEG_FALLBACK_CODEC="libx264",
    )
    engine = FFmpegEngine(settings=cfg)

    with (
        patch.object(
            engine,
            "_exec",
            new=AsyncMock(return_value=(b" V..... h264_nvenc\n V..... libx264\n", b"")),
        ),
        patch.object(engine, "_probe_encoder_usable", new=AsyncMock(return_value=False)),
    ):
        encoder = await engine.resolve_video_encoder()
        assert encoder == "libx264"

    # Test _run_encode_with_fallback when hwaccel encoder fails at runtime
    FFmpegEngine._encoder_cache.clear()
    calls: list[list[str]] = []

    async def fake_exec(args: list[str], _op: str) -> tuple[bytes, bytes]:
        calls.append(args)
        if "h264_nvenc" in args:
            raise ProviderError("ffmpeg", "nvenc init failed")
        return b"", b""

    with (
        patch.object(engine, "resolve_video_encoder", new=AsyncMock(return_value="h264_nvenc")),
        patch.object(engine, "_exec", side_effect=fake_exec),
    ):
        await engine._run_encode_with_fallback(lambda enc: ["ffmpeg", "-c:v", enc], "encode test")
    assert calls == [["ffmpeg", "-c:v", "h264_nvenc"], ["ffmpeg", "-c:v", "libx264"]]

    # Test mix_voice_and_bgm sidechaincompress & master_final_video command building
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.mp3"
    video = tmp_path / "assembled.mp4"
    ass_file = tmp_path / "subs.ass"
    for p in (voice, bgm, video, ass_file):
        p.write_bytes(b"stub")

    recorded_cmds: list[list[str]] = []

    async def capture_exec(args: list[str], _op: str) -> tuple[bytes, bytes]:
        recorded_cmds.append(args)
        return b"", b""

    with (
        patch.object(engine, "probe", new=AsyncMock(return_value=MediaProbe(duration_ms=4000, has_audio=True))),
        patch.object(engine, "resolve_video_encoder", new=AsyncMock(return_value="libx264")),
        patch.object(engine, "_exec", side_effect=capture_exec),
    ):
        await engine.mix_voice_and_bgm(voice, bgm, tmp_path / "mixed.wav", auto_ducking=True)
        (tmp_path / "mixed.wav").write_bytes(b"stub")
        await engine.master_final_video(
            video,
            tmp_path / "mixed.wav",
            tmp_path / "final.mp4",
            aspect_ratio=AspectRatio.PORTRAIT_9_16,
            ass_subtitle_path=ass_file,
            watermark=resolve_watermark_options(SubscriptionTier.FREE, settings=cfg),
        )

    assert any("sidechaincompress" in " ".join(cmd) for cmd in recorded_cmds)
    master_cmd = " ".join(recorded_cmds[-1])
    assert "ass=" in master_cmd
    assert "drawtext=" in master_cmd
    assert "libx264" in master_cmd


# ---------------------------------------------------------------------------
# 4. ArticleScraper Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_article_scraper_trafilatura_and_bs4_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = ArticleScraper()

    with pytest.raises(ValidationError):
        await scraper.scrape("ftp://invalid.example.com/article")

    sample_html = (
        "<html><head><title>Fallback Title</title></head>"
        "<body><article><p>First detailed paragraph with plenty of readable content here.</p>"
        "<p>Second informative paragraph explaining the topic clearly for viewers.</p>"
        "</article></body></html>"
    )
    mock_response = SimpleNamespace(
        status_code=200,
        text=sample_html,
        url="https://example.com/news",
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    # Case 1: trafilatura succeeds
    fake_traf = types.SimpleNamespace(
        extract=lambda *_a, **_kw: " ".join(["word"] * 25),
        extract_metadata=lambda *_a, **_kw: types.SimpleNamespace(title="Traf Title"),
    )
    monkeypatch.setitem(sys.modules, "trafilatura", fake_traf)
    with patch("backend.services.scraper.article_scraper.httpx.AsyncClient", return_value=mock_client):
        art = await scraper.scrape("https://example.com/news")
    assert art.title == "Traf Title"
    assert art.word_count == 25

    # Case 2: trafilatura returns None -> BeautifulSoup fallback
    fake_traf_empty = types.SimpleNamespace(
        extract=lambda *_a, **_kw: None,
        extract_metadata=lambda *_a, **_kw: None,
    )
    monkeypatch.setitem(sys.modules, "trafilatura", fake_traf_empty)
    with patch("backend.services.scraper.article_scraper.httpx.AsyncClient", return_value=mock_client):
        art_bs = await scraper.scrape("https://example.com/news")
    assert art_bs.title == "Fallback Title"
    assert "First detailed paragraph" in art_bs.text


# ---------------------------------------------------------------------------
# 5. CreditService, VietQRService & PaymentWebhookService Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_service_estimate_charge_and_refund() -> None:
    cfg = Settings(
        _env_file=None,
        CREDITS_PER_SECOND_TRANSLATOR=1,
        CREDITS_PER_SECOND_FACELESS=2,
        CREDITS_SURCHARGE_SEPARATION=10,
        CREDITS_SURCHARGE_PREMIUM_TTS=15,
        CREDITS_SURCHARGE_AI_VISUAL=20,
    )
    repo = MagicMock()
    repo.get_user_credits = AsyncMock(return_value={"balance": 30})
    repo.deduct_user_credits = AsyncMock(return_value={"status": "deducted", "new_balance": 10})
    repo.refund_user_credits = AsyncMock(return_value={"status": "refunded", "new_balance": 30})

    svc = CreditService(billing_repo=repo, settings=cfg)

    est = svc.estimate_project_credits(
        ProjectMode.TRANSLATOR,
        10.2,
        {"separation_provider": "replicate", "tts_provider": "elevenlabs"},
    )
    # ceil(10.2)*1 + 10 + 15 = 36
    assert est == 36

    with pytest.raises(InsufficientCreditsError):
        await svc.charge_for_project(uuid4(), uuid4(), 50)

    res = await svc.charge_for_project(uuid4(), uuid4(), 20)
    assert res["new_balance"] == 10
    repo.deduct_user_credits.assert_awaited_once()

    refund = await svc.refund_for_project(uuid4(), uuid4(), 20)
    assert refund["new_balance"] == 30
    repo.refund_user_credits.assert_awaited_once()


@pytest.mark.asyncio
async def test_vietqr_and_payment_webhook_services() -> None:
    cfg = Settings(
        _env_file=None,
        VIETQR_BANK_BIN="970422",
        VIETQR_ACCOUNT_NO="0123456789",
        VIETQR_ACCOUNT_NAME="NGUYEN VAN A",
        PAYMENT_MEMO_PREFIX="AVS",
        VND_PER_CREDIT=100,
        SEPAY_WEBHOOK_SECRET="sepay-secret-123",
        CASSO_WEBHOOK_SECRET="casso-secret-456",
    )
    uid = uuid4()
    tx_id = uuid4()
    repo = MagicMock()
    repo.create_pending_transaction = AsyncMock(
        return_value={"id": tx_id, "status": "pending", "created_at": datetime.now(timezone.utc)}
    )
    repo.process_payment_webhook = AsyncMock(
        return_value={"status": "completed", "idempotent": False, "credits_added": 500}
    )

    vietqr = VietQRService(billing_repo=repo, settings=cfg)
    intent = await vietqr.create_topup_intent(uid, 50000, gateway="sepay")
    assert intent.credits_amount == 500
    assert intent.reference_code.startswith("AVS")
    assert "970422-0123456789" in intent.qr_image_url

    webhook_svc = PaymentWebhookService(billing_repo=repo, settings=cfg)

    with pytest.raises(AuthenticationError):
        await webhook_svc.process_payload(
            {"id": "tx1", "content": intent.reference_code, "transferAmount": 50000},
            headers={"Authorization": "Bearer wrong-secret"},
            gateway="sepay",
        )

    sepay_res = await webhook_svc.process_payload(
        {
            "id": 9991,
            "transferType": "in",
            "transferAmount": 50000,
            "content": f"CK {intent.reference_code} nap credit",
        },
        headers={"Authorization": "Apikey sepay-secret-123"},
        gateway="sepay",
    )
    assert len(sepay_res) == 1
    assert sepay_res[0]["external_tx_id"] == "sepay:9991"
    assert sepay_res[0]["reference_code"] == intent.reference_code

    casso_res = await webhook_svc.process_payload(
        {
            "error": 0,
            "data": [
                {
                    "tid": "FT2401",
                    "amount": 50000,
                    "description": f"Thanh toan {intent.reference_code}",
                }
            ],
        },
        headers={"secure-token": "casso-secret-456"},
        gateway="casso",
    )
    assert len(casso_res) == 1
    assert casso_res[0]["external_tx_id"] == "casso:FT2401"


# ---------------------------------------------------------------------------
# 6. FastAPI Stage 3 Routes: /api/webhooks/payment & /api/v1/billing/*
# ---------------------------------------------------------------------------


def test_webhook_and_billing_api_routes(tmp_path: Path) -> None:
    uid = uuid4()
    tx_id = uuid4()
    cfg = Settings(
        _env_file=None,
        TEMP_DIR=tmp_path / "temp",
        AUTO_CLEANUP_ENABLED=False,
        SEPAY_WEBHOOK_SECRET="secret-sepay",
        VIETQR_BANK_BIN="970422",
        VIETQR_ACCOUNT_NO="0123456789",
    )
    app = create_app(cfg)

    mock_billing_repo = MagicMock()
    mock_billing_repo.get_user_credits = AsyncMock(
        return_value={
            "user_id": str(uid),
            "balance": 150,
            "lifetime_purchased": 100,
            "lifetime_spent": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    mock_billing_repo.list_credit_ledger = AsyncMock(
        return_value=[
            {
                "id": str(uuid4()),
                "user_id": str(uid),
                "entry_type": CreditEntryType.TOPUP.value,
                "amount": 100,
                "balance_after": 150,
                "description": "Top-up",
                "metadata": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )
    mock_billing_repo.create_pending_transaction = AsyncMock(
        return_value={
            "id": str(tx_id),
            "status": PaymentStatus.PENDING.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    mock_billing_repo.get_transaction_by_id = AsyncMock(
        return_value={
            "id": str(tx_id),
            "user_id": str(uid),
            "gateway": PaymentGateway.SEPAY.value,
            "reference_code": "AVS1234ABCD",
            "external_tx_id": None,
            "amount_vnd": 50000,
            "credits_amount": 500,
            "status": PaymentStatus.PENDING.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
    )
    mock_billing_repo.process_payment_webhook = AsyncMock(
        return_value={"status": "completed", "idempotent": False, "credits_added": 500}
    )

    app.state.billing_repository = mock_billing_repo
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(uid),
        email="billing@example.com",
        role="authenticated",
    )

    with TestClient(app) as client:
        # Lifespan registered translator & faceless handlers
        assert app.state.job_queue.get_handler(ProjectMode.TRANSLATOR, "prepare") is not None
        assert app.state.job_queue.get_handler(ProjectMode.TRANSLATOR, "render") is not None
        assert app.state.job_queue.get_handler(ProjectMode.FACELESS, "prepare") is not None
        assert app.state.job_queue.get_handler(ProjectMode.FACELESS, "render") is not None

        credits_resp = client.get("/api/v1/billing/credits")
        assert credits_resp.status_code == 200
        assert credits_resp.json()["balance"] == 150
        assert len(credits_resp.json()["credit_ledger"]) == 1

        intent_resp = client.post(
            "/api/v1/billing/topup-intent",
            json={"amount_vnd": 50000, "gateway": "sepay"},
        )
        assert intent_resp.status_code == 201
        assert intent_resp.json()["credits_amount"] == 500

        tx_resp = client.get(f"/api/v1/billing/transactions/{tx_id}")
        assert tx_resp.status_code == 200
        assert tx_resp.json()["reference_code"] == "AVS1234ABCD"

        bad_wh = client.post(
            "/api/webhooks/payment",
            json={"id": "1", "transferAmount": 50000, "content": "AVS1234ABCD"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert bad_wh.status_code == 401

        ok_wh = client.post(
            "/api/webhooks/payment",
            json={
                "id": "tx-ok-1",
                "transferType": "in",
                "transferAmount": 50000,
                "content": "AVS1234ABCD",
            },
            headers={"Authorization": "Bearer secret-sepay"},
        )
        assert ok_wh.status_code == 200
        assert ok_wh.json()["success"] is True
        assert ok_wh.json()["results"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# 7. Translator & Faceless Orchestrator End-to-End Mocked Flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_translator_orchestrator_prepare_render_and_cleanup(tmp_path: Path) -> None:
    pid = uuid4()
    uid = uuid4()
    cfg = Settings(_env_file=None, TEMP_DIR=tmp_path / "temp", CLEANUP_AFTER_RENDER=True)
    work_dir = cfg.temp_path / str(pid)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_video = work_dir / "source.mp4"
    source_video.write_bytes(b"video")
    audio_orig = work_dir / "audio_orig.wav"
    audio_stt = work_dir / "audio_stt.wav"
    audio_orig.write_bytes(b"wav")
    audio_stt.write_bytes(b"wav")

    project = _make_project(
        project_id=pid,
        user_id=uid,
        mode=ProjectMode.TRANSLATOR,
        status=ProjectStatus.PROCESSING,
        source_mode="url",
        source_inputs={"source_url": "https://youtube.com/watch?v=abc"},
        pipeline_config={"burn_subtitles": True},
    )
    scene = _make_scene(project_id=pid, scene_index=0, start_ms=0, end_ms=2500, text="Xin chao")

    repo = MagicMock()
    repo.get_project = AsyncMock(return_value=project)
    repo.update_project = AsyncMock(return_value=project)
    repo.update_project_progress = AsyncMock(return_value=project)
    repo.update_project_status = AsyncMock(return_value=project)
    repo.delete_project_scenes = AsyncMock()
    repo.upsert_scenes = AsyncMock(return_value=[scene])
    repo.list_scenes = AsyncMock(return_value=[scene])
    repo.get_profile = AsyncMock(return_value={"subscription_tier": "pro"})
    repo.get_user_credits = AsyncMock(return_value={"balance": 500})
    repo.deduct_user_credits = AsyncMock(return_value={"status": "deducted", "new_balance": 490})
    repo.mark_temp_cleaned = AsyncMock()

    bus = SSEEventBus()

    fake_sep = SimpleNamespace(
        provider_name="fast_mode",
        separate=AsyncMock(
            return_value=SeparationResult(
                vocals_path=audio_orig,
                bgm_path=audio_orig,
                source_audio_path=audio_orig,
                mode="fast_mode",
            )
        ),
    )
    fake_stt = SimpleNamespace(
        provider_name="groq",
        transcribe=AsyncMock(
            return_value=TranscriptResult(
                language="en",
                duration_ms=2500,
                full_text="Hello",
                segments=[TranscriptSegment(index=0, start_ms=0, end_ms=2500, text="Hello")],
            )
        ),
    )
    fake_llm = SimpleNamespace(
        provider_name="gemini",
        translate_time_synced=AsyncMock(
            return_value=[
                TranslatedSegment(
                    index=0,
                    start_ms=0,
                    end_ms=2500,
                    original_text="Hello",
                    translated_text="Xin chao",
                )
            ]
        ),
    )

    async def fake_tts_synthesize(
        text: str, voice_id: str, output_path: Path, rate_pct: int = 0, **_kwargs: object
    ):
        del text
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp3")
        return AudioSynthesisResult(
            audio_path=output_path,
            duration_ms=2000,
            voice_id=voice_id,
            rate_pct=rate_pct,
            provider="edge_tts",
        )

    fake_tts = SimpleNamespace(provider_name="edge_tts", synthesize=fake_tts_synthesize)
    fake_storage = SimpleNamespace(
        provider_name="supabase",
        upload_file=AsyncMock(
            return_value=StoredObject(
                bucket="video-studio-assets",
                remote_key=f"{uid}/{pid}/final.mp4",
                signed_url="https://signed.example/final.mp4",
            )
        ),
        get_signed_url=AsyncMock(return_value="https://signed.example/final.mp4"),
    )

    with (
        patch.object(ProviderFactory, "get_separation", return_value=fake_sep),
        patch.object(ProviderFactory, "get_stt", return_value=fake_stt),
        patch.object(ProviderFactory, "get_llm", return_value=fake_llm),
        patch.object(ProviderFactory, "get_tts", return_value=fake_tts),
        patch.object(ProviderFactory, "get_storage", return_value=fake_storage),
        patch(
            "backend.pipelines.translator.stage1_separate.MediaDownloader.stage_source_media",
            new=AsyncMock(return_value=SimpleNamespace(local_path=source_video, duration_seconds=2.5)),
        ),
        patch(
            "backend.pipelines.translator.stage1_separate.FFmpegEngine.probe",
            new=AsyncMock(return_value=MediaProbe(duration_ms=2500, width=1080, height=1920, has_audio=True)),
        ),
        patch(
            "backend.pipelines.translator.stage1_separate.FFmpegEngine.extract_audio",
            new=AsyncMock(side_effect=[audio_stt, audio_orig]),
        ),
        patch(
            "backend.pipelines.translator.stage4_dub_mix.FFmpegEngine.probe",
            new=AsyncMock(return_value=MediaProbe(duration_ms=2500, width=1080, height=1920, has_audio=True)),
        ),
        patch(
            "backend.pipelines.translator.stage4_dub_mix.FFmpegEngine.build_dubbed_audio_track",
            new=AsyncMock(
                side_effect=lambda *args, **kw: (
                    (out := kw.get("output_wav") or args[2]).write_bytes(b"dub"),
                    out,
                )[1]
            ),
        ),
        patch(
            "backend.pipelines.translator.stage4_dub_mix.FFmpegEngine.mix_voice_and_bgm",
            new=AsyncMock(
                side_effect=lambda *args, **kw: (
                    (out := kw.get("output_audio") or args[2]).write_bytes(b"mix"),
                    out,
                )[1]
            ),
        ),
        patch(
            "backend.pipelines.translator.stage4_dub_mix.FFmpegEngine.master_final_video",
            new=AsyncMock(
                side_effect=lambda *args, **kw: (
                    (out := kw.get("output_path") or args[2]).write_bytes(b"mp4"),
                    out,
                )[1]
            ),
        ),
    ):
        prep_res = await run_translator_prepare(
            str(pid),
            {"settings": cfg, "repository": repo, "event_bus": bus},
        )
        assert prep_res["status"] == ProjectStatus.AWAITING_REVIEW.value
        assert prep_res["scenes_count"] == 1
        assert (work_dir / "translator_state.json").is_file()

        render_res = await run_translator_render(
            str(pid),
            {"settings": cfg, "repository": repo, "event_bus": bus},
        )
        assert render_res["status"] == ProjectStatus.COMPLETED.value
        assert render_res["output_video_url"] == "https://signed.example/final.mp4"
        assert not work_dir.exists()
        repo.mark_temp_cleaned.assert_awaited_once_with(str(pid))


@pytest.mark.asyncio
async def test_faceless_orchestrator_prepare_render_and_cleanup(tmp_path: Path) -> None:
    pid = uuid4()
    uid = uuid4()
    cfg = Settings(_env_file=None, TEMP_DIR=tmp_path / "temp", CLEANUP_AFTER_RENDER=True)
    work_dir = cfg.temp_path / str(pid)
    scenes_dir = work_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    local_clip = scenes_dir / "scene_000.mp4"
    local_clip.write_bytes(b"clip")

    project = _make_project(
        project_id=pid,
        user_id=uid,
        mode=ProjectMode.FACELESS,
        status=ProjectStatus.PROCESSING,
        source_mode="topic",
        source_inputs={"topic": "Future of AI"},
    )
    scene = _make_scene(
        project_id=pid,
        scene_index=0,
        start_ms=0,
        end_ms=3500,
        text="AI is transforming video creation",
        visual_url="https://pexels.example/clip.mp4",
    )

    repo = MagicMock()
    repo.get_project = AsyncMock(return_value=project)
    repo.update_project = AsyncMock(return_value=project)
    repo.update_project_progress = AsyncMock(return_value=project)
    repo.update_project_status = AsyncMock(return_value=project)
    repo.delete_project_scenes = AsyncMock()
    repo.upsert_scenes = AsyncMock(return_value=[scene])
    repo.list_scenes = AsyncMock(return_value=[scene])
    repo.get_profile = AsyncMock(return_value={"subscription_tier": "free"})
    repo.get_user_credits = AsyncMock(return_value={"balance": 500})
    repo.deduct_user_credits = AsyncMock(return_value={"status": "deducted", "new_balance": 495})
    repo.mark_temp_cleaned = AsyncMock()

    bus = SSEEventBus()

    fake_llm = SimpleNamespace(
        provider_name="gemini",
        generate_storyboard=AsyncMock(
            return_value=StoryboardResult(
                title="AI Future",
                total_duration_seconds=3.5,
                scenes=[
                    StoryboardScene(
                        scene_index=0,
                        duration_seconds=3.5,
                        narration_text="AI is transforming video creation",
                        search_keywords="futuristic city",
                        image_prompt="neon futuristic city",
                        emotion_tone="inspiring",
                    )
                ],
            )
        ),
    )
    fake_visual = SimpleNamespace(
        provider_name="pexels",
        fetch_visual=AsyncMock(
            return_value=VisualAsset(
                url="https://pexels.example/clip.mp4",
                local_path=local_clip,
                media_type="video",
                source=VisualSource.PEXELS,
                duration_seconds=4.0,
            )
        ),
    )

    async def fake_tts_synthesize(
        text: str, voice_id: str, output_path: Path, rate_pct: int = 0, **_kwargs: object
    ):
        del text
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp3")
        return AudioSynthesisResult(
            audio_path=output_path,
            duration_ms=3200,
            voice_id=voice_id,
            rate_pct=rate_pct,
            provider="edge_tts",
        )

    fake_tts = SimpleNamespace(provider_name="edge_tts", synthesize=fake_tts_synthesize)
    fake_storage = SimpleNamespace(
        provider_name="supabase",
        upload_file=AsyncMock(
            return_value=StoredObject(
                bucket="video-studio-assets",
                remote_key=f"{uid}/{pid}/final.mp4",
                signed_url="https://signed.example/faceless.mp4",
            )
        ),
        get_signed_url=AsyncMock(return_value="https://signed.example/faceless.mp4"),
    )

    fake_ffmpeg = MagicMock(spec=FFmpegEngine)
    fake_ffmpeg.build_dubbed_audio_track = AsyncMock(
        side_effect=lambda *args, **kw: (
            (out := kw.get("output_wav") or args[2]).write_bytes(b"vo"),
            out,
        )[1]
    )
    fake_ffmpeg.prepare_scene_clip = AsyncMock(
        side_effect=lambda *args, **kw: (
            (out := kw.get("output_path") or args[3]).parent.mkdir(parents=True, exist_ok=True),
            out.write_bytes(b"norm"),
            out,
        )[2]
    )
    fake_ffmpeg.concat_video_clips = AsyncMock(
        side_effect=lambda *args, **kw: (
            (out := kw.get("output_path") or args[1]).write_bytes(b"concat"),
            out,
        )[1]
    )
    fake_ffmpeg.mix_voice_and_bgm = AsyncMock(
        side_effect=lambda *args, **kw: (
            (out := kw.get("output_audio") or args[2]).write_bytes(b"mix"),
            out,
        )[1]
    )
    fake_ffmpeg.master_final_video = AsyncMock(
        side_effect=lambda *args, **kw: (
            (out := kw.get("output_path") or args[2]).write_bytes(b"final"),
            out,
        )[1]
    )

    scenes = await run_faceless_prepare(
        str(pid),
        {
            "settings": cfg,
            "repository": repo,
            "billing_repo": repo,
            "event_bus": bus,
            "llm_provider": fake_llm,
            "visual_provider": fake_visual,
        },
    )
    assert len(scenes) == 1
    assert (scenes_dir / "manifest.json").is_file()

    # Ensure manifest points to our existing local_clip
    manifest = json.loads((scenes_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["0"]["local_path"] == str(local_clip)

    output_url = await run_faceless_render(
        str(pid),
        {
            "settings": cfg,
            "repository": repo,
            "event_bus": bus,
            "tts_provider": fake_tts,
            "visual_provider": fake_visual,
            "storage_provider": fake_storage,
            "ffmpeg_engine": fake_ffmpeg,
        },
    )
    assert output_url == "https://signed.example/faceless.mp4"
    assert not work_dir.exists()
    repo.mark_temp_cleaned.assert_awaited_once_with(str(pid))
