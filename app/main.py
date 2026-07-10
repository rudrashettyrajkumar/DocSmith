"""FastAPI entry point — Autonomous Document Agent (Deep Agents edition).

POST /agent                  {"request", "provider", "model", "api_key"} -> {job_id}
GET  /api/jobs/{job_id}      poll live todos/events/result
GET  /api/models             provider -> model catalog (OpenRouter fetched live)
GET  /download/{filename}    the generated .docx
GET  /                       the web UI (built React app)
"""

import logging
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import jobs
from app.docgen import OUTPUT_DIR
from app.providers import STATIC_CATALOGS, get_catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(
    title="Autonomous Document Agent",
    description="LangChain Deep Agent that plans, executes, and delivers Word documents. Bring your own LLM key.",
    version="2.0.0",
)

app.add_middleware(  # open CORS: no cookies/session state, and the frontend may be on a separate origin (e.g. Vercel)
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

PROVIDERS = (*STATIC_CATALOGS.keys(), "openrouter")


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=10, max_length=4000)
    provider: Literal["openai", "anthropic", "groq", "openrouter"]
    model: str = Field(..., min_length=2, max_length=120)
    api_key: str = Field(..., min_length=8, max_length=300, description="Your key — used for this run only, never stored")

    @field_validator("request", "model", "api_key")
    @classmethod
    def strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


@app.post("/agent")
def create_agent_job(body: AgentRequest) -> dict:
    job = jobs.start_job(body.request, body.provider, body.model, body.api_key)
    return {"job_id": job.id, "status": job.status, "poll_url": f"/api/jobs/{job.id}"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/models")
def list_models(free_only: bool = False) -> dict:
    return {"providers": get_catalog(free_only_openrouter=free_only)}


@app.get("/download/{filename}")
def download(filename: str) -> FileResponse:
    path = (OUTPUT_DIR / filename).resolve()
    if path.parent != OUTPUT_DIR.resolve() or not path.is_file():  # blocks ../ traversal
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Serve the built frontend (frontend/dist) at / — API routes above take priority.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="ui")
