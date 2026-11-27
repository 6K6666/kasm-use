"""Session readiness: the optional per-session status file, kasm_wait, and gating."""
import json

import pytest

from kasm_use import server, tools

SID = "a" * 32
IMAGE = "b" * 32
STARTED = 1000.0
MISSING = "The desktop's controller did not report this session."


class FakeVNC:
    width, height = 800, 600
    opened = 0

    def __init__(self, blank=False):
        type(self).opened += 1
        self.blank = blank
        self.actions = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nstate-test"

    def __getattr__(self, name):  # click, drag, type, combo, scroll
        return lambda *a: self.actions.append((name, a))


@pytest.fixture(autouse=True)
def session(monkeypatch, tmp_path):
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setenv("KASM_USER_ID", "owner")
    monkeypatch.setenv("KASM_SESSION_STATUS_DIR", str(tmp_path))
    monkeypatch.setenv("KASM_PILOT_WORKSPACE_ID", IMAGE)
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_SESSIONS", {
        SID: {"started": STARTED, "workspace": "Windows", "image_id": IMAGE, "username": "u"}})
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 10)
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    tools._LAST_SHOT[SID] = (800, 600)
    FakeVNC.opened = 0
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC())
    yield
    tools._LAST_SHOT.clear()


def publish(tmp_path, phase, detail="", typing="ascii", sid=SID):
    (tmp_path / f"{SID}.json").write_text(json.dumps(
        {"session_id": sid, "phase": phase, "detail": detail, "typing": typing, "updated": STARTED}))


# ---------------------------------------------------------------- rule 1: no status file applies
def test_without_a_status_directory_state_follows_the_first_drawn_frame(monkeypatch, tmp_path):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    publish(tmp_path, "failed", "must be ignored")
    assert tools._session_state(SID) == {"state": "preparing", "detail": "", "typing": "unicode"}
    tools._VISIBLE.add(SID)
    assert tools._session_state(SID) == {"state": "ready", "detail": "", "typing": "unicode"}


def test_another_workspace_ignores_the_status_directory(tmp_path):
    tools._SESSIONS[SID]["image_id"] = "c" * 32
    publish(tmp_path, "failed", "must be ignored")
    assert tools._session_state(SID)["state"] == "preparing"
    tools._VISIBLE.add(SID)
    assert tools._session_state(SID) == {"state": "ready", "detail": "", "typing": "unicode"}


@pytest.mark.parametrize("listed", ["c" * 32, f"{'d' * 32}, {'c' * 8}-{'c' * 4}-{'c' * 4}-{'c' * 4}-{'c' * 12} "])
def test_further_workspaces_can_be_listed_for_status_files(monkeypatch, tmp_path, listed):
    """A second controller (macOS beside Windows) prepares another workspace's desktops and
    writes into the same directory."""
    monkeypatch.setenv("KASM_STATUS_WORKSPACE_IDS", listed)
    tools._SESSIONS[SID]["image_id"] = "c" * 32
    publish(tmp_path, "preparing", "macOS is starting")
    assert tools._session_state(SID) == {"state": "preparing", "detail": "macOS is starting", "typing": "ascii"}
    assert "still preparing" in json.loads(tools.kasm_look({"session_id": SID}))["error"]
    tools._SESSIONS[SID]["image_id"] = "e" * 32  # a workspace nobody listed stays a plain desktop
    assert tools._session_state(SID)["state"] == "preparing" and tools._status_file(SID) is None


def test_a_session_recorded_without_its_workspace_id_ignores_the_status_directory(tmp_path):
    del tools._SESSIONS[SID]["image_id"]
    publish(tmp_path, "failed", "must be ignored")
    assert tools._session_state(SID)["state"] == "preparing"


# ------------------------------------------------------------------- rule 2: the file decides
@pytest.mark.parametrize("phase", ["queued", "preparing", "ready", "failed"])
def test_status_file_is_reported(tmp_path, phase):
    publish(tmp_path, phase, "Signing in", "unicode")
    assert tools._session_state(SID) == {"state": phase, "detail": "Signing in", "typing": "unicode"}


