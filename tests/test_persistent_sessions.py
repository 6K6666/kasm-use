import json
import stat

import pytest

from kasm_use import tools

SID = "1b1a2009c172439f9c0a13110a5d9098"
OWNER = "owner-1"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
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


def configure_state_file(monkeypatch, path):
    monkeypatch.setenv("KASM_SESSION_STATE_FILE", str(path))


def test_state_file_recovers_only_tracked_session_metadata_without_tokens(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "secret-token", "path": "desktop/sid/vnc",
        "workspace": "Chrome", "started": 123.5,
    }

    tools._save_sessions()

    file_mode = stat.S_IMODE(path.stat().st_mode)
    directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
    saved = json.loads(path.read_text())
    assert file_mode == 0o600
    assert directory_mode == 0o700
    assert saved == {
        "version": 1,
        "user_id": OWNER,
        "sessions": [{"session_id": SID, "username": "alice", "workspace": "Chrome", "started": 123.5}],
    }
    assert "secret-token" not in path.read_text()

    tools._SESSIONS.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    tools._load_sessions()

    assert tools._resolve_session({"session_id": SID}) == SID
    assert tools._SESSIONS[SID] == {
        "username": "alice", "workspace": "Chrome", "started": 123.5,
        "session_token": None, "path": None,
    }


def test_state_file_is_bound_to_the_configured_kasm_user(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "token", "workspace": "Chrome", "started": 1,
    }
    tools._save_sessions()
    tools._SESSIONS.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    monkeypatch.setenv("KASM_USER_ID", "different-owner")

    with pytest.raises(RuntimeError, match="different Kasm user"):
        tools._load_sessions()
    assert tools._SESSIONS == {}


def test_recovered_session_refreshes_token_before_reconnecting(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "old-secret", "path": "old/path",
        "workspace": "Chrome", "started": 123.5,
    }
    tools._save_sessions()
    tools._SESSIONS.clear()
    tools._STATE_LOADED = False
    tools._STATE_PATH = None
    monkeypatch.setattr(tools, "_call", lambda *_args, **_kwargs: {
        "kasm_url": f"/#/connect/kasm/{SID}/{OWNER}/fresh-token",
        "kasm": {
            "operational_status": "running",
            "port_map": {"vnc": {"path": "desktop/session/vnc"}},
        },
    })
    opened = []
    monkeypatch.setattr(tools.vnc, "Session", lambda *args: opened.append(args) or object())

    assert tools._resolve_session({}) == SID
    tools._session(SID)

    assert opened[0][1:4] == ("desktop/session/vnc", "alice", "fresh-token")
    assert tools._SESSIONS[SID]["session_token"] == "fresh-token"


def test_stop_removes_session_from_recovery_file(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "token", "workspace": "Chrome", "started": 1,
    }
    tools._save_sessions()
    monkeypatch.setattr(tools, "_call", lambda *_args, **_kwargs: {})

    result = json.loads(tools.kasm_stop({"session_id": SID}))

    assert result["ok"] is True
    assert json.loads(path.read_text())["sessions"] == []


def test_start_persists_only_after_request_returns_session_id(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Chrome", "name": "chrome:latest", "image_id": "img"}]},
        "request_kasm": {"kasm_id": "1b1a2009-c172-439f-9c0a-13110a5d9098", "username": "alice", "session_token": "session-secret"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    calls = []
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_args: calls.append(endpoint) or replies[endpoint])
    monkeypatch.setattr(tools.time, "sleep", lambda *_args: None)

    result = json.loads(tools.kasm_start({"workspace": "Chrome"}))

    assert result["ok"] is True
    assert calls == ["get_images", "request_kasm", "get_kasm_status"]
    saved = json.loads(path.read_text())
    assert saved["sessions"] == [{
        "session_id": SID, "username": "alice", "workspace": "Chrome", "started": tools._SESSIONS[SID]["started"],
    }]
    assert "session-secret" not in path.read_text()


def test_failed_recovery_write_destroys_new_session_and_reports_its_id(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Chrome", "name": "chrome:latest", "image_id": "img"}]},
        "request_kasm": {"kasm_id": "1b1a2009-c172-439f-9c0a-13110a5d9098", "username": "alice", "session_token": "session-secret"},
        "destroy_kasm": {},
    }
    calls = []
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_args: calls.append(endpoint) or replies[endpoint])
    real_save = tools._save_sessions
    saves = 0

    def fail_after_allocation():
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("simulated state storage failure")
        real_save()

    monkeypatch.setattr(tools, "_save_sessions", fail_after_allocation)

    result = json.loads(tools.kasm_start({"workspace": "Chrome"}))

    assert result["ok"] is False
    assert SID in result["error"]
    assert calls == ["get_images", "request_kasm", "destroy_kasm"]
    assert SID not in tools._SESSIONS
    assert json.loads(path.read_text())["sessions"] == []


def test_insecure_state_file_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._save_sessions()
    path.chmod(0o644)
    tools._STATE_LOADED = False
    tools._STATE_PATH = None

    with pytest.raises(RuntimeError, match="private regular file"):
        tools._load_sessions()


def test_insecure_state_directory_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "shared" / "sessions.json"
    path.parent.mkdir(mode=0o755)
    path.parent.chmod(0o755)
    configure_state_file(monkeypatch, path)

    with pytest.raises(RuntimeError, match="owner-only"):
        tools._load_sessions()


def test_state_file_symlink_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    tools._save_sessions()
    target = tmp_path / "target.json"
    path.rename(target)
    path.symlink_to(target)
    tools._STATE_LOADED = False
    tools._STATE_PATH = None

    with pytest.raises(RuntimeError, match="regular file"):
        tools._load_sessions()


def test_state_file_is_not_created_when_recovery_is_disabled():
    tools._SESSIONS[SID] = {
        "username": "alice", "session_token": "token", "workspace": "Chrome", "started": 1,
    }

    tools._save_sessions()

    assert tools._SESSIONS[SID]["session_token"] == "token"
