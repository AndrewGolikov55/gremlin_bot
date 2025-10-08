from __future__ import annotations

import logging
import os
import re
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiogram import Bot
from ..models.chat import Chat
from ..models.message import Message
from ..models.persona import StylePrompt
from ..models.user import User
from ..services.persona import StylePromptService, BASE_STYLE_DATA
from ..services.settings import SettingsService
from ..services.app_config import AppConfigService
from ..services.roulette import RouletteService

STYLE_ORDER = ["standup", "gopnik", "boss", "zoomer", "jarvis"]
BOOTSTRAP_CSS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
BOOTSTRAP_JS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"

logger = logging.getLogger("admin")


def create_admin_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: SettingsService,
    personas: StylePromptService,
    app_config: AppConfigService,
    bot: Bot,
    roulette: RouletteService,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    async def get_session():
        async with sessionmaker() as session:
            yield session

    def require_token(token: str = Query(default=None, alias="token")) -> str:
        expected = os.getenv("ADMIN_TOKEN")
        if not expected:
            raise HTTPException(status_code=503, detail="Admin token is not configured")
        if token != expected:
            raise HTTPException(status_code=401, detail="Invalid admin token")
        return token

    @router.get("/chats", response_class=HTMLResponse)
    async def list_chats(
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        result = await session.execute(select(Chat).order_by(Chat.created_at.desc()))
        chats = result.scalars().all()
        body = _render_chats_body(chats, token)
        return HTMLResponse(_render_page("Чаты", token, "chats", body))

    @router.get("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_settings_view(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        conf = await settings.get_all(chat_id)
        style_options = await _ensure_style_options(personas)
        app_conf = await app_config.get_all()
        body = _render_chat_settings_body(chat, conf, app_conf, style_options, token, saved=False)
        return HTMLResponse(_render_page(f"Чат {chat.id}", token, "chats", body))

    @router.post("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_settings_update(
        chat_id: int,
        style: str = Form(...),
        revive_enabled: bool = Form(False),
        revive_days: int = Form(2),
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")

        revive_days = max(1, min(30, revive_days))
        revive_hours = revive_days * 24

        style_options = await _ensure_style_options(personas)
        allowed_styles = {slug for slug, _ in style_options}
        if style not in allowed_styles:
            raise HTTPException(status_code=400, detail="Unknown style persona")

        await settings.set(chat_id, "style", style)
        await settings.set(chat_id, "revive_enabled", bool(revive_enabled))
        await settings.set(chat_id, "revive_after_hours", revive_hours)

        conf = await settings.get_all(chat_id)
        app_conf = await app_config.get_all()
        body = _render_chat_settings_body(chat, conf, app_conf, style_options, token, saved=True)
        return HTMLResponse(_render_page(f"Чат {chat.id}", token, "chats", body))

    @router.post("/chats/{chat_id}/roulette/reset", response_class=HTMLResponse)
    async def reset_daily_winner(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        await roulette.reset_daily_winner(chat_id)
        conf = await settings.get_all(chat_id)
        app_conf = await app_config.get_all()
        style_options = await _ensure_style_options(personas)
        body = _render_chat_settings_body(chat, conf, app_conf, style_options, token, saved=True, note="Победитель дня сброшен")
        return HTMLResponse(_render_page(f"Чат {chat.id}", token, "chats", body))

    @router.get("/chats/{chat_id}/history", response_class=HTMLResponse)
    async def chat_history_view(
        chat_id: int,
        page: int = Query(1, ge=1),
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")

        page_size = 50
        offset = (page - 1) * page_size

        stmt = (
            select(Message, User)
            .outerjoin(User, User.tg_id == Message.user_id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.date.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await session.execute(stmt)
        rows = result.fetchall()

        total_stmt = select(func.count()).select_from(Message).where(Message.chat_id == chat_id)
        total = (await session.execute(total_stmt)).scalar() or 0

        body = _render_history_body(chat, rows, page, page_size, total, token)
        return HTMLResponse(_render_page(f"История чата {chat.id}", token, "chats", body))

    @router.get("/styles", response_class=HTMLResponse)
    async def style_prompts_view(token: str = Depends(require_token)) -> str:
        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)
        body = _render_style_prompts_body(prompts, token)
        return HTMLResponse(_render_page("Персоны", token, "styles", body))

    @router.get("/config", response_class=HTMLResponse)
    async def app_config_view(token: str = Depends(require_token)) -> str:
        conf = await app_config.get_all()
        body = _render_app_config_body(conf, token)
        return HTMLResponse(_render_page("Настройки", token, "config", body))

    @router.post("/config", response_class=HTMLResponse)
    async def app_config_update(
        context_turns: int = Form(...),
        max_length: int = Form(...),
        context_tokens: int = Form(...),
        interject_p: int = Form(...),
        interject_cooldown: int = Form(...),
        summary_daily_limit: int = Form(...),
        llm_daily_limit: int = Form(...),
        token: str = Depends(require_token),
    ) -> str:
        errors: list[str] = []

        context_turns = max(5, min(150, context_turns))
        context_tokens = max(2000, min(60000, context_tokens))
        interject_p = max(0, min(100, interject_p))
        interject_cooldown = max(10, min(3600, interject_cooldown))
        summary_daily_limit = max(0, min(20, summary_daily_limit))
        llm_daily_limit = max(0, min(5000, llm_daily_limit))

        if max_length < 0:
            max_length = 0
        elif max_length > 0:
            if max_length < 50:
                max_length = 50
            elif max_length > 2000:
                max_length = 2000

        try:
            await app_config.set("context_max_turns", context_turns)
            await app_config.set("max_length", max_length)
            await app_config.set("context_max_prompt_tokens", context_tokens)
            await app_config.set("interject_p", interject_p)
            await app_config.set("interject_cooldown", interject_cooldown)
            await app_config.set("summary_daily_limit", summary_daily_limit)
            await app_config.set("llm_daily_limit", llm_daily_limit)
        except Exception as exc:
            errors.append(str(exc))

        conf = await app_config.get_all()
        body = _render_app_config_body(conf, token, saved=not errors, errors=errors)
        return HTMLResponse(_render_page("Настройки", token, "config", body))

    @router.get("/messages", response_class=HTMLResponse)
    async def broadcast_view(
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chats = await _fetch_chats(session)
        body = _render_broadcast_body(chats, token)
        return HTMLResponse(_render_page("Сообщения", token, "messages", body))

    @router.post("/messages", response_class=HTMLResponse)
    async def broadcast_send(
        message_text: str = Form(...),
        scope: str = Form("all"),
        chat_id: int | None = Form(None),
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        message_text = message_text.strip()
        errors: list[str] = []
        chats = await _fetch_chats(session)
        chat_ids = [chat.id for chat in chats]

        if not message_text:
            errors.append("Текст сообщения не может быть пустым.")
        targets: list[int] = []
        if scope == "single":
            if chat_id is None:
                errors.append("Выберите чат.")
            elif chat_id not in chat_ids:
                errors.append("Чат не найден.")
            else:
                targets = [chat_id]
        else:
            targets = chat_ids

        delivered = 0
        if not errors and targets:
            for target in targets:
                try:
                    await bot.send_message(target, message_text)
                    delivered += 1
                except Exception as exc:
                    logger.exception("Broadcast failed chat=%s", target)
                    errors.append(f"Не удалось отправить в чат {target}: {exc}")
        saved = delivered > 0 and not errors
        body = _render_broadcast_body(
            chats,
            token,
            saved=saved,
            errors=None if saved else errors,
            last_message=message_text,
            last_scope=scope,
            last_chat_id=chat_id,
            delivered=delivered,
        )
        return HTMLResponse(_render_page("Сообщения", token, "messages", body))

    @router.post("/styles", response_class=HTMLResponse)
    async def style_prompts_update(
        request: Request,
        token: str = Depends(require_token),
    ) -> str:
        form = await request.form()
        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)

        errors: list[str] = []

        # deletions
        for key, value in form.items():
            if key.startswith("delete__") and value:
                slug = key.split("__", 1)[1]
                try:
                    await personas.delete(slug)
                except ValueError as exc:
                    errors.append(str(exc))

        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)

        updates: dict[str, dict[str, str]] = {}
        for key, value in form.items():
            if key.startswith("display__"):
                slug = key.split("__", 1)[1]
                updates.setdefault(slug, {})["display"] = value
            elif key.startswith("prompt__"):
                slug = key.split("__", 1)[1]
                updates.setdefault(slug, {})["prompt"] = value

        for slug, data in updates.items():
            record = entries.get(slug)
            if record is None:
                errors.append(f"Стиль {slug} не найден")
                continue
            prompt = data.get("prompt", record.prompt)
            display = data.get("display", record.display_name)
            try:
                await personas.set(slug, prompt, display_name=display)
            except ValueError as exc:
                errors.append(str(exc))

        new_style = (form.get("new_style") or "").strip().lower()
        new_display = (form.get("new_display") or "").strip()
        new_prompt = (form.get("new_prompt") or "").strip()
        if new_style or new_display or new_prompt:
            if not new_style or not new_display or not new_prompt:
                errors.append("Для новой персоны заполните код, название и промт полностью")
            elif not re.fullmatch(r"[a-z0-9_-]{3,32}", new_style):
                errors.append("Код новой персоны должен быть 3-32 символа, латиница/цифры/-/_")
            elif new_style in STYLE_ORDER:
                errors.append("Этот код зарезервирован для базовой персоны")
            elif new_style in entries:
                errors.append("Персона с таким кодом уже существует")
            else:
                try:
                    await personas.set(new_style, new_prompt, display_name=new_display)
                except ValueError as exc:
                    errors.append(str(exc))

        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)
        saved = not errors
        body = _render_style_prompts_body(prompts, token, saved=saved, errors=errors)
        return HTMLResponse(_render_page("Персоны", token, "styles", body))

    return router


def _build_url(path: str, token: str | None, **params: object) -> str:
    query: list[tuple[str, object]] = []
    if token:
        query.append(("token", token))
    for key, value in params.items():
        if value is None:
            continue
        query.append((key, value))
    if not query:
        return path
    return f"{path}?{urlencode([(k, str(v)) for k, v in query])}"


def _render_page(title: str, token: str | None, active: str, body: str) -> str:
    nav_items = [
        ("chats", "Чаты", _build_url("/admin/chats", token)),
        ("config", "Настройки", _build_url("/admin/config", token)),
        ("messages", "Сообщения", _build_url("/admin/messages", token)),
        ("styles", "Персоны", _build_url("/admin/styles", token)),
    ]
    nav_html = "".join(
        f"<li class='nav-item'><a class='nav-link{' active' if key == active else ''}' href='{escape(url)}'>{escape(label)}</a></li>"
        for key, label, url in nav_items
    )
    return f"""<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <link rel='stylesheet' href='{BOOTSTRAP_CSS}'>
  <title>{escape(title)} — Gremlin Admin</title>
</head>
<body class='bg-light'>
  <nav class='navbar navbar-expand-lg navbar-dark bg-dark'>
    <div class='container-fluid'>
      <a class='navbar-brand' href='{escape(_build_url('/admin/chats', token))}'>Gremlin Admin</a>
      <button class='navbar-toggler' type='button' data-bs-toggle='collapse' data-bs-target='#adminNav' aria-controls='adminNav' aria-expanded='false' aria-label='Toggle navigation'>
        <span class='navbar-toggler-icon'></span>
      </button>
      <div class='collapse navbar-collapse' id='adminNav'>
        <ul class='navbar-nav me-auto mb-2 mb-lg-0'>
          {nav_html}
        </ul>
      </div>
    </div>
  </nav>
  <main>{body}</main>
  <script src='{BOOTSTRAP_JS}'></script>
</body>
</html>"""


def _render_chats_body(chats: list[Chat], token: str | None) -> str:
    rows = []
    for chat in chats:
        status = "bg-success" if chat.is_active else "bg-secondary"
        status_label = "ON" if chat.is_active else "OFF"
        settings_url = _build_url(f"/admin/chats/{chat.id}", token)
        history_url = _build_url(f"/admin/chats/{chat.id}/history", token)
        rows.append(
            "<tr>"
            f"<td class='text-nowrap'>{escape(str(chat.id))}</td>"
            f"<td>{escape(chat.title)}</td>"
            f"<td><span class='badge {status}'>{status_label}</span></td>"
            "<td class='text-end'>"
            f"<a class='btn btn-sm btn-primary me-1' href='{escape(settings_url)}'>Настройки</a>"
            f"<a class='btn btn-sm btn-outline-secondary' href='{escape(history_url)}'>История</a>"
            "</td>"
            "</tr>"
        )

    table = (
        "<div class='table-responsive'><table class='table table-hover align-middle'>"
        "<thead><tr><th scope='col'>ID</th><th scope='col'>Название</th><th scope='col'>Статус</th><th scope='col' class='text-end'>Действия</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else "<div class='alert alert-info'>Чаты ещё не созданы.</div>"
    )

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        "<h1 class='h3 mb-0'>Чаты</h1>"
        f"<span class='text-muted'>Всего: {len(chats)}</span>"
        "</div>"
        f"{table}"
        "</div>"
    )


def _render_chat_settings_body(
    chat: Chat,
    conf: dict[str, object],
    app_conf: dict[str, object],
    styles: list[tuple[str, str]],
    token: str | None,
    saved: bool,
    note: str | None = None,
) -> str:
    revive_enabled = bool(conf.get("revive_enabled", False))
    revive_hours = int(conf.get("revive_after_hours", 48) or 48)
    revive_days = max(1, revive_hours // 24)
    style_current = str(conf.get("style", styles[0][0] if styles else "standup"))
    custom_title = conf.get("roulette_custom_title")
    title_label = custom_title if custom_title else "по умолчанию"

    alerts = []
    if saved:
        alerts.append("<div class='alert alert-success'>Сохранено</div>")
    if note:
        alerts.append(f"<div class='alert alert-info'>{escape(note)}</div>")
    message = "".join(alerts)
    history_url = _build_url(f"/admin/chats/{chat.id}/history", token)

    options_html = "".join(
        f"<option value='{escape(slug)}'{' selected' if slug == style_current else ''}>{escape(title)}</option>"
        for slug, title in styles
    )

    global_settings = (
        f"<tr><td>Контекст</td><td>{escape(str(app_conf.get('context_max_turns', 100)))} сообщений</td></tr>"
        f"<tr><td>Макс. длина ответа</td><td>{escape(str(app_conf.get('max_length', 0)))} символов</td></tr>"
        f"<tr><td>Лимит окна</td><td>{escape(str(app_conf.get('context_max_prompt_tokens', 32000)))} токенов</td></tr>"
        f"<tr><td>Вмешательства</td><td>{escape(str(app_conf.get('interject_p', 0)))}% шанс, кулдаун {escape(str(app_conf.get('interject_cooldown', 60)))}с</td></tr>"
        f"<tr><td>Прозвище рулетки</td><td>{escape(title_label)}</td></tr>"
    )

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        f"<div><h1 class='h3 mb-0'>Настройки чата</h1><div class='text-muted'>{escape(chat.title)}</div></div>"
        f"<a class='btn btn-outline-secondary' href='{escape(history_url)}'>История чата</a>"
        "</div>"
        f"{message}"
        "<form method='post' class='row g-3'>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Персона</label>"
        f"<select class='form-select' name='style'>{options_html}</select>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Оживление через (дней)</label>"
        f"<input class='form-control' type='number' name='revive_days' min='1' max='30' value='{revive_days}'>"
        "</div>"
        "<div class='col-md-6 d-flex align-items-end'>"
        f"<div class='form-check'><input class='form-check-input' type='checkbox' name='revive_enabled' value='1'{' checked' if revive_enabled else ''}>"
        "<label class='form-check-label'>Оживление при тишине</label></div>"
        "</div>"
        "<div class='col-12'>"
        "<button class='btn btn-primary' type='submit'>Сохранить</button>"
        "</div>"
        "</form>"
        f"<form method='post' action='{escape(_build_url(f'/admin/chats/{chat.id}/roulette/reset', token))}' class='mt-3'>"
        "<button class='btn btn-outline-danger btn-sm' type='submit'>Сбросить победителя дня</button>"
        "</form>"
        "<div class='card mt-4'>"
        "<div class='card-header'>Глобальные параметры</div>"
        "<div class='card-body p-0'>"
        "<div class='table-responsive mb-0'>"
        f"<table class='table table-sm table-borderless mb-0'><tbody>{global_settings}</tbody></table>"
        "</div>"
        "<div class='p-3 text-muted small'>Изменить можно на вкладке «Настройки».</div>"
        "</div></div>"
        "</div>"
    )


def _render_history_body(
    chat: Chat,
    rows: list[tuple[Message, User | None]],
    page: int,
    page_size: int,
    total: int,
    token: str | None,
) -> str:
    items = []
    for message, user in rows:
        speaker = user.username if user and user.username else str(message.user_id)
        date_str = message.date.strftime("%Y-%m-%d %H:%M:%S") if message.date else "—"
        text = escape((message.text or "").replace("\n", " "))
        badge = "bg-secondary" if message.is_bot else "bg-info"
        author_label = "бот" if message.is_bot else "пользователь"
        items.append(
            "<tr>"
            f"<td class='text-nowrap'>{escape(str(message.message_id))}</td>"
            f"<td class='text-nowrap'>{escape(date_str)}</td>"
            f"<td>{escape(speaker)}</td>"
            f"<td><span class='badge {badge}'>{author_label}</span></td>"
            f"<td>{text}</td>"
            "</tr>"
        )

    table = (
        "<div class='table-responsive'><table class='table table-striped align-middle'>"
        "<thead><tr><th>ID</th><th>Время</th><th>Автор</th><th>Тип</th><th>Текст</th></tr></thead>"
        f"<tbody>{''.join(items)}</tbody></table></div>"
        if items
        else "<div class='alert alert-warning'>Сообщений для отображения нет.</div>"
    )

    max_page = (total + page_size - 1) // page_size if total else 1
    has_prev = page > 1
    has_next = page * page_size < total
    pagination = ""
    if has_prev or has_next:
        prev_url = _build_url(f"/admin/chats/{chat.id}/history", token, page=page - 1 if has_prev else 1)
        next_url = _build_url(f"/admin/chats/{chat.id}/history", token, page=page + 1 if has_next else page)
        pagination = (
            "<nav><ul class='pagination'>"
            f"<li class='page-item{' disabled' if not has_prev else ''}'><a class='page-link' href='{escape(prev_url)}'>Предыдущая</a></li>"
            f"<li class='page-item disabled'><span class='page-link'>Страница {page} из {max(1, max_page)}</span></li>"
            f"<li class='page-item{' disabled' if not has_next else ''}'><a class='page-link' href='{escape(next_url)}'>Следующая</a></li>"
            "</ul></nav>"
        )

    settings_url = _build_url(f"/admin/chats/{chat.id}", token)

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        f"<div><h1 class='h3 mb-0'>История чата</h1><div class='text-muted'>{escape(chat.title)}</div></div>"
        f"<a class='btn btn-outline-secondary' href='{escape(settings_url)}'>← Настройки</a>"
        "</div>"
        f"<p class='text-muted'>Всего сообщений: {total}</p>"
        f"{table}"
        f"{pagination}"
        "</div>"
    )


def _render_app_config_body(
    conf: dict[str, object],
    token: str | None,
    *,
    saved: bool = False,
    errors: list[str] | None = None,
) -> str:
    messages = []
    if saved and not errors:
        messages.append("<div class='alert alert-success'>Изменения сохранены</div>")
    if errors:
        items = "".join(f"<li>{escape(err)}</li>" for err in errors)
        messages.append(f"<div class='alert alert-danger'><ul class='mb-0'>{items}</ul></div>")

    context_turns = int(conf.get("context_max_turns", 100) or 100)
    max_length = int(conf.get("max_length", 0) or 0)
    context_tokens = int(conf.get("context_max_prompt_tokens", 32000) or 32000)
    interject_p = int(conf.get("interject_p", 0) or 0)
    interject_cooldown = int(conf.get("interject_cooldown", 60) or 60)
    summary_daily_limit = int(conf.get("summary_daily_limit", 2) or 0)
    llm_daily_limit = int(conf.get("llm_daily_limit", 200) or 0)

    chats_url = _build_url("/admin/chats", token)

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        "<h1 class='h3 mb-0'>Глобальные настройки</h1>"
        f"<a class='btn btn-outline-secondary' href='{escape(chats_url)}'>← К чатам</a>"
        "</div>"
        + "".join(messages)
        + "<form method='post' class='row g-3'>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Контекст (5-150 сообщений)</label>"
        f"<input class='form-control' type='number' name='context_turns' min='5' max='150' value='{context_turns}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Лимит окна (токены 2k-60k)</label>"
        f"<input class='form-control' type='number' name='context_tokens' min='2000' max='60000' value='{context_tokens}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Макс. длина ответа (0 = без ограничения)</label>"
        f"<input class='form-control' type='number' name='max_length' min='0' max='2000' value='{max_length}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Вероятность вмешательства (0-100%)</label>"
        f"<input class='form-control' type='number' name='interject_p' min='0' max='100' value='{interject_p}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Кулдаун вмешательств (10-3600 сек)</label>"
        f"<input class='form-control' type='number' name='interject_cooldown' min='10' max='3600' value='{interject_cooldown}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Сводки в сутки (0 = без ограничения)</label>"
        f"<input class='form-control' type='number' name='summary_daily_limit' min='0' max='20' value='{summary_daily_limit}'>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Запросы к модели в сутки (0 = без ограничения)</label>"
        f"<input class='form-control' type='number' name='llm_daily_limit' min='0' max='5000' value='{llm_daily_limit}'>"
        "</div>"
        "<div class='col-12'>"
        "<button class='btn btn-primary' type='submit'>Сохранить</button>"
        "</div>"
        "</form>"
        "</div>"
    )


def _render_style_prompts_body(
    prompts: list[dict[str, object]],
    token: str | None,
    *,
    saved: bool = False,
    errors: list[str] | None = None,
) -> str:
    messages = []
    if saved and not errors:
        messages.append("<div class='alert alert-success'>Изменения сохранены</div>")
    if errors:
        msg = "".join(f"<li>{escape(err)}</li>" for err in errors)
        messages.append(f"<div class='alert alert-danger'><ul class='mb-0'>{msg}</ul></div>")

    fields = []
    for item in prompts:
        style = str(item["style"])
        display = str(item["display_name"])
        prompt = str(item["prompt"])
        is_default = bool(item.get("is_default", False))
        delete_control = (
            "<div class='form-check form-switch mt-2'>"
            f"<input class='form-check-input' type='checkbox' name='delete__{escape(style)}' value='1'>"
            "<label class='form-check-label'>Удалить эту персону</label>"
            "</div>"
        ) if not is_default else ""
        fields.append(
            "<div class='card mb-4'>"
            "<div class='card-body'>"
            f"<h2 class='h5 card-title'>{escape(display)} <span class='text-muted'>({escape(style)})</span>"
            f"{' <span class=\'badge bg-secondary ms-2\'>базовая</span>' if is_default else ''}</h2>"
            "<div class='mb-3'>"
            "<label class='form-label'>Название</label>"
            f"<input class='form-control' type='text' name='display__{escape(style)}' value='{escape(display)}' maxlength='120'>"
            "</div>"
            "<div class='mb-3'>"
            "<label class='form-label'>Промт</label>"
            f"<textarea class='form-control' name='prompt__{escape(style)}' rows='6'>{escape(prompt)}</textarea>"
            "</div>"
            f"{delete_control}"
            "</div></div>"
        )

    new_persona_card = (
        "<div class='card border-dashed'>"
        "<div class='card-body'>"
        "<h2 class='h5 card-title'>Добавить новую персону</h2>"
        "<div class='row g-3'>"
        "<div class='col-md-4'>"
        "<label class='form-label'>Код</label>"
        "<input class='form-control' type='text' name='new_style' maxlength='32' placeholder='например, chill'>"
        "</div>"
        "<div class='col-md-4'>"
        "<label class='form-label'>Название</label>"
        "<input class='form-control' type='text' name='new_display' maxlength='120' placeholder='Отображаемое имя'>"
        "</div>"
        "<div class='col-12'>"
        "<label class='form-label'>Промт</label>"
        "<textarea class='form-control' name='new_prompt' rows='6' placeholder='Описание поведения'></textarea>"
        "</div>"
        "</div>"
        "</div></div>"
    )

    chats_url = _build_url("/admin/chats", token)

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        "<h1 class='h3 mb-0'>Персоны</h1>"
        f"<a class='btn btn-outline-secondary' href='{escape(chats_url)}'>← К чатам</a>"
        "</div>"
        + "".join(messages)
        + "<form method='post'>"
        + "".join(fields)
        + new_persona_card
        + "<div class='d-flex justify-content-end'><button class='btn btn-primary' type='submit'>Сохранить изменения</button></div>"
        "</form>"
        "</div>"
    )


def _merge_style_entries(entries: dict[str, StylePrompt]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[str] = set()

    for slug in STYLE_ORDER:
        record = entries.get(slug)
        base_data = BASE_STYLE_DATA.get(slug, {})
        if record is None and not base_data:
            continue
        merged.append(
            {
                "style": slug,
                "display_name": record.display_name if record else base_data.get("display_name", slug),
                "prompt": record.prompt if record else base_data.get("prompt", ""),
                "is_default": True,
            }
        )
        seen.add(slug)

    extras = [
        (slug, record)
        for slug, record in entries.items()
        if slug not in seen
    ]
    extras.sort(key=lambda item: item[1].display_name.lower())

    for slug, record in extras:
        merged.append(
            {
                "style": slug,
                "display_name": record.display_name,
                "prompt": record.prompt,
                "is_default": False,
            }
        )

    return merged


async def _ensure_style_options(personas: StylePromptService) -> list[tuple[str, str]]:
    styles = await personas.list_styles()
    if styles:
        return styles
    return [(slug, data["display_name"]) for slug, data in BASE_STYLE_DATA.items()]


async def _fetch_chats(session: AsyncSession) -> list[Chat]:
    res = await session.execute(select(Chat).order_by(Chat.created_at.desc()))
    return list(res.scalars())


def _render_broadcast_body(
    chats: list[Chat],
    token: str | None,
    *,
    saved: bool = False,
    errors: list[str] | None = None,
    last_message: str | None = None,
    last_scope: str = "all",
    last_chat_id: int | None = None,
    delivered: int = 0,
) -> str:
    messages = []
    if saved:
        messages.append(f"<div class='alert alert-success'>Отправлено в {delivered} чатов.</div>")
    if errors:
        items = "".join(f"<li>{escape(err)}</li>" for err in errors)
        messages.append(f"<div class='alert alert-danger'><ul class='mb-0'>{items}</ul></div>")

    options = "".join(
        f"<option value='{chat.id}'{' selected' if last_chat_id == chat.id else ''}>{escape(chat.title)}</option>"
        for chat in chats
    )

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        "<h1 class='h3 mb-0'>Сообщения</h1>"
        f"<span class='text-muted'>Доступно чатов: {len(chats)}</span>"
        "</div>"
        + "".join(messages)
        + "<form method='post' class='row g-3'>"
        "<div class='col-12'>"
        "<label class='form-label'>Текст сообщения</label>"
        f"<textarea class='form-control' name='message_text' rows='4'>{escape(last_message or '')}</textarea>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Куда отправить</label>"
        "<select class='form-select' name='scope'>"
        f"<option value='all'{' selected' if last_scope != 'single' else ''}>Во все чаты</option>"
        f"<option value='single'{' selected' if last_scope == 'single' else ''}>Только выбранный чат</option>"
        "</select>"
        "</div>"
        "<div class='col-md-6'>"
        "<label class='form-label'>Чат</label>"
        f"<select class='form-select' name='chat_id'><option value=''>—</option>{options}</select>"
        "</div>"
        "<div class='col-12'>"
        "<button class='btn btn-primary' type='submit'>Отправить</button>"
        "</div>"
        "</form>"
        "</div>"
    )
