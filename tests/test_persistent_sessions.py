import json
import stat
import time

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
        "image_id": "img",
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


# ------------------------------------------------- one state file shared by several client processes
S1, S2 = "1" * 32, "2" * 32


class Process:
    """One kasm-use process: its own in-memory state, sharing the state file with the others."""

    def __init__(self):
        self.state = {"_SESSIONS": {}, "_PERSISTED": set(), "_STOPPED": set(), "_MINE": set(),
                      "_STATE_LOADED": False, "_STATE_PATH": None}

    def __enter__(self):
        for name, value in self.state.items():
            setattr(tools, name, value)
        return self

    def __exit__(self, *_):
        for name in self.state:
            self.state[name] = getattr(tools, name)

    def start(self, kid):
        with self:
            tools._load_sessions()
            tools._SESSIONS[kid] = {"username": "alice", "session_token": "t", "workspace": "Ubuntu",
                                    "started": time.time()}
            tools._MINE.add(kid)  # as kasm_start does
            tools._save_sessions()

    def default(self):
        with self:
            return tools._resolve_session({})

    def stop(self, kid, monkeypatch):
        with self:
            monkeypatch.setattr(tools, "_call", lambda *a, **k: {})
            assert json.loads(tools.kasm_stop({"session_id": kid}))["ok"] is True

    def save(self):
        with self:
            tools._save_sessions()

    def resolve(self, kid):
        with self:
            return tools._resolve_session({"session_id": kid})


def on_disk(path):
    return sorted(entry["session_id"] for entry in json.loads(path.read_text())["sessions"])


def test_two_processes_starting_sessions_keep_both_records(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    a.start(S1)
    b.start(S2)
    a.save()
    assert on_disk(path) == [S1, S2]
    assert a.resolve(S2) == S2 and b.resolve(S1) == S1


def test_a_session_one_process_stopped_does_not_reappear_when_another_saves(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    a.start(S1)
    b.start(S2)          # b has now read s1 from the file
    assert b.resolve(S1) == S1
    a.stop(S1, monkeypatch)
    b.save()
    assert on_disk(path) == [S2]
    with pytest.raises(PermissionError):
        b.resolve(S1)
    a.save()
    assert on_disk(path) == [S2]


def test_a_running_process_can_use_a_session_another_started_later(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    b.save()             # b loaded the (empty) state before s1 existed
    a.start(S1)
    assert b.resolve(S1) == S1
    a.stop(S1, monkeypatch)
    with pytest.raises(PermissionError):
        b.resolve(S1)


def test_a_session_that_ended_elsewhere_is_dropped_for_every_process(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    a.start(S1)
    b.start(S2)
    with a:              # kasm_list notices Kasm no longer runs s1
        monkeypatch.setattr(tools, "_call", lambda endpoint, **_: {
            "get_images": {"images": []}, "get_kasms": {"kasms": [{"kasm_id": S2, "operational_status": "running"}]},
        }[endpoint])
        listed = json.loads(tools.kasm_list({}))["sessions"]
    assert [row["session_id"] for row in listed] == [S2]
    assert on_disk(path) == [S2]
    b.save()
    assert on_disk(path) == [S2]


def test_real_processes_saving_at_once_lose_no_record(monkeypatch, tmp_path):
    import subprocess
    import sys
    path = tmp_path / "private" / "sessions.json"
    script = (
        "import sys, time\n"
        "from kasm_use import tools\n"
        "kid = sys.argv[1] * 32\n"
        "tools._load_sessions()\n"
        "time.sleep(0.2)\n"  # everyone has loaded the empty file before anyone saves
        "tools._SESSIONS[kid] = {'username': 'alice', 'workspace': 'Ubuntu', 'started': 1}\n"
        "tools._save_sessions()\n"
    )
    env = {"KASM_SESSION_STATE_FILE": str(path), "KASM_USER_ID": OWNER, "KASM_API_URL": "https://kasm.invalid",
           "PATH": "/usr/bin:/bin"}
    workers = [subprocess.Popen([sys.executable, "-c", script, digit], env=env) for digit in "12345678"]
    assert [worker.wait(timeout=30) for worker in workers] == [0] * 8
    assert on_disk(path) == [digit * 32 for digit in "12345678"]


# ------------------------------------------- which session a call means when it names none
def test_the_default_session_never_moves_to_one_another_process_started(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    a.start(S1)
    b.start(S2)                       # newer, and visible to a through the shared file
    assert a.default() == S1          # a's calls keep going to the session a started
    assert b.default() == S2
    assert a.resolve(S2) == S2        # once a names s2 itself, it is one of a's too
    assert a.default() == S2


def test_stop_without_an_id_never_destroys_another_processs_session(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    a, b = Process(), Process()
    a.start(S1)
    b.start(S2)
    destroyed = []
    with a:
        monkeypatch.setattr(tools, "_call", lambda endpoint, **kw: destroyed.append(kw.get("kasm_id")) or {})
        assert json.loads(tools.kasm_stop({}))["session_id"] == S1
    assert destroyed == [S1] and on_disk(path) == [S2]


def test_after_a_restart_the_only_tracked_session_is_the_default(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    Process().start(S1)
    assert Process().default() == S1


def test_after_a_restart_with_several_sessions_the_call_must_name_one(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    configure_state_file(monkeypatch, path)
    Process().start(S1)
    Process().start(S2)
    restarted = Process()
    with pytest.raises(RuntimeError, match="session_id.*kasm_list"):
        restarted.default()
    assert restarted.resolve(S1) == S1 and restarted.default() == S1
