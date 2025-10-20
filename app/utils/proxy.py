from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


logger = logging.getLogger(__name__)

NETWORK_PROXY_URL_ENV = "NETWORK_SOCKS5_PROXY"
NETWORK_PROXY_HOST_ENV = "NETWORK_SOCKS5_HOST"
NETWORK_PROXY_PORT_ENV = "NETWORK_SOCKS5_PORT"
NETWORK_PROXY_USER_ENV = "NETWORK_SOCKS5_USERNAME"
NETWORK_PROXY_PASSWORD_ENV = "NETWORK_SOCKS5_PASSWORD"

_PROXY_URL_CACHE: Optional[str] = None
_LOGGED_STATUS = False


def _sanitize_proxy_url(url: str) -> str:
    parts = urlsplit(url)
    username = parts.username or ""
    host = parts.hostname or ""
    netloc = host
    if username:
        netloc = f"{username}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "", parts.query or "", parts.fragment or ""))


def _build_proxy_url() -> Optional[str]:
    direct = (os.getenv(NETWORK_PROXY_URL_ENV) or "").strip()
    if direct:
        return direct

    host = (os.getenv(NETWORK_PROXY_HOST_ENV) or "").strip()
    port = (os.getenv(NETWORK_PROXY_PORT_ENV) or "").strip()
    if not host or not port:
        return None

    username = (os.getenv(NETWORK_PROXY_USER_ENV) or "").strip()
    password = (os.getenv(NETWORK_PROXY_PASSWORD_ENV) or "").strip()

    auth = ""
    if username:
        safe_user = quote(username, safe="")
        if password:
            safe_password = quote(password, safe="")
            auth = f"{safe_user}:{safe_password}@"
        else:
            auth = f"{safe_user}@"
    elif password:
        logger.warning("%s задан без имени пользователя и будет проигнорирован", NETWORK_PROXY_PASSWORD_ENV)

    return f"socks5://{auth}{host}:{port}"


def get_proxy_url(*, prefer_plain: bool = False) -> Optional[str]:
    global _PROXY_URL_CACHE, _LOGGED_STATUS
    if _PROXY_URL_CACHE is None:
        _PROXY_URL_CACHE = _build_proxy_url()
    if not _LOGGED_STATUS:
        if _PROXY_URL_CACHE:
            logger.info(
                "Outbound SOCKS5 proxy configured: %s",
                _sanitize_proxy_url(_PROXY_URL_CACHE),
            )
        else:
            logger.info("Outbound SOCKS5 proxy not configured; using direct connections")
        _LOGGED_STATUS = True
    if not _PROXY_URL_CACHE:
        return None
    if prefer_plain and _PROXY_URL_CACHE.startswith("socks5h://"):
        return "socks5://" + _PROXY_URL_CACHE[len("socks5h://") :]
    return _PROXY_URL_CACHE


def get_proxy_display() -> Optional[str]:
    proxy = get_proxy_url()
    if not proxy:
        return None
    return _sanitize_proxy_url(proxy)


def httpx_client_kwargs(timeout: float = 60.0) -> dict[str, object]:
    kwargs: dict[str, object] = {"timeout": timeout}
    proxy = get_proxy_url()
    if proxy:
        kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=proxy)
    return kwargs
