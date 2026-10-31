"""Two-level tool response cache: in-memory LRU with TTL in front of the Postgres `tool_cache` table.

Values are stored as JSON text so every hit returns a fresh copy (a service mutating a cached dict
cannot corrupt the cache) and memory/DB/replay paths all behave identically. The DB level is
best-effort: any DB error is logged and treated as a miss.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

from veriloop.core.resilience import Clock

if TYPE_CHECKING:
    from veriloop.db.engine import Database

log = structlog.get_logger(__name__)


class MemoryTTLCache:
    def __init__(self, maxsize: int = 4096, *, clock: Clock = time.monotonic) -> None:
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._maxsize = maxsize
        self._clock = clock

    def get(self, key: str) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires_at, payload = entry
        if self._clock() >= expires_at:
            del self._data[key]
            return False, None
        self._data.move_to_end(key)
        return True, json.loads(payload)

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self.set_json(key, json.dumps(value, ensure_ascii=False), ttl_s)

    def set_json(self, key: str, payload: str, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        self._data[key] = (self._clock() + ttl_s, payload)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class ToolCache:
    def __init__(
        self, db: Database | None = None, *, maxsize: int = 4096, clock: Clock = time.monotonic
    ) -> None:
        self.db = db
        self.memory = MemoryTTLCache(maxsize, clock=clock)

    @staticmethod
    def _mem_key(tool: str, key_hash: str) -> str:
        return f"{tool}:{key_hash}"

    async def get(self, tool: str, key_hash: str) -> tuple[bool, Any]:
        hit, value = self.memory.get(self._mem_key(tool, key_hash))
        if hit or self.db is None:
            return hit, value
        from veriloop.db import repo

        try:
            found = await repo.cache_get(self.db, tool, key_hash)
        except Exception as exc:
            log.warning("tool_cache_db_error", tool=tool, op="get", error=str(exc))
            return False, None
        if found is None:
            return False, None
        value, remaining_s = found
        self.memory.set(self._mem_key(tool, key_hash), value, remaining_s)
        return True, value

    async def set(self, tool: str, key_hash: str, value: Any, ttl_s: float, *, op: str = "") -> None:
        if ttl_s <= 0:
            return
        self.memory.set(self._mem_key(tool, key_hash), value, ttl_s)
        if self.db is None:
            return
        from veriloop.db import repo

        try:
            await repo.cache_put(self.db, tool, key_hash, value=value, ttl_s=ttl_s, op=op)
        except Exception as exc:
            log.warning("tool_cache_db_error", tool=tool, op="put", error=str(exc))
