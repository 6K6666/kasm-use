"""Kasm Workspaces desktop control tools — work with any stock Kasm image.

Shared by the MCP server (kasm_use.server) and the Hermes Agent plugin (register() below).

Container desktops use the KasmVNC proxy. Server Workspaces use the optional
Guacamole browser transport for screenshots and input.

Scope: tools control tracked sessions only. A configured pilot may explicitly claim its own
dashboard sessions from one workspace. Two Kasm facts make
that the important boundary:
- A Kasm API key is server-wide. With the permissions kasm-use needs it can list, view and stop any
  user's sessions, and "Users Auth Session" lets it log in on behalf of any user.
- The session_token request_kasm returns is a login token for KASM_USER_ID, not for one session:
  it opens every session that user has.
So kasm-use checks the pilot's owner and workspace before requesting a login link,
never returns tokens or keys to the model, and
doesn't use Kasm's exec or screenshot APIs at all.

Configuration comes from the environment only:
  KASM_API_URL, KASM_API_KEY, KASM_API_KEY_SECRET  - a Kasm API key (see README for permissions)
  KASM_USER_ID       - the Kasm user sessions are created for, so the owner can watch/take over
  KASM_VERIFY_TLS    - "false" to accept a self-signed Kasm certificate (default: verify)
  KASM_USE_SCREEN    - optional WIDTHxHEIGHT asked of each new desktop (default: the image's own size)
  KASM_PILOT_WORKSPACE_ID, KASM_SESSION_STATUS_DIR - optional; sessions of that one workspace
                       take their readiness from <dir>/<session_id>.json, written by whatever
                       prepares the desktop (see README)

Coordinates: every click/scroll uses the coordinate space of the most recent kasm_look image for
that session. The desktop resizes when someone opens the session in their browser, so clicks are
scaled to the size VNC reports on each connection.
"""
from __future__ import annotations

import base64
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
import json
import math
import os
import re
import ssl
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import guac, vnc

