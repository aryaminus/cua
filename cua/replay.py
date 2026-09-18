"""Deterministic replay — the production execution path. No LLM in the loop.

Given an approved artifact and input parameters, executes the recorded steps
against a live surface and returns a typed result. Design:

- Locators re-resolve against a FRESH snapshot at every step — no stored DOM
  references across steps, so replay survives page identity churn between
  steps.
- Every step asserts its post-condition (``wait``) with a bounded poll —
  never a fixed sleep. Checkpoints assert state, not action completion.
- Outcome detection runs before failure classification at every step: a
  declared business outcome ("No member found for ID 9999") is a legitimate
  answer the caller needs and short-circuits with BUSINESS_OUTCOME.
- Recoverable conditions are engine policy and are recorded, never silent:
  unexpected JS dialogs (auto-dismissed by the surface), a locator that
  resolved only after a re-snapshot.
- Everything else is a hard failure: app errors, session-expiry screens,
  unresolvable locators, failed post-conditions, failed checkpoint. With an
  operator attached, hard failures route through the escalation handoff; the
  run continues from the failed step once the operator resumes control.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from .evidence import RunLog
from .safety import Allowlist, redact_text
from .schema import Artifact, Check, Failure, Recovery, ReplayResult, Step
from .surface import Element, PageSurface, Snapshot, StaleElementError, resolve

WAIT_TIMEOUT_S = 3.0
POLL_S = 0.1


class _HardFailure(Exception):
    def __init__(self, failure: Failure, step: Step | None):
        self.failure = failure
        self.step = step
        super().__init__(f"step {failure.step_id}: {failure.observed}")


class ReplayEngine:
    def __init__(
        self,
        surface: PageSurface,
        artifact: Artifact,
        allowlist: Allowlist,
        run: RunLog,
        *,
        operator=None,
        allow_draft: bool = False,
    ):
        if artifact.status != "approved" and not allow_draft:
            raise ValueError(
                f"artifact {artifact.capability_name!r} is {artifact.status!r}; "
                "unattended replay requires status=approved (or pass --allow-draft)"
            )
        self.surface = surface
        self.art = artifact
        self.allow = allowlist
        self.elog = run
        self.operator = operator
        self.recoveries: list[Recovery] = []
        self.steps_executed = 0
        # Engine-known transient markers (TEXT, from config — see allowlist.json
        # transient_markers): pages that mean "try the same view again", never
        # model decisions and never artifact content.
        raw_allow = getattr(allowlist, "raw", None)
        self._transient_markers: list[str] = list(
            (raw_allow or {}).get("transient_markers", [])
        )

    # ------------------------------------------------------------------ public

    def run(self, params: dict[str, str]) -> ReplayResult:
        started = time.monotonic()
        escalation = None
        v = self.allow.check_url(self.art.entry_url)
        if not v.allowed:
            failure = Failure(step_id=0, action="goto", expected="entry URL allowed",
                              observed=v.reason)
            self.elog.line(f"HARD FAILURE entry: {v.reason}")
            result = self._result("HARD_FAILURE", params, started, failure=failure)
            self.elog.result(result.model_dump())
            self.elog.finish(status=result.status, steps_executed=self.steps_executed)
            return result
        self.surface.goto(self.art.entry_url)  # every replay starts at the declared entry
        try:
            result = self._execute_steps(self.art.steps, params, started)
        except _HardFailure as hf:
            self.elog.line(f"HARD FAILURE step={hf.failure.step_id}: {hf.failure.observed}")
            if self.operator is None:
                result = self._result("HARD_FAILURE", params, started, failure=hf.failure)
            else:
                escalation = self._escalate(hf)
                if not escalation.resumed:
                    result = self._result(
                        "ESCALATED", params, started, failure=hf.failure, escalation=escalation
                    )
                else:
                    try:
                        # Rewind only when needed: if the failed step's wait does
                        # not hold yet, re-establish the declared entry state
                        # (the operator may have left the session on a terminal
                        # page); otherwise the operator already fixed the state.
                        remaining = self._remaining_after(hf.step)
                        if hf.step is not None and hf.step.wait is not None:
                            snap = self.surface.snapshot()
                            if not self._resume_wait_holds(hf.step.wait, snap, params):
                                self.surface.goto(self.art.entry_url)
                        result = self._execute_steps(
                            remaining, params, started, escalation, resuming=True
                        )
                        self._record_recovery(
                            hf.step or self.art.steps[0],
                            "operator_handoff",
                            f"resumed after intervention ({len(escalation.operator_actions)} ops)",
                        )
                        result.recoveries = list(self.recoveries)
                    except _HardFailure as hf2:
                        result = self._result(
                            "HARD_FAILURE",
                            params,
                            started,
                            failure=hf2.failure,
                            escalation=escalation,
                        )
        self.elog.result(result.model_dump())
        self.elog.finish(status=result.status, steps_executed=self.steps_executed)
        return result

    def _remaining_after(self, step: Step | None) -> list[Step]:
        """Steps to re-execute after a handoff. Step ids start at 1; a final
        checkpoint failure carries ``step=None`` (id 0), so every step re-runs."""
        anchor = step.id if step is not None else 0
        return [s for s in self.art.steps if s.id > anchor]

    # ------------------------------------------------------------------ engine

    def _execute_steps(
        self, steps: list[Step], params: dict[str, str], started: float,
        escalation=None, resuming: bool = False,
    ) -> ReplayResult:
        for step in steps:
            if resuming and step.wait is not None:
                # After a handoff, the operator may have completed the failed
                # step itself. A step whose post-condition already holds (and
                # shows no business outcome) is done — skip it instead of
                # re-executing steps onto a state that has moved on.
                # Legacy URL-substring waits are fuzzy by design (they carry
                # {params} and are compiled from observed paths), so the skip
                # must be segment-aware: e.g. "/member/" must not match
                # "/search". Keep `in` for exact hits; otherwise require the
                # key's alphabetic core to appear in a path segment.
                pre = self._settle()
                if self._resume_wait_holds(step.wait, pre, params) and not self._check_outcomes(
                    pre, params
                ):
                    self._record_recovery(
                        step, "skipped_postcondition_held",
                        "step already satisfied after operator handoff",
                    )
                    continue
            self._execute(step, params)  # includes post-conditions + outcome-aware waits
            self.steps_executed += 1
            snap = self._settle()
            outcome = self._check_outcomes(snap, params)
            if outcome:
                return self._result(
                    "BUSINESS_OUTCOME", params, started, outcome_id=outcome, escalation=escalation
                )
            self.elog.step(
                self.steps_executed,
                {
                    "step_id": step.id,
                    "action": step.action,
                    "target": step.target and step.target.model_dump(),
                    "value": step.value,
                    "post_url": snap.url,
                },
            )
        final = self._settle()
        outcome = self._check_outcomes(final, params)
        if outcome:
            return self._result(
                "BUSINESS_OUTCOME", params, started, outcome_id=outcome, escalation=escalation
            )
        if not self._check(self.art.checkpoint, final, params):
            raise self._hard(
                None, f"checkpoint: {self.art.checkpoint.describe()}",
                "final state: " + redact_text(final.text.strip().replace("\n", " | "))[:200],
                screenshot=True,
            )
        outputs = self._extract()
        return self._result(
            "SUCCESS", params, started, outputs=outputs, escalation=escalation
        )

    def _execute(self, step: Step, params: dict[str, str]) -> None:
        snap = self.surface.snapshot()
        target = None
        if step.target is not None:
            target = self._resolve(step, snap)
        self._guard(step, target)
        try:
            if step.action == "goto":
                self.surface.goto(self.art.resolve_value(step.value, params))
            elif step.action == "click":
                assert target is not None
                self.surface.click(target)
            elif step.action == "fill":
                assert target is not None
                self.surface.fill(target, self.art.resolve_value(step.value, params))
            elif step.action == "press_enter":
                self.surface.press_enter(target)
            elif step.action == "read":
                pass  # observation-only: re-snapshot below refreshes state
        except _HardFailure:
            raise
        except StaleElementError as exc:
            raise self._hard(
                step, "target still valid at act time", f"stale element: {exc}",
                screenshot=True,
            ) from exc
        except Exception as exc:
            raise self._hard(
                step, f"{step.action} executed", f"driver error: {exc}", screenshot=True
            ) from exc
        self._post_conditions(step, params)

    def _resolve(self, step: Step, snap: Snapshot) -> Element:
        el = resolve(step.target, snap)  # type: ignore[arg-type]
        if el is not None:
            return el
        time.sleep(POLL_S)
        snap2 = self.surface.snapshot()
        el = resolve(step.target, snap2)  # type: ignore[arg-type]
        if el is not None:
            self._record_recovery(step, "locator_rematch", "resolved after re-snapshot")
            return el
        raise self._hard(
            step,
            f"target {step.target.role} {step.target.name!r} present",  # type: ignore[union-attr]
            "not found on fresh snapshot",
            screenshot=True,
        )

    def _post_conditions(self, step: Step, params: dict[str, str]) -> None:
        if self.surface.dialogs_seen:
            for d in self.surface.dialogs_seen:
                self._record_recovery(step, "unexpected_dialog", f"dismissed: {d}")
            self.surface.clear_dialogs()
        if step.wait is not None:
            deadline = time.monotonic() + WAIT_TIMEOUT_S
            busy_seen = False
            while time.monotonic() < deadline:
                snap = self.surface.snapshot()
                if self._check(step.wait, snap, params):
                    return
                if self._check_outcomes(snap, params):
                    return  # a business outcome satisfies any post-condition
                if not busy_seen and self._transient_markers and any(
                    m.lower() in snap.text.lower() for m in self._transient_markers
                ):
                    # One known-transient reload per step, then continue waiting
                    # on the refreshed state — the bounded recoverable path.
                    busy_seen = True
                    self.surface.reload()
                    self._record_recovery(step, "transient_reload", "page reloaded once")
                    continue
                time.sleep(POLL_S)
            raise self._hard(
                step,
                f"post-condition: {step.wait.describe()}",
                "not observed within timeout",
                screenshot=True,
            )
        url_verdict = self.allow.check_url(self.surface.snapshot().url)
        if not url_verdict.allowed:
            raise self._hard(
                step, "URL stays within allowlist", url_verdict.reason, screenshot=True
            )

    def _settle(self) -> Snapshot:
        """Bounded settle: first snapshot whose URL repeats within the budget.

        A known-transient page seen mid-settle is reloaded once (recorded as a
        recovery) rather than accepted as the settled state — a busy page is
        not a state, it is a request to look again.
        """
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        last_url = None
        busy_seen = False
        snap = self.surface.snapshot()
        while time.monotonic() < deadline and snap.url != last_url:
            if not busy_seen and self._transient_markers and any(
                m.lower() in snap.text.lower() for m in self._transient_markers
            ):
                busy_seen = True
                self.surface.reload()
                step = self.art.steps[min(self.steps_executed, len(self.art.steps) - 1)]
                self._record_recovery(step, "transient_reload", "page reloaded once")
                snap = self.surface.snapshot()
                continue
            last_url = snap.url
            time.sleep(POLL_S)
            snap = self.surface.snapshot()
        return snap

    # ----------------------------------------------------------- classification

    def _check_outcomes(self, snap: Snapshot, params: dict[str, str]) -> str | None:
        for o in self.art.outcomes:
            if self._check(o.detect, snap, params):
                return o.id
        return None

    def _check(self, check: Check, snap: Snapshot, params: dict[str, str]) -> bool:
        if check.url_contains:
            return self.art.resolve_value(check.url_contains, params) in snap.url
        if check.text_contains:
            want = self.art.resolve_value(check.text_contains, params)
            return want.lower() in snap.text.lower()
        if check.element_present:
            return resolve(check.element_present, snap) is not None
        return False

    def _resume_wait_holds(self, check: Check, snap: Snapshot, params: dict[str, str]) -> bool:
        """Stricter variant of _check used only for resume-skips: textual and
        element waits are exact, and URL-substring waits additionally require
        the wait's core path letters (e.g. 'member') to appear in a segment of
        the live path — so '/member/' cannot 'match' '/search'."""
        if check.text_contains:
            return self._check(check, snap, params)
        if check.element_present:
            return self._check(check, snap, params)
        if check.url_contains:
            want = self.art.resolve_value(check.url_contains, params)
            if want not in snap.url:
                return False
            live_path = urlparse(snap.url).path.strip("/").split("/")
            core = [seg for seg in want.strip("/").split("/") if re.search(r"[a-zA-Z]", seg)]
            return any(
                seg.lower() in {p.lower() for p in live_path if re.search(r"[a-zA-Z]", p)}
                for seg in core
            )
        return False

    def _extract(self) -> dict[str, str]:
        snap = self.surface.snapshot()
        outs: dict[str, str] = {}
        for o in self.art.outputs:
            m = re.search(o.extract.pattern, snap.text)
            if not m:
                raise self._hard(
                    None,
                    f"output {o.name!r} extractable via {o.extract.pattern!r}",
                    "no match on final page",
                    screenshot=True,
                )
            outs[o.name] = m.group(o.extract.group)
        return outs

    # -------------------------------------------------------------- escalation

    def _escalate(self, hf: _HardFailure):
        from .escalation import ControlSession, build_intervention, write_intervention

        snap = self.surface.snapshot()
        shot_candidate = str(self.elog.dir / "steps" / "escalation.png")
        shot = shot_candidate if self.surface.screenshot(shot_candidate) else None
        req = build_intervention(
            run_dir=str(self.elog.dir),
            capability=self.art.capability_name,
            step=hf.step,
            reason=f"hard failure at step {hf.failure.step_id}: {hf.failure.observed}",
            expected=hf.failure.expected,
            observed=hf.failure.observed,
            snapshot=snap,
            screenshot=shot,
        )
        write_intervention(req)
        self.elog.line("ESCALATION: intervention.json written; handing control to operator")
        cs = ControlSession()
        cs.to(ControlSession.HUMAN, why=req["reason"])
        record = self.operator.handle(self.surface, req)
        cs.to(
            ControlSession.AUTOMATION if record.resumed else ControlSession.ABORTED,
            why="operator resumed" if record.resumed else "operator aborted",
        )
        record.operator_actions.append(f"control-transitions: {cs.transitions}")
        return record

    # ------------------------------------------------------------------ guards

    def _guard(self, step: Step, target: Element | None) -> None:
        v = self.allow.check_action(step.action)
        if not v.allowed:
            raise self._hard(step, f"action {step.action!r} allowed", v.reason)
        if step.action == "goto":
            v = self.allow.check_url(step.value or "")
            if not v.allowed:
                raise self._hard(step, "goto target allowed", v.reason)
        if target is not None and step.action in ("click", "fill"):
            v = self.allow.check_element(step.action, target.role, target.name)
            if not v.allowed:
                raise self._hard(step, f"{step.action} on {target.name!r} permitted", v.reason)

    # ------------------------------------------------------------------ plumbing

    def _hard(self, step: Step | None, expected: str, observed: str, *, screenshot=False):
        shot = None
        if screenshot:
            candidate = str(self.elog.dir / "steps" / f"{(step.id if step else 0):03d}-failure.png")
            shot = candidate if self.surface.screenshot(candidate) else None
        return _HardFailure(
            Failure(
                step_id=step.id if step else 0,
                action=step.action if step else "-",
                expected=expected,
                observed=observed,
                screenshot=shot,
            ),
            step,
        )

    def _record_recovery(self, step: Step, condition: str, action: str) -> None:
        self.recoveries.append(Recovery(step_id=step.id, condition=condition, action_taken=action))

    def _result(
        self,
        status: str,
        params: dict[str, str],
        started: float,
        *,
        outputs=None,
        outcome_id=None,
        failure=None,
        escalation=None,
    ) -> ReplayResult:
        returns: dict[str, str] = {}
        if outcome_id:
            for o in self.art.outcomes:
                if o.id == outcome_id:
                    returns = {k: self.art.resolve_value(v, params) for k, v in o.returns.items()}
        return ReplayResult(
            artifact=self.art.capability_name,
            params_redacted={k: redact_text(str(v)) for k, v in params.items()},
            status=status,
            outputs=outputs if outputs else returns,
            outcome_id=outcome_id,
            failure=failure,
            recoveries=list(self.recoveries),
            escalation=escalation,
            steps_executed=self.steps_executed,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
