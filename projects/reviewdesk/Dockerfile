FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY reviewdesk ./reviewdesk
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DB_PATH=/data/reviewdesk.db
EXPOSE 8000
CMD ["uvicorn", "reviewdesk.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
