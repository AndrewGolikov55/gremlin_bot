from __future__ import annotations

import os
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..services.settings import SettingsService


def create_admin_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: SettingsService,
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
        return HTMLResponse(_render_settings_page(chat, conf, token=token))

    @router.post("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_settings_update(
        chat_id: int,
        tone: int = Form(...),
        max_length: int = Form(...),
        context_turns: int = Form(...),
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

        tone = max(0, min(10, tone))
        if max_length < 0:
            max_length = 0
        elif max_length > 0:
            max_length = max(50, min(2000, max_length))
        context_turns = max(5, min(150, context_turns))
        probability = max(0, min(100, probability))
        cooldown = max(10, min(3600, cooldown))
        revive_days = max(1, min(30, revive_days))
        revive_hours = revive_days * 24

        await settings.set(chat_id, "tone", tone)
        await settings.set(chat_id, "max_length", max_length)
        await settings.set(chat_id, "context_max_turns", context_turns)
        await settings.set(chat_id, "interject_p", probability)
        await settings.set(chat_id, "interject_cooldown", cooldown)
        await settings.set(chat_id, "revive_enabled", bool(revive_enabled))
        await settings.set(chat_id, "revive_after_hours", revive_hours)

        conf = await settings.get_all(chat_id)
        page = _render_settings_page(chat, conf, saved=True, token=token)
        return HTMLResponse(page)

    return router


def _render_settings_page(
    chat: Chat,
    conf: dict[str, object],
    saved: bool = False,
    token: str | None = None,
) -> str:
    tone = int(conf.get("tone", 3) or 0)
    max_length_raw = conf.get("max_length", 0)
    max_length = int(max_length_raw) if max_length_raw is not None else 0
    context_turns = int(conf.get("context_max_turns", 100) or 100)
    probability = int(conf.get("interject_p", 0) or 0)
    cooldown = int(conf.get("interject_cooldown", 60) or 60)
    revive_enabled = bool(conf.get("revive_enabled", False))
    revive_hours = int(conf.get("revive_after_hours", 48) or 48)
    revive_days = max(1, revive_hours // 24)
    message = "<p style='color:green;'>Сохранено</p>" if saved else ""
    suffix = f"?token={escape(token)}" if token else ""
    return (
        "<html><head><title>Chat Settings</title></head><body>"
        f"<h1>Чат {escape(chat.title)} ({chat.id})</h1>"
        f"{message}"
        "<form method='post'>"
        "<label>Тональность (0-10): <input type='number' name='tone' min='0' max='10' value='" + str(tone) + "'></label><br><br>"
        "<label>Макс. длина ответа (0 = без ограничения, иначе 50-2000): <input type='number' name='max_length' min='0' max='2000' value='" + str(max_length) + "'></label><br><br>"
        "<label>Контекст (5-150 сообщений): <input type='number' name='context_turns' min='5' max='150' value='" + str(context_turns) + "'></label><br><br>"
        "<label>Вероятность вмешательства (0-100%): <input type='number' name='probability' min='0' max='100' value='" + str(probability) + "'></label><br><br>"
        "<label>Кулдаун (10-3600 сек): <input type='number' name='cooldown' min='10' max='3600' value='" + str(cooldown) + "'></label><br><br>"
        f"<label><input type='checkbox' name='revive_enabled' value='1' {'checked' if revive_enabled else ''}> Оживление при тишине</label><br><br>"
        "<label>Оживление через (дней): <input type='number' name='revive_days' min='1' max='30' value='" + str(revive_days) + "'></label><br><br>"
        "<button type='submit'>Сохранить</button>"
        "</form>"
        + (f"<p><a href='/admin/chats{suffix}'>← Назад к списку</a></p>" if token else "")
        + "</body></html>"
    )