def test_a_claimed_dashboard_session_reads_its_status_file(tmp_path):
    tools._SESSIONS[SID] = {"started": STARTED, "workspace": "Windows", "pilot": True, "username": "u"}
    publish(tmp_path, "ready")
    assert tools._session_state(SID)["state"] == "ready"


def test_missing_file_is_preparing_for_sixty_seconds_then_failed(monkeypatch):
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 59)
    assert tools._session_state(SID)["state"] == "preparing"
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 61)
    state = tools._session_state(SID)
    assert (state["state"], state["detail"]) == ("failed", MISSING)


# ---------------------------------------------------- rule 3: unreadable or malformed = missing
@pytest.mark.parametrize("content", [
    "{", "[]", "null", '{"phase": "ready"}',
    json.dumps({"session_id": SID, "phase": "booting", "detail": "", "typing": "ascii", "updated": 1}),
    json.dumps({"session_id": SID, "phase": "ready", "detail": "", "typing": "telepathy", "updated": 1}),
    json.dumps({"session_id": SID, "phase": "ready", "detail": 7, "typing": "ascii", "updated": 1}),
    json.dumps({"session_id": "d" * 32, "phase": "ready", "detail": "", "typing": "ascii", "updated": 1}),
])
def test_malformed_file_counts_as_missing(monkeypatch, tmp_path, content):
    (tmp_path / f"{SID}.json").write_text(content)
    assert tools._session_state(SID)["state"] == "preparing"
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 61)
    assert tools._session_state(SID)["detail"] == MISSING


def test_unreadable_file_counts_as_missing(monkeypatch, tmp_path):
    (tmp_path / f"{SID}.json").mkdir()
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 61)
    assert tools._session_state(SID)["state"] == "failed"


# ------------------------------------------------------------------------------------ gating
GATED = [
    (tools.kasm_look, {}),
    (tools.kasm_click, {"x": 10, "y": 20}),
    (tools.kasm_drag, {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40}),
    (tools.kasm_type, {"text": "hello"}),
    (tools.kasm_key, {"keys": "Return"}),
    (tools.kasm_scroll, {"direction": "down"}),
]


@pytest.mark.parametrize(("tool", "args"), GATED)
@pytest.mark.parametrize("phase", ["preparing", "queued"])
def test_tools_refuse_a_session_that_is_not_ready(tmp_path, tool, args, phase):
    publish(tmp_path, phase, "Windows first-time setup")
    result = json.loads(tool({"session_id": SID, **args}))
    assert result["ok"] is False
    assert "is still preparing" in result["error"] and "kasm_wait" in result["error"]
    assert phase in result["error"] and "Windows first-time setup" in result["error"]
    assert FakeVNC.opened == 0


@pytest.mark.parametrize(("tool", "args"), GATED)
def test_tools_refuse_a_failed_session_with_its_reason(tmp_path, tool, args):
    publish(tmp_path, "failed", "The guest did not boot.")
    result = json.loads(tool({"session_id": SID, **args}))
    assert result["ok"] is False and "The guest did not boot." in result["error"]
    assert "kasm_stop" in result["error"]
    assert FakeVNC.opened == 0


@pytest.mark.parametrize(("tool", "args"), GATED)
def test_tools_refuse_when_the_controller_never_reported(monkeypatch, tool, args):
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 61)
    result = json.loads(tool({"session_id": SID, **args}))
    assert result["ok"] is False and MISSING in result["error"]


@pytest.mark.parametrize(("tool", "args"), GATED)
def test_tools_act_once_the_session_is_ready(tmp_path, tool, args):
    publish(tmp_path, "ready")
    result = tool({"session_id": SID, **args})
    assert FakeVNC.opened == 1
    assert isinstance(result, dict) or json.loads(result)["ok"] is True


