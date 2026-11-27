"""kasm_attach: take over a session this instance's own Kasm user launched from the dashboard.

The owner launches a desktop, signs in to something there, and hands the desktop to the agent
without sharing a credential. Seen 2026-11-27: only Windows pilot sessions could be claimed, and
an Ubuntu session the owner had prepared was out of reach.
"""
import json

import pytest

from kasm_use import tools

SID = "a" * 32
OTHER = "d" * 32
OWNER = "c" * 32
IMAGE = "b" * 32


class FakeVNC:
    width, height = 800, 600

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nattach-test"

    def __getattr__(self, name):
        return lambda *a: None


def row(kid=SID, **changes):
    return {"kasm_id": kid, "user_id": OWNER, "image_id": IMAGE, "operational_status": "running",
            "username": "agent@example.test",
            "image": {"friendly_name": "AI Agent Development", "image_type": "Container"}, **changes}


def status(kid=SID, **changes):
    return {"kasm": {**row(kid, **changes), "port_map": {"vnc": {"path": f"desktop/{kid}/vnc"}}},
            "kasm_url": f"/#/connect/kasm/{kid}/{OWNER}/login-token"}


@pytest.fixture(autouse=True)
def kasm(monkeypatch):
    """A Kasm with two running sessions: the owner's, and another user's. Returns the calls made."""
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.delenv("KASM_WORKSPACE_ALLOW", raising=False)
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR", raising=False)
    monkeypatch.setenv("KASM_USER_ID", OWNER)
    monkeypatch.setenv("KASM_ATTACH", "true")
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setattr(tools, "_MINE", set())
    monkeypatch.setattr(tools, "_STOPPED", set())
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC())
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    calls = []
    answers = {
        "get_images": lambda **_: {"images": []},
        "get_kasms": lambda **_: {"kasms": [row(), row(OTHER, user_id="e" * 32)]},
        "get_kasm_status": lambda kasm_id, **_: status(kasm_id, **({"user_id": "e" * 32} if kasm_id == OTHER else {})),
        "destroy_kasm": lambda **_: {},
    }

    def call(endpoint, **kwargs):
        calls.append(endpoint)
        return answers[endpoint](**kwargs)

    monkeypatch.setattr(tools, "_call", call)
    yield calls, answers
    tools._LAST_SHOT.clear()


def attach(kid=SID):
    return json.loads(tools.kasm_attach({"session_id": kid}))


def test_attach_is_off_unless_the_instance_enables_it(monkeypatch, kasm):
    calls, _ = kasm
    monkeypatch.delenv("KASM_ATTACH")
    result = attach()
    assert (result["ok"], result["error"]) == (False, "Attaching to dashboard sessions is not enabled for this kasm-use instance.")
    assert calls == [] and not tools._SESSIONS


def test_attach_takes_over_a_running_session_of_the_instances_own_user():
    result = attach()
    assert result == {"ok": True, "session_id": SID, "workspace": "AI Agent Development", "state": "ready",
                      "note": "The owner launched this session and may be signed in to things in it: do only what "
                              "they asked. Call kasm_look next. kasm_stop releases it without ending it."}
    creds = tools._SESSIONS[SID]
    assert (creds["username"], creds["session_token"], creds["path"]) == ("agent@example.test", "login-token", f"desktop/{SID}/vnc")
    assert tools._resolve_session({}) == SID  # a call that names no session means this one
    assert isinstance(tools.kasm_look({"wait_seconds": 0}), dict)


def test_attach_refuses_a_session_of_another_kasm_user():
    result = attach(OTHER)
    assert (result["ok"], result["error"]) == (False, "No running session of this instance's Kasm user has that ID.")
    assert not tools._SESSIONS


@pytest.mark.parametrize("change", [{"operational_status": "starting"}, {"kasm_id": "f" * 32}, {"username": ""},
                                    {"image": {"friendly_name": "Server", "image_type": "Server"}}])
def test_attach_refuses_what_it_cannot_safely_control(kasm, change):
    _, answers = kasm
    answers["get_kasm_status"] = lambda kasm_id, **_: status(kasm_id, **change)
    assert attach()["ok"] is False
    assert not tools._SESSIONS


