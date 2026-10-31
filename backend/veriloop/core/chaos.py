"""Per-tool fault injection (POST /admin/chaos) for live resilience demos.

ToolClient checks this before touching cache or network: `timeout` returns a ToolError with
error_type="timeout", `error` returns error_type="chaos"; both messages start with "chaos:" so the
UI/audit can tell an injected fault from a real one. Injection is immediate (no real waiting) so a
demo of "tool X is down" stays fast.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, get_args

ChaosMode = Literal["timeout", "error", "off"]
CHAOS_MODES: frozenset[str] = frozenset(get_args(ChaosMode))


class ChaosState:
    def __init__(self, known_tools: Iterable[str] | None = None) -> None:
        if known_tools is None:
            from veriloop.core.tools_registry import TOOL_NAMES

            known_tools = TOOL_NAMES
        self._known = frozenset(known_tools)
        self._modes: dict[str, ChaosMode] = {}

    def set(self, tool: str, mode: str) -> None:
        if tool not in self._known:
            raise ValueError(f"unknown tool {tool!r}; known: {sorted(self._known)}")
        if mode not in CHAOS_MODES:
            raise ValueError(f"unknown chaos mode {mode!r}; expected one of {sorted(CHAOS_MODES)}")
        if mode == "off":
            self._modes.pop(tool, None)
        else:
            self._modes[tool] = mode  # type: ignore[assignment]

    def get(self, tool: str) -> ChaosMode:
        return self._modes.get(tool, "off")

    def active(self) -> dict[str, ChaosMode]:
        return dict(self._modes)

    def clear(self) -> None:
        self._modes.clear()
