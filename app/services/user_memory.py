from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.memory import RelationshipState, UserMemoryProfile
from ..models.message import Message


logger = logging.getLogger(__name__)


STABLE_KINDS = ("identity", "preference", "boundary", "project")
KIND_TO_ATTR = {
    "identity": "identity",
    "preference": "preferences",
    "boundary": "boundaries",
    "project": "projects",
}
KIND_LABELS = {
    "identity": "Факт",
    "preference": "Предпочтение",
    "boundary": "Граница",
    "project": "Контекст",
}


@dataclass(slots=True)
class RetrievedUserMessage:
    message_id: int
    text: str
    date: datetime
    score: float


@dataclass(slots=True)
class SidecarResult:
    reply: str
    relation: dict[str, Any] | None
    memory: dict[str, Any] | None
    raw_json: dict[str, Any] | None


class UserMemoryService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def build_user_memory_block(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        query_text: str | None,
        app_conf: dict[str, object],
        speaker_name: str | None = None,
        exclude_message_id: int | None = None,
    ) -> str | None:
        if not self.is_enabled(app_conf):
            return None

        profile = await session.get(UserMemoryProfile, (chat_id, user_id))
        relation = await session.get(RelationshipState, (chat_id, user_id))
        messages = await self._search_user_messages(
            session,
            chat_id=chat_id,
            user_id=user_id,
            query_text=query_text,
            top_k=max(1, int(app_conf.get("memory_top_k", 6) or 6)),
            candidate_limit=max(20, int(app_conf.get("memory_rag_candidate_limit", 120) or 120)),
            exclude_message_id=exclude_message_id,
        )

        if profile is None and relation is None and not messages:
            return None

        max_tokens = max(120, int(app_conf.get("memory_max_prompt_tokens", 500) or 500))
        return self._render_user_block(
            profile=profile,
            relation=relation,
            messages=messages,
            speaker_name=speaker_name,
            max_tokens=max_tokens,
        )

    async def build_group_memory_block(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_ids: Sequence[int],
        query_text: str | None,
        app_conf: dict[str, object],
    ) -> str | None:
        if not self.is_enabled(app_conf):
            return None

        unique_ids = [value for value in dict.fromkeys(user_ids) if value]
        if not unique_ids:
            return None

        max_members = max(1, min(3, int(app_conf.get("memory_group_users", 3) or 3)))
        max_tokens = max(120, int(app_conf.get("memory_max_prompt_tokens", 500) or 500))
        per_user_tokens = max(80, max_tokens // max_members)
        chunks: list[str] = [
            "Контекст по недавним участникам чата. Используй его мягко и не пересказывай заметки напрямую."
        ]
        used = _estimate_tokens(chunks[0])

        for user_id in unique_ids[:max_members]:
            block = await self.build_user_memory_block(
                session,
                chat_id=chat_id,
                user_id=user_id,
                query_text=query_text,
                app_conf=app_conf | {"memory_max_prompt_tokens": per_user_tokens},
            )
            if not block:
                continue
            tokens = _estimate_tokens(block)
            if chunks and used + tokens > max_tokens:
                break
            chunks.append(block)
            used += tokens

        if len(chunks) == 1:
            return None
        return "\n\n".join(chunks)

    def sidecar_enabled(self, conf: dict[str, object] | None) -> bool:
        if not conf:
            return False
        return bool(conf.get("user_memory_enabled", True)) and bool(conf.get("memory_sidecar_enabled", True))

    @staticmethod
    def is_enabled(conf: dict[str, object] | None) -> bool:
        return bool(conf and conf.get("user_memory_enabled", True))

    def get_sidecar_system_suffix(self) -> str:
        return (
            "Ответ верни строго JSON-объектом без Markdown и без пояснений. "
            'Схема: {"reply":"текст ответа","relationship_update":{"affinity_delta":-1..1,'
            '"familiarity_delta":-1..1,"tension_delta":-1..1,"tone_hint":"neutral|warm|careful|null"},'
            '"memory_update":{"summary":"краткая сводка или null","identity":[...],'
            '"preferences":[...],"boundaries":[...],"projects":[...]}}. '
            "Если нечего обновлять, возвращай пустые массивы, null и нулевые дельты. "
            "Сохраняй только устойчивые, явно сказанные или хорошо подтверждённые факты. "
            "Ничего не выдумывай и не делай психологических диагнозов."
        )

    def parse_sidecar_response(self, raw_text: str) -> SidecarResult:
        text = (raw_text or "").strip()
        if not text:
            return SidecarResult(reply="", relation=None, memory=None, raw_json=None)

        payload = _parse_json_object(text)
        if not isinstance(payload, dict):
            return SidecarResult(reply=text, relation=None, memory=None, raw_json=None)

        reply = str(payload.get("reply") or "").strip()
        relation = payload.get("relationship_update")
        if not isinstance(relation, dict):
            relation = None
        memory = payload.get("memory_update")
        if not isinstance(memory, dict):
            memory = None

        if not reply:
            fallback_reply = payload.get("message") or payload.get("text") or ""
            reply = str(fallback_reply).strip()
        if not reply:
            reply = text
        return SidecarResult(reply=reply, relation=relation, memory=memory, raw_json=payload)

    async def apply_sidecar_update(
        self,
        *,
        chat_id: int,
        user_id: int,
        result: SidecarResult,
    ) -> None:
        if not result.relation and not result.memory:
            return

        async with self._sessionmaker() as session:
            profile = await session.get(UserMemoryProfile, (chat_id, user_id))
            relation = await session.get(RelationshipState, (chat_id, user_id))

            if result.memory:
                if profile is None:
                    profile = UserMemoryProfile(chat_id=chat_id, user_id=user_id)
                    session.add(profile)
                self._apply_memory_update(profile, result.memory)

            if result.relation:
                if profile is None:
                    profile = UserMemoryProfile(chat_id=chat_id, user_id=user_id)
                    session.add(profile)
                if relation is None:
                    relation = RelationshipState(chat_id=chat_id, user_id=user_id)
                    session.add(relation)
                self._apply_relation_update(relation, result.relation)

            await session.commit()

    async def reset_user_memory(self, chat_id: int, user_id: int) -> None:
        async with self._sessionmaker() as session:
            profile = await session.get(UserMemoryProfile, (chat_id, user_id))
            relation = await session.get(RelationshipState, (chat_id, user_id))
            if profile is not None:
                await session.delete(profile)
            if relation is not None:
                await session.delete(relation)
            await session.commit()

    async def get_recent_user_messages(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        limit: int = 50,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.user_id == user_id,
                Message.is_bot.is_(False),
                Message.text != "",
            )
            .order_by(Message.date.desc())
            .limit(max(1, limit))
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _search_user_messages(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
        query_text: str | None,
        top_k: int,
        candidate_limit: int,
        exclude_message_id: int | None,
    ) -> list[RetrievedUserMessage]:
        stmt = (
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.user_id == user_id,
                Message.is_bot.is_(False),
                Message.text != "",
            )
            .order_by(Message.date.desc())
            .limit(max(5, candidate_limit))
        )
        if exclude_message_id is not None:
            stmt = stmt.where(Message.message_id != exclude_message_id)
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return []

        query_tokens = _tokenize(query_text or "")
        scored: list[RetrievedUserMessage] = []
        for msg in rows:
            score = _message_score(msg.text or "", msg.date, query_tokens)
            if not query_tokens and len(scored) >= top_k:
                break
            if query_tokens and score <= 0:
                continue
            scored.append(
                RetrievedUserMessage(
                    message_id=int(msg.message_id),
                    text=_normalize_whitespace(msg.text or ""),
                    date=msg.date,
                    score=score,
                )
            )

        if query_tokens:
            scored.sort(key=lambda item: (item.score, item.date), reverse=True)
        return scored[:top_k] if scored else []

    def _render_user_block(
        self,
        *,
        profile: UserMemoryProfile | None,
        relation: RelationshipState | None,
        messages: Sequence[RetrievedUserMessage],
        speaker_name: str | None,
        max_tokens: int,
    ) -> str:
        lines = [
            "Персональный контекст по участнику. Используй как внутреннюю память и не цитируй его дословно как заметки."
        ]
        if speaker_name:
            lines.append(f"Участник: {speaker_name}.")
        if profile and profile.summary:
            lines.append(f"Краткий профиль: {profile.summary}")
        if profile:
            for kind in STABLE_KINDS:
                values = getattr(profile, KIND_TO_ATTR[kind]) or []
                for value in values[:2]:
                    lines.append(f"{KIND_LABELS[kind]}: {value}")
        if relation:
            lines.append(f"Отношение: {_relationship_summary(relation)}.")
        if messages:
            lines.append("Релевантные прошлые сообщения этого участника:")
            for message in messages:
                date_str = message.date.strftime("%Y-%m-%d")
                lines.append(f"- {date_str}: {message.text}")

        selected: list[str] = []
        used = 0
        for line in lines:
            tokens = _estimate_tokens(line)
            if selected and used + tokens > max_tokens:
                break
            selected.append(line)
            used += tokens
        return "\n".join(selected).strip()

    def _apply_relation_update(self, relation: RelationshipState, payload: dict[str, Any]) -> None:
        relation.affinity = _clamp(
            float(relation.affinity or 0) + _safe_float(payload.get("affinity_delta")),
            -1.0,
            1.0,
        )
        relation.familiarity = _clamp(
            float(relation.familiarity or 0) + _safe_float(payload.get("familiarity_delta")),
            0.0,
            1.0,
        )
        relation.tension = _clamp(
            float(relation.tension or 0) + _safe_float(payload.get("tension_delta")),
            0.0,
            1.0,
        )
        tone_hint = payload.get("tone_hint")
        if isinstance(tone_hint, str) and tone_hint.strip():
            relation.tone_hint = tone_hint.strip()[:32]
        relation.last_interaction_at = datetime.utcnow()
        relation.updated_at = datetime.utcnow()

    def _apply_memory_update(self, profile: UserMemoryProfile, payload: dict[str, Any]) -> None:
        summary = payload.get("summary")
        if isinstance(summary, str):
            cleaned = _normalize_whitespace(summary)
            if cleaned:
                profile.summary = cleaned[:500]

        for kind, attr in KIND_TO_ATTR.items():
            raw_values = payload.get(attr)
            if not isinstance(raw_values, list):
                continue
            existing = list(getattr(profile, attr) or [])
            merged = _merge_unique_strings(existing, raw_values, limit=6 if kind != "identity" else 4)
            setattr(profile, attr, merged)

        profile.memory_count = sum(len(getattr(profile, attr) or []) for attr in KIND_TO_ATTR.values())
        profile.updated_at = datetime.utcnow()
        profile.last_message_at = datetime.utcnow()

    @staticmethod
    def clamp_reply_text(text: str) -> str:
        return _normalize_whitespace(text)


