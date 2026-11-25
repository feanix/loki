"""Output size (#60, decision 51): --size with --fit, --pad-colour and --pad-align, in core and on the command line.

Runs loki.cli.main in-process with the real Loki encoder; ImageToPAA is faked (the hand-over test) or never
needed. The pixel geometry, resampling quality and mip equality are proven in native/paa/src/tests_size.rs;
these tests check the wiring (options, defaults, flags, up-to-date logic, hand-over copy, stored settings).
"""

import struct
from pathlib import Path

import pytest
from PIL import Image

from loki import _paa, cli, core, outputs
from paa_helpers import paa_bytes


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)


def png(path: Path, size, colour=(200, 100, 50, 255), mode="RGBA") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, colour if mode == "RGBA" else colour[:3]).save(path)
    return path


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main([str(a) for a in argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# --- parsing and validation ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [("512x256", (512, 256)), (" 4X4 ", (4, 4)), ("1x1", (1, 1)), ("4096x4096", (4096, 4096))])
def test_parse_size_accepts_powers_of_two_from_1_to_4096(text, want):
    assert core.parse_size(text) == want


@pytest.mark.parametrize("text", ["300x200", "512", "512x", "0x4", "8192x4", "4x-4", "512*256", "abc", "", "6x4"])
def test_parse_size_rejects_with_a_plain_message(text):
    with pytest.raises(ValueError, match="size"):
        core.parse_size(text)


@pytest.mark.parametrize(
    "text,want",
    [
        ("#ff8000", (255, 128, 0, 255)),
        ("#FF800040", (255, 128, 0, 64)),
        ("128,128,255,255", (128, 128, 255, 255)),
        (" 0, 0, 0, 0 ", (0, 0, 0, 0)),
    ],
)
def test_parse_colour_hex6_hex8_and_rgba(text, want):
    assert core.parse_colour(text) == want


@pytest.mark.parametrize("text", ["#12345", "#1234567", "#gggggg", "ff8000", "1,2,3", "1,2,3,4,5", "256,0,0,0", "-1,0,0,0", "a,b,c,d", ""])
def test_parse_colour_rejects_with_a_plain_message(text):
    with pytest.raises(ValueError, match="colour"):
        core.parse_colour(text)


def test_parse_align_accepts_every_name_and_center():
    for name in core.ALIGNS:
        assert core.parse_align(name) == name
    assert core.parse_align("Center") == "centre"
    with pytest.raises(ValueError, match="alignment"):
        core.parse_align("middle")


def test_output_size_defaults_and_bad_fit():
    s = core.output_size("512x256")
    assert (s.width, s.height, s.fit, s.colour, s.align) == (512, 256, "contain", None, "centre")
    with pytest.raises(ValueError, match="fit"):
        core.output_size("512x256", "fill")


# --- defaults per type ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,want",
    [
        *[(f"x_{s}.png", (128, 128, 255, 255)) for s in ("nohq", "no", "novhq", "nofhq", "nshq", "non", "normalmap", "ns", "nof")],
        *[(f"x_{s}.png", (255, 255, 255, 255)) for s in ("as", "ads", "adshq")],
        *[(f"x_{s}.png", (0, 0, 0, 0)) for s in ("ca", "lca", "cat", "can", "8888", "mc", "gs")],
        *[(f"x_{s}.png", (0, 0, 0, 255)) for s in ("co", "lco", "sky", "sm", "dxt1")],
        ("plain.png", (0, 0, 0, 255)),
        ("X_NOHQ.PNG", (128, 128, 255, 255)),
    ],
)
def test_default_pad_colour_by_type(name, want):
    assert _paa.default_pad_colour(name) == want


def corner(path: Path, xy=(0, 0)):
    with Image.open(path) as im:
        return im.convert("RGBA").getpixel(xy)


@pytest.mark.parametrize("suffix,want", [("nohq", (128, 128, 255, 255)), ("as", (255, 255, 255, 255)), ("ca", (0, 0, 0, 0)), ("co", (0, 0, 0, 255))])
def test_default_fill_is_used_and_explicit_colour_wins(tmp_path, suffix, want):
    src = png(tmp_path / f"t_{suffix}.png", (4, 4), (9, 9, 9, 255))
    out = tmp_path / "out" / src.name
    out.parent.mkdir()
    _paa.fit_png(str(src), str(out), (8, 8), "pad", None, "top-left")
    assert corner(out, (7, 7)) == want and corner(out, (0, 0)) == (9, 9, 9, 255)
    _paa.fit_png(str(src), str(out), (8, 8), "pad", (1, 2, 3, 4), "top-left")
    assert corner(out, (7, 7)) == (1, 2, 3, 4)


# --- fit modes and alignments --------------------------------------------------------------------------


@pytest.mark.parametrize("canvas", [(512, 256), (256, 256)])
@pytest.mark.parametrize("fit", ["stretch", "contain"])
def test_stretch_and_contain_make_exactly_the_size(tmp_path, capsys, canvas, fit):
    p = png(tmp_path / "a_co.png", (300, 200))
    code, out, _ = run(capsys, p, "--size", f"{canvas[0]}x{canvas[1]}", "--fit", fit)
    assert code == 0 and f"({canvas[0]}x{canvas[1]}, loki," in out
    assert core.paa_size(tmp_path / "a_co.paa") == canvas


def test_default_fit_is_contain(tmp_path, capsys):
    p = png(tmp_path / "a_co.png", (300, 200))
    assert run(capsys, p, "--size", "512x256")[0] == 0
    a = (tmp_path / "a_co.paa").read_bytes()
    assert run(capsys, p, "--size", "512x256", "--fit", "contain")[0] == 0
    assert (tmp_path / "a_co.paa").read_bytes() == a
    assert outputs.recorded(tmp_path / "a_co.paa") == "size=512x256 fit=contain colour=000000ff align=centre"


def test_pad_fits_in_the_larger_canvas_and_flags_a_larger_image(tmp_path, capsys):
    p = png(tmp_path / "a_co.png", (300, 200))
    code, out, _ = run(capsys, p, "--size", "512x256", "--fit", "pad")
    assert code == 0 and core.paa_size(tmp_path / "a_co.paa") == (512, 256)
    (tmp_path / "a_co.paa").unlink()
    code, out, _ = run(capsys, p, "--size", "256x256", "--fit", "pad")
    assert code == 1 and out.startswith("flagged") and "300x200 is larger than the output size 256x256" in out
    assert "pad does not resize" in out and not (tmp_path / "a_co.paa").exists()
    entry = core.check_png(p, size=core.output_size("256x256", "pad"))
    assert entry.status == core.FLAGGED and "larger than the output size" in entry.reason


def test_every_alignment_reaches_the_encoder(tmp_path):
    src = png(tmp_path / "a_co.png", (2, 2), (255, 255, 255, 255))
    want = {
        "top-left": (0, 0), "top": (3, 0), "top-right": (6, 0),
        "left": (0, 3), "centre": (3, 3), "right": (6, 3),
        "bottom-left": (0, 6), "bottom": (3, 6), "bottom-right": (6, 6),
    }  # fmt: skip
    assert set(want) == set(core.ALIGNS)
    for align, (x0, y0) in want.items():
        out = tmp_path / "out" / f"{align}_co.png"
        out.parent.mkdir(exist_ok=True)
        _paa.fit_png(str(src), str(out), (8, 8), "pad", (0, 0, 0, 255), align)
        with Image.open(out) as im:
            px = im.convert("RGBA")
            for y in range(8):
                for x in range(8):
                    inside = x0 <= x < x0 + 2 and y0 <= y < y0 + 2
                    assert px.getpixel((x, y))[0] == (255 if inside else 0), (align, x, y)


def test_options_reach_the_paa_and_center_and_color_aliases_work(tmp_path, capsys):
    p = png(tmp_path / "a_co.png", (300, 200))
    code, _, _ = run(capsys, p, "--size", "512x256", "--pad-color", "#ff0000", "--pad-align", "center")
    assert code == 0
    assert outputs.recorded(tmp_path / "a_co.paa") == "size=512x256 fit=contain colour=ff0000ff align=centre"
    code, _, _ = run(capsys, p, "--size", "512x256", "--fit", "stretch", "--pad-colour", "1,2,3,4", "--pad-align", "top-left")
    assert code == 0 and outputs.recorded(tmp_path / "a_co.paa") == "size=512x256 fit=stretch"


# --- usage errors ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,needle",
    [
        (["--size", "512x256", "--max-size", "256"], "--size and --max-size"),
        (["--size", "512x256", "--max-size", "off"], "--size and --max-size"),
        (["--fit", "pad"], "--fit needs --size"),
        (["--pad-colour", "#000000"], "--pad-colour needs --size"),
        (["--pad-color", "#000000"], "--pad-colour needs --size"),
        (["--pad-align", "top"], "--pad-align needs --size"),
        (["--size", "300x200"], "powers of two"),
        (["--size", "512"], "512x256"),
        (["--size", "8192x8192"], "powers of two"),
        (["--size", "512x256", "--fit", "fill"], "fit must be"),
        (["--size", "512x256", "--pad-colour", "red"], "colour must be"),
        (["--size", "512x256", "--pad-colour", "1,2,3"], "colour must be"),
        (["--size", "512x256", "--pad-colour", "300,0,0,0"], "0-255"),
        (["--size", "512x256", "--pad-align", "middle"], "alignment must be"),
    ],
)
def test_usage_errors_exit_2_with_a_plain_message(tmp_path, capsys, argv, needle):
    p = png(tmp_path / "a_co.png", (64, 64))
    code, out, err = run(capsys, p, *argv)
    assert code == 2 and out == "" and needle in err and "Traceback" not in err
    assert not (tmp_path / "a_co.paa").exists()


