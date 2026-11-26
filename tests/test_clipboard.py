"""kasm_paste: the KasmVNC binary clipboard message, then the paste shortcut."""
import json
import struct

import pytest

from kasm_use import tools, vnc

SID = "a" * 32


class Wire:
    def __init__(self, incoming=b""):
        self.sent, self.incoming = [], bytearray(incoming)

    def send(self, data):
        self.sent.append(bytes(data))

    def read(self, n):
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        assert len(out) == n, "read past the end of the scripted server data"
        return out


def bare_session(incoming=b""):
    session = object.__new__(vnc.Session)
    session.ws, session.width, session.height = Wire(incoming), 4, 2
    return session


def test_set_clipboard_writes_one_utf8_text_plain_entry():
    session = bare_session()
    session.set_clipboard("é€日本")
    assert session.ws.sent == [
        bytes([180, 1, 10]) + b"text/plain" + b"\x00\x00\x00\x0b"
        + b"\xc3\xa9\xe2\x82\xac\xe6\x97\xa5\xe6\x9c\xac"]


def test_a_clipboard_message_from_the_server_is_skipped_while_waiting_for_a_frame():
    clip = (bytes([180, 2])
            + struct.pack(">IB", 7, 10) + b"text/plain" + struct.pack(">I", 5) + b"hello"
            + struct.pack(">IB", 8, 9) + b"text/html" + struct.pack(">I", 0))
    session = bare_session(clip + bytes([0, 0, 0, 0]))  # then a FramebufferUpdate with no rectangles
    assert session._update(0, 0, 1, 1) == []
    assert not session.ws.incoming


class FakeVNC:
    width, height = 800, 600

    def __init__(self):
        self.actions = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def set_clipboard(self, text):
        self.actions.append(("set_clipboard", text))

    def combo(self, syms):
        self.actions.append(("combo", syms))

    def screenshot(self):
        self.actions.append(("screenshot",))
        return b"\x89PNG\r\n\x1a\npaste-test"


@pytest.fixture
def desktop(monkeypatch):
    fake = FakeVNC()
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR", raising=False)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {SID: {"started": 1, "workspace": "Ubuntu"}})
    monkeypatch.setattr(tools, "_session", lambda kid: fake)
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    return fake


def test_paste_sets_the_clipboard_then_presses_ctrl_v(desktop):
    result = json.loads(tools.kasm_paste({"session_id": SID, "text": "é ñ 日本"}))
    note = result.pop("note")
    assert result == {"ok": True, "session_id": SID, "pasted_chars": 6, "shortcut": "ctrl+v"}
    assert "no confirmation" in note and "kasm_look" in note  # the desktop never acknowledges a clipboard
    assert desktop.actions == [("set_clipboard", "é ñ 日本"), ("combo", [0xFFE3, ord("v")])]


def test_paste_uses_the_requested_shortcut(desktop):
    tools.kasm_paste({"session_id": SID, "text": "x", "shortcut": "ctrl+shift+v"})
    assert desktop.actions[1] == ("combo", [0xFFE3, 0xFFE1, ord("v")])


@pytest.mark.parametrize(("args", "error"), [
    ({}, "text is required"),
    ({"text": ""}, "text is required"),
    ({"text": "x", "shortcut": "ctrl+nonsense"}, "unknown key name"),
    ({"text": "x", "shortcut": "ctrl+v; rm"}, "shortcut must be"),
    ({"text": "x" * 65537}, "too large"),
])
def test_paste_refuses_bad_input_before_touching_the_clipboard(desktop, args, error):
    result = json.loads(tools.kasm_paste({"session_id": SID, **args}))
    assert result["ok"] is False and error in result["error"]
    assert desktop.actions == []


def test_paste_can_return_a_screenshot(desktop):
    result = tools.kasm_paste({"session_id": SID, "text": "x", "verify_after": True})
    assert result["_multimodal"] and desktop.actions[-1] == ("screenshot",)


def test_paste_is_refused_on_a_transport_without_a_clipboard(desktop, monkeypatch):
    class NoClipboard:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr(tools, "_session", lambda kid: NoClipboard())
    result = json.loads(tools.kasm_paste({"session_id": SID, "text": "x"}))
    assert result["ok"] is False and "kasm_type" in result["error"]


def test_paste_is_refused_while_the_session_prepares(desktop, monkeypatch, tmp_path):
    monkeypatch.setenv("KASM_SESSION_STATUS_DIR", str(tmp_path))
    monkeypatch.setenv("KASM_PILOT_WORKSPACE_ID", "b" * 32)
    tools._SESSIONS[SID]["image_id"] = "b" * 32
    tools._SESSIONS[SID]["started"] = tools.time.time()
    result = json.loads(tools.kasm_paste({"session_id": SID, "text": "x"}))
    assert "is still preparing" in result["error"] and desktop.actions == []


def test_paste_description_warns_about_terminals_and_secrets():
    description = next(d for name, _, d, _ in tools._TOOLS if name == "kasm_paste")
    typed = next(d for name, _, d, _ in tools._TOOLS if name == "kasm_type")
    assert 'Terminals usually need shortcut "ctrl+shift+v".' in description
    warning = typed[typed.index("NEVER type"):]
    assert warning.replace("NEVER type", "NEVER paste") in description
    assert "clipboard" in description and "old clipboard" in description  # what happens when clipboard-in is off