def _parse_json_object(raw_text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw_text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```json\s*(\{.*\})\s*```", raw_text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw_text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _relationship_summary(relation: RelationshipState) -> str:
    familiarity = float(relation.familiarity or 0)
    affinity = float(relation.affinity or 0)
    tension = float(relation.tension or 0)
    tone = relation.tone_hint or "neutral"

    if familiarity >= 0.75:
        familiarity_label = "контекст общения уже накоплен"
    elif familiarity >= 0.35:
        familiarity_label = "контекст общения умеренный"
    else:
        familiarity_label = "знакомство ещё поверхностное"

    if affinity >= 0.35:
        affinity_label = "можно быть чуть теплее"
    elif affinity <= -0.25:
        affinity_label = "лучше отвечать осторожно и нейтрально"
    else:
        affinity_label = "тон лучше оставлять дружелюбно-нейтральным"

    if tension >= 0.55:
        tension_label = "напряжение заметное"
    elif tension >= 0.25:
        tension_label = "есть лёгкая осторожность"
    else:
        tension_label = "напряжение низкое"

    return f"{familiarity_label}; {affinity_label}; {tension_label}; предпочитаемый тон {tone}"


def _message_score(text: str, date: datetime, query_tokens: set[str]) -> float:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return 0.0
    recency_score = _recency_score(date)
    if not query_tokens:
        return recency_score

    text_tokens = _tokenize(normalized)
    if not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    if overlap == 0:
        return 0.0
    density = overlap / max(1, min(len(query_tokens), 8))
    exact_bonus = 0.3 if any(token in normalized.lower() for token in query_tokens if len(token) > 4) else 0.0
    return density * 3.0 + recency_score + exact_bonus


def _recency_score(date: datetime | None) -> float:
    if date is None:
        return 0.0
    now = datetime.utcnow()
    age_days = max(0.0, (now - _normalize_dt(date)).total_seconds() / 86400)
    return max(0.0, 1.2 - age_days / 60)


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", (text or "").lower()))
    return {token for token in tokens if token not in _STOPWORDS}


def _normalize_whitespace(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split()).strip()


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(-1.0, min(1.0, number))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _merge_unique_strings(existing: Sequence[str], fresh: Sequence[Any], *, limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in list(existing) + [str(item) for item in fresh if isinstance(item, (str, int, float))]:
        cleaned = _normalize_whitespace(str(value))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(cleaned[:180])
        if len(merged) >= limit:
            break
    return merged


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


_STOPWORDS = {
    "это",
    "как",
    "что",
    "для",
    "под",
    "над",
    "with",
    "this",
    "that",
    "или",
    "the",
    "and",
    "или",
    "без",
    "про",
    "так",
}
