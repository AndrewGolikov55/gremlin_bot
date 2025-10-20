from __future__ import annotations

from typing import Any, List, Mapping


class LLMService:
    async def generate(
        self,
        messages: List[Mapping[str, str]],
        *,
        temperature: float = 1.0,
        top_p: float = 0.9,
        max_tokens: int = 400,
    ) -> str:
        raise NotImplementedError
