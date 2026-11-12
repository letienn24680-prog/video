"""Tests for Stage 4 frontend static serving and /api/v1/public-config endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.main import create_app


def test_frontend_static_serving_and_public_config(tmp_path: Path) -> None:
    cfg = Settings(
        _env_file=None,
        APP_NAME="AI Video Studio",
        TEMP_DIR=tmp_path / "temp",
        AUTO_CLEANUP_ENABLED=False,
        SUPABASE_URL="https://demo-project.supabase.co",
        SUPABASE_ANON_KEY="public-anon-key-demo",
        SUPABASE_SERVICE_ROLE_KEY="SUPER-SECRET-SERVICE-ROLE-KEY",
        SUPABASE_JWT_SECRET="SUPER-SECRET-JWT-SIGNING-KEY",
        GROQ_API_KEY="gsk-super-secret-groq",
        GEMINI_API_KEY="AIza-super-secret-gemini",
        ELEVENLABS_API_KEY="sk-super-secret-elevenlabs",
        PEXELS_API_KEY="super-secret-pexels",
        REPLICATE_API_TOKEN="r8-super-secret-replicate",
        SEPAY_WEBHOOK_SECRET="super-secret-sepay",
    )

    app = create_app(cfg)

    with TestClient(app) as client:
        index_resp = client.get("/")
        assert index_resp.status_code == 200
        assert "text/html" in index_resp.headers["content-type"]
        assert "AI Video Studio" in index_resp.text

        css_resp = client.get("/css/studio.css")
        assert css_resp.status_code == 200

        js_resp = client.get("/js/app.js")
        assert js_resp.status_code == 200

        cfg_resp = client.get("/api/v1/public-config")
        assert cfg_resp.status_code == 200
        data = cfg_resp.json()

        assert data["app_name"] == "AI Video Studio"
        assert data["supabase_url"] == "https://demo-project.supabase.co"
        assert data["supabase_anon_key"] == "public-anon-key-demo"
        assert data["max_batch_size"] == cfg.MAX_BATCH_SIZE
        assert data["max_upload_mb"] == cfg.MAX_UPLOAD_MB
        assert data["vnd_per_credit"] == cfg.VND_PER_CREDIT
        assert "tts" in data["default_providers"]

        serialized = json.dumps(data)
        for secret_value in (
            "SUPER-SECRET-SERVICE-ROLE-KEY",
            "SUPER-SECRET-JWT-SIGNING-KEY",
            "gsk-super-secret-groq",
            "AIza-super-secret-gemini",
            "sk-super-secret-elevenlabs",
            "super-secret-pexels",
            "r8-super-secret-replicate",
            "super-secret-sepay",
        ):
            assert secret_value not in serialized

        for secret_key in (
            "supabase_service_role_key",
            "supabase_jwt_secret",
            "groq_api_key",
            "gemini_api_key",
            "elevenlabs_api_key",
            "pexels_api_key",
            "replicate_api_token",
            "sepay_webhook_secret",
        ):
            assert secret_key not in data

        # Existing API & docs routes remain unaffected
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200
