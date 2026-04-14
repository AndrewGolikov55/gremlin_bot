"""Tests for send_reply_maybe_voice: voice-or-text reply helper."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _build_msg(chat_id: int = -100, message_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=43)),
    )


def _build_deps(
    *,
    tts_enabled: bool = True,
    tts_daily_limit: int = 0,
    should_reply_with_voice: bool = True,
    consume_allowed: bool = True,
    persona: str = "gopnik",
) -> dict:
    bot = MagicMock()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=44))
    app_conf = {
        "tts_enabled": tts_enabled,
        "tts_daily_limit": tts_daily_limit,
        "tts_voice_gopnik": "onyx",
        "tts_voice_standup": "echo",
        "tts_voice_boss": "onyx",
        "tts_voice_zoomer": "nova",
        "tts_voice_jarvis": "fable",
    }
    conf = {"style": persona}
    policy = MagicMock()
    policy.should_reply_with_voice = AsyncMock(return_value=should_reply_with_voice)
    usage_limits = MagicMock()
    usage_limits.consume = AsyncMock(return_value=(consume_allowed, {"tts": 1}, []))
    return {
        "bot": bot,
        "conf": conf,
        "app_conf": app_conf,
        "policy": policy,
        "usage_limits": usage_limits,
    }


@pytest.mark.asyncio
async def test_tts_disabled_sends_text() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps(tts_enabled=False)
    msg = _build_msg()
    with patch("app.bot.voice_reply.synthesize_speech", new=AsyncMock()) as mock_tts:
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    msg.reply.assert_awaited_once_with("hi")
    mock_tts.assert_not_awaited()
    deps["bot"].send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_refuses_sends_text() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps(should_reply_with_voice=False)
    msg = _build_msg()
    with patch("app.bot.voice_reply.synthesize_speech", new=AsyncMock()) as mock_tts:
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    msg.reply.assert_awaited_once_with("hi")
    mock_tts.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_success_sends_voice() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps()
    msg = _build_msg()
    with patch(
        "app.bot.voice_reply.synthesize_speech",
        new=AsyncMock(return_value=b"opus-audio-bytes"),
    ) as mock_tts:
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    deps["bot"].send_voice.assert_awaited_once()
    msg.reply.assert_not_awaited()
    mock_tts.assert_awaited_once()
    tts_args = mock_tts.await_args
    assert tts_args is not None
    assert tts_args.args[0] == "hi"
    assert tts_args.kwargs["voice"] == "onyx"  # gopnik persona → onyx default


@pytest.mark.asyncio
async def test_tts_returns_none_falls_back_to_text() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps()
    msg = _build_msg()
    with patch("app.bot.voice_reply.synthesize_speech", new=AsyncMock(return_value=None)):
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    msg.reply.assert_awaited_once_with("hi")
    deps["bot"].send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_voice_exception_falls_back_to_text() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps()
    deps["bot"].send_voice = AsyncMock(side_effect=RuntimeError("telegram rejected"))
    msg = _build_msg()
    with patch(
        "app.bot.voice_reply.synthesize_speech",
        new=AsyncMock(return_value=b"opus"),
    ):
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    msg.reply.assert_awaited_once_with("hi")


@pytest.mark.asyncio
async def test_tts_daily_limit_exhausted_sends_text() -> None:
    from app.bot.voice_reply import send_reply_maybe_voice

    deps = _build_deps(tts_daily_limit=10, consume_allowed=False)
    msg = _build_msg()
    with patch("app.bot.voice_reply.synthesize_speech", new=AsyncMock()) as mock_tts:
        await send_reply_maybe_voice(
            bot=deps["bot"], message=msg, text="hi",  # type: ignore[arg-type]
            conf=deps["conf"], app_conf=deps["app_conf"],
            policy=deps["policy"], usage_limits=deps["usage_limits"],
            incoming_is_voice_reply_to_bot=False,
        )

    msg.reply.assert_awaited_once_with("hi")
    mock_tts.assert_not_awaited()
