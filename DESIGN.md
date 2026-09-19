# Design

Eight sections. Every claim is backed by a run under
`evidence/`; the boundary table at the end says what is real, scripted, or
designed-only.

## 1. Architecture

One Python package, one process per run, no services to deploy. The
agent-facing product decides *what* to do; this system is *how* it reliably
and safely does it inside software that offers no other way in:

```
goal + params ──► DiscoveryAgent ──(OpenRouter LLM, temp 0)──► trace
                     │  sees: numbered element table + redacted page text
                     │  emits: typed actions on element indexes, never selectors/code
                     ▼
                 Artifact v1  (typed, versioned, draft ──approve──► approved)
                     │
params ──► ReplayEngine ──► Surface ──► live app    # no LLM on this path
                     │  fresh snapshot per step · locator re-resolution · post-condition waits
                     │  outcome detection → BUSINESS_OUTCOME / recoverable / hard failure
                     ▼
                 ReplayResult (closed typed contract)        evidence/ (redacted, generated)
```

Judgment calls and why, one per §4 bullet:

- **Language/runtime: Python + Playwright + pydantic + pytest, Hatchling
  build, uv env.** Python for the schema and the offline determinism suite;
  sync Playwright for a single-process loop with no async machinery; uv for
  one-command reproducibility. A second language or service tier adds moving
  parts the core seams do not need.
- **LLM provider: OpenRouter, deepseek-v4.1-flash.** One key serves the chat
  loop and the Jev decisions endpoint. Loop structure: one typed JSON
  decision per observation (never free text), `reasoning: disabled` plus the
  response-healing plugin for stable actions, temp 0 + fixed seed, and a Jev
  continue/stuck pair asked only when the deterministic no-progress rule
  fires. That pair is calibrated: 12 labeled probes (varied observations =
  progressing, identical repeats = stuck) score 83% rule accuracy, Brier
  0.075 on the noul question and 0.195 on the choice question, with every
  miss a false-stuck on a transient page, the safe direction
  (`cua bench --jev`, `evidence/performance/jev-calibration.json`).
  Recorded cost: $0.0008 discovery, $0 replay.
- **Computer-use technology: Playwright DOM automation narrowed to a
  numbered element table** — not screenshots+coordinates, not raw DOM
  passthrough. Deterministic, near-zero cost, and structurally the
  accessibility-tree analog the §4 story needs. Screenshots are failure
  evidence, not perception; a pixel/OCR front end would feed the same table.
- **Target: a local Flask mock, not a public site.** Determinism (fixed seed
  data), fault injection for every §3.3 class, no ToS or credential risk
  (§9), and evidence any grader can regenerate. The legacy-hostility
  properties (tables, no test IDs, unassociated labels, non-semantic
  controls) are reproduced in the mock instead.
- **Artifact: versioned pydantic JSON (`schema_version: "1.0"`), one file per
  capability, `draft → approved` gate.** JSON because a human reviewer and a
  calling agent both read it without tooling; versioned so replay can refuse
  what it doesn't understand; approval is when a human declares the business
  outcomes a happy-path run cannot enumerate.
- **Determinism: role+name locators with declared fallbacks**
  (`text_contains` → `tag_ordinal`), re-resolved against a fresh snapshot
  every step; waits as bounded post-condition polls, never sleeps; canonical
  JSON; configuration-armed faults; five identical replays in
  `evidence/replay-stability/`.
- **Architecture: single process, sync, Protocols at every seam.** No
  queues, services, or workers (explicitly unrewarded), with each seam
  testable offline (`FakePage`, `FakeLLM`, `ScriptedOperator`).
- **UI-only by design.** Where an API exists, integrating through it is the
  right call; out of scope here, so the mock exposes none and the system
  never assumes one. This layer exists for the no-API long tail only.

## 2. Artifact schema

`cua/schema.py` (pydantic v2, `schema_version: "1.0"`): ordered **steps**
(state changes only); per-step **locators** with fallbacks and **rationale**;
typed **inputs** (`{member_id}` may appear in any value or detect condition);
typed **outputs** (extraction regexes, verified at compile and replay);
per-step **waits** (post-conditions); a final **checkpoint** (state, never
action completion); declared **business outcomes** with detect conditions and
parameterized returns; **status** (`draft → approved`); **provenance** (model,
run id, cost, call count).

Why shaped this way:

- **Locators are role+name with fallbacks, not CSS/XPath**: the most stable
  identity a legacy surface offers, mirroring an AX tree. Name inference
  ignores `name`/`id` attributes and uses label, placeholder, title, or the
  adjacent table cell; demonstrated live, where "Member ID" is named purely
  from the neighboring `<td>`.
