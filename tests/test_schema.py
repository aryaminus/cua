
import pytest

from cua.schema import Artifact, Check, Locator, Outcome, Param, Step
from cua.surface import Element, Snapshot, locator_for, resolve

from .fakepage import FakePage

# --------------------------------------------------------------------- schema

def _artifact(**over) -> Artifact:
    base = dict(
        capability_name="lookup_member",
        description="test",
        app="mock",
        entry_url="http://127.0.0.1:8791/search",
        inputs=[Param(name="member_id", type="string", example="1001")],
        outputs=[],
        steps=[
            Step(id=1, action="fill", value="{member_id}",
                 target=Locator(role="textbox", name="Member ID")),
            Step(id=2, action="click", target=Locator(role="button", name="Search")),
        ],
        checkpoint=Check(text_contains="Member Detail"),
        provenance={"run_id": "t", "recorded_at": "2026-01-01T00:00:00Z", "model": "t"},
    )
    base.update(over)
    return Artifact(**base)


def test_step_ids_must_be_sequential():
    with pytest.raises(ValueError, match="step ids"):
        _artifact(steps=[Step(id=2, action="click", target=Locator(role="button", name="Search"))])


def test_undeclared_param_refs_rejected():
    bad = Step(id=1, action="fill", value="{nope}",
               target=Locator(role="textbox", name="Member ID"))
    with pytest.raises(ValueError, match="undeclared params"):
        _artifact(steps=[bad])


def test_check_exactly_one_condition():
    with pytest.raises(ValueError, match="exactly one"):
        Check(url_contains="/x", text_contains="y")
    with pytest.raises(ValueError, match="exactly one"):
        Check()


def test_step_action_shape_validation():
    with pytest.raises(ValueError):
        Step(id=1, action="goto", value=None)
    with pytest.raises(ValueError):
        Step(id=1, action="fill", value="x")  # no target


def test_resolve_value_substitution_and_strictness():
    art = _artifact()
    assert art.resolve_value("ID {member_id}", {"member_id": "42"}) == "ID 42"
    with pytest.raises(ValueError, match="unresolved"):
        art.resolve_value("{other}", {})


def test_outcome_returns_parametrized():
    art = _artifact(outcomes=[Outcome(
        id="NOT_FOUND", detect=Check(text_contains="No member found"),
        returns={"message": "No member found for ID {member_id}"},
    )])
    # round-trip through JSON like the CLI does
    parsed = Artifact.model_validate_json(art.model_dump_json())
    assert parsed.outcomes[0].returns["message"].endswith("{member_id}")


# -------------------------------------------------------------------- surface

def test_locator_resolution_exact_then_contains():
    page = FakePage()
    page.goto("http://127.0.0.1:8791/search")
    snap = page.snapshot()
    el = resolve(Locator(role="textbox", name="Member ID"), snap)
    assert el is not None and el.index == 1
    el = resolve(Locator(role="textbox", name="member id "), snap)  # case/space
    assert el is not None
    el = resolve(Locator(role="button", name="Sea"), snap)  # contains
    assert el is not None and el.name == "Search"


def test_locator_fallback_to_tag_ordinal():
    snap = Snapshot(url="x", text="t", elements=[
        Element(index=0, role="textbox", name="", tag="input"),
        Element(index=1, role="textbox", name="", tag="input"),
    ])
    loc = Locator(role="textbox", name="Member ID",
                  fallbacks=[{"kind": "tag_ordinal", "tag": "input", "ordinal": 1}])
    assert resolve(loc, snap).index == 1


def test_locator_unresolvable_returns_none():
    snap = Snapshot(url="x", text="t", elements=[])
    assert resolve(Locator(role="button", name="Search"), snap) is None


def test_locator_for_element_has_fallbacks():
    loc = locator_for(Element(index=2, role="button", name="Search", tag="input"))
    assert loc.role == "button" and loc.name == "Search"
    assert loc.fallbacks[0]["kind"] == "tag_ordinal"
