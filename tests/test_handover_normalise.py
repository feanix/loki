"""ImageToPAA hand-over input: grey, 16-bit and palette PNGs go to the tool as 8-bit RGB(A),
and a 4096x4096 DXT5 whose mip 0 overflows the PAA length field says so (no hand-over).

Why: ImageToPAA mangles grey and low-depth PNGs, fails 16-bit RGBA / grey+alpha / palette+tRNS, and ignores tRNS
colour keys (the deep scan, findings F1 to F3). The real-tool tests are skipped when Arma 3 Tools is absent.
Inputs are generated here; the PAA is read back with the crate's own decoder (`paa-proto decode`)."""

import math
import os
import struct
import subprocess
import zlib
from pathlib import Path

import build_native
import pytest
from PIL import Image

from loki import _paa, core

try:
    TOOL = core.find_imagetopaa()
    WHY = ""
except core.ToolNotFound as exc:
    TOOL = None
    WHY = str(exc)

needs_tool = pytest.mark.skipif(TOOL is None, reason=f"ImageToPAA not available: {WHY}")

N = 64


# --- hand-written PNGs: every bit depth and chunk exact -------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _pack_row(samples: list[int], depth: int) -> bytes:
    if depth == 8:
        return bytes(samples)
    if depth == 16:
        return b"".join(struct.pack(">H", v) for v in samples)
    per = 8 // depth
    out = bytearray()
    for i in range(0, len(samples), per):
        b = 0
        for v in samples[i : i + per] + [0] * (per - len(samples[i : i + per])):
            b = (b << depth) | v
        out.append(b)
    return bytes(out)


