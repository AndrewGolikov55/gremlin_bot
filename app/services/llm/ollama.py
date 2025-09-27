from __future__ import annotations

from typing import Iterable, Mapping


async def generate(
    messages: Iterable[Mapping[str, str]],
    *,
    temperature: float = 0.8,
    top_p: float = 0.9,
    max_tokens: int = 400,
) -> str:
    """Stub implementation that echoes last user message.

    Later we will replace this with a real call to Ollama/OpenRouter/etc.
    """

    messages = list(messages)
    last = messages[-1] if messages else {"content": ""}
    content = last.get("content", "")
    snippet = content[-max_tokens:]
    return f"[stubbed LLM reply] {snippet}"