TOOLSET = "kasm"
_REQUIRED_ENV = ["KASM_API_URL", "KASM_API_KEY", "KASM_API_KEY_SECRET", "KASM_USER_ID"]
_CTX = (ssl.create_default_context() if os.environ.get("KASM_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
        else ssl._create_unverified_context())

# Sessions started by this process, or restored from its owner-only state file. Tokens remain in
# memory only; the optional state file stores session IDs and display metadata, never login tokens.
_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_STATE_LOADED = False
_STATE_PATH: str | None = None
# Shared state file bookkeeping: the session IDs last seen in the file, and the ones this process
# stopped (or saw end), which must not be adopted again from a copy another process still holds.
_PERSISTED: set[str] = set()
_STOPPED: set[str] = set()
_STATE_VERSION = 1

# kasm_id -> (image_width, image_height) of the last screenshot shown to the model.
_LAST_SHOT: dict[str, tuple[int, int]] = {}
# Sessions whose desktop has drawn something at least once while this process watched.
_VISIBLE: set[str] = set()
START_READY_SECONDS = 20
STATUS_GRACE_SECONDS = 60  # how long a status-file session may go unreported before it has failed


# ----------------------------------------------------------------------------------- API
def _call(endpoint: str, **payload):
    body = dict(api_key=os.environ["KASM_API_KEY"], api_key_secret=os.environ["KASM_API_KEY_SECRET"], **payload)
    req = urllib.request.Request(
        f"{os.environ['KASM_API_URL'].rstrip('/')}/api/public/{endpoint}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        data = urllib.request.urlopen(req, timeout=120, context=_CTX).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Kasm {endpoint} HTTP {e.code}: {e.read()[:200]!r}") from None
    parsed = json.loads(data or b"{}")
    if isinstance(parsed, dict) and parsed.get("error_message"):
        raise RuntimeError(f"Kasm {endpoint}: {parsed['error_message']}")
    return parsed


def _user() -> str:
    return os.environ["KASM_USER_ID"]


def _kid(kasm_id: str) -> str:
    return str(kasm_id).replace("-", "").strip().lower()


def _ok(**fields) -> str:
    return json.dumps({"ok": True, **fields})


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def _session_state_path() -> str | None:
    configured = os.environ.get("KASM_SESSION_STATE_FILE")
    if not configured:
        return None
    path = os.path.abspath(os.path.expanduser(configured))
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    info = os.stat(parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or os.path.realpath(parent) != parent
    ):
        raise RuntimeError("KASM_SESSION_STATE_FILE must be inside an owner-only, non-symlink directory.")
    return path


def _read_state(path: str) -> dict[str, dict]:
    """The session records in the state file, validated. Empty when the file does not exist yet."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError:
        raise RuntimeError("Kasm session state must be a private regular file.") from None

    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
            or info.st_size > 1_048_576
        ):
            raise RuntimeError("Kasm session state must be a private regular file.")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            saved = json.load(stream)
    except (OSError, ValueError, TypeError):
        raise RuntimeError("Kasm session state is unreadable or invalid.") from None
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(saved, dict) or saved.get("version") != _STATE_VERSION:
        raise RuntimeError("Kasm session state has an unsupported format.")
    if _kid(saved.get("user_id", "")) != _kid(_user()):
        raise RuntimeError("Kasm session state belongs to a different Kasm user.")
    entries = saved.get("sessions")
    if not isinstance(entries, list) or len(entries) > 1000:
        raise RuntimeError("Kasm session state has an invalid session list.")

    restored = {}
    for entry in entries:
        if (not isinstance(entry, dict)
            or set(entry) - {"transport", "image_id"} != {"session_id", "username", "workspace", "started"}
            or entry.get("transport", "vnc") not in ("vnc", "guac")
            or not isinstance(entry.get("image_id", ""), str) or len(entry.get("image_id", "")) > 200):
            raise RuntimeError("Kasm session state contains invalid session metadata.")
        kid = _kid(entry["session_id"])
        username, workspace, started = entry["username"], entry["workspace"], entry["started"]
        if (
            not re.fullmatch(r"[a-f0-9]{32}", kid)
            or not isinstance(username, str)
            or not username
            or len(username) > 200
            or not isinstance(workspace, str)
            or not workspace
            or len(workspace) > 200
            or isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
            or started < 0
            or kid in restored
        ):
            raise RuntimeError("Kasm session state contains invalid session metadata.")
        restored[kid] = {
            "username": username,
            "workspace": workspace,
            "started": started,
            "session_token": None,
            "path": None,
            **({"transport": "guac"} if entry.get("transport") == "guac" else {}),
            **({"image_id": entry["image_id"]} if entry.get("image_id") else {}),
        }
    return restored


def _reconcile(on_disk: dict[str, dict]) -> None:
    """Bring this process in line with the state file, which other client processes also write.

    A record on disk that this process does not know was started by another process: adopt it.
    A record this process once saw on disk that is gone was stopped by another process: drop it.
    A record this process stopped is never adopted again.
    """
    with _LOCK:
        gone = [kid for kid in _SESSIONS if kid in _PERSISTED and kid not in on_disk]
        browsers = [kid for kid in gone if _SESSIONS[kid].get("transport") == "guac"]
        for kid in gone:
            _SESSIONS.pop(kid, None)
            _LAST_SHOT.pop(kid, None)
            _VISIBLE.discard(kid)
        for kid, creds in on_disk.items():
            if kid not in _STOPPED:
                _SESSIONS.setdefault(kid, creds)
        _PERSISTED.clear()
        _PERSISTED.update(on_disk)
    for kid in browsers:
        guac.close(kid)


def _state_path() -> str | None:
    """The configured state file. It cannot change while kasm-use is running."""
    global _STATE_LOADED, _STATE_PATH
    path = _session_state_path()
    if _STATE_LOADED and path != _STATE_PATH:
        raise RuntimeError("KASM_SESSION_STATE_FILE cannot change while kasm-use is running.")
    return path


def _load_sessions() -> None:
    """Re-read the state file. Every tool call does, so a session another process started is usable here."""
    global _STATE_LOADED, _STATE_PATH
    with _STATE_LOCK:
        path = _state_path()
        if path is not None:
            _reconcile(_read_state(path))
        _STATE_PATH, _STATE_LOADED = path, True


def _save_sessions() -> None:
    global _STATE_LOADED, _STATE_PATH
    with _STATE_LOCK:
        path = _state_path()
        if path is None:
            _STATE_PATH, _STATE_LOADED = None, True
            return
        # Several client processes share this file. Hold an exclusive lock from the re-read to the
        # replace, so a record another process wrote in between is merged and never overwritten.
        try:
            lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise RuntimeError("Could not write private Kasm session state.") from None
        try:
            if fcntl:  # shortcut: no cross-process lock on Windows; use one client process per state file there
                fcntl.flock(lock, fcntl.LOCK_EX)
            _reconcile(_read_state(path))
            _STATE_PATH, _STATE_LOADED = path, True
            with _LOCK:
                sessions = [
                    {
                        "session_id": kid,
                        "username": creds["username"],
                        "workspace": creds["workspace"],
                        "started": creds["started"],
                        **({"transport": "guac"} if creds.get("transport") == "guac" else {}),
                        **({"image_id": creds["image_id"]} if creds.get("image_id") else {}),
                    }
                    for kid, creds in _SESSIONS.items() if not creds.get("pilot")
                ]
            payload = {"version": _STATE_VERSION, "user_id": _user(), "sessions": sessions}
            fd, temporary = tempfile.mkstemp(prefix=".sessions-", dir=os.path.dirname(path))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    fd = -1
                    json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except (OSError, TypeError, ValueError):
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise RuntimeError("Could not write private Kasm session state.") from None
            with _LOCK:
                _PERSISTED.clear()
                _PERSISTED.update(entry["session_id"] for entry in sessions)
        finally:
            os.close(lock)


def _verify_after(kid: str, result: str, enabled: bool):
    """Return a fresh screenshot with a successful action when requested."""
    if not enabled:
        return result
    action = json.loads(result)
    if not action.get("ok"):
        return result

    look = kasm_look({"session_id": kid, "wait_seconds": 0})
    if not isinstance(look, dict) or not look.get("_multimodal"):
        action["verify_after"] = {
            "status": "unavailable",
            "action_may_have_succeeded": True,
            "instruction": "Call kasm_look and inspect before retrying.",
        }
        return json.dumps(action)

    action["verify_after"] = {"status": "screenshot_returned", "input_sent": True}
    text = json.dumps(action)
    image_parts = [part for part in look["content"] if part.get("type") == "image_url"]
    return {
        "_multimodal": True,
        "content": [{"type": "text", "text": text}, *image_parts],
        "text_summary": text,
    }


def _resolve_session(args: dict) -> str:
    """Resolve a session started by, or restored into, this kasm-use instance."""
    _load_sessions()
    with _LOCK:
        mine = list(_SESSIONS)
    requested = _kid(args.get("session_id") or "")
    if requested:
        if requested not in mine:
            raise PermissionError(
                "kasm-use only controls sessions tracked by this instance. "
                "That session was not started here or recovered from its private state file.")
        return requested
    if not mine:
        raise RuntimeError("No session started yet. Start one with kasm_start first.")
    return max(mine, key=lambda k: _SESSIONS[k]["started"])


def _status_file(kid: str) -> str | None:
    """Where this session's readiness is published, or None when no status file applies to it."""
    directory = os.environ.get("KASM_SESSION_STATUS_DIR")
    workspace = _kid(os.environ.get("KASM_PILOT_WORKSPACE_ID", ""))
    creds = _SESSIONS.get(kid) or {}
    if (not directory or not workspace or not re.fullmatch(r"[a-f0-9]{32}", kid)
            or not (creds.get("pilot") or _kid(creds.get("image_id") or "") == workspace)):
        return None
    return os.path.join(directory, f"{kid}.json")


def _session_state(kid: str) -> dict:
    """{"state": queued|preparing|ready|failed, "detail": str, "typing": ascii|unicode}."""
    creds = _SESSIONS.get(kid) or {}
    path = _status_file(kid)
    if path is None:
        seen = kid in _VISIBLE or creds.get("transport") == "guac"
        return {"state": "ready" if seen else "preparing", "detail": "", "typing": "unicode"}
    try:
        with open(path, encoding="utf-8") as stream:
            status = json.loads(stream.read(65536))
        state, detail, typing = status["phase"], status["detail"], status["typing"]
        if (status["session_id"] == kid and state in ("queued", "preparing", "ready", "failed")
                and isinstance(detail, str) and typing in ("ascii", "unicode")):
            return {"state": state, "detail": detail, "typing": typing}
    except (OSError, ValueError, KeyError, TypeError):
        pass  # unreadable or malformed counts as missing
    if time.time() - creds.get("started", 0) < STATUS_GRACE_SECONDS:
        return {"state": "preparing", "detail": "", "typing": "ascii"}
    return {"state": "failed", "detail": "The Windows controller did not report this session.", "typing": "ascii"}


def _ready_session(args: dict) -> str:
    """Resolve a session for looking or input; refuse one whose status file says it is not ready.

    Only a status file gates. A plain desktop that has drawn nothing yet stays usable, because a
    screen that is legitimately one flat colour would otherwise lock the agent out for good.
    """
    kid = _resolve_session(args)
    if _status_file(kid):
        status = _session_state(kid)
        detail = f": {status['detail'].rstrip('.')}" if status["detail"] else ""
        if status["state"] in ("queued", "preparing"):
            raise RuntimeError(f"Session {kid[:8]} is still preparing (state {status['state']}{detail}). "
                               "Call kasm_wait until its state is ready.")
        if status["state"] == "failed":
            raise RuntimeError(f"Session {kid[:8]} failed to start{detail}. Call kasm_stop, then start a new one.")
    return kid


def _session(kid: str) -> vnc.Session | guac.Session:
    creds = _SESSIONS[kid]
    if creds.get("transport") == "guac":
        cached_url = creds.get("browser_url")
        if not cached_url:
            _refresh_session(kid)
        verify_tls = _CTX.verify_mode != ssl.CERT_NONE
        try:
            return guac.Session(kid, creds["browser_url"], creds["path"], verify_tls)
        except guac.ConnectionError:
            if not cached_url:
                raise
            # Refresh only while attaching, before a caller can send input.
            _refresh_session(kid)
            return guac.Session(kid, creds["browser_url"], creds["path"], verify_tls)
    if not creds.get("path") or not creds.get("session_token"):
        _refresh_session(kid)
    try:
        return vnc.Session(
            os.environ["KASM_API_URL"], creds["path"], creds["username"], creds["session_token"], _CTX
        )
    except vnc.AuthenticationError:
        # The Kasm status API provides a new user login URL. Retry only before the caller has
        # sent any input, and only once, because that URL may invalidate the older login token.
        _refresh_session(kid)
        return vnc.Session(
            os.environ["KASM_API_URL"], creds["path"], creds["username"], creds["session_token"], _CTX
        )


def _refresh_session(kid: str) -> None:
    response = _call("get_kasm_status", kasm_id=kid, user_id=_user())
    _set_connection(kid, _SESSIONS[kid], response)


def _set_connection(kid: str, creds: dict, response: dict) -> None:
    status = response.get("kasm") or {}
    operation_status = (status.get("operational_status") or response.get("operational_status") or "").lower()
    if operation_status and operation_status != "running":
        raise RuntimeError("Kasm session is not running.")

    url = response.get("kasm_url") or ""
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.fragment.strip("/").split("/")
    if (
        len(parts) != 5
        or parts[:2] != ["connect", "kasm"]
        or _kid(parts[2]) != kid
        or _kid(parts[3]) != _kid(_user())
        or not parts[4]
    ):
        raise RuntimeError("Kasm returned a connection link that did not match this session and owner.")

    path = ((status.get("port_map") or {}).get("vnc") or {}).get("path")
    if not path:
        raise RuntimeError("Kasm did not report a VNC path for this session.")
    response_owner = status.get("user_id")
    if response_owner and _kid(response_owner) != _kid(_user()):
        raise RuntimeError("Kasm returned a connection link that did not match this session and owner.")

    if creds.get("transport") == "guac" or creds.get("pilot"):
        relative = path.strip("/").split("/")
        if (parsed.scheme or parsed.netloc or parsed.path not in ("", "/") or parsed.query
                or len(relative) != 3 or relative[::2] != ["desktop", "vnc"]
                or _kid(relative[1]) != kid):
            raise RuntimeError("Kasm returned a connection link that did not match this session and owner.")
        if creds.get("transport") == "guac":
            creds["browser_url"] = urllib.parse.urljoin(os.environ["KASM_API_URL"], url)
    creds["path"] = path
    creds["session_token"] = parts[4]


def _to_desktop(kid: str, x, y, width: int, height: int) -> tuple[int, int]:
    w, h = _LAST_SHOT.get(kid, (0, 0))
    if not w:
        raise RuntimeError("Call kasm_look first so coordinates can be mapped to the screen.")
    x, y = int(x), int(y)
    if not (0 <= x < w and 0 <= y < h):
        raise RuntimeError(f"({x}, {y}) is outside the {w}x{h} screenshot. "
                           "Call kasm_look and use coordinates from that image.")
    return x * width // w, y * height // h


def _screen() -> tuple[int, int] | None:
    """KASM_USE_SCREEN as (width, height), or None when unset."""
    value = os.environ.get("KASM_USE_SCREEN", "")
    if not value:
        return None
    match = re.fullmatch(r"(\d{3,4})[xX](\d{3,4})", value)
    size = (int(match[1]), int(match[2])) if match else (0, 0)
    if not all(640 <= side <= 2560 for side in size):
        raise ValueError("KASM_USE_SCREEN must be WIDTHxHEIGHT with each side from 640 to 2560, like 1280x800.")
    return size


def _apply_screen(kid: str) -> None:
    """Ask a new desktop for the configured size, once, right after it starts."""
    size = _screen()
    if not size or _SESSIONS[kid].get("transport") == "guac":
        return
    for _ in range(5):
        try:
            with _session(kid) as s:
                s.set_desktop_size(*size)
            return
        except Exception:
            time.sleep(1)  # a new desktop may refuse connections for its first seconds
    # A desktop that never took the size keeps its own; kasm_look reports the real one.


def _shot(kid: str) -> tuple[bytes, tuple[int, int], bool]:
    """One live screenshot: PNG, desktop size, and whether the frame was a single flat colour."""
    with _session(kid) as s:
        png, size = s.screenshot(), (s.width, s.height)
        blank = bool(getattr(s, "blank", False))  # the Guacamole transport does not report it
    if not blank:
        _VISIBLE.add(kid)
    return png, size, blank


def _wait_visible(kid: str, seconds: float) -> bool:
    """Poll once a second until the desktop has drawn something. False when it never did."""
    for _ in range(max(1, int(seconds))):
        try:
            if not _shot(kid)[2]:
                return True
        except Exception:
            pass  # a new desktop may refuse connections for its first seconds
        time.sleep(1)
    return False


def _resolve_workspace(images, requested):
    """Pick a Kasm workspace by exact friendly name before any partial match.

    Same-name image versions retain the original newest-tag preference.
    A partial name is accepted only when it resolves to ONE friendly name;
    never silently choose an unrelated image or ambiguous substring.
    """
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("Workspace name must be a nonempty string")
    wanted = requested.strip().casefold()
    enabled = [i for i in images if i.get("enabled")
               and isinstance(i.get("friendly_name"), str)
               and i.get("friendly_name").strip()]
    exact = [i for i in enabled if i["friendly_name"].strip().casefold() == wanted]
    if exact:
        return sorted(exact, key=lambda i: i.get("name") or "")[-1]
    partial = [i for i in enabled if wanted in i["friendly_name"].casefold()]
    distinct = {i["friendly_name"].strip().casefold() for i in partial}
    if len(distinct) > 1:
        names = sorted({i["friendly_name"] for i in partial})
        raise ValueError("Ambiguous workspace name; use an exact name: " + ", ".join(names))
    if not partial:
        names = sorted({i["friendly_name"] for i in enabled})
        raise ValueError("No matching workspace. Available: " + ", ".join(names))
    return sorted(partial, key=lambda i: i.get("name") or "")[-1]


# --------------------------------------------------------------------------------- tools
def kasm_list(args, **kw):
    try:
        _load_sessions()
        images = [{"workspace": i.get("friendly_name"), "image": i.get("name")}
                  for i in _call("get_images").get("images", []) if i.get("enabled")]
        running = {_kid(k["kasm_id"]): k.get("operational_status") for k in _call("get_kasms").get("kasms", [])}
        with _LOCK:
            before = len(_SESSIONS)
            expired = [k for k in _SESSIONS if k not in running]
            browsers = [k for k in expired if _SESSIONS[k].get("transport") == "guac"]
            for kid in expired:  # stopped elsewhere (e.g. dashboard)
                _SESSIONS.pop(kid, None)
                _LAST_SHOT.pop(kid, None)
                _VISIBLE.discard(kid)
                _STOPPED.add(kid)
            sessions = [{"session_id": kid, "workspace": s["workspace"], "status": running.get(kid),
                         "state": _session_state(kid)["state"]}
                        for kid, s in _SESSIONS.items()]
            removed = len(_SESSIONS) != before
        for kid in browsers:
            guac.close(kid)
        if removed:
            _save_sessions()
        return _ok(workspaces=images, sessions=sessions,
                   note="Only sessions tracked by this kasm-use instance are listed or controllable.")
    except Exception as e:
        return _err(str(e))


def kasm_claim_pilot(args, **kw):
    """Explicitly attach to the selected Kasm owner's group-scoped pilot."""
    try:
        kid = _kid(args.get("session_id", ""))
        image = _kid(os.environ.get("KASM_PILOT_WORKSPACE_ID", ""))
        username = os.environ.get("KASM_PILOT_USERNAME", "")
        if not re.fullmatch(r"[a-f0-9]{32}", image) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,200}", username):
            return _err("Dashboard pilot attachment is disabled.")
        if not re.fullmatch(r"[a-f0-9]{32}", kid):
            return _err("Supply the exact dashboard session ID.")
        _load_sessions()
        matches = [row for row in _call("get_kasms").get("kasms", [])
                   if _kid(row.get("kasm_id", "")) == kid
                   and _kid(row.get("user_id", "")) == _kid(_user())
                   and _kid(row.get("image_id", "")) == image
                   and row.get("operational_status") == "running"]
        if len(matches) != 1:
            return _err("No running dashboard pilot matches this session and configured owner.")

        response = _call("get_kasm_status", kasm_id=kid, user_id=_user())
        row = response.get("kasm") or {}
        if (_kid(row.get("kasm_id", "")) != kid
                or _kid(row.get("user_id", "")) != _kid(_user())
                or _kid(row.get("image_id", "")) != image
                or row.get("operational_status") != "running"):
            return _err("Kasm could not confirm the dashboard pilot's identity.")
        creds = {"username": username, "workspace": "Windows 11 Dockur Pilot", "started": time.time(), "pilot": True}
        _set_connection(kid, creds, response)
        with _LOCK:
            _SESSIONS.setdefault(kid, creds)
        # A claim is process-local. Restarting this process requires a fresh claim.
        return _ok(session_id=kid, workspace=creds["workspace"])
    except Exception as e:
        return _err(str(e))


