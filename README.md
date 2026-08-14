# AutoBrowse

An AI agent that completes plain-language tasks by driving a real Chromium
browser, step by step, in front of you.

You type *"search for wireless headphones and list the titles of the first 3
results"*. AutoBrowse opens a browser, reads the page, decides what to click or
type, does it, re-reads the page, and repeats until it can answer you.

```
observe (serialize DOM) -> think (LLM picks one action) -> act (Playwright) -> repeat
```

---

## Quick start

```bash
# 1. dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. the browser Playwright drives
playwright install chromium

# 3. your LLM key (see below)
export GROQ_API_KEY="gsk_..."

# 4. run
uvicorn main:app --reload
```

Open **http://localhost:8000**, type a task, press Run. A Chromium window opens
and you watch it work while the log panel fills in.

> A `.venv/` with everything installed (including Chromium) is already present in
> this directory — `source .venv/bin/activate` and skip to step 3.

### Getting a free Groq API key

1. Go to **https://console.groq.com** and sign up (free, no card).
2. Open **API Keys** in the left sidebar → **Create API Key**.
3. Copy the key (it starts with `gsk_`) — it is shown only once.
4. Export it before starting the server:
   ```bash
   export GROQ_API_KEY="gsk_..."
   ```
   To make it stick, add that line to `~/.zshrc`, or drop it in a `.env` file
   next to `main.py` (`python-dotenv` picks it up automatically).

The free tier is rate-limited but comfortably enough for this — a typical task
is 3–8 requests.

### Running without a key (Ollama fallback)

If `GROQ_API_KEY` is not set, AutoBrowse talks to a local Ollama instead:

```bash
brew install ollama      # or https://ollama.com/download
ollama serve             # leave running
ollama pull llama3
uvicorn main:app         # no key needed
```

The header in the UI tells you which provider is live, and warns you if Ollama
is unreachable or the model isn't pulled. Local llama3 is noticeably worse at
sticking to the JSON schema than llama-3.3-70b — expect more wasted steps.

---

## What's in the box

| File | Role |
|---|---|
| `main.py` | FastAPI server, the agent loop, safety layer, memory |
| `browser_controller.py` | Playwright wrapper — launch, serialize the DOM, execute actions |
| `llm_client.py` | Groq client with Ollama fallback and a forgiving JSON parser |
| `static/index.html` | Chat-style frontend with a live step log |

### 1. DOM serializer

Every step, a script runs inside the page and pulls out the interactive
elements — links, buttons, inputs, selects, textareas, and ARIA equivalents.
Each gets a short numeric id and is rendered as one line:

```
[1] <a> "Learn about DuckDuckGo"
[2] <input type="text" placeholder="Search privately" name="q" role="combobox"> "Search with DuckDuckGo"
[3] <button> "Search"
```

Dropped before the model ever sees them: `display:none` / `visibility:hidden` /
zero-size / `aria-hidden` nodes, hidden inputs, disabled controls, elements
inside ad containers (`id`/`class` matching `ad`, `ads`, `sponsored`,
`taboola`, …), and scripts. The list is capped at 120 elements.

Each reported element is stamped with `data-autobrowse-id` in the live DOM, so
`target_id: 2` resolves back to the exact node the model was looking at — no
brittle selector guessing. **Ids are renumbered every step**, which the system
prompt states explicitly.

Alongside the element list, the model gets a trimmed snapshot of the page's
visible text (2500 chars). That's what lets it actually *read* results rather
than just click around.

### 2. Agent loop

Each step sends the task, the current URL, the element list, the page text, and
the recent action history. The model must reply with exactly one JSON object:

```json
{
  "action": "click" | "type" | "scroll" | "navigate" | "wait" | "done",
  "target_id": 2,
  "value": "wireless headphones\n",
  "reasoning": "type the query into the search box and submit"
}
```

| Action | `target_id` | `value` |
|---|---|---|
| `click` | required | — |
| `type` | required | text to type; **end with `\n` to press Enter** |
| `scroll` | — | `down` / `up` / `top` / `bottom` / a pixel count |
| `navigate` | — | a URL |
| `wait` | — | seconds |
| `done` | — | the final answer for the user |

The loop stops on `done` or after **15 steps** (configurable). Malformed JSON,
unknown actions, and missing fields are fed back to the model as failed steps
rather than crashing the run — it gets to correct itself.

### 3. Safety layer

Before any action executes, the target element's label and the typed value are
scanned for risky keywords — `buy`, `pay`, `checkout`, `place order`,
`submit order`, `delete`, `confirm`, `subscribe`, `add to cart`, and ~20 more.

A match **pauses the run**. The step is printed to the terminal and an
Approve / Deny card appears in the log panel. Nothing touches the page until you
click. Deny stops the run; no answer after 3 minutes counts as a deny.

### 4. Memory

Every step is recorded with its outcome and replayed to the model as history.
When the *same* action signature fails twice, two things happen:

- a hard warning is injected into the prompt (*"…has already failed twice. You
  MUST try a genuinely different approach"*), and
- a third attempt at that exact action is refused outright, before it reaches
  the browser.

That's what stops the classic loop of clicking a stale id fifteen times.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | — | set → use Groq; unset → use Ollama |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model |
| `OLLAMA_MODEL` | `llama3` | fallback model |
| `OLLAMA_HOST` | `http://localhost:11434` | fallback endpoint |
| `AUTOBROWSE_MAX_STEPS` | `15` | step safety limit |
| `AUTOBROWSE_HEADLESS` | `0` | `1` hides the browser window |
| `AUTOBROWSE_CONFIRM_TIMEOUT` | `180` | seconds to wait for approval |

---

## Notes and known limits

- **Keep `headless=False`.** It is the default, and it isn't only for watching:
  DuckDuckGo's search serves a bot-block page (HTTP 418) to headless Chromium
  but works normally headed. Setting `AUTOBROWSE_HEADLESS=1` will break DDG
  search.
- **Amazon will fight you.** It detects Playwright aggressively and often serves
  a CAPTCHA wall. The example task from the spec ("wireless headphones under $50
  on Amazon") frequently fails for that reason, not because of agent logic.
  DuckDuckGo, Bing, and most normal sites work fine.
- **Sponsored results.** Ad *elements* are stripped from the actionable list, but
  ad *text* still appears in the page-text snapshot (search engines render it
  inline). The system prompt tells the model to skip anything marked AD or
  Sponsored; a weaker model occasionally reports one anyway.
- **One tab, followed automatically.** If a click opens a new tab, AutoBrowse
  switches to it.
- **Fresh browser per run.** Each task launches its own Chromium context and
  closes it when finished — no cookies or login state carry over between runs.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Executable doesn't exist … playwright install` | run `playwright install chromium` inside the venv |
| Header shows "Ollama not reachable" | `export GROQ_API_KEY=...`, or start `ollama serve` |
| Header shows "'llama3' is not pulled" | `ollama pull llama3` |
| Search returns an "Unexpected error" page | you're running headless — unset `AUTOBROWSE_HEADLESS` |
| Run ends at "15-step safety limit" | task was too vague; name the site and what to extract |
| Log panel stays empty | check the terminal — the server logs every step too |

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/run` | `{task, url}` → `{run_id}` |
| `GET /api/events/{run_id}` | SSE stream: `status`, `thinking`, `step`, `confirm`, `done`, `error`, `end` |
| `POST /api/confirm/{run_id}` | `{approved: bool}` — answer a safety prompt |
| `POST /api/cancel/{run_id}` | abort a run |
| `GET /api/health` | which LLM provider is live |
# autobrowse
