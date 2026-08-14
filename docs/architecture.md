# Architecture

Four layers, plus a small HTTP surface. Each step of a run is one pass through
the first two.

```
observe (serialize DOM) → think (LLM picks one action) → act (Playwright) → repeat
```

---

## 1. DOM serializer

`browser_controller.py`

Every step, a script runs inside the page and pulls out the interactive
elements — links, buttons, inputs, selects, textareas, and their ARIA
equivalents. Each gets a short numeric id and is rendered as one line:

```
[1] <a> "Learn about DuckDuckGo"
[2] <input type="text" placeholder="Search privately" name="q" role="combobox"> "Search with DuckDuckGo"
[3] <button> "Search"
```

**Dropped before the model ever sees them:** `display:none` /
`visibility:hidden` / zero-size / `aria-hidden` nodes, hidden inputs, disabled
controls, elements inside ad containers (`id`/`class` matching `ad`, `ads`,
`sponsored`, `taboola`, …), and scripts.

**Stable targeting.** Each reported element is stamped with
`data-autobrowse-id` in the live DOM, so `target_id: 2` resolves back to the
exact node the model was looking at — no brittle selector guessing. Ids are
renumbered every step, which the system prompt states explicitly.

**Truncation keeps what's visible.** The list is capped
(`AUTOBROWSE_MAX_ELEMENTS`, default 60). When the cap bites, on-screen elements
are kept first and off-screen ones dropped — plain document order would spend
the budget on header and nav chrome and cut off before reaching the content the
agent is looking at. The surviving elements are then renumbered in document
order so ids still read top-to-bottom.

**Page text.** Alongside the element list, the model gets a trimmed snapshot of
the page's visible text (`AUTOBROWSE_MAX_PAGE_TEXT`, default 1400 chars). This
is what lets it actually *read* results rather than only click around.

---

## 2. Agent loop

`main.py`

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

The loop stops on `done` or after 15 steps (`AUTOBROWSE_MAX_STEPS`).

**Failure is fed back, not fatal.** Malformed JSON, unknown actions, and
missing fields become failed steps in the history so the model can correct
itself. Same for a page that won't load: the prompt says so in plain words
(*"The current page FAILED TO LOAD… use the 'navigate' action"*) rather than
leaving the model to interpret `chrome-error://chromewebdata/`.

**The `\n` convention** exists because search pages are hostile to the
alternative. DuckDuckGo swaps its Search button for a "Clear input" button the
moment you type, so hunting for a submit button costs steps and often clicks
the wrong thing. Ending the typed value with a newline presses Enter instead.

---

## 3. Safety layer

Before any action executes, the target element's label and the typed value are
scanned for risky keywords — `buy`, `pay`, `checkout`, `place order`,
`submit order`, `delete`, `confirm`, `subscribe`, `add to cart`, and ~20 more.

A match **pauses the run**. The step is printed to the terminal and an
Approve / Deny card appears inline in the timeline. Nothing touches the page
until you click. Deny stops the run; no answer within
`AUTOBROWSE_CONFIRM_TIMEOUT` (default 180s) counts as a deny.

---

## 4. Memory

Every step is recorded with its outcome and replayed to the model as history
(`AUTOBROWSE_HISTORY`, default the last 5).

When the *same* action signature fails twice, two things happen:

- a hard warning is injected into the prompt — *"…has already failed twice. You
  MUST try a genuinely different approach"* — and
- a third attempt at that exact action is refused outright, before it reaches
  the browser.

That's what stops the classic loop of clicking a stale id fifteen times.

---

## HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /api/models` | providers, their available models, and the default pick |
| `POST /api/run` | `{task, url, provider?, model?}` → `{run_id, provider, model}` |
| `GET /api/events/{run_id}` | SSE stream (see below) |
| `POST /api/confirm/{run_id}` | `{approved: bool}` — answer a safety prompt |
| `POST /api/cancel/{run_id}` | abort a run |
| `GET /api/health` | which LLM provider is live |

SSE event types: `status`, `thinking`, `step`, `confirm`, `done`, `error`,
`end`. Events are buffered per run, so a client that connects late still
replays the whole stream from the beginning.

Each run gets its own Chromium context, closed when the run finishes — no
cookies or login state carry over between runs. A run also gets its own LLM
client, so the model you picked applies to that run alone.
