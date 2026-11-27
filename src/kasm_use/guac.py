"""Drive the Kasm 1.19 Guacamole display from its authenticated browser client."""

import base64
from concurrent.futures import ThreadPoolExecutor
import struct
import time
from urllib.parse import urlsplit

from .vnc import char_keysym

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kasm-guac")
_playwright = None
_browser = None
_pages = {}
_SIZE = "() => [app.guac.getDisplay().getWidth(), app.guac.getDisplay().getHeight()]"
class ConnectionError(RuntimeError):
    """The browser could not attach to the Kasm desktop."""


_READY = """() => typeof app !== 'undefined' && app.isConnected() &&
    app.guac.getDisplay().getWidth() > 0 && app.guac.getDisplay().getHeight() > 0
"""


# Right after the browser attaches, the display has its size but no frame yet (flattening it gives
# a fully transparent image), and Windows then sends one solid black frame before the desktop.
# A frame of one flat colour is "not drawn", as for the VNC transport. A new attach waits this long
# for its first drawn frame; afterwards a flat screen is reported at once.
FIRST_FRAME_SECONDS = 8
# Windows drops input sent in the first moments after an RDP connection shows its desktop (measured
# 2026-11-27: everything typed within 1 s was lost, everything typed 3 s later arrived). The first
# input after an attach waits until this long after the first drawn frame.
INPUT_SETTLE_SECONDS = 3
# Pause after every key event. Sent in one burst, Windows applications that read the keyboard
# state late lose Shift and Unicode characters (Notepad, measured 2026-11-27: 0 and 5 ms corrupt
# the text, 20 ms is exact). About 25 characters a second.
KEY_DELAY_MS = 20
_fresh = set()  # sessions whose browser attached and has not sent input yet
_drawn_at = {}  # for those: when the first drawn frame was seen
_FRAME = """async limit => {
    const display = app.guac.getDisplay(), deadline = Date.now() + limit;
    for (;;) {
        const canvas = display.flatten();
        const p = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        let drawn = false;
        for (let i = 4; i < p.length && !drawn; i += 4)
            drawn = p[i] !== p[0] || p[i + 1] !== p[1] || p[i + 2] !== p[2] || p[i + 3] !== p[3];
        if (drawn || Date.now() >= deadline) return [drawn, canvas.toDataURL('image/png').split(',')[1]];
        await new Promise(resolve => setTimeout(resolve, 200));
    }
}"""


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.netloc


def _start_browser():
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return
    from playwright.sync_api import sync_playwright

    if _playwright is None:
        _playwright = sync_playwright().start()
    _pages.clear()
    _browser = _playwright.chromium.launch()


def require_browser():
    try:
        _worker.submit(_start_browser).result()
    except Exception:
        raise RuntimeError(
            "Windows Server Workspaces require the kasm-use guac extra and Playwright Chromium."
        ) from None


def _get_frame(kid, url, path, verify_tls):
    _start_browser()
    origin = _origin(url)
    target = f"/{path.strip('/')}/vnc.html"

    def matches(address):
        return _origin(address) == origin and urlsplit(address).path == target

    entry = _pages.get(kid)
    if entry is not None:
        context, frame = entry
        try:
            if matches(frame.url) and frame.evaluate(_READY):
                return frame
        except Exception:
            pass
        _pages.pop(kid)
        context.close()

    context = _browser.new_context(
        ignore_https_errors=not verify_tls, service_workers="block",
        viewport={"width": 1280, "height": 800},
    )
    try:
        # The Kasm login link is sensitive. Keep the context on its Kasm origin.
        def route_request(route):
            if _origin(route.request.url) == origin:
                route.continue_()
            else:
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(url, wait_until="domcontentloaded")
        frame = page.locator("#iframe-id").element_handle().content_frame()
        frame.wait_for_url(matches)
        frame.wait_for_function(_READY)
        _pages[kid] = context, frame
        _fresh.add(kid)
        _drawn_at.pop(kid, None)
        return frame
    except Exception:
        context.close()
        raise


