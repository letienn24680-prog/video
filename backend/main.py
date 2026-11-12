"""FastAPI application entrypoint, lifespan management, CORS, and exception handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.v1 import webhooks
from backend.api.v1.router import api_v1_router
from backend.core.config import Settings, get_settings
from backend.core.events import get_event_bus
from backend.core.exceptions import NotFoundError, StudioError
from backend.core.logging import get_logger, setup_logging
from backend.pipelines.faceless.orchestrator import register_faceless_handlers
from backend.pipelines.translator.orchestrator import register_translator_handlers
from backend.services.supabase.billing_repo import SupabaseBillingRepository
from backend.services.supabase.local_sandbox import (
    LocalSandboxBillingRepository,
    LocalSandboxRepository,
)
from backend.services.supabase.repository import SupabaseRepository
from backend.workers.cleanup_worker import AutoCleanupWorker
from backend.workers.task_queue import AsyncJobQueue

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_FRONTEND_DIR = _PROJECT_ROOT / "frontend"


def create_app(
    settings: Settings | None = None,
    *,
    frontend_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    cfg = settings or get_settings()
    resolved_frontend_dir = Path(frontend_dir) if frontend_dir is not None else _DEFAULT_FRONTEND_DIR

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        setup_logging(cfg.LOG_LEVEL)
        cfg.temp_path.mkdir(parents=True, exist_ok=True)

        event_bus = get_event_bus()
        repository = getattr(app_instance.state, "repository", None)
        if repository is None:
            repository = (
                LocalSandboxRepository(cfg)
                if cfg.LOCAL_SANDBOX_MODE
                else SupabaseRepository()
            )

        billing_repository = getattr(app_instance.state, "billing_repository", None)
        if billing_repository is None:
            if cfg.LOCAL_SANDBOX_MODE and isinstance(repository, LocalSandboxRepository):
                billing_repository = LocalSandboxBillingRepository(repository=repository)
            elif cfg.LOCAL_SANDBOX_MODE:
                billing_repository = LocalSandboxBillingRepository(settings=cfg)
            else:
                billing_repository = SupabaseBillingRepository(repository=repository)

        job_queue = getattr(app_instance.state, "job_queue", None) or AsyncJobQueue(
            settings=cfg,
            repository=repository,
            event_bus=event_bus,
        )
        cleanup_worker = getattr(app_instance.state, "cleanup_worker", None) or AutoCleanupWorker(
            settings=cfg,
            repository=repository,
        )

        register_translator_handlers(job_queue)
        register_faceless_handlers(job_queue)

        app_instance.state.settings = cfg
        app_instance.state.event_bus = event_bus
        app_instance.state.repository = repository
        app_instance.state.billing_repository = billing_repository
        app_instance.state.job_queue = job_queue
        app_instance.state.cleanup_worker = cleanup_worker

        cleanup_worker.start()
        logger.info("Started %s (%s)", cfg.APP_NAME, cfg.APP_ENV)
        try:
            yield
        finally:
            await cleanup_worker.stop()
            await job_queue.shutdown()
            logger.info("Shutdown complete for %s", cfg.APP_NAME)

    app_instance = FastAPI(
        title=cfg.APP_NAME,
        version="0.1.0",
        debug=cfg.APP_DEBUG,
        lifespan=lifespan,
    )

    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app_instance)
    app_instance.state.settings = cfg
    if settings is not None:
        app_instance.dependency_overrides[get_settings] = lambda: cfg
    app_instance.include_router(api_v1_router)
    app_instance.include_router(webhooks.router)
    _mount_local_media(app_instance, cfg)
    _mount_frontend(app_instance, resolved_frontend_dir)
    return app_instance


def _mount_local_media(app_instance: FastAPI, cfg: Settings) -> None:
    """Mount local storage and temporary media directories when sandbox or local storage is enabled."""
    if not (cfg.LOCAL_SANDBOX_MODE or cfg.STORAGE_PROVIDER == "local"):
        return

    cfg.temp_path.mkdir(parents=True, exist_ok=True)
    local_storage_dir = cfg.temp_path / "local_storage"
    local_storage_dir.mkdir(parents=True, exist_ok=True)

    app_instance.mount(
        "/local-media/storage",
        StaticFiles(directory=str(local_storage_dir)),
        name="local-media-storage",
    )
    app_instance.mount(
        "/local-media",
        StaticFiles(directory=str(cfg.temp_path)),
        name="local-media",
    )


def _mount_frontend(app_instance: FastAPI, frontend_dir: Path) -> None:
    """Mount static frontend assets and serve ``frontend/index.html`` at ``GET /``."""
    for folder in ("css", "js", "assets"):
        sub_dir = frontend_dir / folder
        if sub_dir.is_dir():
            app_instance.mount(
                f"/{folder}",
                StaticFiles(directory=str(sub_dir)),
                name=f"frontend-{folder}",
            )

    @app_instance.get("/", include_in_schema=False)
    async def serve_frontend_index() -> FileResponse:
        index_path = frontend_dir / "index.html"
        if not index_path.is_file():
            raise NotFoundError("Frontend index.html not found")
        return FileResponse(path=str(index_path), media_type="text/html")


def _register_exception_handlers(app_instance: FastAPI) -> None:
    @app_instance.exception_handler(StudioError)
    async def studio_error_handler(_request: Request, exc: StudioError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("StudioError (%s): %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app_instance.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": "Invalid request payload",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app_instance.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected internal error occurred",
                    "details": {},
                }
            },
        )


app = create_app()
