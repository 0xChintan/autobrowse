"""AutoBrowse — FastAPI server hosting the observe → think → act agent loop.

Run:  uvicorn main:app --reload   then open http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from browser_controller import ActionError, BrowserController, Observation
from llm_client import LLMClient, LLMError, discover_providers

try:  # optional convenience; not required
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
MAX_STEPS = int(os.getenv("AUTOBROWSE_MAX_STEPS", "15"))
CONFIRM_TIMEOUT_S = float(os.getenv("AUTOBROWSE_CONFIRM_TIMEOUT", "180"))
HISTORY_IN_PROMPT = int(os.getenv("AUTOBROWSE_HISTORY", "5"))

VALID_ACTIONS = {"click", "type", "scroll", "navigate", "wait", "done"}

# Safety layer: an action whose target label or typed value hits one of these
# gets held for human approval before it touches the page.
RISKY_KEYWORDS = [
    "buy", "buy now", "purchase", "pay", "payment", "pay now", "checkout",
    "check out", "place order", "place your order", "submit order", "order now",
    "complete purchase", "confirm order", "confirm", "delete", "remove account",
    "deactivate", "cancel subscription", "subscribe", "sign up", "send money",
    "transfer", "withdraw", "bid", "add to cart",
]


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = f"""You are AutoBrowse, an agent that completes tasks by driving a real web browser.

Each turn you receive: the task, the current page URL, a numbered list of the
interactive elements on that page, a snapshot of the visible page text, and the
history of what you have already done.

Reply with EXACTLY ONE JSON object and nothing else. Schema:

{{
  "action": "click" | "type" | "scroll" | "navigate" | "wait" | "done",
  "target_id": <integer id from the element list, or null>,
  "value": <string, or null>,
  "reasoning": <one short sentence explaining this step>
}}

Action rules:
- "click"    — target_id required. value null.
- "type"     — target_id required, value is the text to type. End the value with
               a newline character (\\n) to press Enter afterwards; that is how
               you submit a search box. Prefer this over hunting for a button.
- "scroll"   — value "down", "up", "top", "bottom", or a pixel count. target_id null.
- "navigate" — value is a full URL. target_id null.
- "wait"     — value is seconds (e.g. "2"). Use only when the page is clearly loading.
- "done"     — value is the FINAL ANSWER to the task, written for the user.
               target_id null.

Hard rules:
1. Only ever use a target_id that appears in the element list of the CURRENT page.
   Ids are renumbered every step — never reuse an id from an earlier step.
2. One action per reply. Never explain outside the JSON.
3. Read the page text before answering. If the information the task asks for is
   already visible, emit "done" with the actual answer, not a description of it.
4. The answer in "done" must contain the real extracted content (titles, prices,
   names) — never "I found the results" or "see above".
5. Dismiss cookie banners or consent dialogs if they block the content.
6. Ignore sponsored blocks — anything marked "Ad", "AD", or "Sponsored" is not a
   real result. Report the first genuine organic results instead.
7. You have at most {MAX_STEPS} steps. Be direct: navigate, search, read, answer.
8. If something failed twice, do not repeat it — change approach (different
   element, scroll first, or navigate straight to a URL).
