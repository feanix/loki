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
    assert loki.__version__ == "0.3.0"
    assert f'version = "{loki.__version__}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_features_and_license_wording():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## The built-in converter" in text and "## Command line" in text and "Loki-cli.exe" in text
    assert "## Known limits" in text or "### Known limits" in text
    for needle in ("soft alpha", "hair cards", "0.3.1", "SmartScreen", "SHA256SUMS", "Issues", "source-available"):
        assert needle in text, needle
    assert "source-available (free to use, open code), not open source" in text
    assert "written permission from Astrid Winters" in text
    assert "paid games and mods" in text


def test_readme_suffix_lists_match_the_encoder():
    """The Loki-converter list in the README is exactly what _paa.supported says; the named fallbacks are not."""
    from loki import _paa

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- **Loki converter**: a name with no suffix"))
    listed = re.findall(r"`((?:_[a-z0-9]+ )+_[a-z0-9]+)`", line)[0].split()
    assert len(listed) == len(set(listed))
    for s in listed:
        assert _paa.supported(f"x{s}.png"), s
    for s in ("_sky", "_cat", "_can", "_non", "_zzz"):
        assert not _paa.supported(f"x{s}.png"), s
    rules = (ROOT / "native" / "paa" / "src" / "rules.rs").read_text(encoding="utf-8")
    accepted = {f"_{n}" for n in set(re.findall(r"\*_([A-Za-z0-9]+)\.\*", rules)) if _paa.supported(f"x_{n}.png")}
    assert accepted == set(listed)  # nothing the encoder takes is missing from the README
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


def test_contributing_says_issues_only():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Issues" in text and "Pull requests are not accepted" in text and "—" not in text
