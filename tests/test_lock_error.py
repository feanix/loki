"""core.is_lock_error is precise: only sharing/lock violations count, not access denied."""

import getpass
import shutil
import subprocess
import sys

import pytest
from PIL import Image

from loki import core


def win_err(code):
    exc = PermissionError(13, "boom")
    exc.winerror = code  # constructible on any platform
    return exc


def test_is_lock_error_classifies_constructed_errors():
    assert core.is_lock_error(win_err(32))
    assert core.is_lock_error(win_err(33))
    assert not core.is_lock_error(win_err(5))  # access denied
    assert core.is_lock_error(PermissionError(13, "no winerror"))  # non-Windows form
    assert not core.is_lock_error(OSError(2, "gone"))
    assert not core.is_lock_error(ValueError("x"))
    plain = OSError(32, "sharing")
    plain.winerror = 32
    assert core.is_lock_error(plain)


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("icacls") is None, reason="needs Windows icacls")
def test_check_png_flags_an_access_denied_png(tmp_path):
    png = tmp_path / "a.png"
    Image.new("RGB", (64, 64)).save(png, format="PNG")
    user = getpass.getuser()
    subprocess.run(["icacls", str(png), "/deny", f"{user}:(RD)"], check=True, capture_output=True)
    try:
        entry = core.check_png(png)
    finally:
        subprocess.run(["icacls", str(png), "/remove:d", user], check=True, capture_output=True)
    assert entry.status == core.FLAGGED
    assert entry.reason == "cannot read: access denied"