def close(kid):
    def close_context():
        entry = _pages.pop(kid, None)
        _fresh.discard(kid)
        _drawn_at.pop(kid, None)
        if entry is not None:
            entry[0].close()

    _worker.submit(close_context).result()


def _evaluate(kid, url, path, verify_tls, script, data):
    frame = _get_frame(kid, url, path, verify_tls)
    return frame.evaluate(script, data)


class Session:
    """Expose the same screenshot and input operations as the VNC transport."""

    def __init__(self, kid, url, path, verify_tls):
        self.target = kid, url, path, verify_tls
        self.width, self.height = self._eval(_SIZE)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        # The browser stays connected until Kasm ends the session.
        return False

    def _eval(self, script, data=None):
        try:
            return _worker.submit(_evaluate, *self.target, script, data).result()
        except Exception:
            # Playwright errors can include the session-token URL.
            raise ConnectionError("Kasm Guacamole desktop is disconnected or awaiting RDP login.") from None

    def _frame(self):
        kid = self.target[0]
        waiting = kid in _fresh and kid not in _drawn_at
        drawn, data = self._eval(_FRAME, FIRST_FRAME_SECONDS * 1000 if waiting else 0)
        if drawn and kid in _fresh:
            _drawn_at.setdefault(kid, time.monotonic())
        return drawn, data

    def screenshot(self):
        drawn, data = self._frame()
        self.blank = not drawn  # kasm_look says so, as for a VNC desktop that has drawn nothing
        png = base64.b64decode(data)
        self.width, self.height = struct.unpack(">II", png[16:24])
        return png

    def _settle(self):
        """Hold the first input after an attach until the desktop can take it."""
        kid = self.target[0]
        if kid not in _fresh:
            return
        if kid not in _drawn_at:
            self._frame()
        if kid in _drawn_at:  # a desktop that never drew gets the input anyway, and waits again next time
            time.sleep(max(0.0, _drawn_at[kid] + INPUT_SETTLE_SECONDS - time.monotonic()))
            _fresh.discard(kid)

    def _mouse(self, states, *, shift=False):
        self._settle()
        self._eval("""({states, shift}) => {
            if (shift) app.guac.sendKeyEvent(1, 0xffe1);
            try {
                for (const [x, y, button] of states) {
                    app.guac.sendMouseState(new Guacamole.Mouse.State(
                        x, y, button === 1, button === 2, button === 3,
                        button === 4, button === 5), false);
                }
                app.mouse.lastActiveAt = Date.now();
            } finally {
                if (shift) app.guac.sendKeyEvent(0, 0xffe1);
            }
        }""", {"states": states, "shift": shift})

    def click(self, x, y, button, count):
        self._mouse([(x, y, state) for _ in range(count) for state in (button, 0)])

    def drag(self, x0, y0, x1, y1, button):
        self._mouse([(x0, y0, 0), (x0, y0, button), (x1, y1, button), (x1, y1, 0)])

    def scroll(self, x, y, direction, amount):
        # Guacamole has only vertical wheel buttons. Windows apps commonly use Shift+wheel.
        button = 4 if direction in ("up", "left") else 5
        self._mouse([(x, y, state) for _ in range(amount) for state in (button, 0)],
                    shift=direction in ("left", "right"))

    def _keys(self, states):
        self._settle()
        self._eval("""async ({states, delay}) => {
            for (const [sym, down] of states) {
                app.guac.sendKeyEvent(down, sym);
                await new Promise(resolve => setTimeout(resolve, delay));
            }
            app.keyboard.lastActiveAt = Date.now();
        }""", {"states": states, "delay": KEY_DELAY_MS})

    def combo(self, syms):
        self._keys([(sym, 1) for sym in syms] + [(sym, 0) for sym in reversed(syms)])

    def type(self, text):
        self._keys([(char_keysym(ch), down) for ch in text for down in (1, 0)])
