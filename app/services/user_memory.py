from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.memory import ChatMemory, RelationshipState, UserMemoryProfile
from ..models.message import Message

logger = logging.getLogger(__name__)


STABLE_KINDS = ("identity", "preference", "boundary")
KIND_TO_ATTR = {
    "identity": "identity",
    "preference": "preferences",
    "boundary": "boundaries",
}
KIND_LABELS = {
    "identity": "Факт о пользователе",
    "preference": "Предпочтение пользователя",
    "boundary": "Граница пользователя",
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
    chat_memory: dict[str, Any] | None
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
        include_relation: bool = True,
    ) -> str | None:
        if not self.is_enabled(app_conf):
            return None

        profile = await session.get(UserMemoryProfile, (chat_id, user_id))
        relation = await session.get(RelationshipState, (chat_id, user_id)) if include_relation else None
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
            include_relation=include_relation,
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
            "Краткая справка о недавних участниках чата. Это сведения о пользователях, не о тебе."
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

    async def build_summary_social_block(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        participants: Sequence[tuple[int, str | None]],
        app_conf: dict[str, object],
    ) -> str | None:
        if not self.is_enabled(app_conf):
            return None

        unique: list[tuple[int, str | None]] = []
        seen: set[int] = set()
        for user_id, speaker_name in participants:
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            unique.append((user_id, speaker_name))

        if not unique:
            return None

        max_members = min(4, max(1, int(app_conf.get("memory_group_users", 3) or 3)))
        max_tokens = min(260, max(80, int(app_conf.get("memory_max_prompt_tokens", 500) or 500) // 2))
        lines = [
            "Социальный контекст активных участников. Используй его только как фон для тона и атмосферы, не перечисляй как досье."
        ]
        used = _estimate_tokens(lines[0])

        for user_id, speaker_name in unique[:max_members]:
            profile = await session.get(UserMemoryProfile, (chat_id, user_id))
            relation = await session.get(RelationshipState, (chat_id, user_id))
            line = self._render_summary_social_line(
                profile=profile,
                relation=relation,
                speaker_name=speaker_name,
            )
            if not line:
                continue
            tokens = _estimate_tokens(line)
            if len(lines) > 1 and used + tokens > max_tokens:
                break
            lines.append(line)
            used += tokens

        if len(lines) == 1:
            return None
        return "\n".join(lines)

    async def build_reaction_memory_block(
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
        visible_memory = _profile_memory_values(profile)
        relation_summary = _relationship_summary(relation) if relation else None

        messages: list[RetrievedUserMessage] = []
        if not relation_summary or not any(visible_memory.values()):
            messages = await self._search_user_messages(
                session,
                chat_id=chat_id,
                user_id=user_id,
                query_text=query_text,
                top_k=2,
                candidate_limit=min(40, max(10, int(app_conf.get("memory_rag_candidate_limit", 120) or 120))),
                exclude_message_id=exclude_message_id,
            )

        if profile is None and relation_summary is None and not messages:
            return None

        lines = ["Короткий контекст для выбора реакции на сообщение."]
        if speaker_name:
            lines.append(f"Пользователь: {speaker_name}.")
        if relation_summary:
            lines.append(f"Отношения: {relation_summary}.")
        if visible_memory["boundary"]:
            lines.append(f"Граница: {_truncate_text(visible_memory['boundary'][0], 70)}")
        elif visible_memory["preference"]:
            lines.append(f"Предпочтение: {_truncate_text(visible_memory['preference'][0], 70)}")
        elif visible_memory["identity"]:
            lines.append(f"Факт: {_truncate_text(visible_memory['identity'][0], 70)}")
        elif messages:
            lines.append("Недавно писал:")
            for item in messages[:2]:
                lines.append(f"- {_truncate_text(item.text, 90)}")

        selected: list[str] = []
        used = 0
        for line in lines:
            tokens = _estimate_tokens(line)
            if selected and used + tokens > 90:
                break
            selected.append(line)
            used += tokens
        return "\n".join(selected).strip() or None

    def sidecar_enabled(self, conf: dict[str, object] | None) -> bool:
        if not conf:
            return False
        return bool(conf.get("user_memory_enabled", True)) and bool(conf.get("memory_sidecar_enabled", True))

    @staticmethod
    def is_enabled(conf: dict[str, object] | None) -> bool:
        return bool(conf and conf.get("user_memory_enabled", True))

    def get_sidecar_system_suffix(self) -> str:
        return (
            "Верни только JSON без Markdown: "
            '{"reply":"...","relationship_update":{"rapport_delta":-1..1,'
            '"tone_hint":"neutral|warm|careful|null"},'
            '"memory_update":{"summary":null|"...",'
            '"identity":[],"preferences":[],"boundaries":[]},'
            '"chat_memory_update":{"members":[],"lore":[]}}. '
            "Если обновлять нечего, используй 0, null и пустые массивы. "
            "rapport_delta описывает общее отношение к пользователю: минус означает больше дистанции "
            "и раздражения, плюс означает больше расположения и доверия. "
            "Явные оскорбления и агрессия обычно уменьшают rapport и могут ставить tone_hint=careful. "
            "Извинение и попытка помириться могут немного повышать rapport, но не обязаны сразу "
            "снимать всю настороженность. Не копируй служебные поля relationship_update в "
            "memory_update: tone_hint и rapport не являются фактами о пользователе. "
            "Память обновляй только устойчивыми явными фактами без догадок. "
            "ВАЖНО: identity, preferences и boundaries — это факты ТОЛЬКО о том, кто пишет сообщение. "
            "Не записывай туда ники, имена и факты о других людях, которых он упоминает. "
            "Если пользователь просит запомнить что-то о третьем лице — игнорируй, это не его профиль. "
            "Не дублируй уже известные факты другими словами — если факт уже есть, не добавляй его снова. "
            "chat_memory_update.members — факты о конкретных участниках чата (не о тебе, не об отправителе — "
            "его факты идут в memory_update). "
            "chat_memory_update.lore — общий контекст чата: внутренние шутки, темы, события. "
            "Используй null или [] в chat_memory_update когда обновлять нечего."
        )

    def parse_sidecar_response(self, raw_text: str) -> SidecarResult:
        text = (raw_text or "").strip()
        if not text:
            return SidecarResult(reply="", relation=None, memory=None, chat_memory=None, raw_json=None)

        payload = _parse_json_object(text)
        if not isinstance(payload, dict):
            return SidecarResult(reply=text, relation=None, memory=None, chat_memory=None, raw_json=None)

        reply = str(payload.get("reply") or "").strip()
        relation = payload.get("relationship_update")
        if not isinstance(relation, dict):
            relation = None
        memory = payload.get("memory_update")
        if not isinstance(memory, dict):
            memory = None
        chat_memory = payload.get("chat_memory_update")
        if not isinstance(chat_memory, dict):
            chat_memory = None

        if not reply:
            fallback_reply = payload.get("message") or payload.get("text") or ""
            reply = str(fallback_reply).strip()
        if not reply:
            reply = text
        return SidecarResult(reply=reply, relation=relation, memory=memory, chat_memory=chat_memory, raw_json=payload)

    async def apply_sidecar_update(
        self,
        *,
        chat_id: int,
        user_id: int,
        result: SidecarResult,
    ) -> None:
        if not result.relation and not result.memory and not result.chat_memory:
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

            if result.chat_memory:
                chat_mem = await session.get(ChatMemory, chat_id)
                is_new = chat_mem is None
                if is_new:
                    chat_mem = ChatMemory(chat_id=chat_id)
                if self._apply_chat_memory_update(chat_mem, result.chat_memory):
                    if is_new:
                        session.add(chat_mem)

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
        include_relation: bool,
    ) -> str:
        lines = [
            "Справка о пользователе. Эти сведения относятся к собеседнику, не к тебе."
        ]
        visible_memory = _profile_memory_values(profile)
        if speaker_name:
            lines.append(f"Пользователь: {speaker_name}.")
        if profile and profile.summary:
            summary = _sanitize_profile_summary(profile.summary)
            if summary and not _is_redundant_summary(summary, visible_memory):
                lines.append(f"Кратко о пользователе: {_truncate_text(summary, 180)}")
        if profile:
            for kind in STABLE_KINDS:
                values = visible_memory[kind]
                if values:
                    lines.append(f"{KIND_LABELS[kind]}: {_truncate_text(values[0], 120)}")
        if include_relation and relation:
            relation_summary = _relationship_summary(relation)
            if relation_summary:
                lines.append(f"Отношение к пользователю: {relation_summary}.")
        if messages:
            lines.append("Ранее пользователь писал:")
            for message in messages[:4]:
                lines.append(f"- {_truncate_text(message.text, 140)}")

        selected: list[str] = []
        used = 0
        for line in lines:
            tokens = _estimate_tokens(line)
            if selected and used + tokens > max_tokens:
                break
            selected.append(line)
            used += tokens
        return "\n".join(selected).strip()

    def _render_summary_social_line(
        self,
        *,
        profile: UserMemoryProfile | None,
        relation: RelationshipState | None,
        speaker_name: str | None,
    ) -> str | None:
        if profile is None and relation is None:
            return None

        label = speaker_name or "участник"
        relation_summary = _relationship_summary(relation) if relation else None
        parts = [f"- {label}: {relation_summary or 'без выраженного отношения'}"]

        if profile is not None:
            visible_memory = _profile_memory_values(profile)
            salient = (
                visible_memory["boundary"][:1]
                or visible_memory["preference"][:1]
                or visible_memory["identity"][:1]
            )
            if salient:
                parts.append(f"факт: {_truncate_text(str(salient[0]), 70)}")

        return "; ".join(parts)

    def _apply_relation_update(self, relation: RelationshipState, payload: dict[str, Any]) -> None:
        current_rapport = _relationship_rapport(relation)
        rapport_delta = _safe_float(payload.get("rapport_delta"))
        if rapport_delta == 0.0:
            rapport_delta = _safe_float(payload.get("affinity_delta")) - _safe_float(
                payload.get("tension_delta")
            )
        relation.affinity = _clamp(current_rapport + rapport_delta, -1.0, 1.0)
        relation.familiarity = 0.0
        relation.tension = 0.0
        tone_hint = payload.get("tone_hint")
        if isinstance(tone_hint, str) and tone_hint.strip():
            relation.tone_hint = tone_hint.strip()[:32]
        relation.last_interaction_at = datetime.utcnow()
        relation.updated_at = datetime.utcnow()

    def _apply_memory_update(self, profile: UserMemoryProfile, payload: dict[str, Any]) -> None:
        summary = payload.get("summary")
        if isinstance(summary, str):
            cleaned = _sanitize_profile_summary(summary)
            if cleaned:
                profile.summary = cleaned[:500]

        for kind, attr in KIND_TO_ATTR.items():
            raw_values = payload.get(attr)
            if not isinstance(raw_values, list):
                continue
            values = _visible_memory_values(kind, raw_values)
            existing = _visible_memory_values(kind, list(getattr(profile, attr) or []))
            merged = _merge_unique_strings(
                existing,
                values,
                limit=6 if kind != "identity" else 4,
            )
            setattr(profile, attr, merged)

        if profile.summary and _is_redundant_summary(profile.summary, _profile_memory_values(profile)):
            profile.summary = None

        profile.memory_count = sum(len(getattr(profile, attr) or []) for attr in KIND_TO_ATTR.values())
        profile.updated_at = datetime.utcnow()
        profile.last_message_at = datetime.utcnow()

    def _apply_chat_memory_update(self, chat_mem: ChatMemory, payload: dict[str, Any]) -> bool:
        wrote_any = False
        for bucket in ("members", "lore"):
            raw = payload.get(bucket)
            if not isinstance(raw, list):
                continue
            fresh = [str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()]
            if not fresh:
                continue
            existing = list(getattr(chat_mem, bucket) or [])
            # newest-first storage: fresh prepended, oldest (tail) evicted when at limit
            merged = _merge_unique_strings(fresh + existing, [], limit=12)
            setattr(chat_mem, bucket, merged)
            wrote_any = True
        if wrote_any:
            chat_mem.updated_at = datetime.utcnow()
        return wrote_any

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
    rapport = _relationship_rapport(relation)
    if rapport >= 0.6:
        return "отношения дружеские"
    if rapport >= 0.2:
        return "отношения тёплые"
    if rapport <= -0.6:
        return "отношения враждебные"
    if rapport <= -0.2:
        return "отношения напряжённые"
    return "отношения нейтральные"


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


def _truncate_text(value: str, limit: int) -> str:
    text = _normalize_whitespace(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _sanitize_profile_summary(value: str) -> str:
    text = _normalize_whitespace(value)
    patterns = [
        r"(?:,?\s*)предпочитает\s+(?:нейтральный|т[её]плый|осторожный)\s+тон\.?",
        r"(?:,?\s*)предпочитаемый\s+тон:\s*(?:neutral|warm|careful|нейтральный|т[её]плый|осторожный)\.?",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return _normalize_whitespace(text.strip(" ,.;"))


def _visible_memory_values(kind: str, values: Sequence[Any]) -> list[Any]:
    if kind == "preference":
        return [item for item in values if not _is_relationship_artifact(item)]
    return list(values)


def _profile_memory_values(profile: UserMemoryProfile | None) -> dict[str, list[str]]:
    if profile is None:
        return {kind: [] for kind in STABLE_KINDS}
    return {
        kind: [str(item) for item in _visible_memory_values(kind, getattr(profile, KIND_TO_ATTR[kind]) or [])]
        for kind in STABLE_KINDS
    }


def _is_redundant_summary(summary: str, memory_values: dict[str, list[str]]) -> bool:
    normalized_summary = _summary_key(summary)
    if not normalized_summary:
        return False
    for values in memory_values.values():
        for value in values:
            if normalized_summary == _summary_key(value):
                return True
    return False


def _summary_key(value: str) -> str:
    text = _sanitize_profile_summary(value).lower()
    text = re.sub(r"^пользователь\s+", "", text)
    return _normalize_whitespace(text.strip(" ,.;"))


def _relationship_rapport(relation: RelationshipState) -> float:
    affinity = float(relation.affinity or 0)
    tension = float(relation.tension or 0)
    return _clamp(affinity - tension, -1.0, 1.0)


def _is_relationship_artifact(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = _normalize_whitespace(value).lower()
    return bool(
        re.fullmatch(
            r"предпочита(?:е|ё)мый тон:\s*(neutral|warm|careful|нейтральный|т[её]плый|осторожный)",
            normalized,
        )
    )


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
