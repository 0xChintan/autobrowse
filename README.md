# AutoBrowse

An AI agent that completes plain-language tasks by driving a real Chromium
browser, step by step, in front of you.

You type *"search for wireless headphones and list the titles of the first 3
results"*. AutoBrowse opens a browser, reads the page, decides what to click or
type, does it, re-reads the page, and repeats until it can answer you.

```
observe (serialize DOM) → think (LLM picks one action) → act (Playwright) → repeat
```

Runs on **Groq** (free tier) or a **local Ollama** — both appear in the model
picker and you choose per run.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

export GROQ_API_KEY="gsk_..."      # free key: https://console.groq.com
uvicorn main:app --reload
```

Open **http://localhost:8000**, type a task, press Run. A Chromium window opens
and you watch it work while the timeline fills in.

No key? Run [Ollama](docs/models.md#running-locally-with-ollama) locally
instead — `ollama serve && ollama pull llama3`, then pick it from the dropdown.

> A `.venv/` with everything installed (including Chromium) is already present
> in this directory — `source .venv/bin/activate` and skip to the export.

---

## Docs

| | |
|---|---|
| [Architecture](docs/architecture.md) | DOM serializer, agent loop, safety layer, memory, HTTP API |
| [Models](docs/models.md) | Groq key setup, Ollama setup, which model to use, token budget |
| [Configuration](docs/configuration.md) | every environment variable |
| [Tasks](docs/tasks.md) | example tasks from easy to hard, and search engines that work |
| [Troubleshooting](docs/troubleshooting.md) | known limits and common failures |

---

## What's in the box

| File | Role |
|---|---|
| `main.py` | FastAPI server, the agent loop, safety layer, memory |
| `browser_controller.py` | Playwright wrapper — launch, serialize the DOM, execute actions |
| `llm_client.py` | Groq client + Ollama client, provider discovery, JSON parsing |
| `static/index.html` | Split-pane dashboard — controls left, live run timeline right |

## The interface

A two-pane console. The left sidebar holds the task box, start URL, model
picker, Run/Stop, theme swatches, and recent runs (click one to reload its
task). The right pane is a vertical timeline: one numbered node per step
showing the action, the model's reasoning, and what actually happened — green
for success, red for failure. A status pill tracks the run (`idle` →
`thinking` → `working` → `done`) with the step counter, elapsed time, and the
page the agent is currently on.

Approval prompts for risky actions appear inline as amber cards with
Approve / Deny buttons. The final answer lands in a card at the bottom.
`Cmd/Ctrl+Enter` in the task box runs it.

Five themes — Nebula (default), Midnight, Ember, Phosphor, Daylight —
switchable from the swatches and remembered in `localStorage`.

## Two things worth knowing

- **Keep the browser visible.** `headless=False` is the default and isn't only
  for watching: DuckDuckGo serves a bot-block page to headless Chromium but
  works normally headed.
- **Risky actions pause.** Anything whose target or value matches `buy`, `pay`,
  `checkout`, `delete`, `confirm`, `submit order` and ~20 more waits for your
  approval in the UI before it touches the page.
