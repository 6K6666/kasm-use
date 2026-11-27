"""Guacamole transport and Kasm session ownership regressions."""

from contextlib import contextmanager
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


@contextmanager
def kasm_page(paint):
    """A stand-in for Kasm's Guacamole client page in real Chromium. `paint` is JavaScript that
    draws on the 23x19 display `canvas`; without any, the display stays empty (transparent)."""
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
                    const fill = (colour, width) => { const c = canvas.getContext("2d"); c.fillStyle = colour; c.fillRect(0, 0, width, 19); };
                    const black = () => fill("#000000", 23);  // what Windows sends first: drawn, but one flat colour
                    const draw = () => { fill("#285d8d", 23); fill("#ffffff", 5); window.drawnAt = performance.now(); };
                    window.events = []; window.stamps = [];
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
                            sendMouseState: (state, scaled) => { stamps.push(performance.now()); events.push([state.args, scaled]); },
                            sendKeyEvent: (down, sym) => {
                                if (down !== 0 && down !== 1) throw Error("Guacamole expects numeric key state");
                                stamps.push(performance.now());
                                events.push([down, sym]);
                            }
                        }
                    };
                    PAINT
                    </script>""".replace("PAINT", paint)
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
        yield f"http://127.0.0.1:{server.server_port}{LINK}"
    finally:
        guac.close(SID)
        server.shutdown()
        server.server_close()
        thread.join()


def drawn_pixels(png):
    """How many pixels of a screenshot are not transparent, counted by the browser that made it."""
    import base64

    return guac._worker.submit(lambda: guac._pages[SID][1].evaluate("""async data => {
        const image = new Image();
        image.src = "data:image/png;base64," + data;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width; canvas.height = image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        let drawn = 0;
        for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) drawn++;
        return drawn;
    }""", base64.b64encode(png).decode())).result()


def test_browser_drives_exact_guacamole_frame(monkeypatch):
    """Run with the guac extra and installed Chromium."""
    monkeypatch.setattr(guac, "INPUT_SETTLE_SECONDS", 0)
    with kasm_page("draw();") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            assert desktop.screenshot().startswith(b"\x89PNG\r\n\x1a\n")
            assert (desktop.width, desktop.height) == (23, 19)
            assert desktop.blank is False
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
        assert events[12] == [1, 0xffe1]
        assert events[15] == [0, 0xffe1]
        assert events[-6:] == [[1, 233], [0, 233], [1, 0xffe3],
                              [1, 0x61], [0, 0x61], [0, 0xffe3]]


def test_screenshot_waits_for_the_first_frame_after_attaching():
    """Seen live on 2026-11-27: right after the browser attaches, Guacamole's display has its size
    but nothing drawn, and the screenshot was a fully transparent image."""
    with kasm_page("setTimeout(draw, 700);") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            png = desktop.screenshot()
            assert drawn_pixels(png) == 23 * 19
            assert desktop.blank is False


@pytest.mark.parametrize("paint", ["", "black();"])
def test_screenshot_says_blank_when_no_frame_ever_arrives(monkeypatch, paint):
    monkeypatch.setattr(guac, "FIRST_FRAME_SECONDS", 1)
    with kasm_page(paint) as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            png = desktop.screenshot()  # still returns what there is, after the wait
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert desktop.blank is True


def test_screenshot_waits_past_the_flat_black_frame_windows_sends_first():
    """Seen live on 2026-11-27: the first frame of a Windows RDP session is solid black, then the
    desktop follows. One flat colour is not a desktop yet, as for the VNC transport."""
    with kasm_page("black(); setTimeout(draw, 700);") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            desktop.screenshot()
            assert desktop.blank is False
            assert desktop._eval("() => window.drawnAt > 0")


def test_a_flat_screen_later_on_is_reported_without_waiting(monkeypatch):
    import time

    with kasm_page("draw(); setTimeout(black, 300);") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            desktop.screenshot()  # the desktop has been seen once
            time.sleep(0.5)
            began = time.monotonic()
            desktop.screenshot()
            assert desktop.blank is True
            assert time.monotonic() - began < 1  # only a new attach waits for its first frame


def test_input_right_after_attaching_waits_until_the_desktop_can_take_it(monkeypatch):
    """Seen live on 2026-11-27: Windows silently dropped everything typed in the first second after
    the browser attached (4 of 4 at 0 to 1 s), and took everything typed 3 s later."""
    import time

    monkeypatch.setattr(guac, "INPUT_SETTLE_SECONDS", 0.6)
    with kasm_page("black(); setTimeout(draw, 500);") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            desktop.type("a")  # no screenshot first
            first, drawn = desktop._eval("() => [window.stamps[0], window.drawnAt]")
            assert first - drawn >= 550  # ms after the first real frame
            began = time.monotonic()
            desktop.click(2, 3, 1, 1)
            desktop.type("b")
            assert time.monotonic() - began < 0.4  # only the first input after an attach waits


def test_look_says_when_a_guacamole_desktop_has_drawn_nothing(monkeypatch):
    class Empty:
        width, height, blank = 1280, 800, True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def screenshot(self):
            return b"\x89PNG\r\n\x1a\nempty"

    tools._SESSIONS[SID] = {"transport": "guac", "username": "alice", "workspace": "Windows", "started": 1}
    monkeypatch.setattr(tools, "_session", lambda kid: Empty())
    monkeypatch.setattr(tools.time, "sleep", lambda *_: None)
    assert "Screen is blank" in tools.kasm_look({"session_id": SID})["content"][0]["text"]


def test_key_events_are_paced_so_windows_keeps_shift_and_unicode(monkeypatch):
    """Measured in Notepad on Windows 11, 2026-11-27: with no pause between key events "<ok> (a)"
    arrived as ",ok. 9a0" and the text was cut short; 5 ms still lost characters; 20 ms was exact."""
    monkeypatch.setattr(guac, "INPUT_SETTLE_SECONDS", 0)
    assert guac.KEY_DELAY_MS == 20
    with kasm_page("draw();") as url:
        with guac.Session(SID, url, PATH, True) as desktop:
            desktop.type("<é")
            desktop.combo([0xffe3, 0x61])
            stamps, events = desktop._eval("() => [window.stamps, window.events]")
    assert events == [[1, 0x3c], [0, 0x3c], [1, 0xe9], [0, 0xe9], [1, 0xffe3], [1, 0x61], [0, 0x61], [0, 0xffe3]]
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    assert min(gaps) >= guac.KEY_DELAY_MS - 1


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
