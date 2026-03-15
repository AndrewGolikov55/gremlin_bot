# Персональная память по пользователям: варианты внедрения

## 1) Срез текущей архитектуры (факты)
- Стек: FastAPI + aiogram, async SQLAlchemy, PostgreSQL, Redis; webhook/polling поднимаются в `app/main.py`. Точка входа Telegram апдейтов — `/webhook/telegram`, далее `Dispatcher.feed_update`.  
- Основной обработчик входящих текстов — `collect_messages` в `app/bot/router_triggers.py`: сообщение валидируется, сохраняется в БД, проверяются триггеры mention/reply и вызывается LLM.  
- История сейчас хранится только в таблице `messages` (`chat_id`, `user_id`, `text`, `date`, `is_bot`) без слоя «долгой памяти»/профиля пользователя.  
- Получение контекста для LLM делается через `ContextService.get_recent_turns(chat_id, limit)`, т.е. берутся последние сообщения чата, без пользовательского разреза.  
- Prompt собирается в `build_system_prompt` + `build_messages` (`app/services/context.py`): system prompt + блок `История:` + финальный user-вопрос.  
- Персонализация сейчас только на уровне чата через `chat_settings` (`style`, `temperature`, `quiet_hours`, `revive_*`), не на уровне `(chat_id, user_id)`.  
- Глобальные лимиты/промпты/LLM-провайдер лежат в `app_settings` через `AppConfigService`; есть лимиты в Redis через `UsageLimiter`.  
- Пользователи есть в таблице `users` (`tg_id`, `username`, `is_admin_cached`), но без предпочтений/границ/фактов; связь с чатом не нормализована отдельно (кроме факта сообщений в `messages`).  
- В проекте не найдено текущего механизма TTL/retention для `messages`: нет плановой очистки, только ограничение глубины выборки контекста (`context_max_turns`, `context_max_prompt_tokens`).

---

## 2) Вариант 1 — «Минимальные изменения (MVP)»
### Идея
Добавить легковесную таблицу памяти пользователя в рамках чата и подмешивать её в prompt перед историей. Без сложного поиска, только «сжатый профиль + отношения».

### Хранение
Новые таблицы:

```sql
create table user_chat_memory (
  id bigserial primary key,
  chat_id bigint not null,
  user_id bigint not null,
  affinity smallint not null default 0,            -- -5..+5
  tone_hint varchar(64) null,                      -- дружелюбно/формально/иронично
  boundaries jsonb not null default '[]'::jsonb,   -- что не поднимать
  preferences jsonb not null default '{}'::jsonb,  -- темы/формат
  facts jsonb not null default '{}'::jsonb,        -- подтверждённые факты
  summary text null,                               -- короткая выжимка
  updated_at timestamp not null default now(),
  expires_at timestamp null,
  unique(chat_id, user_id)
);

create index ix_user_chat_memory_chat_user on user_chat_memory(chat_id, user_id);
create index ix_user_chat_memory_expires on user_chat_memory(expires_at);
```

Опционально (минимально): добавить `message_retention_days` в `app_settings` и cron-задачу удаления старых `messages`.

### Как формируется контекст для LLM
Порядок:
1. `chat summary` (если есть, можно хранить в `chat_settings.key=chat_summary`),
2. `user memory` для автора текущего сообщения `(chat_id, from_user.id)`,
3. последние `N` сообщений (как сейчас).

Псевдокод:

```python
turns = context.get_recent_turns(session, chat_id, max_turns)
mem = user_memory_repo.get(chat_id, from_user_id)
chat_summary = settings.get(chat_id, "chat_summary")

system = build_system_prompt(chat_conf, focus_text, ...)
memory_block = format_memory(chat_summary, mem)  # 300-600 токенов
messages = build_messages(system + "\n\n" + memory_block, turns, max_turns, max_prompt_tokens)
```

Лимиты:
- `memory_max_tokens` (например 400) — новый ключ в `app_settings`.
- Суммаризацию `summary/facts` обновлять не на каждое сообщение, а раз в K сообщений пользователя или по триггеру mention/reply.

### Как задаются «отношения»
Поля: `affinity`, `tone_hint`, `boundaries`, `preferences`, `facts`, `summary`.

Обновление:
- авто: небольшой post-processing после ответа LLM (или по lightweight-правилам),
- ручное: через админку (новая вкладка per-chat-user) или команды админа.

Кто меняет:
- автообновление системой;
- ручное — только админ чата/глобальный админ.

