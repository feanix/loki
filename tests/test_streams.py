"""_ensure_streams: devnull streams must accept the cell glyphs (windowed exe)."""

import sys

from loki.__main__ import _ensure_streams


def test_devnull_streams_accept_glyphs(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    _ensure_streams()
    out, err = sys.stdout, sys.stderr
    try:
        print("▾ ☐ ☑")
        print("▾ ☐ ☑", file=sys.stderr)
    finally:
        out.close()
        err.close()
