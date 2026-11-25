"""The native PAA encoder (_paa, built from native/paa) and its build glue."""

import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path

import build_native
import pytest
from PIL import Image

from loki import _paa


def read_header(paa: Path) -> tuple[int, list[tuple[int, int]], dict[str, bytes]]:
    """(type word, [(w, h) per mip], {tag name: data}) from a PAA file."""
    b = paa.read_bytes()
    ty = struct.unpack_from("<H", b, 0)[0]
    pos, tags = 2, {}
    while b[pos : pos + 4] == b"GGAT":
        name = b[pos + 4 : pos + 8][::-1].decode()
        (n,) = struct.unpack_from("<I", b, pos + 8)
        tags[name] = b[pos + 12 : pos + 12 + n]
        pos += 12 + n
    pos += 2  # palette count
    mips = []
    while True:
        w, h = struct.unpack_from("<HH", b, pos)
        if w == 0 and h == 0:
            return ty, mips, tags
        (n,) = struct.unpack("<I", b[pos + 4 : pos + 7] + b"\0")
        mips.append((w & 0x7FFF, h))
        pos += 7 + n


def make_png(path: Path, size: int = 64, alpha: bool = False) -> Path:
    import random

    rnd = random.Random(1)
    mode = "RGBA" if alpha else "RGB"
    im = Image.new(mode, (size, size))
    n = 4 if alpha else 3
    im.frombytes(bytes(rnd.randrange(256) for _ in range(size * size * n)))
    im.save(path)
    return path


def hostile_png(w: int, h: int) -> bytes:
    """A 68-byte PNG that only declares w x h (the case): header, junk IDAT, IEND."""

    def chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = bytes([0x78, 0x9C, 0x63, 0, 0, 0, 1, 0, 1, 0, 0])
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    assert len(data) == 68
    return data


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("rock_co.png", True),
        ("rock_ca.png", True),
        ("ROCK_CO.PNG", True),
        ("rock.png", True),
        ("rock_nohq.png", True),
        ("rock_dt.png", True),
        ("rock_smdi.png", True),
        ("rock_as.png", True),
        ("rock_sky.png", True),  # verified under the relative checks
        ("rock_cat.png", False),
        ("rock_foo.png", True),  # unknown suffix: the default rule, as ImageToPAA does
        ("rock_co2.png", True),
        ("rock_nohq2.png", True),
        ("rock_co.jpg", False),
    ],
)
def test_supported_every_suffix_kind(name, want):
    assert _paa.supported(name) is want


# Every TexConvert suffix, with the type word ImageToPAA writes for an opaque image.
SUFFIX_TYPES = {
    "8888": 0x1555, "co": 0xFF01, "raw": 0x1555, "ca": 0xFF05, "sky": 0xFF05, "detail": 0xFF01,
    "cdt": 0xFF01, "lco": 0xFF01, "mco": 0xFF01, "draftlco": 0x1555, "lca": 0xFF05, "mask": 0xFF01,
    "pr": 0xFF01, "ads": 0xFF01, "adshq": 0xFF05, "dtsmdi": 0xFF01, "mc": 0xFF05, "as": 0xFF01,
    "sm": 0xFF01, "smdi": 0xFF01, "dt": 0xFF01, "normalmap": 0xFF01, "no": 0xFF01, "noex": 0x1555,
    "nohq": 0xFF05, "novhq": 0xFF05, "nofhq": 0xFF05, "nof": 0xFF01, "nopx": 0xFF05, "nofex": 0x1555,
    "ns": 0xFF01, "nsex": 0x1555, "nshq": 0xFF05, "gs": 0x8080, "4444": 0x4444, "1555": 0x1555,
    "88": 0x8080, "dxt1": 0xFF01, "dxt5": 0xFF05,
}
NOISE_SUFFIXES = ["cat", "can", "non"]


@pytest.mark.parametrize("suffix", sorted(SUFFIX_TYPES))
def test_encode_every_suffix_writes_the_imagetopaa_type(tmp_path, suffix):
    png = make_png(tmp_path / f"t_{suffix}.png", 32)
    _paa.encode(str(png), str(tmp_path / "o.paa"), None)
    assert read_header(tmp_path / "o.paa")[0] == SUFFIX_TYPES[suffix]
    # Routing: every suffix of the table is verified and claimed (`sky` joined in).
    assert _paa.supported(f"t_{suffix}.png") is True


