# Design Report

Seven sections, per the spec. Every claim is backed by a run under
`evidence/`; the boundary table at the end says what is real, scripted, or
designed-only.

## 1. Architecture

One Python package, one process per run, no services to deploy:

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

Key decisions:

- **The element table is the seam.** Discovery, replay, and the operator
  protocol all address the surface through one normalized element model
  (role + accessible-name approximation + value) extracted by a single module
  (`surface.py`). The model never sees HTML and never emits selectors — it
  picks an index into observed, validated elements. A new driver (frameset
  walker, OS accessibility tree) implements one protocol; schema, replay, and
  escalation don't change (also the §4 story).
- **Sync Playwright, one surface per run.** The spec penalizes premature
  infrastructure; queues/services would be exactly that. The seams (surface,
  LLM client, operator) are Protocols, so each is testable offline with
  doubles (`FakePage`, `FakeLLM`, `ScriptedOperator`).
- **Jev (TypeSafe's decision model, via OpenRouter's Decisions API) does one
  thing:** stuck/no-progress detection in discovery. Action choice stays with
  a generative model, which must also produce free text (fill values, output
  regexes) — the planner/decision-point split, without betting the core loop
  on a week-old service. If the decisions call fails, deterministic
  no-progress rules still terminate the run.
- **Determinism levers:** temp 0 + fixed seed; replay has no model;
  condition-based waits (never sleeps); deterministic mock seed data;
  configuration-armed faults; canonical JSON artifacts. Five-run stability in
  `evidence/replay-stability/`.

## 2. Artifact schema

`cua/schema.py` (pydantic v2, `schema_version: "1.0"`): ordered **steps**
(goto/click/fill/press_enter — state changes only); per-step **locators**
with fallback chains and **rationale**; typed **inputs** (`{member_id}` may
appear in any step value or detect condition); typed **outputs** (extraction
regexes, verified at compile time and replay); per-step **waits**
(post-conditions); a final **checkpoint** (a state assertion, never action
completion); declared **business outcomes** with detect conditions and
parameterized returns; **status** (`draft → approved`); **provenance**
(model, run id, cost, call count).

Why shaped this way:

- **Locators are role+name with fallbacks, not CSS/XPath** — the most stable
  identity a legacy surface offers, mirroring what an AX tree exposes. Name
  inference deliberately ignores `name`/`id` attributes (legacy markup never
  carries meaningful ones); it uses label association, placeholder, title, or
  the adjacent table cell — demonstrated live, where "Member ID" is named
  purely from the neighboring `<td>`.
- **Observations are not steps.** Replay re-observes fresh each step, so a
  recorded screen state can never be trusted wrongly.
- **Business outcomes are declared at approval, not discovered.** One
  happy-path run cannot enumerate an app's legitimate non-success answers;
  pretending otherwise is the taxonomy confusion the spec warns about.
  `cua approve` is where a human who knows the app declares them; `draft`
  artifacts are rejected for unattended replay by default.
- **Outputs are redacted in all persisted evidence**, returned intact to the
  in-process caller — the caller is entitled to the answer; the trail is not.

## 3. Determinism & error handling

Each replay step: fresh snapshot → resolve locator (exact role+name, then
contains, then declared fallbacks; one re-snapshot retry recorded as a
recovery) → allowlist guard → act → **bounded post-condition poll** (business
outcomes satisfy waits) → outcome detection. Then checkpoint assertion, then
extraction.

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
  wait or checkpoint. Carries step id, expected vs observed (with a redacted
  excerpt of the actual page), and a screenshot.

Runtime errors, not UI drift, are the modeled risk: waits assert state,
locators re-resolve per step, and the mock's faults exercise each taxonomy
class deterministically (`evidence/replay-*`: success, parametrization, three
business outcomes, slow/busy recoveries, hard failure, escalation resume, 5×
stability).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `PageSurface` + the element table: a
legacy-web driver is the same JS over frameset children; a desktop driver
maps the OS accessibility tree into the same element model and "click" into
an AX action. The schema is unchanged — locators already speak role+name.
Honest gap: pixel-only surfaces need an OCR/segmentation front end feeding
the same table; schema holds, perception cost moves.

**Multi-tenant reuse.** Artifacts would gain an `app_signature` (product +
major version, not tenant) and canonical parameterized routes
(`/member/{member_id}` — already how waits are recorded). Tenant differences
(branding, renamed labels, extra interstitials) become an override layer:
per-tenant locator aliases + extra recovery steps, resolved at replay start
and recorded in the evidence. Drift management: scheduled canary replays per
tenant; `locator_unresolved` / `checkpoint_failed` is the drift signal —
the same taxonomy, reused for monitoring. Not built (per the spec); the
schema's current shape — parameterized values, fallback locators, declared
outcomes — is what makes it possible without per-tenant re-recording.

## 5. Escalation & handoff

Stuck detection: in replay, any hard failure after recovery attempts; in
discovery, a no-change rule plus an optional Jev decision ("continue vs
stuck", confidence-gated), two consecutive signals stopping the run — the
community-validated "is this run still making progress?" pattern, verified
live on our own trace (continue@0.78 while healthy). On trigger:

1. **Detect & route.** `intervention.json` in the run folder: capability,
   failed step, expected vs observed, URL, redacted page excerpt, screenshot,
   operator command list.
2. **Take over the live session.** Automation pauses; a `ControlSession`
   machine (`AUTOMATION → HUMAN → AUTOMATION|ABORTED`) records every
   transition with a reason. The operator drives the **same surface
   instance** through a deliberately bare protocol
   (`goto`/`click`/`fill`/`look`/`resume`/`abort`) — REPL for a human, script
   for evidence. The spec's scope note allows exactly this.
3. **Hand back.** `resume` returns control; replay continues from the failed
   step. Everything the operator did is captured as command log + state,
   folded into `result.escalation` and recovery `operator_handoff`.

Demonstrated: `evidence/replay-escalation-handoff/` — session-expiry fault
hard-fails the run; the operator re-authenticates and redoes the lookup live;
the run resumes and completes.

## 6. Safety

Default-deny allowlist (`config/allowlist.json`) enforced before **every**
action in **both** loops: action types, origins + routes, and risky-control
patterns (`role+name_contains` — e.g. the freeze button, irreversible without
supervisor approval). A blocked attempt returns to the model as an
observation and is logged — `evidence/vet-guard/` shows a live model
attempting the freeze, being refused, and correctly declining the unsafe
goal. A post-action URL guard catches unexpected navigations. Artifacts are
inert: replay runs four action types against re-resolved observed elements.

Data handling: SSNs, card-like numbers, and amounts are redacted from
everything persisted and everything sent to the model — the loop never sees
a raw SSN (test-asserted). Caller-facing outputs return intact in process.
`.env` is gitignored; artifacts hold placeholders, never credentials.

**Limits:** risky classification is role/name patterns — right for a known
app catalog, not the open web. The same pre-execution shape has independent
field evidence of catching most attacks near-zero false blocks at a fraction
of a judge's cost — cited as validation of our guard placement, not a new
component. Dialog auto-dismissal suits interstitials, not consequential
choices. Redaction is regex-based; production would do it at the schema
layer. Model supply: Jev is days old, RLCD benchmarks still thin, launch-week
data terms not enterprise-grade — so all inputs here are synthetic fixtures
and `CUA_DATA_COLLECTION=deny` routes calls to zero-retention endpoints (off
by default, since it can exclude cheaper providers).

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
