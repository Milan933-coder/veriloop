"""Per-job in-process pub/sub for SSE progress events.

Every published event is kept in a bounded per-job history and stamped with a monotonically
increasing `seq`, so a subscriber that connects late (the browser opens the SSE stream after
POST /verify returns, or reconnects with Last-Event-ID) first receives the replay and then live
events, without gaps or duplicates. `close(job_id)` ends every stream for that job; subscribing to
a closed job yields its history and returns.

All methods must be called from the event loop thread (no locking needed: snapshot + register in
`subscribe` happen without an await in between).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

_CLOSE: dict[str, Any] = {"__close__": True}


@dataclass
class _Channel:
    history: deque[dict[str, Any]]
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    closed: bool = False
    seq: int = 0


class EventBus:
    def __init__(self, *, max_events_per_job: int = 1000, max_jobs: int = 500) -> None:
        self._channels: OrderedDict[str, _Channel] = OrderedDict()
        self._max_events = max_events_per_job
        self._max_jobs = max_jobs

    def _channel(self, job_id: str) -> _Channel:
        ch = self._channels.get(job_id)
        if ch is None:
            ch = _Channel(history=deque(maxlen=self._max_events))
            self._channels[job_id] = ch
            self._evict()
        else:
            self._channels.move_to_end(job_id)
        return ch

    def _evict(self) -> None:
        # Drop the least-recently-used finished channels without listeners; never an active one.
        if len(self._channels) <= self._max_jobs:
            return
        for job_id in list(self._channels):
            if len(self._channels) <= self._max_jobs:
                break
            ch = self._channels[job_id]
            if ch.closed and not ch.subscribers:
                del self._channels[job_id]

    def publish(self, job_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Record and fan out an event. Returns the stored copy (with `seq`). No-op after close()."""
        ch = self._channel(job_id)
        if ch.closed:
            return dict(event)
        ch.seq += 1
        stored = {**event, "seq": ch.seq}
        ch.history.append(stored)
        for queue in ch.subscribers:
            queue.put_nowait(stored)
        return stored

    def close(self, job_id: str) -> None:
        """Mark the job's stream finished: live subscribers drain and stop, later ones replay and stop."""
        ch = self._channel(job_id)
        if ch.closed:
            return
        ch.closed = True
        for queue in ch.subscribers:
            queue.put_nowait(_CLOSE)

    def is_closed(self, job_id: str) -> bool:
        ch = self._channels.get(job_id)
        return bool(ch and ch.closed)

    def history(self, job_id: str) -> list[dict[str, Any]]:
        ch = self._channels.get(job_id)
        return list(ch.history) if ch else []

    def subscriber_count(self, job_id: str) -> int:
        ch = self._channels.get(job_id)
        return len(ch.subscribers) if ch else 0

    async def subscribe(
        self, job_id: str, *, after_seq: int = 0, replay: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield past events with seq > after_seq (if replay), then live events until close()."""
        ch = self._channel(job_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        backlog = [e for e in ch.history if e["seq"] > after_seq] if replay else []
        closed = ch.closed
        if not closed:
            ch.subscribers.add(queue)
        try:
            for event in backlog:
                yield event
            if closed:
                return
            while True:
                event = await queue.get()
                if event is _CLOSE:
                    return
                yield event
        finally:
            ch.subscribers.discard(queue)

    def close_all(self) -> None:
        for job_id in list(self._channels):
            self.close(job_id)
