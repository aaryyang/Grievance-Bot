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

# Pre-download HuggingFace models at build time so cold-starts are fast
RUN python -c "\
    from transformers import pipeline;\
    pipeline('zero-shot-classification', model='valhalla/distilbart-mnli-12-3');\
    pipeline('sentiment-analysis',       model='nlptown/bert-base-multilingual-uncased-sentiment');\
    print('Models cached.')"

EXPOSE 8000

CMD ["python", "main.py"]