def test_non_power_of_two_is_converted_only_with_a_size(tmp_path, capsys):
    p = png(tmp_path / "odd_co.png", (300, 200))
    code, out, _ = run(capsys, p)
    assert code == 1 and "sides must be powers of two (e.g. 256x256) or set an output size" in out
    assert not (tmp_path / "odd_co.paa").exists()
    code, out, _ = run(capsys, p, "--size", "512x256")
    assert code == 0 and core.paa_size(tmp_path / "odd_co.paa") == (512, 256)
    with Image.open(p) as im:
        assert im.size == (300, 200)  # the source is never modified


# --- up to date ----------------------------------------------------------------------------------------


def test_other_settings_are_not_up_to_date(tmp_path, capsys):
    p = png(tmp_path / "a_co.png", (256, 128))
    args = ["--size", "256x128", "--pad-colour", "#102030", "--pad-align", "top"]
    base = core.output_size("256x128", "contain", "#102030", "top")
    assert run(capsys, p, *args)[0] == 0
    assert core.check_png(p, size=base).status == core.UP_TO_DATE
    others = {
        "size": core.output_size("128x128", "contain", "#102030", "top"),
        "fit": core.output_size("256x128", "stretch", "#102030", "top"),
        "pad": core.output_size("256x128", "pad", "#102030", "top"),
        "colour": core.output_size("256x128", "contain", "#102031", "top"),
        "align": core.output_size("256x128", "contain", "#102030", "bottom"),
    }
    for what, other in others.items():
        assert core.check_png(p, size=other).status == core.NEEDS_CONVERTING, what
    # A plain run does not reuse a PAA made with an output size, even at the same size.
    assert core.check_png(p, None).status == core.NEEDS_CONVERTING
    # --skip-up-to-date follows the same rule.
    code, out, _ = run(capsys, p, *args, "--skip-up-to-date")
    assert code == 0 and out.startswith("skipped")
    for other in (["--pad-align", "bottom"], ["--pad-colour", "#102031"], ["--fit", "stretch"], ["--size", "128x128"]):
        code, out, _ = run(capsys, p, "--size", "256x128", "--pad-colour", "#102030", "--pad-align", "top", *other, "--skip-up-to-date")
        assert code == 0 and out.startswith("converted"), other
        assert run(capsys, p, *args)[0] == 0  # back to the base settings
    code, out, _ = run(capsys, p, "--skip-up-to-date")
    assert code == 0 and out.startswith("converted")  # a plain run