### Плюсы / минусы / риски
Плюсы: быстро внедряется, минимум миграций, не ломает текущий pipeline.
Минусы: нет точного retrieval по эпизодам, возможен «раздутый» summary.
Риски: галлюцинации при автообновлении facts — нужен флаг `confidence/source` или правило «факты только из явных фраз пользователя».

### Оценка трудоёмкости
S

### Изменения в коде
- `app/models/` — добавить модель `UserChatMemory`.
- `migrations/versions/` — миграция новой таблицы и индексов.
- `app/services/` — новый сервис `user_memory.py` (CRUD + TTL + форматирование блока).
- `app/bot/router_triggers.py` — перед `build_messages` подмешивать memory block по `message.from_user.id`.
- `app/services/interjector.py` — аналогично для спонтанных ответов (если есть target user в фокусе).
- `app/admin/router.py` — минимум просмотр/сброс памяти `(chat_id,user_id)`.

### Миграция и обратная совместимость
- Шаг 1: deploy с новой таблицей, но feature-flag `user_memory_enabled=false` в `app_settings`.
- Шаг 2: включить флаг на части чатов.
- Если записи памяти нет — логика 100% как сейчас.

---

## 3) Вариант 2 — «Масштабируемая память (поиск по фактам/эпизодам)»
### Идея
Разделить память на: (а) устойчивый профиль пользователя, (б) журнал эпизодов/фактов. На каждом ответе подтягивать только релевантные элементы (retrieval), а не весь профиль.

### Хранение
1) Профиль:
```sql
create table user_chat_profile (
  chat_id bigint not null,
  user_id bigint not null,
  profile jsonb not null default '{}'::jsonb,
  version int not null default 1,
  updated_at timestamp not null default now(),
  primary key(chat_id, user_id)
);
```

2) Event log:
```sql
create table memory_events (
  id bigserial primary key,
  chat_id bigint not null,
  user_id bigint not null,
  event_type varchar(32) not null,   -- preference|boundary|fact|tone_signal
  payload jsonb not null,
  source_message_id bigint null,
  score real not null default 1.0,
  created_at timestamp not null default now(),
  expires_at timestamp null
);

create index ix_memory_events_chat_user_time on memory_events(chat_id, user_id, created_at desc);
create index ix_memory_events_type on memory_events(event_type);
```

3) Опционально для Postgres: `tsvector` + GIN индекс для FTS по текстовым полям `payload->>'text'` (без внедрения внешнего vector DB).

### Ретрив
Во время ответа:
- берем автора `uid`,
- ищем топ-K событий по `(chat_id, uid)` с фильтром TTL,
- ранжируем по: типу события, свежести, текстовой релевантности к текущему вопросу,
- в prompt кладем только 5–10 фактов/эпизодов.

Псевдокод:

```python
query = normalize(message.text)
events = memory_repo.search(chat_id, uid, query, limit=10)
profile = profile_repo.get(chat_id, uid)

memory_context = render(profile, events)
messages = build_messages(system + "\n\n" + memory_context, recent_turns, ...)
```

### Обновление памяти
Правила записи:
- сохраняем только сигналы: «предпочитаю», «не хочу», «меня зовут», «мне важно» и т.п.;
- не сохраняем одноразовый шум/флуд/команды.

Дедуп:
- hash `(chat_id,user_id,event_type,normalized_payload)`;
- при дубле обновляем `created_at/score`, а не создаем новый.

TTL:
- `boundary` и явные `facts` — длинный TTL или без TTL,
- `tone_signal`/эпизоды — 30–90 дней.

Версионирование:
- `user_chat_profile.version` + периодический re-build профиля из событий.

### Плюсы / минусы / риски
Плюсы: контролируемый рост, выше точность персонализации, меньше токенов в prompt.
Минусы: больше кода (retrieval/ranking), сложнее отладка.
Риски: неверный rank и потеря важного факта; нужен fallback «если retrieval пуст — брать compact summary из profile».

### Оценка трудоёмкости
M

### Изменения в коде
- `app/models/` — `UserChatProfile`, `MemoryEvent`.
- `migrations/versions/` — новые таблицы + индексы (+ опц. FTS индексы).
- `app/services/` — `memory_extract.py`, `memory_retrieval.py`, `memory_profile.py`.
- `app/bot/router_triggers.py` и `app/services/interjector.py` — интеграция retrieval в сборку prompt.
- `app/admin/router.py` — просмотр/поиск/очистка событий пользователя.
- `app/infra/scheduler.py` + job — TTL cleanup и compaction.