def test_a_plain_desktop_that_has_drawn_nothing_is_not_gated(monkeypatch):
    # Only a status file gates: a desktop that is legitimately one flat colour must stay usable.
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    assert tools._session_state(SID)["state"] == "preparing"
    assert json.loads(tools.kasm_key({"session_id": SID, "keys": "Return"}))["ok"] is True


# --------------------------------------------------------------------------------- kasm_wait
def test_wait_returns_as_soon_as_the_session_is_ready(monkeypatch, tmp_path):
    publish(tmp_path, "preparing", "Signing in")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            publish(tmp_path, "ready")

    monkeypatch.setattr(tools.time, "sleep", sleep)
    assert json.loads(tools.kasm_wait({"session_id": SID})) == {
        "ok": True, "session_id": SID, "state": "ready", "detail": "", "waited_seconds": 3}
    assert sleeps == [1, 1, 1]


def test_wait_returns_a_failure_without_waiting(tmp_path):
    publish(tmp_path, "failed", "The guest did not boot.")
    result = json.loads(tools.kasm_wait({"session_id": SID}))
    assert (result["ok"], result["state"], result["detail"], result["waited_seconds"]) == (
        True, "failed", "The guest did not boot.", 0)


@pytest.mark.parametrize(("asked", "waited"), [(None, 60), (1, 5), (30, 30), (1000, 90)])
def test_wait_timeout_is_clamped_and_still_preparing_is_not_an_error(monkeypatch, tmp_path, asked, waited):
    publish(tmp_path, "preparing", "Signing in")
    sleeps = []
    monkeypatch.setattr(tools.time, "sleep", sleeps.append)
    args = {"session_id": SID, **({} if asked is None else {"timeout_seconds": asked})}
    result = json.loads(tools.kasm_wait(args))
    assert result == {"ok": True, "session_id": SID, "state": "preparing", "detail": "Signing in",
                      "waited_seconds": waited}
    assert len(sleeps) == waited


def test_wait_stops_at_the_wall_clock_deadline_even_when_polling_is_slow(monkeypatch, tmp_path):
    publish(tmp_path, "preparing")
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(tools.time, "monotonic", lambda: next(clock))
    sleeps = []
    monkeypatch.setattr(tools.time, "sleep", sleeps.append)
    assert json.loads(tools.kasm_wait({"session_id": SID, "timeout_seconds": 60}))["state"] == "preparing"
    assert len(sleeps) < 5


def test_wait_rejects_a_nonfinite_timeout(tmp_path):
    result = json.loads(tools.kasm_wait({"session_id": SID, "timeout_seconds": float("nan")}))
    assert result == {"ok": False, "error": "timeout_seconds must be finite"}


def test_wait_probes_a_plain_desktop_until_it_draws(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    frames = iter([True, True, False])
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=next(frames)))
    result = json.loads(tools.kasm_wait({"session_id": SID}))
    assert (result["state"], result["waited_seconds"]) == ("ready", 2)


def test_wait_refuses_a_session_this_instance_does_not_track():
    assert "only controls sessions tracked" in json.loads(tools.kasm_wait({"session_id": "e" * 32}))["error"]


# ------------------------------------------------------------------- list, stop, start, server
def test_list_includes_each_sessions_state(monkeypatch, tmp_path):
    publish(tmp_path, "queued", "Another Windows session is using the last slot.")
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: {
        "get_images": {"images": []}, "get_kasms": {"kasms": [{"kasm_id": SID, "operational_status": "running"}]},
    }[endpoint])
    sessions = json.loads(tools.kasm_list({}))["sessions"]
    assert sessions == [{"session_id": SID, "workspace": "Windows", "status": "running", "state": "queued"}]


@pytest.mark.parametrize("phase", ["queued", "preparing", "ready", "failed", None])
def test_stop_works_in_every_state(monkeypatch, tmp_path, phase):
    if phase:
        publish(tmp_path, phase)
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 61)
    calls = []
    monkeypatch.setattr(tools, "_call", lambda endpoint, **kw: calls.append((endpoint, kw)) or {})
    assert json.loads(tools.kasm_stop({"session_id": SID})) == {"ok": True, "session_id": SID, "stopped": True}
    assert calls == [("destroy_kasm", {"kasm_id": SID, "user_id": "owner"})]


