"""Unit tests for loki.core using temp trees and a stubbed ImageToPAA runner (no real tool needed)."""

import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

from loki import core
from paa_helpers import paa_bytes


def mk(path: Path, size=(256, 256), mode="RGB", color=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color or ((10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30))).save(path)
    return path


def set_mtime(path: Path, t: float):
    os.utime(path, (t, t))


def check_all(folder, max_size=None):
    """Status of every png in a folder, by file name (the app lists files, core checks one at a time)."""
    return {p.name: core.check_png(p, max_size) for p in sorted(Path(folder).glob("*.png"))}


def ok_runner(tool, src, dst):
    dst.write_bytes(paa_bytes(*Image.open(src).size))


# --- one file ---
def test_file_key_is_the_one_identity_rule(tmp_path):
    p = tmp_path / "Tex" / "A.png"
    assert core.file_key(p) == os.path.normcase(os.path.abspath(p))
    assert core.file_key(str(p)) == core.file_key(p)
    if os.name == "nt":
        assert core.file_key(tmp_path / "tex" / "a.PNG") == core.file_key(p)
        assert core.file_key(str(p).replace("\\", "/")) == core.file_key(p)


def test_missing_file_is_a_status_not_an_error(tmp_path):
    e = core.check_png(tmp_path / "gone.png")
    assert (e.status, e.reason, e.size) == ("missing", None, None) and e.path.is_absolute()
    mk(tmp_path / "gone.png")  # it returns: a normal status again
    assert core.check_png(tmp_path / "gone.png").status == "needs_converting"
    assert not hasattr(e, "rel") and not hasattr(core, "scan")


def test_status_up_to_date_vs_needs_converting(tmp_path):
    png = mk(tmp_path / "fresh.png")
    paa = tmp_path / "fresh.paa"
    paa.write_bytes(paa_bytes(256, 256))
    set_mtime(png, 1000)
    set_mtime(paa, 1000)  # equal mtime counts as up to date
    stale = mk(tmp_path / "stale.png")
    (tmp_path / "stale.paa").write_bytes(b"x")
    set_mtime(tmp_path / "stale.paa", 1000)
    set_mtime(stale, 2000)
    mk(tmp_path / "none.png")
    (tmp_path / "upper.PAA").write_bytes(paa_bytes(256, 256))
    up = mk(tmp_path / "upper.png")
    set_mtime(up, 1000)
    set_mtime(tmp_path / "upper.PAA", 1500)
    got = check_all(tmp_path)
    assert got["fresh.png"].status == "up_to_date"
    assert got["stale.png"].status == "needs_converting"
    assert got["none.png"].status == "needs_converting"
    assert got["upper.png"].status == "up_to_date"


def test_unreadable_png_flagged(tmp_path):
    (tmp_path / "bad.png").write_bytes(b"junk")
    e = core.check_png(tmp_path / "bad.png")
    assert e.status == "flagged" and e.reason.startswith("cannot read:") and e.size is None


# --- power of two ---
def test_non_pow2_flagged_with_reason(tmp_path):
    mk(tmp_path / "odd.png", (1000, 700))
    mk(tmp_path / "ok.png", (1024, 512))
    (tmp_path / "odd.paa").write_bytes(b"x")  # flagged wins over a fresh paa
    got = check_all(tmp_path)
    assert got["odd.png"].status == "flagged"
    assert got["odd.png"].reason == "1000x700: sides must be powers of two (e.g. 1024x512) or set an output size"
    assert got["odd.png"].size == (1000, 700)
    assert got["ok.png"].status == "needs_converting"


# --- max size ---
@pytest.mark.parametrize(
    "w,h,cap,want",
    [
        (4096, 2048, 1024, (1024, 512)),
        (512, 512, 1024, (512, 512)),
        (256, 4096, 256, (16, 256)),
        (4096, 512, 1024, (1024, 128)),
        (4096, 1, 256, (256, 1)),
        (2048, 1024, None, (2048, 1024)),
        (4096, 64, 512, (512, 8)),
        # ImageToPAA's measured 4096 output limit, also with max size off; a lower cap still wins
        (8192, 8192, None, (4096, 4096)),
        (8192, 2048, None, (4096, 1024)),
        (2048, 8192, None, (1024, 4096)),
        (8192, 4096, None, (4096, 2048)),
        (8192, 8192, 8192, (4096, 4096)),
        (8192, 8192, 1024, (1024, 1024)),
    ],
)
def test_target_size(w, h, cap, want):
    assert core.target_size(w, h, cap) == want


def test_resize_keeps_source_alpha_and_cleans_up(tmp_path, monkeypatch):
    png = mk(tmp_path / "big.png", (4096, 2048), "RGBA")
    h0 = hashlib.sha256(png.read_bytes()).hexdigest()
    set_mtime(png, 1234)
    seen = []

    def run(tool, src, dst):
        assert src != png and src.parent != tmp_path  # temp copy, not beside the source
        with Image.open(src) as im:
            seen.append((im.size, im.mode, im.getpixel((0, 0))))
        return ok_runner(tool, src, dst)

    monkeypatch.setattr(core, "_run_tool", run)
    res = core.convert(png, "tool", 1024)
    assert res.ok and res.error is None
    assert seen[0][0] == (1024, 512) and seen[0][1] == "RGBA" and seen[0][2][3] == 128
    assert hashlib.sha256(png.read_bytes()).hexdigest() == h0
    assert png.stat().st_mtime == 1234
    assert sorted(p.name for p in tmp_path.iterdir()) == ["big.paa", "big.png"]


