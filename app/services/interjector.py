from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InterjectDecision:
    should_reply: bool
    reason: str


class InterjectorService:
    # Placeholder for probability/cooldown/quiet-hours logic
    def decide(self) -> InterjectDecision:
        return InterjectDecision(False, "not-implemented")

