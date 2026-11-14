FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/apps/api:/app/packages/shared/src
COPY packages/shared packages/shared
COPY apps/api apps/api
COPY apps/consumer apps/consumer
RUN uv sync --directory apps/api --locked --no-dev
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "-c", "exec uvicorn main:app --app-dir apps/api --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
