"""TGA and JPG sources next to PNG: decoding variants, equality with a PNG of the same pixels, two sources for
one PAA, folder/glob pickup in the CLI, the window drop and an Auto watch run on a TGA.

TGA fixtures are written byte by byte here (not by Pillow's own writer) so the origin, the BGR order and the
16-bit layout are checked against what the format says. Real Loki encoder throughout; ImageToPAA is absent
except where a test stands in a fake one for the hand-over copy."""

import os
import struct
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import read_when_free, track_app
from loki import app as app_mod
from loki import cli, core


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)


# --- a TGA writer ------------------------------------------------------------------------------------------------


def write_tga(path: Path, rows, *, depth=24, origin_top=True, rle=False, cmap=None, grey=False, alpha_bits=0) -> Path:
    """`rows`: top-to-bottom rows of pixels. 8-bit grey/index: ints; 16: ints (1555); 24/32: (r, g, b[, a])."""
    h, w = len(rows), len(rows[0])
    body = b""
    for row in rows if origin_top else rows[::-1]:
        data = []
        for px in row:
            if depth == 16:
                data.append(struct.pack("<H", px))
            elif depth == 8:
                data.append(bytes([px]))
            else:
                r, g, b, *a = px
                data.append(bytes([b, g, r, *a]))
        if not rle:
            body += b"".join(data)
            continue
        i = 0  # packets never cross a scanline
        while i < len(data):
            n = 1
            while i + n < len(data) and n < 128 and data[i + n] == data[i]:
                n += 1
            body += (bytes([0x80 | (n - 1)]) if n > 1 else bytes([0])) + data[i]
            i += n
    cm = b"".join(bytes([b, g, r]) for r, g, b in cmap) if cmap else b""
    kind = 3 if grey else 1 if cmap else 2
    head = struct.pack(
        "<BBBHHBHHHHBB", 0, 1 if cmap else 0, kind + (8 if rle else 0), 0, len(cmap or ()), 24 if cmap else 0,
        0, 0, w, h, depth, (0x20 if origin_top else 0) | alpha_bits,
    )  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + cm + body)
    return path


def decoded(path: Path) -> tuple[str, list]:
    out = path.parent / "_decoded" / "x.png"
    core.decode_to_png(path, out)
    with Image.open(out) as im:
        return im.mode, list(im.getdata())


RGB = [[(10 * (x + 4 * y), 200 - 7 * x, 3 * y + 5) for x in range(4)] for y in range(4)]
RGBA = [[(*px, 40 * x + 15) for x, px in enumerate(row)] for row in RGB]
GREY = [[16 * (x + 4 * y) for x in range(4)] for y in range(4)]


def flat(rows):
    return [px for row in rows for px in row]


@pytest.mark.parametrize("origin_top", [True, False], ids=["top-left", "bottom-left"])
@pytest.mark.parametrize("rle", [False, True], ids=["raw", "rle"])
def test_tga_variants_decode_to_the_same_picture(tmp_path, origin_top, rle):
    kw = {"origin_top": origin_top, "rle": rle}
    assert decoded(write_tga(tmp_path / "rgb.tga", RGB, depth=24, **kw)) == ("RGB", flat(RGB))
    assert decoded(write_tga(tmp_path / "rgba.tga", RGBA, depth=32, alpha_bits=8, **kw)) == ("RGBA", flat(RGBA))
    mode, px = decoded(write_tga(tmp_path / "grey.tga", GREY, depth=8, grey=True, **kw))
    assert mode == "RGB" and px == [(v, v, v) for v in flat(GREY)]
    pal = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (9, 8, 7)]
    idx = [[(x + y) % 4 for x in range(4)] for y in range(4)]
    mode, px = decoded(write_tga(tmp_path / "cmap.tga", idx, depth=8, cmap=pal, **kw))
    assert mode == "RGB" and px == [pal[i] for i in flat(idx)]