def test_a_class_default_counts_as_the_colour(tmp_path):
    # The default colour is part of the settings, so the same options on another type compare by the real colour.
    assert core.output_size("8x8").key("a_nohq.png").endswith("colour=8080ffff align=centre")
    assert core.output_size("8x8").key("a_co.png").endswith("colour=000000ff align=centre")
    assert core.output_size("8x8", "stretch").key("a_co.png") == "size=8x8 fit=stretch"


def test_plain_paa_is_not_up_to_date_for_a_size_and_max_size_still_works(tmp_path, capsys):
    p = png(tmp_path / "a_co.png", (512, 512))
    assert run(capsys, p, "--max-size", "256")[0] == 0
    assert core.check_png(p, 256).status == core.UP_TO_DATE
    assert core.check_png(p, size=core.output_size("256x256")).status == core.NEEDS_CONVERTING  # no settings stored
    assert outputs.recorded(tmp_path / "a_co.paa") is None


# --- the stored settings ------------------------------------------------------------------------------


def tags(paa: Path) -> list[bytes]:
    """The tag names (on-disk spelling) of a PAA header, in order."""
    data = paa.read_bytes()
    pos, out = 2, []
    while data[pos : pos + 4] == b"GGAT":
        out.append(data[pos + 4 : pos + 8])
        (length,) = struct.unpack_from("<I", data, pos + 8)
        pos += 12 + length
    return out


