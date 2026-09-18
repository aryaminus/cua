# Design Report

Seven sections, per the spec. Everything claimed here is backed by a run
under `evidence/`; the evidence-boundary table at the end says what is real,
scripted, or designed-only.

## 1. Architecture

One Python package, one process per run, no services to deploy:

```
goal + params ──► DiscoveryAgent ──(OpenRouter LLM, temp 0)──► trace
                     │  sees: numbered element table (role, name, value) + page text (redacted)
                     │  emits: typed actions against element indexes; never selectors/code
                     ▼
                 Artifact v1  (typed, versioned, draft ──approve──► approved)
                     │
params ──► ReplayEngine ──► Surface ──► live app     # no LLM anywhere on this path
                     │  fresh snapshot per step · locator re-resolution · post-condition waits
                     │  outcome detection → BUSINESS_OUTCOME / recoverable / hard failure
                     ▼
                 ReplayResult (closed typed contract)          evidence/ (redacted, generated)
```

Key decisions and trade-offs:

- **The element table is the seam.** Discovery, replay, and the operator
  protocol all address the surface through one normalized element model
  (role + accessible-name approximation + value), extracted by a single
  module (`surface.py`). The model never sees HTML and never emits selectors —
  it picks an index into observed, validated elements. Swapping in an
  accessibility-tree or OS-level driver means implementing one protocol, not
  touching the schema, replay, or escalation (this is also the §4 story).
- **Sync Playwright, single surface instance per run.** The spec warns
  against premature infrastructure; a queue/service topology would be exactly
  that. The seams (surface, LLM client, operator) are Protocols, so each is
  testable offline with doubles (`FakePage`, `FakeLLM`, `ScriptedOperator`).