@pytest.mark.parametrize("origin_top", [True, False], ids=["top-left", "bottom-left"])
def test_tga_16_bit_1555_alpha_bit_means_opaque(tmp_path, origin_top):
    red, green = 0x7C00, 0x03E0  # 5-bit channels at 0 or 31 so the expansion scheme does not matter
    rows = [[red | 0x8000, green, 0x8000, 0], [green | 0x8000] * 4, [red] * 4, [0x8000] * 4]
    mode, px = decoded(write_tga(tmp_path / "a.tga", rows, depth=16, origin_top=origin_top, alpha_bits=1))
    assert mode == "RGBA"
    assert px[:4] == [(255, 0, 0, 255), (0, 255, 0, 0), (0, 0, 0, 255), (0, 0, 0, 0)]
    mode, px = decoded(write_tga(tmp_path / "b.tga", rows, depth=16, origin_top=origin_top, alpha_bits=0))
    assert mode == "RGB" and px[:4] == [(255, 0, 0), (0, 255, 0), (0, 0, 0), (0, 0, 0)]  # no alpha bits: opaque


def test_bottom_left_origin_is_not_upside_down(tmp_path):
    top = [[(255, 0, 0)] * 4] + [[(0, 0, 255)] * 4] * 3
    for origin_top in (True, False):
        _, px = decoded(write_tga(tmp_path / f"o{origin_top}.tga", top, origin_top=origin_top))
        assert px[:4] == [(255, 0, 0)] * 4 and px[-4:] == [(0, 0, 255)] * 4


def test_tga_the_loader_cannot_read_is_refused_in_plain_words(tmp_path):
    pal = [(1, 2, 3, 4), (5, 6, 7, 8)]  # a 32-bit colour map
    head = struct.pack("<BBBHHBHHHHBB", 0, 1, 1, 0, 2, 32, 0, 0, 2, 1, 8, 0x20)
    p = tmp_path / "wall_co.tga"
    p.write_bytes(head + b"".join(bytes(q) for q in pal) + b"\x00\x01")
    r = core.convert(p, None, converter=core.LOKI)
    assert not r.ok and "unsupported TGA variant" in r.error and not p.with_suffix(".paa").exists()
    short = tmp_path / "bad_co.tga"
    short.write_bytes(b"\x00" * 10)
    assert core.check_png(short).status == core.FLAGGED


def test_a_png_named_tga_or_jpg_is_refused_naming_the_problem(tmp_path):
    Image.new("RGB", (16, 16)).save(tmp_path / "a_co.png")
    for ext, label in ((".tga", "TGA"), (".jpg", "JPG")):
        p = tmp_path / f"a_co{ext}"
        p.write_bytes((tmp_path / "a_co.png").read_bytes())
        e = core.check_png(p)
        assert e.status == core.FLAGGED and f"not a valid {label}" in e.reason and "PNG" in e.reason
        assert not core.convert(p, None, converter=core.LOKI).ok


# --- JPG ---------------------------------------------------------------------------------------------------------


def jpg(path: Path, mode="RGB", **kw) -> Path:
    band = Image.effect_noise((32, 32), 50)
    im = Image.merge("RGB", (band, band.transpose(Image.Transpose.FLIP_LEFT_RIGHT), band.transpose(Image.Transpose.ROTATE_90)))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.convert(mode).save(path, "JPEG", **kw)
    return path


@pytest.mark.parametrize(
    ("mode", "kw"),
    [("RGB", {}), ("RGB", {"progressive": True}), ("L", {}), ("L", {"progressive": True})],
    ids=["baseline", "progressive", "grey", "grey-progressive"],
)
@pytest.mark.parametrize("ext", [".jpg", ".JPEG"])
def test_jpg_equals_its_decoded_pixels_saved_as_png(tmp_path, mode, kw, ext):
    src = jpg(tmp_path / "j" / f"wall_co{ext}", mode, **kw)
    twin = tmp_path / "p" / "wall_co.png"
    twin.parent.mkdir()
    with Image.open(src) as im:
        im.convert("RGB").save(twin)
    assert core.check_png(src).status == core.NEEDS_CONVERTING
    assert core.convert(src, None, converter=core.LOKI).ok and core.convert(twin, None, converter=core.LOKI).ok
    assert (tmp_path / "j" / "wall_co.paa").read_bytes() == (tmp_path / "p" / "wall_co.paa").read_bytes()


def test_cmyk_jpg_is_refused_clearly(tmp_path):
    p = jpg(tmp_path / "c_co.jpg", "CMYK")
    e = core.check_png(p)
    assert e.status == core.FLAGGED and "CMYK JPG is not supported" in e.reason
    r = core.convert(p, None, converter=core.LOKI)
    assert not r.ok and "CMYK JPG is not supported" in r.error and not p.with_suffix(".paa").exists()


