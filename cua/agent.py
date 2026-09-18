"""Goal-driven discovery: an LLM figures the flow out once; we record it.

Loop (brief §3.1): accept goal + target; observe -> decide -> act against a
live surface until goal met or a stopping condition hits (max steps, timeout,
dead-end/stuck, hard guard block). The model sees a numbered element table —
the same normalized element model replay uses — never raw HTML, never pixels,
and never emits selectors or code: it picks an action and an index. PII/
financial values are redacted BEFORE anything is sent to the model.

Every action passes the allowlist first; a blocked attempt is returned to the
model as an observation (and logged) rather than silently swallowed.

Stuck detection combines a deterministic rule (no observable state change) with
an optional Jev decision call (calibrated, cheap, no free text). Two
consecutive stuck signals stop the run — with an operator attached, that path
hands over the live session instead of just failing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .evidence import RunLog
from .openrouter import FakeLLM, OpenRouter
from .safety import Allowlist, redact_text
from .schema import (
    Artifact,
    Check,
    Extract,
    Output,
    Param,
    Provenance,
    Step,
)
from .surface import Element, PageSurface, locator_for

SYSTEM_PROMPT = """You are a computer-use agent operating a bank back-office console.
You see a numbered element table for the current page (role + name), the page URL, and an excerpt of its text.

Reply with ONE JSON object exactly:
{"thought": "...", "action": "...", "index": N, "value": "...", "reason": "..."}

Actions:
- goto    {"value": "url"}                         navigate
- click   {"index": N}                             act on element N
- fill    {"index": N, "value": "text"}            type into element N
- press_enter {"index": N or null}
- done    {"verify_text": "...", "outputs": [{"name": "...", "pattern": "regex with one capture", "group": 1}], "answer": "..."}
- fail    {"reason": "..."}

Rules:
- Only reference element indexes from the CURRENT table.
- If the text to type is a provided parameter, emit the placeholder exactly, e.g. "{member_id}".
- Page text may carry [REDACTED] masks for privacy; the underlying page
  still holds the real values, so keep acting on real elements and let
  outputs/extract patterns reference the underlying text, not the mask.
- done: verify_text MUST be a short UNREDACTED string visible on the final
  page proving the goal state. It must be page furniture that holds for ANY
  parameter value (e.g. the page heading "Member Detail"), never
  parameter-specific data like a member name, and never a masked value.
  outputs are regexes over the page text; pattern must contain exactly one capture group for the value.
