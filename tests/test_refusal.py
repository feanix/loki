"""Per-file encoder refusal and routing per suffix (#025, criterion 5).

The encoder alone decides what it refuses (`_paa.NeedsImageToPAA`); Python never predicts it. So the
expectations below come from `_paa.supported` and from asking `_paa.encode` itself, never from a suffix
list written here. The suffixes are the ones Bohemia's own TexConvert.cfg names (read from the tool's folder).
Tests that run ImageToPAA are skipped with a reason when it is not installed.
"""

import re
import time
from pathlib import Path

import pytest
from PIL import Image

from loki import _paa, cli, core
from loki import app as app_mod

try:
    TOOL = core.find_imagetopaa()
    WHY = ""
except core.ToolNotFound as exc:
    TOOL = None
    WHY = str(exc)

needs_tool = pytest.mark.skipif(TOOL is None, reason=f"ImageToPAA not available: {WHY}")


def tool_gone(_override):
    raise core.ToolNotFound("ImageToPAA not found: forced missing")


def soft_alpha_png(path: Path, size: int = 64) -> Path:
    im = Image.effect_noise((size, size), 60).convert("RGBA")
    im.putalpha(Image.effect_noise((size, size), 90).convert("L"))  # noise: soft alpha, not 0/255
    im.save(path)
    return path


def bit_alpha_png(path: Path, size: int = 64) -> Path:
    im = Image.effect_noise((size, size), 60).convert("RGBA")
    im.putalpha(Image.effect_noise((size, size), 90).convert("L").point(lambda v: 255 if v >= 128 else 0))
    im.save(path)
    return path


def encoder_refuses(png: Path, tmp_path: Path) -> str | None:
    """The encoder's own answer: its short reason, or None when it encodes the file."""
    try:
        _paa.encode(str(png), str(tmp_path / "probe.paa"), None)
    except _paa.NeedsImageToPAA as exc:
        return str(exc)
    finally:
        (tmp_path / "probe.paa").unlink(missing_ok=True)
    return None


def make_app(tmp_path, find_tool):
    return app_mod.App(tmp_path / "settings.json", find_tool=find_tool, interval=0.02, settle=0.1, workers=4)


def run_all(app, timeout=240.0):
    assert app.convert_all()
    end = time.monotonic() + timeout
    while app.converting:
        app.pump()
        assert time.monotonic() < end, "convert all timed out"
        time.sleep(0.02)
    app.pump()


# --- core.convert: the refusal falls back ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["soft_ca.png", "mask_co.png"])  # soft alpha; DXT1 1-bit alpha
def test_refused_file_is_not_converted_and_says_why_without_the_tool(tmp_path, name):
    png = (soft_alpha_png if name == "soft_ca.png" else bit_alpha_png)(tmp_path / name)
    why = encoder_refuses(png, tmp_path)
    assert why in ("soft alpha", "1-bit alpha")
    res = core.convert(png, None, None, core.LOKI)
    assert not res.ok and res.needs_tool and not res.retry
    assert res.error == f"needs Arma 3 Tools ({why})"
    assert res.converter is None
    assert not png.with_suffix(".paa").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]  # no temp folder left


@needs_tool
@pytest.mark.parametrize("name", ["soft_ca.png", "mask_co.png"])
def test_refused_file_is_converted_by_imagetopaa_when_found(tmp_path, name):
    png = (soft_alpha_png if name == "soft_ca.png" else bit_alpha_png)(tmp_path / name)
    assert encoder_refuses(png, tmp_path)
    res = core.convert(png, TOOL, None, core.LOKI)
    assert res.ok and res.converter == core.BOHEMIA and not res.needs_tool, res.error
    assert core.check_png(png, None).status == core.UP_TO_DATE
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([name, name.replace(".png", ".paa")])


@needs_tool
def test_refusal_fallback_honours_max_size(tmp_path):
    png = soft_alpha_png(tmp_path / "big_ca.png", 128)
    res = core.convert(png, TOOL, 64, core.LOKI)
    assert res.ok and res.converter == core.BOHEMIA, res.error
    assert core.check_png(png, 64).status == core.UP_TO_DATE


# --- the command line -------------------------------------------------------------------------------------


