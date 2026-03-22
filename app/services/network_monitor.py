from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..utils.proxy import get_proxy_display, httpx_client_kwargs


logger = logging.getLogger("network.monitor")

DEFAULT_PROBE_TARGET = "https://api.telegram.org"
PROBE_INTERVAL_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 8.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ProbeState:
    enabled: bool
    target: str
    via_proxy: bool
    proxy: str | None
    ok: bool | None = None
    status_code: int | None = None
    error: str | None = None
    latency_ms: int | None = None
    consecutive_failures: int = 0
    last_attempt_at: str | None = None
    last_success_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "target": self.target,
            "via_proxy": self.via_proxy,
            "proxy": self.proxy,
            "ok": self.ok,
            "status_code": self.status_code,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "consecutive_failures": self.consecutive_failures,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
        }


class NetworkMonitorService:
    def __init__(self) -> None:
        self._enabled = True
        self._target = self._resolve_target()
        self._timeout = PROBE_TIMEOUT_SECONDS
        proxy = get_proxy_display()
        self._state = ProbeState(
            enabled=self._enabled,
            target=self._target,
            via_proxy=bool(proxy),
            proxy=proxy,
        )

    @staticmethod
    def _resolve_target() -> str:
        bot_token = (os.getenv("BOT_TOKEN") or "").strip()
        if bot_token:
            return f"https://api.telegram.org/bot{bot_token}/getMe"
        return DEFAULT_PROBE_TARGET

    def snapshot(self) -> dict[str, Any]:
        return self._state.as_dict()

    async def probe_once(self) -> None:
        if not self._enabled:
            return

        proxy = get_proxy_display()
        self._state.via_proxy = bool(proxy)
        self._state.proxy = proxy
        self._state.last_attempt_at = _utc_now_iso()

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                **httpx_client_kwargs(timeout=self._timeout),
            ) as client:
                response = await client.get(self._target)
        except Exception as exc:
            self._mark_failure(str(exc))
            logger.warning(
                "Connectivity check failed target=%s via=%s error=%s failures=%s",
                self._target,
                proxy or "direct",
                exc,
                self._state.consecutive_failures,
            )
            return

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            self._mark_failure(f"upstream status {response.status_code}", status_code=response.status_code, latency_ms=latency_ms)
            logger.warning(
                "Connectivity check got bad upstream status target=%s via=%s status=%s failures=%s",
                self._target,
                proxy or "direct",
                response.status_code,
                self._state.consecutive_failures,
            )
            return

        was_failing = self._state.ok is False
        self._state.ok = True
        self._state.status_code = response.status_code
        self._state.error = None
        self._state.latency_ms = latency_ms
        self._state.consecutive_failures = 0
        self._state.last_success_at = _utc_now_iso()

        if was_failing:
            logger.info(
                "Connectivity restored target=%s via=%s status=%s latency_ms=%s",
                self._target,
                proxy or "direct",
                response.status_code,
                latency_ms,
            )
        else:
            logger.debug(
                "Connectivity check ok target=%s via=%s status=%s latency_ms=%s",
                self._target,
                proxy or "direct",
                response.status_code,
                latency_ms,
            )

    def _mark_failure(
        self,
        error: str,
        *,
        status_code: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        self._state.ok = False
        self._state.status_code = status_code
        self._state.error = error
        self._state.latency_ms = latency_ms
        self._state.consecutive_failures += 1