def kasm_start(args, **kw):
    workspace = args.get("workspace") or "Chrome"
    try:
        _load_sessions()
        _save_sessions()  # fail before allocating a Kasm if configured recovery storage is unusable
        # Persistent profiles are opt-in per request on Kasm 1.19. Never
        # expose Reset: it can delete an existing profile and SSH keys.
        mode = args.get("persistent_profile_mode")
        if mode not in (None, "Enabled", "Disabled"):
            return _err("persistent_profile_mode must be Enabled or Disabled")
        images = _call("get_images").get("images", [])
        img = _resolve_workspace(images, workspace)
        request = {"image_id": img["image_id"], "user_id": _user(), "enable_sharing": False}
        transport = "vnc"
        if img.get("image_type") in ("Server", "Server Pool"):
            client = img.get("rdp_client_type")
            if client == "RDP_CLIENT":
                return _err("This workspace requires native RDP. Enable the Kasm web client for kasm-use.")
            guac.require_browser()
            transport = "guac"
            if client == "USER_SELECT":
                request["rdp_client_type"] = "GUAC"
        if mode is not None:
            request["persistent_profile_mode"] = mode
        started = _call("request_kasm", **request)
        kid = _kid(started["kasm_id"])
        if not (started.get("session_token") and started.get("username")):
            try:  # don't leave an uncontrollable session running
                _call("destroy_kasm", kasm_id=kid, user_id=_user())
            except Exception:
                pass
            return _err("Kasm did not return a session login token (needs Kasm 1.19+ and the API key's "
                        "'Users Auth Session' permission), so kasm-use can't control the session.")
        with _LOCK:
            _SESSIONS[kid] = {"username": started["username"], "session_token": started["session_token"],
                              "workspace": img.get("friendly_name"), "started": time.time(), "transport": transport,
                              "image_id": img["image_id"]}
        try:
            _save_sessions()
        except Exception:
            try:
                _call("destroy_kasm", kasm_id=kid, user_id=_user())
            except Exception:
                return _err(
                    f"Kasm session {kid} was created, but its recovery record could not be saved and "
                    "automatic destruction failed. Use kasm_list and stop this exact session; do not start another."
                )
            with _LOCK:
                _SESSIONS.pop(kid, None)
                _LAST_SHOT.pop(kid, None)
                _VISIBLE.discard(kid)
                _STOPPED.add(kid)
            try:
                _save_sessions()
            except Exception:
                pass
            return _err(f"Kasm session {kid} was created but not saved for recovery, so it was destroyed.")
        for _ in range(60):
            status = (_call("get_kasm_status", kasm_id=kid, user_id=_user()).get("kasm") or {}).get("operational_status")
            if status == "running":
                break
            time.sleep(5)
        else:
            return _err(f"Session {kid} did not reach 'running'.")
        _apply_screen(kid)
        if _status_file(kid):  # whatever prepares this desktop says when it is ready; the viewer's own screen does not
            state = _session_state(kid)["state"]
        else:
            # shortcut: a Guacamole desktop is reported ready unseen; probing it would open a browser here.
            state = "ready" if transport == "guac" or _wait_visible(kid, START_READY_SECONDS) else "preparing"
        ready = state == "ready"
        return _ok(session_id=kid, workspace=img.get("friendly_name"), image=img.get("name"), state=state, ready=ready,
                   note="Desktop is visible. Call kasm_look next." if ready
                   else "Desktop is still starting. Call kasm_wait.")
    except Exception as e:
        return _err(str(e))


