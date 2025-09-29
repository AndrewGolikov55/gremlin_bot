from __future__ import annotations

import os
from html import escape
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..models.persona import StylePrompt
from ..services.settings import SettingsService
from ..services.persona import StylePromptService, BASE_STYLE_DATA


STYLE_ORDER = ["standup", "gopnik", "boss", "zoomer", "jarvis"]


def create_admin_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: SettingsService,
    personas: StylePromptService,
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
        rows = "".join(
            f"<tr><td>{escape(str(chat.id))}</td><td>{escape(chat.title)}</td>"
            f"<td>{'ON' if chat.is_active else 'OFF'}</td>"
            f"<td><a href=\"/admin/chats/{chat.id}?token={escape(token)}\">Настроить</a></td></tr>"
            for chat in chats
        )
        html = (
            "<html><head><title>Chats</title></head><body>"
            "<h1>Список чатов</h1>"
            f"<p><a href='/admin/styles?token={escape(token)}'>Настроить персоны</a></p>"
            "<p>Токен проверен. Нажмите «Настроить», чтобы изменить параметры.</p>"
            "<table border='1' cellpadding='6'>"
            "<tr><th>ID</th><th>Название</th><th>Статус</th><th></th></tr>"
            f"{rows or '<tr><td colspan=4>Нет чатов</td></tr>'}"
            "</table>"
            "</body></html>"
        )
        return HTMLResponse(html)

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
        style_options = await personas.list_styles()
        if not style_options:
            style_options = [(slug, data["display_name"]) for slug, data in BASE_STYLE_DATA.items()]
        return HTMLResponse(_render_settings_page(chat, conf, style_options, token=token))

    @router.post("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_settings_update(
        chat_id: int,
        max_length: int = Form(...),
        context_turns: int = Form(...),
        context_tokens: int = Form(...),
        style: str = Form(...),
        probability: int = Form(...),
        cooldown: int = Form(...),
        revive_enabled: bool = Form(False),
        revive_days: int = Form(2),
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")

        if max_length < 0:
            max_length = 0
        elif max_length > 0:
            max_length = max(50, min(2000, max_length))
        context_turns = max(5, min(150, context_turns))
        context_tokens = max(2000, min(60000, context_tokens))
        probability = max(0, min(100, probability))
        cooldown = max(10, min(3600, cooldown))
        revive_days = max(1, min(30, revive_days))
        revive_hours = revive_days * 24

        style_options = await personas.list_styles()
        if not style_options:
            style_options = [(slug, data["display_name"]) for slug, data in BASE_STYLE_DATA.items()]
        allowed_styles = {slug for slug, _ in style_options}
        if style not in allowed_styles:
            raise HTTPException(status_code=400, detail="Unknown style persona")

        await settings.set(chat_id, "max_length", max_length)
        await settings.set(chat_id, "context_max_turns", context_turns)
        await settings.set(chat_id, "context_max_prompt_tokens", context_tokens)
        await settings.set(chat_id, "style", style)
        await settings.set(chat_id, "interject_p", probability)
        await settings.set(chat_id, "interject_cooldown", cooldown)
        await settings.set(chat_id, "revive_enabled", bool(revive_enabled))
        await settings.set(chat_id, "revive_after_hours", revive_hours)

        conf = await settings.get_all(chat_id)
        page = _render_settings_page(chat, conf, style_options, saved=True, token=token)
        return HTMLResponse(page)

    @router.get("/styles", response_class=HTMLResponse)
    async def style_prompts_view(token: str = Depends(require_token)) -> str:
        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)
        return HTMLResponse(_render_style_prompts(prompts, token=token))

    @router.post("/styles", response_class=HTMLResponse)
    async def style_prompts_update(
        request: Request,
        token: str = Depends(require_token),
    ) -> str:
        form = await request.form()
        entries = await personas.get_entries()
        prompts = _merge_style_entries(entries)

        errors: list[str] = []

        # Process deletions first
        for key, value in form.items():
            if key.startswith("delete__") and value:
                slug = key.split("__", 1)[1]
                try:
                    await personas.delete(slug)
                except ValueError as exc:
                    errors.append(str(exc))

        # Refresh entries after deletions
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
            prompt = data.get("prompt")
            display = data.get("display")
            existing = entries.get(slug)
            if existing is None:
                errors.append(f"Стиль {slug} не найден")
                continue
            final_prompt = prompt if prompt is not None else existing.prompt
            final_display = display if display is not None else existing.display_name
            try:
                await personas.set(slug, final_prompt, display_name=final_display)
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
        page = _render_style_prompts(prompts, saved=saved, token=token, errors=errors)
        return HTMLResponse(page)

    return router


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