def test_under_cap_passed_straight_through(tmp_path, monkeypatch):
    png = mk(tmp_path / "s.png", (256, 256))
    srcs = []

    def run(tool, src, dst):
        srcs.append(src)
        return ok_runner(tool, src, dst)

    monkeypatch.setattr(core, "_run_tool", run)
    assert core.convert(png, "tool", 1024).ok
    assert srcs == [png]


# --- discovery ---
def fake_steam(tmp_path):
    root = tmp_path / "Steam"
    lib2 = tmp_path / "Lib2"
    (root / "steamapps").mkdir(parents=True)

    def esc(p):
        return str(p).replace("\\", "\\\\")

    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n "0"\n {\n  "path"\t\t"' + esc(root) + '"\n }\n'
        ' "1"\n {\n  "path"\t\t"' + esc(lib2) + '"\n }\n}\n'
    )
    tool = lib2 / "steamapps" / "common" / "Arma 3 Tools" / "ImageToPAA" / "ImageToPAA.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"MZ")
    return root, tool


def test_find_in_secondary_library(tmp_path):
    root, tool = fake_steam(tmp_path)
    assert core.find_imagetopaa(steam_roots=lambda: [root]) == tool


def test_find_override_and_errors(tmp_path):
    _, tool = fake_steam(tmp_path)
    assert core.find_imagetopaa(tool, steam_roots=list) == tool
    with pytest.raises(core.ToolNotFound, match="nope.exe"):
        core.find_imagetopaa(tmp_path / "nope.exe", steam_roots=list)
    with pytest.raises(core.ToolNotFound, match="Steam was not detected"):
        core.find_imagetopaa(steam_roots=list)
    empty = tmp_path / "Empty"
    empty.mkdir()
    with pytest.raises(core.ToolNotFound, match="any Steam library"):
        core.find_imagetopaa(steam_roots=lambda: [empty])


def test_malformed_vdf_does_not_crash(tmp_path):
    root = tmp_path / "S"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_bytes(b'\xff\xfe{{ "path" ')
    with pytest.raises(core.ToolNotFound):
        core.find_imagetopaa(steam_roots=lambda: [root])


# --- failure paths (real tool in test_core_e2e.py) ---
def test_convert_failure_returns_error_and_keeps_old_paa(tmp_path, monkeypatch):
    png = mk(tmp_path / "f.png")
    paa = tmp_path / "f.paa"
    paa.write_bytes(b"old")

    def boom(tool, src, dst):
        dst.write_bytes(b"half")
        return "ImageToPAA failed (exit 1): nope"

    monkeypatch.setattr(core, "_run_tool", boom)
    res = core.convert(png, "tool")
    assert not res.ok and "exit 1" in res.error
    assert paa.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.paa", "f.png"]


def _denied(winerror=5):
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror
    return exc


def test_convert_rides_out_a_brief_hold_on_the_old_paa(tmp_path, monkeypatch):
    """A reader holding the old .paa makes os.replace fail with winerror 5 for a few ms; not an error."""
    png = mk(tmp_path / "h.png")
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 3:
            raise _denied()
        real_replace(src, dst)

    monkeypatch.setattr(core, "_run_tool", lambda tool, src, dst: ok_runner(tool, src, dst))
    monkeypatch.setattr(core.os, "replace", flaky)
    monkeypatch.setattr(core, "_REPLACE_PAUSE", 0)
    res = core.convert(png, "tool")
    assert res.ok and len(calls) == 4 and (tmp_path / "h.paa").is_file()


def test_convert_persistent_denial_still_reports_error(tmp_path, monkeypatch):
    png = mk(tmp_path / "i.png")
    calls = []

    def always(src, dst):
        calls.append(1)
        raise _denied()

    monkeypatch.setattr(core, "_run_tool", lambda tool, src, dst: ok_runner(tool, src, dst))
    monkeypatch.setattr(core.os, "replace", always)
    monkeypatch.setattr(core, "_REPLACE_PAUSE", 0)
    res = core.convert(png, "tool")
    assert not res.ok and "PermissionError" in res.error and len(calls) == core._REPLACE_TRIES
    assert sorted(p.name for p in tmp_path.iterdir()) == ["i.png"]


def test_convert_missing_tool_and_missing_png_do_not_raise(tmp_path):
    png = mk(tmp_path / "g.png")
    res = core.convert(png, tmp_path / "no-tool.exe")
    assert not res.ok and res.error
    res = core.convert(tmp_path / "ghost.png", tmp_path / "no-tool.exe", 256)
    assert not res.ok and res.error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["g.png"]