- **Jev (TypeSafe's decision model, via OpenRouter's Decisions API) is used
  for exactly one thing:** stuck/no-progress detection in discovery. The
  per-step *choice of action* stays with a generative model, which must also
  produce free text (fill values, output regexes). This mirrors the
  planner/decision-point split we studied (Jev is "a decision point, not an
  agentic loop") without betting the core loop on a week-old service: if the
  decisions call fails, deterministic no-progress rules still terminate the
  run.
- **Determinism levers:** temperature 0 + fixed seed on discovery; replay has
  no model at all; condition-based waits (never sleeps); deterministic mock
  seed data; fault injection by explicit configuration; canonical JSON
  artifacts. Five-run stability is asserted in `evidence/replay-stability/`.

## 2. Artifact schema

`cua/schema.py` (pydantic v2, `schema_version: "1.0"`). A capability artifact
carries: ordered **steps** (goto/click/fill/press_enter — state-changing
actions only); per-step **locators** with declared fallback chains and a
**rationale**; typed **inputs** (`{member_id}` placeholders may appear in any
step value or detect condition); typed **outputs** (extraction regexes with a
capture group, verified against the final page at compile time and again at
replay); per-step **waits** (post-conditions asserting the action landed); a
final **checkpoint** (a state assertion, never action completion); declared
**business outcomes** with detect conditions and parameterized return
payloads; **status** (`draft → approved`); and **provenance** (model, run id,
cost, call count).

Shape decisions worth defending:

- **Locators are role+name with fallbacks, not CSS/XPath.** They reference
  what an accessibility tree would expose, which is the most stable identity a
  legacy surface offers; the fallback chain (`text_contains` → `tag_ordinal`)
  degrades explicitly instead of brittle-ly. Name inference deliberately does
  *not* read `name`/`id` attributes — legacy enterprise markup essentially
  never carries meaningful ones; it infers from label association, placeholder,
  title, or the adjacent table cell (demonstrated live: the mock's "Member ID"
  textbox is named purely from the neighboring `<td>`).
- **Observations are not steps.** Replay re-observes fresh at every step; the
  artifact records only state changes. This keeps artifacts small and makes
  "the recorded screen state" impossible to trust wrongly.
- **Business outcomes are declared at approval time, not discovered.** A
  single happy-path run cannot enumerate the legitimate non-success answers an
  app can give; pretending the model can would be the exact taxonomy confusion
  the spec warns about. The approval step (`cua approve`) is where a human
  who knows the app declares them; unapproved artifacts cannot run unattended
  (`draft` is rejected by default).
- **Outputs are redacted in all persisted evidence** but returned intact to
  the in-process caller — the caller is entitled to the answer; the evidence
  trail is not.

## 3. Determinism & error handling

Replay executes each step as: fresh snapshot → resolve locator (exact
role+name, then contains, then declared fallbacks; one re-snapshot retry
recorded as a recovery) → allowlist guard → act → **post-condition wait**
(bounded poll; business outcomes satisfy waits) → outcome detection. After the
last step: checkpoint assertion, then output extraction.

The result contract is a closed enum (`ReplayResult`):

- **SUCCESS** — checkpoint passed; declared outputs extracted and returned.
- **BUSINESS_OUTCOME** — a declared outcome matched at any step
  (`NOT_FOUND`, `INVALID_INPUT`, `PERMISSION_DENIED` in our artifact; the
  last covers the frozen-record member a teller role may not view). Returns
  the outcome's parameterized payload. "No such member" is an answer, not a
  crash.
- **Recoverable conditions are engine policy, recorded never silent:** JS
  `confirm()` dialogs auto-dismissed and logged (`unexpected_dialog`);
  locator re-snapshot rematch; waits allowed their full budget (a deliberately
  slow member response is absorbed here, inside the timeout, not around it).
  A known-transient "System Busy" page is reloaded exactly once per step
  (`transient_reload`); a second consecutive busy page is treated as a real
  failure, not an infinite wait. Each appears in `result.recoveries`.
- **HARD_FAILURE** — anything else: app 5xx (fault-injected demo), session
  expiry, unresolvable locator, failed post-condition, failed checkpoint.
  Failures carry step id, action, expected vs observed (observed includes a
  redacted excerpt of what the page actually showed), and a screenshot —
  `evidence/replay-hard-failure-server-error/`.

Runtime errors, not UI drift, are the modeled risk (the spec's point about
stable enterprise UIs): waits assert state, locators re-resolve per step, and
the mock's faults exercise each taxonomy class deterministically. Evidence
that it all works end-to-end: `evidence/replay-*` (success, parametrized
success, two business outcomes, hard failure, escalation resume, 5×
stability).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `PageSurface` + the element table. A
legacy-web driver is the same JS over frameset children (iterate
`document.frames`, merge tables with a frame tag); a desktop driver maps the
OS accessibility tree into the same element model and "click" into an AX
action. The artifact schema needs no change — locators already speak
role+name, which is exactly what AX exposes. The one honest gap: pixel-only
surfaces (canvas terminals) need an OCR/segmentation front end feeding the
same table; the schema still holds, perception cost moves.

**Multi-tenant reuse.** Artifacts would gain an `app_signature` (product +
major version, not tenant) and canonical parameterized routes
(`/member/{member_id}` — already how our waits are recorded). Tenant-level
differences (branding, renamed labels, extra interstitials) become an override
layer: per-tenant locator alias sets and extra recovery steps, resolved at
replay start and recorded in the run evidence. Drift management: a scheduled
canary replay per tenant; a failure with `locator_unresolved` or
`checkpoint_failed` is the drift signal — the same taxonomy, reused for
monitoring. We did not build this (the spec says don't); the point is that
the schema's current shape — parameterized values, role+name locators with
fallback chains, declared outcomes — is what makes it possible without
per-tenant re-recording.

## 5. Escalation & handoff

Stuck/blocked detection: in replay, any hard failure after recovery attempts;
in discovery, a deterministic rule (unchanged observable state across steps)
plus an optional Jev decision ("continue vs stuck", confidence-gated) — two
consecutive signals stop the run. The periodic "is this run still making
progress?" check is the community-validated stuck-agent pattern for exactly
this loop shape (verified live against our own discovery trace: continue@0.78
while the run was healthy). On trigger:

1. **Detect & route.** An `intervention.json` is written into the run folder:
   capability, failed step, expected vs observed, current URL, redacted page
   excerpt, screenshot, and the operator command list.
2. **Take control of the live session.** Automation pauses; a `ControlSession`
   state machine (`AUTOMATION → HUMAN → AUTOMATION|ABORTED`) records every
   transition with a reason. The operator drives the **same surface instance**
   through a deliberately bare command protocol (`goto` / `click` / `fill` /
   `look` / `resume` / `abort`) — terminal REPL for a human, script for
   evidence reproduction. The spec's scope note allows exactly this.
3. **Hand back.** `resume` returns control; replay re-runs from the failed
   step against fresh state. Everything the operator did is captured as a
   command log + before/after state, folded into the result
   (`result.escalation`, recovery `operator_handoff`).

Demonstrated live: `evidence/replay-escalation-handoff/` — a session-expiry
fault hard-fails the run; the operator re-authenticates and redoes the lookup
on the live session; the run resumes and completes successfully.

## 6. Safety

Default-deny allowlist (`config/allowlist.json`) enforced before **every**
action in **both** loops: action types, navigation origins + routes (fnmatch),
and risky-control patterns (`role+name_contains`, e.g. the freeze button —
irreversible, requires supervisor approval in the app's own terms). A blocked
attempt is returned to the discovery model as an observation and logged —
`evidence/discovery-live/` shows the model attempting the freeze and being
refused. A post-action URL guard catches unexpected navigations. Artifacts are
inert data: replay executes only the four action types against re-resolved
observed elements.

Data handling: SSNs, card-like numbers, and currency amounts are redacted
from everything persisted (run logs, step records, intervention files,
results) and from everything sent to the model — the discovery loop never sees
a raw SSN (test-asserted). Caller-facing outputs are returned intact in
process. `.env` is gitignored; no credentials exist in artifacts by
construction (only parameter placeholders).

**Limits:** risky-control classification is by declared role/name patterns,
appropriate for a known app catalog — not open-web automation. A pre-execution
safety-monitor pattern of this exact shape (check each proposed action against
policy before it executes) has independent field evidence of catching most
attacks with near-zero false blocks at a fraction of a generative judge's
cost — cited as the validation for our guard placement, not as a new
component. Dialog auto-dismissal is safe for interstitials but would need
policy for dialogs whose choice matters. Redaction is regex-based; a
production system would field-level redaction at the schema, not the
serialization, layer. On the model-supply side: Jev is days old, trained by a
method (RLCD) with independent benchmarks still thin, and third-party data
terms for a launch-week service are not yet enterprise-grade — so every input
here is synthetic fixture data, PII never leaves the fixture set, and
`CUA_DATA_COLLECTION=deny` routes calls only to zero-retention endpoints
(off by default, since it can exclude cheaper providers).

## 7. Cuts

Deliberately not built (with the next step if we continued):

- **Operator console UI** — the command protocol + control-state machine are
  real; a co-browsing console is the natural next build on top.
- **Multi-tenant override layer & canary fleet** — designed (§4), not built;
  next step is `app_signature` + per-tenant alias sets on a second app variant.
- **Desktop / frameset drivers** — the seam exists; a Linux AX (AT-SPI) driver
  would be the first port.
- **Jev beyond stuck detection** (element-choice acceleration) and the
  capability catalog / codegen stretch goals — the decision-provider seam
  (LLM client Protocol) keeps these addable without touching the engine.
- **Parallelism, queues, services** — explicitly not rewarded by the spec;
  the seams make them additive, not necessary.

Built beyond the letter of the spec, cheaply: the 5× stability signal and the
draft→approved gate (both stretch goals the spec lists).

---

### Evidence boundary

| Claim | Evidence | Status |
|---|---|---|
| Real LLM discovery completes the goal and compiles an artifact | `evidence/discovery-live/` (OpenRouter run; provenance in `meta.json`) | real model run |
| Deterministic replay: success, parametrization, both business outcomes, hard failure | `evidence/replay-success-*`, `replay-business-*`, `replay-hard-failure-*` | real browser runs |
| Escalation handoff on the live session | `evidence/replay-escalation-handoff/` (session-expiry fault; scripted operator via the real command protocol) | real mechanism, scripted operator |
| 5× replay stability | `evidence/replay-stability/` | real runs |
| Guardrail refusal on the irreversible action (live model) | `evidence/vet-guard/` | real model run, real refusal |
| Offline discovery demo | `evidence/discovery-offline/` | scripted stand-in, labeled as such |
| Multi-tenant + desktop story | REPORT §4 | design only, per brief |
