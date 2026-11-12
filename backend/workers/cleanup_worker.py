"""Background janitor worker and helper for cleaning stale temp/{job_id}/ directories."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from uuid import UUID

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)


async def cleanup_job_dir(
    job_id: str | UUID,
    *,
    temp_dir: Path | None = None,
    repository: SupabaseRepository | None = None,
) -> bool:
    """Delete ``temp/{job_id}/`` safely and record ``temp_cleaned_at`` when possible."""
    cfg = get_settings()
    root_dir = (temp_dir or cfg.temp_path).resolve()
    clean_id = str(job_id).strip()
    if not clean_id or clean_id in (".", "..") or "/" in clean_id or "\\" in clean_id:
        logger.warning("Refusing cleanup for unsafe job_id '%s'", job_id)
        return False

    target = (root_dir / clean_id).resolve()
    try:
        target.relative_to(root_dir)
    except ValueError:
        logger.warning("Refusing cleanup outside TEMP_DIR for job_id '%s'", job_id)
        return False

    removed = False
    if target.exists() and target.is_dir():
        await asyncio.to_thread(shutil.rmtree, target)
        removed = True
        logger.info("Deleted temp directory for job %s (%s)", clean_id, target)

    if repository is not None:
        try:
            await repository.mark_temp_cleaned(clean_id)
        except Exception as exc:
            logger.warning("Failed to mark temp_cleaned_at for job %s: %s", clean_id, exc)

    return removed


class AutoCleanupWorker:
    """Periodic asyncio worker removing temp/{job_id}/ directories older than TEMP_TTL_HOURS."""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: SupabaseRepository | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Return True if the background cleanup loop task is currently active."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic cleanup loop when AUTO_CLEANUP_ENABLED is True."""
        if not self.settings.AUTO_CLEANUP_ENABLED:
            logger.info("AutoCleanupWorker disabled via AUTO_CLEANUP_ENABLED=false")
            return
        if self.is_running:
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="auto-cleanup-worker")
        logger.info(
            "Started AutoCleanupWorker (interval=%dm, ttl=%dh, temp_dir=%s)",
            self.settings.AUTO_CLEANUP_INTERVAL_MINUTES,
            self.settings.TEMP_TTL_HOURS,
            self.settings.temp_path,
        )

    async def stop(self) -> None:
        """Stop the periodic cleanup loop cleanly."""
        self._stop_event.set()
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Stopped AutoCleanupWorker")

    async def run_once(self, *, now: float | None = None) -> list[str]:
        """Scan TEMP_DIR once and delete any job subdirectories older than TEMP_TTL_HOURS."""
        temp_root = self.settings.temp_path
        if not temp_root.exists():
            return []

        ttl_seconds = self.settings.TEMP_TTL_HOURS * 3600
        current_time = now if now is not None else time.time()
        cleaned_ids: list[str] = []

        for entry in list(temp_root.iterdir()):
            if entry.name == ".gitkeep" or not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
                age_seconds = current_time - mtime
                if age_seconds >= ttl_seconds:
                    await asyncio.to_thread(shutil.rmtree, entry)
                    cleaned_ids.append(entry.name)
                    logger.info(
                        "AutoCleanup removed stale temp dir '%s' (age=%.1fh)",
                        entry.name,
                        age_seconds / 3600.0,
                    )
                    if self.repository is not None:
                        try:
                            await self.repository.mark_temp_cleaned(entry.name)
                        except Exception as exc:
                            logger.warning(
                                "Failed to mark temp_cleaned_at for '%s': %s", entry.name, exc
                            )
            except Exception as exc:
                logger.warning("Error cleaning temp entry '%s': %s", entry, exc)

        return cleaned_ids

    async def _run_loop(self) -> None:
        interval_seconds = max(1, self.settings.AUTO_CLEANUP_INTERVAL_MINUTES) * 60
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("AutoCleanupWorker pass failed: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
