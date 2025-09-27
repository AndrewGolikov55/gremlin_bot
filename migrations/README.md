This directory contains Alembic migration scripts.

Quick start:

1) Ensure DATABASE_URL is set (env or .env).
2) Create a new revision:

   alembic revision -m "init" --autogenerate

3) Apply migrations:

   alembic upgrade head

