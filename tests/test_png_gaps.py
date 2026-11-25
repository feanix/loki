"""Python side of the encoder test-gap bundle.

What is here and why:
* tRNS colour keys end to end (item 3) and the 16-bit key scaling in core._to_8bit_rgb (review).
* Sides below 4 and error detail at the Python boundary (lock, missing file, panic text, bad PLTE).
* The non-power-of-two refusal text (the 8192 hand-over is test_loki_converter.test_route).

Inputs are generated here. ImageToPAA-backed tests skip without Arma 3 Tools."""

import struct
from pathlib import Path

import pytest
from PIL import Image

from lockhelp import locked, windows_only
from loki import _paa, core
from test_handover_normalise import (  # noqa: F401 (paa_proto is a fixture)
    TOOL,
    N,
    decode,
    needs_tool,
    paa_proto,
    write_png,
)

# --- item 3: tRNS colour key on an RGB PNG ------------------------------------------------------------------


def _rgb8key(p: Path) -> list[list[int]]:
    """64x64 RGB8 with a colour key; a 16x16 square is exactly the key. Returns the ideal alpha per row."""
    key = (12, 200, 99)
    rows, alpha = [], []
    for y in range(N):
        row, arow = [], []
        for x in range(N):
            keyed = 16 <= x < 32 and 16 <= y < 32
            row += list(key) if keyed else [(x * 3) % 256, (y * 4) % 256, 77]  # blue 77 never equals the key's 99
            arow.append(0 if keyed else 255)
        rows.append(row)
        alpha.append(arow)
    write_png(p, rows, 8, 2, trns=struct.pack(">HHH", *key))
    return alpha


def _alpha_error(got, alpha) -> tuple[float, int]:
    """(mean alpha shift, number of texels whose alpha differs by more than 1 level)."""
    flat = [(g[3], a) for gr, ar in zip(got, alpha, strict=True) for g, a in zip(gr, ar, strict=True)]
    return sum(g - a for g, a in flat) / len(flat), sum(abs(g - a) > 1 for g, a in flat)


def test_colour_key_rgb_as_ca_keeps_the_cut_out_in_the_loki_encoder(tmp_path, paa_proto):  # noqa: F811
    """Loki's own `_ca` route turns the key into alpha 0: alpha mean within 1 level of the ideal (F3)."""
    png = tmp_path / "key_ca.png"
    alpha = _rgb8key(png)
    res = core.convert(png, None, None, core.LOKI)
    assert res.ok and res.converter == core.LOKI, res.error
    shift, wrong = _alpha_error(decode(paa_proto, png.with_suffix(".paa"), tmp_path / "back.png"), alpha)
    assert abs(shift) <= 1 and wrong == 0, (shift, wrong)


def test_colour_key_rgb_as_co_keeps_the_cut_out_in_the_loki_encoder(tmp_path, paa_proto):  # noqa: F811
    """`_co` has 1-bit alpha: Loki cuts its own mask, so it converts without the tool and the
    16x16 hole of the key is clear (it lies on block edges, so the mask is exact)."""
    png = tmp_path / "key_co.png"
    alpha = _rgb8key(png)
    res = core.convert(png, None, None, core.LOKI)
    assert res.ok and res.converter == core.LOKI, res.error
    shift, wrong = _alpha_error(decode(paa_proto, png.with_suffix(".paa"), tmp_path / "back.png"), alpha)
    assert abs(shift) <= 1 and wrong == 0, (shift, wrong)


@needs_tool
def test_colour_key_rgb_as_co_through_imagetopaa_keeps_the_cut_out(tmp_path, paa_proto):  # noqa: F811
    """The hand-over copy turns the key into real alpha, which ImageToPAA ignores as a key: the DXT1 mask
    has the 16x16 hole (D-G-3 was alpha +15.9 on the whole image)."""
    png = tmp_path / "key_co.png"
    alpha = _rgb8key(png)
    res = core.convert(png, TOOL, None, core.BOHEMIA)  # the Bohemia converter is chosen: the hand-over copy path
    assert res.ok and res.converter == core.BOHEMIA, res.error
    shift, wrong = _alpha_error(decode(paa_proto, png.with_suffix(".paa"), tmp_path / "back.png"), alpha)
    assert abs(shift) <= 1 and wrong == 0, (shift, wrong)


# --- the 16-bit colour key in core._to_8bit_rgb ---------------------------------------------------


def test_16bit_rgb_colour_key_is_scaled_to_8_bit_and_the_keyed_texels_are_transparent(tmp_path):
    """Pillow keeps the key as 16-bit values while the pixels are 8-bit, so the key is scaled by >> 8. Without
    the scaling no texel matches and the copy has no transparency (ImageToPAA ignores a key itself)."""
    key = (0x1234, 0x5678, 0x9ABC)
    row = [*key, 0x1234, 0x5678, 0x9B00, 0x0001, 0x0002, 0x0003, *key]  # keyed, opaque (blue differs), opaque, keyed
    png = write_png(tmp_path / "k16.png", [row], 16, 2, trns=struct.pack(">HHH", *key))
    with Image.open(png) as im:
        assert im.info["transparency"] == key  # the premise: 16-bit key, not yet scaled
    tdir = tmp_path / "t"
    tdir.mkdir()
    src = core._imagetopaa_input(png, tdir, None)
    assert src != png
    with Image.open(src) as im:
        assert im.mode == "RGBA"
        assert [im.getpixel((x, 0)) for x in range(4)] == [
            (0x12, 0x56, 0x9A, 0), (0x12, 0x56, 0x9B, 255), (0x00, 0x00, 0x00, 255), (0x12, 0x56, 0x9A, 0),
        ]  # fmt: skip


