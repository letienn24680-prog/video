"""Structured logging with asyncio contextvars job_id propagation."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar, Token

job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | [job=%(job_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class JobContextFilter(logging.Filter):
    """Injects the current async task's job_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = job_id_var.get() or "-"
        return True


def set_job_id(job_id: str | None) -> Token[str | None]:
    """Bind a job_id to the current async context and return the reset token."""
    return job_id_var.set(job_id)


def reset_job_id(token: Token[str | None]) -> None:
    """Restore the previous job_id context using its reset token."""
    job_id_var.reset(token)


def get_job_id() -> str | None:
    """Return the active job_id in the current async context, if any."""
    return job_id_var.get()


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = str(level).strip().upper()
    resolved = logging.getLevelName(normalized)
    return resolved if isinstance(resolved, int) else logging.INFO


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    """Configure root structured logging with timestamp and [job=<id>] context."""
    numeric_level = _resolve_level(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(JobContextFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Keep verbose third-party transport loggers quiet unless DEBUG is explicitly requested.
    noisy_level = logging.INFO if numeric_level <= logging.DEBUG else logging.WARNING
    for noisy_name in ("httpx", "httpcore", "hpack", "urllib3", "asyncio"):
        logging.getLogger(noisy_name).setLevel(noisy_level)

    return logging.getLogger("ai_video_studio")


def get_logger(name: str) -> logging.Logger:
    """Return a module logger that inherits the [job=<id>] filter."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, JobContextFilter) for f in logger.filters):
        logger.addFilter(JobContextFilter())
    return logger
