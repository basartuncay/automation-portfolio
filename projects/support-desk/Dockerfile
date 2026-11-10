# Built by docker-compose.yml, which passes the two reused projects as named build contexts.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app/support-desk
COPY --from=email-triage pyproject.toml /app/email-triage/
COPY --from=email-triage email_triage /app/email-triage/email_triage
COPY --from=doc-qa pyproject.toml /app/doc-qa/
COPY --from=doc-qa doc_qa /app/doc-qa/doc_qa
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY support_desk ./support_desk
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/support-desk/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DB_PATH=/data/support.db KB_DIR=/kb
EXPOSE 8000
CMD ["uvicorn", "support_desk.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
