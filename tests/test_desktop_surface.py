"""Regression checks for the public desktop MCP's tool boundary.

No Kasm API, Docker socket, VM or host commands are used by these tests.
"""
from __future__ import annotations

import pytest

from kasm_use import tools
from kasm_use.desktop_surface import (
    CORE_DESKTOP_TOOLS,
    OPTIONAL_DESKTOP_TOOLS,
    validated_desktop_handlers,
)


def registered(name: str, callback=lambda _: ""):
    return (name, callback, "test", {"type": "object", "properties": {}})


def baseline():
    return [registered(n) for n in sorted(CORE_DESKTOP_TOOLS)]


def test_upstream_registry_matches_reviewed_names():
    result = validated_desktop_handlers(tools._TOOLS)
    assert set(result) == CORE_DESKTOP_TOOLS
    assert not set(result) & OPTIONAL_DESKTOP_TOOLS


def test_server_uses_the_same_reviewed_registry():
    from kasm_use import server

    assert set(server.HANDLERS) == set(validated_desktop_handlers(tools._TOOLS))


@pytest.mark.parametrize("unexpected", [
    "kasm_exec_run", "kasm_exec_grant", "execute_kasm_command",
    "read_kasm_file", "write_kasm_file", "create_kasm_user",
    "shell_run", "host_exec", "kasm_archive_inspect", "kasm_profile_plan",
])
def test_privileged_or_wrong_layer_tools_are_rejected(unexpected):
    with pytest.raises(ValueError, match="unreviewed desktop tool"):
        validated_desktop_handlers([*baseline(), registered(unexpected)])


def test_missing_baseline_tool_fails_closed():
    with pytest.raises(ValueError, match="missing core desktop tools"):
        validated_desktop_handlers(baseline()[:-1])


def test_duplicate_registration_fails_closed():
    with pytest.raises(ValueError, match="duplicate desktop tool"):
        validated_desktop_handlers([*baseline(), registered("kasm_list")])


def test_known_read_only_extension_is_optional():
    for name in OPTIONAL_DESKTOP_TOOLS:
        assert name in validated_desktop_handlers([*baseline(), registered(name)])


def test_invalid_or_uncallable_registration_fails_closed():
    with pytest.raises(ValueError, match="not callable"):
        validated_desktop_handlers([*baseline(), registered("kasm_session_inspect", None)])
    with pytest.raises(ValueError, match="invalid desktop tool registration"):
        validated_desktop_handlers([*baseline(), ("bad",)])