def kasm_wait(args, **kw):
    try:
        kid = _resolve_session(args)
        timeout = float(args.get("timeout_seconds", 60))
        if not math.isfinite(timeout):
            return _err("timeout_seconds must be finite")
        timeout = int(max(5.0, min(timeout, 90.0)))  # a public proxy cuts idle responses at 100 s
        began = time.monotonic()
        for waited in range(timeout + 1):
            if not _status_file(kid) and kid not in _VISIBLE:
                try:
                    _shot(kid)  # a plain desktop is ready once it has drawn something
                except Exception:
                    pass
            status = _session_state(kid)
            if (status["state"] in ("ready", "failed") or waited == timeout
                    or time.monotonic() - began >= timeout):
                break
            time.sleep(1)
        return _ok(session_id=kid, state=status["state"], detail=status["detail"], waited_seconds=waited)
    except Exception as e:
        return _err(str(e))


def kasm_look(args, **kw):
    try:
        kid = _ready_session(args)
        delay = float(args.get("wait_seconds", 2))
        if not math.isfinite(delay):
            return _err("wait_seconds must be finite")
        time.sleep(max(0.0, min(delay, 30.0)))
        png, size, blank = _shot(kid)
        _LAST_SHOT[kid] = size
        summary = (f"Screenshot of Kasm session {kid[:8]} ({size[0]}x{size[1]}, live). Give kasm_click / kasm_drag / "
                   f"kasm_scroll coordinates in THIS image's pixels.")
        if blank:
            summary += " Screen is blank; the desktop is still starting. Wait and call kasm_look again."
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ],
            "text_summary": summary,
        }
    except Exception as e:
        return _err(str(e))


