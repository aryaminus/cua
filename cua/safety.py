"""Safety guardrails: a configurable allowlist enforced on every action in
both loops, plus PII/financial-data redaction applied to everything persisted
(and to anything sent to a model).

Model (default-deny):
  1. Action type must be in ``actions``.
  2. ``goto``/navigation target must match an allowed origin AND route
     (fnmatch-style, ``*`` spans path segments).
  3. click/fill targets are checked against ``forbidden_elements`` — role/name
     patterns marking risky or irreversible controls (e.g. Freeze Accounts).
     Policy is "block": the attempt is refused, logged, and surfaced to the
     discovery agent as a blocked outcome.
  4. After any action, the live URL must still match an allowed route
     (navigation guard — catches redirects to unexpected surfaces).

Limits (documented in DESIGN.md §6): element risk is classified by
declared role/name patterns, not by resolving form targets — appropriate for a
known back-office app catalog, not for open-web automation.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_ALLOWLIST_PATH = Path(__file__).parent.parent / "config" / "allowlist.json"


@dataclass
class Verdict:
    allowed: bool
    risk: str  # SAFE | RISKY | BLOCKED
    reason: str


class Allowlist:
    def __init__(self, cfg: dict):
        self.raw = dict(cfg)
        self.origins: list[str] = cfg.get("origins", [])
        self.routes: list[str] = cfg.get("routes", [])
        self.actions: set[str] = set(cfg.get("actions", []))
        self.forbidden: list[dict] = cfg.get("forbidden_elements", [])
        self.policy: str = cfg.get("risky_policy", "block")

    @classmethod
    def load(cls, path: str | Path | None = None) -> Allowlist:
        p = Path(path) if path else DEFAULT_ALLOWLIST_PATH
        return cls(json.loads(p.read_text()))

    # -- checks ----------------------------------------------------------------

    def check_action(self, action: str) -> Verdict:
        if action in self.actions:
            return Verdict(True, "SAFE", f"action {action!r} allowed")
        return Verdict(False, "BLOCKED", f"action {action!r} not in allowlist")

    def check_url(self, url: str) -> Verdict:
        origin = urlparse(url)
        origin_str = f"{origin.scheme}://{origin.netloc}"
        if origin_str not in self.origins:
            return Verdict(False, "BLOCKED", f"origin {origin_str!r} not allowed")
        if not any(fnmatch.fnmatch(origin.path or "/", r) for r in self.routes):
            return Verdict(False, "BLOCKED", f"route {origin.path!r} not in allowlist routes")
        return Verdict(True, "SAFE", f"route {origin.path!r} allowed")

    def check_element(self, action: str, role: str, name: str) -> Verdict:
        for pat in self.forbidden:
            if pat.get("role") and pat["role"] != role:
                continue
            needle = (pat.get("name_contains") or "").lower()
            if needle and needle in name.lower():
                return Verdict(
                    False,
                    "RISKY",
                    f"risky control {role} {name!r} ({pat.get('class', 'irreversible')})"
                    f" — policy {self.policy}",
                )
        return Verdict(True, "SAFE", f"{action} on {role} {name!r} allowed")


# ------------------------------------------------------------------ redaction --

_PATTERNS: list[tuple[str, str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED-PAN]"),
    (re.compile(r"\$\s?[\d,]+\.\d{2}\b"), "$[REDACTED]"),
    (re.compile(r"\b\d{1,3}(?:,\d{3})+\.\d{2}\b"), "[REDACTED-AMT]"),  # bare 2,451.90
]


def redact_text(text: str) -> str:
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact(obj):
    """Recursively redact strings inside dicts/lists (used for all persistence)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj
