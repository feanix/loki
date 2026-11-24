"""Local-path scrubbing (#32): the scanner finds planted paths, the native build remaps them away."""

import sys
import zipfile
from pathlib import Path

import build_native
import path_scan
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
import release

FAKE_ROOT = Path("C:/Fake Build/loki-checkout")
FAKE = path_scan.needles(FAKE_ROOT, user="fakeuser", home="C:\\Users\\fakeuser")


def test_bytes_each_kind_both_slashes_encodings_and_case() -> None:
    assert path_scan.scan_bytes(b"x C:\\USERS\\FakeUser\\.cargo y", FAKE) == ["user", "home"]
    assert path_scan.scan_bytes(b"c:/users/fakeuser/.cargo", FAKE) == ["user", "home"]
    assert path_scan.scan_bytes("C:/Fake Build/Loki-Checkout/src".encode("utf-16-le"), FAKE) == ["checkout"]
    assert path_scan.scan_bytes(b"cargo\\registry\\src\\loki\\app.py", FAKE) == []


def test_short_user_name_only_matches_inside_a_path() -> None:
    ndl = path_scan.needles(FAKE_ROOT, user="ab", home="")
    assert path_scan.scan_bytes(b"table fabric", ndl) == []
    assert path_scan.scan_bytes(b"C:\\Users\\ab\\x", ndl) == ["user"]


def test_counts() -> None:
    assert path_scan.count_bytes(b"fakeuser FAKEUSER", FAKE)["user"] == 2


def test_zip_with_planted_path_fails_and_clean_zip_passes(tmp_path: Path) -> None:
    bad, ok = tmp_path / "bad.zip", tmp_path / "ok.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("README.md", "fine")
        z.writestr("notes.txt", "built in C:/Fake Build/loki-checkout/build")
    with zipfile.ZipFile(ok, "w") as z:
        z.writestr("README.md", "fine")
    assert path_scan.scan_zip(bad, FAKE) == [("bad.zip/notes.txt", "checkout")]
    assert path_scan.scan_zip(ok, FAKE) == []


def test_release_gate_reports_kind_only_and_never_the_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(path_scan, "needles", lambda root=None, extra=None: FAKE)
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("LICENSE", "see C:\\Users\\FakeUser\\secret")
    leaks = release.leaks_in([z])
    assert ("r.zip/LICENSE", "user") in leaks
    release.print_leaks(leaks)
    out = capsys.readouterr().out
    assert "r.zip/LICENSE: user" in out and "fakeuser" not in out.lower() and "secret" not in out


def test_remap_flags_cover_cargo_rustup_checkout_target() -> None:
    env = {"USERPROFILE": "C:\\Users\\fakeuser", "CARGO_HOME": "D:\\cg home"}
    flags = build_native.remap_flags(Path("E:/t/target"), Path("E:/work/loki"), env)
    maps = [f for f in flags if "=" in f and not f.startswith("link-arg")]
    assert sorted(m.split("=")[1] for m in maps) == ["cargo", "loki", "rustup", "target"]
    assert "D:\\cg home=cargo" in maps and any(m.endswith(".rustup=rustup") for m in maps)


def test_rustflags_env_keeps_the_callers_own_flags(tmp_path: Path) -> None:
    mine = build_native.rustflags_env(tmp_path, tmp_path, {"RUSTFLAGS": "-C target-cpu=native"})
    assert mine["CARGO_ENCODED_RUSTFLAGS"].startswith("-C\x1ftarget-cpu=native\x1f--remap-path-prefix")
    enc = build_native.rustflags_env(tmp_path, tmp_path, {"CARGO_ENCODED_RUSTFLAGS": "-Cfoo=a b\x1f-Cbar"})
    assert enc["CARGO_ENCODED_RUSTFLAGS"].startswith("-Cfoo=a b\x1f-Cbar\x1f--remap-path-prefix")


def test_build_native_changes_make_the_pyd_stale() -> None:
    assert Path(build_native.__file__) in build_native.sources()


def test_built_pyd_carries_no_local_paths() -> None:
    data = build_native.PYD.read_bytes()
    ndl = path_scan.needles(extra={"target": str(build_native.target_dir())})
    assert path_scan.scan_bytes(data, ndl) == []


@pytest.mark.skipif(not (path_scan.ROOT / "dist" / "Loki-cli.exe").is_file(), reason="no built exe")
def test_built_exe_entries_are_scannable_and_clean() -> None:
    exe = path_scan.ROOT / "dist" / "Loki-cli.exe"
    names = [n for n, _d in path_scan.exe_entries(exe)]
    assert any(":loki.app" in n for n in names)  # PYZ modules are really read
    assert path_scan.scan_exe(exe, path_scan.needles()) == []
