import json

import pytest

from cua.evidence import RunLog
from cua.replay import ReplayEngine
from cua.safety import Allowlist
from cua.schema import Artifact, Check, Locator, Outcome, Param, Step

from .conftest import CFG, OUTCOMES
from .fakepage import FakePage


def lookup_artifact(tmp_path, *, outcomes=OUTCOMES, status="approved") -> Artifact:
    art = Artifact(
        capability_name="lookup_member",
        description="Look up a member and read their savings balance",
        app="mock",
        entry_url="http://127.0.0.1:8791/search",
        status=status,
        inputs=[Param(name="member_id", type="string", example="1001")],
        outputs=[{
            "name": "savings_balance",
            "extract": {"pattern": r"Savings\s*\$([\d,]+\.\d{2})", "group": 1},
        }],
        steps=[
            Step(id=1, action="goto", value="http://127.0.0.1:8791/search",
                 wait=Check(url_contains="/search"),
                 rationale="entry point"),
            Step(id=2, action="fill", value="{member_id}",
                 target=Locator(role="textbox", name="Member ID",
                                fallbacks=[{"kind": "tag_ordinal", "tag": "input", "ordinal": 0}]),
                 rationale="the member id parameter"),
            Step(id=3, action="click", target=Locator(role="button", name="Search"),
                 wait=Check(url_contains="/member/"),
                 rationale="submit the lookup"),
        ],
        checkpoint=Check(text_contains="Member Detail"),
        outcomes=outcomes,
        provenance={"run_id": "t", "recorded_at": "2026-01-01T00:00:00Z", "model": "t"},
    )
    return art


def engine(tmp_path, page: FakePage, art: Artifact, operator=None, allow_draft=False):
    run = RunLog(tmp_path / "evidence", "test-run")
    run.meta(mode="test")
    return ReplayEngine(page, art, Allowlist(CFG), run,
                        operator=operator, allow_draft=allow_draft)


def test_success_extracts_outputs(tmp_path):
    r = engine(tmp_path, FakePage(), lookup_artifact(tmp_path)).run({"member_id": "1001"})
    assert r.status == "SUCCESS"
    assert r.outputs["savings_balance"] == "2,451.90"
    assert r.steps_executed == 3


def test_parametrization_same_artifact_different_member(tmp_path):
    r = engine(tmp_path, FakePage(), lookup_artifact(tmp_path)).run({"member_id": "1002"})
    assert r.status == "SUCCESS"
    assert r.outputs["savings_balance"] == "19,340.00"


def test_business_outcome_not_found_is_not_a_failure(tmp_path):
    r = engine(tmp_path, FakePage(), lookup_artifact(tmp_path)).run({"member_id": "9999"})
    assert r.status == "BUSINESS_OUTCOME"
    assert r.outcome_id == "NOT_FOUND"
    assert r.outputs["message"] == "No member found for ID 9999"
    assert r.failure is None


def test_business_outcome_invalid_input(tmp_path):
    r = engine(tmp_path, FakePage(), lookup_artifact(tmp_path)).run({"member_id": "40a"})
    assert r.status == "BUSINESS_OUTCOME" and r.outcome_id == "INVALID_INPUT"


def test_business_outcome_permission_denied_on_frozen_member(tmp_path):
    art = lookup_artifact(tmp_path)
    art.outcomes = art.outcomes + [Outcome(
        id="PERMISSION_DENIED", detect=Check(text_contains="Access denied"),
        returns={"message": "Access denied for member ID {member_id}: frozen record"},
    )]
    r = engine(tmp_path, FakePage(), art).run({"member_id": "1005"})
    assert r.status == "BUSINESS_OUTCOME" and r.outcome_id == "PERMISSION_DENIED"
    assert r.outputs["message"] == "Access denied for member ID 1005: frozen record"


def test_transient_busy_reloaded_once_and_succeeds(tmp_path):
    r = engine(tmp_path, FakePage(busy_once=True), lookup_artifact(tmp_path)).run(
        {"member_id": "1002"}
    )
    assert r.status == "SUCCESS"
    assert r.outputs["savings_balance"] == "19,340.00"
    assert any(rec.condition == "transient_reload" for rec in r.recoveries)


def test_hard_failure_on_server_error_has_debug_detail(tmp_path):
    page = FakePage(fault="server_error_member_1003")
    r = engine(tmp_path, page, lookup_artifact(tmp_path)).run({"member_id": "1003"})
    assert r.status == "HARD_FAILURE"
    assert r.failure is not None
    assert "checkpoint" in r.failure.expected
    assert r.failure.screenshot  # richer failure signal captured


