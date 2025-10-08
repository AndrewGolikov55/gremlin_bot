from __future__ import annotations

import logging


TRACE_LEVEL = 5


def ensure_trace_level() -> None:
    if getattr(logging, "TRACE", None) == TRACE_LEVEL:
        return

    logging.addLevelName(TRACE_LEVEL, "TRACE")
    logging.TRACE = TRACE_LEVEL  # type: ignore[attr-defined]
    logging._nameToLevel["TRACE"] = TRACE_LEVEL  # type: ignore[attr-defined]
    logging._levelToName[TRACE_LEVEL] = "TRACE"  # type: ignore[attr-defined]

    def trace(self, message, *args, **kwargs):
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, message, args, **kwargs)

    logging.Logger.trace = trace  # type: ignore[attr-defined]
