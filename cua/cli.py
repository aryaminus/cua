"""CLI: serve / discover / approve / replay / demo.

Demo paths (README documents the exact commands):

  # full vertical slice, offline-capable except the discovery run:
  cua serve &                                          # the mock back-office app
  cua discover --goal "Look up member {member_id} and report their current
      savings balance" --param member_id=1001          # real LLM run -> artifact
  cua approve evidence/<run>/artifact.json --preset-lookup-outcomes
  cua replay evidence/<run>/artifact.json --param member_id=1002
  cua demo --part matrix                               # replay outcome matrix
  cua demo --part escalation                           # handoff demonstration
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from . import mockapp
from .agent import DiscoveryAgent
from .budgets import Budgets, load_budgets
from .evidence import RunLog
from .openrouter import FakeLLM, OpenRouter
from .replay import ReplayEngine
from .safety import Allowlist
from .schema import Artifact, Outcome, Param
from .surface import PlaywrightSurface

EVIDENCE = Path("evidence")
APP_URL = os.environ.get("CUA_APP_URL", "http://127.0.0.1:8791")

# Offline discovery script (FakeLLM): what a competent model would do on the
# mock app. Used for tests, demos, and regenerating evidence without a key —
# the LIVE discovery run (OpenRouter) is what produced the committed artifact.
FAKE_SCRIPT = [
    {"thought": "start at the member lookup page", "action": "goto",
     "value": APP_URL + "/search", "reason": "entry point for member lookup"},
    {"thought": "type the member id", "action": "fill", "index": 0,
     "value": "{member_id}", "reason": "parameterized member id goes in the only textbox"},
    {"thought": "run the search", "action": "click", "index": 1,
     "reason": "Search submits the lookup form"},
    {"thought": "goal state reached", "action": "done",
     "verify_text": "Member Detail",
     "outputs": [{"name": "savings_balance", "pattern": r"Savings\s*\$([\d,]+\.\d{2})",
                  "group": 1, "value_hint": "current savings balance"}],
     "answer": "savings balance read from the member detail page"},
]

LOOKUP_OUTCOMES = [
    Outcome(
        id="NOT_FOUND",
        description="No member exists for the requested ID — a legitimate answer, not a failure.",
        detect={"text_contains": "No member found"},
        returns={"message": "No member found for ID {member_id}"},
    ),
    Outcome(
        id="INVALID_INPUT",
        description="The supplied ID failed validation (non-numeric).",
        detect={"text_contains": "Member ID must be numeric"},
        returns={"message": "Invalid member ID supplied ({member_id})"},
    ),
    Outcome(
        id="PERMISSION_DENIED",
        description="The member record exists but the teller role may not view it (frozen).",
        detect={"text_contains": "Access denied"},
        returns={"message": "Access denied for member ID {member_id}: frozen record"},
    ),
]


def _ensure_server(port: int = 8791) -> None:
    """Start the mock app in-process (idempotent per port).

    Fault switches ride on process env vars and are read per request, so the
    in-process server inherits whatever the demo armed. Runs on `port` so a
    demo can pick a fault port that never collides with an external `cua
    serve`, and re-point the replay at it via CUA_APP_URL.
    """
    global _srv
    if isinstance(_srv, dict):
        servers = _srv
    else:
        servers = {}
        _srv = servers
    if port in servers:
        return
    import socket

    probe = socket.socket()
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return  # something already serves this port — use it
    finally:
        probe.close()
    from werkzeug.serving import make_server

    mockapp.reset_state()
    servers[port] = make_server("127.0.0.1", port, mockapp.app, threaded=False)
    threading.Thread(target=servers[port].serve_forever, daemon=True).start()


_srv = None


# ------------------------------------------------------------------ commands --

def _budgets_from(args) -> Budgets:
    """Defaults <- config/budgets.json <- --budget group.key=value overrides."""
    b = load_budgets()
    for ov in getattr(args, "budget", None) or []:
        if "=" not in ov:
            raise SystemExit(f"--budget expects group.key=value, got {ov!r}")
        dotted, _, value = ov.partition("=")
        b.apply(dotted, value)
    return b


def cmd_serve(args) -> None:
    mockapp.reset_state()
    print(f"MemberServ console on http://127.0.0.1:{args.port} (Ctrl-C to stop)")
    mockapp.serve(port=args.port)


def cmd_discover(args) -> None:
    allow = Allowlist.load(args.allowlist)
    run = RunLog(EVIDENCE, args.run_id)
    params = _parse_params(args.param)
    param_spec = {
        name: Param(name=name, type="string", example=ex, description=f"{name} (from CLI)")
        for name, ex in params.items()
    }
    b = _budgets_from(args)
    llm = OpenRouter(budget=b.llm)
    model = os.environ.get("CUA_MODEL", "deepseek/deepseek-v4.1-flash")
    jev = os.environ.get("CUA_JEV_MODEL", "")
    run.meta(mode="discovery", goal=args.goal, model=model, decisions_model=jev or None,
             params=params, app_url=APP_URL, allowlist=str(args.allowlist),
             budgets=b.model_dump())
    surface = PlaywrightSurface(headless=not args.headed, act_timeout_s=b.replay.act_timeout_s)
    try:
        operator = None
        if args.operator_script:
            from .escalation import ScriptedOperator

            operator = ScriptedOperator(
                [c.strip() for c in args.operator_script.split(";") if c.strip()])
        elif args.operator_repl:
            from .escalation import TerminalOperator

            operator = TerminalOperator()
        agent = DiscoveryAgent(
            surface, llm, allow, run, model=model, decisions_model=jev,
            max_steps=args.max_steps or b.discovery.max_steps,
            deadline_s=args.deadline_s or b.discovery.wall_clock_s,
            operator=operator, budget=b.discovery,
        )
        out = agent.run(args.goal, args.entry or APP_URL, param_spec, args.run_id,
                        name=args.name)
    finally:
        surface.close()
    run.line(f"discovery ok={out.ok} answer={out.answer!r} reason={out.reason}")
    if out.ok and out.artifact:
        run.artifact(out.artifact.model_dump_json(indent=2))
        path = run.dir / "artifact.json"
        print(f"DISCOVERY OK — draft artifact: {path}")
        print(f"  next: cua approve {path} --preset-lookup-outcomes")
    else:
        run.finish(status="failed", reason=out.reason)
        print(f"DISCOVERY FAILED: {out.reason}")
        sys.exit(1)
    run.finish(status="ok", llm_calls=out.llm_calls, cost_usd=round(llm.spent_usd, 6),
               llm_latency_s=[round(x, 3) for x in llm.latencies_s],
               llm_retries=llm.retries)


def cmd_approve(args) -> None:
    path = Path(args.artifact)
    art = Artifact.model_validate_json(path.read_text())
    outcomes = []
    if args.preset_lookup_outcomes:
        outcomes = LOOKUP_OUTCOMES
    for raw in args.outcome_json or []:
        outcomes.append(Outcome(**json.loads(raw)))
    if outcomes:
        existing = {o.id for o in art.outcomes}
        art.outcomes = art.outcomes + [o for o in outcomes if o.id not in existing]
    # Approval ceremony: the reviewer sees the full capability sheet, the
    # artifact is dry-run validated, and approval requires an explicit typed
    # confirmation (or --yes for scripts). The decision is ledgered.
    print(_capability_sheet(art))
    problems = _validate_artifact_for_approval(art)
    if problems:
        print("approval REFUSED — artifact failed pre-approval validation:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(3)
    if art.status == "approved" and not args.force:
        print("already approved; pass --force to re-approve (re-ledgers the decision).")
        return
    if not args.yes:
        try:
            confirm = input(
                "type APPROVE to approve this capability for unattended replay: "
            ).strip()
        except EOFError:
            confirm = ""
        if confirm != "APPROVE":
            print("not approved — no changes written.")
            sys.exit(4)
    art.status = "approved"
    path.write_text(art.model_dump_json(indent=2))
    actor = os.environ.get("USER", "reviewer")
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with open(path.parent / "run.log", "a") as fh:
        fh.write(f"{stamp} approval actor={actor} status=draft->approved "
                 f"outcomes={[o.id for o in art.outcomes]}\n")
    print(f"approved {art.capability_name!r}: status={art.status}, "
          f"steps={len(art.steps)}, outcomes={[o.id for o in art.outcomes]}")


def _fmt_inputs(art: Artifact) -> str:
    return ", ".join(f"{p.name}:{p.type}" + ("" if p.required else "?") for p in art.inputs)


def _capability_sheet(art: Artifact) -> str:
    lines = [
        f"capability : {art.capability_name}  (schema {art.schema_version}, status {art.status})",
        f"app        : {art.app}",
        f"entry      : {art.entry_url}",
        f"inputs     : {_fmt_inputs(art)}",
        f"outputs    : {', '.join(o.name for o in art.outputs)}",
        f"checkpoint : {art.checkpoint.describe()}",
        f"outcomes   : {', '.join(o.id for o in art.outcomes) or '(none declared)'}",
        "steps      :",
    ]
    for s in art.steps:
        tgt = f"{s.target.role} {s.target.name!r}" if s.target else "-"
        wait = f"  wait: {s.wait.describe()}" if s.wait else ""
        lines.append(f"  {s.id}. {s.action} {tgt}"
                     + (f"  value={s.value!r}" if s.value else "") + wait)
    lines.append(f"provenance : {art.provenance.model}, run {art.provenance.run_id}, "
                 f"{art.provenance.steps_llm_calls} llm calls, ${art.provenance.cost_usd}")
    return "\n".join(lines)


def _validate_artifact_for_approval(art: Artifact) -> list[str]:
    """Deterministic pre-approval dry run: schema invariants (pydantic already
    enforced them on load) plus the checks only a reviewer pass can do —
    every {param} resolves, waits/checkpoint carry no masked values, the
    checkpoint differs from every step wait (it must assert the END state)."""
    problems: list[str] = []
    params = {p.name: p.example or "1" for p in art.inputs}
    try:
        for s in art.steps:
            if s.value:
                art.resolve_value(s.value, params)
            if s.wait:
                for f in ("url_contains", "text_contains"):
                    v = getattr(s.wait, f)
                    if v:
                        art.resolve_value(v, params)
        for o in art.outcomes:
            v = o.detect.text_contains or o.detect.url_contains or ""
            art.resolve_value(v, params)
            for t in o.returns.values():
                art.resolve_value(t, params)
    except ValueError as exc:
        problems.append(str(exc))
    for label, chk in [("checkpoint", art.checkpoint)] + [
        (f"step {s.id} wait", s.wait) for s in art.steps if s.wait
    ]:
        for f in ("text_contains",):
            v = getattr(chk, f)
            if v and "[REDACTED" in v:
                problems.append(f"{label} references a masked value: {v!r}")
    cp = art.checkpoint.describe()
    if any(s.wait and s.wait.describe() == cp for s in art.steps):
        problems.append("checkpoint is identical to a step wait — it must assert the end state")
    if not art.outputs:
        problems.append("no declared outputs — an agent-invocable capability must return something")
    return problems


def cmd_replay(args) -> None:
    allow = Allowlist.load(args.allowlist)
    art = Artifact.model_validate_json(Path(args.artifact).read_text())
    params = _parse_params(args.param)
    run = RunLog(EVIDENCE, args.run_id or f"replay-{art.capability_name[:24]}")
    run.meta(mode="replay", artifact=args.artifact, params=params,
             allowlist=str(args.allowlist), stability=args.stability)
    operator = None
    if args.operator_script:
        from .escalation import ScriptedOperator

        cmds = [c.strip() for c in args.operator_script.split(";") if c.strip()]
        operator = ScriptedOperator(cmds)
    elif args.operator_repl:
        from .escalation import TerminalOperator

        operator = TerminalOperator()
    _ensure_server()  # standalone boots the default port; the demo re-points via CUA_APP_URL
    b = _budgets_from(args)
    surface = PlaywrightSurface(headless=not args.headed, act_timeout_s=b.replay.act_timeout_s)
    try:
        engine = ReplayEngine(surface, art, allow, run, operator=operator,
                              allow_draft=args.allow_draft, budget=b.replay)
        result = engine.run(params)
    finally:
        surface.close()

    if args.stability > 1:
        sigs = [result.summarize()]
        for _ in range(args.stability - 1):
            s = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
            try:
                r = ReplayEngine(s, art, allow, run, allow_draft=args.allow_draft,
                                 budget=b.replay).run(params)
                sigs.append(r.summarize())
            finally:
                s.close()
        identical = len(set(sigs)) == 1
        run.meta(stability={"runs": len(sigs), "identical": identical, "signatures": sigs})
        print(f"stability: {len(sigs)} runs, identical={identical}")
    print(f"REPLAY {result.summarize()}")
    print(f"  evidence: {run.dir}/result.json")
    sys.exit(0 if result.status in ("SUCCESS", "BUSINESS_OUTCOME") else 2)


def cmd_demo(args) -> None:
    # Boot the default-port server once for the fault-free phases. Fault
    # phases (matrix fault case, escalation) each boot a dedicated server on
    # their own port so an external `cua serve` on 8791 never collides.
    _ensure_server()
    allow = Allowlist.load()
    b = _budgets_from(args)
    base = Path(args.evidence_root) if args.evidence_root else EVIDENCE

    app_base = {"url": APP_URL}  # mutable base: fault phases re-point it

    if args.part in ("discovery-offline", "all"):
        run = RunLog(base, "discovery-offline")
        run.meta(mode="discovery", model="fake-llm (scripted)",
                 note="offline demo; live artifact in discovery-live/")
        surface = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
        try:
            agent = DiscoveryAgent(
                surface, FakeLLM(FAKE_SCRIPT), allow, run, model="fake-llm",
                max_steps=8, operator=None, budget=b.discovery,
            )
            params = {"member_id": Param(name="member_id", type="string", example="1001",
                                         description="member number to look up")}
            out = agent.run(
                "Look up member {member_id} and report their current savings balance",
                app_base["url"] + "/search", params, "discovery-offline",
            )
        finally:
            surface.close()
        assert out.ok and out.artifact, f"offline discovery failed: {out.reason}"
        out.artifact.outcomes = LOOKUP_OUTCOMES
        out.artifact.status = "approved"
        run.artifact(out.artifact.model_dump_json(indent=2))
        run.finish(status="ok")
        print(f"[discovery-offline] artifact approved: {run.dir}/artifact.json")

    needs_art = args.part in ("matrix", "escalation", "stability", "all")
    if needs_art:
        art_path = base / "discovery-offline" / "artifact.json"
        if not art_path.exists():
            cmd_demo(argparse.Namespace(part="discovery-offline", evidence_root=args.evidence_root))

    if args.part in ("matrix", "all"):
        art = Artifact.model_validate_json(art_path.read_text())
        cases = [
            ("success-1001", {"member_id": "1001"}, {}, "SUCCESS"),
            ("success-1002-parametrized", {"member_id": "1002"}, {}, "SUCCESS"),
            ("business-not-found-9999", {"member_id": "9999"}, {}, "BUSINESS_OUTCOME"),
            ("business-invalid-input", {"member_id": "401a"}, {}, "BUSINESS_OUTCOME"),
            ("business-permission-denied-1005", {"member_id": "1005"}, {}, "BUSINESS_OUTCOME"),
            ("slow-response-absorbed-1006", {"member_id": "1006"},
             {"MOCKAPP_SLOW_MEMBER": "1006", "MOCKAPP_SLOW_SECONDS": "2.5"}, "SUCCESS"),
            ("transient-busy-reload-1002", {"member_id": "1002"},
             {"MOCKAPP_BUSY_MEMBER": "1002"}, "SUCCESS"),
            ("hard-failure-server-error", {"member_id": "1003"},
             {"MOCKAPP_FAULT": "server_error_member_1003"}, "HARD_FAILURE"),
        ]
        for run_id, params, env, expect in cases:
            from .safety import Allowlist as _MatrixAllow

            _matrix_allow = _MatrixAllow.load()
            # The fault case replays against a dedicated fault server on a
            # private port with a port-rewritten artifact copy, so it never
            # collides with an external `cua serve` on 8791.
            art_use = art
            fault_allow = _matrix_allow
            if env:
                fault_base = "http://127.0.0.1:8793"
                saved = {k: os.environ.get(k) for k in set(env)}
                os.environ.update(env)
                mockapp.reset_state()
                _ensure_server(port=8793)
                art_use, fault_allow = _port_rewrite(art, APP_URL, fault_base, _matrix_allow)
                run = RunLog(base, f"replay-{run_id}")
                run.meta(mode="replay-matrix", case=run_id, params=params, env=env)
            else:
                mockapp.reset_state()
                run = RunLog(base, f"replay-{run_id}")
                run.meta(mode="replay-matrix", case=run_id, params=params, env=env)
            surface = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
            try:
                r = ReplayEngine(surface, art_use,
                                 fault_allow if env else _matrix_allow, run,
                                 allow_draft=True, budget=b.replay).run(params)
            finally:
                surface.close()
                if env:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                    mockapp.reset_state()
            run.line(f"case {run_id}: {r.summarize()}")
            mark = "OK " if r.status == expect else "UNEXPECTED"
            print(f"[matrix] {mark} {run_id}: {r.summarize()}")

    if args.part in ("escalation", "all"):
        _phase_escalation(base, allow, b)  # prints its own [escalation] line

    if args.part in ("stability", "all"):
        art = Artifact.model_validate_json(art_path.read_text())
        mockapp.reset_state()
        run = RunLog(base, "replay-stability")
        sigs = []
        for _ in range(5):
            surface = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
            try:
                r = ReplayEngine(surface, art, allow, run, allow_draft=True,
                                 budget=b.replay).run(
                    {"member_id": "1001"})
            finally:
                surface.close()
            sigs.append(r.summarize())
        identical = len(set(sigs)) == 1
        run.meta(stability={"runs": 5, "identical": identical, "signatures": sorted(set(sigs))})
        print(f"[stability] 5 runs identical={identical}: {sigs[0]}")


def _phase_escalation(base: Path, allow: Allowlist, b: Budgets) -> tuple[bool, int]:
    """Escalation demo phase, factored so `cua bench` can time the same
    scenario without duplicating the fault wiring. Returns (ok, duration_ms)."""
    import time as _time

    art = Artifact.model_validate_json((base / "discovery-offline" / "artifact.json").read_text())
    fault_base = "http://127.0.0.1:8792"
    saved = {k: os.environ.get(k) for k in ("MOCKAPP_SESSION_TTL",)}
    os.environ["MOCKAPP_SESSION_TTL"] = "2"
    mockapp.reset_state()
    _ensure_server(port=8792)
    art8792, allow8792 = _port_rewrite(art, APP_URL, fault_base, allow)
    run = RunLog(base, "replay-escalation-handoff")
    run.meta(mode="escalation-demo", params={"member_id": "1002"},
             env={"MOCKAPP_SESSION_TTL": "2"},
             note="session expiry forces hard failure; operator takes the live session")
    script = (
        "look; goto " + fault_base + "/; goto " + fault_base + "/search; "
        "fill textbox 'Member ID' 1002; click button 'Search'; look; resume"
    )
    from .escalation import ScriptedOperator

    t0 = _time.monotonic()
    surface = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
    try:
        engine = ReplayEngine(
            surface, art8792, allow8792, run, operator=ScriptedOperator(
                [c.strip() for c in script.split(";")]),
            allow_draft=True, budget=b.replay,
        )
        r = engine.run({"member_id": "1002"})
    finally:
        surface.close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mockapp.reset_state()
    ms = int((_time.monotonic() - t0) * 1000)
    good = r.status == "SUCCESS" and r.escalation is not None and r.escalation.resumed
    print(f"[escalation] {'OK ' if good else 'UNEXPECTED'} status={r.status} "
          f"escalated={bool(r.escalation)} "
          f"resumed={r.escalation.resumed if r.escalation else None} "
          f"ops={len(r.escalation.operator_actions) if r.escalation else 0} ms={ms}")
    return good, ms


# ------------------------------------------------------------------ plumbing --

def _pct(sorted_ms: list[int], p: float) -> int:
    """Percentile of an already-sorted list (inclusive, nearest-rank)."""
    if not sorted_ms:
        return 0
    k = max(0, min(len(sorted_ms) - 1, round(p / 100 * (len(sorted_ms) - 1))))
    return sorted_ms[k]


def _stats(ms: list[int]) -> dict:
    s = sorted(ms)
    return {
        "n": len(s), "min_ms": s[0], "p50_ms": _pct(s, 50), "p95_ms": _pct(s, 95),
        "max_ms": s[-1],
    }


def cmd_bench(args) -> None:
    """Latency/cost benchmark: baseline numbers behind DESIGN.md §7.

    Offline by default (replay cases, escalation cycle, server boot). --live
    adds three minimal LLM probes (~$0.0002) for API latency; --tests times
    the offline suite. Writes evidence/performance/bench.json.
    """
    import time as _time

    b = _budgets_from(args)
    _ensure_server()
    allow = Allowlist.load()
    root = Path(args.evidence_root) if args.evidence_root else EVIDENCE
    art_path = root / "discovery-offline" / "artifact.json"
    if not art_path.exists():
        cmd_demo(argparse.Namespace(part="discovery-offline", evidence_root=str(root),
                                    budget=args.budget))
    art = Artifact.model_validate_json(art_path.read_text())

    report: dict = {"budgets": b.model_dump(), "runs_per_case": args.runs, "cases": {}}

    # server cold boot: fresh make_server on a scratch port -> first 200
    from werkzeug.serving import make_server as _mk

    t0 = _time.monotonic()
    mockapp.reset_state()
    srv = _mk("127.0.0.1", 8799, mockapp.app, threaded=False)
    import threading as _th

    _th.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request as _ur

    _ur.urlopen("http://127.0.0.1:8799/search", timeout=5).read()
    report["server_boot_ms"] = int((_time.monotonic() - t0) * 1000)
    srv.shutdown()
    srv.server_close()

    # replay cases: canonical subset x N (fault-free + slow + busy)
    cases = [
        ("success-1001", {"member_id": "1001"}, {}),
        ("business-not-found-9999", {"member_id": "9999"}, {}),
        ("slow-response-absorbed-1006", {"member_id": "1006"},
         {"MOCKAPP_SLOW_MEMBER": "1006", "MOCKAPP_SLOW_SECONDS": "2.5"}),
        ("transient-busy-reload-1002", {"member_id": "1002"},
         {"MOCKAPP_BUSY_MEMBER": "1002"}),
    ]
    for run_id, params, env in cases:
        durations: list[int] = []
        saved = {k: os.environ.get(k) for k in set(env)} if env else {}
        art_use, allow_use = art, allow
        if env:
            os.environ.update(env)
            mockapp.reset_state()
            _ensure_server(port=8793)
            art_use, allow_use = _port_rewrite(art, APP_URL, "http://127.0.0.1:8793", allow)
        for i in range(args.runs):
            if not env:
                mockapp.reset_state()
            surface = PlaywrightSurface(headless=True, act_timeout_s=b.replay.act_timeout_s)
            try:
                scratch = root / "performance" / "scratch"
                scratch.mkdir(parents=True, exist_ok=True)
                run = RunLog(scratch, f"bench-{run_id}-{i}")
                r = ReplayEngine(surface, art_use, allow_use, run, allow_draft=True,
                                 budget=b.replay).run(params)
            finally:
                surface.close()
            durations.append(r.duration_ms)
        if env:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            mockapp.reset_state()
        report["cases"][run_id] = {"stats": _stats(durations), "status": r.status}

    # escalation cycle (scripted operator, dedicated fault server)
    good, esc_ms = _phase_escalation(root, allow, b)
    report["escalation_cycle_ms"] = esc_ms
    report["escalation_ok"] = good

    if args.live:
        llm = OpenRouter(budget=b.llm)
        model = os.environ.get("CUA_MODEL", "deepseek/deepseek-v4.1-flash")
        lat: list[float] = []
        for _ in range(3):
            reply, _u = llm.chat_json(model, "Reply with JSON.",
                                      [{"role": "user", "content": '{"ack": true}'}],
                                      max_tokens=50)
            assert reply.get("ack") or reply, reply  # any parseable JSON counts
        lat = [round(x, 3) for x in llm.latencies_s]
        report["live_llm"] = {
            "model": model, "calls": len(lat),
            "latency_s": {"min": min(lat), "p50": sorted(lat)[len(lat) // 2], "max": max(lat)},
            "cost_usd": round(llm.spent_usd, 6), "retries": llm.retries,
        }

    if args.tests:
        import subprocess

        # Release every port bench holds (8791/8792/8793/8799) first: the
        # suite's integration tests bind their own servers and must not race us.
        for srv in (_srv or {}).values():
            srv.shutdown()
            srv.server_close()
        _srv.clear()
        t0 = _time.monotonic()
        proc = subprocess.run(
            ["uv", "run", "python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
        )
        report["test_suite"] = {
            "ok": proc.returncode == 0,
            "wall_s": round(_time.monotonic() - t0, 1),
            "tail": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
        }

    out = root / "performance"
    out.mkdir(parents=True, exist_ok=True)
    # strip scratch from the report (it is bulky; bench.json is the artifact)
    (out / "bench.json").write_text(json.dumps(report, indent=2))
    print(f"[bench] wrote {out / 'bench.json'}")
    for case, info in report["cases"].items():
        st = info["stats"]
        print(f"[bench] {case:34s} p50={st['p50_ms']:>5}ms p95={st['p95_ms']:>5}ms "
              f"max={st['max_ms']:>5}ms ({info['status']})")
    print(f"[bench] server_boot={report['server_boot_ms']}ms "
          f"escalation_cycle={report['escalation_cycle_ms']}ms")
    if "live_llm" in report:
        lv = report["live_llm"]
        print(f"[bench] live_llm p50={lv['latency_s']['p50']}s cost=${lv['cost_usd']}")
    if "test_suite" in report:
        ts = report["test_suite"]
        print(f"[bench] tests ok={ts['ok']} wall={ts['wall_s']}s")


def _port_rewrite(artifact: Artifact, src_base: str, dst_base: str,
                  allowlist: Allowlist) -> tuple[Artifact, Allowlist]:
    """Copy an artifact onto a demo server port, widening the allowlist copy
    to that origin. Fault-demo phases use this so they never collide with an
    external `cua serve` on 8791.
    """
    from copy import deepcopy

    raw = artifact.model_dump()

    def _swap(v):
        return v.replace(src_base, dst_base) if isinstance(v, str) else v

    raw["entry_url"] = _swap(raw.get("entry_url"))
    for st in raw.get("steps", []):
        st["value"] = _swap(st.get("value"))
        wait = st.get("wait") or {}
        for k in ("url_contains", "text_contains"):
            if isinstance(wait.get(k), str):
                wait[k] = _swap(wait[k])
        tgt = st.get("target") or {}
        for fb in tgt.get("fallbacks", []) or []:
            for k in ("text",):
                if isinstance(fb.get(k), str):
                    fb[k] = _swap(fb[k])
    for o in raw.get("outcomes", []):
        det = o.get("detect") or {}
        for k in ("url_contains", "text_contains"):
            if isinstance(det.get(k), str):
                det[k] = _swap(det[k])
        o["returns"] = {k2: _swap(v2) for k2, v2 in (o.get("returns") or {}).items()}
    allow2 = deepcopy(allowlist)
    if dst_base not in allow2.origins:
        allow2.origins = list(allow2.origins) + [dst_base]
    return Artifact.model_validate(raw), allow2


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param expects name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def main(argv=None) -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="cua", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the mock MemberServ console")
    s.add_argument("--port", type=int, default=8791)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("discover", help="LLM-driven discovery run -> draft artifact")
    s.add_argument("--goal", required=True)
    s.add_argument("--name", default=None, help="capability name (default: derived from goal)")
    s.add_argument("--param", action="append", default=[], metavar="name=example")
    s.add_argument("--entry", default=None, help="entry URL (default $CUA_APP_URL)")
    s.add_argument("--max-steps", type=int, default=None,
                   help="step ceiling (default: budgets discovery.max_steps)")
    s.add_argument("--deadline-s", type=float, default=None,
                   help="wall-clock timeout (default: budgets discovery.wall_clock_s)")
    s.add_argument("--run-id", default=None)
    s.add_argument("--allowlist", default=None)
    s.add_argument("--budget", action="append", default=[], metavar="group.key=value",
                   help="override a runtime budget (repeatable)")
    s.add_argument("--operator-script", default=None,
                   help="';'-separated operator commands if discovery gets stuck")
    s.add_argument("--operator-repl", action="store_true",
                   help="interactive operator if discovery gets stuck")
    s.add_argument("--headed", action="store_true")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("approve", help="approve a draft artifact (declare business outcomes)")
    s.add_argument("artifact")
    s.add_argument("--preset-lookup-outcomes", action="store_true")
    s.add_argument(
        "--outcome-json", action="append", default=[],
        help='repeatable JSON Outcome: {"id":..., "detect":{...}, "returns":{...}}',
    )
    s.add_argument("--yes", action="store_true",
                   help="non-interactive: skip the typed APPROVE confirmation")
    s.add_argument("--force", action="store_true",
                   help="re-approve an already-approved artifact (re-ledgers)")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("replay", help="deterministic replay of an artifact")
    s.add_argument("artifact")
    s.add_argument("--param", action="append", default=[], metavar="name=value")
    s.add_argument("--allow-draft", action="store_true")
    s.add_argument("--stability", type=int, default=1)
    s.add_argument("--run-id", default=None)
    s.add_argument("--allowlist", default=None)
    s.add_argument("--operator-script", default=None,
                   help="';'-separated operator commands for escalation handoff")
    s.add_argument("--operator-repl", action="store_true")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--budget", action="append", default=[], metavar="group.key=value",
                   help="override a runtime budget (repeatable)")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("demo", help="offline demo phases: matrix | escalation | stability | all")
    s.add_argument("--part", default="all",
                   choices=["discovery-offline", "matrix", "escalation", "stability", "all"])
    s.add_argument("--evidence-root", default=None)
    s.add_argument("--budget", action="append", default=[], metavar="group.key=value",
                   help="override a runtime budget (repeatable), e.g. replay.total_s=30")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("bench", help="latency/cost benchmark -> evidence/performance/bench.json")
    s.add_argument("--runs", type=int, default=5, help="runs per replay case")
    s.add_argument("--live", action="store_true",
                   help="add 3 live LLM probes (~$0.0002) for API latency")
    s.add_argument("--tests", action="store_true", help="time the offline test suite too")
    s.add_argument("--evidence-root", default=None)
    s.add_argument("--budget", action="append", default=[], metavar="group.key=value")
    s.set_defaults(fn=cmd_bench)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
