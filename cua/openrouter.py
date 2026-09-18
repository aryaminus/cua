"""OpenRouter client: chat-completions (generative) + the Decisions API (Jev).

One key (OPENROUTER_API_KEY) covers both. Jev (typesafe/jev-*) is exposed via
POST /api/alpha/decisions with the Choice/Noul/Score question shapes — a
non-generative decision model returning calibrated probabilities. We use it
for exactly one thing: stuck/no-progress detection during discovery (see
REPORT.md §Architecture for why not more).
"""

from __future__ import annotations

import json
import os
import time

import httpx

from .budgets import LLMBudget

BASE = "https://openrouter.ai/api"


class OpenRouterError(RuntimeError):
    pass


class _Retry(Exception):
    """Internal: carry (status, text) of a retryable HTTP response upward."""


def _loads_forgiving(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # salvage the outermost JSON object (models sometimes wrap in prose
        # or code fences despite json_object mode)
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


class OpenRouter:
    def __init__(self, api_key: str | None = None, budget: LLMBudget | None = None):
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key:
            raise OpenRouterError("OPENROUTER_API_KEY not set (see .env.example)")
        self.budget = budget or LLMBudget()
        t = self.budget
        self._client = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=httpx.Timeout(
                t.read_timeout_s,
                connect=t.connect_timeout_s,
                write=t.write_timeout_s,
                pool=t.pool_timeout_s,
            ),
        )
        self.spent_usd = 0.0
        self.latencies_s: list[float] = []  # per chat call, after retries
        self.retries: int = 0  # transport/5xx/429 retries actually performed
        # Optional privacy posture (see REPORT.md §Safety): when
        # CUA_DATA_COLLECTION=deny, OpenRouter routes only to providers that do
        # not retain prompts. Off by default — it can exclude cheaper providers.
        dc = os.environ.get("CUA_DATA_COLLECTION", "").strip().lower()
        self._provider = {"data_collection": "deny"} if dc == "deny" else None

    def _body(self, body: dict) -> dict:
        if self._provider:
            body["provider"] = self._provider
        return body

    def _post(self, path: str, body: dict) -> dict:
        """POST with the bounded network ladder: retry transport errors and
        wait-and-retry statuses (429/503 honor Retry-After, per OpenRouter
        docs), raise immediately on client errors (4xx) and after
        max_attempts on anything else. Never silent, never unbounded."""
        b = self.budget
        last = ""
        for attempt in range(b.max_attempts):
            t0 = time.monotonic()
            try:
                r = self._client.post(path, json=body)
            except httpx.TransportError as exc:  # connect/read timeouts, resets
                last = f"transport: {exc}"
                if attempt + 1 < b.max_attempts:
                    self.retries += 1
                    time.sleep(b.backoff_s[min(attempt, len(b.backoff_s) - 1)])
                    continue
                break
            if r.status_code in b.retry_status:
                last = f"http {r.status_code}: {r.text[:300]}"
                if attempt + 1 < b.max_attempts:
                    self.retries += 1
                    ra = r.headers.get("Retry-After", "")
                    try:
                        wait = min(float(ra), b.retry_after_cap_s) if ra else 0.0
                    except ValueError:
                        wait = 0.0
                    wait = wait or b.backoff_s[min(attempt, len(b.backoff_s) - 1)]
                    time.sleep(wait)
                    continue
                break
            if r.status_code != 200:
                # client error (bad key, bad request, moderation) — not retryable
                err = r.json().get("error", {}) if r.text[:1] == "{" else {}
                raise OpenRouterError(f"http {r.status_code}: {err.get('message') or r.text[:300]}")
            self.latencies_s.append(time.monotonic() - t0)
            return r.json()
        raise OpenRouterError(f"giving up after {b.max_attempts} attempts: {last}")

    # ------------------------------------------------------------ generative --

    def chat_json(
        self,
        model: str,
        system: str,
        messages: list[dict],
        *,
        seed: int = 7,
        temperature: float = 0.0,
        max_tokens: int = 3000,
        attempts: int = 2,
    ) -> tuple[dict, dict]:
        """Chat completion constrained to JSON. Returns (parsed_json, usage).

        Robustness ladder (bounded, never silent): transport/5xx/429 retries
        per the network budget, OpenRouter's response healing plugin, then a
        salvage parse of the outermost {...} block, then one full re-request.
        After that we raise.
        """
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_tokens,
            # The action loop wants one small JSON object, not deliberation:
            # disabling reasoning keeps replies terse, cheap, and stable.
            "reasoning": {"enabled": False},
            "plugins": [{"id": "response-healing"}],
        }
        last_err = ""
        for _ in range(attempts):
            data = self._post("/v1/chat/completions", self._body(body))
            usage = data.get("usage", {})
            self.spent_usd += usage.get("cost") or 0.0
            try:
                content = data["choices"][0]["message"]["content"] or ""
                return _loads_forgiving(content), usage
            except (KeyError, json.JSONDecodeError) as exc:
                last_err = f"{exc}: {str(data)[:200]}"
        raise OpenRouterError(f"unparseable chat reply after {attempts} attempts: {last_err}")

    # -------------------------------------------------------------- decisions --

    def decide(self, model: str, state: dict, questions: dict) -> dict:
        """Jev-style decisions: typed questions, typed answers, no free text."""
        body = self._body({"model": model, "state": state, "questions": questions})
        data = self._post("/alpha/decisions", body)
        self.spent_usd += data.get("usage", {}).get("cost") or 0.0
        return data.get("answers", {})


class FakeLLM:
    """Offline stand-in: replays scripted decisions; used by tests and demos."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.i = 0
        self.spent_usd = 0.0
        self.seen_prompts: list[str] = []

    def chat_json(self, model, system, messages, **kw) -> tuple[dict, dict]:
        self.seen_prompts.append(messages[-1]["content"])
        if self.i >= len(self.script):
            return {"action": "fail", "reason": "script exhausted"}, {}
        nxt = self.script[self.i]
        self.i += 1
        return nxt, {}

    def decide(self, model, state, questions) -> dict:
        return {
            "progress": {"type": "noul", "noul": 0.9},
            "mode": {"type": "choice", "choice": "continue", "confidence": 0.9},
        }
