"""Loki's record of the output settings it used (outputs.json, #60 decision 55): up-to-date through the CLI,
mtime/size staleness, pruning, a corrupt file, parallel writers. The store is a temp file (conftest, LOKI_OUTPUTS)."""

import json
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from loki import cli, core, outputs


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)


def png(path: Path, size=(256, 128)) -> Path:
    Image.new("RGBA", size, (200, 100, 50, 255)).save(path)
    return path


def skip_run(capsys, p, *opts) -> str:
    """One `--skip-up-to-date` CLI run; the first word of its line: converted or skipped."""
    assert cli.main([str(p), "--skip-up-to-date", *opts]) == 0
    return capsys.readouterr().out.split()[0]


BASE = ("--size", "256x128", "--pad-colour", "#102030", "--pad-align", "top")


def test_unchanged_second_run_skips_and_each_changed_setting_reconverts(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    assert skip_run(capsys, p, *BASE) == "converted"
    assert skip_run(capsys, p, *BASE) == "skipped"
    for change in (["--fit", "stretch"], ["--fit", "pad"], ["--pad-colour", "#102031"], ["--pad-align", "bottom"]):
        assert skip_run(capsys, p, *BASE, *change) == "converted", change
        assert skip_run(capsys, p, *BASE) == "converted"  # back to the base settings
        assert skip_run(capsys, p, *BASE) == "skipped"


def test_a_plain_run_rewrites_and_drops_the_record(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    assert skip_run(capsys, p, *BASE) == "converted"
    assert outputs.recorded(tmp_path / "a_co.paa") is not None
    assert skip_run(capsys, p) == "converted"  # a plain run never reuses a --size PAA, even at the same size
    assert outputs.recorded(tmp_path / "a_co.paa") is None
    assert skip_run(capsys, p) == "skipped"
    assert core.file_key(tmp_path / "a_co.paa") not in json.loads(outputs.store_path().read_text())["entries"]


def test_touching_the_paa_or_deleting_the_store_reconverts(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    paa = tmp_path / "a_co.paa"
    assert skip_run(capsys, p, *BASE) == "converted"
    st = paa.stat()
    os.utime(paa, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # mtime changed, size the same
    assert skip_run(capsys, p, *BASE) == "converted"
    assert skip_run(capsys, p, *BASE) == "skipped"
    outputs.store_path().unlink()
    assert skip_run(capsys, p, *BASE) == "converted"
    assert skip_run(capsys, p, *BASE) == "skipped"


def test_the_entry_format(tmp_path):
    p = png(tmp_path / "a_co.png")
    size = core.output_size("256x128", "contain", "#102030", "top")
    assert core.convert(p, None, None, core.LOKI, size=size).ok
    paa = tmp_path / "a_co.paa"
    entries = json.loads(outputs.store_path().read_text(encoding="utf8"))["entries"]
    assert entries == {
        core.file_key(paa): {"size": paa.stat().st_size, "mtime_ns": paa.stat().st_mtime_ns, "settings": size.key("a_co.png")}
    }


def test_a_write_prunes_the_entry_of_a_deleted_paa(tmp_path):
    a, b = (png(tmp_path / n) for n in ("a_co.png", "b_co.png"))
    size = core.output_size("256x128")
    assert core.convert(a, None, None, core.LOKI, size=size).ok and core.convert(b, None, None, core.LOKI, size=size).ok
    assert len(json.loads(outputs.store_path().read_text())["entries"]) == 2
    (tmp_path / "a_co.paa").unlink()
    outputs.remember(tmp_path / "b_co.paa", size.key("b_co.png"))  # any write prunes
    assert list(json.loads(outputs.store_path().read_text())["entries"]) == [core.file_key(tmp_path / "b_co.paa")]


@pytest.mark.parametrize("junk", ["", "{not json", "[1, 2]", '{"version": 1, "entries": 5}', '{"version": 9, "entries": {}}'])
def test_a_corrupt_store_is_empty_and_rewritten(tmp_path, capsys, junk):
    p = png(tmp_path / "a_co.png")
    outputs.store_path().write_text(junk)
    assert skip_run(capsys, p, *BASE) == "converted"  # no crash
    assert skip_run(capsys, p, *BASE) == "skipped"  # rewritten as a good file
    assert json.loads(outputs.store_path().read_text())["version"] == outputs.VERSION


def test_an_unreadable_store_is_empty(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    outputs.store_path().mkdir()  # a folder where the file should be: every read fails
    assert outputs.recorded(tmp_path / "a_co.paa") is None
    assert cli.main([str(p), *BASE]) == 0  # the write fails too: ignored, the PAA is still made
    assert (tmp_path / "a_co.paa").exists()


def test_parallel_writers_lose_no_entry(tmp_path):
    files = []
    for i in range(24):
        f = tmp_path / f"f{i}_co.paa"
        f.write_bytes(b"x" * (i + 1))
        files.append(f)
    errors = []

    def write(f):
        try:
            outputs.remember(f, f"size=8x8 fit=stretch #{f.name}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(json.loads(outputs.store_path().read_text())["entries"]) == sorted(core.file_key(f) for f in files)
    assert all(outputs.recorded(f) == f"size=8x8 fit=stretch #{f.name}" for f in files)


def test_parallel_conversions_with_jobs_record_every_file(tmp_path, capsys):
    pngs = [png(tmp_path / f"t{i}_co.png", (64, 64)) for i in range(8)]
    assert cli.main([*(str(p) for p in pngs), "--size", "32x32", "--jobs", "4"]) == 0
    capsys.readouterr()
    assert len(json.loads(outputs.store_path().read_text())["entries"]) == 8