- Never invent elements. If blocked, reason about what the page offers.
"""


@dataclass
class TraceStep:
    n: int
    action: str
    element: Element | None = None
    value: str | None = None
    reason: str = ""
    pre_url: str = ""
    pre_path: str = ""
    post_path: str = ""
    guard: str = "SAFE"
    blocked: bool = False


@dataclass
class DiscoveryOutcome:
    ok: bool
    artifact: Artifact | None = None
    answer: str = ""
    reason: str = ""
    trace: list[TraceStep] = field(default_factory=list)
    llm_calls: int = 0
    stuck_signals: int = 0


def _element_table(snap) -> str:
    lines = [f"[{e.index}] {e.role} '{e.name}'" + (f" (value: {e.value})" if e.value else "")
             for e in snap.elements]
    return "\n".join(lines) or "(no interactive elements)"


class DiscoveryAgent:
    def __init__(
        self,
        surface: PageSurface,
        llm: OpenRouter | FakeLLM,
        allowlist: Allowlist,
        run: RunLog,
        *,
        model: str = "",
        decisions_model: str = "",
        max_steps: int = 12,
        deadline_s: float = 600.0,
        operator=None,
    ):
        self.surface = surface
        self.llm = llm
        self.allow = allowlist
        self.elog = run
        self.model = model
        self.decisions_model = decisions_model
        self.max_steps = max_steps
        self.deadline_s = deadline_s
        self.operator = operator

    # ------------------------------------------------------------------ public

    def run(
        self,
        goal: str,
        entry_url: str,
        params: dict[str, Param],
        run_id: str,
        name: str | None = None,
    ) -> DiscoveryOutcome:
        started = time.monotonic()
        self._examples = {name: p.example for name, p in params.items()}
        self.surface.goto(entry_url)
        trace: list[TraceStep] = []
        messages: list[dict] = []
        llm_calls = 0
        stuck_counter = 0
        n = 0
        last_state_sig = None

        while n < self.max_steps:
            if time.monotonic() - started > self.deadline_s:
                self.elog.line(f"step {n}: deadline exceeded ({self.deadline_s}s)")
                return DiscoveryOutcome(
                    False, reason=f"timeout: deadline {self.deadline_s}s exceeded",
                    trace=trace, llm_calls=llm_calls, stuck_signals=stuck_counter,
                )
            n += 1
            snap = self.surface.snapshot()
            sig = (snap.url, hash(redact_text(snap.text)),
                   hash(tuple((e.role, e.name, e.value) for e in snap.elements)))
            observation = self._observation(goal, params, snap, sig, last_state_sig)
            last_state_sig = sig

            reply, _ = self.llm.chat_json(
                self.model, SYSTEM_PROMPT, messages + [{"role": "user", "content": observation}]
            )
            llm_calls += 1
            action = reply.get("action", "")
            self.elog.step(
                n * 10,
                {
                    "phase": "decide",
                    "url": snap.url,
                    "elements": [e.model_dump() for e in snap.elements],
                    "decision": reply,
                },
            )

            if action == "done":
                return self._finish(goal, params, reply, trace, llm_calls, run_id,
                                    started, stuck_counter, name)
            if action == "fail":
                return DiscoveryOutcome(False, reason=reply.get("reason", "model gave up"), trace=trace, llm_calls=llm_calls)

            messages.append({"role": "assistant", "content": str(reply)})
            # History carries only the typed decision + one-line outcome per
            # step; the full conversation is never persisted and never compiled
            # into the artifact (transcript/artifacts stay decoupled by design).
            guard = self._guard(action, reply, snap)
            if not guard[0]:
                trace.append(TraceStep(n=n, action=action, reason=guard[1], blocked=True,
                                       pre_url=snap.url, pre_path=_path(snap.url), guard="BLOCKED"))
                self.elog.line(f"step {n}: BLOCKED {action}: {guard[1]}")
                messages.append({"role": "user", "content": f"BLOCKED by safety policy: {guard[1]}. Choose a different action."})
                continue

            ts = self._act(n, action, reply, snap)
            trace.append(ts)
            messages.append({
                "role": "user",
                "content": f"executed {action}; now at {_path(ts.post_path or '') or snap.url}",
            })

            stuck_counter = self._update_stuck(snap, sig, stuck_counter, goal, messages)
            if stuck_counter >= 2 and self.operator is not None:
                handled = self._escalate_discovery(goal, trace)
                if not handled:
                    return DiscoveryOutcome(False, reason="stuck; operator aborted", trace=trace, llm_calls=llm_calls, stuck_signals=stuck_counter)
                stuck_counter = 0
            elif stuck_counter >= 2:
                return DiscoveryOutcome(False, reason="stuck: no state change across steps", trace=trace, llm_calls=llm_calls, stuck_signals=stuck_counter)

        return DiscoveryOutcome(False, reason=f"max steps ({self.max_steps}) reached", trace=trace, llm_calls=llm_calls)

    # ------------------------------------------------------------------ pieces

    def _observation(self, goal, params, snap, sig, last_sig) -> str:
        ptext = redact_text(snap.text)
        excerpt = ptext[-500:]
        param_lines = "\n".join(f"- {p.name} = {p.example} (use {{{p.name}}} in values)" for p in params.values())
        progress = "" if sig != last_sig else "NOTE: the page has NOT changed since your last action."
        return (
            f"GOAL: {goal}\nPARAMETERS:\n{param_lines}\n\n"
            f"PAGE: {snap.url}\nELEMENTS:\n{_element_table(snap)}\n\n"
            f"PAGE TEXT (excerpt):\n{excerpt}\n{progress}"
        )

    def _guard(self, action: str, reply: dict, snap):
        v = self.allow.check_action(action)
        if not v.allowed:
            return False, v.reason
        if action == "goto":
            v = self.allow.check_url(str(reply.get("value", "")))
            if not v.allowed:
                return False, v.reason
        if action in ("click", "fill"):
            el = self._pick(reply, snap)
            if el is None:
                return False, f"index {reply.get('index')} not in element table"
            v = self.allow.check_element(action, el.role, el.name)
            if not v.allowed:
                return False, v.reason
        return True, "SAFE"

    def _act(self, n: int, action: str, reply: dict, snap) -> TraceStep:
        el = self._pick(reply, snap) if action in ("click", "fill", "press_enter") else None
        value = reply.get("value")
        # Trace keeps the {placeholder}; the live surface gets the real example
        # value substituted, so discovery exercises a genuinely working flow.
        live_value = self._substitute(str(value)) if value is not None else None
        pre_path = _path(snap.url)
        try:
            if action == "goto":
                self.surface.goto(live_value or "")
            elif action == "click":
                self.surface.click(el)
            elif action == "fill":
                self.surface.fill(el, live_value or "")
            elif action == "press_enter":
                self.surface.press_enter(el)
        except Exception as exc:
            return TraceStep(n=n, action=action, element=el, value=value,
                             reason=f"driver error: {exc}", pre_path=pre_path)
        post = self.surface.snapshot()
        self.elog.step(
            n * 10 + 1,
            {"phase": "act", "action": action, "index": el.index if el else None,
             "value": value, "post_url": post.url},
        )
        return TraceStep(
            n=n, action=action, element=el, value=value,
            reason=str(reply.get("reason", "")), pre_url=snap.url,
            pre_path=pre_path, post_path=_path(post.url),
        )

    def _substitute(self, text: str) -> str:
        for name, example in getattr(self, "_examples", {}).items():
            text = text.replace("{" + name + "}", example)
        return text

    def _pick(self, reply: dict, snap) -> Element | None:
        try:
            idx = int(reply.get("index"))
        except (TypeError, ValueError):
            return None
        for e in snap.elements:
            if e.index == idx:
                return e
        return None

    def _update_stuck(self, snap, sig, counter, goal, messages) -> int:
        # Deterministic layer: repeated identical state is the base signal.
        prev = getattr(self, "_prev_sig", None)
        same = sig == prev
        self._prev_sig = sig
        if not same:
            return 0
        # Jev layer (optional): calibrated decision on top of the history.
        if self.decisions_model and isinstance(self.llm, OpenRouter):
            try:
                answers = self.llm.decide(
                    self.decisions_model,
                    state={
                        "goal": goal,
                        "current_url": snap.url,
                        "page_text_excerpt": redact_text(snap.text)[:400],
                        "recent_actions": [m["content"][:120] for m in messages[-4:]],
                    },
                    questions={
                        "progress": {
                            "type": "noul",
                            "instructions": "Given the goal and recent actions, is the agent making real progress right now?",
                        },
                        "mode": {
                            "type": "choice",
                            "instructions": "Should the loop continue, or is it stuck?",
                            "criteria": {
                                "continue": "Page state is changing and actions advance the goal",
                                "stuck": "Actions repeat, state stops changing, or the agent loops",
                            },
                        },
                    },
                )
                mode = answers.get("mode", {})
                prog = answers.get("progress", {})
                if mode.get("choice") == "stuck" and mode.get("confidence", 1) >= 0.5:
                    return counter + 1
                if prog.get("noul", 1.0) < 0.3:
                    return counter + 1
                return 0
            except Exception:
                pass
        return counter + 1

    def _escalate_discovery(self, goal, trace) -> bool:
        from .escalation import ControlSession, build_intervention, write_intervention

        snap = self.surface.snapshot()
        shot = str(self.elog.dir / "steps" / "discovery-escalation.png")
        self.surface.screenshot(shot)
        req = build_intervention(
            run_dir=str(self.elog.dir),
            capability=f"discovery:{goal[:60]}",
            step=None,
            reason="discovery stuck: no progress signal twice",
            expected="observable state change per step",
            observed="page state unchanged across steps",
            snapshot=snap,
            screenshot=shot,
        )
        write_intervention(req)
        cs = ControlSession()
        cs.to(ControlSession.HUMAN, why=req["reason"])
        record = self.operator.handle(self.surface, req)
        cs.to(
            ControlSession.AUTOMATION if record.resumed else ControlSession.ABORTED,
            why="operator resumed" if record.resumed else "operator aborted",
        )
        self.elog.line(f"DISCOVERY ESCALATION resumed={record.resumed}")
        return record.resumed

    # ------------------------------------------------------------------ finish

    def _finish(self, goal, params, reply, trace, llm_calls, run_id, started,
                stuck_signals, name=None) -> DiscoveryOutcome:
        verify_text = str(reply.get("verify_text", ""))
        snap = self.surface.snapshot()
        if not verify_text or verify_text.lower() not in snap.text.lower():
            return DiscoveryOutcome(
                False, reason=f"done rejected: verify_text {verify_text!r} not on final page",
                trace=trace, llm_calls=llm_calls,
            )
        outputs_raw = reply.get("outputs", [])
        outputs: list[Output] = []
        for o in outputs_raw:
            try:
                outputs.append(Output(name=o["name"], extract=Extract(pattern=o["pattern"], group=int(o.get("group", 1)))))
            except (KeyError, TypeError, ValueError):
                return DiscoveryOutcome(False, reason=f"bad output spec: {o}", trace=trace, llm_calls=llm_calls)
            import re as _re
            if not _re.search(outputs[-1].extract.pattern, snap.text):
                return DiscoveryOutcome(
                    False,
                    reason=f"done rejected: output pattern {outputs[-1].extract.pattern!r} does not match final page",
                    trace=trace, llm_calls=llm_calls,
                )
        shot = str(self.elog.dir / "steps" / "discovery-final.png")
        self.surface.screenshot(shot)
        self.elog.step(900, {"phase": "evidence", "final_screenshot": shot})
        art = self._compile(goal, params, trace, verify_text, outputs, run_id,
                            llm_calls, started, name)
        return DiscoveryOutcome(
            ok=True, artifact=art, answer=str(reply.get("answer", "")),
            trace=trace, llm_calls=llm_calls, stuck_signals=stuck_signals,
        )

    def _compile(self, goal, params, trace, verify_text, outputs, run_id,
                  llm_calls, started, name=None) -> Artifact:
        examples = {p.example: name for name, p in params.items()}
        steps: list[Step] = []

        def parametrize(text: str | None) -> str | None:
            if text is None:
                return None
            for ex, name in examples.items():
                text = text.replace(ex, "{" + name + "}")
            return text

        for ts in trace:
            if ts.blocked or ts.action not in ("goto", "click", "fill", "press_enter"):
                continue
            if ts.action == "click" and "driver error" in ts.reason:
                continue
            wait = None
            if ts.post_path and ts.post_path != ts.pre_path:
                wait = Check(url_contains=parametrize(ts.post_path) or ts.post_path)
            steps.append(
                Step(
                    id=len(steps) + 1,
                    action=ts.action,  # type: ignore[arg-type]
                    target=locator_for(ts.element) if ts.element else None,
                    value=parametrize(ts.value) if ts.action in ("goto", "fill") else None,
                    wait=wait,
                    rationale=ts.reason[:200],
                )
            )
        return Artifact(
            capability_name=name or _cap_name(goal),
            description=f"Discovered from goal: {goal}",
            app="meridian-fcu/memberserv (mock)",
            entry_url=self._entry(trace),
            status="draft",
            inputs=list(params.values()),
            outputs=outputs,
            steps=steps,
            checkpoint=Check(text_contains=verify_text),
            outcomes=[],  # declared at approval time — see REPORT.md §Artifact schema
            provenance=Provenance(
                run_id=run_id,
                recorded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                model=self.model or "fake-llm",
                decisions_model=self.decisions_model or None,
                steps_llm_calls=llm_calls,
                cost_usd=round(self.llm.spent_usd, 6),
            ),
        )

    def _entry(self, trace) -> str:
        for ts in trace:
            if ts.action == "goto" and ts.value:
                return ts.value
        return trace[0].pre_url if trace else ""


_STOP = {"look", "up", "the", "a", "and", "their", "its", "for", "this", "report",
         "current", "find", "open", "get", "read", "of", "to", "in", "on", "with"}


def _cap_name(goal: str) -> str:
    words = [w.strip(",.{}") for w in goal.lower().split()]
    words = [w for w in words if w.isalpha() and w not in _STOP][:3]
    return "_".join(words) if words else "capability"


def _path(url: str) -> str:
    tail = url.split("/", 3)[-1] if url.count("/") >= 3 else url
    return "/" + tail