def kasm_click(args, **kw):
    try:
        kid = _ready_session(args)
        button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button", "left"), 1)
        with _session(kid) as s:
            s.click(*_to_desktop(kid, args["x"], args["y"], s.width, s.height), button,
                    2 if args.get("double") else 1)
        return _verify_after(kid, _ok(session_id=kid, clicked=[args["x"], args["y"]]), args.get("verify_after"))
    except Exception as e:
        return _err(str(e))


def kasm_drag(args, **kw):
    try:
        kid = _ready_session(args)
        button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button", "left"), 1)
        with _session(kid) as s:
            start = _to_desktop(kid, args["from_x"], args["from_y"], s.width, s.height)
            end = _to_desktop(kid, args["to_x"], args["to_y"], s.width, s.height)
            s.drag(*start, *end, button)
        return _verify_after(
            kid,
            _ok(session_id=kid, dragged=[[args["from_x"], args["from_y"]], [args["to_x"], args["to_y"]]]),
            args.get("verify_after"),
        )
    except Exception as e:
        return _err(str(e))


def kasm_type(args, **kw):
    text = args.get("text", "")
    if not text:
        return _err("text is required")
    try:
        kid = _ready_session(args)
        if _session_state(kid)["typing"] == "ascii":
            bad = list(dict.fromkeys(ch for ch in text if not (" " <= ch <= "~" or ch in "\n\t")))
            if bad:
                return _err(f"This session cannot type: {' '.join(bad)}. Nothing was typed. "
                            "Use kasm_paste for this text.")
        with _session(kid) as s:
            s.type(text)
            if args.get("press_enter"):
                s.combo([vnc.keysym("Return")])
        return _verify_after(
            kid,
            _ok(session_id=kid, typed_chars=len(text), pressed_enter=bool(args.get("press_enter"))),
            args.get("verify_after"),
        )
    except Exception as e:
        return _err(str(e))


