"""An unreadable outputs.json is retried and never taken as empty (a write must not drop the other entries)."""

import os
import threading
from pathlib import Path

import pytest

from loki import cli, outputs
from test_outputs import hold_open, no_tool, png  # noqa: F401  (no_tool is an autouse fixture)

winonly = pytest.mark.skipif(os.name != "nt", reason="share-mode locks are a Windows thing")


def _two_entries(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "a_co.paa", tmp_path / "b_co.paa"
    a.write_bytes(b"x")
    b.write_bytes(b"yy")
    outputs.remember(a, "first")
    return a, b


@winonly
def test_remember_rides_out_a_store_held_without_read_share(tmp_path):
    a, b = _two_entries(tmp_path)
    close = hold_open(outputs.store_path(), share=0)  # no read share: every read fails until released
    threading.Timer(0.6, close).start()
    outputs.remember(b, "second")
    assert outputs.recorded(a) == "first"  # the old entry survived
    assert outputs.recorded(b) == "second"


@winonly
def test_a_store_held_past_the_limit_is_not_rewritten(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(outputs, "_WRITE_WAIT", 0.4)
    a, b = _two_entries(tmp_path)
    close = hold_open(outputs.store_path(), share=0)
    try:
        with caplog.at_level("WARNING", logger="loki.outputs"):
            outputs.remember(b, "second")
    finally:
        close()
    assert "could not record output settings" in caplog.text
    assert outputs.recorded(a) == "first"  # still there
    assert outputs.recorded(b) is None  # no write happened


@winonly
def test_conversion_ok_and_entries_kept_when_the_store_is_unreadable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(outputs, "_WRITE_WAIT", 0.3)
    p = png(tmp_path / "a_co.png")
    q = png(tmp_path / "b_co.png")
    assert cli.main([str(p), "--size", "256x128"]) == 0
    capsys.readouterr()
    close = hold_open(outputs.store_path(), share=0)
    try:
        assert cli.main([str(q), "--size", "256x128"]) == 0
    finally:
        close()
    assert (tmp_path / "b_co.paa").exists()
    assert outputs.recorded(tmp_path / "a_co.paa") is not None
