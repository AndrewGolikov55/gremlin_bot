from __future__ import annotations

import math
from typing import Mapping, Any


def resolve_temperature(
    conf: Mapping[str, Any] | None,
    *,
    default: float = 1.0,
    minimum: float = 0.0,
    maximum: float = 2.0,
) -> float:
    if not conf:
        return default
    raw = conf.get("temperature")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value