GONE = "Kasm destroy_kasm HTTP 400: b'{\"error_message\": \"Invalid kasm_id\"}'"


def refuse_destroy(listed):
    def call(endpoint, **_):
        if endpoint == "destroy_kasm":
            raise RuntimeError(GONE)
        if listed is None:
            raise RuntimeError("Kasm get_kasms HTTP 502: b''")
        return {"kasms": [{"kasm_id": kid} for kid in listed]}
    return call


def test_stop_of_a_session_kasm_already_ended_succeeds_and_forgets_it(monkeypatch):
    """Seen 2026-11-27: the controller ends the Kasm session of a failed launch, and kasm_stop then
    answered with Kasm's HTTP 400 although the session was gone, as the agent wanted."""
    monkeypatch.setattr(tools, "_call", refuse_destroy([]))
    assert json.loads(tools.kasm_stop({"session_id": SID})) == {
        "ok": True, "session_id": SID, "stopped": True, "note": "Kasm had already ended this session."}
    assert SID not in tools._SESSIONS


@pytest.mark.parametrize("listed", [[SID], None])
def test_stop_keeps_a_session_it_could_not_confirm_gone(monkeypatch, listed):
    monkeypatch.setattr(tools, "_call", refuse_destroy(listed))
    result = json.loads(tools.kasm_stop({"session_id": SID}))
    assert (result["ok"], result["error"]) == (False, GONE)
    assert SID in tools._SESSIONS


@pytest.mark.parametrize("reply", [{}, {"kasms": None}, {"kasms": "busy"}, []])
def test_stop_keeps_a_session_when_kasms_list_is_not_a_list(monkeypatch, reply):
    """A reply without a session list says nothing about this session; dropping the record would
    leave a live desktop that nothing tracks."""
    def call(endpoint, **_):
        if endpoint == "destroy_kasm":
            raise RuntimeError(GONE)
        return reply
    monkeypatch.setattr(tools, "_call", call)
    result = json.loads(tools.kasm_stop({"session_id": SID}))
    assert (result["ok"], result["error"]) == (False, GONE)
    assert SID in tools._SESSIONS


def test_stop_keeps_a_session_when_kasm_cannot_be_reached(monkeypatch):
    import urllib.error

    def call(endpoint, **_):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(tools, "_call", call)
    result = json.loads(tools.kasm_stop({"session_id": SID}))
    assert result["ok"] is False and "connection refused" in result["error"]
    assert SID in tools._SESSIONS


