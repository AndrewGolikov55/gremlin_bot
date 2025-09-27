from __future__ import annotations

from typing import Iterable, List, Tuple


def build_messages(system_prompt: str, turns: List[Tuple[str, str]], max_turns: int = 20):
    msgs = [{"role": "system", "content": system_prompt}]
    for speaker, text in turns[-max_turns:]:
        msgs.append({"role": "user", "content": f"{speaker}: {text}"})
    msgs.append({"role": "user", "content": "Ответь уместно одним сообщением."})
    return msgs