"""


def build_user_prompt(
    task: str,
    obs: Observation,
    history: list["Step"],
    warnings: list[str],
    step_no: int,
) -> str:
    parts = [
        f"TASK: {task}",
        "",
        f"STEP {step_no} of {MAX_STEPS}",
        "",
        "CURRENT PAGE",
        f"  url:   {obs.url}",
        f"  title: {obs.title}",
        f"  view:  {obs.scroll_note()}",
        "",
        "INTERACTIVE ELEMENTS",
        obs.element_lines(),
        "",
        "VISIBLE PAGE TEXT",
        obs.text or "(empty)",
        "",
    ]

    if history:
        parts.append("HISTORY (most recent last)")
        for step in history[-HISTORY_IN_PROMPT:]:
            mark = "ok" if step.ok else "FAILED"
            desc = describe(step.action, step.target_id, step.value)
            parts.append(f"  {step.n}. {desc} -> [{mark}] {step.result}")
        parts.append("")

    if warnings:
        parts.append("IMPORTANT")
        parts.extend(f"  - {w}" for w in warnings)
        parts.append("")

    parts.append("Respond with the single JSON action object for this step.")
    return "\n".join(parts)


def describe(action: str, target_id: int | None, value: str | None) -> str:
    bits = [action]
    if target_id is not None:
        bits.append(f"[{target_id}]")
    if value:
        shown = value.replace("\n", "\\n")
        bits.append(f'"{shown[:60]}"')
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    n: int
    action: str
    target_id: int | None
    value: str | None
    reasoning: str
    result: str
    ok: bool


@dataclass
class Run:
    id: str
    task: str
    url: str
    events: list[dict[str, Any]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    finished: bool = False
    answer: str | None = None
    new_event: asyncio.Event = field(default_factory=asyncio.Event)
    confirm_gate: asyncio.Event | None = None
    confirm_approved: bool = False
    task_handle: asyncio.Task | None = None

    def emit(self, kind: str, **payload: Any) -> None:
        event = {"type": kind, **payload}
        self.events.append(event)
        self.new_event.set()
        # Mirror to the terminal so the server log tells the same story.
        if kind == "step":
            flag = "ok " if payload.get("ok") else "ERR"
            print(f"[{self.id[:6]}] {payload['n']:>2}. {flag} {payload['description']} :: {payload['result']}")
        elif kind in ("status", "confirm", "error", "done"):
            print(f"[{self.id[:6]}] {kind.upper()}: {payload.get('message') or payload.get('answer') or ''}")


RUNS: dict[str, Run] = {}


# --------------------------------------------------------------------------- #
# Safety layer
# --------------------------------------------------------------------------- #


def risk_check(action: str, value: str | None, element_label: str) -> str | None:
    """Return the matched keyword if this action needs human approval."""
    if action in ("scroll", "wait", "done"):
        return None
    haystack = f"{value or ''} {element_label}".lower()
    for keyword in RISKY_KEYWORDS:
        if keyword in haystack:
            return keyword
    return None


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #


async def run_agent(run: Run, llm: LLMClient) -> None:
    browser = BrowserController(start_url=run.url)
    failures: Counter[str] = Counter()
    blocked: set[str] = set()

    try:
        run.emit("status", message=f"Launching Chromium · thinking with {llm.description}")
        await browser.start()
        if browser.start_error:
            # Not fatal: the agent still gets a turn and can navigate itself.
            run.emit("status", message=f"Start URL failed — {browser.start_error}")
        else:
            run.emit("status", message=f"Opened {run.url}")

        for step_no in range(1, MAX_STEPS + 1):
            obs = await browser.observe()

            warnings: list[str] = []
            # A failed load leaves Chromium on an opaque error page. Say so
            # plainly, or the model tries to interact with an empty document.
            if obs.url.startswith("chrome-error://") or (step_no == 1 and browser.start_error):
                warnings.append(
                    "The current page FAILED TO LOAD. There is nothing here to click. "
                    "Use the 'navigate' action to open a working URL before anything else."
                )
            for sig in sorted(blocked):
                warnings.append(
                    f"'{sig}' has already failed twice and is now blocked. "
                    "You MUST try a genuinely different approach."
                )
            if step_no == MAX_STEPS:
                warnings.append(
                    "This is your LAST step. Emit action 'done' with the best answer "
                    "you can give from what you have seen."
                )

            run.emit(
                "thinking",
                n=step_no,
                url=obs.url,
                title=obs.title,
                elements=len(obs.elements),
            )

            prompt = build_user_prompt(run.task, obs, run.steps, warnings, step_no)
            try:
                decision = await llm.complete_json(SYSTEM_PROMPT, prompt)
            except LLMError as exc:
                run.emit("error", message=str(exc))
                return

            action, target_id, value, reasoning, problem = parse_decision(decision)
            if problem:
                record(run, step_no, action, target_id, value, reasoning, problem, ok=False)
                failures[describe(action, target_id, value)] += 1
                continue

            # ---- done ----------------------------------------------------- #
            if action == "done":
                answer = (value or "").strip() or reasoning or "Task reported complete."
                run.answer = answer
                record(run, step_no, "done", None, answer, reasoning, "task complete", ok=True)
                run.emit("done", answer=answer, steps=len(run.steps))
                return

            signature = describe(action, target_id, value)
            label = obs.label_for(target_id)

            # ---- memory: refuse a third attempt at a twice-failed action --- #
            if signature in blocked:
                msg = "blocked by memory: this exact action already failed twice — pick a different approach"
                record(run, step_no, action, target_id, value, reasoning, msg, ok=False)
                continue

            # ---- safety layer --------------------------------------------- #
            keyword = risk_check(action, value, label)
            if keyword:
                approved = await request_confirmation(run, step_no, signature, label, keyword, reasoning)
                if not approved:
                    run.emit(
                        "step",
                        n=step_no,
                        action=action,
                        target_id=target_id,
                        value=value,
                        reasoning=reasoning,
                        description=signature,
                        result=f"denied by user (risky: '{keyword}')",
                        ok=False,
                    )
                    run.emit("error", message="Run stopped: you denied a risky action.")
                    return

            # ---- execute --------------------------------------------------- #
            try:
                result = await browser.execute(action, target_id, value)
                ok = True
            except ActionError as exc:
                result, ok = str(exc), False
            except Exception as exc:  # keep the loop alive on unexpected faults
                result, ok = f"unexpected error: {exc.__class__.__name__}: {exc}", False

            record(run, step_no, action, target_id, value, reasoning, result, ok)

            if not ok:
                failures[signature] += 1
                if failures[signature] >= 2 and signature not in blocked:
                    blocked.add(signature)
                    run.emit(
                        "status",
                        message=f"Memory: '{signature}' failed twice — forcing a different approach.",
                    )

        run.emit(
            "error",
            message=f"Hit the {MAX_STEPS}-step safety limit without finishing the task.",
        )

    except asyncio.CancelledError:
        run.emit("error", message="Run cancelled.")
        raise
    except Exception as exc:
        run.emit("error", message=f"{exc.__class__.__name__}: {exc}")
    finally:
        await browser.close()
        run.finished = True
        run.new_event.set()


def parse_decision(
    decision: dict[str, Any],
) -> tuple[str, int | None, str | None, str, str | None]:
    """Validate the LLM's JSON. Returns (action, target_id, value, reasoning, problem)."""
    action = str(decision.get("action", "")).strip().lower()
    reasoning = str(decision.get("reasoning") or "").strip()

    raw_target = decision.get("target_id")
    target_id: int | None = None
    if isinstance(raw_target, bool):
        raw_target = None
    if isinstance(raw_target, (int, float)):
        target_id = int(raw_target)
    elif isinstance(raw_target, str) and raw_target.strip().lstrip("[").rstrip("]").isdigit():
        target_id = int(raw_target.strip().lstrip("[").rstrip("]"))

    raw_value = decision.get("value")
    value = None if raw_value is None else str(raw_value)

    if action not in VALID_ACTIONS:
        return action or "invalid", target_id, value, reasoning, (
            f"invalid action '{action}' — must be one of {sorted(VALID_ACTIONS)}"
        )
    if action in ("click", "type") and target_id is None:
        return action, target_id, value, reasoning, f"action '{action}' requires a target_id"
    if action == "type" and not value:
        return action, target_id, value, reasoning, "action 'type' requires a value"
    if action == "navigate" and not value:
        return action, target_id, value, reasoning, "action 'navigate' requires a URL in value"
    return action, target_id, value, reasoning, None