# --- equality with PNG, both routes ------------------------------------------------------------------------------

NAMES = ["wall_co", "wall_ca", "wall_nohq", "wall_smdi", "wall"]


def noisy(size, alpha):
    base = Image.effect_noise(size, 60).convert("RGB")
    if not alpha:
        return base
    a = Image.effect_noise(size, 80).convert("L")
    return Image.merge("RGBA", (*base.split()[:3], a))


@pytest.mark.parametrize("alpha", [False, True], ids=["rgb", "rgba"])
@pytest.mark.parametrize("origin_top", [True, False], ids=["top-left", "bottom-left"])
@pytest.mark.parametrize("rle", [False, True], ids=["raw", "rle"])
def test_tga_paa_is_byte_identical_to_the_png_paa(tmp_path, alpha, origin_top, rle):
    im = noisy((32, 16), alpha)
    px = list(im.getdata())
    rows = [px[y * 32:(y + 1) * 32] for y in range(16)]
    for name in NAMES:
        t = tmp_path / "t"
        p = tmp_path / "p"
        p.mkdir(exist_ok=True)
        write_tga(t / f"{name}.tga", rows, depth=32 if alpha else 24, origin_top=origin_top, rle=rle, alpha_bits=8 if alpha else 0)
        im.save(p / f"{name}.png")
        assert core.convert(t / f"{name}.tga", None, converter=core.LOKI).ok
        assert core.convert(p / f"{name}.png", None, converter=core.LOKI).ok
        assert (t / f"{name}.paa").read_bytes() == (p / f"{name}.paa").read_bytes(), name
        assert not any(q.name.startswith(".loki-tmp") for q in t.iterdir())  # nothing else beside the source


def test_grey_and_colour_mapped_tga_equal_their_png_of_the_same_pixels(tmp_path):
    grey = [[(x * 31 + y * 17) % 256 for x in range(16)] for y in range(16)]
    write_tga(tmp_path / "t" / "g_co.tga", grey, depth=8, grey=True)
    (tmp_path / "p").mkdir()
    g =Image.new("L", (16, 16))
    g.putdata(flat(grey))
    g.save(tmp_path / "p" / "g_co.png")  # a grey PNG, the other way to hold the same picture
    for d in "tp":
        ext = "tga" if d == "t" else "png"
        assert core.convert(tmp_path / d / f"g_co.{ext}", None, converter=core.LOKI).ok
    assert (tmp_path / "t" / "g_co.paa").read_bytes() == (tmp_path / "p" / "g_co.paa").read_bytes()


def test_handover_gets_the_same_8_bit_copy_as_a_png_and_the_paa_lands_beside_the_source(tmp_path, monkeypatch):
    im = noisy((16, 16), True)
    px = list(im.getdata())
    write_tga(tmp_path / "t" / "a_co.tga", [px[y * 16:(y + 1) * 16] for y in range(16)], depth=32, alpha_bits=8)
    seen = {}

    def fake_tool(tool, src, dst):
        with Image.open(src) as got:
            seen[src.suffix] = (got.mode, got.size, list(got.getdata()))
        Path(dst).write_bytes(b"paa")

    monkeypatch.setattr(core, "_run_tool", fake_tool)
    assert core.convert(tmp_path / "t" / "a_co.tga", Path("tool.exe"), converter=core.BOHEMIA).ok
    mode, size, data = seen[".png"]
    assert mode == "RGBA" and size == (16, 16) and data == px
    assert (tmp_path / "t" / "a_co.paa").read_bytes() == b"paa"
    assert sorted(q.name for q in (tmp_path / "t").iterdir()) == ["a_co.paa", "a_co.tga"]


def test_every_existing_check_applies_to_the_new_types(tmp_path):
    write_tga(tmp_path / "odd_co.tga", [[(1, 2, 3)] * 6] * 4)
    e = core.check_png(tmp_path / "odd_co.tga")
    assert e.status == core.FLAGGED and "sides must be powers of two" in e.reason
    p = jpg(tmp_path / "a_co.jpg")  # 32x32: output size and max size work as for a PNG
    size = core.output_size("16x8")
    assert core.convert(p, None, converter=core.LOKI, size=size).ok
    assert core.paa_size(p.with_suffix(".paa")) == (16, 8)
    assert core.check_png(p, None, size).status == core.UP_TO_DATE
    assert core.check_png(p, None, None).status == core.NEEDS_CONVERTING  # plain run does not reuse a sized PAA
    assert core.convert(p, None, 16, core.LOKI).ok and core.paa_size(p.with_suffix(".paa")) == (16, 16)
    assert core.check_png(p, 16).status == core.UP_TO_DATE


