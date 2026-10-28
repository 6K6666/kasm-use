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
    """Reject direct and literal-dynamic privileged imports and os shell calls.

    Defense in depth only: nonliteral imports, reassignment, and arbitrary Python
    execution require runtime isolation, not an AST heuristic.
    """
    os_aliases = set()
    importlib_aliases = set()
    import_module_aliases = set()
    builtins_aliases = set()
    builtin_import_aliases = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, (source, alias.name)
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
                elif alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
                elif alias.name == "builtins":
                    builtins_aliases.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, (source, node.module)
            if node.module == "os":
                for alias in node.names:
                    assert alias.name not in FORBIDDEN_OS_CALLS, (source, alias.name)
            elif node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_aliases.add(alias.asname or alias.name)
            elif node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in os_aliases:
                assert func.attr not in FORBIDDEN_OS_CALLS, (source, func.value.id, func.attr)
        if (isinstance(func, ast.Name) and func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
                and node.args[0].id in os_aliases
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            assert node.args[1].value not in FORBIDDEN_OS_CALLS, (source, "getattr", node.args[1].value)
        dynamic_import = (
            isinstance(func, ast.Name) and func.id in (builtin_import_aliases | import_module_aliases)
            or isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and ((func.value.id in importlib_aliases and func.attr == "import_module")
                 or (func.value.id in builtins_aliases and func.attr == "__import__"))
        )
        if dynamic_import and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            target = node.args[0].value
            assert target.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, (source, "dynamic-import", target)


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



def test_static_guard_catches_literal_dynamic_imports_and_getattr_shell():
    import pytest

    blocked = (
        "__import__('subprocess')",
        "import importlib as loader\nloader.import_module('docker')",
        "from importlib import import_module as load\nload('paramiko.client')",
        "import builtins as builtin\nbuiltin.__import__('ctypes')",
        "from builtins import __import__ as load\nload('fabric')",
        "import os as shell\ngetattr(shell, 'system')('echo test')",
    )
    for source in blocked:
        with pytest.raises(AssertionError):
            _check_desktop_ast(ast.parse(source), "synthetic")
    for source in (
        "import importlib as loader\nloader.import_module('json')",
        "import os as safe\ngetattr(safe, 'getcwd')()",
    ):
        _check_desktop_ast(ast.parse(source), "synthetic")


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