def test_attach_refuses_a_desktop_path_that_is_not_this_sessions(kasm):
    _, answers = kasm
    wrong = status()
    wrong["kasm"]["port_map"]["vnc"]["path"] = f"desktop/{OTHER}/vnc"
    answers["get_kasm_status"] = lambda **_: wrong
    result = attach()
    assert (result["ok"], result["error"]) == (False, "Kasm returned a connection link that did not match this session and owner.")
    assert not tools._SESSIONS


def test_attach_accepts_the_session_id_as_the_dashboard_shows_it():
    dashed = f"{SID[:8]}-{SID[8:12]}-{SID[12:16]}-{SID[16:20]}-{SID[20:]}".upper()
    assert attach(dashed)["session_id"] == SID


@pytest.mark.parametrize("bad", ["", "not-a-session", "A" * 31, "../" + "a" * 29])
def test_attach_needs_a_session_id(kasm, bad):
    calls, _ = kasm
    assert attach(bad) == {"ok": False, "error": "Give the session_id of an attachable session from kasm_list."}
    assert calls == []


def test_attach_again_is_harmless():
    attach()
    token = tools._SESSIONS[SID]["session_token"]
    assert attach()["ok"] is True
    assert tools._SESSIONS[SID]["session_token"] == token and len(tools._SESSIONS) == 1


def test_list_shows_the_owners_untracked_running_sessions_as_attachable(monkeypatch):
    listed = json.loads(tools.kasm_list({}))
    assert listed["attachable"] == [{"session_id": SID, "workspace": "AI Agent Development"}]  # never another user's
    assert "kasm_attach" in listed["note"]
    attach()
    listed = json.loads(tools.kasm_list({}))
    assert listed["attachable"] == []
    assert [s["session_id"] for s in listed["sessions"]] == [SID]
    monkeypatch.delenv("KASM_ATTACH")
    assert "attachable" not in json.loads(tools.kasm_list({}))


def test_stop_releases_an_attached_session_and_never_ends_it(kasm):
    """The owner launched it and may have work in it; ending it is theirs to do."""
    calls, _ = kasm
    attach()
    calls.clear()
    assert json.loads(tools.kasm_stop({})) == {
        "ok": True, "session_id": SID, "stopped": False, "released": True,
        "note": "The owner launched this session, so it was released, not ended. It is still running; "
                "the owner can end it from the Kasm dashboard."}
    assert "destroy_kasm" not in calls
    assert SID not in tools._SESSIONS
    assert attach()["ok"] is True  # and it can be taken over again


def test_an_attached_session_is_not_written_to_the_shared_state_file(monkeypatch, tmp_path):
    """Like a claimed pilot it is this process's own: another client process must attach for itself,
    and none can mistake it for a session kasm-use started and may end."""
    path = tmp_path / "state" / "sessions.json"
    monkeypatch.setenv("KASM_SESSION_STATE_FILE", str(path))
    monkeypatch.setattr(tools, "_STATE_PATH", None)
    attach()
    tools._save_sessions()
    assert json.loads(path.read_text())["sessions"] == []
    assert SID in tools._SESSIONS


def test_an_attached_session_of_a_prepared_workspace_waits_for_its_status_file(monkeypatch, tmp_path):
    monkeypatch.setenv("KASM_SESSION_STATUS_DIR", str(tmp_path))
    monkeypatch.setenv("KASM_STATUS_WORKSPACE_IDS", IMAGE)
    (tmp_path / f"{SID}.json").write_text(json.dumps(
        {"session_id": SID, "phase": "preparing", "detail": "macOS is starting", "typing": "ascii",
         "updated": tools.time.time()}))
    assert attach()["state"] == "preparing"
    assert "is still preparing" in json.loads(tools.kasm_look({"wait_seconds": 0}))["error"]


def test_the_tool_is_offered_with_its_one_required_argument():
    schema = next(params for name, _, _, params in tools._TOOLS if name == "kasm_attach")
    assert (schema["required"], set(schema["properties"])) == (["session_id"], {"session_id"})
