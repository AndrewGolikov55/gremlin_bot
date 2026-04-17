from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Optional

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.persona import StylePrompt

DEFAULT_STYLE_KEY = "gopnik"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DISPLAY_NAME_RE = re.compile(r"^display_name:\s*(.+)$", re.MULTILINE)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_PERSONAS_DIR = _PROJECT_ROOT / "personas"


def parse_persona_file(content: str, fallback_display_name: str = "") -> Dict[str, str]:
    """Parse a persona .md file with optional YAML frontmatter.

    Returns a dict with 'display_name' and 'prompt' keys.
    """
    display_name = fallback_display_name
    prompt = content

    m = _FRONTMATTER_RE.match(content)
    if m:
        frontmatter = m.group(1)
        dn_match = _DISPLAY_NAME_RE.search(frontmatter)
        if dn_match:
            display_name = dn_match.group(1).strip()
        prompt = content[m.end():]

    return {"display_name": display_name, "prompt": prompt.strip()}


def load_persona_files(directory: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """Load all *.md persona files from *directory* (default: personas/ at project root).

    Returns {style_key: {"display_name": ..., "prompt": ...}} where style_key is the
    filename without the .md extension. Returns an empty dict if the directory does not exist.
    """
    if directory is None:
        directory = _PERSONAS_DIR

    directory = Path(directory)
    if not directory.is_dir():
        return {}

    result: Dict[str, Dict[str, str]] = {}
    for md_file in sorted(directory.glob("*.md")):
        style_key = md_file.stem
        content = md_file.read_text(encoding="utf-8")
        result[style_key] = parse_persona_file(content, fallback_display_name=style_key)
    return result


BASE_STYLE_DATA: Dict[str, Dict[str, str]] = load_persona_files()

DEFAULT_STYLE_PROMPTS: Dict[str, str] = {key: value["prompt"] for key, value in BASE_STYLE_DATA.items()}


class StylePromptService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, defaults: Dict[str, Dict[str, str]]):
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._defaults = defaults
        self._cache_key = "style_prompts:v1"

    async def ensure_defaults(self) -> None:
        # Base personas are now loaded from files and do not go into the DB.
        pass

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

        # Start with file-based defaults, then overlay only CUSTOM DB personas.
        prompts: Dict[str, str] = {style: data["prompt"] for style, data in self._defaults.items()}
        records = await self._fetch_all()
        for style, obj in records.items():
            if style not in self._defaults:
                prompts[style] = obj.prompt

        await self._redis.set(self._cache_key, json.dumps(prompts, ensure_ascii=False), ex=300)
        return prompts

    async def get(self, style: str) -> str:
        prompts = await self.get_all()
        fallback = self._defaults.get(DEFAULT_STYLE_KEY, {}).get("prompt", "")
        default_prompt = prompts.get(DEFAULT_STYLE_KEY, fallback)
        return prompts.get(style, default_prompt)

    async def get_display_map(self) -> Dict[str, str]:
        # Start with file-based defaults, then overlay only CUSTOM DB personas.
        display_map: Dict[str, str] = {style: data["display_name"] for style, data in self._defaults.items()}
        records = await self._fetch_all()
        for style, obj in records.items():
            if style not in self._defaults:
                display_map[style] = obj.display_name
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
        if style in self._defaults:
            raise ValueError("Нельзя изменить базовую персону")
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
                    display = style
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