def test_16bit_grey_colour_key_is_scaled_to_8_bit_and_the_keyed_texels_are_transparent(tmp_path):
    """The `I;16` branch of the same helper: the integer key is scaled with the pixels' high byte."""
    png = write_png(tmp_path / "g16k.png", [[0x8012, 0x8100, 0x0000, 0x8012]], 16, 0, trns=struct.pack(">H", 0x8012))
    tdir = tmp_path / "t"
    tdir.mkdir()
    with Image.open(core._imagetopaa_input(png, tdir, None)) as im:
        assert im.mode == "RGBA"
        assert [im.getpixel((x, 0)) for x in range(4)] == [
            (0x80, 0x80, 0x80, 0), (0x81, 0x81, 0x81, 255), (0, 0, 0, 255), (0x80, 0x80, 0x80, 0),
        ]  # fmt: skip


# --- item 5: sizes the encoder cannot take, with the tool present ----------------------------------------------


def test_non_power_of_two_png_is_refused_with_a_clear_text_and_not_handed_over(tmp_path, monkeypatch):
    """Both converters refuse a 100x100 texture (D-B-3); route() still picks Loki (it never looks at the
    size), and Loki's message names the rule."""
    called = []
    monkeypatch.setattr(core, "_run_tool", lambda *a: called.append(a))
    png = tmp_path / "odd_co.png"
    Image.new("RGB", (100, 100), (9, 9, 9)).save(png)
    assert core.route(png.name, core.LOKI, True) == core.LOKI
    res = core.convert(png, Path("ImageToPAA.exe"), None, core.LOKI)
    assert not res.ok and not res.needs_tool and called == []
    assert res.error == "Loki converter: size 100x100: only power-of-two sizes are supported"


# --- item 4 (F9): a lock inside `_paa.encode` -------------------------------------------------------------------


@windows_only
def test_paa_encode_reports_a_locked_png_as_a_lock(tmp_path):
    png = tmp_path / "a_co.png"
    Image.new("RGB", (8, 8)).save(png)
    with locked(png), pytest.raises(OSError) as err:
        _paa.encode(str(png), str(tmp_path / "a.paa"), None)
    assert core.is_lock_error(err.value), (err.value, err.value.errno, getattr(err.value, "winerror", None))


@windows_only
def test_a_lock_taken_between_the_probe_and_the_encoder_is_retried(tmp_path, monkeypatch):
    png = tmp_path / "a_co.png"
    Image.new("RGB", (8, 8)).save(png)
    real = _paa.encode

    def locked_encode(*args):
        with locked(png):
            return real(*args)

    monkeypatch.setattr(_paa, "encode", locked_encode)
    res = core.convert(png, None, None, core.LOKI)
    assert not res.ok and res.retry, (res.error, res.retry)


def test_paa_encode_maps_a_missing_file_to_file_not_found(tmp_path):
    missing = tmp_path / "nope_co.png"
    with pytest.raises(FileNotFoundError) as err:
        _paa.encode(str(missing), str(tmp_path / "x.paa"), None)
    assert err.value.errno is not None and err.value.strerror and err.value.filename == str(missing)


def test_a_panic_is_reported_with_its_message():
    with pytest.raises(RuntimeError, match="selftest panic"):
        _paa._selftest_panic()


def test_a_bad_plte_is_a_value_error_not_a_panic(tmp_path):
    """F8: a 25-byte PLTE panicked inside the png crate; load_png now rejects it first."""
    import zlib

    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    raw = b"".join(b"\0" + b"\0" * 8 for _ in range(8))  # 8x8, 8-bit indexed, every index 0
    png = tmp_path / "plte_co.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", b"\x09" * 25) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="PLTE length 25"):
        _paa.encode(str(png), str(tmp_path / "x.paa"), None)


def test_mip_too_large_keeps_its_own_exception():
    assert issubclass(_paa.MipTooLarge, ValueError) and _paa.MipTooLarge is not ValueError


# --- item 6 (F4): sides below 4 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["_raw", "_4444", "_gs", ""])
@pytest.mark.parametrize("size", [(1, 1), (2, 2), (1, 256), (256, 1)])
def test_tiny_sides_give_a_file_or_a_hand_over_never_a_value_error(tmp_path, suffix, size):
    """ImageToPAA converts these classes at these sizes; `NeedsImageToPAA` (hand over) or a PAA are the
    answers. The 256x1 text must not blame a power of two (it is a power of two)."""
    png = tmp_path / f"tiny{suffix}.png"
    Image.new("RGB", size, (50, 60, 70)).save(png)
    out = tmp_path / "tiny.paa"
    try:
        _paa.encode(str(png), str(out), None)
    except _paa.NeedsImageToPAA:
        return
    assert out.is_file()
