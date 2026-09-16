FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY supervisor ./supervisor
RUN pip install --no-cache-dir uv==0.8.22 && uv sync --frozen --no-dev
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
ENTRYPOINT ["pig-supervisor"]
