COMPOSE_DEV := docker compose -f docker-compose.dev.yml

.PHONY: dev-build dev-up dev-restart dev-down dev-logs dev-ps dev-shell dev-migrate lint lint-fix typecheck test check

dev-build:
	$(COMPOSE_DEV) build

dev-up:
	$(COMPOSE_DEV) up -d

dev-restart:
	$(COMPOSE_DEV) up -d --build --force-recreate

dev-down:
	$(COMPOSE_DEV) down

dev-logs:
	$(COMPOSE_DEV) logs -f --tail=200

dev-ps:
	$(COMPOSE_DEV) ps

dev-shell:
	$(COMPOSE_DEV) exec bot bash

dev-migrate:
	$(COMPOSE_DEV) exec bot alembic upgrade head

lint:
	ruff check .

lint-fix:
	ruff check . --fix

typecheck:
	mypy app tests

test:
	pytest

check:
	ruff check .
	mypy app tests
	pytest