def test_cli_prints_a_refusal_as_needs_tool_and_counts_it(tmp_path, monkeypatch, capsys):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)
    png = soft_alpha_png(tmp_path / "soft_ca.png")
    ok = tmp_path / "plain_co.png"
    Image.effect_noise((64, 64), 60).convert("RGB").save(ok)
    code = cli.main(["convert", str(png), str(ok)])
    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"needs-tool\s+.*soft_ca\.png: needs Arma 3 Tools \(soft alpha\)", out)
    assert "converted" in out and "1 need Arma 3 Tools, 0 errors" in out
    assert not png.with_suffix(".paa").exists() and ok.with_suffix(".paa").exists()


# --- criterion 5: one generated PNG per suffix the cfg knows, tool present and absent ----------------------


def cfg_suffixes() -> list[str]:
    cfg = TOOL.parent / "TexConvert.cfg" if TOOL else None
    if cfg is None or not cfg.is_file():
        pytest.skip("TexConvert.cfg not available (needs Arma 3 Tools)")
    return sorted(set(re.findall(r'name\s*=\s*"\*_(\w+)\.\*"', cfg.read_text(errors="replace"))))


@pytest.fixture(scope="module")
def suffix_set(tmp_path_factory):
    """One opaque RGB and one soft-alpha RGBA PNG per suffix the cfg knows, plus a suffix-less file."""
    root = tmp_path_factory.mktemp("suffixes")
    files = {}
    for kind in ("opaque", "soft"):
        d = root / kind
        d.mkdir()
        for suf in [*cfg_suffixes(), None]:
            p = d / (f"t_{suf}.png" if suf else "t.png")
            if kind == "opaque":
                Image.effect_noise((64, 64), 60).convert("RGB").save(p)
            else:
                soft_alpha_png(p)
            files[p] = kind
    return root, files


def expected(png: Path, scratch: Path):
    """(made_by, row text) the router and the encoder's own answer give for this file."""
    if not _paa.supported(png.name):
        return None, f"needs Arma 3 Tools for {core.suffix_of(png)}" if core.suffix_of(png) else None
    why = encoder_refuses(png, scratch)
    return (None, f"needs Arma 3 Tools ({why})") if why else (core.LOKI, None)


def copy_set(suffix_set, dest: Path) -> list[Path]:
    _root, files = suffix_set
    out = []
    for p in files:
        q = dest / p.parent.name / p.name
        q.parent.mkdir(exist_ok=True)
        q.write_bytes(p.read_bytes())
        out.append(q)
    return out


def test_every_cfg_suffix_with_the_tool_absent(tmp_path, suffix_set):
    pngs = copy_set(suffix_set, tmp_path)
    app = make_app(tmp_path, tool_gone)
    try:
        app.add_files(pngs)
        run_all(app)
        rows = {Path(r.path): r for r in app.rows}
        seen = set()
        for p in pngs:
            made, text = expected(p, tmp_path)
            row = rows[p]
            if made:
                assert row.status == core.UP_TO_DATE and row.made_by == made, (p.name, row.status_text)
                assert p.with_suffix(".paa").exists()
            else:
                assert row.status == core.NEEDS_TOOL and row.made_by is None, (p.name, row.status)
                assert not p.with_suffix(".paa").exists()
                if text:
                    assert row.status_text == text, p
                else:
                    assert row.status_text.startswith("needs Arma 3 Tools")
            seen.add((made is not None, text is not None))
        assert {(True, False), (False, True)} <= seen  # both outcomes occurred in the set
        assert any("soft alpha" in r.status_text for r in rows.values())  # a per-file refusal reached the row
    finally:
        app.close()


@needs_tool
def test_every_cfg_suffix_with_the_tool_present(tmp_path, suffix_set):
    pngs = copy_set(suffix_set, tmp_path)
    app = make_app(tmp_path, lambda _o: TOOL)
    try:
        app.add_files(pngs)
        run_all(app)
        rows = {Path(r.path): r for r in app.rows}
        for p in pngs:
            made, _ = expected(p, tmp_path)
            row = rows[p]
            assert row.status == core.UP_TO_DATE, (p.name, row.status_text)
            assert row.made_by == (made or core.BOHEMIA), (p.name, row.made_by)
        assert {r.made_by for r in rows.values()} == {core.LOKI, core.BOHEMIA}
    finally:
        app.close()
