import pytest

from kasm_use import tools

SID = "1b1a2009c172439f9c0a13110a5d9098"
OWNER = "owner-1"
VNC_PATH = "desktop/1b1a2009-c172-439f-9c0a-13110a5d9098/vnc"


def status_response(token="fresh-token", user_id=OWNER, kasm_id=SID):
    return {
        "kasm_url": f"/#/connect/kasm/{kasm_id}/{user_id}/{token}",
        "kasm": {
            "operational_status": "running",
            "port_map": {"vnc": {"path": VNC_PATH}},
        },
    }


@pytest.fixture(autouse=True)
def own_session(monkeypatch):
    monkeypatch.setenv("KASM_API_URL", "https://kasm.invalid")
    monkeypatch.setenv("KASM_USER_ID", OWNER)
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    yield
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None


def test_session_uses_fresh_login_when_path_or_token_is_missing(monkeypatch):
    tools._SESSIONS[SID] = {"username": "alice", "workspace": "Chrome", "started": 1}
    calls = []
    opened = []
    monkeypatch.setattr(tools, "_call", lambda endpoint, **args: calls.append((endpoint, args)) or status_response())
    monkeypatch.setattr(tools.vnc, "Session", lambda *args: opened.append(args) or object())

    session = tools._session(SID)

    assert session is not None
    assert calls == [("get_kasm_status", {"kasm_id": SID, "user_id": OWNER})]
    assert tools._SESSIONS[SID]["session_token"] == "fresh-token"
    assert tools._SESSIONS[SID]["path"] == VNC_PATH
    assert opened[0][2:4] == ("alice", "fresh-token")


def test_session_refreshes_once_after_vnc_authentication_rejection(monkeypatch):
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "stale-token", "path": VNC_PATH,
        "workspace": "Chrome", "started": 1,
    }
    opened = []
    refreshes = []

    def open_vnc(*args):
        opened.append(args)
        if len(opened) == 1:
            raise tools.vnc.AuthenticationError("VNC authentication rejected")
        return object()

    monkeypatch.setattr(tools.vnc, "Session", open_vnc)
    monkeypatch.setattr(
        tools, "_call", lambda endpoint, **args: refreshes.append((endpoint, args)) or status_response()
    )

    assert tools._session(SID) is not None
    assert len(opened) == 2
    assert opened[0][3] == "stale-token"
    assert opened[1][3] == "fresh-token"
    assert refreshes == [("get_kasm_status", {"kasm_id": SID, "user_id": OWNER})]


def test_reconnect_does_not_repeat_the_input_action(monkeypatch):
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "stale-token", "path": VNC_PATH,
        "workspace": "Chrome", "started": 1,
    }
    tools._LAST_SHOT[SID] = (800, 600)
    opened = []
    clicks = []

    class FakeVNC:
        width = 800
        height = 600

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def click(self, *args):
            clicks.append(args)

    def open_vnc(*args):
        opened.append(args)
        if len(opened) == 1:
            raise tools.vnc.AuthenticationError("VNC authentication rejected")
        return FakeVNC()

    monkeypatch.setattr(tools.vnc, "Session", open_vnc)
    monkeypatch.setattr(tools, "_call", lambda *_args, **_kwargs: status_response())

    result = tools.kasm_click({"session_id": SID, "x": 10, "y": 20})

    assert '"ok": true' in result
    assert len(opened) == 2
    assert clicks == [(10, 20, 1, 1)]


def test_session_does_not_retry_other_connection_failures(monkeypatch):
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "token", "path": VNC_PATH,
        "workspace": "Chrome", "started": 1,
    }
    opened = []
    monkeypatch.setattr(tools.vnc, "Session", lambda *_: opened.append(1) or (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(tools, "_call", lambda *_args, **_kwargs: pytest.fail("must not refresh"))

    with pytest.raises(ConnectionError, match="offline"):
        tools._session(SID)
    assert opened == [1]


@pytest.mark.parametrize(
    ("user_id", "kasm_id"),
    [("other-owner", SID), (OWNER, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")],
)
def test_session_rejects_refresh_link_for_wrong_owner_or_session(monkeypatch, user_id, kasm_id):
    tools._SESSIONS[SID] = {"username": "alice", "workspace": "Chrome", "started": 1}
    monkeypatch.setattr(tools, "_call", lambda *_args, **_kwargs: status_response(user_id=user_id, kasm_id=kasm_id))
    monkeypatch.setattr(tools.vnc, "Session", lambda *_: pytest.fail("must not open an unbound session"))

    with pytest.raises(RuntimeError, match="did not match"):
        tools._session(SID)
