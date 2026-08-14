# Configuration

Everything is an environment variable. Set them in the shell, or in a `.env`
file next to `main.py` (loaded automatically by `python-dotenv`).

## Models

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | — | set → Groq appears in the picker |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | preselected Groq model |
| `OLLAMA_MODEL` | `llama3` | preselected Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_NUM_CTX` | `4096` | Ollama context window — must exceed ~2,500 |

## Agent

| Variable | Default | Meaning |
|---|---|---|
| `AUTOBROWSE_MAX_STEPS` | `15` | step safety limit per run |
| `AUTOBROWSE_HISTORY` | `5` | past steps replayed in the prompt |
| `AUTOBROWSE_CONFIRM_TIMEOUT` | `180` | seconds to wait for a safety approval |

## Browser

| Variable | Default | Meaning |
|---|---|---|
| `AUTOBROWSE_HEADLESS` | `0` | `1` hides the browser window — **breaks DuckDuckGo search** |
| `AUTOBROWSE_NAV_TIMEOUT` | `45000` | ms to wait for a page load |

## Prompt size

These three decide what a step costs — in Groq tokens and in local context.
Defaults land an observation at ~1,700–1,850 tokens.

| Variable | Default | Effect |
|---|---|---|
| `AUTOBROWSE_MAX_ELEMENTS` | `60` | interactive elements per observation |
| `AUTOBROWSE_MAX_PAGE_TEXT` | `1400` | chars of visible page text |
| `AUTOBROWSE_HISTORY` | `5` | past steps replayed |

Raising `AUTOBROWSE_MAX_ELEMENTS` helps on dense pages where the agent claims
it can't find a control; it costs tokens on every subsequent step. Lowering all
three is the fastest way to fit a smaller local context window.

## Example `.env`

```bash
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.1-8b-instant     # smaller model, separate daily quota
AUTOBROWSE_MAX_STEPS=25             # for harder multi-page tasks
AUTOBROWSE_NAV_TIMEOUT=60000        # slow connection
```
