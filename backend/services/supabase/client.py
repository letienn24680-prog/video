"""Supabase client factory using service_role credentials for backend operations."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConfigurationError
from backend.core.logging import get_logger

if TYPE_CHECKING:
    from supabase import Client

logger = get_logger(__name__)

_PLACEHOLDER_URLS = {"", "https://your-project-ref.supabase.co"}
_PLACEHOLDER_KEYS = {
    "",
    "your_supabase_anon_key_here",
    "your_supabase_service_role_key_here",
}

_client_lock = threading.Lock()
_admin_clients: dict[tuple[str, str], Client] = {}
_anon_clients: dict[tuple[str, str], Client] = {}


def _import_create_client():
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ConfigurationError(
            "The 'supabase' Python package is not installed. Run: pip install supabase>=2.10.0"
        ) from exc
    return create_client


def _validate_url(url: str) -> str:
    clean_url = (url or "").strip()
    if clean_url in _PLACEHOLDER_URLS or not clean_url.startswith(("http://", "https://")):
        raise ConfigurationError("SUPABASE_URL is missing or uses an unconfigured placeholder.")
    return clean_url


def _validate_key(key: str, var_name: str) -> str:
    clean_key = (key or "").strip()
    if clean_key in _PLACEHOLDER_KEYS:
        raise ConfigurationError(f"{var_name} is missing or uses an unconfigured placeholder.")
    return clean_key


def get_supabase_client(settings: Settings | None = None) -> Client:
    """Return cached Supabase admin Client initialized with SUPABASE_SERVICE_ROLE_KEY."""
    cfg = settings or get_settings()
    url = _validate_url(cfg.SUPABASE_URL)
    service_key = _validate_key(cfg.SUPABASE_SERVICE_ROLE_KEY, "SUPABASE_SERVICE_ROLE_KEY")

    cache_key = (url, service_key)
    with _client_lock:
        client = _admin_clients.get(cache_key)
        if client is None:
            create_client = _import_create_client()
            client = create_client(url, service_key)
            _admin_clients[cache_key] = client
            logger.debug("Initialized Supabase service_role client")
        return client


def get_supabase_anon_client(settings: Settings | None = None) -> Client:
    """Return cached Supabase client initialized with SUPABASE_ANON_KEY."""
    cfg = settings or get_settings()
    url = _validate_url(cfg.SUPABASE_URL)
    anon_key = _validate_key(cfg.SUPABASE_ANON_KEY, "SUPABASE_ANON_KEY")

    cache_key = (url, anon_key)
    with _client_lock:
        client = _anon_clients.get(cache_key)
        if client is None:
            create_client = _import_create_client()
            client = create_client(url, anon_key)
            _anon_clients[cache_key] = client
            logger.debug("Initialized Supabase anon client")
        return client


def reset_supabase_clients() -> None:
    """Clear cached Supabase client instances (useful in tests)."""
    with _client_lock:
        _admin_clients.clear()
        _anon_clients.clear()
