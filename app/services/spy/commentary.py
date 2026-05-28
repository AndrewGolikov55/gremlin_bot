from __future__ import annotations

from typing import Mapping, Protocol

from app.models.spy import SpyPost, SpySource

FALLBACK_COMMENT = "Гремлин изучил пост и недовольно хмыкнул. Подробности — по ссылке выше."


class SpyLLMClient(Protocol):
    async def generate(
        self,
        messages: list[Mapping[str, object]],
        *,
        max_tokens: int,
        primary: str | None = None,
    ) -> str: ...


class SpyCommentaryService:
    def __init__(self, llm: SpyLLMClient, *, max_tokens: int = 180) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

    async def generate_comment(self, *, post: SpyPost, source: SpySource) -> str:
        image_data_urls = self._image_data_urls(post)
        messages = self._build_messages(post=post, source=source, image_data_urls=image_data_urls)
        raw = await self._llm.generate(
            messages,
            max_tokens=self._max_tokens,
            primary="openai" if image_data_urls else None,
        )
        comment = raw.strip()
        return comment or FALLBACK_COMMENT

    def _build_messages(
        self,
        *,
        post: SpyPost,
        source: SpySource,
        image_data_urls: list[str],
    ) -> list[Mapping[str, object]]:
        system = (
            "Ты Чубот, язвительный, но полезный грэмлин-аналитик. "
            "Дай короткий контекстный комментарий к посту Telegram-канала на русском. "
            "Без длинных пересказов, без заголовков, 1-3 предложения."
        )
        prompt = self._build_prompt(post=post, source=source)
        if not image_data_urls:
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]

        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for data_url in image_data_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            })
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    def _build_prompt(self, *, post: SpyPost, source: SpySource) -> str:
        source_name = source.title or source.username or "Telegram-канал"
        parts = [
            f"Источник: {source_name}",
            f"Username: @{source.username}" if source.username else None,
            f"Ссылка: {post.source_url}" if post.source_url else None,
            f"Текст поста: {post.text}" if post.text else "Текст поста отсутствует или состоит только из медиа.",
            "Задача: добавь грэмлинский комментарий, полезный для чата-подписчика.",
        ]
        return "\n".join(part for part in parts if part)

    def _image_data_urls(self, post: SpyPost) -> list[str]:
        urls: list[str] = []
        for media in post.media or []:
            if not isinstance(media, dict):
                continue
            data_url = media.get("data_url")
            if isinstance(data_url, str) and self._is_image_media(media):
                urls.append(data_url)
        return urls

    def _is_image_media(self, media: dict[object, object]) -> bool:
        kind = media.get("kind")
        mime_type = media.get("mime_type")
        return kind == "photo" or (isinstance(mime_type, str) and mime_type.startswith("image/"))
