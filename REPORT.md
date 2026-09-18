# Design Report

Seven sections, per the spec. Every claim is backed by a run under
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

Judgment calls the spec leaves open, and why — one per §4 bullet:

- **Language/runtime: Python + Playwright + pydantic + pytest, Hatchling
  build, uv env.** Python for the schema and the offline determinism suite;
  sync Playwright for a single-process loop with no async machinery; uv for
  one-command reproducibility. A second language or service tier adds moving
  parts with no new brief coverage.
- **LLM provider: OpenRouter, deepseek-v4.1-flash.** One key serves the chat
  loop and the Jev decisions endpoint. Loop structure: one typed JSON
  decision per observation (never free text), `reasoning: disabled` plus the
  response-healing plugin for stable actions, temp 0 + fixed seed, and a Jev
  continue/stuck pair asked only when the deterministic no-progress rule
  fires. Recorded cost: $0.0014 discovery, $0 replay.
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
  queues, services, or workers — explicitly unrewarded — with each seam
  testable offline (`FakePage`, `FakeLLM`, `ScriptedOperator`).
- **UI-only by design.** Where an API exists the spec says to integrate
  through it — out of scope here, so the mock exposes none and the system
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

- **Locators are role+name with fallbacks, not CSS/XPath** — the most stable
  identity a legacy surface offers, mirroring an AX tree. Name inference
  ignores `name`/`id` attributes and uses label, placeholder, title, or the
  adjacent table cell — demonstrated live, where "Member ID" is named purely
  from the neighboring `<td>`.
- **Observations are not steps.** Replay re-observes fresh each step.
- **Business outcomes are declared at approval, not discovered.** One
  happy-path run cannot enumerate an app's legitimate non-success answers;
  pretending otherwise is the taxonomy confusion the spec warns about.
  `cua approve` is where a human who knows the app declares them; `draft`
  artifacts are rejected for unattended replay by default.
- **Outputs are redacted in all persisted evidence**, returned intact to the
  in-process caller — the caller is entitled to the answer; the trail is not.

## 3. Determinism & error handling

Each step: fresh snapshot → resolve locator (exact, contains, declared
fallbacks; one recorded re-snapshot retry) → guard → act → **bounded
post-condition poll** (business outcomes satisfy waits) → outcome detection.
Then checkpoint, then extraction.

`ReplayResult` is a closed enum:

- **SUCCESS** — checkpoint passed; declared outputs extracted and returned.
- **BUSINESS_OUTCOME** — a declared outcome matched at any step
  (`NOT_FOUND`, `INVALID_INPUT`, `PERMISSION_DENIED` — the last covers the
  frozen record a teller role may not view). A legitimate answer, not a crash.
- **Recoverable = engine policy, recorded never silent:** auto-dismissed JS
  `confirm()`s (`unexpected_dialog`); locator re-snapshot rematch; full wait
  budgets absorbing a deliberately slow page; a known-transient "System Busy"
  page reloaded exactly once per step (`transient_reload`) — a second
  consecutive busy page fails instead of looping.
- **HARD_FAILURE** — app 5xx, session expiry, unresolvable locator, failed
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
action. The schema is unchanged — locators already speak role+name. Honest
gap: pixel-only surfaces need an OCR front end feeding the same table.

**Multi-tenant reuse.** Artifacts would gain an `app_signature` (product +
major version, not tenant) and canonical parameterized routes
(`/member/{member_id}` — already how waits are recorded). Tenant differences
(branding, renamed labels, extra interstitials) become an override layer:
per-tenant locator aliases + recovery steps, resolved at replay start and
recorded in evidence. Drift management: scheduled canary replays;
`locator_unresolved` / `checkpoint_failed` is the drift signal — the same
taxonomy, reused. Not built (per the spec); the schema's shape —
parameterized values, fallback locators, declared outcomes — is what makes it
possible without per-tenant re-recording.

## 5. Escalation & handoff

Stuck detection: in replay, any hard failure after recovery attempts; in
discovery, a no-change rule plus an optional Jev decision, two consecutive
signals stopping the run — the community-validated "still making progress?"
pattern, verified live (continue@0.78 while healthy). On trigger:

1. **Detect & route.** `intervention.json`: capability, failed step, expected
   vs observed, URL, redacted excerpt, screenshot, command list.
2. **Take over the live session.** Automation pauses; a `ControlSession`
   machine records every transition. The operator drives the **same surface
   instance** via a bare protocol (`goto`/`click`/`fill`/`look`/`resume`/
   `abort`) — REPL for a human, script for evidence, per the scope note.
3. **Hand back.** `resume` returns control; replay continues from the failed
   step. Operator actions fold into `result.escalation` and recovery
   `operator_handoff`.

Demonstrated: `evidence/replay-escalation-handoff/` — expiry fault, operator
re-authenticates and redoes the lookup live, run resumes and completes.

## 6. Safety

Default-deny allowlist enforced before **every** action in **both** loops:
action types, origins + routes, risky-control patterns (e.g. the freeze
button, irreversible without supervisor approval). A blocked attempt returns
to the model as an observation and is logged — `evidence/vet-guard/` shows a
live model attempting the freeze, refused, correctly declining the goal. A
post-action URL guard catches unexpected navigations.

Data handling: SSNs, card-like numbers, amounts redacted from everything
persisted and everything sent to the model — the loop never sees a raw SSN
(test-asserted). Caller outputs return intact in process. `.env` is
gitignored; artifacts hold placeholders, never credentials.

**Limits:** role/name patterns suit a known app catalog, not the open web.
The same pre-execution shape has field evidence of most-attacks caught,
near-zero false blocks, at a fraction of a judge's cost — cited as
validation, not a new component. Dialog auto-dismissal suits interstitials,
not consequential choices. Redaction is regex-based; production would do it
at the schema layer. Model supply: Jev is days old, RLCD benchmarks thin,
launch-week data terms not enterprise-grade — all inputs here are synthetic
fixtures, and `CUA_DATA_COLLECTION=deny` opts into zero-retention endpoints.

## 7. Cuts

Not built, with the next step if we continued:

- **Operator console UI** — protocol + state machine are real; co-browsing is
  the natural next build.
- **Multi-tenant overrides + canary fleet** — designed (§4); next is
  `app_signature` + per-tenant aliases on a second app variant.
- **Desktop / frameset drivers** — the seam exists; a Linux AT-SPI driver
  would be the first port.
- **Jev beyond stuck detection**, capability catalog, codegen — the seams
  keep these additive without touching the engine.
- **Parallelism, queues, services** — explicitly unrewarded; additive, not
  necessary.

Built beyond the letter, cheaply: the 5× stability signal and the
draft→approved gate (both listed stretch goals).

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
| Multi-tenant + desktop story | REPORT §4 | design only, per brief |