_KEY_RE = re.compile(r"^[A-Za-z0-9_+]+$")
PASTE_MAX_BYTES = 65536  # shortcut: KasmVNC drops an oversized clipboard silently, raise if a real paste needs more
PASTE_SETTLE_SECONDS = 0.5


def kasm_paste(args, **kw):
    text = args.get("text", "")
    if not text:
        return _err("text is required")
    if len(text.encode("utf-8")) > PASTE_MAX_BYTES:
        return _err(f"text is too large to paste ({PASTE_MAX_BYTES} bytes at most). Paste it in parts.")
    shortcut = args.get("shortcut") or "ctrl+v"
    if not _KEY_RE.match(shortcut):
        return _err("shortcut must be key names joined by +, like ctrl+v or ctrl+shift+v")
    try:
        combo = [vnc.keysym(part) for part in shortcut.split("+") if part]
        kid = _ready_session(args)
        with _session(kid) as s:
            if not hasattr(s, "set_clipboard"):
                return _err("This workspace's connection has no clipboard. Use kasm_type.")
            s.set_clipboard(text)
            time.sleep(PASTE_SETTLE_SECONDS)  # let the desktop see the new clipboard before the shortcut
            s.combo(combo)
            time.sleep(PASTE_SETTLE_SECONDS)  # the paste is served from this connection; keep it open
        return _verify_after(kid, _ok(session_id=kid, pasted_chars=len(text), shortcut=shortcut),
                             args.get("verify_after"))
    except Exception as e:
        return _err(str(e))


