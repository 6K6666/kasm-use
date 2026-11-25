"""Guacamole transport and Kasm session ownership regressions."""

import json

import pytest

from kasm_use import guac, tools

SID = "1b1a2009c172439f9c0a13110a5d9098"
OWNER = "owner-1"
PATH = "desktop/1b1a2009-c172-439f-9c0a-13110a5d9098/vnc"
LINK = f"/#/connect/kasm/{SID}/{OWNER}/fresh-token"


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setenv("KASM_API_URL", "https://kasm.invalid")
    monkeypatch.setenv("KASM_USER_ID", OWNER)
    monkeypatch.delenv("KASM_SESSION_STATE_FILE", raising=False)
    monkeypatch.setattr(tools, "_SESSIONS", {})
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    monkeypatch.setattr(tools, "_STATE_PATH", None)


def status(link=LINK):
    return {
        "kasm_url": link,
        "kasm": {"operational_status": "running", "user_id": OWNER,
                 "port_map": {"vnc": {"path": PATH}}},
    }


def test_guacamole_uses_owned_link_and_not_vnc(monkeypatch):
    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice",
                            "workspace": "Windows", "started": 1}
    opened = []
    monkeypatch.setattr(tools, "_call", lambda *_a, **_kw: status())
    monkeypatch.setattr(guac, "Session", lambda *args: opened.append(args))
    tools._session(SID)
    assert opened[0][:2] == (SID, "https://kasm.invalid" + LINK)


@pytest.mark.parametrize("link", [
    "https://elsewhere.invalid" + LINK,
    LINK.replace(OWNER, "someone-else"),
    LINK.replace(SID, "a" * 32),
])
def test_guacamole_rejects_mismatched_link_before_browser_open(monkeypatch, link):
    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice",
                            "workspace": "Windows", "started": 1}
    monkeypatch.setattr(tools, "_call", lambda *_a, **_kw: status(link))
    with pytest.raises(RuntimeError, match="connection link"):
        tools._session(SID)


def test_user_select_windows_requests_browser_transport(monkeypatch):
    calls = []

    def api(endpoint, **args):
        calls.append((endpoint, args))
        if endpoint == "get_images":
            return {"images": [{"enabled": True, "friendly_name": "Windows", "image_id": "img",
                                "image_type": "Server", "rdp_client_type": "USER_SELECT"}]}
        if endpoint == "request_kasm":
            return {"kasm_id": SID, "username": "alice", "session_token": "login-token"}
        return status()

    monkeypatch.setattr(tools, "_call", api)
    monkeypatch.setattr(guac, "require_browser", lambda: None)
    assert json.loads(tools.kasm_start({"workspace": "Windows"}))["ok"]
    assert calls[1][1]["rdp_client_type"] == "GUAC"
    assert tools._SESSIONS[SID]["transport"] == "guac"


def test_browser_drives_exact_guacamole_frame():
    """Run with the guac extra and installed Chromium."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    pytest.importorskip("playwright.sync_api")
    try:
        guac.require_browser()
    except RuntimeError:
        pytest.skip("Playwright Chromium is not installed")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/":
                html = f'<iframe id="iframe-id" src="/{PATH}/vnc.html"></iframe>'
            elif self.path == f"/{PATH}/vnc.html":
                html = """<div id="display"></div><script>
                    const canvas = document.createElement("canvas");
                    canvas.width = 23; canvas.height = 19;
                    window.events = [];
                    const Guacamole = { Mouse: { State: class {
                        constructor(...args) { this.args = args; }
                    }}};
                    const app = {
                        isConnected: () => true, mouse: {}, keyboard: {},
                        guac: {
                            getDisplay: () => ({
                                getWidth: () => 23, getHeight: () => 19,
                                flatten: () => canvas
                            }),
                            sendMouseState: (state, scaled) => events.push([state.args, scaled]),
                            sendKeyEvent: (down, sym) => events.push([down, sym])
                        }
                    };
                    </script>"""
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}{LINK}"
        with guac.Session(SID, url, PATH, True) as desktop:
            assert desktop.screenshot().startswith(b"\x89PNG\r\n\x1a\n")
            assert (desktop.width, desktop.height) == (23, 19)
            desktop.click(2, 3, 1, 2)
            desktop.drag(2, 3, 4, 5, 3)
            desktop.scroll(3, 4, "down", 2)
            desktop.scroll(3, 4, "left", 1)
            desktop.type("é")
            desktop.combo([0xffe3, 0x61])
            events = desktop._eval("() => window.events")
        assert events[0] == [[2, 3, True, False, False, False, False], False]
        assert events[6] == [[4, 5, False, False, True, False, False], False]
        assert events[8] == [[3, 4, False, False, False, False, True], False]
        assert events[12] == [True, 0xffe1]
        assert events[15] == [False, 0xffe1]
        assert events[-6:] == [[True, 233], [False, 233], [True, 0xffe3],
                              [True, 0x61], [False, 0x61], [False, 0xffe3]]
    finally:
        guac.close(SID)
        server.shutdown()
        server.server_close()
        thread.join()


def test_browser_transport_restores_without_link_or_token(monkeypatch, tmp_path):
    path = tmp_path / "private" / "sessions.json"
    monkeypatch.setenv("KASM_SESSION_STATE_FILE", str(path))
    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice",
                            "workspace": "Windows", "started": 1,
                            "browser_url": LINK, "session_token": "private-token"}
    tools._save_sessions()
    assert "private-token" not in path.read_text()
    assert "fresh-token" not in path.read_text()

    tools._SESSIONS.clear()
    monkeypatch.setattr(tools, "_STATE_LOADED", False)
    tools._load_sessions()
    assert tools._SESSIONS[SID]["transport"] == "guac"
    assert "browser_url" not in tools._SESSIONS[SID]


def test_guacamole_rejects_other_sessions_desktop_path(monkeypatch):
    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice",
                            "workspace": "Windows", "started": 1}
    reply = status()
    reply["kasm"]["port_map"]["vnc"]["path"] = f"desktop/{'a' * 32}/vnc"
    monkeypatch.setattr(tools, "_call", lambda *_a, **_kw: reply)

    with pytest.raises(RuntimeError, match="connection link"):
        tools._session(SID)


def test_guacamole_refreshes_stale_browser_link_before_input(monkeypatch):
    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice",
                            "workspace": "Windows", "started": 1, "path": PATH,
                            "browser_url": "https://kasm.invalid" + LINK.replace("fresh", "stale")}
    attempts = []

    def attach(_kid, url, _path, _tls):
        attempts.append(url)
        if len(attempts) == 1:
            raise guac.ConnectionError("stale browser link")
        return object()

    monkeypatch.setattr(tools, "_call", lambda *_a, **_kw: status())
    monkeypatch.setattr(guac, "Session", attach)
    assert tools._session(SID) is not None
    assert attempts[-1] == "https://kasm.invalid" + LINK
