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
        (tools.kasm_key, {"keys": "Tab"}),  # an Enter waits longer, below
        (tools.kasm_scroll, {"direction": "down", "amount": 2, "x": 10, "y": 20}),
    ],
)
def test_verify_after_reopens_same_session_and_returns_screenshot(monkeypatch, tool, args):
    opened = []

    def open_session(kid):
        opened.append(kid)
        return FakeVNC()

    monkeypatch.setattr(tools, "_session", open_session)
    FakeVNC.frames[:] = [b"before the input"]
    result = tool({"session_id": SID, "verify_after": True, **args})

    assert opened == [SID, SID]
    assert len(FakeVNC.instances) == 2
    assert len(FakeVNC.instances[0].actions) == 1
    assert FakeVNC.instances[0].screenshots == 1  # the screen as it was, on the input's own connection
    assert FakeVNC.instances[1].screenshots == 3  # changed, then two looks that show nothing new
    assert result["_multimodal"] is True
    action = json.loads(result["content"][0]["text"])
    assert action["ok"] is True
    assert action["verify_after"] == {"status": "screenshot_returned", "input_sent": True, "changed": True,
                                      "settled": True, "waited_seconds": 1.5, "screen": [800, 600]}
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


# The first frame is the screen just before the input; the rest are the looks after it.
@pytest.mark.parametrize(("frames", "samples", "shown"), [
    ([b"Z", b"A", b"A", b"A"], 3, b"A"),                    # changed once, then still
    ([b"Z", b"A", b"B", b"C", b"C", b"C"], 5, b"C"),        # an animation that ends
    ([b"Z", b"A", b"B", b"A", b"B"], 4, b"B"),              # a blinking cursor
    ([b"Z", b"A", b"A", b"B", b"B", b"B"], 5, b"B"),        # a program that pauses while it opens
])
def test_verify_after_returns_the_screen_once_it_stops_changing(monkeypatch, frames, samples, shown):
    """Seen 2026-11-27: the screenshot was taken the instant the input was sent, and showed Windows
    in the middle of maximising a window. Seen 2026-11-28: macOS paused half a second while opening
    TextEdit and one still look was taken for the end."""
    verify, image, taken = verified_click(monkeypatch, frames)
    assert (taken, image) == (samples, shown)
    assert verify == {"status": "screenshot_returned", "input_sent": True, "changed": True,
                      "settled": True, "waited_seconds": samples * 0.5, "screen": [800, 600]}


def test_verify_after_gives_up_on_a_screen_that_never_settles(monkeypatch):
    verify, image, taken = verified_click(monkeypatch, [bytes([n]) for n in range(20)])
    assert (taken, image) == (8, bytes([8]))
    assert (verify["changed"], verify["settled"], verify["waited_seconds"]) == (True, False, 4.0)
    assert verify["instruction"] == "The screen was still changing. Call kasm_look to see the final state."


def test_verify_after_does_not_call_an_unchanged_screen_settled(monkeypatch):
    """An input the desktop has not reacted to yet is not done: an agent told it was settled would
    send it again and open the program twice."""
    verify, image, taken = verified_click(monkeypatch, [])  # every look shows the screen as it was
    assert (taken, image) == (8, PNG)
    assert (verify["changed"], verify["settled"], verify["waited_seconds"]) == (False, False, 4.0)
    assert "call kasm_look before sending it again" in verify["instruction"]


