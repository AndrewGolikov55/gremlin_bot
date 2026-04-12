# Gremlin Telegram Bot

Телеграм-бот для групповых чатов с персонами, памятью участников и админ-панелью. Python, FastAPI, aiogram, PostgreSQL, Redis.

## Возможности

- polling и webhook
- ответы по контексту истории + персональная память участников (RAG)
- базовые персоны (standup, gopnik, boss, zoomer, jarvis) и кастомные
- команды `/settings`, `/style`, `/summary`, `/roll` и др.
- админ-панель (Bootstrap): чаты, настройки, история, память, персоны, рассылки
- провайдеры LLM: OpenRouter или OpenAI
- реакции эмодзи на сообщения
- `/health`, `/metrics`, сетевой probe

## Быстрый старт

```bash
cp .env.example .env   # заполните BOT_TOKEN, ADMIN_TOKEN, OPENAI_API_KEY и пр.
docker compose up --build
```

Проверка: `http://localhost:8080/health`, админка: `/admin/chats?token=<ADMIN_TOKEN>`.

Hot reload — через `docker-compose.dev.yml`. Миграции (`alembic upgrade head`) выполняются при старте контейнера автоматически.

## Переменные окружения

Все переменные описаны в [`.env.example`](.env.example). Основные:

- `BOT_TOKEN`, `ADMIN_TOKEN` — обязательны
- `USE_POLLING=1` — для локального запуска; `PUBLIC_BASE_URL` + `TELEGRAM_SECRET_TOKEN` — для webhook
- `OPENROUTER_API_KEY` / `OPENAI_API_KEY` — LLM провайдеры
- `DATABASE_URL`, `REDIS_URL` — инфраструктура
- `NETWORK_SOCKS5_*` — опциональный SOCKS5 для всех исходящих

## Разработка

```bash
python -m pip install -r requirements-dev.txt
```

- `make lint` проверяет только `ruff check .`
- `make typecheck` запускает только `mypy app tests`
- `make test` запускает только `pytest`
- `make check` запускает `ruff check .`, `mypy app tests`, `pytest` подряд

Миграции: `alembic revision -m "..." --autogenerate`, `alembic upgrade head`, `alembic downgrade -1`.
