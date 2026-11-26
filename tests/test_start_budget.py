"""kasm_start answers inside its time budget: a public proxy cuts a response at 100 s."""
import json

import pytest

from conftest import REAL_WAIT_VISIBLE
from kasm_use import tools

SID = "a" * 32
IMAGES = [{"image_id": "img", "name": "homelab/ubuntu:1", "friendly_name": "Ubuntu", "enabled": True}]
ATTEMPT_SECONDS = 20  # what one connection to an unreachable desktop costs (vnc's socket timeout)


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock that only sleeping and connecting advance."""
    now = [0.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.fixture
def kasm(monkeypatch, clock):
    status = {"value": "running"}

    def call(endpoint, **kwargs):
        if endpoint == "get_images":
            return {"images": IMAGES}
        if endpoint == "request_kasm":
            return {"kasm_id": SID, "session_token": "offline-token", "username": "test-only"}
        if endpoint == "get_kasm_status":
            return {"kasm": {"operational_status": status["value"]}}
        return {}

    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR", raising=False)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_call", call)
    monkeypatch.setattr(tools, "_user", lambda: "owner")
    monkeypatch.setattr(tools, "_SESSIONS", {})
    return status


def test_start_stops_waiting_for_kasm_at_the_budget_and_points_to_wait(kasm, clock):
    kasm["value"] = "starting"
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert (result["ok"], result["session_id"], result["state"], result["ready"]) == (True, SID, "preparing", False)
    assert "kasm_wait" in result["note"]
    assert clock[0] <= tools.START_BUDGET_SECONDS
    assert SID in tools._SESSIONS  # still tracked, so kasm_wait and kasm_stop can name it


def test_start_does_not_keep_probing_an_unreachable_desktop_past_the_budget(kasm, clock, monkeypatch):
    def unreachable(kid):
        clock[0] += ATTEMPT_SECONDS
        raise TimeoutError("timed out")

    monkeypatch.setenv("KASM_USE_SCREEN", "1280x800")
    monkeypatch.setattr(tools, "_session", unreachable)
    monkeypatch.setattr(tools, "_wait_visible", REAL_WAIT_VISIBLE)
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert (result["ok"], result["state"], result["ready"]) == (True, "preparing", False)
    assert "kasm_wait" in result["note"]
    # Only an attempt already under way may run past the budget.
    assert clock[0] <= tools.START_BUDGET_SECONDS + ATTEMPT_SECONDS


def test_a_desktop_that_draws_in_time_is_still_reported_ready(kasm, clock, monkeypatch):
    class Drawn:
        width, height, blank = 1280, 800, False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def screenshot(self):
            return b"\x89PNG\r\n\x1a\nbudget-test"

        def set_desktop_size(self, *_):
            pass

    monkeypatch.setenv("KASM_USE_SCREEN", "1280x800")
    monkeypatch.setattr(tools, "_session", lambda kid: Drawn())
    monkeypatch.setattr(tools, "_wait_visible", REAL_WAIT_VISIBLE)
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert (result["state"], result["ready"]) == ("ready", True)


def test_wait_says_preparing_while_kasm_itself_is_still_starting_the_session(kasm, monkeypatch):
    kasm["value"] = "starting"
    monkeypatch.setattr(tools, "_SESSIONS", {SID: {"started": 1, "workspace": "Ubuntu", "username": "u"}})

    def not_running(kid):
        raise RuntimeError("Kasm session is not running.")

    monkeypatch.setattr(tools, "_session", not_running)
    result = json.loads(tools.kasm_wait({"session_id": SID, "timeout_seconds": 5}))
    assert (result["state"], result["detail"]) == ("preparing", "Kasm is still starting the session.")
