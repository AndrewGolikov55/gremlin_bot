from __future__ import annotations

from typing import Iterable, Mapping, Optional


async def generate(
    messages: Iterable[Mapping[str, str]],
    *,
    temperature: float = 0.8,
    top_p: float = 0.9,
    max_tokens: Optional[int] = None,
) -> str:
    """Stub implementation that echoes last user message.

    Later we will replace this with a real call to Ollama/OpenRouter/etc.
    """

    messages = list(messages)
    last = messages[-1] if messages else {"content": ""}
    content = last.get("content", "")
    if max_tokens is not None and max_tokens > 0:
        snippet = content[-max_tokens:]
    else:
        snippet = content
    return f"[stubbed LLM reply] {snippet}"
