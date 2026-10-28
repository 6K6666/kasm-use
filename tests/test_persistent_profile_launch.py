"""Kasm 1.19 explicit persistent-profile launch request tests. No live API."""
import json
import os
import pytest

os.environ.setdefault("KASM_API_URL", "https://kasm.invalid")
os.environ.setdefault("KASM_API_KEY", "k")
os.environ.setdefault("KASM_API_KEY_SECRET", "s")
os.environ.setdefault("KASM_USER_ID", "u")
from kasm_use import tools


IMAGES = [{"image_id": "dev", "name": "homelab/ubuntu-noble-desktop:1.19.0-ca1",
           "friendly_name": "AI Agent Development", "enabled": True}]


@pytest.fixture
def launch(monkeypatch):
    calls = []
    def fake_call(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "get_images":
            return {"images": IMAGES}
        if endpoint == "request_kasm":
            return {"kasm_id": "a" * 32, "session_token": "offline-token",
                    "username": "test-only"}
        if endpoint == "get_kasm_status":
            return {"kasm": {"operational_status": "running"}}
        return {}
    monkeypatch.setattr(tools, "_call", fake_call)
    monkeypatch.setattr(tools, "_user", lambda: "test-user")
    tools._SESSIONS.clear()
    yield calls
    tools._SESSIONS.clear()


def _request(calls):
    rows = [kw for op, kw in calls if op == "request_kasm"]
    assert len(rows) == 1
    return rows[0]


def test_explicit_enabled_is_sent_to_kasm(launch):
    result = json.loads(tools.kasm_start({
        "workspace": "AI Agent Development", "persistent_profile_mode": "Enabled"}))
    assert result["ok"] is True
    req = _request(launch)
    assert req == {"image_id": "dev", "user_id": "test-user",
                   "enable_sharing": False, "persistent_profile_mode": "Enabled"}


def test_explicit_disabled_is_supported(launch):
    result = json.loads(tools.kasm_start({
        "workspace": "AI Agent Development", "persistent_profile_mode": "Disabled"}))
    assert result["ok"] is True
    assert _request(launch)["persistent_profile_mode"] == "Disabled"


def test_omitted_mode_preserves_legacy_call(launch):
    result = json.loads(tools.kasm_start({"workspace": "AI Agent Development"}))
    assert result["ok"] is True
    assert "persistent_profile_mode" not in _request(launch)


@pytest.mark.parametrize("mode", ["Reset", "enabled", "", True, 1, [], {}])
def test_reset_and_invalid_modes_never_reach_api(launch, mode):
    out = json.loads(tools.kasm_start({
        "workspace": "AI Agent Development", "persistent_profile_mode": mode}))
    assert out["ok"] is False
    assert "persistent_profile_mode" in out["error"]
    assert not any(op == "request_kasm" for op, _ in launch)


def test_schema_never_offers_reset():
    row = next(x for x in tools._TOOLS if x[0] == "kasm_start")
    schema = row[3]
    assert schema["properties"]["persistent_profile_mode"]["enum"] == [
        "Enabled", "Disabled"]
    assert "Reset" not in schema["properties"]["persistent_profile_mode"]["enum"]
