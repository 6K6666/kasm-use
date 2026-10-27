"""Explicit, fail-closed MCP tool surface for the desktop controller.

This guard prevents accidental registration of an execution/admin API on the
low-privilege desktop endpoint. It is not a replacement for caller
authentication, Kasm API permissions, or session ownership checks.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CORE_DESKTOP_TOOLS = frozenset({
    "kasm_list",
    "kasm_start",
    "kasm_look",
    "kasm_click",
    "kasm_drag",
    "kasm_type",
    "kasm_key",
    "kasm_scroll",
    "kasm_stop",
})

# Optional, read-only extensions may be introduced after their own reviews.
# Image-profile attestation and archive handling belong to the broker, not here.
OPTIONAL_DESKTOP_TOOLS = frozenset({
    "kasm_session_inspect",
    "kasm_agent_capabilities",
})


def validated_desktop_handlers(registry: Iterable[tuple[Any, ...]]) -> dict[str, Any]:
    """Build a registry only when its names match the reviewed desktop surface.

    The public endpoint must never accidentally publish an exec, file-write,
    authentication-grant, host-control or Kasm administrator tool.
    """
    allowed = CORE_DESKTOP_TOOLS | OPTIONAL_DESKTOP_TOOLS
    handlers: dict[str, Any] = {}
    for entry in registry:
        if not isinstance(entry, tuple) or len(entry) != 4:
            raise ValueError("invalid desktop tool registration")
        name, callback, _description, _schema = entry
        if not isinstance(name, str) or name not in allowed:
            raise ValueError(f"unreviewed desktop tool: {name!r}")
        if name in handlers:
            raise ValueError(f"duplicate desktop tool: {name!r}")
        if not callable(callback):
            raise ValueError(f"desktop tool callback is not callable: {name!r}")
        handlers[name] = callback
    missing = CORE_DESKTOP_TOOLS - handlers.keys()
    if missing:
        raise ValueError("missing core desktop tools: " + ", ".join(sorted(missing)))
    return handlers
