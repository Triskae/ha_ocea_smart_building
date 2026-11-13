"""Tests for the local brand images."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

BRAND_DIR = (
    Path(__file__).parents[1] / "custom_components" / "ocea_smart_building" / "brand"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _size(name: str) -> tuple[int, int]:
    with Image.open(BRAND_DIR / name) as image:
        return image.size


@pytest.mark.parametrize(
    "name",
    [
        "icon.png",
        "icon@2x.png",
        "logo.png",
        "logo@2x.png",
        "dark_logo.png",
        "dark_logo@2x.png",
    ],
)
def test_brand_images_are_png_files(name: str) -> None:
    """Test every brand image is a PNG file."""
    assert (BRAND_DIR / name).read_bytes().startswith(PNG_SIGNATURE)


def test_icons_are_square_with_expected_sizes() -> None:
    """Test icons follow the Home Assistant brands specification."""
    assert _size("icon.png") == (256, 256)
    assert _size("icon@2x.png") == (512, 512)


@pytest.mark.parametrize("prefix", ["logo", "dark_logo"])
def test_logos_follow_size_limits_and_keep_ratio(prefix: str) -> None:
    """Test logo short sides and matching hDPI ratio."""
    width, height = _size(f"{prefix}.png")
    width2x, height2x = _size(f"{prefix}@2x.png")
    assert 128 <= min(width, height) <= 256
    assert 256 <= min(width2x, height2x) <= 512
    assert abs(width * height2x - width2x * height) <= max(height2x, width2x)
