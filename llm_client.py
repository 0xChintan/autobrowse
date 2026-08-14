"""LLM client: Groq (llama-3.3-70b-versatile) with a local Ollama fallback.

Provider selection happens once, at construction:
  * GROQ_API_KEY set  -> Groq
  * otherwise         -> Ollama at OLLAMA_HOST (default http://localhost:11434)

Both paths ask for JSON mode and go through the same lenient parser, because
small models still occasionally wrap JSON in prose or code fences.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self) -> None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.provider = "groq" if api_key else "ollama"
        self.model = GROQ_MODEL if api_key else OLLAMA_MODEL
        self._groq = None
        self._json_mode = True

        if self.provider == "groq":
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
                    raise LLMError(f"Groq request failed: {exc2}") from exc2
            else:
                raise LLMError(f"Groq request failed: {exc}") from exc
        return resp.choices[0].message.content or ""

    async def _call_ollama(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_predict": max_tokens},
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
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{OLLAMA_HOST}/api/tags")
                resp.raise_for_status()
                names = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception:
            return False, f"Ollama not reachable at {OLLAMA_HOST} (and no GROQ_API_KEY set)"
        if not any(n.split(":")[0] == self.model.split(":")[0] for n in names):
            return False, f"Ollama is up but '{self.model}' is not pulled — run: ollama pull {self.model}"
        return True, self.description


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
