"""Deep-agent runner built on LangChain's `deepagents` framework.

The framework supplies the agent harness: a planning tool (write_todos) the
model uses to create and update its own TODO list, a virtual filesystem for
drafting, and the tool-calling loop. We supply:
  - the LLM (any provider the user selected, via app.providers)
  - a `save_word_document` tool that renders the final .docx (app.docgen)
  - a system prompt that defines the document-writing workflow

`run_agent_job` streams the graph and forwards every todo-list update and
tool call to a callback so the API/UI can show live progress.
"""

import json
import logging
import re
from typing import Callable

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
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


def run_agent_job(
    chat_model: BaseChatModel,
    request: str,
    on_event: Callable[[str, object], None],
) -> dict:
    """Run the deep agent to completion. Emits ("todos"|"event", payload) via on_event."""
    sink: dict = {}
    agent = create_deep_agent(
        model=chat_model,
        tools=[_make_save_tool(sink)],
        system_prompt=SYSTEM_PROMPT,
    )

    last_todos_repr = ""
    final_text = ""
    for state in agent.stream(
        {"messages": [{"role": "user", "content": request}]},
        config={"recursion_limit": RECURSION_LIMIT},
        stream_mode="values",
    ):
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
    }
