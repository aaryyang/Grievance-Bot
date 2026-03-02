# ── Stage 1: build deps ───────────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

# System deps for torch + transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: app ──────────────────────────────────────────────────────────────
FROM base
WORKDIR /app
COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
