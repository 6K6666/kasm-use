"""Kasm Workspaces desktop control tools — work with any stock Kasm image.

Shared by the MCP server (kasm_use.server) and the Hermes Agent plugin (register() below).

Eyes and hands are the session's own KasmVNC server, reached through Kasm's proxy (see vnc.py):
live framebuffer reads for screenshots, VNC pointer/key events for input.

Scope: every tool acts ONLY on sessions this process started with kasm_start. Two Kasm facts make
that the important boundary:
- A Kasm API key is server-wide. With the permissions kasm-use needs it can list, view and stop any
  user's sessions, and "Users Auth Session" lets it log in on behalf of any user.
- The session_token request_kasm returns is a login token for KASM_USER_ID, not for one session:
  it opens every session that user has.
So kasm-use never accepts a session it didn't start, never returns tokens or keys to the model, and
doesn't use Kasm's exec or screenshot APIs at all.

Configuration comes from the environment only:
  KASM_API_URL, KASM_API_KEY, KASM_API_KEY_SECRET  - a Kasm API key (see README for permissions)
  KASM_USER_ID       - the Kasm user sessions are created for, so the owner can watch/take over
  KASM_VERIFY_TLS    - "false" to accept a self-signed Kasm certificate (default: verify)

Coordinates: every click/scroll uses the coordinate space of the most recent kasm_look image for
that session. The desktop resizes when someone opens the session in their browser, so clicks are
scaled to the size VNC reports on each connection.
"""
from __future__ import annotations

import base64
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

from . import vnc

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
_STATE_VERSION = 1

# kasm_id -> (image_width, image_height) of the last screenshot shown to the model.
_LAST_SHOT: dict[str, tuple[int, int]] = {}


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


def _load_sessions() -> None:
    global _STATE_LOADED, _STATE_PATH
    with _STATE_LOCK:
        path = _session_state_path()
        if _STATE_LOADED:
            if path != _STATE_PATH:
                raise RuntimeError("KASM_SESSION_STATE_FILE cannot change while kasm-use is running.")
            return
        if path is None:
            _STATE_PATH, _STATE_LOADED = None, True
            return

        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
        except FileNotFoundError:
            _STATE_PATH, _STATE_LOADED = path, True
            return
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
            if not isinstance(entry, dict) or set(entry) != {"session_id", "username", "workspace", "started"}:
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
            }

        with _LOCK:
            for kid, creds in restored.items():
                _SESSIONS.setdefault(kid, creds)
        _STATE_PATH, _STATE_LOADED = path, True


def _save_sessions() -> None:
    _load_sessions()
    with _STATE_LOCK:
        path = _session_state_path()
        if path is None:
            return
        with _LOCK:
            sessions = [
                {
                    "session_id": kid,
                    "username": creds["username"],
                    "workspace": creds["workspace"],
                    "started": creds["started"],
                }
                for kid, creds in _SESSIONS.items()
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


def _session(kid: str) -> vnc.Session:
    creds = _SESSIONS[kid]
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
    status = response.get("kasm") or {}
    operation_status = (status.get("operational_status") or response.get("operational_status") or "").lower()
    if operation_status and operation_status != "running":
        raise RuntimeError("Kasm session is not running.")

    url = response.get("kasm_url") or ""
    parts = urllib.parse.urlsplit(url).fragment.strip("/").split("/")
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

    creds = _SESSIONS[kid]
    creds["path"] = path
    creds["session_token"] = parts[4]


def _to_desktop(kid: str, x, y, width: int, height: int) -> tuple[int, int]:
    w, h = _LAST_SHOT.get(kid, (0, 0))
    if not w:
        raise RuntimeError("Call kasm_look first so coordinates can be mapped to the screen.")
    x, y = max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))
    return x * width // w, y * height // h


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
            for kid in [k for k in _SESSIONS if k not in running]:  # stopped elsewhere (e.g. dashboard)
                _SESSIONS.pop(kid, None)
                _LAST_SHOT.pop(kid, None)
            sessions = [{"session_id": kid, "workspace": s["workspace"], "status": running.get(kid)}
                        for kid, s in _SESSIONS.items()]
            removed = len(_SESSIONS) != before
        if removed:
            _save_sessions()
        return _ok(workspaces=images, sessions=sessions,
                   note="Only sessions tracked by this kasm-use instance are listed or controllable.")
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
                              "workspace": img.get("friendly_name"), "started": time.time()}
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
        return _ok(session_id=kid, workspace=img.get("friendly_name"), image=img.get("name"),
                   note="Session is running and visible in the owner's Kasm dashboard. Call kasm_look next.")
    except Exception as e:
        return _err(str(e))


