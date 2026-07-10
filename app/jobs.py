"""In-memory job store + background execution.

Agent runs take 15-90s, so POST /api/agent returns a job id immediately and
the client polls GET /api/jobs/{id}. The job carries the live token stream
(`stream_text`) so the UI can show the document being written in real time,
and on failure an `error_type` plus — when anything was salvaged — a partial
result with a download URL. API keys are used to build the chat model and are
never stored on the job or written to logs.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.agent import AgentInterrupted, run_agent_job
from app.providers import build_chat_model

logger = logging.getLogger("agent.jobs")

MAX_JOBS_KEPT = 200
MAX_STREAM_CHARS = 200_000  # safety cap; a full document is ~10-30k chars

_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit", "tokens per minute", "quota")
_AUTH_MARKERS = ("401", "invalid api key", "invalid_api_key", "authentication", "unauthorized", "incorrect api key")


def classify_error(exc: Exception) -> str:
    """Map provider exceptions to a coarse type the UI can act on."""
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in text for marker in _AUTH_MARKERS):
        return "auth"
    return "unknown"


@dataclass
class Job:
    id: str
    request: str
    provider: str
    model: str
    status: str = "queued"  # queued | running | completed | failed
    todos: list = field(default_factory=list)
    events: list = field(default_factory=list)
    stream_text: str = ""
    result: dict | None = None
    error: str | None = None
    error_type: str | None = None  # rate_limit | auth | unknown
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.created_at
        return {
            "id": self.id, "request": self.request, "provider": self.provider,
            "model": self.model, "status": self.status, "todos": self.todos,
            "events": self.events, "stream_text": self.stream_text,
            "result": self.result, "error": self.error, "error_type": self.error_type,
            "elapsed_seconds": round(elapsed, 1),
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _prune() -> None:
    if len(_jobs) > MAX_JOBS_KEPT:
        for job_id in sorted(_jobs, key=lambda j: _jobs[j].created_at)[: len(_jobs) - MAX_JOBS_KEPT]:
            del _jobs[job_id]


def get_job(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def _run(job: Job, api_key: str) -> None:
    def on_event(kind: str, payload) -> None:
        with _lock:
            if kind == "todos":
                job.todos = payload
            elif kind == "stream":
                if len(job.stream_text) < MAX_STREAM_CHARS:
                    job.stream_text += str(payload)
            else:
                job.events.append({"at": round(time.time() - job.created_at, 1), "text": str(payload)})

    try:
        job.status = "running"
        on_event("event", f"Agent started on {job.provider} / {job.model}")
        chat_model = build_chat_model(job.provider, job.model, api_key)
        result = run_agent_job(chat_model, job.request, on_event)
        with _lock:
            job.result = {**result, "download_url": f"/download/{result['filename']}"}
            job.status = "completed"
    except AgentInterrupted as exc:  # provider died mid-run; maybe with a salvaged draft
        logger.warning("Job %s interrupted: %s", job.id, exc.original)
        with _lock:
            job.status = "failed"
            job.error = f"{type(exc.original).__name__}: {exc.original}"
            job.error_type = classify_error(exc.original)
            if exc.partial:
                job.result = {**exc.partial, "download_url": f"/download/{exc.partial['filename']}"}
    except Exception as exc:  # surface a clean message; never the API key
        logger.exception("Job %s failed", job.id)
        with _lock:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.error_type = classify_error(exc)
    finally:
        job.finished_at = time.time()


def start_job(request: str, provider: str, model: str, api_key: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], request=request, provider=provider, model=model)
    with _lock:
        _jobs[job.id] = job
        _prune()
    threading.Thread(target=_run, args=(job, api_key), daemon=True).start()
    return job
