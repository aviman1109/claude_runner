"""Claude Runner API — lightweight HTTP executor for Claude CLI tasks."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
import sys
import tempfile
import time
import uuid
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

# OTel globals (populated by setup_otel)
_tracer: trace.Tracer | None = None
_counter_tasks: metrics.Counter | None = None
_histogram_duration: metrics.Histogram | None = None
_counter_images: metrics.Counter | None = None


def setup_otel() -> None:
    global _tracer, _counter_tasks, _histogram_duration, _counter_images

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    resource = Resource.create({"service.name": "claude-runner", "service.version": "0.1.0"})

    # Traces
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otel_endpoint}/v1/traces")))
    trace.set_tracer_provider(tp)
    _tracer = trace.get_tracer("claude-runner")

    # Metrics
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{otel_endpoint}/v1/metrics"),
        export_interval_millis=30_000,
    )
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)
    meter = metrics.get_meter("claude-runner")

    _counter_tasks = meter.create_counter(
        "claude_runner.tasks_total",
        description="Total tasks executed",
    )
    _histogram_duration = meter.create_histogram(
        "claude_runner.task.duration_seconds",
        description="Task execution duration in seconds",
        unit="s",
    )
    _counter_images = meter.create_counter(
        "claude_runner.image.downloads_total",
        description="Image download attempts",
    )

    # Auto-instrument httpx
    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry configured → %s", otel_endpoint)

WORKSPACES_ROOT = Path(os.getenv("WORKSPACES_ROOT", os.path.expanduser("~/workspaces")))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "haiku")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))

# Track running tasks
_running: dict[str, dict] = {}
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


app = FastAPI(
    title="Claude Runner",
    description="Accepts task requests from Home Assistant and executes Claude CLI.",
    version="0.1.0",
)
FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    try:
        body_str = body.decode("utf-8", errors="replace")[:500]
    except Exception:
        body_str = "<unreadable>"
    logger.error("422 validation error from %s | errors: %s | body: %s",
                 request.client.host if request.client else "unknown",
                 exc.errors(), body_str)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class RunRequest(BaseModel):
    workspace: str = Field(description="Workspace name under ~/workspaces/")
    prompt: str = Field(description="The prompt to send to Claude CLI")
    model: str = Field(default=DEFAULT_MODEL, description="Claude model to use (haiku, sonnet, opus)")
    allowed_tools: str | None = Field(default=None, description="Comma-separated list of allowed tools")
    image_url: str | None = Field(default=None, description="URL of an image to download and include in the prompt")
    image_data: str | None = Field(default=None, description="Base64-encoded image data to include in the prompt (alternative to image_url)")


class RunResponse(BaseModel):
    status: str
    task_id: str
    message: str


class TaskStatus(BaseModel):
    task_id: str
    status: str  # running, completed, failed
    started_at: str
    completed_at: str | None = None
    workspace: str
    model: str
    result: str | None = None
    error: str | None = None


async def _execute(task_id: str, req: RunRequest) -> None:
    workspace_dir = WORKSPACES_ROOT / req.workspace
    if not workspace_dir.is_dir():
        _running[task_id]["status"] = "failed"
        _running[task_id]["error"] = f"Workspace not found: {workspace_dir}"
        _running[task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
        if _counter_tasks:
            _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "failed"})
        return

    span_ctx = _tracer.start_as_current_span(
        "claude_runner.execute",
        attributes={"workspace": req.workspace, "model": req.model, "task_id": task_id},
    ) if _tracer else __import__("contextlib").nullcontext()

    with span_ctx as span:
        prompt = req.prompt
        image_path: Path | None = None
        t_start = time.monotonic()

        # Download image if provided via URL
        if req.image_url:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(req.image_url)
                    resp.raise_for_status()
                ext = ".jpg"
                if "png" in (resp.headers.get("content-type") or ""):
                    ext = ".png"
                image_path = Path(tempfile.gettempdir()) / f".tmp_runner_image_{task_id}{ext}"
                image_path.write_bytes(resp.content)
                prompt = f"An image has been saved at {image_path}. Read it with the Read tool to see its contents.\n\n{prompt}"
                logger.info("Image downloaded: %s (%d bytes)", image_path, len(resp.content))
                if _counter_images:
                    _counter_images.add(1, {"status": "ok"})
            except Exception as exc:
                logger.warning("Failed to download image %s: %s", req.image_url, exc)
                if _counter_images:
                    _counter_images.add(1, {"status": "failed"})

        # Decode base64 image if provided directly
        elif req.image_data:
            try:
                image_bytes = base64.b64decode(req.image_data)
                image_path = Path(tempfile.gettempdir()) / f".tmp_runner_image_{task_id}.jpg"
                image_path.write_bytes(image_bytes)
                prompt = f"An image has been saved at {image_path}. Read it with the Read tool to see its contents.\n\n{prompt}"
                logger.info("Image from base64: %s (%d bytes)", image_path, len(image_bytes))
            except Exception as exc:
                logger.warning("Failed to decode image_data: %s", exc)

        cmd_parts = [
            CLAUDE_BIN,
            "-p", prompt,
            "--model", req.model,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        if req.allowed_tools:
            cmd_parts.extend(["--allowedTools", req.allowed_tools])

        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        logger.info("Executing: workspace=%s model=%s task_id=%s", req.workspace, req.model, task_id)

        async with _semaphore:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workspace_dir),
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    _running[task_id]["status"] = "completed"
                    _running[task_id]["result"] = stdout.decode("utf-8", errors="replace").strip()
                    if _counter_tasks:
                        _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "completed"})
                else:
                    _running[task_id]["status"] = "failed"
                    err_text = stderr.decode("utf-8", errors="replace").strip()
                    out_text = stdout.decode("utf-8", errors="replace").strip()
                    # claude CLI writes rate-limit / auth errors to stdout, not stderr.
                    combined = err_text or out_text or "(no output on either stream)"
                    _running[task_id]["error"] = combined
                    _running[task_id]["stdout"] = out_text
                    logger.error(
                        "Task %s failed (rc=%d): stderr=%r stdout=%r",
                        task_id, proc.returncode, err_text, out_text,
                    )
                    if _counter_tasks:
                        _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "failed"})
                    if span:
                        span.set_attribute("error", True)
                        span.set_attribute("error.returncode", proc.returncode)
            except Exception as exc:
                _running[task_id]["status"] = "failed"
                _running[task_id]["error"] = str(exc)
                logger.exception("Task %s exception", task_id)
                if _counter_tasks:
                    _counter_tasks.add(1, {"workspace": req.workspace, "model": req.model, "status": "failed"})
            finally:
                _running[task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                duration = time.monotonic() - t_start
                if _histogram_duration:
                    _histogram_duration.record(duration, {"workspace": req.workspace, "model": req.model})
                if image_path and image_path.exists():
                    image_path.unlink(missing_ok=True)


@app.post("/run", response_model=RunResponse)
async def run_task(req: RunRequest):
    """Accept a task and execute it asynchronously (fire-and-forget)."""
    workspace_dir = WORKSPACES_ROOT / req.workspace
    if not workspace_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Workspace not found: {req.workspace}")

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

    return RunResponse(
        status="accepted",
        task_id=task_id,
        message=f"Task queued: {req.workspace} (model={req.model})",
    )


@app.get("/status/{task_id}", response_model=TaskStatus)
async def get_status(task_id: str):
    """Check the status of a running or completed task."""
    if task_id not in _running:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatus(task_id=task_id, **_running[task_id])


@app.post("/restart-sessions")
async def restart_sessions():
    """Directly trigger Claude session restart on the host via SSH (bypasses container PID namespace)."""
    ssh_key = Path(os.path.expanduser("~/.ssh/id_ed25519_runner"))
    if not ssh_key.exists():
        raise HTTPException(status_code=500, detail=f"SSH key not found: {ssh_key}")
    cmd = (
        f"ssh -i {ssh_key} -o StrictHostKeyChecking=no -o BatchMode=yes "
        f"casper@127.0.0.1 'nohup ~/restart-claude-sessions.sh > /tmp/restart-claude.log 2>&1 &'"
    )
    await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("Claude session restart triggered via SSH")
    return {"status": "triggered", "log": "/tmp/restart-claude.log"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    running_count = sum(1 for t in _running.values() if t["status"] == "running")
    return {
        "status": "ok",
        "running_tasks": running_count,
        "max_concurrent": MAX_CONCURRENT,
    }


def main() -> None:
    port = int(os.getenv("PORT", "38095"))
    host = os.getenv("HOST", "0.0.0.0")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    setup_otel()
    logger.info("Claude Runner starting on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
