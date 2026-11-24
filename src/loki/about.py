"""Name, version, license text for the About box and the `about` scenario command; icon lookup."""

from __future__ import annotations

import sys
from pathlib import Path

from loki import __version__

LICENSE_NAME = "MIT + Commons Clause"
COPYRIGHT = "© 2026 Astrid Winters"


def about_text() -> str:
    return (
        f"Loki {__version__}\n"
        "Drag PNG textures onto the window; Loki converts them to Arma 3 PAA, on demand or when you save them.\n"
        f"{COPYRIGHT}\n"
        f"{LICENSE_NAME}\n"
        "Loki uses Bohemia Interactive's ImageToPAA (from Arma 3 Tools, not included)."
    )


def icon_path() -> Path | None:
    """assets/loki.ico: next to the unpacked exe data when frozen, else in the repo."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[2]
    p = base / "assets" / "loki.ico"
    return p if p.is_file() else None
