FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ops_agent ./ops_agent
COPY evals ./evals
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DB_PATH=/data/ops.db EVAL_REPORT=/data/eval-report.json
EXPOSE 8000
CMD ["uvicorn", "ops_agent.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
