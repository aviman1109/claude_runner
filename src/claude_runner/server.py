"""Claude Runner API — HTTP executor for Claude CLI tasks (single-shot + multi-turn sessions)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# OpenTelemetry
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("claude-runner")

_tracer: trace.Tracer | None = None
_counter_tasks: metrics.Counter | None = None
_histogram_duration: metrics.Histogram | None = None
_counter_images: metrics.Counter | None = None


def setup_otel() -> None:
    global _tracer, _counter_tasks, _histogram_duration, _counter_images

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    resource = Resource.create({"service.name": "claude-runner", "service.version": "0.2.0"})

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otel_endpoint}/v1/traces")))
    trace.set_tracer_provider(tp)
    _tracer = trace.get_tracer("claude-runner")

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{otel_endpoint}/v1/metrics"),
        export_interval_millis=30_000,
    )
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)
    meter = metrics.get_meter("claude-runner")

    _counter_tasks = meter.create_counter("claude_runner.tasks_total", description="Total tasks executed")
    _histogram_duration = meter.create_histogram(
        "claude_runner.task.duration_seconds", description="Task execution duration in seconds", unit="s",
    )
    _counter_images = meter.create_counter(
        "claude_runner.image.downloads_total", description="Image download attempts",
    )

    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry configured → %s", otel_endpoint)


WORKSPACES_ROOT = Path(os.getenv("WORKSPACES_ROOT", os.path.expanduser("~/workspaces")))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "haiku")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
SESSION_DB = Path(os.getenv("SESSION_DB", "/data/sessions.sqlite"))
IDLE_CLOSE_SECS = int(os.getenv("IDLE_CLOSE_SECS", "7200"))  # 2h
IDLE_SCAN_SECS = int(os.getenv("IDLE_SCAN_SECS", "300"))     # scan every 5m
MEMORY_CLOSE_PROMPT = os.getenv(
    "MEMORY_CLOSE_PROMPT",
    "本次對話即將結束。請把這次交談裡值得留存的事實、決定、或對我的了解寫進 auto-memory 系統，然後直接回覆「已寫入記憶」即可。",
)

_running: dict[str, dict] = {}
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# ── SQLite session store ──────────────────────────────────────────────────

def _db_init() -> None:
    SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SESSION_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                model TEXT NOT NULL,
                created_ts INTEGER NOT NULL,
                last_seen_ts INTEGER NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0,
                closed_reason TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON sessions(last_seen_ts, closed)")


@contextmanager
def _db():
    conn = sqlite3.connect(SESSION_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _session_get(user_id: str) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def _session_upsert(user_id: str, session_id: str, workspace: str, model: str) -> None:
    now = int(time.time())
    with _db() as c:
        c.execute("""
            INSERT INTO sessions (user_id, session_id, workspace, model, created_ts, last_seen_ts, closed)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                session_id=excluded.session_id,
                workspace=excluded.workspace,
                model=excluded.model,
                created_ts=excluded.created_ts,
                last_seen_ts=excluded.last_seen_ts,
                closed=0,
                closed_reason=NULL
        """, (user_id, session_id, workspace, model, now, now))
        c.commit()


def _session_touch(user_id: str) -> None:
    with _db() as c:
        c.execute("UPDATE sessions SET last_seen_ts = ? WHERE user_id = ?", (int(time.time()), user_id))
        c.commit()


def _session_close(user_id: str, reason: str) -> None:
    with _db() as c:
        c.execute("UPDATE sessions SET closed = 1, closed_reason = ? WHERE user_id = ?", (reason, user_id))
        c.commit()


# ── Claude CLI invocation ─────────────────────────────────────────────────

async def _run_claude(
    prompt: str, workspace_dir: Path, model: str,
    session_id: str | None, resume: bool,
    allowed_tools: str | None = None,
) -> tuple[int, str, str]:
    """Invoke claude CLI; return (returncode, stdout, stderr). stdout is JSON on success."""
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    if resume and session_id:
        cmd.extend(["--resume", session_id])
    elif session_id:
        cmd.extend(["--session-id", session_id])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace_dir),
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


def _parse_claude_json(out: str) -> tuple[str, str]:
    """Return (session_id, reply_text) from claude --output-format json output."""
    data = json.loads(out)
    return data.get("session_id", ""), data.get("result", "")


# ── App ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Claude Runner", version="0.2.0")
FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")[:500]
    logger.error("422 validation error | errors: %s | body: %s", exc.errors(), body_str)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ── Legacy single-shot endpoint ───────────────────────────────────────────

class RunRequest(BaseModel):
    workspace: str
    prompt: str
    model: str = Field(default=DEFAULT_MODEL)
    allowed_tools: str | None = None
    image_url: str | None = None
    image_data: str | None = None


async def _execute(task_id: str, req: RunRequest) -> None:
    workspace_dir = WORKSPACES_ROOT / req.workspace
    if not workspace_dir.is_dir():
        _running[task_id].update(status="failed", error=f"Workspace not found: {workspace_dir}",
                                 completed_at=datetime.now(timezone.utc).isoformat())
        if _counter_tasks: _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "failed"})
        return

    prompt = req.prompt
    image_path: Path | None = None
    t_start = time.monotonic()

    if req.image_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(req.image_url)
                resp.raise_for_status()
            ext = ".png" if "png" in (resp.headers.get("content-type") or "") else ".jpg"
            image_path = Path(tempfile.gettempdir()) / f".tmp_runner_image_{task_id}{ext}"
            image_path.write_bytes(resp.content)
            prompt = f"An image has been saved at {image_path}. Read it with the Read tool to see its contents.\n\n{prompt}"
            if _counter_images: _counter_images.add(1, {"status": "ok"})
        except Exception as exc:
            logger.warning("Image download failed: %s", exc)
            if _counter_images: _counter_images.add(1, {"status": "failed"})
    elif req.image_data:
        try:
            image_bytes = base64.b64decode(req.image_data)
            image_path = Path(tempfile.gettempdir()) / f".tmp_runner_image_{task_id}.jpg"
            image_path.write_bytes(image_bytes)
            prompt = f"An image has been saved at {image_path}. Read it with the Read tool to see its contents.\n\n{prompt}"
        except Exception as exc:
            logger.warning("image_data decode failed: %s", exc)

    async with _semaphore:
        try:
            rc, out, err = await _run_claude(
                prompt=prompt, workspace_dir=workspace_dir, model=req.model,
                session_id=None, resume=False, allowed_tools=req.allowed_tools,
            )
            if rc == 0:
                try:
                    _sid, reply = _parse_claude_json(out)
                except Exception:
                    reply = out.strip()
                _running[task_id].update(status="completed", result=reply)
                if _counter_tasks: _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "completed"})
            else:
                combined = err.strip() or out.strip() or "(no output)"
                _running[task_id].update(status="failed", error=combined, stdout=out.strip())
                logger.error("Task %s failed rc=%d stderr=%r stdout=%r", task_id, rc, err, out)
                if _counter_tasks: _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "failed"})
        except Exception as exc:
            _running[task_id].update(status="failed", error=str(exc))
            logger.exception("Task %s exception", task_id)
        finally:
            _running[task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            if _histogram_duration:
                _histogram_duration.record(time.monotonic() - t_start, {"workspace": req.workspace, "model": req.model})
            if image_path and image_path.exists():
                image_path.unlink(missing_ok=True)


@app.post("/run")
async def run_task(req: RunRequest):
    workspace_dir = WORKSPACES_ROOT / req.workspace
    if not workspace_dir.is_dir():
        raise HTTPException(404, f"Workspace not found: {req.workspace}")
    task_id = uuid.uuid4().hex[:8]
    _running[task_id] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "workspace": req.workspace,
        "model": req.model,
        "result": None,
        "error": None,
    }
    asyncio.create_task(_execute(task_id, req))
    return {"status": "accepted", "task_id": task_id, "message": f"queued: {req.workspace}"}


@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in _running:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, **_running[task_id]}


# ── Conversational /chat endpoint ─────────────────────────────────────────

class ChatRequest(BaseModel):
    user_id: str = Field(description="Stable identifier for the user (e.g. Line userId).")
    message: str
    workspace: str = Field(default="training")
    model: str = Field(default="sonnet")
    allowed_tools: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest):
    """Send a message; auto-start or resume the user's session. Blocks until Claude replies."""
    workspace_dir = WORKSPACES_ROOT / req.workspace
    if not workspace_dir.is_dir():
        raise HTTPException(404, f"Workspace not found: {req.workspace}")

    sess = _session_get(req.user_id)
    now = int(time.time())
    resume = bool(sess and not sess["closed"] and (now - sess["last_seen_ts"]) < IDLE_CLOSE_SECS)
    session_id = sess["session_id"] if resume else str(uuid.uuid4())

    async with _semaphore:
        rc, out, err = await _run_claude(
            prompt=req.message, workspace_dir=workspace_dir, model=req.model,
            session_id=session_id, resume=resume, allowed_tools=req.allowed_tools,
        )
    if rc != 0:
        combined = err.strip() or out.strip() or "(no output)"
        logger.error("chat failed rc=%d user=%s: %s", rc, req.user_id, combined[:200])
        raise HTTPException(500, f"claude CLI failed: {combined[:500]}")

    try:
        returned_sid, reply = _parse_claude_json(out)
    except Exception as exc:
        logger.error("chat JSON parse fail: %s\nout=%r", exc, out[:500])
        raise HTTPException(500, "claude output not JSON")

    effective_sid = returned_sid or session_id
    _session_upsert(req.user_id, effective_sid, req.workspace, req.model)

    return {
        "user_id": req.user_id,
        "session_id": effective_sid,
        "reply": reply,
        "resumed": resume,
    }