def kasm_key(args, **kw):
    keys = [k for k in re.split(r"[\s,]+", args.get("keys", "")) if k]
    bad = [k for k in keys if not _KEY_RE.match(k)]
    if not keys or bad:
        return _err(f"keys must be key names like ctrl+l, Return, Tab, Escape (invalid: {bad})")
    try:
        combos = [[vnc.keysym(part) for part in k.split("+") if part] for k in keys]
        kid = _ready_session(args)
        with _session(kid) as s:
            for combo in combos:
                s.combo(combo)
        return _verify_after(kid, _ok(session_id=kid, keys=keys), args.get("verify_after"))
    except Exception as e:
        return _err(str(e))


def kasm_scroll(args, **kw):
    try:
        kid = _ready_session(args)
        amount = max(1, min(int(args.get("amount", 5)), 30))
        direction = args.get("direction", "down")
        if direction not in ("up", "down", "left", "right"):
            return _err("direction must be up, down, left or right")
        with _session(kid) as s:
            if "x" in args and "y" in args:
                x, y = _to_desktop(kid, args["x"], args["y"], s.width, s.height)
            else:
                x, y = s.width // 2, s.height // 2
            s.scroll(x, y, direction, amount)
        return _verify_after(
            kid, _ok(session_id=kid, direction=direction, amount=amount), args.get("verify_after")
        )
    except Exception as e:
        return _err(str(e))


def kasm_stop(args, **kw):
    try:
        kid = _resolve_session(args)
        _call("destroy_kasm", kasm_id=kid, user_id=_user())
        with _LOCK:
            stopped = _SESSIONS.pop(kid, {})
            _LAST_SHOT.pop(kid, None)
            _VISIBLE.discard(kid)
            _STOPPED.add(kid)
        _save_sessions()
        if stopped.get("transport") == "guac":
            guac.close(kid)
        return _ok(session_id=kid, stopped=True)
    except Exception as e:
        return _err(str(e))


