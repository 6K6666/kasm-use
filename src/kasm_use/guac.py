"""Drive the Kasm 1.19 Guacamole display from its authenticated browser client."""

import base64
from concurrent.futures import ThreadPoolExecutor
import struct
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
        return frame
    except Exception:
        context.close()
        raise


def close(kid):
    def close_context():
        entry = _pages.pop(kid, None)
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

    def screenshot(self):
        data = self._eval(
            "() => app.guac.getDisplay().flatten().toDataURL('image/png').split(',')[1]"
        )
        png = base64.b64decode(data)
        self.width, self.height = struct.unpack(">II", png[16:24])
        return png

    def _mouse(self, states, *, shift=False):
        self._eval("""({states, shift}) => {
            if (shift) app.guac.sendKeyEvent(true, 0xffe1);
            try {
                for (const [x, y, button] of states) {
                    app.guac.sendMouseState(new Guacamole.Mouse.State(
                        x, y, button === 1, button === 2, button === 3,
                        button === 4, button === 5), false);
                }
                app.mouse.lastActiveAt = Date.now();
            } finally {
                if (shift) app.guac.sendKeyEvent(false, 0xffe1);
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
        self._eval("""states => {
            for (const [sym, down] of states) app.guac.sendKeyEvent(down, sym);
            app.keyboard.lastActiveAt = Date.now();
        }""", states)

    def combo(self, syms):
        self._keys([(sym, True) for sym in syms] + [(sym, False) for sym in reversed(syms)])

    def type(self, text):
        self._keys([(char_keysym(ch), down) for ch in text for down in (True, False)])
