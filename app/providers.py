"""LLM provider registry — bring-your-own-key, multi-provider.

Supported providers:
  openai      -> ChatOpenAI          (paid; gpt-4o-mini, gpt-4o, ...)
  anthropic   -> ChatAnthropic       (paid; Claude Sonnet/Haiku/Opus)
  groq        -> ChatGroq            (free tier; open-source Llama models)
  openrouter  -> ChatOpenAI w/ base_url (paid OR free models, incl. NVIDIA Nemotron)

The OpenRouter catalog is fetched live from its public /models endpoint and
cached, so the "free models" list is always current — no hard-coded guesses.
"""

import logging
import time

import requests
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

logger = logging.getLogger("agent.providers")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"
CATALOG_CACHE_TTL = 600  # seconds

# Static catalogs for providers without a public unauthenticated models API.
STATIC_CATALOGS: dict[str, list[dict]] = {
    "openai": [
        {"id": "gpt-4o-mini", "name": "GPT-4o mini", "free": False},
        {"id": "gpt-4o", "name": "GPT-4o", "free": False},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 mini", "free": False},
        {"id": "gpt-4.1", "name": "GPT-4.1", "free": False},
        {"id": "o4-mini", "name": "o4-mini (reasoning)", "free": False},
    ],
    "anthropic": [
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "free": False},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "free": False},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "free": False},
    ],
    "groq": [
        {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B", "free": True},
        {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B Instant", "free": True},
        {"id": "openai/gpt-oss-120b", "name": "GPT-OSS 120B", "free": True},
        {"id": "qwen/qwen3-32b", "name": "Qwen3 32B", "free": True},
    ],
}

_openrouter_cache: dict = {"fetched_at": 0.0, "models": []}


def get_openrouter_models(free_only: bool = False) -> list[dict]:
    """Fetch (and cache) the live OpenRouter catalog; optionally only $0 models."""
    now = time.time()
    if now - _openrouter_cache["fetched_at"] > CATALOG_CACHE_TTL:
        try:
            response = requests.get(OPENROUTER_MODELS_URL, timeout=15)
            response.raise_for_status()
            models = []
            for m in response.json().get("data", []):
                pricing = m.get("pricing", {})
                models.append({
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "free": pricing.get("prompt") == "0" and pricing.get("completion") == "0",
                })
            models.sort(key=lambda m: (not m["free"], m["id"]))
            _openrouter_cache.update(fetched_at=now, models=models)
        except requests.RequestException as exc:
            logger.warning("OpenRouter catalog fetch failed (%s), using stale/fallback", exc)
            if not _openrouter_cache["models"]:
                _openrouter_cache["models"] = [
                    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 3 Super 120B", "free": True},
                    {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "name": "Nemotron 3 Ultra 550B", "free": True},
                ]
    models = _openrouter_cache["models"]
    return [m for m in models if m["free"]] if free_only else models


def get_catalog(free_only_openrouter: bool = False) -> dict[str, list[dict]]:
    return {**STATIC_CATALOGS, "openrouter": get_openrouter_models(free_only_openrouter)}


def build_chat_model(provider: str, model: str, api_key: str) -> BaseChatModel:
    """Construct the LangChain chat model for the user's provider + key.

    max_retries=2 gives every provider automatic retry-with-backoff on
    transient failures (429/5xx) via the underlying SDKs.
    """
    common = {"temperature": 0.3, "max_retries": 2, "timeout": 120}
    if provider == "openai":
        return ChatOpenAI(model=model, api_key=api_key, **common)
    if provider == "anthropic":
        return ChatAnthropic(model=model, api_key=api_key, max_tokens=8000, **common)
    if provider == "groq":
        return ChatGroq(model=model, api_key=api_key, **common)
    if provider == "openrouter":
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
            default_headers={"X-Title": "Autonomous Document Agent"},
            **common,
        )
    raise ValueError(f"Unknown provider: {provider}")
