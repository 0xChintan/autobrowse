"""LLM client: Groq (llama-3.3-70b-versatile) and local Ollama, side by side.

Both backends are always available to the app; which one a run uses is chosen
per run — from the UI dropdown, or by falling back to whatever is configured:

  * GROQ_API_KEY set  -> Groq
  * otherwise         -> Ollama at OLLAMA_HOST (default http://localhost:11434)

Both paths ask for JSON mode and go through the same lenient parser, because
small models still occasionally wrap JSON in prose or code fences.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

# Ollama sizes its default context from available VRAM (2048 on older builds,
# 4096 on a small Apple Silicon box) and silently truncates the *front* of an
# over-long prompt — the system prompt and the task — leaving the model to
# invent actions. So ask explicitly. 4096 comfortably fits a trimmed
# observation plus generation, and unlike 8192 it still leaves room for an 8B
# model's weights on a 5-6 GB VRAM budget. Raise it if you have the memory.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))

# Shown if the live Groq catalog can't be fetched. Cheapest first is deliberate:
# free-tier rate limits are per-model, so a smaller model is the escape hatch
# when the big one hits its daily token cap.
GROQ_STATIC_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# Groq hosts speech and moderation models too; they can't drive the agent loop.
_NON_CHAT = re.compile(r"whisper|tts|guard|embed", re.I)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()

        if provider not in ("groq", "ollama"):
            provider = "groq" if api_key else "ollama"
        self.provider = provider
        self.model = model or (GROQ_MODEL if provider == "groq" else OLLAMA_MODEL)
        self._groq = None
        self._json_mode = True

        if self.provider == "groq":
            if not api_key:
                raise LLMError("Groq was selected but GROQ_API_KEY is not set.")
            try:
                from groq import AsyncGroq
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "GROQ_API_KEY is set but the groq package is missing — pip install groq"
                ) from exc
            self._groq = AsyncGroq(api_key=api_key, max_retries=2)

    @property
    def description(self) -> str:
        if self.provider == "groq":
            return f"Groq · {self.model}"
        return f"Ollama · {self.model} ({OLLAMA_HOST})"

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 700,
    ) -> dict[str, Any]:
        """Ask for one JSON object and return it parsed."""
        raw = (
            await self._call_groq(system, user, temperature, max_tokens)
            if self.provider == "groq"
            else await self._call_ollama(system, user, temperature, max_tokens)
        )
        parsed = extract_json(raw)
        if parsed is None:
            raise LLMError(f"model did not return usable JSON: {raw[:300]!r}")
        return parsed

    async def _call_groq(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        assert self._groq is not None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await self._groq.chat.completions.create(**kwargs)
        except Exception as exc:
            # Not every Groq-hosted model accepts JSON mode. Drop it once and
            # lean on the parser instead of failing the whole run.
            if self._json_mode and "response_format" in str(exc).lower():
                self._json_mode = False
                kwargs.pop("response_format", None)
                try:
                    resp = await self._groq.chat.completions.create(**kwargs)
                except Exception as exc2:
                    raise LLMError(self._groq_error(exc2)) from exc2
            else:
                raise LLMError(self._groq_error(exc)) from exc
        return resp.choices[0].message.content or ""

    def _groq_error(self, exc: Exception) -> str:
        """Turn Groq's raw error JSON into something worth reading."""
        text = str(exc)
        if "rate_limit_exceeded" not in text and "429" not in text:
            return f"Groq request failed: {text}"

        retry = re.search(r"try again in ([\dhms.]+)", text)
        limit = re.search(r"Limit (\d+), Used (\d+)", text)
        msg = f"Groq rate limit hit for '{self.model}'"
        if limit:
            msg += f" ({int(limit.group(2)):,} of {int(limit.group(1)):,} daily tokens used)"
        if retry:
            msg += f". Resets in {retry.group(1).rstrip('.')}"
        return (
            msg + ". Limits are per model — pick a smaller model (e.g. "
            "llama-3.1-8b-instant) or switch to Ollama in the dropdown and run again."
        )

    async def _call_ollama(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": OLLAMA_NUM_CTX,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.ConnectError as exc:
            raise LLMError(
                f"No GROQ_API_KEY set and Ollama is not reachable at {OLLAMA_HOST}. "
                "Either export GROQ_API_KEY=... or run `ollama serve` with llama3 pulled."
            ) from exc
        except Exception as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        return (data.get("message") or {}).get("content", "")

    async def healthcheck(self) -> tuple[bool, str]:
        """Cheap reachability probe used by the frontend banner."""
        if self.provider == "groq":
            return True, self.description
        models = await list_ollama_models()
        if models is None:
            return False, f"Ollama not reachable at {OLLAMA_HOST}"
        if not any(n.split(":")[0] == self.model.split(":")[0] for n in models):
            return False, f"Ollama is up but '{self.model}' is not pulled — run: ollama pull {self.model}"
        return True, self.description


# --------------------------------------------------------------------------- #
# Discovery — what the UI offers in its model picker
# --------------------------------------------------------------------------- #


async def list_groq_models() -> list[str] | None:
    """Live Groq catalog, or None if the key is missing / the call fails."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=api_key, max_retries=0, timeout=8.0)
        resp = await client.models.list()
        names = [m.id for m in resp.data if m.id and not _NON_CHAT.search(m.id)]
    except Exception:
        return list(GROQ_STATIC_MODELS)  # key exists; just couldn't enumerate
    return sorted(names) or list(GROQ_STATIC_MODELS)


async def list_ollama_models() -> list[str] | None:
    """Installed Ollama models, or None if Ollama isn't running."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
            return sorted(m.get("name", "") for m in resp.json().get("models", []))
    except Exception:
        return None


async def discover_providers() -> dict[str, Any]:
    """Both backends probed in parallel, shaped for the frontend dropdown."""
    groq_models, ollama_models = await asyncio.gather(
        list_groq_models(), list_ollama_models()
    )

    providers = [
        {
            "id": "groq",
            "label": "Groq",
            "available": groq_models is not None,
            "models": groq_models or [],
            "preferred": GROQ_MODEL,
            "reason": None if groq_models is not None else "GROQ_API_KEY is not set",
        },
        {
            "id": "ollama",
            "label": "Ollama (local)",
            "available": bool(ollama_models),
            "models": ollama_models or [],
            "preferred": OLLAMA_MODEL,
            "reason": (
                None
                if ollama_models
                else (
                    f"no models pulled — try: ollama pull {OLLAMA_MODEL}"
                    if ollama_models == []
                    else f"not reachable at {OLLAMA_HOST}"
                )
            ),
        },
    ]

    default = None
    for p in providers:
        if not p["available"]:
            continue
        model = p["preferred"] if p["preferred"] in p["models"] else p["models"][0]
        default = {"provider": p["id"], "model": model}
        break

    return {"providers": providers, "default": default}


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    text = text.strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(text)
    if fenced:
        try:
            obj = json.loads(fenced.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Scan for the first balanced {...}, ignoring braces inside strings.
    start = text.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
