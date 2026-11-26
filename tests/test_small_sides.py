"""Sides below 4 px through the Python boundary and `core.convert` (0.3.1).

Why: Loki's own encoder writes the ARGB file ImageToPAA writes for 1x1, 2x2, 1xN and Nx1 (no hand-over), and
refuses forced-DXT classes with a message that names the 4 px minimum (ImageToPAA fails there too and leaves a
truncated file). The Rust tests (tests_small.rs) hold the oracle comparison; these check the wiring and that
a refusal leaves no file or temp folder behind. No ImageToPAA needed.
"""

from pathlib import Path

import pytest
from PIL import Image

from loki import _paa, core


def _png(path: Path, size, mode="RGB") -> Path:
    colour = (50, 60, 70) if mode == "RGB" else (50, 60, 70, 255)
    Image.new(mode, size, colour).save(path)
    return path


@pytest.mark.parametrize("suffix", ["_raw", "_8888", "_4444", "_1555", "_88", "_gs", "_normalmap", ""])
@pytest.mark.parametrize("size", [(1, 1), (2, 2), (1, 256), (256, 1)])
def test_loki_converts_small_sides_for_non_dxt_classes(tmp_path, suffix, size):
    png = _png(tmp_path / f"tiny{suffix}.png", size)
    res = core.convert(png, None, None, core.LOKI)
    assert res.ok and res.converter == core.LOKI, res.error
    # The size a reader reports is mip 0.
    assert core.paa_size(res.paa) == size


@pytest.mark.parametrize("suffix", ["_co", "_ca", "_nohq", "_dxt1", "_dxt5"])
@pytest.mark.parametrize("size", [(1, 1), (2, 2), (1, 256), (256, 1)])
def test_forced_dxt_classes_name_the_4_px_minimum_and_leave_nothing(tmp_path, suffix, size):
    png = _png(tmp_path / f"tiny{suffix}.png", size)
    res = core.convert(png, Path("ImageToPAA.exe"), None, core.LOKI)
    assert not res.ok and not res.needs_tool and not res.retry
    assert "DXT" in res.error and "at least 4 px" in res.error and "power" not in res.error, res.error
    assert sorted(p.name for p in tmp_path.iterdir()) == [png.name]  # no .paa, no .loki-tmp folder


def test_the_native_encoder_raises_value_error_not_a_hand_over_for_a_dxt_class(tmp_path):
    png = _png(tmp_path / "a_co.png", (256, 1))
    with pytest.raises(ValueError, match="at least 4 px") as e:
        _paa.encode(str(png), str(tmp_path / "a.paa"), None)
    assert not isinstance(e.value, _paa.NeedsImageToPAA)
    assert not (tmp_path / "a.paa").exists()


def test_soft_alpha_small_unsuffixed_image_is_encoded_like_a_large_one(tmp_path):
    """The AVGC of soft alpha is reproduced, so a tiny soft-alpha image is encoded (ARGB4444), not handed over."""
    png = tmp_path / "soft.png"
    Image.new("RGBA", (2, 2), (50, 60, 70, 128)).save(png)
    assert _paa.encode(str(png), str(tmp_path / "soft.paa"), None) is None
    assert (tmp_path / "soft.paa").is_file()
