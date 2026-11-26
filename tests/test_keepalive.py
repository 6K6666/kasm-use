"""Kasm destroys a session nobody keeps alive (one hour by default). Every connection renews it."""
import json

import pytest

from kasm_use import tools

SID = "a" * 32


class FakeVNC:
    width, height, blank = 800, 600, False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def screenshot(self):
        return b"\x89PNG"

    def combo(self, syms):
        pass


@pytest.fixture
def kasm(monkeypatch):
    calls, clock = [], [1000.0]
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR", raising=False)
    monkeypatch.setenv("KASM_API_URL", "https://kasm.invalid")
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {SID: {
        "started": 1, "workspace": "Ubuntu", "username": "alice", "session_token": "login-token", "path": "desktop/x/vnc"}})
    monkeypatch.setattr(tools.vnc, "Session", lambda *a: FakeVNC())
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tools, "_web", lambda endpoint, **payload: calls.append((endpoint, payload)) or {})
    return calls, clock


def test_a_tool_call_keeps_its_session_alive_as_the_sessions_own_user(kasm):
    calls, _ = kasm
    assert json.loads(tools.kasm_key({"session_id": SID, "keys": "Return"}))["ok"] is True
    assert calls == [("keepalive", {"kasm_id": SID, "token": "login-token", "username": "alice"})]


def test_keepalive_is_sent_at_most_every_five_minutes_per_session(kasm):
    calls, clock = kasm
    for _ in range(3):
        tools.kasm_look({"session_id": SID, "wait_seconds": 0})
    assert len(calls) == 1
    clock[0] += tools.KEEPALIVE_SECONDS + 1
    tools.kasm_look({"session_id": SID, "wait_seconds": 0})
    assert len(calls) == 2


def test_a_failed_keepalive_does_not_fail_the_call_and_is_tried_again(kasm, monkeypatch):
    attempts = []

    def refuse(endpoint, **payload):
        attempts.append(endpoint)
        raise RuntimeError("Kasm keepalive HTTP 403")

    monkeypatch.setattr(tools, "_web", refuse)
    for _ in range(2):
        assert json.loads(tools.kasm_key({"session_id": SID, "keys": "Return"}))["ok"] is True
    assert attempts == ["keepalive", "keepalive"]


def test_the_login_token_never_reaches_a_tool_result(kasm):
    result = tools.kasm_key({"session_id": SID, "keys": "Return"}) + json.dumps(tools.kasm_look({"session_id": SID, "wait_seconds": 0}))
    assert "login-token" not in result