def _render_settings_page(
    chat: Chat,
    conf: dict[str, object],
    styles: list[tuple[str, str]],
    saved: bool = False,
    token: str | None = None,
) -> str:
    max_length_raw = conf.get("max_length", 0)
    max_length = int(max_length_raw) if max_length_raw is not None else 0
    context_turns = int(conf.get("context_max_turns", 100) or 100)
    context_tokens = int(conf.get("context_max_prompt_tokens", 32000) or 32000)
    probability = int(conf.get("interject_p", 0) or 0)
    cooldown = int(conf.get("interject_cooldown", 60) or 60)
    revive_enabled = bool(conf.get("revive_enabled", False))
    revive_hours = int(conf.get("revive_after_hours", 48) or 48)
    revive_days = max(1, revive_hours // 24)
    message = "<p style='color:green;'>Сохранено</p>" if saved else ""
    suffix = f"?token={escape(token)}" if token else ""
    styles_link = f"/admin/styles?token={escape(token)}" if token else "/admin/styles"
    style_current = str(conf.get("style", styles[0][0] if styles else "standup"))

    return (
        "<html><head><title>Chat Settings</title></head><body>"
        f"<h1>Чат {escape(chat.title)} ({chat.id})</h1>"
        f"{message}"
        "<form method='post'>"
        "<label>Макс. длина ответа (0 = без ограничения, иначе 50-2000): <input type='number' name='max_length' min='0' max='2000' value='" + str(max_length) + "'></label><br><br>"
        "<label>Контекст (5-150 сообщений): <input type='number' name='context_turns' min='5' max='150' value='" + str(context_turns) + "'></label><br><br>"
        "<label>Лимит окна (токены 2k-60k): <input type='number' name='context_tokens' min='2000' max='60000' value='" + str(context_tokens) + "'></label><br><br>"
        + "<label>Стиль: <select name='style'>"
        + "".join(
            "<option value='" + escape(slug) + ("' selected>" if slug == style_current else "'>") + escape(title) + "</option>"
            for slug, title in styles
        )
        + "</select></label><br><br>"
        "<label>Вероятность вмешательства (0-100%): <input type='number' name='probability' min='0' max='100' value='" + str(probability) + "'></label><br><br>"
        "<label>Кулдаун (10-3600 сек): <input type='number' name='cooldown' min='10' max='3600' value='" + str(cooldown) + "'></label><br><br>"
        f"<label><input type='checkbox' name='revive_enabled' value='1' {'checked' if revive_enabled else ''}> Оживление при тишине</label><br><br>"
        "<label>Оживление через (дней): <input type='number' name='revive_days' min='1' max='30' value='" + str(revive_days) + "'></label><br><br>"
        "<button type='submit'>Сохранить</button>"
        "</form>"
        + "<hr>"
        + "<h2>Персоны</h2>"
        + "<p>Доступные стили: "
        + ", ".join(
            f"<b>{escape(title)}</b> (<code>{escape(slug)}</code>)" for slug, title in styles
        )
        + "</p>"
        + (f"<p><a href='{styles_link}'>Настроить промты →</a></p>"
           if styles_link else "")
        + (f"<p><a href='/admin/chats{suffix}'>← Назад к списку</a></p>" if token else "")
        + "</body></html>"
    )


def _render_style_prompts(
    prompts: list[dict[str, object]],
    *,
    saved: bool = False,
    token: str | None = None,
    errors: list[str] | None = None,
) -> str:
    messages = []
    if saved and not errors:
        messages.append("<p style='color:green;'>Сохранено</p>")
    if errors:
        escaped_errors = "".join(f"<li>{escape(err)}</li>" for err in errors)
        messages.append(f"<div style='color:red;'><ul>{escaped_errors}</ul></div>")
    suffix = f"?token={escape(token)}" if token else ""

    rows: list[str] = []
    for item in prompts:
        style = str(item["style"])
        display = str(item["display_name"])
        prompt = str(item["prompt"])
        is_default = bool(item.get("is_default", False))
        rows.append("<fieldset style='margin-bottom:16px;'>")
        rows.append(
            "<legend>" + escape(display) + (" (базовая)" if is_default else "") + " — " + escape(style) + "</legend>"
        )
        rows.append(
            "<label>Название:<br><input type='text' name='display__"
            + escape(style)
            + "' value='"
            + escape(display)
            + "' maxlength='120'></label><br><br>"
        )
        rows.append(
            "<label>Промт:<br><textarea name='prompt__"
            + escape(style)
            + "' rows='8' cols='120'>"
            + escape(prompt)
            + "</textarea></label><br>"
        )
        if not is_default:
            rows.append(
                "<label><input type='checkbox' name='delete__"
                + escape(style)
                + "' value='1'> Удалить эту персону</label><br>"
            )
        rows.append("</fieldset>")

    rows.append("<hr><h2>Добавить новую персону</h2>")
    rows.append(
        "<label>Код (латиница/цифры/-/_): <input type='text' name='new_style' maxlength='32'></label><br><br>"
        "<label>Название: <input type='text' name='new_display' maxlength='120'></label><br><br>"
        "<label>Промт:<br><textarea name='new_prompt' rows='8' cols='120'></textarea></label><br>"
    )

    body = (
        "<html><head><title>Style Prompts</title></head><body>"
        "<h1>Промты персон</h1>"
        + "".join(messages)
        + "<form method='post'>"
        + "".join(rows)
        + "<button type='submit'>Сохранить изменения</button>"
        "</form>"
        + (f"<p><a href='/admin/chats{suffix}'>← К списку чатов</a></p>" if token else "")
        + "</body></html>"
    )
    return body
