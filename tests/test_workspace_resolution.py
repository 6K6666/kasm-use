"""Workspace resolution must never substitute a different named image."""
import json
import os

os.environ.setdefault("KASM_API_URL", "https://kasm.invalid")
os.environ.setdefault("KASM_API_KEY", "k")
os.environ.setdefault("KASM_API_KEY_SECRET", "s")
os.environ.setdefault("KASM_USER_ID", "u")

import pytest
from kasm_use import tools

IMAGES = [
    {"friendly_name": "Ubuntu Noble", "name": "homelab/base:1.0", "image_id": "base", "enabled": True},
    {"friendly_name": "Ubuntu Noble Worker v3 Test", "name": "homelab/worker:3.0",
     "image_id": "worker", "enabled": True},
    {"friendly_name": "Chrome", "name": "chrome:1.0", "image_id": "chrome1", "enabled": True},
    {"friendly_name": "Chrome", "name": "chrome:2.0", "image_id": "chrome2", "enabled": True},
    {"friendly_name": "Ubuntu Unsafe Disabled", "name": "unsafe:9.0",
     "image_id": "disabled", "enabled": False},
]


def test_exact_match_wins_over_substring_newer_image():
    assert tools._resolve_workspace(IMAGES, "Ubuntu Noble")["image_id"] == "base"


def test_exact_match_casefold_and_whitespace():
    assert tools._resolve_workspace(IMAGES, "  uBUNTU noble  ")["image_id"] == "base"


def test_exact_worker_still_accessible():
    assert tools._resolve_workspace(IMAGES, "Ubuntu Noble Worker v3 Test")["image_id"] == "worker"


def test_same_friendly_name_preserves_latest_image_preference():
    assert tools._resolve_workspace(IMAGES, "Chrome")["image_id"] == "chrome2"


def test_unique_partial_name_can_resolve():
    assert tools._resolve_workspace(IMAGES, "Worker v3")["image_id"] == "worker"


def test_ambiguous_partial_name_is_denied():
    with pytest.raises(ValueError, match="Ambiguous workspace name"):
        tools._resolve_workspace(IMAGES, "Ubuntu")


def test_nonexistent_or_disabled_image_denied():
    for name in ("Ubuntu Unsafe Disabled", "unknown"):
        with pytest.raises(ValueError, match="No matching workspace"):
            tools._resolve_workspace(IMAGES, name)


@pytest.mark.parametrize("bad", ["", "  ", None, 5, []])
def test_bad_workspace_name_fails_closed(bad):
    with pytest.raises(ValueError, match="nonempty string"):
        tools._resolve_workspace(IMAGES, bad)


