# PyInstaller spec: dist/Loki.exe (windowed GUI) and dist/Loki-cli.exe (console). Only Loki's own code, Pillow, Tk and the icon.
# Nothing from Arma 3 Tools is bundled (ImageToPAA is found on the user's machine at run time).
from pathlib import Path

root = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is provided by PyInstaller

a = Analysis(  # noqa: F821
    [str(root / "build" / "entry.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(root / "assets" / "loki.ico"), "assets"),
        (str(root / "LICENSE"), "."),
        (str(root / "THIRD_PARTY_NOTICES"), "."),
    ],
    # the native PAA encoder (built by build/build_native.py), shipped inside the loki package
    binaries=[(str(root / "src" / "loki" / "_paa.pyd"), "loki")],
    excludes=["pytest", "numpy", "unittest", "pydoc", "doctest"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Loki",
    console=False,
    icon=str(root / "assets" / "loki.ico"),
    upx=False,
)

# The command line: a separate console exe, no Tk (Loki.exe stays windowed).
cli = Analysis(  # noqa: F821
    [str(root / "build" / "cli_entry.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(root / "LICENSE"), "."),
        (str(root / "THIRD_PARTY_NOTICES"), "."),
    ],
    binaries=[(str(root / "src" / "loki" / "_paa.pyd"), "loki")],
    excludes=["pytest", "numpy", "unittest", "pydoc", "doctest", "tkinter"],
)
cli_pyz = PYZ(cli.pure)  # noqa: F821
cli_exe = EXE(  # noqa: F821
    cli_pyz,
    cli.scripts,
    cli.binaries,
    cli.datas,
    name="Loki-cli",
    console=True,
    icon=str(root / "assets" / "loki.ico"),
    upx=False,
)
