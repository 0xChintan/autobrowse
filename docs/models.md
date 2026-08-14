# Models

Groq and Ollama both appear in the **Model** dropdown; you choose per run.
Unavailable backends stay visible but disabled with the reason shown, so it's
obvious *why* an option is missing.

---

## Getting a free Groq API key

1. Go to **https://console.groq.com** and sign up — free, no card.
2. Open **API Keys** in the sidebar → **Create API Key**.
3. Copy it immediately; it starts with `gsk_` and is shown only once.
4. Export it before starting the server:

```bash
export GROQ_API_KEY="gsk_..."
```

Add it to `~/.zshrc` to make it stick, or drop it in a `.env` file next to
`main.py` — `python-dotenv` picks it up automatically. **Add `.env` to
`.gitignore`.**

The catalog in the dropdown is fetched live from the Groq API, so whatever your
account can reach shows up.

---

## Rate limits and token budget

Groq's free tier caps **tokens per day, per model**. When you hit it:

```
Groq rate limit hit for 'llama-3.3-70b-versatile' (98,508 of 100,000 daily
tokens used). Resets in 18m33s. Limits are per model — pick a smaller model
(e.g. llama-3.1-8b-instant) or switch to Ollama in the dropdown and run again.
```

Because limits are **per model**, switching models in the dropdown gives you an
untouched budget immediately — no waiting.

**What a run costs.** An observation is ~1,700–1,850 input tokens, measured on
a DuckDuckGo run; the element list is the bulk of it, and history grows as the
run goes on. A full 15-step run is therefore ~26k tokens — roughly four long
runs or two dozen short ones inside a 100k/day cap. Ollama has no cap at all.

Trim further with `AUTOBROWSE_MAX_ELEMENTS`, `AUTOBROWSE_MAX_PAGE_TEXT`, and
`AUTOBROWSE_HISTORY` (see [Configuration](configuration.md)).

---

## Running locally with Ollama

No API key, no rate limits, no daily cap.

**1. Install and start**

```bash
brew install ollama          # or the app: https://ollama.com/download
ollama serve                 # leave running in its own terminal
```

If you installed the **.app**, launching it starts the server for you — a llama
appears in the menu bar and `ollama serve` is unnecessary (it errors with
"address already in use", which just means it's already up).

**2. Pull a model**

```bash
ollama pull llama3           # 8B, ~4.7 GB
```

**3. Pick it in the dropdown**

Start `uvicorn main:app --reload` and open http://localhost:8000. Everything
you've pulled appears under *Ollama (local)*. Refresh the page if you started
Ollama after loading it.

Verify Ollama is reachable — AutoBrowse hits this same endpoint:

```bash
curl http://localhost:11434/api/tags
```

### Which local model

This agent emits strict JSON against a ~1,800-token observation every step,
which punishes small models. In rough order of how well they hold the schema:

| Model | Pull | Size |
|---|---|---|
| `qwen2.5:7b-instruct` | `ollama pull qwen2.5:7b-instruct` | ~4.7 GB |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | ~4.9 GB |
| `llama3` | `ollama pull llama3` | ~4.7 GB |
| under 3B | — | won't hold the action schema |

### Memory and context window

Ollama sizes its default context from available VRAM — 2048 on older builds,
4096 on a small Apple Silicon box — and **silently truncates the front of an
over-long prompt**, which is the system prompt and your task. The model then
invents actions from a bare element list, and it reads as "local models are
bad" rather than a config problem.

AutoBrowse therefore always sends `num_ctx` explicitly. Default **4096**,
override with `OLLAMA_NUM_CTX`.

Why not larger: on ~5.3 GB of usable VRAM (an 8 GB Mac), an 8B model at Q4
needs ~4.3 GB for weights alone. A 4096-token KV cache adds ~0.5 GB and fits;
8192 adds ~1 GB and doesn't — Ollama spills layers to CPU and generation
crawls. Raise it only if you have headroom.

Check what your machine reports in the `ollama serve` log:

```
msg="inference compute" ... description="Apple M3" total="5.3 GiB"
msg="vram-based default context" total_vram="5.3 GiB" default_num_ctx=4096
```

**On an 8 GB Mac, prefer a 3B model** — an 8B leaves almost no headroom once
the KV cache and compute buffers are added:

```bash
ollama pull qwen2.5:3b-instruct    # ~2 GB, the better of the two at strict JSON
ollama pull llama3.2:3b            # ~2 GB
```

### Speed

Profiled on an 8 GB M3 with `llama3:latest`, `num_ctx=4096`:

| Phase | Time |
|---|---|
| Chromium launch | 1.6 s |
| Page load + settle | 2.1 s |
| Serialize the DOM | **0.04 s** |
| Inference — model **cold** | **56 s** |
| Inference — model **hot** | **3.8 s** |

The browser is not the bottleneck; loading the model off disk is. Everything
below targets that one number.

**1. AutoBrowse preloads the model for you.** Picking an Ollama model in the
dropdown fires `POST /api/warm` immediately, so the load overlaps with you
typing the task instead of being charged to step 1. A run also warms the model
concurrently with the Chromium launch. You'll see `Model ready · llama3:latest
resident (13.6s)` in the timeline.

**2. It stays resident for 30 minutes.** Ollama's own default is 5 minutes, so
a coffee break silently re-paid the load cost. Override with
`OLLAMA_KEEP_ALIVE` (`-1` keeps it loaded until Ollama restarts).

**3. Use a smaller model.** On 8 GB the 8B is both slow to load and close to
the VRAM ceiling. A 3B loads in a fraction of the time and roughly halves
per-step latency:

```bash
ollama pull qwen2.5:3b-instruct
```

With the model hot, a 4-step run is roughly `4 × 3.8 s` of thinking plus ~4 s
of browser — around 20 s, against ~114 s for the same run cold.

**4. If you want fast, use Groq.** Inference is sub-second there; the local
path buys you privacy and no quota, not speed.