@pytest.mark.parametrize("suffix", NOISE_SUFFIXES)
def test_noise_suffixes_are_refused_and_never_routed(tmp_path, suffix):
    png = make_png(tmp_path / f"t_{suffix}.png", 16)
    with pytest.raises(_paa.NeedsImageToPAA, match="noise mip filter"):
        _paa.encode(str(png), str(tmp_path / "o.paa"), None)
    assert not (tmp_path / "o.paa").exists()
    assert _paa.supported(f"t_{suffix}.png") is False


def test_needs_imagetopaa_is_a_value_error_with_a_short_reason(tmp_path):
    assert issubclass(_paa.NeedsImageToPAA, ValueError)
    png = make_png(tmp_path / "a_cat.png", 32, alpha=True)  # a noise mip filter: the one refusal left
    with pytest.raises(_paa.NeedsImageToPAA) as exc:
        _paa.encode(str(png), str(tmp_path / "o.paa"), None)
    assert str(exc.value) == "noise mip filter"


def test_swizzled_suffix_writes_swiz_tag(tmp_path):
    png = make_png(tmp_path / "t_nohq.png", 32)
    _paa.encode(str(png), str(tmp_path / "o.paa"), None)
    assert read_header(tmp_path / "o.paa")[2]["SWIZ"] == bytes([5, 4, 2, 3])


def test_encode_co_dxt1_and_ca_dxt5(tmp_path):
    co = make_png(tmp_path / "a_co.png", 64)
    ca = make_png(tmp_path / "a_ca.png", 64, alpha=True)
    im = Image.open(ca)
    im.putalpha(im.getchannel("A").point(lambda v: 255 if v >= 128 else 0))  # binary alpha: AVGC is reproduced
    im.save(ca)
    assert _paa.encode(str(co), str(tmp_path / "a_co.paa"), None) is None
    assert _paa.encode(str(ca), str(tmp_path / "a_ca.paa"), None) is None
    ty, mips, tags = read_header(tmp_path / "a_co.paa")
    assert ty == 0xFF01
    assert mips == [(64, 64), (32, 32), (16, 16), (8, 8), (4, 4)]
    assert "AVGC" in tags and tags["MAXC"] == b"\xff" * 4
    assert read_header(tmp_path / "a_ca.paa")[0] == 0xFF05


def test_soft_alpha_and_one_bit_alpha_are_encoded_not_refused(tmp_path):
    """The AVGC of soft alpha is reproduced and the DXT1 1-bit mask is Loki's own cut-out, so no alpha
    file is handed to ImageToPAA any more: a file, the type and FLAG ImageToPAA writes, and the AVGC alpha of the mean."""
    for name, ty, flag in (("a_co.png", 0xFF01, 2), ("a_ca.png", 0xFF05, 1), ("b_raw.png", 0x1555, 2)):
        png = make_png(tmp_path / name, 32, alpha=True)  # random alpha: soft
        out = tmp_path / (name + ".paa")
        assert _paa.encode(str(png), str(out), None) is None
        got_ty, _mips, tags = read_header(out)
        assert got_ty == ty and int.from_bytes(tags["FLAG"], "little") == flag, name
        assert tags["AVGC"][3] < 255
    # binary alpha in a DXT1 class: the mask is cut at 128 and the file is written
    co = Image.open(make_png(tmp_path / "c_co.png", 32, alpha=True))
    co.putalpha(co.getchannel("A").point(lambda v: 255 if v >= 128 else 0))
    co.save(tmp_path / "c_co.png")
    assert _paa.encode(str(tmp_path / "c_co.png"), str(tmp_path / "c.paa"), None) is None


def test_encode_max_size_halves(tmp_path):
    png = make_png(tmp_path / "big_co.png", 128)
    _paa.encode(str(png), str(tmp_path / "o.paa"), 32)
    assert read_header(tmp_path / "o.paa")[1][0] == (32, 32)


def test_encode_errors_are_python_exceptions(tmp_path):
    out = str(tmp_path / "o.paa")
    with pytest.raises(OSError, match="missing_co.png"):
        _paa.encode(str(tmp_path / "missing_co.png"), out, None)
    cat = make_png(tmp_path / "x_cat.png")
    with pytest.raises(_paa.NeedsImageToPAA, match="noise mip filter"):
        _paa.encode(str(cat), out, None)
    odd = make_png(tmp_path / "odd_co.png", 24)  # not a power of two
    with pytest.raises(ValueError, match="power-of-two"):
        _paa.encode(str(odd), out, None)
    ok = make_png(tmp_path / "ok_co.png", 16)
    with pytest.raises(ValueError, match="max_size"):
        _paa.encode(str(ok), out, 0)
    assert not (tmp_path / "o.paa").exists()


