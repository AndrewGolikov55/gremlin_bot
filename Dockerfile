# syntax=docker/dockerfile:1

############################
# Builder: compile wheels
############################
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

# System deps for building wheels (add dev headers only here)
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Only requirements first to maximize cache hits
COPY requirements.txt ./

# Build wheels for all deps
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

############################
# Runtime: slim final image
############################
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

# Install only runtime libraries (no compilers)
# Add bash if your entrypoint scripts rely on it
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash libpq5 \
 && rm -rf /var/lib/apt/lists/*

# Install python deps from prebuilt wheels
COPY requirements.txt ./requirements.txt
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# App files
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app

EXPOSE 8080

# Default command (can be overridden by docker-compose)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
