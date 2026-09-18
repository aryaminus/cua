"""Budget enforcement and the network retry ladder (REPORT.md §6).

Every budget here is enforced, not advisory: breaches produce typed failures
with `budget exceeded` reasons, never silent continuation and never hangs.
"""

import httpx
import pytest

from cua.budgets import Budgets, DiscoveryBudget, ReplayBudget, load_budgets
from cua.evidence import RunLog
from cua.openrouter import OpenRouter, OpenRouterError
from cua.replay import ReplayEngine
from cua.safety import Allowlist
from cua.schema import Param

from .conftest import CFG
from .fakepage import FakePage
from .test_agent import SCRIPT
from .test_replay import lookup_artifact

# ---------------------------------------------------------------- budgets.py --

def test_budget_defaults_load_without_config_file(tmp_path):
    b = load_budgets(tmp_path / "nonexistent.json")
    assert b.replay.total_s == 180.0
    assert b.llm.retry_status == [408, 429, 500, 502, 503, 504]


def test_budget_override_accepts_known_key_and_rejects_unknown():
    b = Budgets()
    b.apply("replay.total_s", "30")
    assert b.replay.total_s == 30.0
    with pytest.raises(SystemExit):
        b.apply("replay.nonexistent", "1")
    with pytest.raises(SystemExit):
        b.apply("nosuch.group", "1")


def test_budget_flat_address_space_is_complete():
    flat = Budgets().flat()
    assert "llm.max_attempts" in flat and "discovery.max_cost_usd" in flat
    assert "replay.total_s" in flat


# --------------------------------------------------------------- openrouter --

def _mock_client(handler) -> OpenRouter:
    """An OpenRouter whose transport is fully mocked (no network, no key)."""
    orv = object.__new__(OpenRouter)  # skip __init__'s key requirement
    orv.budget = Budgets().llm
    orv._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
    orv.spent_usd = 0.0
    orv.latencies_s = []
    orv.retries = 0
    orv._provider = None
    return orv


def test_retry_on_429_honors_retry_after_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": 1}'}}],
            "usage": {"cost": 0.0001},
        })

    orv = _mock_client(handler)
    reply, _ = orv.chat_json("m", "s", [{"role": "user", "content": "x"}])
    assert reply == {"ok": 1}
    assert calls["n"] == 2 and orv.retries == 1
    assert len(orv.latencies_s) == 1  # latency recorded for the successful call


def test_client_error_401_raises_immediately_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"code": 401, "message": "bad key"}})

    orv = _mock_client(handler)
    with pytest.raises(OpenRouterError, match="401"):
        orv.chat_json("m", "s", [{"role": "user", "content": "x"}])
    assert calls["n"] == 1  # never retried: retrying a bad key is wrong


def test_transport_error_retried_then_gives_up_bounded():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    orv = _mock_client(handler)
    orv.budget.max_attempts = 2
    orv.budget.backoff_s = [0.0]
    with pytest.raises(OpenRouterError, match="2 attempts"):
        orv.chat_json("m", "s", [{"role": "user", "content": "x"}])
    assert calls["n"] == 2


# ------------------------------------------------------------------- replay --

def _engine(tmp_path, page, art, budget):
    run = RunLog(tmp_path / "evidence", "budget-test")
    run.meta(mode="test")
    return ReplayEngine(page, art, Allowlist(CFG), run, allow_draft=True, budget=budget)


def test_replay_total_budget_breach_is_hard_failure(tmp_path):
    r = _engine(tmp_path, FakePage(), lookup_artifact(tmp_path),
                ReplayBudget(total_s=0.0)).run({"member_id": "1001"})
    assert r.status == "HARD_FAILURE"
    assert r.failure and "budget exceeded: total" in r.failure.observed
    # the envelope self-describes in the result contract
    assert r.budgets["in_effect"]["total_s"] == 0.0
    assert r.budgets["actuals"]["duration_ms"] == r.duration_ms


def test_result_carries_budget_block(tmp_path):
    r = _engine(tmp_path, FakePage(), lookup_artifact(tmp_path),
                Budgets().replay).run({"member_id": "1001"})
    assert r.status == "SUCCESS"
    assert set(r.budgets) == {"in_effect", "actuals"}
    assert r.budgets["in_effect"]["postcondition_wait_s"] == 3.0


def test_postcondition_wait_budget_flows_into_wait_loop(tmp_path):
    # Instant pages succeed under any wait budget; the assertion target is
    # the wiring itself (budget value visible in the result envelope).
    r = _engine(tmp_path, FakePage(), lookup_artifact(tmp_path),
                ReplayBudget(postcondition_wait_s=0.2)).run({"member_id": "1001"})
    assert r.status == "SUCCESS"
    assert r.budgets["in_effect"]["postcondition_wait_s"] == 0.2


# ----------------------------------------------------------------- discovery --

def test_discovery_cost_budget_stops_cleanly(tmp_path):
    from cua.agent import DiscoveryAgent
    from cua.openrouter import FakeLLM

    class CostlyLLM(FakeLLM):
        """Every call burns $1, so a $0.05 cap trips before step 2 —
        deterministically ahead of the stuck-detector (2 no-change steps)."""

        def __init__(self):
            super().__init__([{"action": "read", "reason": "looking"}] * 50)
            self.spent_usd = 0.0

        def chat_json(self, *a, **kw):
            out = super().chat_json(*a, **kw)
            self.spent_usd += 1.0
            return out

    run = RunLog(tmp_path / "evidence", "disc-budget")
    run.meta(mode="test")
    agent = DiscoveryAgent(
        FakePage(), CostlyLLM(), Allowlist(CFG), run, model="fake",
        max_steps=50, budget=DiscoveryBudget(max_cost_usd=0.05),
    )
    out = agent.run(
        "Look up member {member_id}", "http://127.0.0.1:8791/search",
        {"member_id": Param(name="member_id", type="string", example="1001")}, "t",
    )
    assert not out.ok
    assert "budget exceeded: cost" in out.reason
    assert out.llm_calls == 1  # stopped before the second call, ahead of stuck-detection


def test_discovery_llm_call_budget_stops_cleanly(tmp_path):
    from cua.agent import DiscoveryAgent
    from cua.openrouter import FakeLLM

    run = RunLog(tmp_path / "evidence", "disc-calls")
    run.meta(mode="test")
    agent = DiscoveryAgent(
        FakePage(), FakeLLM(list(SCRIPT)), Allowlist(CFG), run, model="fake",
        max_steps=50, budget=DiscoveryBudget(max_llm_calls=2),
    )
    out = agent.run(
        "Look up member {member_id}", "http://127.0.0.1:8791/search",
        {"member_id": Param(name="member_id", type="string", example="1001")}, "t",
    )
    assert not out.ok
    assert "budget exceeded: llm_calls" in out.reason
    assert out.llm_calls == 2
