import json

import pytest

from kasm_use import tools

SESSION = "a" * 32
IMAGE = "b" * 32
OWNER = "c" * 32


@pytest.mark.parametrize("wrong", [None, "user_id", "image_id"])
def test_claim_checks_owner_and_workspace_before_requesting_login(monkeypatch, wrong):
    monkeypatch.setenv("KASM_USER_ID", OWNER)
    monkeypatch.setenv("KASM_PILOT_WORKSPACE_ID", IMAGE)
    monkeypatch.setenv("KASM_PILOT_USERNAME", "tester")
    monkeypatch.setattr(tools, "_load_sessions", lambda: None)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    row = {"kasm_id": SESSION, "image_id": IMAGE, "user_id": OWNER, "operational_status": "running"}
    calls = []

    def call(method, **kwargs):
        calls.append(method)
        if method == "get_kasms":
            return {"kasms": [{**row, **({wrong: "d" * 32} if wrong else {})}]}
        return {
            "kasm": {**row, "port_map": {"vnc": {"path": f"desktop/{SESSION}/vnc"}}},
            "kasm_url": f"/#/connect/kasm/{SESSION}/{OWNER}/token",
        }

    monkeypatch.setattr(tools, "_call", call)
    result = json.loads(tools.kasm_claim_pilot({"session_id": SESSION}))
    if wrong:
        assert not result["ok"]
        assert calls == ["get_kasms"]
        assert not tools._SESSIONS
    else:
        assert result["ok"]
        assert calls == ["get_kasms", "get_kasm_status"]
        assert tools._SESSIONS[SESSION]["session_token"] == "token"
