from cua.agent import DiscoveryAgent
from cua.evidence import RunLog
from cua.openrouter import FakeLLM
from cua.safety import Allowlist

from .fakepage import FakePage

CFG = {
    "origins": ["http://127.0.0.1:8791"],
    "routes": ["/", "/search", "/lookup", "/member/*"],
    "actions": ["goto", "click", "fill", "press_enter", "read"],
    "risky_policy": "block",
    "forbidden_elements": [
        {"role": "button", "name_contains": "freeze", "class": "irreversible"}
    ],
}

SCRIPT = [
    {"thought": "go", "action": "goto", "value": "http://127.0.0.1:8791/search",
     "reason": "entry"},
    {"thought": "type id", "action": "fill", "index": 1, "value": "{member_id}",
     "reason": "param"},
    {"thought": "search", "action": "click", "index": 2, "reason": "submit"},
    {"thought": "read", "action": "done", "verify_text": "Member Detail",
     "outputs": [{"name": "savings_balance", "pattern": r"Savings\s*\$([\d,]+\.\d{2})",
                  "group": 1}],
     "answer": "read from page"},
]


def discover(tmp_path, script=None, page=None):
    page = page or FakePage()
    run = RunLog(tmp_path / "evidence", "disc-test")
    run.meta(mode="discovery-test")
    agent = DiscoveryAgent(page, FakeLLM(script or SCRIPT), Allowlist(CFG), run,
                           model="fake-llm", max_steps=8)
    from cua.schema import Param

    spec = {"member_id": Param(name="member_id", type="string", example="1001")}
    return agent.run(
        "Look up member {member_id} and report their savings balance",
        "http://127.0.0.1:8791/search", spec, "disc-test",
    )


def test_discovery_compiles_parameterized_artifact(tmp_path):
    out = discover(tmp_path)
    assert out.ok, out.reason
    art = out.artifact
    assert art.inputs[0].name == "member_id"
    fill = [s for s in art.steps if s.action == "fill"][0]
    assert fill.value == "{member_id}"  # parameterized, not the literal
    click = [s for s in art.steps if s.action == "click"][0]
    assert click.target.role == "button" and click.target.name == "Search"
    assert click.wait.url_contains == "/member/{member_id}"
    assert art.checkpoint.text_contains == "Member Detail"
    assert art.status == "draft"  # approval is a separate, deliberate step


def test_discovery_deadline_stops_the_loop(tmp_path):
    page = FakePage()
    run = RunLog(tmp_path / "evidence", "disc-deadline")
    run.meta(mode="discovery-test")
    agent = DiscoveryAgent(page, FakeLLM(SCRIPT), Allowlist(CFG), run,
                           model="fake-llm", max_steps=50, deadline_s=0.0)
    from cua.schema import Param

    spec = {"member_id": Param(name="member_id", type="string", example="1001")}
    out = agent.run(
        "Look up member {member_id} and report their savings balance",
        "http://127.0.0.1:8791/search", spec, "disc-deadline",
    )
    assert not out.ok and "timeout" in out.reason


def test_done_rejected_when_verify_text_missing(tmp_path):
    script = SCRIPT[:3] + [{**SCRIPT[3], "verify_text": "PAGE THAT DOES NOT EXIST"}]
    out = discover(tmp_path, script)
    assert not out.ok and "verify_text" in out.reason


def test_done_rejected_when_output_regex_fails(tmp_path):
    script = SCRIPT[:3] + [
        {**SCRIPT[3], "outputs": [{"name": "x", "pattern": r"WontMatch(\d+)", "group": 1}]}
    ]
    out = discover(tmp_path, script)
    assert not out.ok and "does not match" in out.reason


def test_blocked_action_is_fed_back_to_model(tmp_path):
    script = [
        {"action": "goto", "value": "http://127.0.0.1:8791/member/1001", "reason": "detail"},
        {"action": "click", "index": 1, "reason": "try the risky freeze"},
        {"action": "goto", "value": "http://127.0.0.1:8791/search", "reason": "back"},
        *SCRIPT[1:],
    ]
    out = discover(tmp_path, script)
    assert out.ok  # recovered after the block
    blocked = [t for t in out.trace if t.blocked]
    assert blocked and blocked[0].guard == "BLOCKED"
    assert any("risky" in t.reason.lower() or "freeze" in t.reason.lower() for t in blocked)


def test_model_never_receives_raw_ssn(tmp_path):
    page = FakePage()
    run = RunLog("/tmp/opencode/evid-check", "prompt-check")
    run.meta(mode="test")
    agent = DiscoveryAgent(page, FakeLLM(SCRIPT), Allowlist(CFG), run, model="m")
    from cua.schema import Param

    agent.run("goal", "http://127.0.0.1:8791/search",
              {"member_id": Param(name="member_id", type="string", example="1001")}, "r")
    prompts = agent.llm.seen_prompts  # type: ignore[attr-defined]
    assert prompts, "no prompts captured"
    joined = "\n".join(prompts)
    assert "411-23-8891" not in joined  # SSN redacted before the model sees it
