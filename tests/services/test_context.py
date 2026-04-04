from __future__ import annotations

from app.services.context import (
    DEFAULT_CHAT_PROMPT,
    DEFAULT_INTERJECT_SUFFIX,
    DEFAULT_STYLE_PROMPTS,
    ChatTurn,
    build_messages,
    build_system_prompt,
)


def test_build_messages_filters_service_lines_and_keeps_recent_context() -> None:
    turns = [
        ChatTurn("alice", 1, "/settings", False),
        ChatTurn("system", None, "Bob joined the chat", False),
        ChatTurn("alice", 1, "first line", False),
        ChatTurn("alice", 1, "second line", False),
        ChatTurn("bot", 99, "bot answer", True),
        ChatTurn("bob", 2, "final question", False),
    ]

    messages = build_messages(
        " system prompt ",
        turns,
        context_blocks=["  extra context  "],
        closing_text="Ответь кратко.",
    )

    assert messages == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "extra context"},
        {"role": "user", "content": "История:\nalice: first line second line\nbot: bot answer\nbob: final question"},
        {"role": "user", "content": "Ответь кратко."},
    ]


def test_build_messages_combines_trailing_user_messages() -> None:
    turns = [
        ChatTurn("alice", 1, "older context", False),
        ChatTurn("bob", 2, "first follow-up", False),
        ChatTurn("bob", 2, "second follow-up", False),
    ]

    messages = build_messages("system prompt", turns)

    assert messages[-2]["content"] == "История:\nalice: older context"
    assert messages[-1]["content"] == "first follow-up\n\nsecond follow-up"


def test_build_messages_uses_closing_text_fallback_without_current_user_message() -> None:
    turns = [ChatTurn("bot", 999, "already answered", True)]

    messages = build_messages("system prompt", turns, closing_text="Ответь кратко.")

    assert messages[-2]["content"] == "История:\nbot: already answered"
    assert messages[-1]["content"] == "Ответь кратко."


def test_build_system_prompt_uses_style_default_and_focus_suffix() -> None:
    prompt = build_system_prompt(
        {"style": "gopnik"},
        focus_text='что там "сегодня"?',
    )

    assert DEFAULT_CHAT_PROMPT in prompt
    assert DEFAULT_STYLE_PROMPTS["gopnik"] in prompt
    assert DEFAULT_INTERJECT_SUFFIX not in prompt
    assert "что там 'сегодня'?" in prompt
    assert 'Вопрос: "что там \'сегодня\'?". Ответь одним сообщением.' in prompt