def kasm_look(args, **kw):
    try:
        kid = _resolve_session(args)
        delay = float(args.get("wait_seconds", 2))
        if not math.isfinite(delay):
            return _err("wait_seconds must be finite")
        time.sleep(max(0.0, min(delay, 30.0)))
        with _session(kid) as s:
            png, size = s.screenshot(), (s.width, s.height)
        _LAST_SHOT[kid] = size
        summary = (f"Screenshot of Kasm session {kid[:8]} ({size[0]}x{size[1]}, live). Give kasm_click / kasm_drag / "
                   f"kasm_scroll coordinates in THIS image's pixels.")
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
        kid = _resolve_session(args)
        button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button", "left"), 1)
        with _session(kid) as s:
            s.click(*_to_desktop(kid, args["x"], args["y"], s.width, s.height), button,
                    2 if args.get("double") else 1)
        return _verify_after(kid, _ok(session_id=kid, clicked=[args["x"], args["y"]]), args.get("verify_after"))
    except Exception as e:
        return _err(str(e))


def kasm_drag(args, **kw):
    try:
        kid = _resolve_session(args)
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
        kid = _resolve_session(args)
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


def kasm_key(args, **kw):
    keys = [k for k in re.split(r"[\s,]+", args.get("keys", "")) if k]
    bad = [k for k in keys if not _KEY_RE.match(k)]
    if not keys or bad:
        return _err(f"keys must be key names like ctrl+l, Return, Tab, Escape (invalid: {bad})")
    try:
        combos = [[vnc.keysym(part) for part in k.split("+") if part] for k in keys]
        kid = _resolve_session(args)
        with _session(kid) as s:
            for combo in combos:
                s.combo(combo)
        return _verify_after(kid, _ok(session_id=kid, keys=keys), args.get("verify_after"))
    except Exception as e:
        return _err(str(e))


def kasm_scroll(args, **kw):
    try:
        kid = _resolve_session(args)
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
            _SESSIONS.pop(kid, None)
            _LAST_SHOT.pop(kid, None)
        _save_sessions()
        return _ok(session_id=kid, stopped=True)
    except Exception as e:
        return _err(str(e))


# ------------------------------------------------------------------------------ schemas
_SID = {"session_id": {"type": "string",
                       "description": "A session started by kasm_start. Omit to use the most recent one."}}

_TOOLS = [
    ("kasm_list", kasm_list, "List Kasm workspaces you can start, and the sessions you started.",
     {"type": "object", "properties": {}}),
    ("kasm_start", kasm_start,
     "Start a Kasm workspace session (e.g. 'Chrome', 'Terminal') as the owner, so they can watch it. "
     "Returns once the session is running; then call kasm_look. Only sessions you start can be controlled.",
     {"type": "object", "properties": {
         "workspace": {"type": "string", "description": "Workspace name, e.g. Chrome"},
         "persistent_profile_mode": {
             "type": "string", "enum": ["Enabled", "Disabled"],
             "description": "Explicitly enable or disable a workspace persistent profile at launch. "
                            "Defaults to Kasm policy. Reset is intentionally not supported."
         }
     }}),
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
