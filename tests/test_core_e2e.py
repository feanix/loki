"""End-to-end: real ImageToPAA.exe. Skipped with a reason when the tool is not installed."""

import pytest
from PIL import Image

from loki import core, outputs

pytestmark = pytest.mark.e2e

try:
    TOOL = core.find_imagetopaa()
    WHY = ""
except core.ToolNotFound as exc:
    TOOL = None
    WHY = str(exc)

needs_tool = pytest.mark.skipif(TOOL is None, reason=f"ImageToPAA not available: {WHY}")

# PAA type tags are little-endian on disk: DXT1 0xFF01, DXT5 0xFF05, ARGB4444, ARGB1555, AI88, ARGB8888.
VALID_TAGS = {b"\x01\xff", b"\x05\xff", b"\x44\x44", b"\x55\x15", b"\x80\x80", b"\x88\x88"}


@needs_tool
@pytest.mark.parametrize("size,mode", [((256, 256), "RGB"), ((512, 256), "RGBA")])
def test_real_convert(tmp_path, size, mode):
    png = tmp_path / "tex.png"
    Image.new(mode, size, (200, 30, 30, 100) if mode == "RGBA" else (200, 30, 30)).save(png)
    (tmp_path / "tex.paa").write_bytes(b"old")  # must be overwritten
    res = core.convert(png, TOOL)
    assert res.ok, res.error
    data = res.paa.read_bytes()
    assert len(data) > 0 and data[:2] in VALID_TAGS
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tex.paa", "tex.png"]


@needs_tool
def test_real_convert_with_cap_and_bad_png(tmp_path):
    png = tmp_path / "big.png"
    Image.new("RGBA", (1024, 512), (1, 2, 3, 4)).save(png)
    assert core.convert(png, TOOL, 256).ok
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"junk")
    res = core.convert(bad, TOOL)
    assert not res.ok and res.error and not (tmp_path / "bad.paa").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.png", "big.paa", "big.png"]


@needs_tool
def test_real_discovery_is_a_file():
    assert TOOL.name.lower() == "imagetopaa.exe" and TOOL.is_file()


@needs_tool
def test_real_convert_in_odd_folder_name(tmp_path):
    d = tmp_path / "tex dir \u00e9"
    d.mkdir()
    png = d / "t\u00e9x.png"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(png)
    res = core.convert(png, TOOL)
    assert res.ok, res.error
    assert res.paa.read_bytes()[:2] in VALID_TAGS


# --- format follows the output name suffix, sizes follow the cap ---
@needs_tool
@pytest.mark.parametrize("max_size", [None, 512])
@pytest.mark.parametrize(
    "name,mode,tag",
    [("x_ca.png", "RGBA", b"\x05\xff"), ("x_co.png", "RGB", b"\x01\xff"), ("x_co.png", "RGBA", b"\x01\xff"),
     ("x_ca.png", "RGB", b"\x05\xff"), ("plain.png", "RGB", b"\x01\xff")],
)  # fmt: skip
def test_real_format_follows_suffix_and_size_follows_cap(tmp_path, name, mode, tag, max_size):
    png = tmp_path / name
    Image.new(mode, (1024, 1024), (200, 30, 30, 100) if mode == "RGBA" else (200, 30, 30)).save(png)
    res = core.convert(png, TOOL, max_size)
    assert res.ok, res.error
    assert res.paa.read_bytes()[:2] == tag
    want = (512, 512) if max_size else (1024, 1024)
    assert core.paa_size(res.paa) == want
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([name, res.paa.name])


@needs_tool
@pytest.mark.parametrize("size", [(1024, 512), (256, 128), (64, 8), (4, 4)])
@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_real_paa_size_reads_tool_output(tmp_path, size, mode):
    png = tmp_path / "t_co.png"
    Image.new(mode, size, (9, 9, 9)).save(png)
    assert core.convert(png, TOOL).ok
    assert core.paa_size(tmp_path / "t_co.paa") == size


@needs_tool
def test_parallel_convert_all_matches_sequential_conversion_byte_for_byte(tmp_path):
    import shutil
    import time

    from loki.app import App

    specs = [  # name, size, mode, cap: _ca (DXT5 by suffix), _co, a capped row, a plain row, a capped _ca
        ("sky_ca.png", (256, 128), "RGBA", None),
        ("rock_co.png", (256, 256), "RGB", None),
        ("wall.png", (512, 256), "RGB", 256),
        ("floor.png", (128, 128), "RGB", None),
        ("leaf_ca.png", (512, 512), "RGBA", 256),
    ]
    par, seq = tmp_path / "par", tmp_path / "seq"
    par.mkdir()
    seq.mkdir()
    for i, (name, size, mode, _cap) in enumerate(specs):
        img = Image.effect_noise(size, 60 + i).convert(mode)
        img.save(par / name)
        shutil.copy2(par / name, seq / name)
    app = App(tmp_path / "s.json", find_tool=lambda _o: TOOL, interval=0.05, settle=0.2, workers=4)
    app.set_converter(core.BOHEMIA)  # this compares the pool with sequential ImageToPAA runs
    try:
        app.add_files([par / s[0] for s in specs])
        for s in specs:
            app.set_max_size([core.file_key(par / s[0])], s[3])
        assert app.convert_all()
        end = time.monotonic() + 120
        while app.converting:
            app.pump()
            assert time.monotonic() < end, "convert all timed out"
            time.sleep(0.02)
        app.pump()
        assert app.summary() == f"Converted {len(specs)} file(s)", app.summary()
    finally:
        app.close()
    for name, _size, _mode, cap in specs:
        res = core.convert(seq / name, TOOL, cap)
        assert res.ok, res.error
        assert (par / name).with_suffix(".paa").read_bytes() == (seq / name).with_suffix(".paa").read_bytes(), name
    assert core.paa_size(par / "leaf_ca.paa") == (256, 256) and core.paa_size(par / "wall.paa") == (256, 128)
    assert not [p for p in par.iterdir() if p.name.startswith(".loki-tmp")]


@needs_tool
def test_real_handover_with_an_output_size_and_the_paa_reads_back(tmp_path):
    """ImageToPAA converts the sized copy, and reads back the untouched .paa."""
    png = tmp_path / "tex.png"
    Image.new("RGBA", (300, 200), (200, 30, 30, 255)).save(png)
    size = core.output_size("512x256", "contain", "#00ff00", "top-left")
    res = core.convert(png, TOOL, None, core.BOHEMIA, size=size)
    assert res.ok, res.error
    assert core.paa_size(res.paa) == (512, 256) and outputs.recorded(res.paa) == size.key("tex.png")
    back = tmp_path / "back.png"
    assert core._run_tool(TOOL, res.paa, back) is None
    with Image.open(back) as im:
        assert im.size == (512, 256)
        assert im.convert("RGB").getpixel((0, 0))[0] > 150  # the image sits top-left: red there, green fill elsewhere
        assert im.convert("RGB").getpixel((511, 255))[1] > 150
