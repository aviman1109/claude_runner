# Claude Runner

Lightweight HTTP API that executes Claude CLI (`claude -p`) tasks on behalf of any caller — Home Assistant automations, MCP servers, scripts, or cron jobs.

## Architecture

```
Caller (HA / livetrack-mcp / script / cron)
  → POST http://localhost:38095/run
  → Claude Runner spawns claude -p in the target workspace
  → Claude calls MCP tools (Garmin, Calendar, Telegram, etc.)
  → Claude sends results via Telegram MCP
  → Runner returns {"status": "accepted"} immediately (fire-and-forget)
```

The Runner is a **stateless executor**. All scheduling, trigger logic, and prompt definitions live in the caller (HA automations, livetrack-mcp scheduler, etc.).

## Callers

| Caller | Workspace | Use case |
|---|---|---|
| Home Assistant automations | `training`, `work` | Daily summaries, alerts |
| livetrack-mcp (port 38099) | `training` | Race-day LiveTrack analysis every 10 min |
| Scripts / cron | any | One-off tasks |

## API

### POST /run

Submit a task for execution.

**Request:**

```json
{
  "workspace": "training",
  "prompt": "查 Garmin 最新訓練數據，分析表現後用 telegram MCP 發送摘要",
  "model": "haiku",
  "allowed_tools": "garmin,telegram"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `workspace` | `str` | Yes | Workspace name under `~/workspaces/` |
| `prompt` | `str` | Yes | Prompt to send to `claude -p` |
| `model` | `str` | No | Model name (default: `haiku`) |
| `allowed_tools` | `str` | No | Comma-separated tool allowlist |

**Response:**

```json
{
  "status": "accepted",
  "task_id": "a6279fa2",
  "message": "Task queued: training (model=haiku)"
}
```

### GET /status/{task_id}

Check task execution status.

**Response:**

```json
{
  "task_id": "a6279fa2",
  "status": "completed",
  "started_at": "2026-04-02T03:03:44Z",
  "completed_at": "2026-04-02T03:03:52Z",
  "workspace": "training",
  "model": "haiku",
  "result": "Message sent successfully",
  "error": null
}
```

Status values: `running`, `completed`, `failed`.

### GET /health

```json
{
  "status": "ok",
  "running_tasks": 0,
  "max_concurrent": 3
}
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `PORT` | `38095` | Server port |
| `HOST` | `0.0.0.0` | Bind address |
| `WORKSPACES_ROOT` | `~/workspaces` | Path to workspace directories |
| `CLAUDE_BIN` | `~/.local/bin/claude` | Path to Claude CLI binary |
| `DEFAULT_MODEL` | `haiku` | Default model for tasks |
| `MAX_CONCURRENT` | `3` | Maximum concurrent `claude -p` processes |

## Deployment

### Docker Compose (recommended)

Runs with `network_mode: host` and volume mounts for workspace and Claude CLI access.

```bash
docker compose up -d claude-runner
```

**Key Docker settings:**
- `user: "1000:1000"` — runs as casper (Claude CLI rejects root with `--dangerously-skip-permissions`)
- `network_mode: host` — direct access to localhost MCP endpoints
- Volume mounts: workspaces (ro), Claude CLI (ro), auth config (ro)

### systemd (alternative)

```bash
cp claude-runner.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-runner
```

## Home Assistant Integration

### rest_command

```yaml
# HA configuration.yaml
rest_command:
  claude_run:
    url: "http://192.168.1.x:38095/run"
    method: POST
    content_type: "application/json"
    payload: '{"workspace":"{{ workspace }}","prompt":"{{ prompt }}","model":"{{ model | default("haiku") }}"}'
    timeout: 10
```

### Automation Example

```yaml
automation:
  - alias: "工作日行程摘要"
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    action:
      - service: rest_command.claude_run
        data:
          workspace: work
          prompt: >-
            查今天行事曆，整理出今日行程摘要，完成後用 telegram MCP 發送。
          model: haiku
```

## Project Structure

```
claude_runner/
├── Dockerfile
├── pyproject.toml
├── README.md
├── claude-runner.service  # systemd unit file (host deployment)
├── src/claude_runner/
│   ├── __init__.py
│   ├── __main__.py
│   └── server.py          # FastAPI application
└── config/                # Reserved for future config
```

## Security Notes

- The Runner executes `claude -p --dangerously-skip-permissions` which grants full tool access
- Only expose port 38095 on the LAN (bound to `0.0.0.0` via host network)
- Workspace volumes are mounted read-only
- Consider adding authentication if exposing beyond the local network

## License

MIT