# --- two sources, one PAA ----------------------------------------------------------------------------------------


def test_two_sources_for_one_paa_are_both_flagged_and_neither_is_converted(tmp_path):
    Image.new("RGB", (16, 16)).save(tmp_path / "wall_co.png")
    write_tga(tmp_path / "wall_co.tga", [[(1, 2, 3)] * 16] * 16)
    a, b = core.check_png(tmp_path / "wall_co.png"), core.check_png(tmp_path / "wall_co.tga")
    assert a.status == b.status == core.FLAGGED
    assert "wall_co.tga" in a.reason and "wall_co.png" in b.reason and "remove one" in a.reason
    for src in (tmp_path / "wall_co.png", tmp_path / "wall_co.tga"):
        r = core.convert(src, None, converter=core.LOKI)
        assert not r.ok and "wall_co" in r.error
    assert not (tmp_path / "wall_co.paa").exists()
    (tmp_path / "wall_co.png").unlink()  # one removed: the other converts
    assert core.check_png(tmp_path / "wall_co.tga").status == core.NEEDS_CONVERTING
    assert core.convert(tmp_path / "wall_co.tga", None, converter=core.LOKI).ok


def test_a_different_stem_or_folder_is_no_conflict(tmp_path):
    Image.new("RGB", (16, 16)).save(tmp_path / "a_co.png")
    Image.new("RGB", (16, 16)).save(tmp_path / "b_co.jpg")
    (tmp_path / "sub").mkdir()
    Image.new("RGB", (16, 16)).save(tmp_path / "sub" / "a_co.jpeg")
    assert {core.check_png(p).status for p in (tmp_path / "a_co.png", tmp_path / "b_co.jpg", tmp_path / "sub" / "a_co.jpeg")} == {
        core.NEEDS_CONVERTING
    }


def test_cli_flags_both_conflicting_sources_and_exits_1(tmp_path, capsys):
    Image.new("RGB", (16, 16)).save(tmp_path / "wall_co.png")
    write_tga(tmp_path / "wall_co.tga", [[(1, 2, 3)] * 16] * 16)
    code = cli.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1 and out.count("flagged") >= 2 and "same PAA as wall_co.tga" in out and "same PAA as wall_co.png" in out
    assert not (tmp_path / "wall_co.paa").exists()


# --- the CLI: folders and globs ----------------------------------------------------------------------------------


def make_folder(root: Path) -> None:
    Image.new("RGB", (16, 16)).save(root / "a_co.png")
    write_tga(root / "b_co.tga", [[(1, 2, 3)] * 16] * 16)
    jpg(root / "c_co.jpg")
    jpg(root / "d_co.JPEG")
    (root / "e_co.TGA").write_bytes((root / "b_co.tga").read_bytes())
    (root / "notes.txt").write_text("x")
    (root / "f_co.bmp").write_bytes(b"x")


def test_cli_folder_pickup_and_recursion_for_every_source_type(tmp_path, capsys):
    make_folder(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    jpg(sub / "g_co.jpeg")
    code = cli.main([str(tmp_path)])
    assert code == 0, capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.glob("*.paa")) == ["a_co.paa", "b_co.paa", "c_co.paa", "d_co.paa", "e_co.paa"]
    assert not (sub / "g_co.paa").exists()
    assert cli.main([str(tmp_path), "--recursive"]) == 0
    assert (sub / "g_co.paa").is_file()