def write_png(path: Path, rows: list[list[int]], depth: int, ctype: int, plte: bytes = b"", trns: bytes = b"") -> Path:
    """rows: per row, the interleaved samples (channels x width) already at `depth`."""
    ihdr = struct.pack(">IIBBBBB", len(rows[0]) // {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype], len(rows), depth, ctype, 0, 0, 0)
    raw = b"".join(b"\0" + _pack_row(r, depth) for r in rows)
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
    if plte:
        data += _chunk(b"PLTE", plte)
    if trns:
        data += _chunk(b"tRNS", trns)
    path.write_bytes(data + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
    return path


# --- the cases: (name, builder) -> (png written, ideal RGBA per pixel) -------------------------------------


def _gray8(p: Path):
    g = [[(x * 3 + y * 2) % 256 for x in range(N)] for y in range(N)]
    write_png(p, g, 8, 0)
    return [[(v, v, v, 255) for v in r] for r in g]


def _gray1(p: Path):
    g = [[1 if (x // 8 + y // 8) % 2 else 0 for x in range(N)] for y in range(N)]
    write_png(p, g, 1, 0)
    return [[(v * 255,) * 3 + (255,) for v in r] for r in g]


def _gray8key(p: Path):
    g = [[(x * 3 + y * 2) % 200 + 20 for x in range(N)] for y in range(N)]
    for y in range(16, 32):
        for x in range(16, 32):
            g[y][x] = 128
    write_png(p, g, 8, 0, trns=struct.pack(">H", 128))
    return [[(v, v, v, 0 if v == 128 else 255) for v in r] for r in g]


def _rgb16(v: int) -> int:
    return (v * 255 + 32767) // 65535


def _rgba16(p: Path):
    rows, ideal = [], []
    for y in range(N):
        r16 = [[(x * 900 + 100 + 37 * y) % 65536, (y * 900 + 77) % 65536, (x * 400 + y * 400 + 5000) % 65536,
                (x * 1000 + y * 100) % 65536] for x in range(N)]
        rows.append([v for px in r16 for v in px])
        ideal.append([tuple(_rgb16(v) for v in px) for px in r16])
    write_png(p, rows, 16, 6)
    return ideal


def _graya16(p: Path):
    rows, ideal = [], []
    for y in range(N):
        px = [((x * 1000 + y * 300 + 200) % 65536, (x * 1000 + y * 100) % 65536) for x in range(N)]
        rows.append([v for pair in px for v in pair])
        ideal.append([(_rgb16(g),) * 3 + (_rgb16(a),) for g, a in px])
    write_png(p, rows, 16, 4)
    return ideal


def _pal8t(p: Path):
    pal = [((i * 16) % 256, (255 - i * 12) % 256, (i * 7 + 40) % 256) for i in range(16)]
    alpha = [[0, 255, 128, 64][i % 4] for i in range(16)]
    idx = [[(x // 4 + y // 4) % 16 for x in range(N)] for y in range(N)]
    write_png(p, idx, 8, 3, plte=bytes(c for rgb in pal for c in rgb), trns=bytes(alpha))
    return [[pal[i] + (alpha[i],) for i in r] for r in idx]


CASES = [
    ("gray8", "_co", _gray8, False),
    ("gray1", "_co", _gray1, False),
    ("gray8key", "_ca", _gray8key, False),
    ("rgba16", "_ca", _rgba16, True),
    ("graya16", "_ca", _graya16, True),
    ("pal8t", "_co", _pal8t, False),
    ("pal8t", "_ca", _pal8t, True),
]


# --- reading the PAA back ----------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def paa_proto(tmp_path_factory):
    """The crate's CLI (decoder), built into this checkout's cargo target dir."""
    tdir = build_native.target_dir()
    env = {**os.environ, "CARGO_TARGET_DIR": str(tdir)}
    r = subprocess.run(
        ["cargo", "build", "--release", "--locked", "--bin", "paa-proto"],
        cwd=build_native.CRATE, env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr[-800:]
    exe = tdir / "release" / "paa-proto.exe"
    return exe if exe.is_file() else tdir / "release" / "paa-proto"


def decode(exe: Path, paa: Path, out: Path) -> list[list[tuple[int, ...]]]:
    subprocess.run([str(exe), "decode", str(paa), str(out), "--mip", "0"], check=True, capture_output=True)
    with Image.open(out) as im:
        im = im.convert("RGBA")
        raw = im.tobytes()
    px = [tuple(raw[i : i + 4]) for i in range(0, len(raw), 4)]
    return [px[y * N : (y + 1) * N] for y in range(N)]


def dxt1_ideal(ideal):
    """What a `_co` (DXT1) texture can hold of the ideal pixels: 1-bit alpha, transparent pixels black (0,0,0,0).
    The threshold is the one ImageToPAA applies to a plain 8-bit RGBA `_co` (alpha 128 stays opaque, 64 does not)."""
    return [[px if px[3] == 255 else ((0, 0, 0, 0) if px[3] < 128 else (*px[:3], 255)) for px in r] for r in ideal]


def psnr_and_alpha_shift(got, ideal) -> tuple[float, float]:
    chans = 4
    se = n = 0
    shift = 0.0
    for gr, ir in zip(got, ideal, strict=True):
        for g, i in zip(gr, ir, strict=True):
            for k in range(chans):
                se += (g[k] - i[k]) ** 2
                n += 1
            shift += g[3] - i[3]
    mse = se / n
    return (99.0 if mse == 0 else 10 * math.log10(255**2 / mse)), shift / (N * N)


@needs_tool
@pytest.mark.parametrize(("name", "suffix", "build", "hands_over"), CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_imagetopaa_gets_8bit_rgb_and_the_paa_matches_the_ideal(tmp_path, paa_proto, name, suffix, build, hands_over, capsys):
    """A PAA is written and decodes to within 35 dB and 2 alpha levels of the ideal pixels: by real ImageToPAA with the
    Bohemia converter, and (`hands_over`, the cases Loki handed over before) by Loki's own encoder."""
    for converter in (core.BOHEMIA, core.LOKI) if hands_over else (core.BOHEMIA,):
        png = tmp_path / converter / f"{name}{suffix}.png"
        png.parent.mkdir()
        ideal = build(png)
        before = png.read_bytes()
        res = core.convert(png, TOOL, None, converter)
        assert res.ok, res.error
        assert res.converter == converter  # no alpha file is handed over, the Loki converter encodes it itself
        assert png.read_bytes() == before  # the source is never modified
        got = decode(paa_proto, png.with_suffix(".paa"), png.parent / "back.png")
        psnr, shift = psnr_and_alpha_shift(got, dxt1_ideal(ideal) if suffix == "_co" else ideal)
        with capsys.disabled():
            print(f"\nHANDOVER {name}{suffix} via {converter}: PSNR {psnr:.2f} dB, alpha shift {shift:+.2f}")
        assert psnr >= 35, (name, suffix, psnr)
        assert abs(shift) < 2, (name, suffix, shift)
        assert sorted(p.name for p in png.parent.iterdir() if p.name.startswith(".loki-tmp")) == []  # temp folder gone


# --- the helper -------------------------------------------------------------------------------------------


def test_rgb8_and_rgba8_are_passed_as_they_are(tmp_path):
    for mode in ("RGB", "RGBA"):
        png = tmp_path / f"plain{mode}.png"
        Image.new(mode, (8, 8), (10, 20, 30, 40)[: len(mode)]).save(png)
        assert core._imagetopaa_input(png, tmp_path / "t", None) == png  # same path: no copy
    assert not (tmp_path / "t").exists()


def test_16bit_grey_keeps_the_high_byte_and_never_clips(tmp_path):
    png = write_png(tmp_path / "g16.png", [[0x80FF, 0xFFFF, 0x0000, 0x00FF]], 16, 0)
    tdir = tmp_path / "t"
    tdir.mkdir()
    src = core._imagetopaa_input(png, tdir, None)
    assert src != png and src.parent == tdir
    with Image.open(src) as im:
        assert im.mode == "RGB"
        assert [im.getpixel((x, 0))[0] for x in range(4)] == [0x80, 0xFF, 0x00, 0x00]  # 0x80FF is not 255


def test_16bit_rgba_is_reduced_to_8bit_by_the_high_byte(tmp_path):
    png = write_png(tmp_path / "c16.png", [[0x80FF, 0x1234, 0xFF00, 0x7FFF]], 16, 6)
    tdir = tmp_path / "t"
    tdir.mkdir()
    with Image.open(core._imagetopaa_input(png, tdir, None)) as im:
        assert im.mode == "RGBA" and im.getpixel((0, 0)) == (0x80, 0x12, 0xFF, 0x7F)


def test_rgb8_with_a_colour_key_becomes_rgba(tmp_path):
    png = write_png(tmp_path / "key.png", [[10, 200, 30, 1, 2, 3]], 8, 2, trns=struct.pack(">HHH", 10, 200, 30))
    tdir = tmp_path / "t"
    tdir.mkdir()
    with Image.open(core._imagetopaa_input(png, tdir, None)) as im:
        assert im.mode == "RGBA" and [im.getpixel((x, 0))[3] for x in range(2)] == [0, 255]


def test_resize_and_normalise_share_one_copy(tmp_path):
    png = write_png(tmp_path / "g.png", [[(x * 4) % 256 for x in range(8)] for _ in range(8)], 8, 0)
    tdir = tmp_path / "t"
    tdir.mkdir()
    with Image.open(core._imagetopaa_input(png, tdir, 4)) as im:
        assert im.size == (4, 4) and im.mode == "RGB"


# --- F5: MipTooLarge --------------------------------------------------------------------------------------


def test_incompressible_4096_dxt5_has_its_own_message_and_no_handover(tmp_path, monkeypatch):
    """The mip-too-large case: 4096x4096 RGBA noise as _nohq (DXT5) is 1 byte over the 24-bit mip length field."""
    png = tmp_path / "noise_nohq.png"
    im = Image.frombytes("RGBA", (4096, 4096), os.urandom(4096 * 4096 * 4))
    im.save(png, compress_level=0)
    with pytest.raises(_paa.MipTooLarge):
        _paa.encode(str(png), str(tmp_path / "x.paa"), None)
    assert issubclass(_paa.MipTooLarge, ValueError) and not issubclass(_paa.MipTooLarge, _paa.NeedsImageToPAA)
    called = []

    monkeypatch.setattr(core, "_run_tool", lambda *a: called.append(a))
    res = core.convert(png, Path("ImageToPAA.exe"), None, core.LOKI)
    assert not res.ok and res.error == "too detailed to fit at 4096 as DXT5: set max size 2048"
    assert not res.needs_tool and called == []  # not handed over
    assert not png.with_suffix(".paa").exists()
    res = core.convert(png, None, 2048, core.LOKI)  # the advice works
    assert res.ok, res.error


def test_incompressible_8192_dxt5_is_halved_and_then_has_the_mip_too_large_message(tmp_path, monkeypatch):
    """The 4096 DXT5 overflow message still applies after an 8192 PNG is halved to 4096 (256 MiB noise PNG)."""
    png = tmp_path / "noise_nohq.png"
    Image.frombytes("RGBA", (8192, 8192), os.urandom(8192 * 8192 * 4)).save(png, compress_level=0)
    called = []
    monkeypatch.setattr(core, "_run_tool", lambda *a: called.append(a))
    res = core.convert(png, Path("ImageToPAA.exe"), None, core.LOKI)
    assert not res.ok and res.error == core.MIP_TOO_LARGE and called == []
    res = core.convert(png, None, 2048, core.LOKI)  # the advice works: max size applies on top of the halving
    assert res.ok, res.error
    assert core.paa_size(png.with_suffix(".paa")) == (2048, 2048)
