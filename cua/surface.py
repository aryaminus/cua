"""Surface seam: perception and action against a normalized element model.

Everything above this module speaks in terms of a numbered element table
(role, accessible-name approximation, value) and typed actions. Only this
module knows there is a browser underneath. That is the seam the spec's §3.7
asks for: a legacy frameset page, an accessibility-tree driver, or an OS-level
driver would implement the same :class:`PageSurface` protocol without touching
the artifact schema, replay engine, or escalation logic.

Deliberate constraint (legacy fidelity): the name inference mirrors what an
accessibility tree exposes — label association, placeholder, title, adjacent
cell text. It does *not* read ``name``/``id`` attributes, because legacy
enterprise markup essentially never carries meaningful ones (glossary: "Test
ID — legacy enterprise apps essentially never have them").

Acting on an element keeps a reference to the real node: the snapshot assigns
indices over a fixed selector list in document order, and actions resolve
``.nth(index)`` on that same list — the "validated handle" pattern. The model
never emits selectors, coordinates, or code.
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel

from .schema import Locator

# Stable selector list shared by snapshot() and act() — document order both
# times, so an element's index is a live reference to the real node.
SELECTOR = "a, button, input, select, textarea, [onclick]"

_SNAPSHOT_JS = """
() => {
  const nameFor = (el) => {
    const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    if ((el.type === 'submit' || el.type === 'button') && el.value) return clean(el.value);
    const cell = el.closest('td');
    if (cell && cell.previousElementSibling) {
      const t = clean(cell.previousElementSibling.innerText).replace(/:$/, '');
      if (t) return t;
    }
    return clean(el.innerText);
  };
  const roleFor = (el) => {
    const tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'INPUT') {
      if (el.type === 'submit' || el.type === 'button') return 'button';
      if (el.type === 'checkbox') return 'checkbox';
      if (el.type === 'hidden') return null;
      return 'textbox';
    }
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (el.hasAttribute('onclick')) return 'clicktext';
    return null;
  };
  const out = [];
  document.querySelectorAll(%SELECTOR%).forEach((el) => {
    const role = roleFor(el);
    if (!role) return;
    if (el.offsetParent === null && role !== 'clicktext') return; // not visible
    out.push({
      index: out.length,
      role,
      name: nameFor(el),
      value: el.value === undefined ? '' : String(el.value),
      tag: el.tagName.toLowerCase(),
    });
  });
  return out;
}
""".replace("%SELECTOR%", json.dumps(SELECTOR))


class Element(BaseModel):
    index: int
    role: str
    name: str
    value: str = ""
    tag: str = ""


class Snapshot(BaseModel):
    url: str
    text: str  # body innerText — state reading, checkpoints, extraction
    elements: list[Element]


# ----------------------------------------------------------- locator logic --
# Pure: resolved against a fresh Snapshot every time. No stored references
# across steps, so replay survives DOM identity churn between steps.


def resolve(locator: Locator, snap: Snapshot) -> Element | None:
    """Primary: role + name (exact, then contains). Then declared fallbacks."""
    cands = [e for e in snap.elements if e.role == locator.role]
    exact = [e for e in cands if e.name.strip().lower() == locator.name.strip().lower()]
    if len(exact) == 1:
        return exact[0]
    contains = [
        e for e in cands if locator.name.strip().lower() in e.name.strip().lower()
    ]
    if len(contains) == 1:
        return contains[0]
    for fb in locator.fallbacks:
        if fb.get("kind") == "text_contains":
            hits = [
                e
                for e in snap.elements
                if fb.get("text", "").lower() in e.name.lower()
            ]
            if len(hits) == 1:
                return hits[0]
        elif fb.get("kind") == "tag_ordinal":
            hits = [e for e in snap.elements if e.tag == fb.get("tag")]
            i = fb.get("ordinal", 0)
            if len(hits) > i:
                return hits[i]
    return None


def locator_for(el: Element) -> Locator:
    """Derive the artifact locator for an element the discovery run acted on."""
    fallbacks: list[dict] = []
    if el.tag:
        fallbacks.append({"kind": "tag_ordinal", "tag": el.tag, "ordinal": 0})
    return Locator(role=el.role, name=el.name, fallbacks=fallbacks)


# ------------------------------------------------------------------ page ----

class PageSurface(Protocol):
    """What replay/agent/escalation need from a live session."""

    def goto(self, url: str) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def click(self, el: Element) -> None: ...
    def fill(self, el: Element, value: str) -> None: ...
    def press_enter(self, el: Element | None) -> None: ...
    def screenshot(self, path: str) -> bool: ...
    @property
    def dialogs_seen(self) -> list[str]: ...


class PlaywrightSurface:
    """Playwright-backed PageSurface. One instance == one live browser session."""

    def __init__(self, url: str | None = None, headless: bool = True):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._page = self._browser.new_page()
        self._dialogs: list[str] = []
        self._page.on("dialog", self._on_dialog)
        if url:
            self.goto(url)

    def _on_dialog(self, dialog) -> None:  # pragma: no cover - driver callback
        # Recoverable-condition policy: unexpected JS dialogs are dismissed
        # and logged; escalation is a count-based engine decision, not ours.
        self._dialogs.append(f"{dialog.type}: {dialog.message[:120]}")
        dialog.dismiss()

    @property
    def dialogs_seen(self) -> list[str]:
        return list(self._dialogs)

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def snapshot(self) -> Snapshot:
        elements = [Element(**e) for e in self._page.evaluate(_SNAPSHOT_JS)]
        return Snapshot(
            url=self._page.url,
            text=self._page.evaluate("() => document.body.innerText"),
            elements=elements,
        )

    def _nth(self, el: Element):
        return self._page.locator(SELECTOR).nth(el.index)

    def click(self, el: Element) -> None:
        self._nth(el).click()

    def fill(self, el: Element, value: str) -> None:
        self._nth(el).fill(value)

    def press_enter(self, el: Element | None) -> None:
        if el is not None:
            self._nth(el).press("Enter")
        else:
            self._page.keyboard.press("Enter")

    def screenshot(self, path: str) -> bool:
        try:
            self._page.screenshot(path=path)
            return True
        except Exception:  # pragma: no cover - best-effort evidence
            return False

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()
