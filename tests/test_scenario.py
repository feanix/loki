"""Scenario runner tests: `python -m loki --scenario` in a subprocess, asserting digest lines.

One test per behaviour that is observable headlessly (drop goes through
App.add_files, the same handler the OS drop calls). The converter is a fake .bat tool that writes a PAA
header sized like its source png and fails for files named badcopy*, plus real-tool tests that skip
without Arma 3 Tools.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from loki import core

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

FAKE_TOOL_PY = f"""import sys
sys.path.insert(0, {str(TESTS)!r})
from pathlib import Path
from PIL import Image
from paa_helpers import paa_bytes
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
if src.stem == "badcopy":
    print("rejected")
    sys.exit(3)
dst.write_bytes(paa_bytes(*Image.open(src).size))
"""


@pytest.fixture
def env(tmp_path):
    tex, more = tmp_path / "tex", tmp_path / "more tex"
    tex.mkdir()
    more.mkdir()
    for path, size in (
        (tex / "a.png", (64, 64)),
        (tex / "b.png", (128, 64)),
        (more / "c.png", (32, 32)),
        (more / "sp ace.png", (16, 16)),
        (tex / "odd.png", (50, 30)),
    ):
        Image.new("RGBA", size, (5, 6, 7, 255)).save(path)
    (tex / "bad.png").write_bytes(Image.new("RGBA", (16, 16)).tobytes())  # not a png: flagged
    Image.new("RGBA", (16, 16)).save(tex / "badcopy.png")  # a good png the fake tool rejects
    (tex / "notes.txt").write_text("not a png")
    tool = tmp_path / "fake_tool.bat"
    helper = tmp_path / "fake_tool.py"
    helper.write_text(FAKE_TOOL_PY, encoding="utf-8")
    tool.write_text(f'@echo off\n"{sys.executable}" "{helper}" %1 %2\n')
    return {"tex": tex, "more": more, "tool": tool, "settings": tmp_path / "settings.json", "dir": tmp_path}


def run(env, script: str, settings: Path | None = None):
    scn = env["dir"] / "run.scn"
    scn.write_text(script, encoding="utf-8")
    e = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "loki", "--scenario", str(scn), "--settings", str(settings or env["settings"])],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=e, cwd=env["dir"], check=False,
    )  # fmt: skip


def blocks(out: str) -> list[list[str]]:
    return [b.splitlines() for b in out.strip().split("\n\n")]


def row(block: list[str], name: str) -> str:
    """The digest row of a file by name, with its folder cut off: 'name | max=.. | auto watch=.. | status | size'."""
    (line,) = [ln for ln in block if (os.sep + name + " | ") in ln]
    return line.strip().split(os.sep)[-1]


def status_of(cut_row: str) -> str:
    """The status column of a row() result; the whole row is the message when it does not parse."""
    cols = cut_row.split(" | ")
    assert len(cols) in (5, 6), f"unparsable digest row (status holds a path or error?): {cut_row!r}"
    return cols[3]


def rows_of(block: list[str]) -> list[str]:
    return [ln for ln in block if " | max=" in ln]


def with_tool(env, body: str) -> str:
    return f"tool {env['tool']}\nconverter bohemia\n{body}"  # the fake tool stands in for ImageToPAA


def test_criterion1_no_folder_anywhere(env):
    r = run(env, "dump\n")
    assert r.returncode == 0, r.stderr
    (d,) = blocks(r.stdout)
    assert d[0] == "controls: Converter, Convert all, All on, All off, Remove, About"
    assert d[1] == "columns: Auto watch, File, Folder, Size, Max size, Output size, Status"
    assert "  files: 0" in d
    text = r.stdout.lower()
    assert "choose folder" not in text and "subfolder" not in text and "folder:" not in text
    assert "drag png files onto the window" in text
    saved = json.loads(env["settings"].read_text(encoding="utf-8")) if env["settings"].exists() else {}
    assert not {"folder", "include_subfolders", "bake", "max_size"} & set(saved)


def test_criterion2_drop_mixed_files_adds_rows_only(env):
    tex, more = env["tex"], env["more"]
    before = sorted(p.name for p in tex.iterdir())
    r = run(
        env,
        f"drop {tex / 'a.png'} \"{more / 'c.png'}\" {tex / 'notes.txt'} {tex}\n"
        f'dump\ndrop "{more / "sp ace.png"}" {tex / "A.PNG"} {tex / "ghost.png"}\ndump\nidle\ndump\nls {tex}\n',
    )
    assert r.returncode == 0, r.stderr
    first, second, third, listing = blocks(r.stdout)
    assert [x.split(" | ")[0].split(os.sep)[-1] for x in rows_of(first)] == ["a.png", "c.png"]
    assert row(first, "a.png") == "a.png | max=off ▾ | auto watch=☐ off | needs_converting | 64x64"
    assert "status: Added 2 files; skipped 2 not PNG" in first
    names = [x.split(" | ")[0].split(os.sep)[-1] for x in rows_of(second)]
    assert names == ["a.png", "c.png", "sp ace.png"]  # the re-drop with another case added nothing
    note = "skipped 1 not found, 1 already listed" if os.name == "nt" else "skipped 1 not found"
    assert f"status: Added 1 file; {note}" in second
    assert rows_of(second) == rows_of(third)  # nothing changed by itself
    assert "ls tex: " + ", ".join(before) == listing[0]  # no .paa, nothing written


def test_criterion3_two_rows_same_source_size_own_caps_fake_tool(env):
    tex = env["tex"]
    Image.new("RGBA", (512, 256), (1, 1, 1, 255)).save(tex / "wide1.png")
    Image.new("RGBA", (512, 256), (1, 1, 1, 255)).save(tex / "wide2.png")
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'wide1.png'} {tex / 'wide2.png'}\nmaxsize {tex / 'wide2.png'} 256\ndump\n"
            f"convertall\ndump\npaainfo {tex / 'wide1.png'}\npaainfo {tex / 'wide2.png'}\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    before, after, info1, info2 = blocks(r.stdout)
    assert row(before, "wide1.png") == "wide1.png | max=off ▾ | auto watch=☐ off | needs_converting | 512x256"
    assert row(before, "wide2.png") == "wide2.png | max=256 ▾ | auto watch=☐ off | needs_converting | 512x256"
    assert row(after, "wide1.png") == "wide1.png | max=off ▾ | auto watch=☐ off | up_to_date | 512x256 | made_by=bohemia"
    assert row(after, "wide2.png") == "wide2.png | max=256 ▾ | auto watch=☐ off | up_to_date | 512x256 | made_by=bohemia"
    assert info1[0].endswith("512x256") and info2[0].endswith("256x128")


def test_criterion4_bake_converts_only_the_listed_toggled_file_at_its_cap(env):
    tex = env["tex"]
    for n in ("save1.png", "save2.png", "unlisted.png"):
        Image.new("RGBA", (512, 256), (3, 3, 3, 255)).save(tex / n)
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'save1.png'} {tex / 'save2.png'}\nmaxsize {tex / 'save1.png'} 256\n"
            f"toggle {tex / 'save1.png'}\ndump\n"
            f"touch {tex / 'save1.png'}\ntouch {tex / 'save2.png'}\ntouch {tex / 'unlisted.png'}\nidle\n"
            f"dump\npaainfo {tex / 'save1.png'}\npaainfo {tex / 'save2.png'}\nls {tex}\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    before, after, info1, info2, listing = blocks(r.stdout)
    assert row(before, "save1.png") == "save1.png | max=256 ▾ | auto watch=☑ on | needs_converting | 512x256"
    assert row(after, "save1.png") == "save1.png | max=256 ▾ | auto watch=☑ on | up_to_date | 512x256 | made_by=bohemia"
    assert row(after, "save2.png") == "save2.png | max=off ▾ | auto watch=☐ off | needs_converting | 512x256"
    assert info1[0].endswith("256x128") and info2[0].endswith("unreadable")
    assert "save1.paa" in listing[0] and "save2.paa" not in listing[0] and "unlisted.paa" not in listing[0]


def test_criterion4_all_on_all_off_and_convert_all_over_the_list(env):
    tex = env["tex"]
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'a.png'} {tex / 'odd.png'} {tex / 'bad.png'} {tex / 'badcopy.png'}\n"
            "allon\ndump\nalloff\ndump\nconvertall\ndump\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    on, off, done = blocks(r.stdout)
    assert all("auto watch=☑ on" in x for x in rows_of(on)) and len(rows_of(on)) == 4
    assert not any("auto watch=☑ on" in x for x in rows_of(off))
    assert row(done, "a.png") == "a.png | max=off ▾ | auto watch=☐ off | up_to_date | 64x64 | made_by=bohemia"
    assert row(done, "badcopy.png").startswith("badcopy.png | max=off ▾ | auto watch=☐ off | error: ImageToPAA failed (exit 3): rejected")
    assert row(done, "odd.png").split(" | ")[3].startswith("flagged: 50x30")
    assert row(done, "bad.png").split(" | ")[3].startswith("flagged: cannot read")
    assert "status: Converted 1 file(s), 1 failed" in done


def test_criterion5_remove_leaves_files_and_stops_watching(env):
    tex = env["tex"]
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'a.png'} {tex / 'b.png'}\nconvertall\nls {tex}\n"
            f"toggle {tex / 'a.png'}\nremove {tex / 'a.png'}\ndump\nls {tex}\n"
            # a real wait: a removed file is not watched, so idle would return at once and prove nothing
            f"touch {tex / 'a.png'}\nwait 0.7\nls {tex}\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    ls_before, dump, ls_after, ls_saved = blocks(r.stdout)
    assert [x.split(" | ")[0].split(os.sep)[-1] for x in rows_of(dump)] == ["b.png"]
    assert "a.paa" in ls_before[0] and ls_after == ls_before  # the png and its paa are untouched
    assert ls_saved == ls_before  # saved while off the list: not converted again
    saved = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert [os.path.basename(f["path"]) for f in saved["files"]] == ["b.png"]


def test_criterion6_persistence_missing_and_recovery(env):
    tex = env["tex"]
    hidden = env["dir"] / "hidden.png"
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'a.png'} {tex / 'b.png'} \"{env['more'] / 'c.png'}\"\n"
            f"maxsize {tex / 'b.png'} 256\ntoggle {tex / 'a.png'}\ndump\n"
            f"move {tex / 'b.png'} {hidden}\nrestart\ndump\n"
            f"move {hidden} {tex / 'b.png'}\nidle\ndump\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    before, gone, back = blocks(r.stdout)
    assert rows_of(gone)[0] == rows_of(before)[0]
    assert row(gone, "b.png") == "b.png | max=256 ▾ | auto watch=☐ off | missing | ?"
    assert row(gone, "a.png").startswith("a.png | max=off ▾ | auto watch=☑ on")
    assert "status: 3 png: 2 need converting, 0 flagged, 1 missing, 0 errors; auto watch on for 1" in gone
    assert row(back, "b.png") == "b.png | max=256 ▾ | auto watch=☐ off | needs_converting | 128x64"
    # and a brand new process (second run, same settings file) sees the same list
    r2 = run(env, "dump\n")
    assert [x.split(" | ")[:3] for x in rows_of(blocks(r2.stdout)[0])] == [x.split(" | ")[:3] for x in rows_of(back)]


def order_of(block: list[str]) -> list[str]:
    """File names of the digest rows, in display order."""
    return [x.strip().split(" | ")[0].split(os.sep)[-1] for x in rows_of(block)]


def test_sort_default_is_file_name_natural_case_insensitive(env):
    nat = env["dir"] / "nat"
    nat.mkdir()
    for n in ("tex10.png", "Tex1.png", "tex2.png"):
        Image.new("RGBA", (16, 16)).save(nat / n)
    r = run(env, f"drop {nat / 'tex10.png'} {nat / 'Tex1.png'} {nat / 'tex2.png'}\ndump\n")
    assert r.returncode == 0, r.stderr
    (d,) = blocks(r.stdout)
    assert order_of(d) == ["Tex1.png", "tex2.png", "tex10.png"] and "sort: File asc" in d


def test_sort_every_column_both_directions(env):
    tex, more = env["tex"], env["more"]
    files = [tex / "a.png", tex / "b.png", tex / "odd.png", more / "c.png", more / "sp ace.png"]
    caps = {"a.png": "256", "b.png": "512", "odd.png": "off", "c.png": "1024", "sp ace.png": "2048"}
    setup = "drop " + " ".join(f'"{f}"' for f in files) + "\n"
    setup += "".join(f'maxsize "{f}" {caps[f.name]}\n' for f in files)
    setup += f'toggle "{tex / "a.png"}"\ntoggle "{more / "c.png"}"\n'
    cmds = ["sort file asc", "sort file desc", "sort folder asc", "sort folder desc", "sort size asc", "sort size desc",
            "sort max size asc", "sort max size desc", "sort auto watch asc", "sort auto watch desc",
            "sort status asc", "sort status desc"]  # fmt: skip
    r = run(env, with_tool(env, setup + "".join(f"{c}\ndump\n" for c in cmds)))
    assert r.returncode == 0, r.stderr
    got = dict(zip(cmds, blocks(r.stdout), strict=True))
    want = {
        "file asc": ["a.png", "b.png", "c.png", "odd.png", "sp ace.png"],
        "file desc": ["sp ace.png", "odd.png", "c.png", "b.png", "a.png"],
        "folder asc": ["c.png", "sp ace.png", "a.png", "b.png", "odd.png"],  # "more tex" before "tex"
        "folder desc": ["a.png", "b.png", "odd.png", "c.png", "sp ace.png"],  # ties by name stay ascending
        "size asc": ["sp ace.png", "c.png", "odd.png", "a.png", "b.png"],
        "size desc": ["b.png", "a.png", "odd.png", "c.png", "sp ace.png"],
        "max size asc": ["a.png", "b.png", "c.png", "sp ace.png", "odd.png"],  # Off counts as largest
        "max size desc": ["odd.png", "sp ace.png", "c.png", "b.png", "a.png"],
        "auto watch asc": ["b.png", "odd.png", "sp ace.png", "a.png", "c.png"],  # Off before On
        "auto watch desc": ["a.png", "c.png", "b.png", "odd.png", "sp ace.png"],
        "status asc": ["odd.png", "a.png", "b.png", "c.png", "sp ace.png"],  # flagged (60x40 is not a power of 2)
        "status desc": ["a.png", "b.png", "c.png", "sp ace.png", "odd.png"],
    }  # fmt: skip
    for cmd, block in got.items():
        col, _, way = cmd.removeprefix("sort ").rpartition(" ")
        assert order_of(block) == want[cmd[5:]], cmd
        assert f"sort: {col} {way}" in "\n".join(block).lower()  # the dump names the active sort


def test_sort_status_change_mid_run_does_not_reorder(env):
    tex = env["tex"]
    r = run(
        env,
        with_tool(
            env,
            f"drop {tex / 'a.png'} {tex / 'badcopy.png'}\nsort status asc\ndump\nconvertall\ndump\n"
            "sort status asc\ndump\n",
        ),
    )
    assert r.returncode == 0, r.stderr
    before, after, resorted = blocks(r.stdout)
    assert order_of(before) == ["a.png", "badcopy.png"]  # both need converting: name order
    assert order_of(after) == ["a.png", "badcopy.png"]  # a is up to date, badcopy an error: nothing moved
    assert "error" in row(after, "badcopy.png") and "up_to_date" in row(after, "a.png")
    assert order_of(resorted) == ["badcopy.png", "a.png"]  # the next header click applies the order


def test_sort_is_remembered_across_restart_and_in_settings(env):
    tex = env["tex"]
    r = run(env, f"drop {tex / 'a.png'} {tex / 'b.png'}\nsort size desc\nrestart\ndump\n")
    assert r.returncode == 0, r.stderr
    (d,) = blocks(r.stdout)
    assert "sort: Size desc" in d and order_of(d) == ["b.png", "a.png"]
    assert json.loads(env["settings"].read_text(encoding="utf-8"))["sort"] == {"column": "size", "descending": True}


def test_sort_unknown_column_exits_2(env):
    r = run(env, "sort nonsense\n")
    assert r.returncode == 2 and "unknown column" in r.stderr


def test_criterion6_a_0_1_0_settings_file_loads(env):
    env["settings"].write_text(
        json.dumps({"folder": str(env["tex"]), "include_subfolders": True, "max_size": 512,
                    "imagetopaa_path": str(env["tool"]), "bake": {str(env["tex"]): {"a.png": True}}}),
        encoding="utf-8",
    )  # fmt: skip
    r = run(env, "dump\n")
    assert r.returncode == 0, r.stderr
    (d,) = blocks(r.stdout)
    assert rows_of(d) == [] and "  files: 0" in d and "tool_found: yes" in "\n".join(d)
    assert str(env["tex"]) not in r.stdout


def test_unknown_scenario_command_exits_2(env):
    r = run(env, "frobnicate\n")
    assert r.returncode == 2 and "unknown command: frobnicate" in r.stderr
    r = run(env, "folder C:/x\n")  # the old folder commands are gone
    assert r.returncode == 2 and "unknown command: folder" in r.stderr


@pytest.mark.e2e
def test_real_tool_two_rows_same_size_own_caps(env):
    try:
        real = core.find_imagetopaa()
    except core.ToolNotFound as exc:
        pytest.skip(f"ImageToPAA not available: {exc}")
    tex = env["dir"] / "maxtex"
    tex.mkdir()
    Image.new("RGB", (512, 256), (9, 9, 9)).save(tex / "grass_co.png")
    Image.new("RGB", (512, 256), (9, 9, 9)).save(tex / "rock_co.png")
    script = (
        f"tool {real}\nconverter bohemia\ndrop {tex / 'grass_co.png'} {tex / 'rock_co.png'}\nmaxsize {tex / 'rock_co.png'} 256\n"
        f"convertall\ndump\npaainfo {tex / 'grass_co.png'}\npaainfo {tex / 'rock_co.png'}\n"
        f"maxsize {tex / 'grass_co.png'} 256\ndump\nconvertall\ndump\npaainfo {tex / 'grass_co.png'}\n"
    )
    r = run(env, script)
    assert r.returncode == 0, r.stderr
    done, g, rk, stale, fresh, g2 = blocks(r.stdout)
    assert g == [f"paa {tex / 'grass_co.png'}: 512x256"] and rk == [f"paa {tex / 'rock_co.png'}: 256x128"]
    assert row(done, "grass_co.png") == "grass_co.png | max=off ▾ | auto watch=☐ off | up_to_date | 512x256 | made_by=bohemia"
    assert row(stale, "grass_co.png") == (
        "grass_co.png | max=256 ▾ | auto watch=☐ off | needs_converting: PAA is 512x256, max size wants 256x128 | 512x256 | made_by=bohemia"
    )
    # parsed columns: a status holding an error text (or a path) fails with the whole row shown, not an IndexError
    assert status_of(row(stale, "rock_co.png")) == "up_to_date"
    assert status_of(row(fresh, "grass_co.png")) == "up_to_date"
    assert g2 == [f"paa {tex / 'grass_co.png'}: 256x128"]


@pytest.mark.e2e
def test_real_tool_8192_png_is_up_to_date_after_one_convert_all(env):
    try:
        real = core.find_imagetopaa()
    except core.ToolNotFound as exc:
        pytest.skip(f"ImageToPAA not available: {exc}")
    png = env["dir"] / "huge_co.png"
    Image.new("RGB", (8192, 8192), (9, 9, 9)).save(png)
    script = (
        f"tool {real}\nconverter loki\ndrop {png}\nconvertall\ndump\npaainfo {png}\nconvertall\ndump\n"
    )
    r = run(env, script)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "paa " + str(png) + ": 4096x4096" in out
    assert out.count("up_to_date") >= 2 and "needs_converting" not in out
    assert "Converted 1 file(s)" in out and "Converted 0 file(s)" in out


def test_about_command_and_out_file(tmp_path):
    from loki.__main__ import main

    scn = tmp_path / "a.scn"
    scn.write_text("about\n", encoding="utf-8")
    log = tmp_path / "log.txt"
    assert main(["--scenario", str(scn), "--settings", str(tmp_path / "s.json"), "--out", str(log)]) == 0
    text = log.read_text(encoding="utf-8")
    assert "MIT + Commons Clause" in text and "© 2026 Astrid Winters" in text and "Loki " in text and "Drag PNG" in text


def test_crash_handler_headless_writes_log_no_dialog(tmp_path, monkeypatch, capsys):
    import loki.__main__ as m

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(m, "_run", lambda argv=None: 1 / 0)
    assert m.main(["--scenario", "x.scn"]) == 1  # headless: a Tk dialog here would hang the test
    text = (tmp_path / "Loki" / "crash.log").read_text(encoding="utf-8")
    assert "ZeroDivisionError" in text and text.startswith("Loki ")
