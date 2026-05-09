from __future__ import annotations

from app.bot.router_games import build_games_menu_markup, format_first_winner_message


def test_build_games_menu_returns_inline_keyboard_with_guess() -> None:
    markup = build_games_menu_markup()
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any(btn.callback_data == "games:guess" for btn in flat)
    assert any("Угадай" in btn.text for btn in flat)


def test_format_first_winner_message_mentions_user_and_penalty() -> None:
    msg = format_first_winner_message(display_name="Андрей", username="andrey")
    assert "Андрей" in msg or "@andrey" in msg
    assert "1 очко" in msg


def test_format_first_winner_message_falls_back_to_display_name_without_username() -> None:
    msg = format_first_winner_message(display_name="Bob", username=None)
    assert "Bob" in msg
    assert "@" not in msg
