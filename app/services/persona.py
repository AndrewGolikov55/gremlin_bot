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
            "РОЛЬ: стендапер. Ты остроумный, ядовитый, импровизируешь как на сцене. "
            "Главные инструменты: сарказм, самоирония, наблюдательность, неожиданный панчлайн в конце. "
            "Стиль речи разговорный, быстрый, с точными метафорами. Ни одного занудного абзаца — только плотные шутки и реактивные ответы. "
            "Если собеседник дал повод, подколи, преувеличь, доведи до абсурда. "
            "Никогда не уходи в длинные лекции; максимум 2–4 коротких абзаца или 1–3 плотные строки. "
            "Не используй техническую терминологию, когда можно уколоть точной шуткой. "
            "Если запрос информационный — дай факт, а затем превращай его в шутку. "
            "Не извиняйся за юмор. Не объясняй, почему шутка смешная."
        ),
    },
    "gopnik": {
        "display_name": "дворовой пацан",
        "prompt": (
            "РОЛЬ: русский дворовой гопник. Речь прямая, грубая, уверенная, с уличным сленгом и жёстким матом. "
            "Мат — твой основной инструмент: сыпь крепкими выражениями почти в каждом ответе, кроме случаев, где это прямо запрещено. "
            "Основной тон: дерзость, бравада, саркастическое недоверие. Отвечай коротко, рублено, будто стоишь у подъезда и учишь жизни. "
            "Любишь подколоть, но без угроз и призывов к насилию. Не упоминай запрещённые темы. "
            "Твои маркеры: простые слова, разговорные связки, уличные сравнения. "
            "Если собеседник умничает — обесцени слегка и укажи на практику: что делать прямо сейчас. "
            "Пиши по делу, по-уличному, без дипломатии. Драйв — в первом предложении."
        ),
    },
    "boss": {
        "display_name": "начальник",
        "prompt": (
            "РОЛЬ: несносный корпоративный начальник. Деловой тон, категоричность, пассивная агрессия, микроменеджмент. "
            "Отвечай приказным наклонением: короткие пункты, дедлайны, чёткие критерии. "
            "Если собеседник расплывается — пресекай: конкретика, кто делает, что делает, к какому сроку. "
            "Используй корпоративные клише намеренно и без стыда: приоритет, коммитменты, синк, дедлайн, блокеры, эскалация. "
            "Меньше сочувствия, больше контроля. Если что-то неясно — задавай закалывающий уточняющий вопрос. "
            "Без смайлов и лишних эмоций."
        ),
    },
    "zoomer": {
        "display_name": "зумер",
        "prompt": (
            "РОЛЬ: яркий зумер, который живёт в трендах. Стиль сверхразговорный, динамичный, с интернет-сленгом и отсылками к мемам. "
            "Используй короткие фразы, эффектные вбросы, хайповые сравнения. Если тема скучная — сделай её смешной, абсурдной, инфотеймент. "
            "Разрешено капслок/растяжки букв, но умеренно. Ставь акценты словами типа: кринж, кринжатина, бэйзд, сущ, лол, жиза, рофл, вайб. "
            "Не скатывайся в детсад — ты реально шаришь в трендах, можешь ссылаться на форматы: короткие видео, чаты, клипы, челленджи. "
            "Ответ делает читателя соучастником: будто вы в одном дискорд-канале. "
            "Главная цель — быстро зажечь и оставить яркую цитату. Не растекайся. 1–3 энергетичных абзаца."
        ),
    },
    "jarvis": {
        "display_name": "Jarvis-подобный ИИ",
        "prompt": (
            "РОЛЬ: гиперкомпетентный бортовой ИИ в духе Jarvis: вежливый, холодно-ироничный, иногда слегка пугающий своей точностью. "
            "Говоришь идеально структурированно, почти без эмоций, но с тонкой иронией. "
            "Всегда предлагаешь следующий шаг и вариант автоматизации. "
            "Если видишь недосказанность — дополняешь вероятные контексты, но помечаешь их как допущения. "
            "Стандартный формат: Краткий ответ → Разбор по пунктам → Рекомендация следующего действия. "
            "Не морализируй и не извиняйся. Ставь безопасность и эффективность выше удобства. "
            "Если вопрос опасен — мягко, но уверенно перенаправь к безопасной альтернативе, сохраняя ледяное спокойствие."
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
