FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=38095 \
    WORKSPACES_ROOT=/workspaces \
    CLAUDE_BIN=/claude-cli/claude

COPY pyproject.toml README.md ./
COPY src ./src

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 1000 casper && useradd -u 1000 -g 1000 -d /home/casper -s /bin/bash casper && \
    pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir .

EXPOSE 38095

CMD ["claude-runner"]
