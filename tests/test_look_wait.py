"""Bounded screenshot waits should not stall an MCP worker."""
import json

from kasm_use import tools


def test_look_wait_is_bounded_and_rejects_nonfinite(monkeypatch):
    tools._SESSIONS.clear()
    tools._SESSIONS["owned"] = {"started": 1, "workspace": "Ubuntu"}
    sleeps = []
    monkeypatch.setattr(tools.time, "sleep", sleeps.append)

    def stop_before_vnc(_kid):
        raise RuntimeError("expected test stop")

    monkeypatch.setattr(tools, "_session", stop_before_vnc)
    try:
        for value in (1e30, -3, 2):
            result = json.loads(tools.kasm_look({"session_id": "owned", "wait_seconds": value}))
            assert result == {"ok": False, "error": "expected test stop"}
        assert sleeps == [30.0, 0.0, 2.0]

        for value in (float("nan"), float("inf"), float("-inf")):
            result = json.loads(tools.kasm_look({"session_id": "owned", "wait_seconds": value}))
            assert result == {"ok": False, "error": "wait_seconds must be finite"}
        assert sleeps == [30.0, 0.0, 2.0]
    finally:
        tools._SESSIONS.clear()