def record(
    run: Run,
    n: int,
    action: str,
    target_id: int | None,
    value: str | None,
    reasoning: str,
    result: str,
    ok: bool,
) -> None:
    run.steps.append(Step(n, action, target_id, value, reasoning, result, ok))
    run.emit(
        "step",
        n=n,
        action=action,
        target_id=target_id,
        value=value,
        reasoning=reasoning,
        description=describe(action, target_id, value),
        result=result,
        ok=ok,
    )


async def request_confirmation(
    run: Run, step_no: int, signature: str, label: str, keyword: str, reasoning: str
) -> bool:
    """Pause the run and wait for the user to approve or deny in the UI."""
    run.confirm_gate = asyncio.Event()
    run.confirm_approved = False
    run.emit(
        "confirm",
        n=step_no,
        description=signature,
        element=label,
        keyword=keyword,
        reasoning=reasoning,
        message=f"Risky action held for approval (matched '{keyword}'): {signature} {label}".strip(),
    )
    print(f"\n[SAFETY] Step {step_no} wants to: {signature} {label}")
    print(f"[SAFETY] Matched keyword '{keyword}'. Approve or deny in the AutoBrowse UI.\n")
    try:
        await asyncio.wait_for(run.confirm_gate.wait(), timeout=CONFIRM_TIMEOUT_S)
    except asyncio.TimeoutError:
        run.emit("status", message="Confirmation timed out — treating as denied.")
        return False
    finally:
        run.confirm_gate = None
    return run.confirm_approved


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #

app = FastAPI(title="AutoBrowse")
llm_client = LLMClient()

if (BASE_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class RunRequest(BaseModel):
    task: str = Field(min_length=1)
    url: str = "https://duckduckgo.com"
    provider: str | None = None  # "groq" | "ollama"; None -> auto-detect
    model: str | None = None


class ConfirmRequest(BaseModel):
    approved: bool


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ok, message = await llm_client.healthcheck()
    return {"ok": ok, "provider": llm_client.provider, "message": message, "max_steps": MAX_STEPS}


@app.get("/api/models")
async def models() -> dict[str, Any]:
    """Everything the UI needs to render its provider/model picker."""
    info = await discover_providers()
    info["max_steps"] = MAX_STEPS
    return info


@app.post("/api/run")
async def start_run(req: RunRequest) -> dict[str, str]:
    # A run gets its own client so the picked model applies to that run only.
    try:
        client = (
            LLMClient(provider=req.provider, model=req.model)
            if (req.provider or req.model)
            else llm_client
        )
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    run = Run(
        id=uuid.uuid4().hex,
        task=req.task.strip(),
        url=req.url.strip() or "https://duckduckgo.com",
    )
    RUNS[run.id] = run
    run.task_handle = asyncio.create_task(run_agent(run, client))
    return {"run_id": run.id, "provider": client.provider, "model": client.model}


@app.post("/api/confirm/{run_id}")
async def confirm(run_id: str, req: ConfirmRequest) -> dict[str, bool]:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    if run.confirm_gate is None:
        raise HTTPException(409, "nothing is waiting for confirmation")
    run.confirm_approved = req.approved
    run.confirm_gate.set()
    return {"ok": True}


@app.post("/api/cancel/{run_id}")
async def cancel(run_id: str) -> dict[str, bool]:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    if run.task_handle and not run.task_handle.done():
        run.task_handle.cancel()
    return {"ok": True}


@app.get("/api/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")

    async def stream() -> AsyncIterator[str]:
        cursor = 0
        while True:
            # Clear before reading so an append during the read still wakes us.
            run.new_event.clear()
            while cursor < len(run.events):
                yield f"data: {json.dumps(run.events[cursor])}\n\n"
                cursor += 1
            if run.finished and cursor >= len(run.events):
                yield 'data: {"type": "end"}\n\n'
                return
            try:
                await asyncio.wait_for(run.new_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