# --- paa_size and size-aware status ---
def test_paa_size_parses_header_variants(tmp_path):
    p = tmp_path / "a.paa"
    for lzo in (True, False):
        p.write_bytes(paa_bytes(512, 256, lzo=lzo))
        assert core.paa_size(p) == (512, 256)
    p.write_bytes(paa_bytes(4, 2, palette=3))
    assert core.paa_size(p) == (4, 2)
    assert core.paa_size(tmp_path / "missing.paa") is None


def test_paa_size_never_raises_on_truncated_or_garbage(tmp_path):
    p = tmp_path / "t.paa"
    good = paa_bytes(512, 256)
    for cut in range(len(good) - 10):  # every truncation inside the header
        p.write_bytes(good[:cut])
        assert core.paa_size(p) is None, cut
    huge = b"\x01\xffGGAT" + b"CGVA" + b"\xff\xff\xff\xff" + b"x" * 64  # length field far past the file
    for data in (b"", b"\x01", huge, b"GGAT" * 3000, paa_bytes(0, 0)):
        p.write_bytes(data)
        assert core.paa_size(p) is None


def test_paa_size_reads_a_bounded_head(tmp_path):
    p = tmp_path / "big.paa"
    p.write_bytes(paa_bytes(64, 64) + bytes(10_000_000))
    assert core.paa_size(p) == (64, 64)


MAX = 256


def _with_paa(tmp_path, name, png_size, paa_data):
    png = mk(tmp_path / f"{name}.png", png_size)
    paa = tmp_path / f"{name}.paa"
    paa.write_bytes(paa_data)
    set_mtime(png, 1000)
    set_mtime(paa, 2000)
    return core.check_png(png, MAX)


def test_status_follows_max_size(tmp_path):
    e = _with_paa(tmp_path, "a", (512, 256), paa_bytes(512, 256))
    assert (e.status, e.reason) == (core.NEEDS_CONVERTING, "PAA is 512x256, max size wants 256x128")
    assert _with_paa(tmp_path, "b", (512, 256), paa_bytes(256, 128)).status == core.UP_TO_DATE
    assert _with_paa(tmp_path, "c", (256, 256), paa_bytes(256, 256)).status == core.UP_TO_DATE
    e = _with_paa(tmp_path, "d", (512, 256), paa_bytes(512, 256)[:20])  # truncated
    assert (e.status, e.reason) == (core.NEEDS_CONVERTING, "PAA unreadable")
    assert _with_paa(tmp_path, "e", (512, 256), b"junk").reason == "PAA unreadable"
    # raising the cap makes a smaller paa stale too: it must match the new target exactly
    png = tmp_path / "b.png"
    assert core.check_png(png, None).reason == "PAA is 256x128, max size wants 512x256"
    # flagged wins over everything
    odd = mk(tmp_path / "odd.png", (100, 60))
    (tmp_path / "odd.paa").write_bytes(b"junk")
    set_mtime(odd, 1000)
    assert core.check_png(odd, MAX).status == core.FLAGGED


def test_temp_files_keep_the_original_name_and_leave_nothing(tmp_path, monkeypatch):
    png = mk(tmp_path / "x_ca.png", (1024, 1024), "RGBA")
    seen = []

    def run(tool, src, dst):
        seen.append((src.name, dst.name, dst.parent != tmp_path))
        return ok_runner(tool, src, dst)

    monkeypatch.setattr(core, "_run_tool", run)
    assert core.convert(png, "tool", 512).ok
    assert seen == [("x_ca.png", "x_ca.paa", True)]  # suffix kept, output in a private folder
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x_ca.paa", "x_ca.png"]
    (tmp_path / "x_ca.paa").unlink()
    monkeypatch.setattr(core, "_run_tool", lambda t, s, d: (d.write_bytes(b"half"), "failed")[1])
    assert not core.convert(png, "tool", 512).ok
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x_ca.png"]


def test_paa_size_random_garbage_is_a_tuple_or_none(tmp_path):
    p = tmp_path / "g.paa"
    p.write_bytes(bytes(range(256)) * 40)  # any value is fine: a wrong size just reads as stale
    got = core.paa_size(p)
    assert got is None or (len(got) == 2 and all(0 < v <= 0x7FFF for v in got))


def test_load_limit_advice_matches_the_rust_message():
    """The Pillow bomb row and the Rust loader refusal give the same advice (one wording)."""
    src = (Path(__file__).parent.parent / "native" / "paa" / "src" / "img.rs").read_text(encoding="utf-8")
    tail = core.LOAD_LIMIT_ADVICE.removeprefix("too big to load ")
    assert "is too big to load " + tail.replace("16384", "{MAX_LOAD_SIDE}") in src


def test_check_png_bomb_row_gives_the_advice(tmp_path, monkeypatch):
    p = tmp_path / "a_co.png"
    Image.new("RGB", (4, 4)).save(p)

    def boom(*a, **k):
        raise Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(core.Image, "open", boom)
    e = core.check_png(p)
    assert e.reason == "cannot read: " + core.LOAD_LIMIT_ADVICE
