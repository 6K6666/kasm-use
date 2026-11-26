"""Kasm destroys a session nobody keeps alive (one hour by default). Every connection renews it."""
import io
import json
import socket
import urllib.error

import pytest

from conftest import REAL_WEB
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


def test_a_refused_or_unreachable_keepalive_never_fails_the_call_or_leaks_the_token(kasm, monkeypatch, capsys):
    url = "https://kasm.invalid/api/keepalive"
    failures = iter([
        urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"error_message": "bad token login-token"}')),
        urllib.error.URLError("unreachable while sending login-token"),
        socket.timeout("timed out"),
    ])
    asked = []

    def urlopen(request, **_kwargs):
        asked.append((request.full_url, json.loads(request.data)))
        raise next(failures)

    monkeypatch.setattr(tools, "_web", REAL_WEB)
    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    for _ in range(3):
        result = tools.kasm_key({"session_id": SID, "keys": "Return"})
        assert json.loads(result)["ok"] is True and "login-token" not in result
    assert asked == [(url, {"kasm_id": SID, "token": "login-token", "username": "alice"})] * 3
    warning = capsys.readouterr().err
    assert warning.count("\n") == 1 and "keepalive" in warning and SID[:8] in warning  # said once per session
    assert "HTTP 403" in warning and "login-token" not in warning


def test_a_reply_carrying_an_error_message_is_a_failed_keepalive(kasm, monkeypatch, capsys):
    asked = []

    class Reply:
        def read(self):
            return b'{"error_message": "Session token login-token is not valid"}'

    monkeypatch.setattr(tools, "_web", REAL_WEB)
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda request, **_kw: asked.append(request.full_url) or Reply())
    for _ in range(2):
        assert json.loads(tools.kasm_key({"session_id": SID, "keys": "Return"}))["ok"] is True
    assert len(asked) == 2  # not counted as renewed, so it is tried again
    warning = capsys.readouterr().err
    assert "keepalive" in warning and "login-token" not in warning
