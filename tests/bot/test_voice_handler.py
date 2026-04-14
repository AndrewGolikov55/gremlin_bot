"""Tests for the voice/video_note path in router_triggers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_voice_handler_module_imports() -> None:
    """Smoke test: handler exists and related enums can be imported."""
    from app.bot.router_triggers import _handle_voice_message  # noqa: F401
    from app.services.spontaneity import ActionKind, InterjectTrigger  # noqa: F401


@pytest.mark.asyncio
async def test_interjector_generate_spontaneous_reply_accepts_focus_override() -> None:
    """InterjectorService.generate_spontaneous_reply gains focus_text_override kwarg."""
    import inspect

    from app.services.interjector import InterjectorService

    sig = inspect.signature(InterjectorService.generate_spontaneous_reply)
    assert "focus_text_override" in sig.parameters
    param = sig.parameters["focus_text_override"]
    assert param.default is None


def _build_voice_message(
    *,
    chat_id: int = -1001,
    message_id: int = 500,
    voice: bool = True,
    caption: str | None = None,
    reply_to_message: object | None = None,
) -> SimpleNamespace:
    voice_obj = SimpleNamespace(file_id="voice-fid-1", duration=5) if voice else None
    video_note_obj = SimpleNamespace(file_id="vnote-fid-1", duration=4) if not voice else None
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        message_id=message_id,
        voice=voice_obj,
        video_note=video_note_obj,
        caption=caption,
        caption_entities=None,
        entities=None,
        text=None,
        from_user=SimpleNamespace(id=42, username="alice", full_name="Alice", is_bot=False),
        reply_to_message=reply_to_message,
        answer=AsyncMock(),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=501)),
        date=None,
    )


def _build_deps(
    *,
    voice_enabled: bool = True,
    voice_max_seconds: int = 0,
    whisper_daily_limit: int = 0,
    llm_daily_limit: int = 0,
    can_interject: bool = True,
    consume_allowed: bool = True,
    bot_id: int = 111,
    bot_username: str = "testbot",
) -> dict[str, object]:
    app_conf = {
        "voice_enabled": voice_enabled,
        "voice_max_seconds": voice_max_seconds,
        "whisper_daily_limit": whisper_daily_limit,
        "llm_daily_limit": llm_daily_limit,
        "context_max_turns": 20,
        "context_max_prompt_tokens": 32000,
        "max_length": 0,
        "prompt_chat_base": "base",
        "prompt_focus_suffix": 'Вопрос: "{question}".',
        "llm_provider": "openrouter",
        "memory_sidecar_enabled": False,
        "user_memory_enabled": False,
    }
    chat_conf: dict[str, object] = {
        "is_active": True,
        "style": "standup",
        "temperature": 1.0,
        "top_p": 0.9,
        "personalization_enabled": False,
    }

    app_config = MagicMock()
    app_config.get_all = AsyncMock(return_value=app_conf)
    settings = MagicMock()
    settings.get_all = AsyncMock(return_value=chat_conf)
    context = MagicMock()
    context.get_recent_turns = AsyncMock(return_value=[])
    personas = MagicMock()
    personas.get_all = AsyncMock(return_value={})
    memory = MagicMock()
    memory.build_user_memory_block = AsyncMock(return_value=None)
    memory.sidecar_enabled = MagicMock(return_value=False)
    memory.get_sidecar_system_suffix = MagicMock(return_value="")
    usage_limits = MagicMock()
    usage_limits.consume = AsyncMock(return_value=(consume_allowed, {"whisper": 1}, []))
    policy = MagicMock()
    policy.can_interject = AsyncMock(return_value=can_interject)
    policy.mark_acted = AsyncMock(return_value=None)
    interjector = MagicMock()
    interjector.generate_spontaneous_reply = AsyncMock(return_value=True)

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=bot_id, username=bot_username))

    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()

    return {
        "app_config": app_config,
        "settings": settings,
        "context": context,
        "personas": personas,
        "memory": memory,
        "usage_limits": usage_limits,
        "policy": policy,
        "interjector": interjector,
        "bot": bot,
        "session": session,
        "conf": chat_conf,
    }


@pytest.mark.asyncio
async def test_voice_disabled_and_addressed_falls_back_to_unsupported_text() -> None:
    """voice_enabled=False + addressed → send _unsupported_media_text."""
    from app.bot.router_triggers import _handle_voice_message

    deps = _build_deps(voice_enabled=False)
    reply_to_bot = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="testbot", is_bot=True),
    )
    msg = _build_voice_message(reply_to_message=reply_to_bot)

    await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]

    msg.answer.assert_awaited_once()
    deps["policy"].mark_acted.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_voice_disabled_and_not_addressed_silently_drops() -> None:
    """voice_enabled=False + not addressed → silent drop, no message to the chat."""
    from app.bot.router_triggers import _handle_voice_message

    deps = _build_deps(voice_enabled=False)
    msg = _build_voice_message()  # no reply_to_message, no bot mention

    await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]

    msg.answer.assert_not_awaited()
    msg.reply.assert_not_awaited()
    deps["policy"].mark_acted.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_interject_path_skipped_when_policy_refuses() -> None:
    """Not addressed + policy.can_interject=False → no Whisper, no send, no mark."""
    from app.bot.router_triggers import _handle_voice_message

    deps = _build_deps(can_interject=False, whisper_daily_limit=10)
    msg = _build_voice_message()

    with patch("app.bot.router_triggers.transcribe_file_id", new=AsyncMock()) as transcribe:
        await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]
        transcribe.assert_not_called()

    deps["usage_limits"].consume.assert_not_called()  # type: ignore[attr-defined]
    deps["interjector"].generate_spontaneous_reply.assert_not_called()  # type: ignore[attr-defined]
    deps["policy"].mark_acted.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_interject_path_transcribe_success_hands_to_interjector() -> None:
    """Not addressed + can_interject=True + whisper success → interjector with focus_text_override."""
    from app.bot.router_triggers import _handle_voice_message
    from app.services.llm.whisper import TranscriptionResult

    deps = _build_deps(can_interject=True, whisper_daily_limit=0)
    msg = _build_voice_message()

    fake_result = TranscriptionResult(text="привет как дела", duration_seconds=3.0)
    with patch("app.bot.router_triggers.transcribe_file_id",
               new=AsyncMock(return_value=fake_result)) as transcribe, \
         patch("app.bot.router_triggers._cache_voice_transcript", new=AsyncMock()) as cache_fn:
        await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]
        transcribe.assert_awaited_once()
        cache_fn.assert_awaited_once()

    called = deps["interjector"].generate_spontaneous_reply  # type: ignore[attr-defined]
    called.assert_awaited_once()
    kwargs = called.await_args.kwargs
    assert kwargs.get("focus_text_override") == "привет как дела"
    deps["policy"].mark_acted.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_direct_reply_transcribe_success_sends_llm_reply() -> None:
    """Reply-to-bot + whisper success → LLM direct reply with transcript as focus_text."""
    from app.bot.router_triggers import _handle_voice_message
    from app.services.llm.whisper import TranscriptionResult

    deps = _build_deps(whisper_daily_limit=0, llm_daily_limit=0)

    reply_to = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="testbot"),
        via_bot=None,
        sender_chat=None,
    )
    msg = _build_voice_message(reply_to_message=reply_to)

    fake_result = TranscriptionResult(text="распознанный текст", duration_seconds=4.0)

    with patch("app.bot.router_triggers.transcribe_file_id",
               new=AsyncMock(return_value=fake_result)), \
         patch("app.bot.router_triggers._cache_voice_transcript", new=AsyncMock()), \
         patch("app.bot.router_triggers.generate_with_fallback",
               new=AsyncMock(return_value="Ок, услышал.")) as gen_call, \
         patch("app.bot.router_triggers.store_telegram_message", new=AsyncMock()):
        await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]
        gen_call.assert_awaited_once()

    msg.reply.assert_awaited_once()
    call_args = gen_call.await_args
    assert call_args is not None
    messages_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("messages")
    assert messages_arg is not None
    flat = "\n".join(
        (m.get("content") if isinstance(m.get("content"), str) else "")
        for m in messages_arg
    )
    assert "распознанный текст" in flat

    deps["policy"].mark_acted.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_direct_reply_whisper_limit_exhausted_generates_excuse() -> None:
    """Reply-to-bot + consume_allowed=False → excuse via LLM, mark_acted DIRECT_REPLY."""
    from app.bot.router_triggers import _handle_voice_message

    deps = _build_deps(whisper_daily_limit=5, consume_allowed=False)
    reply_to = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="testbot"),
        via_bot=None,
        sender_chat=None,
    )
    msg = _build_voice_message(reply_to_message=reply_to)

    with patch("app.bot.router_triggers.transcribe_file_id", new=AsyncMock()) as transcribe, \
         patch("app.bot.router_triggers.generate_with_fallback",
               new=AsyncMock(return_value="нету сил слушать сегодня")) as gen_call, \
         patch("app.bot.router_triggers.store_telegram_message", new=AsyncMock()):
        await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]
        transcribe.assert_not_called()
        gen_call.assert_awaited_once()

    msg.reply.assert_awaited_once()
    deps["policy"].mark_acted.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_direct_reply_whisper_failure_generates_excuse() -> None:
    """Reply-to-bot + whisper None → excuse via LLM."""
    from app.bot.router_triggers import _handle_voice_message

    deps = _build_deps(whisper_daily_limit=0)
    reply_to = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="testbot"),
        via_bot=None,
        sender_chat=None,
    )
    msg = _build_voice_message(reply_to_message=reply_to)

    with patch("app.bot.router_triggers.transcribe_file_id",
               new=AsyncMock(return_value=None)), \
         patch("app.bot.router_triggers.generate_with_fallback",
               new=AsyncMock(return_value="не смог разобрать")) as gen_call, \
         patch("app.bot.router_triggers.store_telegram_message", new=AsyncMock()):
        await _handle_voice_message(msg, **deps)  # type: ignore[arg-type]
        gen_call.assert_awaited_once()

    msg.reply.assert_awaited_once()
    deps["policy"].mark_acted.assert_awaited_once()  # type: ignore[attr-defined]
