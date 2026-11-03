import base64
import json
from typing import ClassVar

import pytest

from kasm_use import tools

SID = "1b1a2009c172439f9c0a13110a5d9098"
PNG = b"\x89PNG\r\n\x1a\nverify-after-test"


class FakeVNC:
    instances: ClassVar[list["FakeVNC"]] = []

    def __init__(self):
        self.width = 800
        self.height = 600
        self.actions = []
        self.screenshots = 0
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def click(self, *args):
        self.actions.append(("click", args))

    def drag(self, *args):
        self.actions.append(("drag", args))

    def type(self, *args):
        self.actions.append(("type", args))

    def combo(self, *args):
        self.actions.append(("combo", args))

    def scroll(self, *args):
        self.actions.append(("scroll", args))

    def screenshot(self):
        self.screenshots += 1
        return PNG


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    FakeVNC.instances.clear()
    tools._SESSIONS[SID] = {"started": 1, "workspace": "Chrome"}
    tools._LAST_SHOT[SID] = (800, 600)
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    yield
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (tools.kasm_click, {"x": 10, "y": 20}),
        (tools.kasm_drag, {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40}),
        (tools.kasm_type, {"text": "hello"}),
        (tools.kasm_key, {"keys": "Return"}),
        (tools.kasm_scroll, {"direction": "down", "amount": 2, "x": 10, "y": 20}),
    ],
)
def test_verify_after_reopens_same_session_and_returns_screenshot(monkeypatch, tool, args):
    opened = []

    def open_session(kid):
        opened.append(kid)
        return FakeVNC()

    monkeypatch.setattr(tools, "_session", open_session)
    result = tool({"session_id": SID, "verify_after": True, **args})

    assert opened == [SID, SID]
    assert len(FakeVNC.instances) == 2
    assert len(FakeVNC.instances[0].actions) == 1
    assert FakeVNC.instances[1].screenshots == 1
    assert result["_multimodal"] is True
    action = json.loads(result["content"][0]["text"])
    assert action["ok"] is True
    assert action["verify_after"] == {"status": "screenshot_returned", "input_sent": True}
    image = next(part for part in result["content"] if part["type"] == "image_url")
    encoded = image["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == PNG


def test_verify_after_failure_does_not_retry_input(monkeypatch):
    opened = []

    def open_session(kid):
        opened.append(kid)
        if len(opened) == 2:
            raise ConnectionError("screenshot connection unavailable")
        return FakeVNC()

    monkeypatch.setattr(tools, "_session", open_session)
    result = json.loads(tools.kasm_click({
        "session_id": SID, "x": 10, "y": 20, "verify_after": True,
    }))

    assert result["ok"] is True
    assert result["verify_after"] == {
        "status": "unavailable",
        "action_may_have_succeeded": True,
        "instruction": "Call kasm_look and inspect before retrying.",
    }
    assert opened == [SID, SID]
    assert FakeVNC.instances[0].actions == [("click", (10, 20, 1, 1))]


def test_verify_after_is_optional_on_each_input_tool(monkeypatch):
    monkeypatch.setattr(tools, "_session", lambda _kid: FakeVNC())
    inputs = {"kasm_click", "kasm_drag", "kasm_type", "kasm_key", "kasm_scroll"}
    schemas = {name: schema for name, _, _, schema in tools._TOOLS}

    for name in inputs:
        schema = schemas[name]
        assert schema["properties"]["verify_after"]["type"] == "boolean"
        assert "verify_after" not in schema.get("required", [])


def test_verify_after_false_keeps_existing_text_result(monkeypatch):
    opened = []
    monkeypatch.setattr(tools, "_session", lambda kid: opened.append(kid) or FakeVNC())

    result = json.loads(tools.kasm_click({"session_id": SID, "x": 10, "y": 20}))

    assert result == {"ok": True, "session_id": SID, "clicked": [10, 20]}
    assert opened == [SID]
