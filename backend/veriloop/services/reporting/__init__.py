"""Reporting service (SPEC §7, BUILD_SPEC §5): evidence bundle, plans, report card. Never submits (D4)."""

from .actions import agent_command, attempt_package, confirm_report, prepare_report
from .bundle import build_bundle, build_bundle_zip
from .card import render_report_card
from .plans import build_plans, preview_text

__all__ = [
    "agent_command",
    "attempt_package",
    "build_bundle",
    "build_bundle_zip",
    "build_plans",
    "confirm_report",
    "prepare_report",
    "preview_text",
    "render_report_card",
]
