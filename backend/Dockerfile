# syntax=docker/dockerfile:1.7
# Multi-stage build на python:3.12-slim (glibc) — wheels ставятся бинарно,
# никакого rust/cargo не требуется → сборка в разы быстрее.
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Минимальные build-зависимости (на случай если какой-то пакет придётся компилировать)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# BuildKit-кеш для pip — повторные сборки почти мгновенные
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user --no-warn-script-location -r requirements.txt

# ---------- Финальный образ ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/root/.local/bin:$PATH \
    TZ=Asia/Tashkent

WORKDIR /app

# Только runtime-зависимости. Docker CLI не нужен: self-update выполняет
# хостовый сервис по файлу-триггеру (services/update_service.py), сокет Docker
# в контейнер не монтируется.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        openssh-client \
        curl \
        git \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /root/.ssh /app/logs \
    && ln -fs /usr/share/zoneinfo/Asia/Tashkent /etc/localtime \
    && echo "Asia/Tashkent" > /etc/timezone \
    && git config --global --add safe.directory /project

COPY --from=builder /root/.local /root/.local

COPY app ./app
COPY migrations ./migrations
COPY alembic ./alembic
COPY alembic.ini .
COPY init_admin.py .
COPY assign_instances_owner.py .

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD curl -fs http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "100", "--timeout-keep-alive", "30"]
