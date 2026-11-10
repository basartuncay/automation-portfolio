# Built by docker-compose.yml, which passes the two reused projects as named build contexts.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app/price-watch
COPY --from=price-monitor pyproject.toml /app/price-monitor/
COPY --from=price-monitor price_monitor /app/price-monitor/price_monitor
COPY --from=alert-bot pyproject.toml /app/alert-bot/
COPY --from=alert-bot alert_bot /app/alert-bot/alert_bot
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY price_watch ./price_watch
COPY samples ./samples
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/price-watch/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DB_PATH=/data/watch.db
EXPOSE 8000
CMD ["uvicorn", "price_watch.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
