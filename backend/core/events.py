"""In-memory, thread-safe and async-safe SSE Event Bus with slow-consumer protection."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.core.logging import get_logger

logger = get_logger(__name__)


class SSEEvent(BaseModel):
    """Structured Server-Sent Event payload."""

    event: str
    data: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_sse_dict(self) -> dict[str, str]:
        """Convert to dict compatible with sse_starlette.ServerSentEvent / EventSourceResponse."""
        return {
            "event": self.event,
            "id": self.id,
            "data": json.dumps(self.data, default=str),
        }


@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[SSEEvent | None]
    loop: asyncio.AbstractEventLoop


class SSEEventBus:
    """Pub/sub event bus routing per-project_id events to bounded asyncio queues."""

    def __init__(self, max_queue_size: int = 256) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        self._max_queue_size = max_queue_size
        self._subscribers: dict[str, list[_Subscriber]] = defaultdict(list)
        self._lock = threading.Lock()

    @staticmethod
    def _enqueue_bounded(queue: asyncio.Queue[SSEEvent | None], item: SSEEvent | None) -> None:
        """Push an item to a bounded queue, evicting the oldest entry if full."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # Extremely unlikely race; drop gracefully rather than blocking publisher.
            logger.warning("SSE subscriber queue remained full after eviction; dropping event")

    def _dispatch_to_subscriber(self, sub: _Subscriber, item: SSEEvent | None) -> None:
        if sub.loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is sub.loop:
            self._enqueue_bounded(sub.queue, item)
        else:
            try:
                sub.loop.call_soon_threadsafe(self._enqueue_bounded, sub.queue, item)
            except RuntimeError:
                # Loop closed concurrently during shutdown.
                pass

    @asynccontextmanager
    async def subscribe(self, project_id: str) -> AsyncIterator[asyncio.Queue[SSEEvent | None]]:
        """Register a subscriber queue for ``project_id`` and clean up on exit."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=self._max_queue_size)
        sub = _Subscriber(queue=queue, loop=loop)
        key = str(project_id)

        with self._lock:
            self._subscribers[key].append(sub)

        try:
            yield queue
        finally:
            with self._lock:
                subs = self._subscribers.get(key)
                if subs:
                    self._subscribers[key] = [s for s in subs if s is not sub]
                    if not self._subscribers[key]:
                        self._subscribers.pop(key, None)

    def publish_nowait(
        self,
        project_id: str,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> SSEEvent:
        """Publish an event synchronously from any thread without blocking."""
        sse_event = SSEEvent(event=event, data=data or {})
        key = str(project_id)

        with self._lock:
            subs = list(self._subscribers.get(key, ()))

        for sub in subs:
            self._dispatch_to_subscriber(sub, sse_event)

        return sse_event

    async def publish(
        self,
        project_id: str,
        event: str,
        data: dict[str, Any] | None = None,
    ) -> SSEEvent:
        """Publish an event to all active subscribers of ``project_id``."""
        return self.publish_nowait(project_id=project_id, event=event, data=data)

    def close_project_nowait(self, project_id: str) -> None:
        """Signal all subscribers of ``project_id`` to close via None sentinel."""
        key = str(project_id)
        with self._lock:
            subs = self._subscribers.pop(key, [])

        for sub in subs:
            self._dispatch_to_subscriber(sub, None)

    async def close_project(self, project_id: str) -> None:
        """Close all SSE streams listening to ``project_id``."""
        self.close_project_nowait(project_id)

    def subscriber_count(self, project_id: str) -> int:
        """Return active subscriber count for a project."""
        with self._lock:
            return len(self._subscribers.get(str(project_id), ()))


sse_event_bus = SSEEventBus()


def get_event_bus() -> SSEEventBus:
    """Return the global SSEEventBus singleton."""
    return sse_event_bus
