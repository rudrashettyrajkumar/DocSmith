"""Deep-agent runner built on LangChain's `deepagents` framework.

The framework supplies the agent harness: a planning tool (write_todos) the
model uses to create and update its own TODO list, a virtual filesystem for
drafting, and the tool-calling loop. We supply:
  - the LLM (any provider the user selected, via app.providers)
  - a `save_word_document` tool that renders the final .docx (app.docgen)
  - a system prompt that defines the document-writing workflow

`run_agent_job` streams the graph and forwards every todo-list update, tool
call AND raw LLM token to a callback so the API/UI can show the document being
written live. If the provider dies mid-run (free-tier 429s are the classic
case) we salvage whatever was generated into a partial .docx and raise
`AgentInterrupted` so the caller can offer it for download.
"""

import json
import logging
import re
from typing import Callable

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import tool

from app import docgen

logger = logging.getLogger("agent.core")

RECURSION_LIMIT = 60

SYSTEM_PROMPT = """You are an autonomous business-document agent. Given a natural
language request, you decide what document is needed, plan your own tasks, execute
them, and deliver a polished Microsoft Word file.

Workflow you must follow:
1. Use the write_todos tool FIRST to create your task plan, and keep statuses
   updated as you work (in_progress / completed).
2. Decide the document type (proposal, meeting minutes, project plan, business
   report, technical design, SOP, product specification, ...), the audience,
   and any assumptions. If the request is vague, missing information, or has
   conflicting requirements, make sensible assumptions instead of asking.
3. Compose the full document content: 4-7 well-written sections with professional
   business prose. Invent realistic mock facts and figures where needed.
4. Call save_word_document EXACTLY ONCE with the complete document as JSON.
5. Finish with a short reply summarising the document you created and the key
   assumptions you made. Do not paste the whole document into the reply.
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_DOC_JSON_PREFIX = re.compile(r'^\s*\{\s*"document_json"\s*:\s*"')

SAVE_TOOL_NAME = "save_word_document"


class AgentInterrupted(Exception):
    """The agent run died mid-generation (rate limit, network, ...).

    `partial` carries a result dict for the salvaged partial .docx, or None if
    nothing usable was generated before the failure.
    """

    def __init__(self, original: Exception, partial: dict | None):
        super().__init__(str(original))
        self.original = original
        self.partial = partial


def _parse_document_json(document_json: str) -> dict:
    """Parse the tool argument leniently — small models love markdown fences."""
    text = document_json.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def _make_save_tool(sink: dict) -> Callable:
    """Create the docx tool bound to this job (writes result into `sink`)."""

    @tool
    def save_word_document(document_json: str) -> str:
        """Render the final Word (.docx) document. Call exactly once, when the
        full content is ready.

        `document_json` must be a JSON object string with this shape:
        {
          "title": "...",
          "document_type": "...",
          "audience": "...",
          "assumptions": ["..."],
          "sections": [{"heading": "...", "paragraphs": ["..."], "bullets": ["..."]}],
          "facts": ["optional key data points shown in an appendix table"]
        }
        """
        try:
            doc = _parse_document_json(document_json)
        except (json.JSONDecodeError, ValueError) as exc:
            return f"ERROR: invalid JSON ({exc}). Fix the JSON and call this tool again."

        if not doc.get("title") or not doc.get("sections"):
            return "ERROR: 'title' and a non-empty 'sections' list are required. Call again with both."

        doc.setdefault("document_type", "Business Document")
        doc.setdefault("audience", "Business stakeholders")
        doc.setdefault("assumptions", [])
        sink["document"] = doc
        sink["filename"] = docgen.build_docx(doc)
        return f"Document saved as {sink['filename']}. Now write your short final summary."

    return save_word_document


# --------------------------------------------------------------- streaming


def _chunk_text(chunk: AIMessageChunk) -> str:
    """Plain-text delta of a message chunk (handles str and block content)."""
    content = chunk.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class _StreamCollector:
    """Accumulates token deltas across the run for live display + recovery.

    We forward the model's prose AND the arguments it streams into the
    save_word_document tool call (that is where the actual document text is
    generated), but skip write_todos / other tool args — they are plumbing.
    """

    def __init__(self, on_delta: Callable[[str], None]):
        self._on_delta = on_delta
        self._tool_names: dict[int, str] = {}  # tool_call index -> name (this message)
        self._message_id: str | None = None
        self.draft = ""            # everything shown to the user
        self.doc_args = ""         # raw save_word_document args JSON (for recovery)

    def feed(self, chunk: AIMessageChunk) -> None:
        if chunk.id and chunk.id != self._message_id:  # new model turn
            self._message_id = chunk.id
            self._tool_names = {}
            if self.draft and not self.draft.endswith("\n\n"):
                self._emit("\n\n")

        delta = _chunk_text(chunk)
        if delta:
            self._emit(delta)

        for tc in chunk.tool_call_chunks or []:
            index = tc.get("index") or 0
            if tc.get("name"):
                self._tool_names[index] = tc["name"]
            args = tc.get("args")
            if args and self._tool_names.get(index) == SAVE_TOOL_NAME:
                self.doc_args += args
                self._emit(args)

    def _emit(self, delta: str) -> None:
        self.draft += delta
        self._on_delta(delta)


# ------------------------------------------------------- partial recovery


def _close_truncated_json(text: str) -> str:
    """Append whatever closers a truncated JSON document needs to parse."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if escaped:  # cut off mid-escape — drop the dangling backslash
        text = text[:-1]
    return text + ('"' if in_string else "") + "".join(reversed(stack))


