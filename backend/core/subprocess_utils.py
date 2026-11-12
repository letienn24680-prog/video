"""Async subprocess helper with thread-pool fallback for Windows SelectorEventLoop."""

from __future__ import annotations

import asyncio
import subprocess
from contextlib import suppress


def _run_blocking_subprocess(
    cmd: list[str],
    timeout: float | None,
) -> tuple[int, bytes, bytes]:
    """Execute a subprocess synchronously in a worker thread with timeout conversion."""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise asyncio.TimeoutError from exc
    return (
        completed.returncode,
        completed.stdout or b"",
        completed.stderr or b"",
    )


async def run_subprocess_exec(
    *args: str,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    """Run an external binary without shell, falling back when SelectorEventLoop forbids pipes."""
    cmd = [str(a) for a in args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except NotImplementedError:
        return await asyncio.to_thread(_run_blocking_subprocess, cmd, timeout)

    try:
        if timeout is not None:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError, OSError):
            proc.kill()
        with suppress(Exception):
            await proc.communicate()
        raise

    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout or b"",
        stderr or b"",
    )