@app.get("/session/{user_id}")
async def session_info(user_id: str):
    sess = _session_get(user_id)
    if not sess:
        raise HTTPException(404, "No session for this user")
    sess["idle_secs"] = int(time.time()) - sess["last_seen_ts"]
    return sess


@app.post("/session/{user_id}/close")
async def session_close(user_id: str):
    """Graceful close: prompt session to save memory, mark closed."""
    sess = _session_get(user_id)
    if not sess:
        raise HTTPException(404, "No session for this user")
    if sess["closed"]:
        return {"already_closed": True, "reason": sess["closed_reason"]}

    workspace_dir = WORKSPACES_ROOT / sess["workspace"]
    try:
        rc, out, err = await _run_claude(
            prompt=MEMORY_CLOSE_PROMPT, workspace_dir=workspace_dir, model=sess["model"],
            session_id=sess["session_id"], resume=True,
        )
        ok = rc == 0
    except Exception as exc:
        logger.exception("session_close exception for %s", user_id)
        ok = False
        err = str(exc)

    reason = "manual_close" if ok else f"close_failed: {err[:200]}"
    _session_close(user_id, reason)
    return {"closed": True, "ok": ok, "reason": reason}


# ── Background idle-close sweeper ─────────────────────────────────────────

async def _idle_sweeper() -> None:
    """Every IDLE_SCAN_SECS, close sessions idle > IDLE_CLOSE_SECS."""
    while True:
        try:
            await asyncio.sleep(IDLE_SCAN_SECS)
            cutoff = int(time.time()) - IDLE_CLOSE_SECS
            with _db() as c:
                rows = c.execute(
                    "SELECT user_id FROM sessions WHERE closed = 0 AND last_seen_ts < ?",
                    (cutoff,),
                ).fetchall()
            for r in rows:
                logger.info("idle_sweeper: closing session for %s", r["user_id"])
                try:
                    await session_close(r["user_id"])
                except Exception as exc:
                    logger.warning("idle close failed for %s: %s", r["user_id"], exc)
        except Exception as exc:
            logger.exception("idle_sweeper loop error: %s", exc)