def test_start_waits_for_the_controllers_first_report_and_says_queued(monkeypatch, tmp_path):
    """Seen 2026-11-27: a queued session was reported "preparing" because kasm_start answered before
    the controller's next pass had written its status file."""
    kid, detail = "f" * 32, "Another macOS session is using the last slot. Yours starts when one ends."
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Windows", "name": "w:1", "image_id": IMAGE}]},
        "request_kasm": {"kasm_id": kid, "username": "alice", "session_token": "t"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: replies[endpoint])
    sleeps = []

    def controller_reports_on_its_next_pass(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            (tmp_path / f"{kid}.json").write_text(json.dumps(
                {"session_id": kid, "phase": "queued", "detail": detail, "typing": "ascii", "updated": STARTED}))

    monkeypatch.setattr(tools.time, "sleep", controller_reports_on_its_next_pass)
    result = json.loads(tools.kasm_start({"workspace": "Windows"}))
    assert (result["state"], result["ready"], result["detail"]) == ("queued", False, detail)
    assert result["note"] == "Queued behind another session. Call kasm_wait; it starts when a slot frees."
    assert sleeps == [0.5, 0.5, 0.5]


def test_start_reports_the_state_of_a_status_file_session_without_probing_it(monkeypatch):
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Windows", "name": "w:1", "image_id": IMAGE}]},
        "request_kasm": {"kasm_id": "f" * 32, "username": "alice", "session_token": "t"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: replies[endpoint])
    monkeypatch.setattr(tools, "_wait_visible", lambda *a: pytest.fail("must not probe the viewer desktop"))
    result = json.loads(tools.kasm_start({"workspace": "Windows"}))
    assert (result["ok"], result["state"], result["ready"]) == (True, "preparing", False)
    assert result["note"] == "Desktop is still starting. Call kasm_wait."


def test_start_reports_ready_for_a_plain_desktop_that_drew(monkeypatch):
    replies = {
        "get_images": {"images": [{"enabled": True, "friendly_name": "Ubuntu", "name": "u:1", "image_id": "c" * 32}]},
        "request_kasm": {"kasm_id": "f" * 32, "username": "alice", "session_token": "t"},
        "get_kasm_status": {"kasm": {"operational_status": "running"}},
    }
    monkeypatch.setattr(tools, "_call", lambda endpoint, **_: replies[endpoint])
    result = json.loads(tools.kasm_start({"workspace": "Ubuntu"}))
    assert (result["state"], result["ready"]) == ("ready", True)


def test_the_workspace_id_survives_a_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("KASM_SESSION_STATE_FILE", str(tmp_path / "private" / "sessions.json"))
    tools._save_sessions()
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    tools._load_sessions()
    assert tools._SESSIONS[SID]["image_id"] == IMAGE
    assert tools._session_state(SID)["state"] == "preparing"


def test_server_instructions_and_tool_list_teach_the_wait_step():
    assert "kasm_start -> kasm_wait until state is ready -> kasm_look -> act" in server.server.instructions
    wait = next(params for name, _, _, params in tools._TOOLS if name == "kasm_wait")
    assert set(wait["properties"]) == {"session_id", "timeout_seconds"}


# ------------------------------------------------------------- kasm_type and what a session can type
def typed(monkeypatch):
    sessions = []

    def connect(kid):
        sessions.append(FakeVNC())
        return sessions[-1]

    monkeypatch.setattr(tools, "_session", connect)
    return sessions


def test_type_refuses_characters_an_ascii_session_cannot_type(monkeypatch, tmp_path):
    publish(tmp_path, "ready", typing="ascii")
    sessions = typed(monkeypatch)
    result = json.loads(tools.kasm_type({"session_id": SID, "text": "café € é", "press_enter": True}))
    assert result["ok"] is False
    assert "cannot type: é €" in result["error"] and "Use kasm_paste" in result["error"]
    assert sessions == []


def test_type_sends_plain_ascii_to_an_ascii_session(monkeypatch, tmp_path):
    publish(tmp_path, "ready", typing="ascii")
    sessions = typed(monkeypatch)
    text = "1<2 3>4 ,./?;:'[]\\|`~!@#$%^&*() <<EOF =+_-\tline\n"
    assert json.loads(tools.kasm_type({"session_id": SID, "text": text}))["ok"] is True
    assert sessions[0].actions == [("type", (text,))]


def test_type_sends_everything_to_a_unicode_session(monkeypatch, tmp_path):
    publish(tmp_path, "ready", typing="unicode")
    sessions = typed(monkeypatch)
    assert json.loads(tools.kasm_type({"session_id": SID, "text": "café € 日本"}))["ok"] is True
    assert sessions[0].actions == [("type", ("café € 日本",))]


def test_type_sends_everything_to_a_plain_desktop(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    sessions = typed(monkeypatch)
    assert json.loads(tools.kasm_type({"session_id": SID, "text": "café €"}))["ok"] is True
    assert sessions[0].actions == [("type", ("café €",))]


# ------------------------------------------------------ a status file nobody is updating any more
@pytest.mark.parametrize("phase", ["queued", "preparing"])
def test_a_status_file_that_stopped_being_updated_is_a_failure(monkeypatch, tmp_path, phase):
    publish(tmp_path, phase, "Signing in")
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + tools.STATUS_STALE_SECONDS - 1)
    assert tools._session_state(SID)["state"] == phase
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + tools.STATUS_STALE_SECONDS + 1)
    state = tools._session_state(SID)
    assert (state["state"], state["detail"]) == ("failed", "The desktop's controller stopped reporting this session.")
    assert "stopped reporting" in json.loads(tools.kasm_look({"session_id": SID}))["error"]