def test_a_size_paa_has_exactly_the_tags_of_a_plain_one_on_the_loki_route(tmp_path):
    p = png(tmp_path / "a_co.png", (256, 128))
    assert core.convert(p, None, None, core.LOKI).ok
    plain = tags(tmp_path / "a_co.paa")
    for size in ("512x256", "64x64"):
        res = core.convert(p, None, None, core.LOKI, size=core.output_size(size, "pad" if size == "512x256" else "contain"))
        assert res.ok and tags(res.paa) == plain, size


def test_a_size_paa_has_exactly_the_tags_of_a_plain_one_on_the_handover_route(tmp_path, monkeypatch):
    def tool(exe, src, dst):
        Path(dst).write_bytes(paa_bytes(512, 256))

    monkeypatch.setattr(core, "_run_tool", tool)
    p = png(tmp_path / "rock_cat.png", (256, 128))
    exe = tmp_path / "ImageToPAA.exe"
    assert core.convert(p, exe, None, core.LOKI).ok
    plain, written = tags(tmp_path / "rock_cat.paa"), (tmp_path / "rock_cat.paa").read_bytes()
    res = core.convert(p, exe, None, core.LOKI, size=core.output_size("512x256"))
    assert res.ok and tags(res.paa) == plain and res.paa.read_bytes() == written  # the tool's output is never touched


def test_sized_loki_paa_equals_the_plain_one(tmp_path):
    p = png(tmp_path / "a_co.png", (64, 64))
    plain, sized = tmp_path / "plain.paa", tmp_path / "sized.paa"
    _paa.encode(str(p), str(plain), None)
    _paa.encode(str(p), str(sized), None, (64, 64))
    assert plain.read_bytes() == sized.read_bytes()  # a size equal to the image leaves the pixels alone
    assert core.convert(p, None, None, core.LOKI, size=core.output_size("64x64")).ok
    assert outputs.recorded(p.with_suffix(".paa")) == "size=64x64 fit=contain colour=000000ff align=centre"


def test_a_size_that_is_a_mip_level_matches_the_mip_path(tmp_path):
    for suffix in ("co", "nohq", "ca"):
        p = tmp_path / f"t_{suffix}.png"
        im = Image.new("RGBA", (256, 128))
        im.putdata([((x * 7) % 256, (y * 5) % 256, 30 + (x + y) % 90, 255) for y in range(128) for x in range(256)])
        im.save(p)
        a, b = tmp_path / "a.paa", tmp_path / "b.paa"
        _paa.encode(str(p), str(a), 64)
        _paa.encode(str(p), str(b), None, (64, 32), "stretch")
        assert a.read_bytes() == b.read_bytes(), suffix
        _paa.encode(str(p), str(b), None, (64, 32), "contain")
        assert a.read_bytes() == b.read_bytes(), suffix


def test_size_and_max_size_together_are_refused_by_the_encoder(tmp_path):
    p = png(tmp_path / "a_co.png", (64, 64))
    with pytest.raises(ValueError, match="cannot be used together"):
        _paa.encode(str(p), str(tmp_path / "a.paa"), 32, (32, 32))


# --- resampling quality (through the Python API) -------------------------------------------------------


