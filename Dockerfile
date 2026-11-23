FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LOG_FORMAT=json

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[api]"

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["alphamacro-bot"]
