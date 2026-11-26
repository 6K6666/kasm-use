"""KASM_USE_SCREEN: ask each new desktop for one size, once, when it starts."""
import json

import pytest

from kasm_use import server, tools

SID = "a" * 32


@pytest.mark.parametrize(("value", "size"), [
    (None, None), ("", None), ("1280x800", (1280, 800)), ("640x640", (640, 640)), ("2560X2560", (2560, 2560))])
def test_screen_setting_is_parsed(monkeypatch, value, size):
    monkeypatch.delenv("KASM_USE_SCREEN", raising=False)
    if value is not None:
        monkeypatch.setenv("KASM_USE_SCREEN", value)
    assert tools._screen() == size


@pytest.mark.parametrize("value", ["1280", "1280x", "x800", "639x800", "1280x2561", "1280x800x1", "wide", "1280 x 800", "-1280x800"])
def test_an_invalid_screen_setting_stops_the_server_and_names_the_variable(monkeypatch, value):
    for name in tools._REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("KASM_USE_SCREEN", value)
    with pytest.raises(SystemExit, match="KASM_USE_SCREEN"):
        server._check_env()


class FakeVNC:
    width, height, blank = 1024, 768, False

    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def set_desktop_size(self, width, height):
        self.log.append((width, height))

    def screenshot(self):
        return b"\x89PNG"


@pytest.fixture
def launch(monkeypatch):
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Ubuntu", "name": "u:1", "image_id": "img"}]},
        "request_kasm": {"kasm_id": SID, "username": "alice", "session_token": "t"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR", raising=False)
    monkeypatch.setenv("KASM_USER_ID", "owner")
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: replies[endpoint])
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    log = []
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(log))
    return log


def test_start_asks_the_new_desktop_for_the_configured_size_once(monkeypatch, launch):
    monkeypatch.setenv("KASM_USE_SCREEN", "1280x800")
    assert json.loads(tools.kasm_start({"workspace": "Ubuntu"}))["ok"] is True
    tools.kasm_look({"session_id": SID, "wait_seconds": 0})
    assert launch == [(1280, 800)]


def test_start_leaves_the_size_alone_when_none_is_configured(monkeypatch, launch):
    monkeypatch.delenv("KASM_USE_SCREEN", raising=False)
    assert json.loads(tools.kasm_start({"workspace": "Ubuntu"}))["ok"] is True
    assert launch == []


def test_start_still_succeeds_when_the_desktop_refuses_connections_for_the_resize(monkeypatch, launch):
    monkeypatch.setenv("KASM_USE_SCREEN", "1280x800")

    def refuse(kid):
        raise ConnectionError("not accepting connections yet")

    monkeypatch.setattr(tools, "_session", refuse)
    assert json.loads(tools.kasm_start({"workspace": "Ubuntu"}))["ok"] is True
