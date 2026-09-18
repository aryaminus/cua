"""Browser-backed integration tests: real PlaywrightSurface + real Flask app.

These validate what the offline FakePage suite cannot: the JS element-table
extraction (especially adjacent-<td> label inference on the legacy markup),
locator resolution against a real DOM, and the full replay matrix end to end.
Slower than the unit suite; still fully local (no network, no keys).
"""

import threading

import pytest
from werkzeug.serving import make_server

from cua import mockapp
from cua.evidence import RunLog
from cua.replay import ReplayEngine
from cua.safety import Allowlist
from cua.schema import Artifact, Check, Locator, Outcome, Param, Step
from cua.surface import PlaywrightSurface, resolve

from .conftest import CFG, OUTCOMES  # allowlist cfg + outcome fixtures shared

srv_holder: dict = {}


@pytest.fixture(scope="module")
def server():
    mockapp.reset_state()
    srv = make_server("127.0.0.1", 8792, mockapp.app, threaded=False)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:8792"
    srv.shutdown()


@pytest.fixture(scope="module")
def surface(server):
    s = PlaywrightSurface(server + "/search")
    yield s
    s.close()


APP = "http://127.0.0.1:8792"


def test_snapshot_infers_legacy_label_from_adjacent_cell(surface):
    snap = surface.snapshot()
    box = [e for e in snap.elements if e.role == "textbox"]
    assert len(box) == 1, [e.model_dump() for e in snap.elements]
    # no <label for=...>, no placeholder — name must come from the adjacent td
    assert box[0].name == "Member ID"
    btn = [e for e in snap.elements if e.role == "button"]
    assert any(b.name == "Search" for b in btn)


def test_snapshot_menu_clicktexts_on_home(surface):
    surface.goto(APP + "/")
    snap = surface.snapshot()
    clicks = [e for e in snap.elements if e.role == "clicktext"]
    assert {e.name for e in clicks} >= {"Member Lookup", "Reports", "Administration"}


def test_snapshot_member_page_and_redaction_ready_text(surface):
    surface.goto(APP + "/member/1001")
    snap = surface.snapshot()
    assert "Member Detail" in snap.text and "ALVAREZ" in snap.text
    assert "411-23-8891" in snap.text  # raw on the page; redaction happens at persistence
    assert any(e.role == "button" and "Freeze" in e.name for e in snap.elements)


def test_fill_and_click_drive_real_form(surface):
    surface.goto(APP + "/search")
    snap = surface.snapshot()
    from cua.surface import Locator as L

    box = resolve(L(role="textbox", name="Member ID"), snap)
    surface.fill(box, "1002")
    snap = surface.snapshot()
    btn = resolve(L(role="button", name="Search"), snap)
    surface.click(btn)
    snap = surface.snapshot()
    assert "/member/1002" in snap.url and "OKAFOR" in snap.text


def art(tmp_path) -> Artifact:
    return Artifact(
        capability_name="lookup_member",
        description="integration",
        app="mock",
        entry_url=APP + "/search",
        inputs=[Param(name="member_id", type="string", example="1001")],
        outputs=[{"name": "savings_balance",
                  "extract": {"pattern": r"Savings\s*\$([\d,]+\.\d{2})", "group": 1}}],
        steps=[
            Step(id=1, action="goto", value=APP + "/search",
                 wait=Check(url_contains="/search")),
            Step(id=2, action="fill", value="{member_id}",
                 target=Locator(role="textbox", name="Member ID",
                                fallbacks=[{"kind": "tag_ordinal", "tag": "input", "ordinal": 0}])),
            Step(id=3, action="click", target=Locator(role="button", name="Search"),
                 wait=Check(url_contains="/member/{member_id}")),
        ],
        checkpoint=Check(text_contains="Member Detail"),
        outcomes=OUTCOMES + [
            Outcome(
                id="PERMISSION_DENIED",
                detect=Check(text_contains="Access denied"),
                returns={"message": "Access denied for member ID {member_id}"},
            )
        ],
        status="approved",
        provenance={"run_id": "t", "recorded_at": "2026", "model": "t"},
    )


def _engine(tmp_path, page, artifact):
    run = RunLog(tmp_path / "evidence", "it")
    run.meta(mode="integration")
    return ReplayEngine(page, artifact, Allowlist(CFG), run)


def test_replay_success_real_browser(tmp_path, surface):
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1001"})
    assert r.status == "SUCCESS"
    assert r.outputs["savings_balance"] == "2,451.90"


def test_replay_not_found_real_browser(tmp_path, surface):
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "9999"})
    assert r.status == "BUSINESS_OUTCOME" and r.outcome_id == "NOT_FOUND"
    assert r.outputs["message"] == "No member found for ID 9999"


def test_replay_server_error_real_browser(tmp_path, surface, monkeypatch):
    monkeypatch.setenv("MOCKAPP_FAULT", "server_error_member_1003")
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1003"})
    monkeypatch.delenv("MOCKAPP_SESSION_TTL", raising=False)
    monkeypatch.delenv("MOCKAPP_FAULT", raising=False)
    assert r.status == "HARD_FAILURE"
    assert r.failure.screenshot  # failure screenshot captured from the live page


def test_replay_parametrized_real_browser(tmp_path, surface):
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1002"})
    assert r.status == "SUCCESS" and r.outputs["savings_balance"] == "19,340.00"


def test_replay_permission_denied_real_browser(tmp_path, surface):
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1005"})
    assert r.status == "BUSINESS_OUTCOME" and r.outcome_id == "PERMISSION_DENIED"
    assert "1005" in r.outputs["message"]


def test_replay_transient_busy_reload_real_browser(tmp_path, surface, monkeypatch):
    monkeypatch.setenv("MOCKAPP_BUSY_MEMBER", "1002")
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1002"})
    monkeypatch.delenv("MOCKAPP_BUSY_MEMBER", raising=False)
    assert r.status == "SUCCESS"
    assert any(rec.condition == "transient_reload" for rec in r.recoveries)


def test_replay_slow_response_absorbed_by_waits(tmp_path, surface, monkeypatch):
    monkeypatch.setenv("MOCKAPP_SLOW_MEMBER", "1006")
    monkeypatch.setenv("MOCKAPP_SLOW_SECONDS", "1.0")
    mockapp.reset_state()
    r = _engine(tmp_path, surface, art(tmp_path)).run({"member_id": "1006"})
    monkeypatch.delenv("MOCKAPP_SLOW_MEMBER", raising=False)
    monkeypatch.delenv("MOCKAPP_SLOW_SECONDS", raising=False)
    assert r.status == "SUCCESS"
