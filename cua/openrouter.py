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

import httpx

BASE = "https://openrouter.ai/api"
TIMEOUT_S = 90.0


class OpenRouterError(RuntimeError):
    pass


class OpenRouter:
    def __init__(self, api_key: str | None = None):
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key:
            raise OpenRouterError("OPENROUTER_API_KEY not set (see .env.example)")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {self.key}"}, timeout=TIMEOUT_S
        )
        self.spent_usd = 0.0

    # ------------------------------------------------------------ generative --

    def chat_json(
        self,
        model: str,
        system: str,
        messages: list[dict],
        *,
        seed: int = 7,
        temperature: float = 0.0,
        max_tokens: int = 700,
    ) -> tuple[dict, dict]:
        """Chat completion constrained to JSON. Returns (parsed_json, usage)."""
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_tokens,
        }
        r = self._client.post("/v1/chat/completions", json=body)
        if r.status_code != 200:
            raise OpenRouterError(f"chat {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usage", {})
        self.spent_usd += usage.get("cost") or 0.0
        try:
            content = data["choices"][0]["message"]["content"] or ""
            return json.loads(content), usage
        except (KeyError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"unparseable chat reply: {exc}: {str(data)[:300]}") from exc

    # -------------------------------------------------------------- decisions --

    def decide(self, model: str, state: dict, questions: dict) -> dict:
        """Jev-style decisions: typed questions, typed answers, no free text."""
        body = {"model": model, "state": state, "questions": questions}
        r = self._client.post("/alpha/decisions", json=body)
        if r.status_code != 200:
            raise OpenRouterError(f"decisions {r.status_code}: {r.text[:300]}")
        data = r.json()
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
