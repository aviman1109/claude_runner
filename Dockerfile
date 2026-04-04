FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=38095 \
    WORKSPACES_ROOT=/workspaces \
    CLAUDE_BIN=/claude-cli/claude

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir .

EXPOSE 38095

CMD ["claude-runner"]
