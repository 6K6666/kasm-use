import base64
import json
from typing import ClassVar

import pytest

from kasm_use import tools

SID = "1b1a2009c172439f9c0a13110a5d9098"
PNG = b"\x89PNG\r\n\x1a\nverify-after-test"


class FakeVNC:
    instances: ClassVar[list["FakeVNC"]] = []
    frames: ClassVar[list[bytes]] = []  # what successive screenshots return; PNG once it is empty

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
        return self.frames.pop(0) if self.frames else PNG


class Clock:
    """Time that passes only when the code under test sleeps, or a fake screenshot says it took a while."""
    now = 0.0

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    FakeVNC.instances.clear()
    FakeVNC.frames.clear()
    tools._SESSIONS[SID] = {"started": 1, "workspace": "Chrome"}
    tools._LAST_SHOT[SID] = (800, 600)
    clock = Clock()
    monkeypatch.setattr(tools.time, "sleep", clock.sleep)
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock.now)
    yield clock
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
    assert FakeVNC.instances[1].screenshots == 2  # a second look that matches the first: nothing is moving
    assert result["_multimodal"] is True
    action = json.loads(result["content"][0]["text"])
    assert action["ok"] is True
    assert action["verify_after"] == {"status": "screenshot_returned", "input_sent": True,
                                      "settled": True, "waited_seconds": 1.0}
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


def verified_click(monkeypatch, frames):
    FakeVNC.frames[:] = frames
    monkeypatch.setattr(tools, "_session", lambda _kid: FakeVNC())
    result = tools.kasm_click({"session_id": SID, "x": 10, "y": 20, "verify_after": True})
    image = next(part for part in result["content"] if part["type"] == "image_url")
    shown = base64.b64decode(image["image_url"]["url"].split(",", 1)[1])
    return json.loads(result["content"][0]["text"])["verify_after"], shown, FakeVNC.instances[1].screenshots


@pytest.mark.parametrize(("frames", "samples", "shown"), [
    ([b"A", b"A"], 2, b"A"),                    # nothing moved
    ([b"A", b"B", b"C", b"C"], 4, b"C"),        # an animation that ends
    ([b"A", b"B", b"A"], 3, b"A"),              # a blinking cursor
])
def test_verify_after_returns_the_screen_once_it_stops_changing(monkeypatch, frames, samples, shown):
    """Seen 2026-11-27: the screenshot was taken the instant the input was sent, and showed Windows
    in the middle of maximising a window."""
    verify, image, taken = verified_click(monkeypatch, frames)
    assert (taken, image) == (samples, shown)
    assert verify == {"status": "screenshot_returned", "input_sent": True,
                      "settled": True, "waited_seconds": samples * 0.5}


def test_verify_after_gives_up_on_a_screen_that_never_settles(monkeypatch):
    verify, image, taken = verified_click(monkeypatch, [bytes([n]) for n in range(20)])
    assert (taken, image) == (8, bytes([7]))
    assert (verify["settled"], verify["waited_seconds"]) == (False, 4.0)
    assert verify["instruction"] == "The screen was still changing. Call kasm_look to see the final state."


def test_verify_after_records_the_screenshot_size_for_the_next_click(monkeypatch):
    tools._LAST_SHOT.clear()
    tools._LAST_SHOT[SID] = (1024, 768)  # the desktop was resized since the last look
    verified_click(monkeypatch, [])
    assert tools._LAST_SHOT[SID] == (800, 600)


@pytest.mark.parametrize(("frames", "ends_at"), [
    ([b"A", b"A"], 1.5),                           # looks at 0.5 and 1.0, the second done at 1.5
    ([bytes([n]) for n in range(20)], 4.0),         # never still: the last look is the one done at 4.0
])
def test_verify_after_counts_the_time_each_look_takes(monkeypatch, clean_session, frames, ends_at):
    """Measured 2026-11-27: one look at a 1280x800 desktop takes half a second. Sleeping half a second
    between looks on top of that doubled every wait, to 8 s for a screen that never settles."""
    look = FakeVNC.screenshot
    monkeypatch.setattr(FakeVNC, "screenshot", lambda self: clean_session.sleep(0.5) or look(self))
    verify, _, _ = verified_click(monkeypatch, frames)
    assert (clean_session.now, verify["waited_seconds"]) == (ends_at, ends_at)
