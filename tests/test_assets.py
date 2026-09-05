"""the packaged default profile pictures: the dashboard shows them whenever no operator file is configured, so they must be real, small, square pngs."""

import struct

import pytest

from taskboy import assets
from taskboy.config import AVATAR_MAX_BYTES

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("path", [assets.DEFAULT_AGENT_AVATAR, assets.DEFAULT_REVIEWER_AVATAR], ids=["agent", "reviewer"])
def test_default_avatar_is_a_small_square_png(path):
    assert path.parent == assets.TEMPLATES_ROOT / "avatars"
    assert path.is_file(), f"{path.name} is missing from the package"
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE
    width, height = struct.unpack(">II", data[16:24])  # ihdr is always the first chunk: length(4) type(4) width(4) height(4)
    assert width == height, f"{path.name} is {width}x{height}, not square"
    assert len(data) <= AVATAR_MAX_BYTES  # the same cap operator files get


def test_default_avatars_are_distinct():
    assert assets.DEFAULT_AGENT_AVATAR.read_bytes() != assets.DEFAULT_REVIEWER_AVATAR.read_bytes()
