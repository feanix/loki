"""Relative links in README.md and the shipped docs resolve to a file, and to a heading when an anchor is given."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ["README.md", "CONTRIBUTING.md", "docs/USER-GUIDE.md", "docs/CLI.md", "docs/CONVERTER.md", "docs/PAA-FORMAT.md"]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf8")
    return {slug(h) for h in re.findall(r"^#+ (.+)$", text, re.MULTILINE)}


def links():
    for name in FILES:
        text = (ROOT / name).read_text(encoding="utf8")
        for target in LINK.findall(text):
            if not target.startswith(("http://", "https://", "mailto:")):
                yield name, target


def test_the_user_docs_exist_and_are_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf8")
    for doc in ("docs/USER-GUIDE.md", "docs/CLI.md", "docs/CONVERTER.md", "docs/PAA-FORMAT.md"):
        assert (ROOT / doc).is_file() and f"({doc}" in readme, doc


@pytest.mark.parametrize("name,target", list(links()))
def test_relative_link_resolves(name, target):
    base = (ROOT / name).parent
    file_part, _, anchor = target.partition("#")
    dest = (base / file_part) if file_part else ROOT / name
    assert dest.is_file(), f"{name}: {target} -> {dest}"
    if anchor:
        assert anchor in anchors(dest), f"{name}: {target}: no heading '{anchor}'"