@pytest.mark.parametrize(("phase", "detail"), [("ready", ""), ("failed", "The guest did not boot.")])
def test_a_finished_status_never_goes_stale(monkeypatch, tmp_path, phase, detail):
    publish(tmp_path, phase, detail)
    monkeypatch.setattr(tools.time, "time", lambda: STARTED + 10 * tools.STATUS_STALE_SECONDS)
    assert tools._session_state(SID) == {"state": phase, "detail": detail, "typing": "ascii"}


@pytest.mark.parametrize("updated", ["now", None, True, float("nan")])
def test_a_status_file_without_a_usable_timestamp_counts_as_missing(tmp_path, updated):
    (tmp_path / f"{SID}.json").write_text(json.dumps(
        {"session_id": SID, "phase": "ready", "detail": "", "typing": "unicode", "updated": updated}))
    assert tools._session_state(SID) == {"state": "preparing", "detail": "", "typing": "ascii"}  # the grace period


# ------------------------------------------------- kasm_wait on a plain desktop that never comes
def test_wait_reports_a_desktop_it_could_never_reach_as_failed(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")

    def gone(kid):
        raise RuntimeError("Kasm session is not running.")

    monkeypatch.setattr(tools, "_session", gone)
    result = json.loads(tools.kasm_wait({"session_id": SID, "timeout_seconds": 5}))
    assert (result["ok"], result["state"], result["waited_seconds"]) == (True, "failed", 5)
    assert result["detail"] == "Could not reach the desktop: Kasm session is not running."


def test_wait_says_when_the_desktop_answers_but_has_drawn_nothing(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    monkeypatch.setattr(tools, "_session", lambda kid: FakeVNC(blank=True))
    result = json.loads(tools.kasm_wait({"session_id": SID, "timeout_seconds": 5}))
    assert (result["state"], result["detail"]) == ("preparing", "The screen is still blank.")


def test_wait_forgives_a_desktop_that_refuses_connections_at_first(monkeypatch):
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    attempts = []

    def connect(kid):
        attempts.append(kid)
        if len(attempts) < 3:
            raise ConnectionError("not accepting connections yet")
        return FakeVNC(blank=False)

    monkeypatch.setattr(tools, "_session", connect)
    assert json.loads(tools.kasm_wait({"session_id": SID}))["state"] == "ready"


# --------------------------------------------------------------------- press_enter after typing
def test_press_enter_waits_a_moment_after_the_text_as_a_person_does(monkeypatch):
    """XFCE's application finder on a 1024x768 desktop, 2026-11-27: Enter sent 30 ms after the last
    character was ignored three times out of three; the same Enter a second later launched the
    program. The finder handles the typed text a moment after it arrives."""
    monkeypatch.delenv("KASM_SESSION_STATUS_DIR")
    desktop = FakeVNC()
    monkeypatch.setattr(tools, "_session", lambda kid: desktop)
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: desktop.actions.append(("sleep", seconds)))
    result = json.loads(tools.kasm_type({"session_id": SID, "text": "xfce4-terminal", "press_enter": True}))
    assert result["pressed_enter"] is True
    assert desktop.actions == [("type", ("xfce4-terminal",)), ("sleep", tools.ENTER_PAUSE_SECONDS),
                               ("combo", ([0xFF0D],))]
    desktop.actions.clear()
    tools.kasm_type({"session_id": SID, "text": "no enter"})
    assert desktop.actions == [("type", ("no enter",))]  # nothing to wait for
