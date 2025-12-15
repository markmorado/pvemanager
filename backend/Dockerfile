# Multi-stage build для минимального размера образа
FROM python:3.12-alpine AS builder

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install build dependencies
RUN apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    postgresql-dev \
    libffi-dev \
    openssl-dev \
    cargo \
    rust

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --user --no-warn-script-location -r requirements.txt

# Final stage - минимальный образ
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/root/.local/bin:$PATH

WORKDIR /app

# Install only runtime dependencies
RUN apk add --no-cache \
    libpq \
    openssh-client \
    curl \
    git \
    docker-cli \
    libstdc++ \
    tzdata \
    && mkdir -p /root/.ssh /app/logs \
    && cp /usr/share/zoneinfo/Asia/Tashkent /etc/localtime \
    && echo "Asia/Tashkent" > /etc/timezone \
    && git config --global --add safe.directory /project

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy application code
COPY app ./app
COPY migrations ./migrations
COPY init_admin.py .

# Health check с быстрой проверкой
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=2 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Оптимизированный запуск с preload для быстрого старта
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "100", "--timeout-keep-alive", "30"]
