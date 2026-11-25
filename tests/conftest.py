"""Shared Tk helpers. A Tk that cannot start must FAIL the run (a silent skip hides GUI regressions);
set LOKI_ALLOW_TK_SKIP=1 to turn that failure into a skip (headless boxes).

Root cause of the old intermittent "Can't find a usable init.tcl" (task #7): pytest's default fd-level
capture (--capture=fd) made Tk init fail ~40% of runs on Windows (0 of 60 with -s or --capture=sys).
pyproject.toml therefore pins addopts --capture=sys. Do not pass --capture=fd."""

import gc
import os
import sys
import threading
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import build_native


def pytest_sessionstart(session):
    """Build src/loki/_paa.pyd when missing or older than a native source (once per session).

    A failed build ends the session with an error: a missing native module is never a silent skip.
    """
    try:
        build_native.ensure()
    except build_native.NativeBuildError as exc:
        pytest.exit(f"native build failed, no tests run:\n{exc}", returncode=2)


def tk_unavailable(exc: BaseException) -> None:
    """Call when Tk init raised: skip only if LOKI_ALLOW_TK_SKIP=1, else fail with the Tcl text."""
    if os.environ.get("LOKI_ALLOW_TK_SKIP") == "1":
        pytest.skip(f"no display for Tk (LOKI_ALLOW_TK_SKIP=1): {exc}")
    pytest.fail(
        f"Tk cannot initialise: {exc} (set LOKI_ALLOW_TK_SKIP=1 to skip on headless machines)", pytrace=False
    )


def make_tk() -> tk.Tk:
    """The one place a test Tk root is built; fails/skips through tk_unavailable."""
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        tk_unavailable(exc)
    r.withdraw()
    return r


_open_apps: list = []  # Apps built by a test with a window; closed in tk_root teardown


def track_app(app):
    """Register an App so tk_root teardown stops its watcher (else the join below waits out its timeout)."""
    _open_apps.append(app)
    return app


@pytest.fixture(autouse=True)
def outputs_store(tmp_path_factory, monkeypatch):
    """Loki's outputs.json (#60) lives in a temp folder for every test, never in the real %APPDATA%."""
    path = tmp_path_factory.mktemp("outputs") / "outputs.json"
    monkeypatch.setenv("LOKI_OUTPUTS", str(path))
    return path


@pytest.fixture
def tk_root():
    """One withdrawn Tk per test, destroyed on the main thread in teardown; loki worker threads joined first."""
    r = make_tk()
    yield r
    while _open_apps:
        _open_apps.pop().close()
    for t in threading.enumerate():
        if t.name.startswith("loki-") and t is not threading.current_thread():
            t.join(5.0)
    try:
        r.destroy()
    except tk.TclError:
        pass
    gc.collect()  # drop Tk objects here, on the main thread, not in a worker's GC
