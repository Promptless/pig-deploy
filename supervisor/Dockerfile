FROM python:3.14-slim
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY supervisor ./supervisor
RUN pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-cache \
    && pip uninstall -y uv pip setuptools wheel
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
ENTRYPOINT ["pig-supervisor"]
