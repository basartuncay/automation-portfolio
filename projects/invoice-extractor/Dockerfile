FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra api --extra ocr --no-install-project
COPY . .
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
ENV PATH="/app/.venv/bin:$PATH" DB_PATH=/data/app.sqlite SEEN_DB=/data/seen.sqlite
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["sh", "-c", "uvicorn invoice_extractor.api:app --host 0.0.0.0 --port 8000"]
