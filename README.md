# Shutnik Telegram Bot (MVP Skeleton)

Минимальный каркас бота для групповых чатов на Python 3.11+ с FastAPI (вебхук/health/metrics), aiogram v3 (обработчики/роутеры), PostgreSQL + Redis, и базовой заготовкой сервисов/моделей.

## Что уже есть
- FastAPI-приложение с эндпоинтами `/health`, `/metrics`, `/webhook/telegram`.
- aiogram v3: базовые роутеры, команды `/bot on|off|status`, `/profanity`, `/settings`.
- Сохранение входящих текстовых сообщений в БД (для будущего контекста/аналитики).
- БД: SQLAlchemy модели `chats`, `chat_settings`, `messages`, `users`; автосоздание таблиц на старте.
- Redis: кэш настроек чата (инвалидация при изменении).
- Заготовки: InterjectorService, LLM (Ollama), Moderation, APScheduler.
- Prometheus-метрики: счётчики обновлений/сообщений.
- Alembic скелет (можно генерировать миграции).
- Dockerfile + docker-compose для локального запуска.

## Быстрый старт
1) Скопируйте `.env.example` → `.env` и задайте переменные:
   - `BOT_TOKEN` — токен бота
   - `USE_POLLING=1` для локальной разработки без вебхука (по умолчанию)
   - Для вебхука: `PUBLIC_BASE_URL=https://your.domain` и `TELEGRAM_SECRET_TOKEN=...`

2) Запуск:
```
docker compose up --build
```
   - Приложение: `http://localhost:8080/health`
   - Метрики: `http://localhost:8080/metrics`
   - В дев-режиме включён polling. Для прод-вебхука поставьте `USE_POLLING=0` и укажите `PUBLIC_BASE_URL`.

   Для разработки с live-reload можно использовать `docker compose -f docker-compose.dev.yml up --build` — код монтируется внутрь контейнера, uvicorn перезапускается при изменениях.

3) Сервисы в compose:
- `db` (PostgreSQL 16)
- `redis` (Redis 7)
- `ollama` (опц.) — локальная LLM. Настройки: `OLLAMA_URL`, `OLLAMA_MODEL`.

## Команды в чате (MVP)
- `/bot on|off|status` — управление включением в чате и краткий статус.
- `/profanity off|soft|hard` — политика лексики (пока без фактической модерации текста).
- `/settings` — вывод основных параметров.

Данные команд пишутся мгновенно в БД (`chat_settings`), кэшируются в Redis.

## Структура
```
app/
  main.py                # FastAPI + webhook/polling, инициализация инфраструктуры
  bot/
    router_admin.py      # /bot, /profanity, /settings
    router_triggers.py   # сбор входящих сообщений, простая реакция на упоминание
    router_interjector.py# заглушка под APScheduler
    middlewares.py       # DI: сессии БД, сервисы
  services/
    settings.py          # CRUD настроек чата + кэш Redis
    context.py           # построение сообщений для LLM
    interjector.py       # заглушка логики «влезания»
    llm/ollama.py        # простой клиент Ollama
    moderation.py        # заглушка локальной модерации
  models/
    base.py, chat.py, message.py, user.py
  infra/
    db.py, redis.py, scheduler.py
migrations/              # Alembic (env.py, README)
Dockerfile
requirements.txt
alembic.ini
```

## Переменные окружения
См. `.env.example`.

Ключевые:
- `BOT_TOKEN` — токен бота.
- `USE_POLLING` — `1` (дефолт, без вебхука) или `0` (вебхук).
- `PUBLIC_BASE_URL` — базовый URL для вебхука (когда `USE_POLLING=0`).
- `TELEGRAM_SECRET_TOKEN` — секрет для заголовка вебхука.
- `DATABASE_URL` — Postgres (по умолчанию на сервис `db`).
- `REDIS_URL` — Redis (по умолчанию на сервис `redis`).
- `OLLAMA_URL`, `OLLAMA_MODEL` — для LLM.

## Вебхук vs Polling
- Dev: `USE_POLLING=1` — FastAPI поднимется, а aiogram запустится в фоне в режиме polling.
- Prod: `USE_POLLING=0` + `PUBLIC_BASE_URL` — на старте выставится вебхук `/webhook/telegram` c секретом `X-Telegram-Bot-Api-Secret-Token`.

## Миграции (Alembic)
- Создать ревизию: `alembic revision -m "init" --autogenerate`
- Применить: `alembic upgrade head`

Сейчас таблицы создаются автоматически на старте (для удобства разработки). В проде переводите на миграции.

## Дальше по плану
- Добавить роутеры: `mentions`, `replies`, `interjector` с вероятностями и кулдаунами.
- Реальная логика InterjectorService + APScheduler.
- Политики стиля/лексики, лимиты, таргет-листы, контекст с суммаризацией.
- Admin API (FastAPI): `/admin/chats/<id>/settings`.

Готов двигаться к следующим фичам — скажите, что делать первым.