### Миграция и обратная совместимость
- Сначала read-through в режиме «пишем memory_events, но в prompt не используем».
- После накопления данных включаем retrieval флагом.
- Текущая логика по `messages` остаётся неизменной как fallback.

---

## 4) Вариант 3 — «Отношения как модель поведения (policy + state machine)»
### Идея
Добавить явную модель состояний отношений бота с пользователем в рамках чата и использовать её как policy-слой поверх памяти: не только «что помнить», но и «как отвечать».

### Хранение
```sql
create table relationship_state (
  chat_id bigint not null,
  user_id bigint not null,
  state varchar(24) not null,       -- neutral|friendly|cold|tense
  affinity smallint not null default 0,
  policy jsonb not null default '{}'::jsonb,   -- тон, ограничения, запреты
  last_transition_at timestamp not null default now(),
  updated_at timestamp not null default now(),
  primary key(chat_id, user_id)
);

create table relationship_events (
  id bigserial primary key,
  chat_id bigint not null,
  user_id bigint not null,
  from_state varchar(24) not null,
  to_state varchar(24) not null,
  reason jsonb not null,
  created_at timestamp not null default now()
);
```

### Управление
- Команды/админка:
  - reset памяти пользователя,
  - export памяти пользователя,
  - disable personalization для чата/пользователя.
- Кто может: только админ чата/глобальный админ.
- Технически в проекте уже есть админ-панель и token-gated доступ — расширяется существующий `app/admin/router.py`.

### Безопасность
Политики:
- не создавать неподтвержденные факты о человеке;
- не усиливать токсичность и манипулятивные паттерны;
- при `state=tense` отвечать нейтрально и коротко;
- уважать `boundaries` как hard constraints в prompt.

Prompt-policy пример:

```python
policy = relationship_policy(chat_id, uid)
# policy: {tone: "neutral", disallow: ["personal attacks", ...], max_directness: 0.4}
system = build_system_prompt(...) + "\n\n" + render_policy(policy)
messages = build_messages(system, turns, ...)
```

### Плюсы / минусы / риски
Плюсы: предсказуемое поведение, лучше управляемость и безопасность.
Минусы: потребуется продумать корректные transition rules.
Риски: слишком жёсткая state-machine может сделать ответы «деревянными».

### Оценка трудоёмкости
M/L

### Изменения в коде
- `app/models/` + `migrations/versions/` — `relationship_state`, `relationship_events`.
- `app/services/` — `relationship_policy.py` (state transitions + render policy block).
- `app/bot/router_triggers.py` — policy в system prompt.
- `app/services/interjector.py` — policy при спонтанных репликах.
- `app/admin/router.py` — ручное управление state/reset/export.

### Миграция и обратная совместимость
- По умолчанию всем `(chat,user)` присвоить `neutral`.
- Если state не найден — применять текущий стиль чата без персонализации.
- Включать state-machine флагом `relationship_policy_enabled`.

---

## 5) Рекомендация: с чего начать
Первым шагом брать **Вариант 1 (MVP)**: он наименее инвазивен к текущему pipeline (`collect_messages -> get_recent_turns -> build_messages -> llm_generate`), быстро даёт персональные «отношения» в группах через ключ `(chat_id,user_id)` и не ломает текущий контекст чата. После стабилизации схемы полей (`affinity/boundaries/preferences/facts`) можно эволюционно перейти к Варианту 2, переиспользуя те же данные как основу retrieval.

---

## 6) Что в итоге реализовано в коде
- База перешла на гибридный вариант: `messages RAG + profile + relationship state`.
- Хранилище:
  - `messages` как сырой источник retrieval
  - `user_memory_profiles`
  - `relationship_states`
- Retrieval работает по прошлым сообщениям конкретного пользователя из таблицы `messages`.
- Память подмешивается:
  - в ответы на `@mention`;
  - в ответы на reply боту;
  - в спонтанные вмешательства;
  - в revive тихого чата.
- Обновление `profile` и `relationship_state` теперь происходит через sidecar-JSON в том же основном LLM-запросе, когда бот и так отвечает.
- Добавлена админка для просмотра и сброса памяти по пользователю.
- Добавлены глобальные настройки памяти и chat-level toggle `personalization_enabled`.

Практический план пилота и тест-кейсы вынесены в [user_memory_pilot.md](user_memory_pilot.md).