- **Observations are not steps.** Replay re-observes fresh each step.
- **Business outcomes are declared at approval, not discovered.** One
  happy-path run cannot enumerate an app's legitimate non-success answers;
  pretending otherwise is the taxonomy confusion this contract exists to
  prevent.
  `cua approve` is where a human who knows the app declares them; `draft`
  artifacts are rejected for unattended replay by default.
- **Outputs are redacted in all persisted evidence**, returned intact to the
  in-process caller: the caller is entitled to the answer; the trail is not.

## 3. Determinism & error handling

Each step: fresh snapshot → resolve locator (exact, contains, declared
fallbacks; one recorded re-snapshot retry) → guard → act → **bounded
post-condition poll** (business outcomes satisfy waits) → outcome detection.
Then checkpoint, then extraction.

`ReplayResult` is a closed enum:

- **SUCCESS**: checkpoint passed; declared outputs extracted and returned.
- **BUSINESS_OUTCOME**: a declared outcome matched at any step
  (`NOT_FOUND`, `INVALID_INPUT`, `PERMISSION_DENIED`; the last covers the
  frozen record a teller role may not view). A legitimate answer, not a crash.
- **Recoverable = engine policy, recorded never silent:** auto-dismissed JS
  `confirm()`s (`unexpected_dialog`); locator re-snapshot rematch; full wait
  budgets absorbing a deliberately slow page; a known-transient "System Busy"
  page reloaded exactly once per step (`transient_reload`): a second
  consecutive busy page fails instead of looping.
- **HARD_FAILURE**: app 5xx, session expiry, unresolvable locator, failed
  wait or checkpoint. Carries step id, expected vs observed (redacted page
  excerpt), and a screenshot.

The modeled risk is runtime errors, not UI drift: waits assert state,
locators re-resolve per step, and the mock's faults exercise each class
deterministically (`evidence/replay-*`: success, parametrization, three
business outcomes, slow/busy recoveries, hard failure, escalation, 5×
stability).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `PageSurface` + the element table: a
legacy-web driver is the same JS over frameset children; a desktop driver
maps the OS accessibility tree into the same model and "click" into an AX
action. The schema is unchanged: locators already speak role+name. Honest
gap: pixel-only surfaces need an OCR front end feeding the same table.

**Multi-tenant reuse.** Artifacts would gain an `app_signature` (product +
major version, not tenant) and canonical parameterized routes
(`/member/{member_id}`, already how waits are recorded; the one field that
cannot generalize as-is is the absolute `entry_url` origin, which would move
into the per-tenant binding alongside it). Tenant differences
(branding, renamed labels, extra interstitials) become an override layer:
per-tenant locator aliases + recovery steps, resolved at replay start and
recorded in evidence. Drift management: scheduled canary replays;
`locator_unresolved` / `checkpoint_failed` is the drift signal: the same
taxonomy, reused. Not built; the schema's shape
(parameterized values, fallback locators, declared outcomes) is what makes it
possible without per-tenant re-recording.

## 5. Escalation & handoff

Stuck detection: in replay, any hard failure after recovery attempts; in
discovery, a no-change rule plus an optional Jev decision, two consecutive
signals stopping the run: the classic "still making progress?" loop
guard, decided by a typed question instead of free-text self-report. Calibration (`evidence/performance/jev-calibration.json`): all six stuck
probes caught (confidence ≥ 0.99); four of six healthy probes rule-continue
(two at 0.89–0.90, two transient cases leaning stuck at low confidence,
the safe direction). On trigger:

1. **Detect & route.** `intervention.json`: capability, failed step, expected
   vs observed, URL, redacted excerpt, screenshot, command list.
2. **Take over the live session.** Automation pauses; a `ControlSession`
   machine records every transition. The operator drives the **same surface
   instance** via a bare protocol (`goto`/`click`/`fill`/`look`/`resume`/
   `abort`): REPL for a human, script for evidence.
3. **Hand back.** `resume` returns control; replay continues from the failed
   step. Operator actions fold into `result.escalation` and recovery
   `operator_handoff`.

Demonstrated: `evidence/replay-escalation-handoff/`: expiry fault, operator
re-authenticates and redoes the lookup live, run resumes and completes.

## 6. Safety

Default-deny allowlist enforced before **every** action in **both** loops:
action types, origins + routes, risky-control patterns (e.g. the freeze
button, irreversible without supervisor approval). A blocked attempt returns
to the model as an observation and is logged: `evidence/vet-guard/` shows a
live model attempting the freeze, refused, correctly declining the goal. A
post-action URL guard catches unexpected navigations.

Data handling: SSNs, card-like numbers, amounts redacted from everything
persisted and everything sent to the model: the loop never sees a raw SSN
(test-asserted). Caller outputs return intact in process. `.env` is
gitignored; artifacts hold placeholders, never credentials.

