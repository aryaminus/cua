"""Runtime budgets: one typed file of truth for every latency/cost ceiling.

Budgets are *runtime posture*, deliberately outside the capability artifact:
an artifact describes what a capability is (environment-independent), budgets
describe how we are willing to run it here. Defaults are compiled in so the
config file is optional; `config/budgets.json` overrides, and
`--budget group.key=value` overrides on the command line ( loudest wins ).

Rationale for each default is in REPORT.md §6 "Budgets, latency, cost".
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

# Statuses OpenRouter documents as wait-and-retry (Retry-After on 429/503;
# 5xx are transient provider failures). 4xx client errors (400/401/403/404)
# are never retried — retrying a bad request or a bad key is wrong.
RETRY_STATUS: tuple[int, ...] = (408, 429, 500, 502, 503, 504)


class LLMBudget(BaseModel):
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    write_timeout_s: float = 30.0
    pool_timeout_s: float = 10.0
    max_attempts: int = 3
    backoff_s: list[float] = Field(default_factory=lambda: [0.5, 1.0])
    retry_status: list[int] = Field(default_factory=lambda: list(RETRY_STATUS))
    retry_after_cap_s: float = 30.0  # never sleep longer than this on Retry-After


class DiscoveryBudget(BaseModel):
    wall_clock_s: float = 600.0
    max_steps: int = 40
    max_llm_calls: int = 60
    max_cost_usd: float = 0.50


class ReplayBudget(BaseModel):
    postcondition_wait_s: float = 3.0
    act_timeout_s: float = 10.0
    total_s: float = 180.0


class Budgets(BaseModel):
    llm: LLMBudget = Field(default_factory=LLMBudget)
    discovery: DiscoveryBudget = Field(default_factory=DiscoveryBudget)
    replay: ReplayBudget = Field(default_factory=ReplayBudget)

    def flat(self) -> dict[str, float | int | list]:
        """{"llm.max_attempts": 3, ...} — the override address space."""
        out: dict[str, float | int | list] = {}
        for group in ("llm", "discovery", "replay"):
            for k, v in getattr(self, group).model_dump().items():
                out[f"{group}.{k}"] = v
        return out

    def apply(self, dotted: str, value: str) -> None:
        """Apply one `group.key=value` override; unknown keys are an error."""
        group, _, key = dotted.partition(".")
        models = {"llm": self.llm, "discovery": self.discovery, "replay": self.replay}
        if group not in models:
            raise SystemExit(f"unknown budget group in --budget {dotted!r} (llm|discovery|replay)")
        m = models[group]
        fields = type(m).model_fields
        current = getattr(m, key, None) if key in fields else None
        if current is None:
            known = ", ".join(sorted(fields))
            raise SystemExit(f"unknown budget key in --budget {dotted!r} (have: {known})")
        try:
            casted: float | int | list = (
                float(value) if isinstance(current, float)
                else int(value) if isinstance(current, int)
                else json.loads(value)
            )
        except ValueError as exc:
            raise SystemExit(f"--budget {dotted!r}: bad value {value!r} ({exc})") from exc
        setattr(m, key, casted)


def load_budgets(path: str | Path | None = None) -> Budgets:
    """Defaults <- config file <- (CLI apply happens separately)."""
    b = Budgets()
    p = Path(path) if path else Path("config/budgets.json")
    if p.exists():
        raw = json.loads(p.read_text())
        b = Budgets.model_validate(raw)
    return b
