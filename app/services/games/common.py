from __future__ import annotations

from enum import Enum


class RoundStatus(str, Enum):
    LOBBY = "lobby"
    ACTIVE = "active"
    VOTING = "voting"
    GENERATING = "generating"
    FINISHED = "finished"
    EXPIRED = "expired"
    ABORTED = "aborted"
    WON = "won"
    LOST = "lost"
    FINALISING = "finalising"
    FINALISED = "finalised"


OPEN_STATUSES_GENERIC = (RoundStatus.LOBBY.value, RoundStatus.ACTIVE.value, RoundStatus.VOTING.value)


class ActiveRoundExistsError(Exception):
    """Raised when a new game round cannot be started because one is already open."""


class NoActiveRoundError(Exception):
    """Raised when an operation expects an open round but none exists."""
