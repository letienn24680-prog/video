"""Async job queue enforcing concurrency limits, timeouts, SSE progress, and error refunds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID

from backend.core.config import Settings, get_settings
from backend.core.events import SSEEventBus, get_event_bus
from backend.core.exceptions import InvalidStateTransitionError, StudioError, ValidationError
from backend.core.logging import get_logger, reset_job_id, set_job_id
from backend.models.enums import ProjectMode, ProjectStatus, can_transition
from backend.services.supabase.repository import SupabaseRepository

logger = get_logger(__name__)

JobPhase = Literal["prepare", "render"]
PipelineHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class _UnregisteredPipelineHandlerError(Exception):
    """Raised internally when no handler is registered for a (mode, phase) pair."""


class AsyncJobQueue:
    """In-process asyncio task queue with bounded concurrency and lifecycle hooks."""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: SupabaseRepository | None = None,
        event_bus: SSEEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or SupabaseRepository()
        self.event_bus = event_bus or get_event_bus()
        self._semaphore = asyncio.Semaphore(self.settings.MAX_CONCURRENT_JOBS)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._handlers: dict[tuple[ProjectMode, JobPhase], PipelineHandler] = {}
        self._shutting_down = False

    @staticmethod
    def _normalize_mode(mode: ProjectMode | str) -> ProjectMode:
        if isinstance(mode, ProjectMode):
            return mode
        try:
            return ProjectMode(str(mode))
        except ValueError as exc:
            raise ValidationError(f"Unsupported project mode: {mode}") from exc

    @staticmethod
    def _normalize_phase(phase: str) -> JobPhase:
        if phase not in ("prepare", "render"):
            raise ValidationError(f"Unsupported job phase '{phase}'; expected 'prepare' or 'render'")
        return phase  # type: ignore[return-value]

    def register_handler(
        self,
        mode: ProjectMode | str,
        phase: JobPhase,
        fn: PipelineHandler,
    ) -> None:
        """Register an async pipeline handler for a (ProjectMode, phase) pair."""
        mode_enum = self._normalize_mode(mode)
        phase_val = self._normalize_phase(phase)
        self._handlers[(mode_enum, phase_val)] = fn
        logger.debug("Registered pipeline handler for (%s, %s)", mode_enum.value, phase_val)

    def get_handler(self, mode: ProjectMode | str, phase: JobPhase) -> PipelineHandler | None:
        """Return the registered handler for (mode, phase), or None if unregistered."""
        mode_enum = self._normalize_mode(mode)
        phase_val = self._normalize_phase(phase)
        return self._handlers.get((mode_enum, phase_val))

    def active_count(self) -> int:
        """Return the number of currently tracked, unfinished job tasks."""
        return sum(1 for task in self._tasks.values() if not task.done())

    def is_running(self, project_id: str | UUID) -> bool:
        """Return True if ``project_id`` currently has an active task."""
        task = self._tasks.get(str(project_id))
        return task is not None and not task.done()

    async def emit_progress(
        self,
        project_id: str | UUID,
        stage: str,
        pct: int,
        message: str = "",
        *,
        event_type: str = "progress",
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        """Persist progress to Supabase and broadcast an SSE event to subscribers."""
        pid = str(project_id)
        clamped_pct = max(0, min(100, int(pct)))

        try:
            await self.repository.update_project_progress(
                pid,
                progress_pct=clamped_pct,
                current_stage=stage,
            )
        except Exception as exc:
            logger.warning("Failed to persist progress for project %s: %s", pid, exc)

        payload: dict[str, Any] = {
            "project_id": pid,
            "stage": stage,
            "progress_pct": clamped_pct,
            "message": message,
        }
        if extra_data:
            payload.update(extra_data)

        await self.event_bus.publish(pid, event_type, payload)

    async def enqueue(
        self,
        project_id: str | UUID,
        mode: ProjectMode | str,
        phase: JobPhase = "prepare",
        context: dict[str, Any] | None = None,
    ) -> asyncio.Task[Any]:
        """Schedule a pipeline job task for ``project_id`` and return the asyncio.Task."""
        if self._shutting_down:
            raise InvalidStateTransitionError(
                "Task queue is shutting down and cannot accept new jobs",
                status_code=503,
            )

        pid = str(project_id)
        mode_enum = self._normalize_mode(mode)
        phase_val = self._normalize_phase(phase)

        existing = self._tasks.get(pid)
        if existing is not None and not existing.done():
            raise InvalidStateTransitionError(
                f"Project {pid} already has an active job in progress",
                details={"project_id": pid},
            )

        job_context: dict[str, Any] = {
            **(context or {}),
            "project_id": pid,
            "mode": mode_enum.value,
            "phase": phase_val,
            "queue": self,
        }

        task = asyncio.create_task(
            self._execute_job(pid, mode_enum, phase_val, job_context),
            name=f"job-{pid}-{phase_val}",
        )
        self._tasks[pid] = task
        task.add_done_callback(lambda done_task: self._cleanup_task_ref(pid, done_task))
        logger.info("Enqueued %s job for project %s (mode=%s)", phase_val, pid, mode_enum.value)
        return task

    def _cleanup_task_ref(self, pid: str, done_task: asyncio.Task[Any]) -> None:
        if self._tasks.get(pid) is done_task:
            self._tasks.pop(pid, None)

    async def cancel(self, project_id: str | UUID) -> bool:
        """Cancel an active task for ``project_id`` if one is running."""
        pid = str(project_id)
        task = self._tasks.get(pid)
        if task is None or task.done():
            return False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Cancelled task for project %s raised %s during teardown", pid, exc)
        logger.info("Cancelled running task for project %s", pid)
        return True

    async def shutdown(self, timeout_seconds: float = 10.0) -> None:
        """Gracefully cancel and await all running job tasks on app shutdown."""
        self._shutting_down = True
        active_tasks = [task for task in self._tasks.values() if not task.done()]
        if not active_tasks:
            return

        logger.info("Shutting down AsyncJobQueue (%d active job(s))", len(active_tasks))
        for task in active_tasks:
            task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*active_tasks, return_exceptions=True),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for %d job(s) to cancel", len(active_tasks))

    async def _transition_to_running(self, pid: str, phase: JobPhase) -> None:
        """Move project to PROCESSING or RENDERING when entering execution."""
        target_status = (
            ProjectStatus.PROCESSING if phase == "prepare" else ProjectStatus.RENDERING
        )
        try:
            project = await self.repository.get_project(pid)
        except Exception as exc:
            logger.debug("Skipping pre-run status fetch for project %s: %s", pid, exc)
            return

        if project.status == target_status:
            return
        if can_transition(project.status, target_status):
            await self.repository.update_project_status(
                pid,
                target_status,
                current_stage=f"{phase}_started",
                enforce_fsm=True,
            )

    async def _handle_job_failure(
        self,
        pid: str,
        *,
        code: str,
        message: str,
        refund_reason: str,
    ) -> None:
        """Mark project FAILED, refund charged credits if > 0, and emit SSE error."""
        refunded = False
        try:
            project = await self.repository.get_project(pid)
        except Exception as exc:
            logger.warning("Could not load project %s during failure cleanup: %s", pid, exc)
            project = None

        if project is not None:
            if project.status not in (ProjectStatus.FAILED, ProjectStatus.CANCELLED):
                try:
                    await self.repository.update_project_status(
                        pid,
                        ProjectStatus.FAILED,
                        current_stage="failed",
                        error_message=message,
                        enforce_fsm=False,
                    )
                except Exception as exc:
                    logger.error("Failed to mark project %s as FAILED: %s", pid, exc)

            if project.credits_charged > 0:
                try:
                    await self.repository.refund_user_credits(
                        project.user_id,
                        project.credits_charged,
                        project_id=pid,
                        description=refund_reason,
                        metadata={"error_code": code},
                    )
                    refunded = True
                except Exception as exc:
                    logger.error(
                        "Failed to refund %d credits for project %s: %s",
                        project.credits_charged,
                        pid,
                        exc,
                    )

        await self.event_bus.publish(
            pid,
            "error",
            {
                "project_id": pid,
                "code": code,
                "message": message,
                "refunded": refunded,
            },
        )
        await self.event_bus.close_project(pid)

    async def _execute_job(
        self,
        pid: str,
        mode: ProjectMode,
        phase: JobPhase,
        context: dict[str, Any],
    ) -> Any:
        token = set_job_id(pid)
        try:
            async with self._semaphore:
                handler = self._handlers.get((mode, phase))
                if handler is None:
                    raise _UnregisteredPipelineHandlerError(
                        f"Pipeline handler for mode='{mode.value}', phase='{phase}' is not registered"
                    )

                await self._transition_to_running(pid, phase)
                await self.event_bus.publish(
                    pid,
                    "stage_started",
                    {
                        "project_id": pid,
                        "stage": f"{phase}_started",
                        "label": f"Starting {mode.value} ({phase})...",
                        "progress_pct": 0,
                    },
                )

                return await asyncio.wait_for(
                    handler(pid, context),
                    timeout=self.settings.JOB_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            logger.info("Job cancelled for project %s (mode=%s, phase=%s)", pid, mode.value, phase)
            raise
        except asyncio.TimeoutError:
            timeout_s = self.settings.JOB_TIMEOUT_SECONDS
            logger.exception("Job timed out after %ss for project %s", timeout_s, pid)
            await self._handle_job_failure(
                pid,
                code="JOB_TIMEOUT",
                message=f"Job timed out after {timeout_s} seconds",
                refund_reason="Automatic refund after job timeout",
            )
        except StudioError as exc:
            logger.exception("StudioError in job %s (%s): %s", pid, exc.code, exc.message)
            await self._handle_job_failure(
                pid,
                code=exc.code,
                message=exc.message,
                refund_reason=f"Automatic refund after job failure ({exc.code})",
            )
        except _UnregisteredPipelineHandlerError as exc:
            logger.error("Unregistered pipeline handler for project %s: %s", pid, exc)
            await self._handle_job_failure(
                pid,
                code="PIPELINE_NOT_REGISTERED",
                message=str(exc),
                refund_reason="Automatic refund: pipeline handler not registered",
            )
        except Exception as exc:
            logger.exception("Unhandled exception in job %s: %s", pid, exc)
            await self._handle_job_failure(
                pid,
                code="INTERNAL_ERROR",
                message=str(exc) or exc.__class__.__name__,
                refund_reason="Automatic refund after unhandled job error",
            )
        finally:
            reset_job_id(token)
