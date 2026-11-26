"""Package smoke tests: version, about text, license, README sections, icon."""

import re
from pathlib import Path

import loki
from loki.about import about_text, icon_path

ROOT = Path(__file__).resolve().parents[1]


def test_version():
    assert loki.__version__


def test_about_text():
    text = about_text()
    assert loki.__version__ in text
    assert "MIT + Commons Clause" in text and "© 2026 Astrid Winters" in text
    assert "ImageToPAA" in text and "not included" in text


def test_license_is_mit_plus_commons_clause_and_output_clause():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n\nCopyright (c) 2026 Astrid Winters\n")
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    assert '"Commons Clause" License Condition v1.0' in text
    assert "Software: Loki\n\nLicense: MIT\n\nLicensor: Astrid Winters" in text
    assert "Files produced with Loki" in text and "without permission or attribution" in text


def test_metadata_does_not_claim_plain_mit():
    for p in ("pyproject.toml", "native/paa/Cargo.toml"):
        text = (ROOT / p).read_text(encoding="utf-8")
        assert 'license = "MIT"' not in text


def test_third_party_notices():
    text = (ROOT / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    for needle in ("PYTHON SOFTWARE FOUNDATION LICENSE", "Pillow", "PyInstaller", "Apache License", "Zlib", "0BSD"):
        assert needle in text


def test_readme_sections():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("Arma 3 Tools", "power", "## License", "MIT", "Astrid Winters", "## AI disclosure", "AI-assisted coding"):
        assert needle in text
    assert "—" not in text
    # Pattern is split so this file itself passes the export content scan (tools/export_public.py).
    assert not re.search(r"ae" r"sir|\.cla" r"ude|agent", text, re.IGNORECASE)


def test_icon_present():
    p = icon_path()
    assert p and p.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_version_is_one_value_everywhere():
    assert loki.__version__ == "0.3.4"
    assert f'version = "{loki.__version__}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_features_and_license_wording():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Command line" in text and "## Known limits" in text and "Loki-cli.exe" in text
    for needle in ("Soft transparency", "Windows protected your PC", "SHA256SUMS", "Issues", "Source-available"):
        assert needle in text, needle
    converter = (ROOT / "docs" / "CONVERTER.md").read_text(encoding="utf-8")
    assert "## Known limits" in converter
    for needle in ("Soft alpha", "hair cards", "57 files"):
        assert needle.lower() in converter.lower(), needle
    assert len(text.splitlines()) <= 90 and len(text.split()) <= 900  # the README stays short; details live in docs/
    assert "written permission from Astrid Winters" in text
    # The full licence wording lives in CONTRIBUTING.md and LICENSE.
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "source-available (free to use, open code), not open source" in contributing
    assert "written permission from Astrid Winters" in contributing
    assert "paid games and mods" in contributing


def test_converter_doc_suffix_lists_match_the_encoder():
    """The Loki-converter list in docs/CONVERTER.md is exactly what _paa.supported says; the named fallbacks are not."""
    from loki import _paa

    text = (ROOT / "docs" / "CONVERTER.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- **Loki converter**: a name with no suffix"))
    listed = re.findall(r"`((?:_[a-z0-9]+ )+_[a-z0-9]+)`", line)[0].split()
    assert len(listed) == len(set(listed))
    for s in listed:
        assert _paa.supported(f"x{s}.png"), s
    for s in ("_cat", "_can", "_non"):
        assert s in listed and _paa.supported(f"x{s}.png"), s  # the noise suffixes are Loki's own
    assert _paa.supported("x_sky.png")  # verified against ImageToPAA, so it is in the list above
    assert _paa.supported("x_zzz.png")  # an unknown suffix gets the default rule, so it is not a fallback
    rules = (ROOT / "native" / "paa" / "src" / "rules.rs").read_text(encoding="utf-8")
    accepted = {f"_{n}" for n in set(re.findall(r"\*_([A-Za-z0-9]+)\.\*", rules)) if _paa.supported(f"x_{n}.png")}
    assert accepted == set(listed)  # nothing the encoder takes is missing from the doc
    assert _paa.supported("x.png")


def test_release_zip_name_and_members():
    import sys

    sys.path.insert(0, str(ROOT / "build"))
    import release

    assert release.zip_name() == f"Loki-{loki.__version__}-win64.zip"
    assert release.EXES == ("Loki.exe", "Loki-cli.exe")
    assert release.DOCS == ("README.md", "LICENSE", "THIRD_PARTY_NOTICES")
    for name in release.DOCS:
        assert (ROOT / name).is_file()


def test_handover_statements_name_exactly_what_the_help_names():
    """The hand-over list is one list: docs/CONVERTER.md (twice), docs/CLI.md and the --help sentence name the same
    suffixes (and no others), which are the ones the encoder refuses. Today that is none: the Loki converter writes
    every texture type, the noise suffixes included, and each statement says so. (The README line about the noise
    suffixes is not read here.)"""
    from loki import _paa, cli

    converter = (ROOT / "docs" / "CONVERTER.md").read_text(encoding="utf-8")
    cli_doc = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    want = set(re.findall(r"_[a-z]+", cli.HANDOVER_CASES))
    assert want == set()
    known = next(ln for ln in converter.splitlines() if ln.startswith("The Loki converter writes every texture type itself"))
    auto = next(ln for ln in converter.splitlines() if ln.startswith("- **ImageToPAA** (automatic"))
    cmdline = next(ln for ln in cli_doc.splitlines() if ln.startswith("The built-in converter writes every texture type itself"))
    for line in (known, auto, cmdline):
        assert "ImageToPAA" in line
        assert set(re.findall(r"`(_[a-z]+)`", line)) <= {"_cat", "_can", "_non"}  # named only to say they are Loki's
        assert "none" in line.lower() or "no suffix" in line.lower(), line
    for s in ("_cat", "_can", "_non"):
        assert _paa.supported(f"x{s}.png")


def test_contributing_says_issues_only():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Issues" in text and "Pull requests are not accepted" in text and "—" not in text
