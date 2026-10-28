"""Contract checks: MCP discovery, dispatch, Hermes and privileged imports."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from kasm_use import server, tools

NAMES = ("kasm_list", "kasm_start", "kasm_look", "kasm_click", "kasm_drag",
         "kasm_type", "kasm_key", "kasm_scroll", "kasm_stop")


def test_advertised_tools_match_dispatch_snapshot():
    got = asyncio.run(server.list_tools())
    assert tuple(t.name for t in got) == NAMES
    assert tuple(server.HANDLERS) == NAMES
    assert all(t.inputSchema == item[3] for t, item in zip(got, tools._TOOLS))


def test_registry_mutation_cannot_change_running_server():
    original = tools._TOOLS
    try:
        tools._TOOLS = [*original, ("host_exec", lambda _: None, "forbidden",
                                  {"type": "object"})]
        assert tuple(t.name for t in asyncio.run(server.list_tools())) == NAMES
        with pytest.raises(ValueError, match="Unknown tool"):
            asyncio.run(server.call_tool("host_exec", {}))
    finally:
        tools._TOOLS = original


@pytest.mark.parametrize("name", [
    "kasm_exec_grant", "kasm_exec_run", "kasm_exec_write", "host_exec",
    "kasm_archive_inspect", "kasm_profile_plan",
])
def test_privileged_tools_denied_at_desktop_endpoint(name):
    with pytest.raises(ValueError, match="Unknown tool"):
        asyncio.run(server.call_tool(name, {}))


def test_hermes_rejects_unreviewed_tool_before_registration():
    original = tools._TOOLS
    class Stub:
        def register_tool(self, **kwargs):
            raise AssertionError("registration reached untrusted callbacks")
    try:
        tools._TOOLS = [*original, ("host_exec", lambda _: None, "forbidden",
                                   {"type": "object"})]
        with pytest.raises(ValueError, match="unreviewed desktop tool"):
            tools.register(Stub())
    finally:
        tools._TOOLS = original


def test_desktop_package_has_no_privileged_process_imports():
    root = Path(__file__).resolve().parents[1] / "src" / "kasm_use"
    forbidden = {"subprocess", "docker", "paramiko", "pexpect",
                 "fabric", "asyncssh", "pty"}
    forbidden_os = {"system", "popen", "execv", "execve", "spawnv",
                    "spawnve", "fork", "forkpty"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in forbidden for a in node.names), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"exec", "eval", "__import__"}, path
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                assert node.func.attr not in forbidden_os, path


def test_advertised_metadata_is_frozen_from_source_schema_mutation():
    schema = tools._TOOLS[0][3]
    assert "rogue" not in schema["properties"]
    try:
        schema["properties"]["rogue"] = {"type": "string"}
        advertised = asyncio.run(server.list_tools())
        assert "rogue" not in advertised[0].inputSchema["properties"]
    finally:
        schema["properties"].pop("rogue", None)

def test_returned_schema_mutation_does_not_affect_later_tools_list():
    first = asyncio.run(server.list_tools())
    assert 'injected_field' not in first[0].inputSchema['properties']
    first[0].inputSchema['properties']['injected_field'] = {'type': 'string'}
    # A caller modifying its own returned metadata must never mutate the
    # server's startup-reviewed schema snapshot or subsequent responses.
    second = asyncio.run(server.list_tools())
    assert 'injected_field' not in second[0].inputSchema['properties']
