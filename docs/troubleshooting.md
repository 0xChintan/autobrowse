# Troubleshooting

## Common failures

| Symptom | Fix |
|---|---|
| `Executable doesn't exist … playwright install` | run `playwright install chromium` inside the venv |
| Dropdown shows "GROQ_API_KEY is not set" | `export GROQ_API_KEY=...` and refresh the page |
| Dropdown shows "not reachable at http://localhost:11434" | start `ollama serve`, then refresh |
| Dropdown shows "no models pulled" | `ollama pull llama3` |
| `could not load … Page.goto: Timeout` | connection saturated (a big download running?) or a heavy page — raise `AUTOBROWSE_NAV_TIMEOUT`, or use a lighter start URL |
| Search returns an "Unexpected error" page | you're running headless — unset `AUTOBROWSE_HEADLESS` |
| `Groq rate limit hit` | limits are per model — pick a different one in the dropdown, or switch to Ollama |
| Run ends at the step limit | task too vague; name the site and what to extract |
| Local model emits junk / invalid actions | context too small or model too small — see [Models](models.md#memory-and-context-window) |
| Timeline stays empty | check the terminal; the server logs every step too |

## Known limits

- **Keep `headless=False`.** It is the default, and not only so you can watch:
  DuckDuckGo's search serves a bot-block page (HTTP 418) to headless Chromium
  but works normally headed. `AUTOBROWSE_HEADLESS=1` breaks DDG search.

- **Amazon will fight you.** It detects Playwright aggressively and often
  serves a CAPTCHA wall. DuckDuckGo, Bing, and most ordinary sites are fine.

- **Sponsored results.** Ad *elements* are stripped from the actionable list,
  but ad *text* still appears in the page-text snapshot — search engines render
  it inline with real results. The system prompt tells the model to skip
  anything marked AD or Sponsored; a weaker model occasionally reports one
  anyway.

- **No login handling.** There is no credential storage, and each run gets a
  fresh browser context, so nothing persists between runs.

- **One tab, followed automatically.** If a click opens a new tab, AutoBrowse
  switches to it.

- **Dense pages truncate.** Only `AUTOBROWSE_MAX_ELEMENTS` (default 60)
  elements are shown, on-screen ones first. If the agent insists a control
  isn't there, raise it — at the cost of tokens on every step.

## Untested paths

Honest inventory of what has not been exercised end to end:

- **Groq inference** was verified only through the error path (rate limit
  formatting, model listing). The chat call itself follows the documented SDK
  surface but has not returned a live completion here.
- Every search engine in [Tasks](tasks.md#search-engines-to-try) without a ✅.
