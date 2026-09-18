"""Human-in-the-loop escalation and live-session handoff.

The mechanism is real even though the operator surface is deliberately bare
(the spec's scope note allows exactly this):

  1. Detect stuck/blocked (replay: hard failure after recovery attempts;
     discovery: no-progress rules + an optional Jev decision).
  2. Raise an intervention request: a JSON file carrying the context a human
     needs — capability/goal, failed step, expected vs observed, screenshot,
     and what the operator can do.
  3. Transfer control: automation pauses; the operator drives the SAME live
     session (same PageSurface) through the operator command protocol.
  4. Resume: control returns to automation, which retries the failed step
     against fresh state. Everything the operator did is captured as a
     before/after diff in the escalation record.

Control-state machine: AUTOMATION -> HUMAN -> AUTOMATION (or ABORTED).
Who holds control is a single field on the session, written on every
transition — that is the "who is in control" seam the spec asks to make
explicit.

Operator commands (the bare operator surface; ScriptedOperator replays what a
human would type, TerminalOperator reads stdin):

  goto <url> | click <role> '<name>' | fill <role> '<name>' <value> | look | resume | abort
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol

from .schema import EscalationRecord, Step
from .surface import PageSurface, Snapshot, resolve


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ControlSession:
    """Tracks who is in control of the live session. Transitions are events."""

    AUTOMATION = "AUTOMATION"
    HUMAN = "HUMAN"
    ABORTED = "ABORTED"

    def __init__(self) -> None:
        self.state = self.AUTOMATION
        self.transitions: list[dict] = []

    def to(self, new: str, why: str) -> None:
        self.transitions.append(
            {"from": self.state, "to": new, "why": why, "at": _now()}
        )
        self.state = new


class Operator(Protocol):
    def handle(self, surface: PageSurface, request: dict) -> EscalationRecord: ...


class ScriptedOperator:
    """Replays a scripted human intervention through the real command protocol."""

    def __init__(self, commands: list[str]):
        self.commands = commands

    def handle(self, surface: PageSurface, request: dict) -> EscalationRecord:
        actions: list[str] = []
        resumed = False
        for cmd in self.commands:
            actions.append(cmd)
            if cmd.strip() == "look":
                snap = surface.snapshot()
                actions.append(f"  -> url={snap.url}")
                continue
            if cmd.strip() == "resume":
                resumed = True
                break
            if cmd.strip() == "abort":
                break
            _run_operator_command(surface, cmd)
        return EscalationRecord(
            reason=request["reason"],
            requested_at=_now(),
            operator_actions=actions,
            resumed=resumed,
            intervention_file=request.get("intervention_file"),
        )


class TerminalOperator:
    """Interactive human operator: reads commands from stdin until resume/abort."""

    def handle(self, surface: PageSurface, request: dict) -> EscalationRecord:
        print(json.dumps(request, indent=2))
        print("operator> type commands (goto/click/fill/look/resume/abort):")
        actions: list[str] = []
        resumed = False
        while True:
            try:
                cmd = input("operator> ").strip()
            except EOFError:
                cmd = "abort"
            if not cmd:
                continue
            actions.append(cmd)
            if cmd == "resume":
                resumed = True
                break
            if cmd == "abort":
                break
            if cmd == "look":
                snap = surface.snapshot()
                actions.append(f"  -> url={snap.url}")
                continue
            try:
                _run_operator_command(surface, cmd)
            except Exception as exc:  # operator errors are recorded, not fatal
                actions.append(f"  ! error: {exc}")
        return EscalationRecord(
            reason=request["reason"],
            requested_at=_now(),
            operator_actions=actions,
            resumed=resumed,
            intervention_file=request.get("intervention_file"),
        )


def _run_operator_command(surface: PageSurface, cmd: str) -> None:
    import shlex

    from .schema import Locator

    parts = shlex.split(cmd)
    verb, args = parts[0], parts[1:]
    if verb == "goto":
        surface.goto(args[0])
    elif verb in ("click", "fill"):
        if len(args) < 2 or (verb == "fill" and len(args) < 3):
            extra = " <value>" if verb == "fill" else ""
            raise ValueError(f"syntax: {verb} <role> '<name>'" + extra)
        role, name = args[0], args[1]
        el = resolve(Locator(role=role, name=name), surface.snapshot())
        if el is None:
            raise ValueError(f"operator: no {role} {name!r} on screen")
        if verb == "click":
            surface.click(el)
        else:
            surface.fill(el, args[2])
    else:
        raise ValueError(f"unknown operator command {verb!r}")


def build_intervention(
    *,
    run_dir: str,
    capability: str,
    step: Step | None,
    reason: str,
    expected: str,
    observed: str,
    snapshot: Snapshot,
    screenshot: str | None,
) -> dict:
    from .safety import redact  # intervention files are persisted evidence

    req = {
        "capability": capability,
        "reason": reason,
        "failed_step": None
        if step is None
        else {"id": step.id, "action": step.action, "wait": step.wait and step.wait.describe()},
        "expected": expected,
        "observed": observed,
        "current_url": snapshot.url,
        "page_text_excerpt": redact(snapshot.text[:600]),
        "screenshot": screenshot,
        "operator_commands": "goto/click/fill/look/resume/abort",
        "requested_at": _now(),
        "intervention_file": f"{run_dir.rstrip('/')}/intervention.json",
    }
    return req


def write_intervention(request: dict) -> str:
    path = request["intervention_file"]
    with open(path, "w") as fh:
        json.dump(request, fh, indent=2)
    return path
