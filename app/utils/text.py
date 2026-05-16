from __future__ import annotations

import re

_MD_BOLD_OR_ITALIC = re.compile(r"(\*\*|\*|__|_)(?=\S)(.+?)(?<=\S)\1")
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """Best-effort markdown removal so LLM output looks clean in Telegram plain text.

    Strips `**bold**`, `*italic*`, `__bold__`, `_italic_`, inline `code`, leading
    `#` headings, and `- ` / `* ` / `+ ` bullet prefixes. Leaves URLs and emojis alone.
    """
    if not text:
        return text
    text = _MD_BOLD_OR_ITALIC.sub(r"\2", text)
    # Run twice to handle nested cases like **__x__**
    text = _MD_BOLD_OR_ITALIC.sub(r"\2", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    return text
