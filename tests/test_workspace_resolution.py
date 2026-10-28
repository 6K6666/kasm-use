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
