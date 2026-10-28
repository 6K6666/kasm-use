"""Public fork static safety regression checks, no Kasm API or credentials needed.

Defense in depth only. AST scans cannot prove absence of malicious behavior.
"""
from __future__ import annotations

import ast
import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "kasm_use"
FORBIDDEN_IMPORT_ROOTS = {
    "subprocess", "docker", "paramiko", "fabric", "pexpect", "ctypes",
    "kasm_exec_v2", "kasmctl",
}
FORBIDDEN_OS_CALLS = {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"}


def _check_desktop_ast(tree: ast.AST, source: str) -> None:
    """Fail on privileged imports and os shell calls, including aliased imports."""
    os_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, (source, alias.name)
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, (source, node.module)
            if node.module == "os":
                for alias in node.names:
                    assert alias.name not in FORBIDDEN_OS_CALLS, (source, alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id in os_aliases:
                assert node.func.attr not in FORBIDDEN_OS_CALLS, (source, receiver.id, node.func.attr)


def test_desktop_package_has_no_privileged_imports_or_os_shell_calls():
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _check_desktop_ast(tree, str(path.relative_to(ROOT)))


def test_static_guard_catches_aliases_and_direct_os_shell_imports():
    import pytest

    for source in (
        "import os as shell\nshell.system('test')",
        "from os import system as shell\nshell('test')",
        "from os import popen",
        "import subprocess as subprocess_alias",
    ):
        with pytest.raises(AssertionError):
            _check_desktop_ast(ast.parse(source), "synthetic")
    _check_desktop_ast(ast.parse("import os as safe\nsafe.path.exists('test')"), "synthetic")


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