def test_cli_globs_and_explicit_files_for_the_new_types(tmp_path, capsys):
    make_folder(tmp_path)
    assert cli.main([str(tmp_path / "*_co.tga")]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.paa")) == ["b_co.paa", "e_co.paa"]
    assert cli.main([str(tmp_path / "*.jp*g"), str(tmp_path / "a_co.png")]) == 0
    assert (tmp_path / "c_co.paa").is_file() and (tmp_path / "d_co.paa").is_file() and (tmp_path / "a_co.paa").is_file()
    capsys.readouterr()
    assert cli.main([str(tmp_path / "f_co.bmp")]) == 1
    assert "not a PNG, TGA or JPG file" in capsys.readouterr().out


def test_cli_help_names_the_accepted_formats(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    assert "PNG, TGA and JPG" in out and "*.tga" in out and "*.jpeg" in out


# --- the window and Auto watch -----------------------------------------------------------------------------------


def wait_for(app, cond, timeout=20.0):
    end = time.monotonic() + timeout
    while not cond():
        app.pump()
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)
    app.pump()


def make_app(tmp_path):
    return track_app(app_mod.App(tmp_path / "settings.json", interval=0.02, settle=0.1))


def test_window_drop_takes_every_source_type_and_skips_the_rest(tmp_path):
    make_folder(tmp_path)
    app = make_app(tmp_path)
    added = app.add_files([tmp_path / n for n in ("a_co.png", "b_co.tga", "c_co.jpg", "d_co.JPEG", "e_co.TGA", "notes.txt", "f_co.bmp")])
    assert len(added) == 5
    assert app.message == "Added 5 files; skipped 2 not PNG, TGA or JPG"
    assert {r.status for r in app.rows} == {core.NEEDS_CONVERTING}
    app.close()


def test_window_flags_a_conflict_and_convert_all_skips_both(tmp_path):
    Image.new("RGB", (16, 16)).save(tmp_path / "wall_co.png")
    write_tga(tmp_path / "wall_co.tga", [[(1, 2, 3)] * 16] * 16)
    app = make_app(tmp_path)
    app.add_files([tmp_path / "wall_co.png", tmp_path / "wall_co.tga"])
    assert [r.status for r in app.rows] == [core.FLAGGED, core.FLAGGED]
    app.convert_all()
    time.sleep(0.3)
    app.pump()
    assert not (tmp_path / "wall_co.paa").exists()
    app.close()


def test_window_convert_all_on_a_tga_and_a_jpg_matches_png(tmp_path):
    im = noisy((32, 32), False)
    px = list(im.getdata())
    write_tga(tmp_path / "t_co.tga", [px[y * 32:(y + 1) * 32] for y in range(32)])
    jpg(tmp_path / "j_co.jpg")
    app = make_app(tmp_path)
    app.add_files([tmp_path / "t_co.tga", tmp_path / "j_co.jpg"])
    app.convert_all()
    wait_for(app, lambda: (tmp_path / "t_co.paa").exists() and (tmp_path / "j_co.paa").exists() and app.quiet())
    assert {r.status for r in app.rows} == {core.UP_TO_DATE}
    im.save(tmp_path / "ref_co.png")
    assert core.convert(tmp_path / "ref_co.png", None, converter=core.LOKI).ok
    assert (tmp_path / "t_co.paa").read_bytes() == (tmp_path / "ref_co.paa").read_bytes()
    app.close()


def test_auto_watch_converts_a_saved_tga(tmp_path):
    p = write_tga(tmp_path / "t" / "wall_co.tga", [[(9, 8, 7)] * 16] * 16)
    app = make_app(tmp_path)
    app.add_files([p])
    wait_for(app, app.watching)
    app.toggle([app.rows[0].key])
    time.sleep(0.2)
    assert not p.with_suffix(".paa").exists()
    write_tga(p, [[(200, 100, 50)] * 16] * 16)  # a save from the paint program
    os.utime(p)
    wait_for(app, lambda: p.with_suffix(".paa").exists() and app.quiet())
    assert app.rows[0].status == core.UP_TO_DATE
    paa = p.with_suffix(".paa")
    wait_for(app, lambda: read_when_free(paa) is not None)
    first = read_when_free(paa)
    write_tga(p, [[(1, 100, 50)] * 16] * 16)
    time.sleep(0.05)
    os.utime(p)
    wait_for(app, lambda: read_when_free(paa) not in (None, first) and app.quiet())
    app.close()


def test_readme_names_the_accepted_formats_and_the_jpg_note():
    """The README tells users which source formats Loki takes and that JPG is lossy, so nobody feeds a JPG
    normal map without knowing the cost."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "Loki turns PNG, TGA and JPG textures into Arma 3 PAA files." in readme
    assert "Drag PNG, TGA or JPG files from Explorer onto the window." in readme
    assert "- JPG is lossy: use PNG or TGA for normal maps and masks." in readme