def _clean_draft_text(raw: str) -> str:
    """Make the raw token stream readable: drop the tool-call JSON envelope
    and undo the most common string escapes."""
    text = _DOC_JSON_PREFIX.sub("", raw.strip())
    return (
        text.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "  ")
        .replace("\\\\", "\\")
    )


def _try_parse_partial_doc(doc_args: str) -> dict | None:
    """Best effort: repair the truncated save_word_document arguments into a
    structured document. Two layers: the outer tool-args JSON, then the
    document JSON string inside it."""
    if not doc_args.strip():
        return None
    try:
        outer = json.loads(_close_truncated_json(doc_args))
        inner = outer.get("document_json", "") if isinstance(outer, dict) else ""
        if not isinstance(inner, str) or not inner.strip():
            return None
        doc = _parse_document_json(_close_truncated_json(inner))
        return doc if isinstance(doc, dict) and doc.get("title") and doc.get("sections") else None
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def _recover_partial_document(request: str, collector: _StreamCollector, reason: str) -> dict | None:
    """Build a partial .docx from whatever the model generated before dying."""
    note = (
        "Generation was interrupted by the model provider "
        f"({reason}). This document is a partial draft recovered from the live stream."
    )
    doc = _try_parse_partial_doc(collector.doc_args)
    if doc is None:
        text = _clean_draft_text(collector.draft)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            return None
        doc = {
            "title": request.strip().capitalize()[:80],
            "sections": [{"heading": "Recovered draft (incomplete)", "paragraphs": paragraphs, "bullets": []}],
        }
    doc.setdefault("document_type", "Business Document")
    doc.setdefault("audience", "Business stakeholders")
    doc.setdefault("assumptions", [])
    doc["assumptions"] = [note, *doc["assumptions"]]
    try:
        filename = docgen.build_docx(doc)
    except Exception:  # never let recovery mask the real error
        logger.exception("Failed to build partial document")
        return None
    return {
        "filename": filename,
        "title": doc["title"],
        "document_type": doc["document_type"],
        "assumptions": doc["assumptions"],
        "summary": "The run was interrupted before the document was finished — "
                   "this file contains everything generated up to that point.",
        "partial": True,
    }


# --------------------------------------------------------------- main run


def run_agent_job(
    chat_model: BaseChatModel,
    request: str,
    on_event: Callable[[str, object], None],
) -> dict:
    """Run the deep agent to completion.

    Emits ("todos"|"event"|"stream", payload) via on_event. Raises
    AgentInterrupted (carrying a partial result when possible) if the
    provider fails mid-run.
    """
    sink: dict = {}
    agent = create_deep_agent(
        model=chat_model,
        tools=[_make_save_tool(sink)],
        system_prompt=SYSTEM_PROMPT,
    )

    collector = _StreamCollector(lambda delta: on_event("stream", delta))
    last_todos_repr = ""
    final_text = ""
    try:
        for mode, payload in agent.stream(
            {"messages": [{"role": "user", "content": request}]},
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode=["values", "messages"],
        ):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, AIMessageChunk):
                    collector.feed(chunk)
                continue

            state = payload
            todos = state.get("todos") or []
            todos_repr = repr(todos)
            if todos and todos_repr != last_todos_repr:
                last_todos_repr = todos_repr
                on_event("todos", [{"content": t["content"], "status": t["status"]} for t in todos])

            message = state["messages"][-1] if state.get("messages") else None
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    if call["name"] != "write_todos":  # todo updates already shown above
                        on_event("event", f"Calling tool: {call['name']}")
            elif isinstance(message, ToolMessage) and str(message.content).startswith("ERROR"):
                on_event("event", f"Tool error, agent will retry: {str(message.content)[:120]}")
            elif isinstance(message, AIMessage) and not message.tool_calls and message.content:
                content = message.content
                final_text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
    except Exception as exc:
        if "filename" in sink:
            # The document itself was already saved — only the final summary
            # died. Deliver the run as a success rather than failing it.
            logger.warning("Run failed after document was saved (%s) — completing anyway", exc)
            on_event("event", "Provider failed after the document was saved — delivering it")
        else:
            logger.exception("Agent run interrupted, attempting partial recovery")
            on_event("event", f"Run interrupted: {type(exc).__name__} — recovering partial document")
            partial = _recover_partial_document(request, collector, type(exc).__name__)
            if partial:
                on_event("event", f"Partial document saved as {partial['filename']}")
            raise AgentInterrupted(exc, partial) from exc

    # Recovery: agent produced prose but never called the docx tool.
    if "filename" not in sink:
        logger.warning("Agent finished without calling save_word_document — recovering")
        on_event("event", "Agent skipped the document tool — building document from its answer")
        fallback = {
            "title": request.strip().capitalize()[:80],
            "document_type": "Business Document",
            "audience": "Business stakeholders",
            "assumptions": ["Document assembled from the agent's final answer (recovery path)."],
            "sections": [{"heading": "Content", "paragraphs": [p for p in final_text.split("\n\n") if p.strip()] or [final_text or "No content produced."], "bullets": []}],
        }
        sink["document"] = fallback
        sink["filename"] = docgen.build_docx(fallback)

    return {
        "filename": sink["filename"],
        "title": sink["document"]["title"],
        "document_type": sink["document"]["document_type"],
        "assumptions": sink["document"].get("assumptions", []),
        "summary": final_text or f"Created '{sink['document']['title']}'.",
        "partial": False,
    }
