from __future__ import annotations

import pytest

from app.services.spy.refs import ChannelRefError, normalize_channel_ref


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@gospodindirectorpivs", "gospodindirectorpivs"),
        ("https://t.me/gospodindirectorpivs", "gospodindirectorpivs"),
        ("http://t.me/gospodindirectorpivs/123", "gospodindirectorpivs"),
        ("t.me/gospodindirectorpivs", "gospodindirectorpivs"),
        ("  gospodindirectorpivs  ", "gospodindirectorpivs"),
        ("HTTPS://T.ME/GospodinDirectorPivs/123", "gospodindirectorpivs"),
    ],
)
def test_normalize_channel_ref(raw: str, expected: str) -> None:
    assert normalize_channel_ref(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://example.com/x",
        "https://t.me/+invite",
        "bad name",
        "t.me/joinchat/abc",
        "https://t.me/channel/not-a-post",
        "https://t.me/channel/123/extra",
    ],
)
def test_normalize_channel_ref_rejects_invalid(raw: str) -> None:
    with pytest.raises(ChannelRefError):
        normalize_channel_ref(raw)