def test_kasm_start_never_requests_wrong_image(monkeypatch):
    calls = []
    def fake_call(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "get_images":
            return {"images": IMAGES}
        if endpoint == "request_kasm":
            return {"kasm_id": "d" * 32, "session_token": "test-only",
                    "username": "test"}
        if endpoint == "get_kasm_status":
            return {"kasm": {"operational_status": "running"}}
        return {}
    monkeypatch.setattr(tools, "_call", fake_call)
    monkeypatch.setattr(tools, "_user", lambda: "test-user")
    tools._SESSIONS.clear()
    try:
        result = json.loads(tools.kasm_start({"workspace": "Ubuntu Noble"}))
        assert result["ok"] is True
        requested = [kwargs["image_id"] for endpoint, kwargs in calls
                     if endpoint == "request_kasm"]
        assert requested == ["base"]
    finally:
        tools._SESSIONS.clear()


def test_kasm_start_ambiguous_never_creates_session(monkeypatch):
    calls = []
    def fake_call(endpoint, **kwargs):
        calls.append(endpoint)
        return {"images": IMAGES}
    monkeypatch.setattr(tools, "_call", fake_call)
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert result["ok"] is False
    assert "Ambiguous workspace" in result["error"]
    assert "request_kasm" not in calls


# ------------------------------------------------------------------------ workspace allow-list
ESTATE = [
    {"friendly_name": "Ubuntu Noble", "name": "homelab/base:1.0", "image_id": "base", "enabled": True},
    {"friendly_name": "Windows 11 Disposable", "name": "win:1", "image_id": "win", "enabled": True},
    {"friendly_name": "Windows 11 POC", "name": None, "image_id": "poc", "enabled": True},
    {"friendly_name": "Ubuntu Noble Worker v3 Test", "name": "homelab/worker:3.0", "image_id": "worker", "enabled": True},
]


@pytest.fixture
def estate(monkeypatch):
    calls = []

    def fake_call(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {
            "get_images": {"images": ESTATE}, "get_kasms": {"kasms": []},
            "request_kasm": {"kasm_id": "d" * 32, "session_token": "test-only", "username": "test"},
            "get_kasm_status": {"kasm": {"operational_status": "running"}},
        }[endpoint]

    monkeypatch.setattr(tools, "_call", fake_call)
    monkeypatch.setattr(tools, "_user", lambda: "test-user")
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setenv("KASM_WORKSPACE_ALLOW", " Ubuntu Noble,windows 11 disposable ,")
    return calls


def requested(calls):
    return [kwargs["image_id"] for endpoint, kwargs in calls if endpoint == "request_kasm"]


def test_list_shows_only_allowed_workspaces(estate):
    listed = json.loads(tools.kasm_list({}))["workspaces"]
    assert [row["workspace"] for row in listed] == ["Ubuntu Noble", "Windows 11 Disposable"]


def test_list_shows_everything_without_an_allow_list(estate, monkeypatch):
    monkeypatch.delenv("KASM_WORKSPACE_ALLOW")
    assert len(json.loads(tools.kasm_list({}))["workspaces"]) == 4


@pytest.mark.parametrize("name", ["Windows 11 POC", "windows 11 poc", "Ubuntu Noble Worker v3 Test", "Worker", "POC"])
def test_start_refuses_a_workspace_that_is_not_allowed(estate, name):
    result = json.loads(tools.kasm_start({"workspace": name}))
    assert result == {"ok": False, "error": "That workspace is not enabled for this kasm-use instance."}
    assert requested(estate) == []


@pytest.mark.parametrize(("name", "image"), [("Ubuntu Noble", "base"), ("Windows 11 Disposable", "win"), ("ubuntu", "base")])
def test_start_resolves_names_among_allowed_workspaces_only(estate, name, image):
    assert json.loads(tools.kasm_start({"workspace": name}))["ok"] is True
    assert requested(estate) == [image]


def test_a_mistyped_name_lists_only_allowed_workspaces(estate):
    error = json.loads(tools.kasm_start({"workspace": "Fedora"}))["error"]
    assert error == "No matching workspace. Available: Ubuntu Noble, Windows 11 Disposable"


def test_start_allows_everything_without_an_allow_list(estate, monkeypatch):
    monkeypatch.delenv("KASM_WORKSPACE_ALLOW")
    assert json.loads(tools.kasm_start({"workspace": "Windows 11 POC"}))["ok"] is True
    assert requested(estate) == ["poc"]


@pytest.mark.parametrize("args", [{}, {"workspace": ""}, {"workspace": None}])
def test_start_needs_a_workspace_name(estate, args):
    result = json.loads(tools.kasm_start(args))
    assert result == {"ok": False, "error": "Give a workspace name from kasm_list."}
    assert estate == []


# ---------------------------------------------------------------------------- descriptions
def schema(tool):
    return next((description, params) for name, _, description, params in tools._TOOLS if name == tool)


def test_tool_descriptions_match_what_the_tools_accept():
    from kasm_use import vnc
    key_description, _ = schema("kasm_key")
    for name in ("plus", "minus", "capslock", "f1", "page_down", "super", "Return".lower()):
        assert name in key_description
    assert all(name in key_description for name in vnc._NAMED)
    start_description, start = schema("kasm_start")
    assert start["properties"]["workspace"]["description"] == "Workspace name exactly as kasm_list shows it."
    assert start["required"] == ["workspace"]
    assert "Chrome" not in start_description and "Terminal" not in start_description
    _, claim = schema("kasm_claim_pilot")
    assert claim["properties"]["session_id"]["description"] == (
        "The 32-character id of a Windows session the owner launched from the Kasm dashboard.")
