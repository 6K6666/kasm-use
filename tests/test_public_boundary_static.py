"""Public fork static safety regression checks, no Kasm API or credentials needed.

Defense in depth only. AST scans cannot prove absence of malicious behavior.
"""
from __future__ import annotations

import ast
import ipaddress
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "kasm_use"
FORBIDDEN_IMPORT_ROOTS = {
    "subprocess", "docker", "paramiko", "fabric", "pexpect", "ctypes",
    "kasm_exec_v2", "kasmctl", "importlib", "multiprocessing",
}
FORBIDDEN_OS_CALLS = {
    "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe", "execv", "execve",
    "execvp", "execvpe", "posix_spawn", "posix_spawnp",
}
FORBIDDEN_ASYNCIO_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}


def _assert_no_privileged_ast(tree: ast.AST, filename: str) -> None:
    """Tripwire for direct imports and shell/process call patterns, not a sandbox."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                root = imported.name.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, (filename, imported.name)
                aliases[imported.asname or root] = root
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, (filename, node.module)
            forbidden = (FORBIDDEN_OS_CALLS if root == "os"
                         else FORBIDDEN_ASYNCIO_CALLS if root == "asyncio"
                         else set())
            for imported in node.names:
                assert imported.name not in forbidden, (filename, imported.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            root = aliases.get(node.func.value.id, node.func.value.id)
            if root == "os":
                assert node.func.attr not in FORBIDDEN_OS_CALLS, (filename, node.func.attr)
            if root == "asyncio":
                assert node.func.attr not in FORBIDDEN_ASYNCIO_CALLS, (filename, node.func.attr)
        # getattr(os, "system") can otherwise evade direct attribute checks.
        if (isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            root = aliases.get(node.args[0].id, node.args[0].id)
            forbidden = (FORBIDDEN_OS_CALLS if root == "os"
                         else FORBIDDEN_ASYNCIO_CALLS if root == "asyncio"
                         else set())
            assert node.args[1].value not in forbidden, (filename, node.args[1].value)


def test_desktop_package_has_no_privileged_imports_or_os_shell_calls():
    # Recursive scan includes any future subpackages.
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _assert_no_privileged_ast(tree, path.as_posix())


@pytest.mark.parametrize("source", [
    "import os as o\no.system('echo hi')",
    "from os import system as run\nrun('echo hi')",
    "import asyncio as aio\naio.create_subprocess_shell('echo hi')",
    "from asyncio import create_subprocess_exec",
    "import importlib",
    "import multiprocessing",
    "import os as o\ngetattr(o, 'execve')('/bin/true', [], {})",
    "import os\nos.posix_spawn('/bin/true', [], {})",
])
def test_privileged_ast_tripwire_rejects_known_aliases(source: str):
    with pytest.raises(AssertionError):
        _assert_no_privileged_ast(ast.parse(source), "negative-fixture")


def test_server_uses_validated_desktop_registry():
    tree = ast.parse((PKG / "server.py").read_text(encoding="utf-8"))
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "HANDLERS" for t in node.targets)]
    assert len(assignments) == 1
    call = assignments[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "validated_desktop_handlers"
    assert len(call.args) == 1 and isinstance(call.args[0], ast.Attribute)
    assert call.args[0].attr == "_TOOLS"


def test_public_boundary_files_do_not_contain_private_topology():
    reviewed = [ROOT / "docs" / "MAINTAINER_ARCHITECTURE.md",
                PKG / "desktop_surface.py",
                ROOT / "tests" / "test_desktop_surface.py"]
    ipv4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
    for path in reviewed:
        text = path.read_text(encoding="utf-8")
        assert ".ts.net" not in text, path
        for address in ipv4.findall(text):
            ip = ipaddress.ip_address(address)
            assert not ip.is_private, (path.name, address)


def test_core_desktop_tools_exactly_nine():
    tree = ast.parse((PKG / "desktop_surface.py").read_text(encoding="utf-8"))
    names = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CORE_DESKTOP_TOOLS":
                    names[target.id] = node.value
    assert "CORE_DESKTOP_TOOLS" in names
    expression = names["CORE_DESKTOP_TOOLS"]
    assert isinstance(expression, ast.Call) and len(expression.args) == 1
    nameset = {element.value for element in expression.args[0].elts}
    assert nameset == {"kasm_list", "kasm_start", "kasm_look", "kasm_click",
                       "kasm_drag", "kasm_type", "kasm_key", "kasm_scroll", "kasm_stop"}
