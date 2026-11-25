"""The command-line help screens: examples, exit codes and the guide link stay true.

Runs loki.cli.main in-process; ImageToPAA is never needed. Example commands in the help are parsed, never run.
"""

import shlex
from pathlib import Path

import pytest
from PIL import Image

from loki import cli, core

ROOT = Path(__file__).resolve().parent.parent
GUIDE = "Full guide: https://github.com/feanix/loki/blob/main/docs/CLI.md"


@pytest.fixture(autouse=True)
def no_tool(monkeypatch):
    def absent(override=None, steam_roots=None):
        raise core.ToolNotFound("absent")

    monkeypatch.setattr(core, "find_imagetopaa", absent)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def png(path: Path, size) -> Path:
    Image.effect_noise(size, 60).convert("RGB").save(path)
    return path


def test_help_screen_ends_with_the_guide_link_and_convert_help_is_the_same(capsys):
    code, out, _ = run(capsys, "--help")
    assert code == 0 and out.rstrip().splitlines()[-1] == GUIDE
    assert run(capsys, "convert", "--help") == (0, out, "")
    assert len(out.splitlines()) <= 60


def test_guide_link_points_at_the_cli_doc():
    path = GUIDE.split("/blob/main/", 1)[1]
    assert (ROOT / path).is_file() and cli.GUIDE_LINE == GUIDE


def test_help_has_sentence_short_form_and_convert_note(capsys):
    _code, out, _ = run(capsys, "--help")
    assert "Loki-cli.exe converts PNG textures to Arma 3 PAA files and exits." in out
    assert "usage: Loki-cli.exe [options] input [input ...]" in out
    assert "  Loki-cli.exe textures --recursive --skip-up-to-date" in out
    assert "The word 'convert' before the inputs is optional (0.3.0 scripts still work)." in out
    assert "commands:" not in out and "Loki-cli.exe convert " not in out


def test_help_has_examples_and_exit_codes(capsys):
    _code, out, _ = run(capsys, "--help")
    for needle in (
        "Examples:",
        "--recursive --skip-up-to-date",
        '"textures\\*_co.png" --max-size 2048',
        "A pattern. Keep the quotes so Loki expands the pattern itself:",
        "In PowerShell, run it from its folder as .\\Loki-cli.exe (or add the\n  folder to PATH).",
        "--quiet",
        "if errorlevel 1",
        '    if ($LASTEXITCODE -ne 0) { "Some textures need attention" }  (PowerShell)',
        "Exit codes:",
        *cli.EXIT_CODE_LINES,
    ):
        assert needle in out, needle


def test_every_example_command_parses(capsys):
    _code, out, _ = run(capsys, "--help")
    examples = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("Loki-cli.exe ") and ln.strip() != "Loki-cli.exe [options] input [input ...]"]
    examples = [e for e in examples if "converts PNG" not in e]
    assert len(examples) >= 4
    for ex in examples:
        words = shlex.split(ex)
        assert words[0] == "Loki-cli.exe"
        args = cli._build_parser().parse_args(words[1:])  # a usage error raises cli.UsageError
        assert args.inputs


def test_exit_codes_match_behaviour(tmp_path, capsys):
    good = png(tmp_path / "a_co.png", (64, 64))
    odd = png(tmp_path / "odd_co.png", (30, 20))
    assert run(capsys, "convert", str(good))[0] == 0  # 0: converted
    assert run(capsys, "convert", str(good), "--skip-up-to-date")[0] == 0  # 0: up to date
    assert run(capsys, "convert", str(odd))[0] == 1  # 1: flagged
    assert run(capsys, "convert", str(tmp_path / "missing.png"))[0] == 1  # 1: failed
    sky = png(tmp_path / "rock_cat.png", (64, 64))
    assert run(capsys, "convert", str(sky))[0] == 1  # 1: needs Arma 3 Tools
    assert run(capsys, "convert", str(tmp_path / "nothing" / "*.png"))[0] == 2  # 2: no PNG matched
    assert run(capsys, "convert", str(good), "--bogus")[0] == 2  # 2: bad option


def test_cli_doc_has_the_powershell_run_line():
    doc = (ROOT / "docs" / "CLI.md").read_text(encoding="utf8")
    assert ".\\Loki-cli.exe" in doc and "add the folder to PATH" in doc


def test_cli_doc_exit_codes_match_help():
    doc = " ".join((ROOT / "docs" / "CLI.md").read_text(encoding="utf8").split())
    for line in cli.EXIT_CODE_LINES:
        code, text = line.split("  ", 1)
        assert f"{code} {text}" in doc, line
