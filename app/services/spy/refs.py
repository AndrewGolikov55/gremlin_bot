from __future__ import annotations

import re
from urllib.parse import urlparse

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class ChannelRefError(ValueError):
    pass


def normalize_channel_ref(raw: str) -> str:
    ref = raw.strip()
    if not ref:
        raise ChannelRefError("channel reference is empty")

    username = _extract_username(ref)
    if username.lower() == "joinchat" or username.startswith("+"):
        raise ChannelRefError("private invite links are not supported")
    if not _USERNAME_RE.fullmatch(username):
        raise ChannelRefError("invalid Telegram channel username")
    return username.lower()


def _extract_username(ref: str) -> str:
    if ref.startswith("@"):
        return ref[1:]

    if not ref.lower().startswith(("t.me/", "telegram.me/")) and "://" not in ref:
        return ref

    parsed = urlparse(ref if "://" in ref else f"https://{ref}")
    if parsed.netloc:
        host = parsed.netloc.lower()
        if host not in {"t.me", "telegram.me"}:
            raise ChannelRefError("unsupported Telegram channel host")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ChannelRefError("channel username is missing")
        if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
            raise ChannelRefError("invalid Telegram post suffix")
        return parts[0]

    return ref