def test_no_halo_around_a_red_square_on_transparent_padding(tmp_path):
    src = tmp_path / "sq_ca.png"
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (32, 32), (255, 0, 0, 255)), (16, 16))
    im.save(src)
    for size in [(32, 16), (128, 128), (256, 32)]:
        out = tmp_path / f"o{size[0]}" / "sq_ca.png"
        out.parent.mkdir()
        _paa.fit_png(str(src), str(out), size, "stretch")
        with Image.open(out) as o:
            raw = o.convert("RGBA").tobytes()
            seen = [raw[i : i + 4] for i in range(0, len(raw), 4) if raw[i + 3] > 0]
        assert seen and all(tuple(px[:3]) == (255, 0, 0) for px in seen), size


def test_normals_are_unit_length_after_resizing(tmp_path):
    src = tmp_path / "n_nohq.png"
    im = Image.new("RGB", (64, 64))
    im.putdata([(60 + (i * 3) % 120, 90 + (i * 7) % 100, 200 + (i % 50), ) for i in range(64 * 64)])
    im.save(src)
    out = tmp_path / "o" / "n_nohq.png"
    out.parent.mkdir()
    _paa.fit_png(str(src), str(out), (128, 32), "stretch")
    with Image.open(out) as o:
        raw = o.convert("RGBA").tobytes()
        for i in range(0, len(raw), 4):
            v = [c / 255 * 2 - 1 for c in raw[i : i + 3]]
            assert abs(sum(c * c for c in v) ** 0.5 - 1) < 0.01


# --- the ImageToPAA hand-over -------------------------------------------------------------------------


def test_handover_gets_the_sized_8bit_copy_and_the_settings_are_recorded(tmp_path, monkeypatch):
    seen = {}

    def tool(exe, src, dst):
        with Image.open(src) as im:
            seen.update(size=im.size, mode=im.mode, name=Path(src).name, corner=im.getpixel((0, 0)))
        Path(dst).write_bytes(paa_bytes(512, 256))

    monkeypatch.setattr(core, "_run_tool", tool)
    p = tmp_path / "rock_cat.png"  # _cat is handed over
    Image.new("L", (300, 200), 77).save(p)  # 8-bit grey: a plain hand-over would need a copy anyway
    sized = core.output_size("512x256", "contain", "10,20,30,40", "top-left")
    res = core.convert(p, tmp_path / "ImageToPAA.exe", None, core.LOKI, size=sized)
    assert res.ok and res.converter == core.BOHEMIA, res.error
    assert seen == {"size": (512, 256), "mode": "RGBA", "name": "rock_cat.png", "corner": (77, 77, 77, 255)}
    assert outputs.recorded(res.paa) == "size=512x256 fit=contain colour=0a141e28 align=top-left"
    assert core.paa_size(res.paa) == (512, 256)
    with Image.open(p) as im:
        assert im.size == (300, 200) and im.mode == "L"  # the source PNG is untouched


def test_handover_without_a_size_is_as_before(tmp_path, monkeypatch):
    got = []

    def tool(exe, src, dst):
        got.append(Path(src))
        Path(dst).write_bytes(paa_bytes(64, 64))

    monkeypatch.setattr(core, "_run_tool", tool)
    p = png(tmp_path / "rock_cat.png", (64, 64), mode="RGB")
    res = core.convert(p, tmp_path / "ImageToPAA.exe", None, core.LOKI)
    assert res.ok and got == [p] and outputs.recorded(res.paa) is None


# --- help and README ----------------------------------------------------------------------------------


def test_help_and_readme_document_the_options_and_the_four_defaults(capsys):
    root = Path(__file__).resolve().parent.parent
    readme = " ".join((root / "README.md").read_text(encoding="utf8").split())
    _code, out, _ = run(capsys, "--help")
    help_text = " ".join(out.split())
    for opt in ("--size", "--fit", "--pad-colour", "--pad-align"):
        assert opt in out and opt in readme, opt
    assert "--size 512x256 --fit pad" in out and "--size 512x256 --fit pad" in readme
    for colour in ("128,128,255,255", "255,255,255,255", "0,0,0,0", "0,0,0,255"):
        assert colour in help_text and colour in readme, colour
    assert "--pad-color" in readme and "center" in readme
    assert "or set an output size" in readme  # criterion 10: the power-of-two messages point at --size