def test_hostile_png_header_raises_value_error_and_process_survives(tmp_path):
    for w, h in [(200_000, 200_000), (20_000, 20_000), (0, 16)]:
        png = tmp_path / f"h{w}_co.png"
        png.write_bytes(hostile_png(w, h))
        with pytest.raises(ValueError, match="dimensions|PNG size"):
            _paa.encode(str(png), str(tmp_path / "h.paa"), None)
    # still alive and encoding
    good = make_png(tmp_path / "g_co.png", 16)
    _paa.encode(str(good), str(tmp_path / "g.paa"), None)
    assert (tmp_path / "g.paa").stat().st_size > 0


def test_rust_panic_becomes_python_exception():
    with pytest.raises(RuntimeError, match="panic"):
        _paa._selftest_panic()
    assert _paa.supported("a_co.png")  # interpreter and module still fine


def test_two_encodes_in_threads_overlap_in_time(tmp_path):
    """The GIL is released for the whole encode: with it held, the second thread could not even
    take its start timestamp until the first encode had finished."""
    pngs = [make_png(tmp_path / f"t{i}_co.png", 1024) for i in range(2)]
    barrier = threading.Barrier(2)
    spans: list[tuple[float, float]] = [(0.0, 0.0)] * 2
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait()
            t0 = time.perf_counter()
            _paa.encode(str(pngs[i]), str(tmp_path / f"t{i}.paa"), None)
            spans[i] = (t0, time.perf_counter())
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    (s0, e0), (s1, e1) = spans
    assert min(e0, e1) - max(s0, s1) > 0.05, f"no overlap: {spans}"


# --- build glue (build/build_native.py, tests/conftest.py) ---------------------------------------


def test_pyd_is_fresh_after_session_start():
    assert build_native.PYD.is_file()
    assert not build_native.is_stale()


def test_target_dir_is_per_checkout_and_outside_it(monkeypatch, tmp_path):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    a, b = tmp_path / "wtA", tmp_path / "wtB"
    base = tmp_path / "Loki" / "cargo-target"
    assert build_native.target_dir(a) == build_native.target_dir(a)
    assert build_native.target_dir(a) != build_native.target_dir(b)
    assert build_native.target_dir(a).parent == base
    assert len(build_native.target_dir(a).name) == 12
    assert build_native.target_dir() == build_native.target_dir(build_native.ROOT)
    monkeypatch.setenv("CARGO_TARGET_DIR", "D:/shared")
    assert build_native.target_dir(a) == Path("D:/shared")


def test_failed_cargo_build_raises(monkeypatch, tmp_path):
    def fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 101, "out text", "boom")

    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    monkeypatch.setattr(build_native.subprocess, "run", fail)
    with pytest.raises(build_native.NativeBuildError, match="boom"):
        build_native.build(tmp_path / "_paa.pyd")
    assert not (tmp_path / "_paa.pyd").exists()


def test_missing_cargo_raises(monkeypatch, tmp_path):
    def gone(cmd, **kw):
        raise FileNotFoundError("cargo")

    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    monkeypatch.setattr(build_native.subprocess, "run", gone)
    with pytest.raises(build_native.NativeBuildError, match="cargo not found"):
        build_native.build(tmp_path / "_paa.pyd")


def test_dll_without_module_init_is_refused(monkeypatch, tmp_path):
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / build_native._cdylib_name()).write_bytes(b"MZ no init symbol here")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    monkeypatch.setattr(build_native.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    with pytest.raises(build_native.NativeBuildError, match="PyInit__paa"):
        build_native.build(tmp_path / "_paa.pyd")


def test_stale_when_missing_or_older_than_a_source(tmp_path):
    pyd = tmp_path / "_paa.pyd"
    assert build_native.is_stale(pyd)
    pyd.write_bytes(b"x")
    newest = max(p.stat().st_mtime for p in build_native.sources())
    import os

    os.utime(pyd, (newest + 10, newest + 10))
    assert not build_native.is_stale(pyd)
    os.utime(pyd, (newest - 10, newest - 10))
    assert build_native.is_stale(pyd)


def test_session_start_fails_the_session_when_the_build_fails(monkeypatch):
    import conftest

    def bad():
        raise build_native.NativeBuildError("no compiler")

    monkeypatch.setattr(conftest.build_native, "ensure", bad)
    with pytest.raises(pytest.exit.Exception, match="no compiler"):
        conftest.pytest_sessionstart(None)


def test_shipped_files():
    """The spec ships the module and the export allowlist carries native/ (never target/)."""
    root = build_native.ROOT
    assert "_paa.pyd" in (root / "build" / "loki.spec").read_text()
    ignore = (root / ".gitignore").read_text()
    assert "/src/loki/_paa.pyd" in ignore and "/native/**/target/" in ignore
