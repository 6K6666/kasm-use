import json

import pytest

from conftest import REAL_WAIT_VISIBLE
from kasm_use import tools, vnc

SID = "a" * 32
PNG = b"\x89PNG\r\n\x1a\nblank-frame-test"
BLANK_NOTE = "Screen is blank; the desktop is still starting. Wait and call kasm_look again."


def test_uniform_frame_is_blank():
    assert vnc.frame_is_blank(bytes(3 * 16))
    assert vnc.frame_is_blank(bytearray(b"\x10\x20\x30" * 16))


def test_one_differing_pixel_is_not_blank():
    frame = bytearray(3 * 16)
    frame[45] = 1
    assert not vnc.frame_is_blank(frame)


def test_screenshot_records_whether_the_frame_was_blank(monkeypatch):
    session = object.__new__(vnc.Session)
    session.width, session.height = 4, 2
    monkeypatch.setattr(session, "_update", lambda *a: [(0, 0, 4, 2, bytearray(24))])
    session.screenshot()
    assert session.blank is True
    lit = bytearray(24)
    lit[3] = 200
    monkeypatch.setattr(session, "_update", lambda *a: [(0, 0, 4, 2, lit)])
    session.screenshot()
    assert session.blank is False


class FakeVNC:
    width, height = 800, 600

    def __init__(self, blank):
        self.blank = blank

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def screenshot(self):
        return PNG


@pytest.fixture
def tracked(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {SID: {"started": 1, "workspace": "Ubuntu"}})
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)


def test_look_says_when_the_screen_is_blank_and_still_returns_the_image(monkeypatch, tracked):
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=True))
    result = tools.kasm_look({"session_id": SID})
    assert BLANK_NOTE in result["content"][0]["text"]
    assert result["content"][1]["type"] == "image_url"


def test_look_has_no_blank_note_for_a_drawn_screen(monkeypatch, tracked):
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=False))
    result = tools.kasm_look({"session_id": SID})
    assert "blank" not in result["content"][0]["text"]


@pytest.fixture
def launch(monkeypatch):
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Ubuntu", "name": "u:1", "image_id": "img"}]},
        "request_kasm": {"kasm_id": SID, "username": "alice", "session_token": "t"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setenv("KASM_USER_ID", "owner")
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: replies[endpoint])
    monkeypatch.setattr(tools, "_wait_visible", REAL_WAIT_VISIBLE)
    sleeps = []
    monkeypatch.setattr(tools.time, "sleep", sleeps.append)
    return sleeps


def test_start_is_ready_when_the_first_frame_is_drawn(monkeypatch, launch):
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=False))
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert result["ready"] is True
    assert result["note"] == "Desktop is visible. Call kasm_look next."
    assert launch == []


def test_start_is_not_ready_after_start_ready_seconds_of_blank_frames(monkeypatch, launch):
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=True))
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert result["ok"] is True and result["ready"] is False
    assert result["note"] == "Desktop is still starting. Call kasm_wait."
    assert launch == [1] * tools.START_READY_SECONDS == [1] * 20


def test_start_keeps_polling_while_the_desktop_refuses_connections(monkeypatch, launch):
    attempts = []

    def connect(kid):
        attempts.append(kid)
        if len(attempts) < 3:
            raise ConnectionError("not accepting connections yet")
        return FakeVNC(blank=False)

    monkeypatch.setattr(tools, "_session", connect)
    assert json.loads(tools.kasm_start({"workspace": "Ubuntu"}))["ready"] is True
    assert launch == [1, 1]