# ------------------------------------------------------------------------------ schemas
_SID = {"session_id": {"type": "string",
                       "description": "A session started by kasm_start. Omit to use the most recent one."}}

_TOOLS = [
    ("kasm_list", kasm_list, "List Kasm workspaces you can start, and the sessions you started.",
     {"type": "object", "properties": {}}),
    ("kasm_claim_pilot", kasm_claim_pilot,
     "Attach to a running Windows pilot session launched from the dashboard by this instance's configured owner.",
     {"type": "object", "required": ["session_id"], "properties": _SID}),
    ("kasm_start", kasm_start,
     "Start a Kasm workspace session (e.g. 'Chrome', 'Terminal') as the owner, so they can watch it. "
     "Returns the session's state: when it is ready call kasm_look, otherwise call kasm_wait until it is. "
     "Only sessions you start can be controlled.",
     {"type": "object", "properties": {
         "workspace": {"type": "string", "description": "Workspace name, e.g. Chrome"},
         "persistent_profile_mode": {
             "type": "string", "enum": ["Enabled", "Disabled"],
             "description": "Explicitly enable or disable a workspace persistent profile at launch. "
                            "Defaults to Kasm policy. Reset is intentionally not supported."
         }
     }}),
    ("kasm_wait", kasm_wait,
     "Wait until a session's desktop is ready to look at and use. Returns its state: ready, preparing, queued "
     "or failed, with a detail line. Some desktops take minutes to prepare; while the state is preparing or "
     "queued, call kasm_wait again. Each call returns within timeout_seconds.",
     {"type": "object", "properties": {
         **_SID, "timeout_seconds": {"type": "number", "description": "How long to wait, 5-90 (default 60)."}}}),
    ("kasm_look", kasm_look,
     "Live screenshot of your Kasm session. Look before clicking. After an action, use verify_after=true "
     "to return a screenshot with the action, or call kasm_look to inspect the result.",
     {"type": "object", "properties": {**_SID, "wait_seconds": {"type": "number", "description": "Pause before capturing (default 2)."}}}),
    ("kasm_click", kasm_click, "Click at x,y in the coordinate space of the latest kasm_look image.",
     {"type": "object", "required": ["x", "y"], "properties": {
         **_SID, "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_drag", kasm_drag,
     "Drag with the mouse held down from one point to another, in the coordinate space of the latest "
     "kasm_look image: move menu items, drag windows or tabs, move sliders, select text, drag and drop.",
     {"type": "object", "required": ["from_x", "from_y", "to_x", "to_y"], "properties": {
         **_SID, "from_x": {"type": "integer"}, "from_y": {"type": "integer"},
         "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_type", kasm_type,
     "Type text into the focused field of the Kasm session (click the field first). NEVER type passwords, "
     "MFA codes, or payment details — stop and ask the owner to take over the session for those.",
     {"type": "object", "required": ["text"], "properties": {
         **_SID, "text": {"type": "string"}, "press_enter": {"type": "boolean"},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_paste", kasm_paste,
     "Paste text into the focused field through the clipboard: fast, and exact for any characters. Sets the "
     "session's clipboard, then presses the paste shortcut. Terminals usually need shortcut \"ctrl+shift+v\". "
     "It replaces whatever was on the session's clipboard. NEVER paste passwords, "
     "MFA codes, or payment details — stop and ask the owner to take over the session for those.",
     {"type": "object", "required": ["text"], "properties": {
         **_SID, "text": {"type": "string"},
         "shortcut": {"type": "string", "description": "Paste shortcut (default ctrl+v)."},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_key", kasm_key, "Press keys/shortcuts in the Kasm session, e.g. 'ctrl+l', 'Return', 'Tab', 'Escape'.",
     {"type": "object", "required": ["keys"], "properties": {
         **_SID, "keys": {"type": "string"},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_scroll", kasm_scroll, "Scroll the Kasm session, optionally at x,y from the latest kasm_look image.",
     {"type": "object", "properties": {
         **_SID, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "description": "Wheel clicks, 1-30 (default 5)"},
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "verify_after": {"type": "boolean", "description": "Return a fresh screenshot after sending the input."}}}),
    ("kasm_stop", kasm_stop, "Stop (destroy) a session you started, when the task is finished or the owner asks.",
     {"type": "object", "properties": {**_SID}}),
]


def _available() -> bool:
    return all(os.environ.get(v) for v in _REQUIRED_ENV)


def register(ctx) -> None:
    """Hermes Agent plugin entry point (see hermes-plugin/)."""
    for name, handler, description, params in _TOOLS:
        ctx.register_tool(
            name=name, toolset=TOOLSET,
            schema={"name": name, "description": description, "parameters": params},
            handler=handler, check_fn=_available, requires_env=_REQUIRED_ENV,
            description=description, emoji="🖥️",
        )
