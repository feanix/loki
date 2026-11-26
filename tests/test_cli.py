"""The command line: inputs, routing, output lines, exit codes and the held-.paa wait.

Runs loki.cli.main in-process with the real Loki encoder; ImageToPAA is never needed (find_imagetopaa is
patched to "absent"), and a real CreateFileW hold stands in for TexView or an exporter.
"""

import os
import re
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from lockhelp import Lock, windows_only
from loki import __main__ as loki_main
from loki import cli, core, settings


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        if override:
            if Path(override).is_file():
                return Path(override)
            raise core.ToolNotFound(f"ImageToPAA not found at the configured path: {override}")
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)
    monkeypatch.setattr(core, "_REPLACE_PAUSE", 0.001)
    monkeypatch.setattr(cli, "RETRY_PASS", 0.05)


def png(path: Path, size=(64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise(size, 60).convert("RGB").save(path)
    return path


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_help_and_version_exit_0(capsys):
    code, out, _ = run(capsys, "--help")
    assert code == 0
    code, out, _ = run(capsys, "convert", "--help")  # the 0.3.0 form prints the same screen
    assert code == 0 and out == run(capsys, "--help")[1]
    for opt in ("--max-size", "--imagetopaa", "--jobs", "--skip-up-to-date", "--recursive", "--wait-locked", "--quiet"):
        assert opt in out
    assert "--converter" not in out
    code, out, _ = run(capsys, "--version")
    assert code == 0 and out.startswith("Loki ")


def test_file_glob_folder_and_recursive_inputs(tmp_path, capsys):
    a = png(tmp_path / "one" / "a_co.png")
    png(tmp_path / "one" / "sub" / "b_co.png")
    png(tmp_path / "two" / "c_co.png")
    code, out, _ = run(capsys, "convert", str(a))
    assert code == 0 and out.count("converted ") == 1 and (tmp_path / "one" / "a_co.paa").is_file()
    code, out, _ = run(capsys, "convert", str(tmp_path / "two" / "*_co.png"))
    assert code == 0 and (tmp_path / "two" / "c_co.paa").is_file()
    code, out, _ = run(capsys, "convert", str(tmp_path / "one"))
    assert code == 0 and not (tmp_path / "one" / "sub" / "b_co.paa").exists()
    code, out, _ = run(capsys, "convert", str(tmp_path / "one"), "--recursive")
    assert code == 0 and (tmp_path / "one" / "sub" / "b_co.paa").is_file()
    assert out.rstrip().splitlines()[-1].startswith("done: 2 converted, 0 skipped, 0 flagged, 0 need Arma 3 Tools, 0 errors")


def test_line_format_and_converter_named(tmp_path, capsys):
    p = png(tmp_path / "wall_co.png", (512, 512))
    code, out, _ = run(capsys, "convert", str(p), "--max-size", "256")
    line = out.splitlines()[0]
    assert code == 0 and line.startswith("converted  ") and "wall_co.png -> wall_co.paa (256x256, loki, " in line
    assert line.endswith(" s)")
    assert core.paa_size(tmp_path / "wall_co.paa") == (256, 256)


@pytest.mark.usefixtures("cat_is_handed_over")
def test_flagged_needs_tool_and_error_lines_exit_1(tmp_path, capsys):
    odd = png(tmp_path / "odd_co.png", (300, 200))
    cat = png(tmp_path / "rock_cat.png")
    code, out, _ = run(capsys, "convert", str(odd), str(cat), str(tmp_path / "gone.png"))
    assert code == 1
    assert "flagged    " in out and "odd_co.png: 300x200: sides must be powers of two" in out
    assert "needs-tool " in out and "rock_cat.png: needs Arma 3 Tools for _cat" in out
    assert "error      " in out and "gone.png: file not found" in out
    assert out.splitlines()[-1].startswith("done: 0 converted, 0 skipped, 1 flagged, 1 need Arma 3 Tools, 1 errors")
    assert not (tmp_path / "odd_co.paa").exists() and not (tmp_path / "rock_cat.paa").exists()


def test_skip_up_to_date_and_quiet(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    assert run(capsys, "convert", str(p))[0] == 0
    code, out, _ = run(capsys, "convert", str(p), "--skip-up-to-date")
    assert code == 0 and out.startswith("skipped    ") and "0 converted, 1 skipped" in out
    code, out, _ = run(capsys, "convert", str(p), "--quiet")  # converts again without --skip-up-to-date
    assert code == 0 and out.count("\n") == 1 and out.startswith("done: 1 converted")


@pytest.mark.parametrize(
    "argv",
    [["convert"], ["convert", "--max-size", "999", "x.png"], ["convert", "--bogus", "x.png"], ["convert", "--jobs", "0", "x.png"],
     ["convert", "--converter", "loki", "x.png"], ["convert", "--wait-locked", "-1", "x.png"], ["--quiet"], ["--bogus", "x.png"],
     ["--max-size", "999", "x.png"], []],
)
def test_usage_errors_exit_2(argv, capsys):
    code, _out, err = run(capsys, *argv)
    assert code == 2 and err


def test_no_png_matched_exits_2(tmp_path, capsys):
    code, _out, err = run(capsys, "convert", str(tmp_path / "*.png"))
    assert code == 2 and "no PNG" in err
    code, _out, err = run(capsys, "convert", str(tmp_path))
    assert code == 2


def test_converter_option_is_gone(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    code, _out, err = run(capsys, "convert", str(p), "--converter", "loki")
    assert code == 2 and "unrecognized arguments" in err and "--converter" in err
    assert not (tmp_path / "a_co.paa").exists()


def test_bad_imagetopaa_path_is_a_usage_error(tmp_path, capsys):
    p = png(tmp_path / "a_co.png")
    code, _out, err = run(capsys, "convert", str(p), "--imagetopaa", str(tmp_path / "nope.exe"))
    assert code == 2 and "not found" in err
    assert not (tmp_path / "a_co.paa").exists()


@pytest.mark.usefixtures("cat_is_handed_over")
def test_imagetopaa_option_path_is_used_for_a_handover_file(tmp_path, capsys, monkeypatch):
    p = png(tmp_path / "rock_cat.png")
    fake = tmp_path / "ImageToPAA.exe"
    fake.write_bytes(b"")
    called = []

    def tool(exe, src, dst):
        called.append(Path(exe))
        Path(dst).write_bytes(b"paa from the tool")

    monkeypatch.setattr(core, "_run_tool", tool)
    code, out, _ = run(capsys, "convert", str(p), "--imagetopaa", str(fake))
    assert code == 0 and called == [fake] and "bohemia" in out.splitlines()[0]


def test_help_names_no_handover_suffix_because_the_encoder_rejects_none(capsys):
    from loki import _paa

    _code, out, _ = run(capsys, "convert", "--help")
    text = " ".join(out.split())
    assert "ImageToPAA" in text
    named = re.findall(r"(?<![\w])(_[a-z]+)\b", cli.HANDOVER_CASES)
    assert named == []  # the encoder takes every suffix: `_cat`, `_can`, `_non` and `_sky` included
    for s in ("_cat", "_can", "_non", "_sky"):
        assert _paa.supported(f"x{s}.png"), s
    assert "writes every texture type itself" in text
    assert "unknown suffix" not in text  # unknown suffixes are encoded by Loki
    intro = text.split("positional arguments")[0]  # the hand-over sentence; the --size help names 4096 as a limit
    assert "4096" not in cli.HANDOVER_CASES and "4096" not in intro  # a PNG over 4096 is halved by Loki
    assert _paa.supported("x_zzz.png")


@pytest.mark.usefixtures("cat_is_handed_over")
def test_handover_to_imagetopaa_when_the_tool_is_present(tmp_path, capsys, monkeypatch):
    p = png(tmp_path / "rock_cat.png")
    fake = tmp_path / "ImageToPAA.exe"
    fake.write_bytes(b"")
    called = []

    def tool(exe, src, dst):
        called.append(Path(exe))
        Path(dst).write_bytes(b"paa from the tool")

    monkeypatch.setattr(core, "find_imagetopaa", lambda override=None, steam_roots=None: fake)
    monkeypatch.setattr(core, "_run_tool", tool)
    code, out, _ = run(capsys, "convert", str(p))
    assert code == 0 and called == [fake] and out.startswith("converted  ") and "bohemia" in out.splitlines()[0]
    assert (tmp_path / "rock_cat.paa").read_bytes() == b"paa from the tool"


@pytest.mark.usefixtures("cat_is_handed_over")
def test_handover_without_the_tool_says_needs_arma_3_tools(tmp_path, capsys):
    p = png(tmp_path / "rock_cat.png")
    code, out, err = run(capsys, "convert", str(p))
    assert code == 1 and "needs-tool " in out and "rock_cat.png: needs Arma 3 Tools for _cat" in out
    assert "manually" not in out + err and not (tmp_path / "rock_cat.paa").exists()


def test_does_not_touch_gui_settings(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    p = png(tmp_path / "a_co.png")
    assert run(capsys, "convert", str(p))[0] == 0
    assert not (tmp_path / "appdata").exists() and not settings.default_path().exists()


def test_windowed_exe_convert_prints_pointer_and_exits_2(capsys):
    assert loki_main.main(["convert", "x.png"]) == 2
    err = capsys.readouterr().err
    assert "Loki-cli.exe <inputs...>" in err


@pytest.mark.parametrize("argv", [["convert"], ["Convert", "x.png"], ["convert", "--quiet"]])
def test_windowed_exe_convert_form_keeps_the_pointer(argv, capsys):
    assert loki_main.main(argv) == 2
    assert "Loki-cli.exe <inputs...>" in capsys.readouterr().err


def test_windowed_exe_unknown_option_is_an_argparse_error_not_a_window(monkeypatch, capsys):
    from loki import gui

    monkeypatch.setattr(gui, "run_window", lambda *a, **k: pytest.fail("window opened"))
    with pytest.raises(SystemExit) as exc:
        loki_main.main(["--quiet", "x.png"])
    assert exc.value.code == 2


def test_windowed_exe_bare_inputs_open_the_window_with_them(monkeypatch, tmp_path):
    from loki import gui

    seen = []
    monkeypatch.setattr(gui, "run_window", lambda settings, paths=None: seen.append((settings, paths)))
    assert loki_main.main(["--settings", "s.json", "a.png", str(tmp_path)]) == 0
    assert seen == [("s.json", [os.path.abspath("a.png"), str(tmp_path)])]
    seen.clear()
    assert loki_main.main(["x.png"]) == 0 and seen[0][1] == [os.path.abspath("x.png")]
    seen.clear()
    assert loki_main.main([]) == 0 and seen == [(None, None)]


def test_windowed_exe_dev_options_are_not_routed_to_the_pointer(capsys):
    with pytest.raises(SystemExit) as exc:  # argparse's own --help: still the window exe's parser
        loki_main.main(["--help"])
    assert exc.value.code == 0 and "--screenshot" in capsys.readouterr().out


# --- convert is the default action, the word `convert` still accepted ---

@pytest.mark.usefixtures("cat_is_handed_over")
def test_short_and_convert_forms_give_identical_results(tmp_path, capsys):
    for name, size in (("wall_co.png", (128, 128)), ("odd_co.png", (30, 20)), ("rock_cat.png", (64, 64))):
        src = png(tmp_path / "a" / name, size)  # noise differs per call, so copy the same bytes to b
        (tmp_path / "b").mkdir(exist_ok=True)
        (tmp_path / "b" / name).write_bytes(src.read_bytes())
    short = run(capsys, str(tmp_path / "a"))
    full = run(capsys, "convert", str(tmp_path / "b"))
    assert short[0] == full[0] == 1

    def normal(out: str, d: str) -> list[str]:  # drop the folder name, timings and ordering
        return sorted(re.sub(r"\d+\.\d+ s", "T", ln).replace(str(tmp_path / d), "D") for ln in out.splitlines())

    assert normal(short[1], "a") == normal(full[1], "b") and short[2] == full[2] == ""
    assert (tmp_path / "a" / "wall_co.paa").read_bytes() == (tmp_path / "b" / "wall_co.paa").read_bytes()
    for args in (["--bogus", str(tmp_path / "a")], ["--max-size", "999", str(tmp_path / "a")], [str(tmp_path / "none" / "*.png")]):
        a, b = run(capsys, *args), run(capsys, "convert", *args)
        assert a[0] == b[0] == 2 and a[2] == b[2]


def test_no_arguments_prints_short_usage_to_stderr_and_exits_2(capsys):
    code, out, err = run(capsys)
    assert code == 2 and out == ""
    assert err == "usage: Loki-cli.exe [options] input [input ...]\nRun 'Loki-cli.exe --help' for all options.\n"
    assert run(capsys, "convert") == (2, "", err)  # the bare old command is the same case


def test_a_folder_named_convert_is_an_input_unless_it_is_the_bare_first_word(tmp_path, capsys, monkeypatch):
    png(tmp_path / "convert" / "a_co.png")
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, ".\\convert")
    assert code == 0 and "a_co.png -> a_co.paa" in out and (tmp_path / "convert" / "a_co.paa").is_file()
    (tmp_path / "convert" / "a_co.paa").unlink()
    assert run(capsys, "convert\\")[0] == 0 and (tmp_path / "convert" / "a_co.paa").is_file()
    (tmp_path / "convert" / "a_co.paa").unlink()
    assert run(capsys, str(tmp_path / "convert"))[0] == 0 and (tmp_path / "convert" / "a_co.paa").is_file()
    assert run(capsys, "convert")[0] == 2  # bare word: the command, with no inputs
    assert run(capsys, "convert", "convert")[0] == 0  # command, then the folder named convert


# --- the output .paa is held open by another program ---

def held_setup(tmp_path):
    p = png(tmp_path / "a_co.png")
    paa = p.with_suffix(".paa")
    paa.write_bytes(b"old paa bytes")
    time.sleep(0.02)
    p.write_bytes(p.read_bytes())  # png newer than the old paa
    return p, paa


@windows_only
def test_held_paa_released_inside_the_limit_converts(tmp_path, capsys):
    p, paa = held_setup(tmp_path)
    lock = Lock(paa, share=1)  # FILE_SHARE_READ: replace and delete fail, like TexView
    threading.Timer(0.6, lock.release).start()
    code, out, _ = run(capsys, "convert", str(p), "--wait-locked", "10")
    assert code == 0 and out.startswith("converted  ")
    assert core.paa_size(paa) == (64, 64)


@windows_only
def test_held_paa_past_the_limit_errors_paa_in_use_and_keeps_old_paa(tmp_path, capsys):
    p, paa = held_setup(tmp_path)
    with Lock(paa, share=1):
        code, out, _ = run(capsys, "convert", str(p), "--wait-locked", "0.5")
    assert code == 1 and "error      " in out and "a_co.png: PAA in use" in out
    assert paa.read_bytes() == b"old paa bytes"
    assert [x for x in tmp_path.iterdir() if x.name.startswith(".loki-tmp-")] == []


def test_non_lock_failure_errors_at_once(tmp_path, capsys, monkeypatch):
    p = png(tmp_path / "a_co.png")
    monkeypatch.setattr(core, "_run_loki", lambda *a: "Loki converter: boom")
    t0 = time.monotonic()
    code, out, _ = run(capsys, "convert", str(p), "--wait-locked", "30")
    assert code == 1 and "a_co.png: Loki converter: boom" in out and time.monotonic() - t0 < 5
