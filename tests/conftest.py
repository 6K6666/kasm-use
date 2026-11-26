import pytest

from kasm_use import tools

REAL_WAIT_VISIBLE = getattr(tools, "_wait_visible", None)
REAL_WEB = getattr(tools, "_web", None)


@pytest.fixture(autouse=True)
def no_desktop_probe(monkeypatch):
    """kasm_start probes the new desktop over VNC; offline tests have no desktop to probe."""
    monkeypatch.setattr(tools, "_wait_visible", lambda kid, seconds: True, raising=False)
    monkeypatch.setattr(tools, "_web", lambda endpoint, **payload: {}, raising=False)  # no Kasm to call
    getattr(tools, "_KEPT", {}).clear()
    getattr(tools, "_KEEPALIVE_WARNED", set()).clear()
    for name in ("_VISIBLE", "_PERSISTED", "_STOPPED", "_MINE"):  # per-process memory; each test is a fresh process
        getattr(tools, name, set()).clear()
