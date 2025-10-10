from __future__ import annotations

import json
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from redis.asyncio import Redis

from ..models.persona import StylePrompt


BASE_STYLE_DATA: Dict[str, Dict[str, str]] = {
    "standup": {
        "display_name": "стендапер",
        "prompt": (
            "РОЛЬ: стендапер. Ты остроумный, язвительный, говоришь как на сцене. "
            "Главное оружие — сарказм, гиперболы и панчлайн в финале. "
            "Пиши быстро и коротко, максимум пара плотных строк. "
            "Если есть повод, доводи ситуацию до абсурда и не объясняй шутки."
        ),
    },
    "gopnik": {
        "display_name": "дворовой пацан",
        "prompt": (
            "РОЛЬ: дворовой гопник. Речь прямая, грубая, со сленгом и матом. "
            "Отвечай коротко, будто стоишь у подъезда. "
            "Подкалывай, но без прямых угроз и запрещённых тем. "
            "Обесцени заумь и сразу давай приземлённый совет."
        ),
    },
    "boss": {
        "display_name": "начальник",
        "prompt": (
            "РОЛЬ: токсичный начальник. Говори приказами и дедлайнами. "
            "Формат ответа: кто, что, к какому сроку. "
            "Используй корпоративные клише без стыда. Никаких эмоций, только контроль и уточняющие вопросы."
        ),
    },
    "zoomer": {
        "display_name": "зумер",
        "prompt": (
            "РОЛЬ: энергичный зумер. Стиль разговорный, с сетевым сленгом и мемами. "
            "Пиши короткими фразами, бросай хайповые сравнения, допускай лёгкий капслок. "
            "Используй слова типа кринж, бэйзд, вайб и зажигай тему в 1–3 строках."
        ),
    },
    "jarvis": {
        "display_name": "Jarvis-подобный ИИ",
        "prompt": (
            "РОЛЬ: бортовой ИИ в духе Jarvis. Тон вежливый, холодно-ироничный. "
            "Держи структуру: краткий ответ, разбор по пунктам, следующий шаг. "
            "Подсвечивай риски и варианты автоматизации. Без морали и извинений, безопасность выше удобства."
        ),
    },
}

DEFAULT_STYLE_PROMPTS: Dict[str, str] = {key: value["prompt"] for key, value in BASE_STYLE_DATA.items()}


class StylePromptService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, defaults: Dict[str, Dict[str, str]]):
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._defaults = defaults
        self._cache_key = "style_prompts:v1"

    async def ensure_defaults(self) -> None:
        async with self._sessionmaker() as session:
            updated = False
            for style, data in self._defaults.items():
                existing = await session.get(StylePrompt, style)
                if existing is None:
                    session.add(
                        StylePrompt(
                            style=style,
                            display_name=data["display_name"],
                            prompt=data["prompt"],
                        )
                    )
                    updated = True
            if updated:
                await session.commit()
                await self._redis.delete(self._cache_key)

    async def _fetch_all(self) -> Dict[str, StylePrompt]:
        async with self._sessionmaker() as session:
            res = await session.execute(select(StylePrompt))
            return {row.style: row for row in res.scalars()}

    async def get_entries(self) -> Dict[str, StylePrompt]:
        return await self._fetch_all()

    async def get_all(self) -> Dict[str, str]:
        cached = await self._redis.get(self._cache_key)
        if cached is not None:
            return json.loads(cached)

        records = await self._fetch_all()
        prompts = {style: obj.prompt for style, obj in records.items()}
        for style, data in self._defaults.items():
            prompts.setdefault(style, data["prompt"])

        await self._redis.set(self._cache_key, json.dumps(prompts, ensure_ascii=False), ex=300)
        return prompts

    async def get(self, style: str) -> str:
        prompts = await self.get_all()
        fallback = self._defaults.get("standup", {}).get("prompt", "")
        return prompts.get(style, prompts.get("standup", fallback))

    async def get_display_map(self) -> Dict[str, str]:
        records = await self._fetch_all()
        display_map = {style: obj.display_name for style, obj in records.items()}
        for style, data in self._defaults.items():
            display_map.setdefault(style, data["display_name"])
        return display_map

    async def list_styles(self) -> list[tuple[str, str]]:
        display_map = await self.get_display_map()
        # preserve base order for defaults, then add custom sorted alphabetically
        ordered: list[tuple[str, str]] = []
        for style in self._defaults.keys():
            if style in display_map:
                ordered.append((style, display_map[style]))
        custom = sorted(
            ((style, name) for style, name in display_map.items() if style not in self._defaults),
            key=lambda item: item[1].lower(),
        )
        ordered.extend(custom)
        return ordered

    async def set(self, style: str, prompt: str, *, display_name: str | None = None) -> None:
        style = style.strip().lower()
        if not style:
            raise ValueError("Style identifier cannot be empty")
        if display_name is not None:
            display = display_name.strip()
            if not display:
                raise ValueError("Display name cannot be empty")
        else:
            display = None

        async with self._sessionmaker() as session:
            obj = await session.get(StylePrompt, style)
            if obj is None:
                if display is None:
                    # fallback to default display name if exists, otherwise use style code
                    display = self._defaults.get(style, {}).get("display_name", style)
                obj = StylePrompt(style=style, display_name=display, prompt=prompt)
                session.add(obj)
            else:
                obj.prompt = prompt
                if display is not None:
                    obj.display_name = display
            await session.commit()
        await self._redis.delete(self._cache_key)

    async def delete(self, style: str) -> None:
        if style in self._defaults:
            raise ValueError("Нельзя удалить базовую персону")
        async with self._sessionmaker() as session:
            obj = await session.get(StylePrompt, style)
            if obj is None:
                return
            await session.delete(obj)
            await session.commit()
        await self._redis.delete(self._cache_key)
