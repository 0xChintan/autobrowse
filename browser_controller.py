"""Playwright wrapper: launch a browser, serialize the DOM, execute agent actions.

The serializer stamps every interactive element it reports with a
`data-autobrowse-id` attribute, so an action referring to `target_id: 7` can be
resolved back to the exact node the LLM was looking at.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

# Elements reported to the LLM per observation. Enough for a real page, small
# enough to keep the prompt cheap — prompt size is the binding constraint both
# for Groq's daily token cap and for fitting a local model's context window.
MAX_ELEMENTS = int(os.getenv("AUTOBROWSE_MAX_ELEMENTS", "60"))
# Visible page text handed to the LLM so it can actually read results.
MAX_PAGE_TEXT = int(os.getenv("AUTOBROWSE_MAX_PAGE_TEXT", "1400"))

DEFAULT_TIMEOUT_MS = 12_000
# Page loads get their own, longer budget: a first paint on a slow or busy
# connection routinely takes longer than an element interaction ever should.
NAV_TIMEOUT_MS = int(os.getenv("AUTOBROWSE_NAV_TIMEOUT", "45000"))


# --------------------------------------------------------------------------- #
# DOM serialization (runs in the page)
# --------------------------------------------------------------------------- #

_SERIALIZE_JS = """
(maxElements) => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="checkbox"]',
    '[role="radio"]', '[role="menuitem"]', '[role="option"]',
    '[role="textbox"]', '[role="combobox"]', '[role="searchbox"]',
    '[contenteditable=""]', '[contenteditable="true"]',
    '[onclick]',
  ].join(',');

  const AD_TOKEN = /(?:^|[-_\\s])(ad|ads|advert|advertisement|adsense|sponsored|promo-banner|doubleclick|taboola|outbrain)(?:$|[-_\\s])/i;
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME']);

  // Clear stamps from the previous observation so ids never go stale.
  for (const el of document.querySelectorAll('[data-autobrowse-id]')) {
    el.removeAttribute('data-autobrowse-id');
  }

  const looksLikeAd = (el) => {
    let node = el;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const id = node.id || '';
      const cls = typeof node.className === 'string' ? node.className : '';
      if (AD_TOKEN.test(id) || AD_TOKEN.test(cls)) return true;
      if (node.getAttribute && node.getAttribute('data-ad-slot') !== null) return true;
    }
    return false;
  };

  const isVisible = (el) => {
    if (SKIP_TAGS.has(el.tagName)) return false;
    if (el.tagName === 'INPUT' && el.type === 'hidden') return false;
    if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity || '1') < 0.05) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    // Parked far off-canvas (common for hidden menus / skip links).
    if (r.bottom < -3000 || r.right < -3000) return false;
    if (r.left > (window.innerWidth || 0) + 3000) return false;
    return true;
  };

  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();

  const labelFor = (el) => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy.split(' ')[0]);
      if (ref) {
        const t = clean(ref.innerText || ref.textContent);
        if (t) return t;
      }
    }

    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (el.id) {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) {
          const t = clean(lab.innerText || lab.textContent);
          if (t) return t;
        }
      }
      return clean(el.placeholder || el.value || el.name || el.title);
    }

    if (el.tagName === 'SELECT') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      return clean(el.name || (opt ? opt.text : '') || el.title);
    }

    if (el.tagName === 'IMG') return clean(el.alt);

    let text = clean(el.innerText || el.textContent);
    if (!text) {
      const img = el.querySelector('img[alt]');
      if (img) text = clean(img.alt);
    }
    if (!text) text = clean(el.title || el.name || el.getAttribute('value'));
    return text;
  };

  // Pass 1 — every element worth reporting, in document order.
  const candidates = [];
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!isVisible(el)) continue;
    if (looksLikeAd(el)) continue;

    const tag = el.tagName.toLowerCase();
    const label = labelFor(el).slice(0, 140);
    const role = el.getAttribute('role') || '';

    // A control with no label and no distinguishing attributes is noise.
    if (!label && !['input', 'textarea', 'select'].includes(tag) && !role) continue;

    const rect = el.getBoundingClientRect();
    candidates.push({
      el, tag, role, label,
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      name: el.getAttribute('name') || '',
      value: tag === 'input' || tag === 'textarea' ? clean(el.value).slice(0, 60) : '',
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      inViewport: rect.top < (window.innerHeight || 0) && rect.bottom > 0,
    });
  }

  // Pass 2 — if we have to drop some, drop off-screen ones first. Plain
  // document order would spend the budget on header/nav chrome and truncate
  // before reaching the content the agent is actually looking at.
  const truncated = candidates.length > maxElements;
  let chosen = candidates;
  if (truncated) {
    const order = new Map(candidates.map((c, i) => [c, i]));
    chosen = candidates.filter(c => c.inViewport)
      .concat(candidates.filter(c => !c.inViewport))
      .slice(0, maxElements)
      .sort((a, b) => order.get(a) - order.get(b));  // ids still read top-to-bottom
  }

  const results = chosen.map((c, i) => {
    const id = i + 1;
    c.el.setAttribute('data-autobrowse-id', String(id));
    return {
      id, tag: c.tag, role: c.role, label: c.label, type: c.type,
      placeholder: c.placeholder, name: c.name, value: c.value,
      href: c.href, inViewport: c.inViewport,
    };
  });

  const text = (document.body ? document.body.innerText : '') || '';
  return {
    url: location.href,
    title: document.title || '',
    elements: results,
    truncated,
    text: text.replace(/\\n{3,}/g, '\\n\\n').trim(),
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(document.body ? document.body.scrollHeight : 0),
    viewportHeight: window.innerHeight || 0,
  };
}
"""


@dataclass
class Observation:
    """One snapshot of the page, ready to be dropped into a prompt."""

    url: str
    title: str
    elements: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    truncated: bool = False
    scroll_y: int = 0
    scroll_height: int = 0
    viewport_height: int = 0

    def element_lines(self) -> str:
        if not self.elements:
            return "(no interactive elements found — the page may still be loading)"
        lines = [_format_element(el) for el in self.elements]
        if self.truncated:
            lines.append(f"... (only the first {MAX_ELEMENTS} elements are shown)")
        return "\n".join(lines)

    def scroll_note(self) -> str:
        if self.scroll_height <= self.viewport_height:
            return "page fits in one screen"
        pct = 0
        span = max(self.scroll_height - self.viewport_height, 1)
        pct = min(100, round(100 * self.scroll_y / span))
        return f"scrolled {pct}% down ({self.scroll_y}px of {self.scroll_height}px)"

    def label_for(self, target_id: int | None) -> str:
        if target_id is None:
            return ""
        for el in self.elements:
            if el["id"] == target_id:
                return _format_element(el)
        return ""


def _format_element(el: dict[str, Any]) -> str:
    """`[2] <input placeholder="Search" name="q">` / `[1] <button> "Submit"`."""
    attrs: list[str] = []
    if el.get("type"):
        attrs.append(f'type="{el["type"]}"')
    if el.get("placeholder"):
        attrs.append(f'placeholder="{el["placeholder"]}"')
    if el.get("name"):
        attrs.append(f'name="{el["name"]}"')
    if el.get("role"):
        attrs.append(f'role="{el["role"]}"')
    if el.get("value"):
        attrs.append(f'value="{el["value"]}"')

    head = f"<{el['tag']}" + ("".join(" " + a for a in attrs)) + ">"
    label = el.get("label") or ""
    if label and label not in (el.get("placeholder"), el.get("value")):
        return f'[{el["id"]}] {head} "{label}"'
    return f"[{el['id']}] {head}"


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #


class ActionError(Exception):
    """An action was attempted and failed in a way the agent should see."""


class BrowserController:
    """Owns one Chromium instance for the lifetime of a single agent run."""

    def __init__(self, headless: bool | None = None, start_url: str = "about:blank"):
        if headless is None:
            headless = os.getenv("AUTOBROWSE_HEADLESS", "0") not in ("0", "", "false", "False")
        self.headless = headless
        self.start_url = start_url
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        # Set when the start URL wouldn't load. Not fatal — the agent can issue
        # its own `navigate` action and recover.
        self.start_error: str | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        self._context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        # Follow target=_blank popups instead of losing track of the tab.
        self._context.on("page", self._on_new_page)
        self.page = await self._context.new_page()
        if self.start_url and self.start_url != "about:blank":
            try:
                await self.goto(self.start_url)
            except ActionError as exc:
                self.start_error = str(exc)

    def _on_new_page(self, page: Page) -> None:
        self.page = page

    async def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None

    # -- observation -------------------------------------------------------- #

    async def goto(self, url: str) -> str:
        assert self.page is not None
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url

        # "commit" resolves as soon as the response starts arriving, so a page
        # that is merely slow still gives us something to serialize instead of
        # failing the step outright.
        last: Exception | None = None
        for wait_until in ("domcontentloaded", "commit"):
            try:
                await self.page.goto(url, wait_until=wait_until, timeout=NAV_TIMEOUT_MS)
                await self._settle()
                return f"loaded {self.page.url}"
            except Exception as exc:
                last = exc

        raise ActionError(
            f"could not load {url} after {NAV_TIMEOUT_MS // 1000}s: {_short(last)} "
            "— the connection may be saturated, or the site may be blocking automation"
        ) from last

    async def observe(self) -> Observation:
        assert self.page is not None
        await self._settle()
        try:
            raw = await self.page.evaluate(_SERIALIZE_JS, MAX_ELEMENTS)
        except Exception:
            # A navigation can land mid-evaluate; one retry is usually enough.
            await asyncio.sleep(0.8)
            raw = await self.page.evaluate(_SERIALIZE_JS, MAX_ELEMENTS)

        text = raw.get("text", "")
        if len(text) > MAX_PAGE_TEXT:
            text = text[:MAX_PAGE_TEXT] + "\n... (page text truncated)"

        return Observation(
            url=raw.get("url", ""),
            title=raw.get("title", ""),
            elements=raw.get("elements", []),
            text=text,
            truncated=bool(raw.get("truncated")),
            scroll_y=int(raw.get("scrollY") or 0),
            scroll_height=int(raw.get("scrollHeight") or 0),
            viewport_height=int(raw.get("viewportHeight") or 0),
        )

    async def _settle(self) -> None:
        """Best-effort wait for the page to stop moving."""
        assert self.page is not None
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass  # Plenty of pages never go idle; that is fine.

    # -- actions ------------------------------------------------------------ #

    async def execute(self, action: str, target_id: int | None, value: str | None) -> str:
        """Run one action. Returns a short human-readable result string."""
        assert self.page is not None
        handlers = {
            "click": self._do_click,
            "type": self._do_type,
            "scroll": self._do_scroll,
            "navigate": self._do_navigate,
            "wait": self._do_wait,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ActionError(f"unknown action '{action}'")
        return await handler(target_id, value)

    def _locator(self, target_id: int | None):
        if target_id is None:
            raise ActionError("this action needs a target_id, but none was given")
        assert self.page is not None
        return self.page.locator(f'[data-autobrowse-id="{target_id}"]').first

    async def _do_click(self, target_id: int | None, value: str | None) -> str:
        loc = self._locator(target_id)
        before = self.page.url  # type: ignore[union-attr]
        try:
            await loc.scroll_into_view_if_needed(timeout=4_000)
        except Exception:
            pass
        try:
            await loc.click(timeout=8_000)
        except Exception as exc:
            # Overlays and cookie walls intercept ordinary clicks; force one.
            try:
                await loc.click(timeout=4_000, force=True)
            except Exception:
                raise ActionError(f"click on [{target_id}] failed: {_short(exc)}") from exc
        await self._settle()
        after = self.page.url  # type: ignore[union-attr]
        if after != before:
            return f"clicked [{target_id}] — navigated to {after}"
        return f"clicked [{target_id}]"

    async def _do_type(self, target_id: int | None, value: str | None) -> str:
        if value is None:
            raise ActionError("the 'type' action needs a value")
        loc = self._locator(target_id)
        submit = value.endswith("\n")
        text = value.rstrip("\n")
        try:
            await loc.scroll_into_view_if_needed(timeout=4_000)
        except Exception:
            pass
        try:
            await loc.fill(text, timeout=6_000)
        except Exception:
            # contenteditable and custom widgets often reject fill().
            try:
                await loc.click(timeout=4_000)
                await self.page.keyboard.press("Meta+A")  # type: ignore[union-attr]
                await self.page.keyboard.type(text, delay=20)  # type: ignore[union-attr]
            except Exception as exc:
                raise ActionError(f"typing into [{target_id}] failed: {_short(exc)}") from exc

        if submit:
            await loc.press("Enter")
            await self._settle()
            return f'typed "{text}" into [{target_id}] and pressed Enter — now at {self.page.url}'  # type: ignore[union-attr]
        return f'typed "{text}" into [{target_id}]'

    async def _do_scroll(self, target_id: int | None, value: str | None) -> str:
        raw = (value or "down").strip().lower()
        if raw in ("top", "up-top"):
            delta = -10**6
        elif raw == "bottom":
            delta = 10**6
        elif raw.lstrip("-").isdigit():
            delta = int(raw)
        elif raw.startswith("up"):
            delta = -700
        else:
            delta = 700
        await self.page.evaluate("(d) => window.scrollBy(0, d)", delta)  # type: ignore[union-attr]
        await asyncio.sleep(0.6)
        y = await self.page.evaluate("() => Math.round(window.scrollY)")  # type: ignore[union-attr]
        return f"scrolled {'down' if delta > 0 else 'up'} (now at y={y})"

    async def _do_navigate(self, target_id: int | None, value: str | None) -> str:
        if not value:
            raise ActionError("the 'navigate' action needs a URL in 'value'")
        return await self.goto(value.strip())

    async def _do_wait(self, target_id: int | None, value: str | None) -> str:
        try:
            seconds = float(value) if value else 2.0
        except (TypeError, ValueError):
            seconds = 2.0
        seconds = max(0.2, min(seconds, 10.0))
        await asyncio.sleep(seconds)
        await self._settle()
        return f"waited {seconds:g}s"


def _short(exc: Exception, limit: int = 160) -> str:
    msg = str(exc).split("\n")[0].strip()
    return msg[:limit] if msg else exc.__class__.__name__
