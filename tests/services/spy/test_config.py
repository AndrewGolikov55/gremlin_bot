from __future__ import annotations

from app.services.spy.config import SpyConfig


def test_spy_config_defaults(monkeypatch):
    for key in [
        "SPY_ENABLED",
        "SPY_POLL_SECONDS",
        "SPY_BATCH_LIMIT",
        "SPY_MAX_IMAGE_BYTES",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_SESSION",
        "TELEGRAM_SESSION_FILE",
    ]:
        monkeypatch.delenv(key, raising=False)

    conf = SpyConfig.from_env()

    assert conf.enabled is True
    assert conf.poll_seconds == 60
    assert conf.batch_limit == 20
    assert conf.max_image_bytes == 8 * 1024 * 1024
    assert conf.telegram_api_id is None
    assert conf.telegram_api_hash is None
    assert conf.telegram_session is None
    assert conf.telegram_session_file == "/app/data/telethon.session"


def test_spy_config_parses_values(monkeypatch):
    monkeypatch.setenv("SPY_ENABLED", "0")
    monkeypatch.setenv("SPY_POLL_SECONDS", "15")
    monkeypatch.setenv("SPY_BATCH_LIMIT", "7")
    monkeypatch.setenv("SPY_MAX_IMAGE_BYTES", "123")
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION", "session")
    monkeypatch.setenv("TELEGRAM_SESSION_FILE", "/tmp/t.session")

    conf = SpyConfig.from_env()

    assert conf.enabled is False
    assert conf.poll_seconds == 15
    assert conf.batch_limit == 7
    assert conf.max_image_bytes == 123
    assert conf.telegram_api_id == 12345
    assert conf.telegram_api_hash == "hash"
    assert conf.telegram_session == "session"
    assert conf.telegram_session_file == "/tmp/t.session"


def test_spy_config_treats_blank_optional_values_as_missing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "  ")
    monkeypatch.setenv("TELEGRAM_SESSION", "")
    monkeypatch.setenv("TELEGRAM_SESSION_FILE", "  ")

    conf = SpyConfig.from_env()

    assert conf.telegram_api_id is None
    assert conf.telegram_api_hash is None
    assert conf.telegram_session is None
    assert conf.telegram_session_file == "/app/data/telethon.session"
