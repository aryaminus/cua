"""Capability artifact schema (v1) and the replay result contract.

The artifact is the center of the system: a typed, versioned, reviewable
description of a recorded flow, decoupled from the model transcript that
produced it. An AI agent (or a human operator) should be able to read an
artifact and know exactly what the capability does, what it needs, and what it
returns — without reading any LLM output.

Design notes (rationale is expanded in REPORT.md):

- Locators reference the *normalized element model* (role + accessible name)
  with explicit fallbacks, not raw CSS/XPath. Same shape works for a clean web
  DOM, a frameset-era page, or an accessibility tree on a desktop app — that is
  the surface seam (brief §3.7).
- Steps are state-changing actions only (goto / click / fill / press_enter).
  Observations are not steps: replay re-observes fresh at every step instead of
  trusting recorded screen state.
- ``wait`` on a step is its post-condition: the observable change that tells us
  the action landed. Checkpoints assert state, never action completion.
- Business outcomes ("no such member") are declared by the artifact with detect
  conditions; recoverable conditions (dialogs, transient loads) are engine
  policy; anything else is a hard failure. Conflating these is the classic
  mistake the spec warns about.
- ``value`` / detect patterns may reference ``{param}`` names, resolved from
  ``inputs`` at replay time — that is what makes a recording a *parameterized
  capability* rather than a scripted demo.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SchemaVersion = Literal["1.0"]


# ------------------------------------------------------------------ locators --

class Locator(BaseModel):
    """How a target control is identified on a surface.

    ``role`` + ``name`` mirror what an accessibility tree would expose; they
    are primary because they survive markup changes that preserve meaning.
    Fallbacks trade precision for reach, in order.
    """

    role: str = Field(description="element role: link|button|textbox|clicktext|...")
    name: str = Field(description="accessible-name approximation: label/placeholder/adjacent text")
    fallbacks: list[dict] = Field(
        default_factory=list,
        description="ordered fallbacks, e.g. {'kind':'tag_ordinal','tag':'input','ordinal':0}",
    )


# -------------------------------------------------------------- conditions --

class Check(BaseModel):
    """A state assertion. Exactly one field is set (validated below).

    ``text_contains`` is preferred over url matching for legacy apps where
    routes are unstable but screens are not. ``{param}`` refs are allowed.
    """

    url_contains: str | None = None
    text_contains: str | None = None
    element_present: Locator | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Check:
        set_fields = [
            f for f in ("url_contains", "text_contains", "element_present") if getattr(self, f)
        ]
        if len(set_fields) != 1:
            raise ValueError(f"Check must set exactly one condition, got {set_fields}")
        return self

    def describe(self) -> str:
        if self.url_contains:
            return f"url contains {self.url_contains!r}"
        if self.text_contains:
            return f"page shows {self.text_contains!r}"
        return f"element present: {self.element_present.role} {self.element_present.name!r}"


# ------------------------------------------------------------------- steps --

Action = Literal["goto", "click", "fill", "press_enter", "read"]


class Step(BaseModel):
    id: int = Field(ge=1)
    action: Action
    target: Locator | None = None  # None for goto / page-level press_enter
    value: str | None = None  # fill text or goto url; may contain {param} refs
    wait: Check | None = None  # post-condition asserting the action landed
    rationale: str = ""  # why this step exists (from discovery; reviewability)

    @model_validator(mode="after")
    def action_shape(self) -> Step:
        if self.action == "goto":
            if not self.value:
                raise ValueError("goto needs value=url")
            if self.target:
                raise ValueError("goto takes no target")
        if self.action == "fill" and (not self.target or self.value is None):
            raise ValueError("fill needs target and value")
        if self.action == "click" and not self.target:
            raise ValueError("click needs target")
        return self


# -------------------------------------------------------- inputs and outputs --

class Param(BaseModel):
    name: str
    type: Literal["string", "integer"]
    required: bool = True
    description: str = ""
    example: str = ""


class Extract(BaseModel):
    kind: Literal["regex"] = "regex"
    pattern: str
    group: int = 1


class Output(BaseModel):
    name: str
    type: Literal["string", "number"] = "string"
    description: str = ""
    extract: Extract


class Outcome(BaseModel):
    """A declared *expected business outcome* — a legitimate answer, not a crash.

    E.g. NOT_FOUND: detect via page text, return a message to the caller.
    """

    id: str
    description: str = ""
    detect: Check
    returns: dict[str, str] = Field(
        default_factory=dict,
        description="output templates, may contain {param} refs, e.g. message",
    )


# ----------------------------------------------------------------- artifact --

class Provenance(BaseModel):
    run_id: str
    recorded_at: str  # ISO-8601
    model: str
    decisions_model: str | None = None  # e.g. typesafe/jev-1.13, if used
    steps_llm_calls: int = 0
    cost_usd: float | None = None


class Artifact(BaseModel):
    schema_version: SchemaVersion = "1.0"
    capability_name: str
    description: str
    app: str  # tenant/app identity the artifact was recorded against
    entry_url: str
    status: Literal["draft", "approved"] = "draft"
    inputs: list[Param]
    outputs: list[Output]
    steps: list[Step]
    checkpoint: Check  # success condition on the final state
    outcomes: list[Outcome] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def consistency(self) -> Artifact:
        ids = [s.id for s in self.steps]
        if ids != list(range(1, len(self.steps) + 1)):
            raise ValueError(f"step ids must be 1..n in order, got {ids}")
        declared = {p.name for p in self.inputs}
        pattern = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
        refs: set[str] = set()
        for s in self.steps:
            if s.value:
                refs |= set(pattern.findall(s.value))
            if s.wait:
                for f in ("url_contains", "text_contains"):
                    v = getattr(s.wait, f)
                    if v:
                        refs |= set(pattern.findall(v))
        for o in self.outcomes:
            v = o.detect.text_contains or o.detect.url_contains or ""
            refs |= set(pattern.findall(v))
            for t in o.returns.values():
                refs |= set(pattern.findall(t))
        unknown = refs - declared
        if unknown:
            raise ValueError(f"steps/outcomes reference undeclared params: {sorted(unknown)}")
        return self

    def resolve_value(self, text: str | None, params: dict[str, str]) -> str:
        if text is None:
            return ""
        out = text
        for k, v in params.items():
            out = out.replace("{" + k + "}", str(v))
        leftovers = re.findall(r"\{[a-z_][a-z0-9_]*\}", out)
        if leftovers:
            raise ValueError(f"unresolved params {leftovers} in {text!r}")
        return out


# ------------------------------------------------------------ replay results --

ResultStatus = Literal["SUCCESS", "BUSINESS_OUTCOME", "HARD_FAILURE", "ESCALATED"]


class Recovery(BaseModel):
    step_id: int
    condition: str  # e.g. "unexpected_dialog", "locator_rematch"
    action_taken: str


class Failure(BaseModel):
    step_id: int
    action: str
    expected: str
    observed: str
    screenshot: str | None = None


class EscalationRecord(BaseModel):
    reason: str
    requested_at: str
    operator_actions: list[str] = Field(default_factory=list)
    resumed: bool = False
    intervention_file: str | None = None


class ReplayResult(BaseModel):
    """The closed result contract callers receive (brief §3.3):

    - SUCCESS: checkpoint passed; declared outputs extracted and returned.
    - BUSINESS_OUTCOME: a declared outcome matched (e.g. NOT_FOUND) — a
      legitimate answer the caller needs, with its ``returns`` payload.
    - HARD_FAILURE: stop; debuggable detail (what step, expected vs observed).
    - ESCALATED: hard failure routed to a human; the run handed over control of
      the live session and (possibly) resumed after intervention.
    """

    artifact: str
    params_redacted: dict[str, str]
    status: ResultStatus
    outputs: dict[str, str] = Field(default_factory=dict)
    outcome_id: str | None = None
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    escalation: EscalationRecord | None = None
    steps_executed: int = 0
    duration_ms: int = 0

    def summarize(self) -> str:
        parts = [f"{self.status}"]
        if self.outcome_id:
            parts.append(f"outcome={self.outcome_id}")
        if self.outputs:
            parts.append(f"outputs={sorted(self.outputs)}")
        if self.failure:
            parts.append(f"failed_step={self.failure.step_id}")
        if self.recoveries:
            parts.append(f"recoveries={len(self.recoveries)}")
        return " ".join(parts)
