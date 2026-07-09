# Docsmith — Autonomous Document Agent (v2, Deep Agents)

A production-style autonomous AI agent built on **LangChain Deep Agents**: you describe
a document in natural language, the agent **writes its own task plan**, executes it,
and delivers a polished **Microsoft Word (.docx)** file — through a polished animated
web UI (React + Tailwind + Framer Motion).

**Bring your own key, choose any model:**

| Provider   | Type        | Example models                                        |
|------------|-------------|-------------------------------------------------------|
| Groq       | Free tier   | Llama 3.3 70B, Llama 3.1 8B, GPT-OSS 120B             |
| OpenRouter | Free + paid | NVIDIA Nemotron 3 Super 120B / Ultra 550B (free), 300+ more |
| OpenAI     | Paid        | gpt-4o-mini, gpt-4o, gpt-4.1, o4-mini                 |
| Anthropic  | Paid        | Claude Sonnet 4.6, Claude Haiku 4.5, Claude Opus 4.8  |

The OpenRouter catalog is fetched **live** from its public API and filterable to
free models only — nothing is hard-coded.

## Architecture

```
Browser (React + Framer Motion UI)
   │  POST /agent {request, provider, model, api_key}   ← key used once, never stored
   │  GET  /api/jobs/{id}   (poll: live todos, events, result)
   ▼
FastAPI (app/main.py) ── app/jobs.py (background jobs, in-memory store)
   ▼
LangChain Deep Agent (app/agent.py)
   ├─ write_todos        ← built-in planning tool: the agent's own TODO list
   ├─ virtual filesystem ← built-in drafting space
   └─ save_word_document ← our tool → python-docx renderer (app/docgen.py)
   ▼
app/providers.py ── ChatOpenAI / ChatAnthropic / ChatGroq / ChatOpenAI(OpenRouter)
                    (max_retries=2 → automatic backoff on 429/5xx)
```

Key production behaviors:
- **BYOK security** — the API key travels with one request, builds the model client,
  and is never persisted or logged. Download endpoint blocks path traversal.
- **Async jobs** — agent runs take 15–90s, so the API returns a `job_id` instantly
  and the UI polls; the server stays responsive.
- **Live agent transparency** — the UI streams the agent's self-written TODO list
  (with per-task status), tool calls, assumptions, and timing.
- **Error handling & recovery** — invalid keys/models fail as a clean job error;
  malformed tool JSON is bounced back to the model to self-correct; if the agent
  forgets to call the docx tool, a recovery path builds the document from its answer.

## Run it

```bash
# backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# frontend (only needed once, or after UI changes)
cd frontend && npm install && npm run build && cd ..

# start
uvicorn app.main:app --port 8000
```

Open **http://localhost:8000** — pick a provider, paste your key, choose a model
(toggle "Free models only" for OpenRouter), and run one of the sample prompts.

For UI development with hot reload: `cd frontend && npm run dev` (proxies to :8000).

## API

```bash
curl -s http://localhost:8000/agent -H "Content-Type: application/json" -d '{
  "request": "Create a business proposal for an AI chatbot for a retail company.",
  "provider": "openrouter",
  "model": "nvidia/nemotron-3-super-120b-a12b:free",
  "api_key": "sk-or-..."
}'
# -> {"job_id": "...", "poll_url": "/api/jobs/..."}

curl -s http://localhost:8000/api/jobs/<job_id>          # todos, events, result
curl -s "http://localhost:8000/api/models?free_only=true" # live model catalog
curl -sO http://localhost:8000/download/<filename>.docx   # the document
```

## Project layout

| Path               | Responsibility                                              |
|--------------------|-------------------------------------------------------------|
| `app/main.py`      | FastAPI routes, validation, CORS, static UI serving         |
| `app/agent.py`     | Deep agent: system prompt, docx tool, stream → live events  |
| `app/providers.py` | Provider registry + live OpenRouter catalog (cached)        |
| `app/jobs.py`      | Background job execution + in-memory job store              |
| `app/docgen.py`    | Word document rendering (python-docx)                       |
| `frontend/`        | React + TypeScript + Tailwind v4 + Framer Motion UI         |

See **GUIDE.md** for the full interview guide (English + Hindi).
