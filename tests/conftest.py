import pytest

from kasm_use import tools

REAL_WAIT_VISIBLE = getattr(tools, "_wait_visible", None)


@pytest.fixture(autouse=True)
def no_desktop_probe(monkeypatch):
    """kasm_start probes the new desktop over VNC; offline tests have no desktop to probe."""
    monkeypatch.setattr(tools, "_wait_visible", lambda kid, seconds: True, raising=False)
    for name in ("_VISIBLE", "_PERSISTED", "_STOPPED"):  # per-process memory; each test is a fresh process
        getattr(tools, name, set()).clear()