def test_draft_artifact_requires_flag_or_approval(tmp_path):
    with pytest.raises(ValueError, match="approved"):
        engine(tmp_path, FakePage(),
               lookup_artifact(tmp_path, status="draft")).run({"member_id": "1001"})
    r = engine(tmp_path, FakePage(), lookup_artifact(tmp_path, status="draft"),
               allow_draft=True).run({"member_id": "1001"})
    assert r.status == "SUCCESS"


def test_risky_control_blocked_by_guard(tmp_path):
    art = lookup_artifact(tmp_path)
    art.steps = art.steps + [Step(
        id=4, action="click",
        target=Locator(role="button", name="Freeze Accounts"),
        rationale="should never run")]
    r = engine(tmp_path, FakePage(), art, allow_draft=True).run({"member_id": "1001"})
    assert r.status == "HARD_FAILURE"
    assert "Freeze" in r.failure.observed and "risky" in r.failure.observed.lower()


def test_unexpected_dialog_recorded_as_recovery(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg["forbidden_elements"] = []  # permit the freeze click so the dialog fires
    art = lookup_artifact(tmp_path)
    art.entry_url = "http://127.0.0.1:8791/member/1001"
    art.steps = [Step(id=1, action="click",
                      target=Locator(role="button", name="Freeze Accounts"))]
    art.checkpoint = Check(text_contains="Member Detail")  # still on detail page
    page = FakePage()
    run = RunLog(tmp_path / "evidence", "dialog-run")
    run.meta(mode="test")
    eng = ReplayEngine(page, art, Allowlist(cfg), run, allow_draft=True)
    r = eng.run({"member_id": "1001"})
    # the confirm() dialog was dismissed automatically and recorded — the run
    # did NOT silently proceed past an unexpected modal it never accepted
    assert any(rec.condition == "unexpected_dialog" for rec in r.recoveries)
    assert "Freeze all accounts" in r.recoveries[0].action_taken


def test_locator_fallback_rescues_changed_markup(tmp_path):
    art = lookup_artifact(tmp_path)
    # wrong accessible name; tag_ordinal fallback still finds the only textbox
    art.steps[1].target = Locator(role="textbox", name="Completely Wrong",
                                  fallbacks=[{"kind": "tag_ordinal", "tag": "input", "ordinal": 0}])
    r = engine(tmp_path, FakePage(), art).run({"member_id": "1001"})
    assert r.status == "SUCCESS" and r.outputs["savings_balance"] == "2,451.90"


def test_escalation_handoff_resumes_and_succeeds(tmp_path):
    from cua.escalation import ScriptedOperator

    page = FakePage(session_ttl=2)
    operator = ScriptedOperator([
        "look",
        "goto http://127.0.0.1:8791/",
        "goto http://127.0.0.1:8791/search",
        "fill textbox 'Member ID' 1002",
        "click button 'Search'",
        "look",
        "resume",
    ])
    r = engine(tmp_path, page, lookup_artifact(tmp_path),
               operator=operator).run({"member_id": "1002"})
    assert r.status == "SUCCESS", r.failure
    assert r.escalation is not None and r.escalation.resumed
    assert any("control-transitions" in a for a in r.escalation.operator_actions)
    assert r.outputs["savings_balance"] == "19,340.00"
    assert any(rec.condition == "operator_handoff" for rec in r.recoveries)

def test_escalation_aborted_by_operator(tmp_path):
    from cua.escalation import ScriptedOperator

    page = FakePage(session_ttl=2)
    r = engine(tmp_path, page, lookup_artifact(tmp_path),
               operator=ScriptedOperator(["abort"])).run({"member_id": "1002"})
    assert r.status == "ESCALATED"
    assert r.escalation is not None and not r.escalation.resumed


def test_operator_commands_are_allowlisted(tmp_path):
    from cua.escalation import _run_operator_command

    page = FakePage()
    with __import__("pytest").raises(ValueError, match="allowlist"):
        _run_operator_command(page, "goto http://evil.example/phish")
    with __import__("pytest").raises(ValueError, match="allowlist"):
        _run_operator_command(page, "click button 'Freeze Accounts'")
    # the same freeze the discovery model attempted live stays refused here too
    assert page.click_log == []


def test_determinism_two_runs_identical_signatures(tmp_path):
    art = lookup_artifact(tmp_path)
    r1 = engine(tmp_path, FakePage(), art).run({"member_id": "1001"})
    r2 = engine(tmp_path, FakePage(), art).run({"member_id": "1001"})
    assert r1.summarize() == r2.summarize()
    assert r1.outputs == r2.outputs


def test_evidence_redacted_no_ssn_in_run_dir(tmp_path):
    page = FakePage()
    engine(tmp_path, page, lookup_artifact(tmp_path)).run({"member_id": "1001"})
    result_json = (tmp_path / "evidence" / "test-run" / "result.json").read_text()
    assert "411-23-8891" not in result_json
    assert "[REDACTED" in result_json  # balance redacted in evidence