@pytest.mark.parametrize(("tool", "args"), [
    (tools.kasm_key, {"keys": "Return"}),
    (tools.kasm_key, {"keys": "ctrl+l enter"}),
    (tools.kasm_type, {"text": "Calculator", "press_enter": True}),
    (tools.kasm_type, {"text": "ls\n"}),
    (tools.kasm_click, {"x": 10, "y": 20, "double": True}),
])
def test_an_input_that_may_open_a_program_waits_for_its_window(monkeypatch, tool, args):
    """Seen 2026-11-28 on macOS: Spotlight closed at once after the Enter, the screen then showed
    nothing new for over a second, and verify_after called that settled before Calculator appeared."""
    FakeVNC.frames[:] = [b"spotlight", b"desktop", b"desktop", b"desktop", b"desktop",
                         b"calculator", b"calculator", b"calculator"]
    monkeypatch.setattr(tools, "_session", lambda _kid: FakeVNC())
    result = tool({"session_id": SID, "verify_after": True, **args})
    verify = json.loads(result["content"][0]["text"])["verify_after"]
    shown = base64.b64decode(next(p for p in result["content"] if p["type"] == "image_url")
                             ["image_url"]["url"].split(",", 1)[1])
    assert (shown, verify["settled"], verify["waited_seconds"]) == (b"calculator", True, 3.5)
    assert "may still be starting" in verify["instruction"]  # a big program can take longer still


def test_an_enter_on_a_screen_that_keeps_still_returns_at_three_seconds(monkeypatch):
    FakeVNC.frames[:] = [b"before"]
    monkeypatch.setattr(tools, "_session", lambda _kid: FakeVNC())
    result = tools.kasm_key({"session_id": SID, "keys": "Return", "verify_after": True})
    verify = json.loads(result["content"][0]["text"])["verify_after"]
    assert (verify["settled"], verify["waited_seconds"]) == (True, 3.0)


def test_an_enter_that_changes_nothing_gives_up_after_five_seconds(monkeypatch):
    monkeypatch.setattr(tools, "_session", lambda _kid: FakeVNC())
    result = tools.kasm_key({"session_id": SID, "keys": "Return", "verify_after": True})
    verify = json.loads(result["content"][0]["text"])["verify_after"]
    assert (verify["changed"], verify["settled"], verify["waited_seconds"]) == (False, False, 5.0)


def test_verify_after_paste_drops_the_look_yourself_note(monkeypatch):
    class Clipboard(FakeVNC):
        def set_clipboard(self, text):
            self.actions.append(("clipboard", text))

    FakeVNC.frames[:] = [b"Z"]
    monkeypatch.setattr(tools, "_session", lambda _kid: Clipboard())
    shown = tools.kasm_paste({"session_id": SID, "text": "é", "verify_after": True})
    assert "note" not in json.loads(shown["content"][0]["text"])
    unseen = json.loads(tools.kasm_paste({"session_id": SID, "text": "é"}))
    assert "check with kasm_look" in unseen["note"]


def test_verify_after_records_the_screenshot_size_for_the_next_click(monkeypatch):
    tools._LAST_SHOT.clear()
    tools._LAST_SHOT[SID] = (1024, 768)  # the desktop was resized since the last look
    verified_click(monkeypatch, [])
    assert tools._LAST_SHOT[SID] == (800, 600)


@pytest.mark.parametrize(("frames", "ends_at"), [
    ([b"Z", b"A", b"A", b"A"], 2.0),               # looks at 0.5, 1.0 and 1.5, the third done at 2.0
    ([bytes([n]) for n in range(20)], 4.0),         # never still: the last look is the one done at 4.0
])
def test_verify_after_counts_the_time_each_look_takes(monkeypatch, clean_session, frames, ends_at):
    """Measured 2026-11-27: one look at a 1280x800 desktop takes half a second. Sleeping half a second
    between looks on top of that doubled every wait, to 8 s for a screen that never settles."""
    look = FakeVNC.screenshot
    monkeypatch.setattr(FakeVNC, "screenshot", lambda self: clean_session.sleep(0.5) or look(self))
    verify, _, _ = verified_click(monkeypatch, frames)
    # the look before the input took its own half second, before the wait began
    assert (clean_session.now, verify["waited_seconds"]) == (ends_at + 0.5, ends_at)


def test_verify_after_says_how_big_the_screen_is_now(monkeypatch):
    """Seen 2026-11-27: the owner opened a session in a browser, Kasm resized the desktop to that
    window, and an agent working with verify_after had no way to notice without a separate look."""
    verify, _, _ = verified_click(monkeypatch, [])
    assert verify["screen"] == [800, 600]