@app.on_event("startup")
async def _startup() -> None:
    _db_init()
    asyncio.create_task(_idle_sweeper())
    logger.info("Session DB at %s, idle close after %ds", SESSION_DB, IDLE_CLOSE_SECS)


# ── Misc ──────────────────────────────────────────────────────────────────

@app.post("/restart-sessions")
async def restart_sessions():
    ssh_key = Path(os.path.expanduser("~/.ssh/id_ed25519_runner"))
    if not ssh_key.exists():
        raise HTTPException(500, f"SSH key not found: {ssh_key}")
    cmd = (f"ssh -i {ssh_key} -o StrictHostKeyChecking=no -o BatchMode=yes "
           f"casper@127.0.0.1 'nohup ~/restart-claude-sessions.sh > /tmp/restart-claude.log 2>&1 &'")
    await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return {"status": "triggered", "log": "/tmp/restart-claude.log"}


@app.get("/health")
async def health():
    running_count = sum(1 for t in _running.values() if t["status"] == "running")
    with _db() as c:
        open_sessions = c.execute("SELECT COUNT(*) AS n FROM sessions WHERE closed = 0").fetchone()["n"]
    return {
        "status": "ok",
        "running_tasks": running_count,
        "open_sessions": open_sessions,
        "max_concurrent": MAX_CONCURRENT,
    }


def main() -> None:
    port = int(os.getenv("PORT", "38095"))
    host = os.getenv("HOST", "0.0.0.0")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    setup_otel()
    logger.info("Claude Runner v0.2 starting on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