**Limits:** role/name patterns suit a known app catalog, not the open web.
A rule-based pre-execution check is cheaper than a model judge, cannot
itself be prompt-injected, and is fully coverable by offline tests: the
component we can prove, not probabilistically vouch for. Dialog auto-dismissal suits interstitials,
not consequential choices. Redaction is regex-based, including a PAN
pattern that deliberately over-matches long digit runs (safe direction for
fixtures; production would scope it at the schema layer). Model supply: Jev
is days old, RLCD benchmarks thin, launch-week data terms not
enterprise-grade: all inputs here are synthetic fixtures, and
`CUA_DATA_COLLECTION=deny` opts into zero-retention endpoints.

## 7. Budgets, latency, cost

Budgets are runtime posture, deliberately outside the artifact schema:
`config/budgets.json` (typed, compiled defaults, `--budget group.key=value`
overrides) is enforced (never advisory) and every replay result
self-describes its envelope (`result.json` gains `budgets.{in_effect,
actuals}`). Breaches are typed failures with `budget exceeded` reasons.

**Network behavior** (per OpenRouter's documented error contract,
[openrouter.ai/docs/api_reference/errors-and-debugging](https://openrouter.ai/docs/api_reference/errors-and-debugging)):
408/429/5xx and
transport errors retried up to 3 attempts with backoff; `Retry-After`
honored on 429/503 (capped at 30s); 4xx client errors (bad key, bad
request, moderation) never retried. Timeouts split connect 10s / read 60s
/ write 30s so a dead network fails in seconds, not minutes.

**Measured baseline** (`cua bench`, `evidence/performance/bench.json`;
5 runs/case; live probe 3 calls; 82 offline tests):

| Case | p50 | p95 | Expectation |
|---|---|---|---|
| replay success | 742 ms | 757 ms | sub-second, local-CPU bound |
| replay business outcome | 607 ms | 617 ms | fastest exit: 2 steps |
| slow page absorbed (2.5 s fault) | 3.27 s | 3.28 s | ≈ fault delay + ε, wait budget absorbs it |
| transient busy + reload | 742 ms | 797 ms | one reload, still sub-second |
| escalation cycle (scripted) | 4.75 s | | dominated by operator actions, not engine |
| server cold boot | 36 ms | | trivial |
| LLM call (live, JSON mode) | 2.14 s | | 4 calls ≈ 8 s discovery; network RTT dominates |
| discovery end-to-end (live) | 9.0 s | | $0.0008, 4 LLM calls |
| offline test suite | 21.6 s | | 82 tests, no keys |

**Enforced budgets and headroom:** replay per-step wait 3s (observed p95
under 1s; slow-fault pages use ~2.6s of it, deliberate), act timeout 10s,
whole-run 180s (≈55× the slowest replay case); discovery wall clock 600s, 40 steps,
60 LLM calls, cost $0.50 (≈600× the $0.0008 observed run; a runaway loop
stops at cents, not dollars). Cost is enforced pre-call: breach stops the
loop cleanly with a `budget exceeded` reason (test-asserted, as are the
retry ladder and every cap above).

**Real-world expectations:** replay latency is local (browser + app)
bound and scales with steps, not load; discovery latency is LLM-RTT bound
(4 × ~2s today, model-swap invariant since calls stay single-digit);
escalation latency is dominated by the human. Costs are bounded by config,
not by hope.

## 8. Cuts

Not built, with the next step if we continued:

- **Operator console UI**: protocol + state machine are real; co-browsing is
  the natural next build.
- **Multi-tenant overrides + canary fleet**: designed (§4); next is
  `app_signature` + per-tenant aliases on a second app variant.
- **Desktop / frameset drivers**: the seam exists; a Linux AT-SPI driver
  would be the first port.
- **Jev beyond stuck detection**, capability catalog, codegen: the seams
  keep these additive without touching the engine.
- **Parallelism, queues, services**: explicitly unrewarded; additive, not
  necessary.

Built beyond the letter, cheaply: the 5× stability signal, the draft→approved
gate (now a real ceremony: capability sheet, pre-approval dry-run validation,
typed confirmation, ledgered decision, the approval-ceremony extension), and
per-port fault servers so evidence regenerates cleanly.

---

### Evidence boundary

| Claim | Evidence | Status |
|---|---|---|
| Real LLM discovery → compiled artifact | `evidence/discovery-live/` (provenance in `meta.json`) | real model run |
| Deterministic replay: success, parametrization, 3 business outcomes, slow/busy recoveries, hard failure | `evidence/replay-*` | real browser runs |
| Escalation handoff on the live session | `evidence/replay-escalation-handoff/` | real mechanism, scripted operator |
| 5× replay stability | `evidence/replay-stability/` | real runs |
| Guardrail refusal on the irreversible action (live model) | `evidence/vet-guard/` | real model run, real refusal |
| Offline discovery demo | `evidence/discovery-offline/` | scripted stand-in, labeled as such |
| Multi-tenant + desktop story | §4 | design only |
