import json

import pytest

from kasm_use import tools


@pytest.fixture(autouse=True)
def clean():
    tools._LAST_SHOT.clear()
    yield
    tools._LAST_SHOT.clear()


def test_out_of_range_coordinate_is_refused():
    tools._LAST_SHOT["k"] = (1024, 768)
    with pytest.raises(RuntimeError, match=r"\(5000, 10\) is outside the 1024x768 screenshot"):
        tools._to_desktop("k", 5000, 10, 1024, 768)


def test_edges_are_accepted():
    tools._LAST_SHOT["k"] = (1024, 768)
    assert tools._to_desktop("k", 1023, 767, 2048, 1536) == (2046, 1534)
    with pytest.raises(RuntimeError):
        tools._to_desktop("k", -1, 0, 1024, 768)


@pytest.mark.parametrize(("tool", "args"), [
    (tools.kasm_click, {"x": 5000, "y": 10}),
    (tools.kasm_drag, {"from_x": 1, "from_y": 1, "to_x": 5000, "to_y": 10}),
    (tools.kasm_scroll, {"x": 5000, "y": 10}),
])
def test_pointer_tools_surface_the_refusal_and_send_nothing(monkeypatch, tool, args):
    sent = []

    class Fake:
        width, height = 1024, 768
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def click(self, *a): sent.append(a)
        def drag(self, *a): sent.append(a)
        def scroll(self, *a): sent.append(a)

    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {"k": {"started": 1, "workspace": "Ubuntu"}})
    monkeypatch.setattr(tools, "_session", lambda kid: Fake())
    tools._LAST_SHOT["k"] = (1024, 768)
    result = json.loads(tool({"session_id": "k", **args}))
    assert result["ok"] is False and "is outside the 1024x768 screenshot" in result["error"]
    assert sent == []
