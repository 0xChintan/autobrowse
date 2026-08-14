# Tasks and test targets

## How to phrase a task

Name **where to look** and **what to bring back**. The extraction clause is the
agent's finish line — without one it keeps exploring until the step limit.

| | |
|---|---|
| ✅ | *"Search for wireless headphones and list the titles of the first 3 results"* |
| ❌ | *"find me headphones"* — no stopping condition, burns all 15 steps |

For anything multi-page, raise the limit: `AUTOBROWSE_MAX_STEPS=25`.

---

## Search engines to try

"Verified" means it was actually driven end to end during development —
serialize, type, submit, read results. The rest are untested suggestions.

| Start URL | Verified | Notes |
|---|---|---|
| `https://duckduckgo.com` | ✅ | the default; **headed only** — headless gets a 418 bot page |
| `https://www.bing.com` | ✅ | typing + Enter works, results parse cleanly; heavy page |
| `https://html.duckduckgo.com/html/?q=test` | ✅ | no-JS DDG. Tiny and fast — **best on a slow connection.** Pass `?q=` in the start URL |
| `https://search.brave.com` | — | independent index, clean DOM, no login |
| `https://www.mojeek.com` | — | very light HTML, own crawler; good low-bandwidth candidate |
| `https://search.marginalia.nu` | — | tiny independent index, minimal markup |
| `https://www.ecosia.org` | — | Bing-backed, simple layout |
| `https://www.startpage.com` | — | Google results; may show a bot check |
| `https://en.wikipedia.org` | — | not a search engine, but the best target for clean extraction |

**Avoid Amazon.** It detects Playwright aggressively and usually serves a
CAPTCHA wall. The spec's original example ("wireless headphones under $50 on
Amazon") fails for that reason, not because of agent logic.

**On a slow or busy connection**, prefer `html.duckduckgo.com` or
`example.com`. A full DuckDuckGo or Bing page is several MB and can exceed the
navigation timeout.

---

## Tasks, easy to hard

### Tier 1 — single search, read the answer (2–4 steps)

```
Search for wireless headphones and list the titles of the first 3 results
What is the current weather in Tokyo?
Look up who won the last FIFA World Cup and in what year
Find the official Python documentation site and tell me the latest stable version
```

### Tier 2 — navigate, then extract (5–8 steps)

```
Go to news.ycombinator.com and list the titles of the top 5 stories
Find the GitHub repo for FastAPI and tell me how many stars it has
Go to en.wikipedia.org, search for "Playwright software", and summarize what it is in 2 sentences
Search for "best budget mechanical keyboard 2026", open the first result, and summarize its top pick
```

### Tier 3 — hard: click-through, comparison, or structure

These need several pages, or reading numbers and reasoning about them. Raise
`AUTOBROWSE_MAX_STEPS` to 25 first.

```
Go to Hacker News, find the highest-scoring story on the front page, open it,
and summarize the linked article in 3 bullet points
```
*Why it's hard: requires reading scores across items, comparing them, then a
click-through and a summary of a page it has never seen.*

```
Go to github.com/trending, switch the language filter to Python, and list the
top 3 repos with their star counts for today
```
*Why it's hard: the language filter is a dropdown — the agent must click to
open it, then find an option that only exists after that click.*

```
Compare the star counts of the FastAPI and Flask GitHub repos, and tell me
which is higher and by how much
```
*Why it's hard: two separate pages, and the answer is arithmetic on values it
has to remember across steps — its only memory is the action history.*

```
Go to arxiv.org, search for "browser agents", open the most recent paper, and
give me the title, the authors, and the first sentence of the abstract
```
*Why it's hard: sorting by date, then structured extraction of three different
fields from one page.*

```
Go to https://httpbin.org/forms/post, order a large pepperoni pizza for
delivery at 20:00, and submit it
```
*Why it's hard: the only task here that isn't read-only. It exercises `type`,
radio buttons, checkboxes and a select — and the submit button trips the safety
layer, so you'll get an Approve/Deny card. A safe sandbox: httpbin just echoes
the form back.*

```
Find the current price of Bitcoin and Ethereum and tell me the ratio between them
```
*Why it's hard: two figures that may live on different pages, plus arithmetic.*

### Tier 4 — expected to fail, instructive anyway

```
Search for wireless headphones under $50 on Amazon and list the top 3
```
CAPTCHA wall. Watch the agent try to work around it, then hit the step limit.

```
Log into <anything> and ...
```
No credential handling exists, by design.

```
Scroll an infinite feed and summarize the first 50 posts
```
No stopping condition the agent can reach inside the step limit.

---

## Reading a failure

| What you see | What it means |
|---|---|
| Same action repeated, then *"blocked by memory"* | the memory layer stopped a loop; the model needed a different approach and didn't find one |
| Hit the step limit | task too vague, or genuinely needed more than 15 steps |
| `invalid action` steps | the model broke the JSON schema — usually a model too small |
| Amber approval card | the safety layer matched a keyword; nothing has touched the page yet |
