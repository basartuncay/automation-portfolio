FROM apify/actor-python:3.12
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes > req.txt && pip install --no-cache-dir -r req.txt
COPY . ./
CMD ["python3", "-m", "src"]
