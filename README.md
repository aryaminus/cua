# cua — Computer-Use Automation System

> **The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it in production.**

An LLM ("computer use") figures out how to accomplish a goal against a live
application surface once; the successful run is compiled into a typed,
versioned **capability artifact**; afterwards the artifact **replays
deterministically with no LLM in the loop**, with typed inputs/outputs, a
declared business-outcome contract, and a human-in-the-loop escalation path
that hands over the *live session*.

The target
surface is a bundled mock of the real environment described in the spec: a
server-rendered, table-based "credit-union back-office console" — no test IDs,
no label association, non-semantic markup, JS-confirm dialogs on the
irreversible action, and deterministic fault injection (not-found, validation
error, session expiry, server error).

Design write-up: **[REPORT.md](REPORT.md)** · Run evidence: **[evidence/](evidence/)**

## Quick start

```bash
uv sync                                   # Python 3.12+, creates .venv
uv run playwright install chromium        # one-time browser download
cp .env.example .env                      # add OPENROUTER_API_KEY (discovery only)
uv run pytest -q                          # 76 tests, offline, no keys needed
```

## Demo path — the exact commands

Terminal 1 (the mock back-office app):

```bash
uv run cua serve                          # http://127.0.0.1:8791
```

Terminal 2 — **discovery** (real LLM run → draft artifact):

```bash
uv run cua discover \
  --goal "Look up member {member_id} and report their current savings balance" \
  --param member_id=1001 \
  --run-id discovery-live \
  --name member_balance_capability
# -> evidence/discovery-live/artifact.json  (status: draft)
```

**Approve** (declare the legitimate business outcomes — the human review step):

```bash
uv run cua approve evidence/discovery-live/artifact.json --preset-lookup-outcomes
```

**Replay** (no LLM — the production path):

```bash
uv run cua replay evidence/discovery-live/artifact.json --param member_id=1002
# SUCCESS outputs={'savings_balance': '19,340.00'}
uv run cua replay evidence/discovery-live/artifact.json --param member_id=9999
# BUSINESS_OUTCOME outcome=NOT_FOUND outputs={'message': 'No member found for ID 9999'}
```

**Full matrix + escalation + stability without any API key** (uses the
scripted offline discovery; regenerates the committed evidence):

```bash
uv run cua demo --part all
```

**Escalation demo on its own** (session-expiry fault → hard failure → operator
takes the live session → run resumes and completes):

```bash
uv run cua demo --part escalation
```

**Latency/cost benchmark** (offline replay percentiles, escalation cycle,
server boot; `--live` adds 3 LLM probes ≈ $0.00003; `--tests` times the
suite — writes `evidence/performance/bench.json`, the baseline behind
REPORT.md §7):

```bash
uv run cua bench --runs 5 --live --tests
```

**Guardrail refusal demo** (a live model told to perform the irreversible
freeze — it navigates there, is refused by the allowlist, and declines the
unsafe goal; see REPORT.md §Safety):

```bash
uv run cua discover --goal "Freeze all accounts for member {member_id}" \
  --param member_id=1001 --run-id vet-guard --name member_freeze_vet --max-steps 8
```

Interactive human operator instead of the script: pass `--operator-repl` to
`cua replay` (commands: `goto` / `click <role> '<name>'` / `fill <role>
'<name>' <value>` / `look` / `resume` / `abort`).

## Running without live services

- `uv run pytest -q` — the full engine suite (replay taxonomy, locators,
  guardrails, redaction, escalation) runs offline against an in-memory surface
  double; browser-backed integration tests run the real app locally.
- `uv run cua demo` — every phase except a *live* discovery run works with no
  `OPENROUTER_API_KEY` (it uses the scripted stand-in and says so).
- Replays only ever talk to `http://127.0.0.1:8791` (allowlist-enforced).

## Layout

```
cua/
  mockapp.py      mock "Meridian FCU MemberServ" console (Flask, fault injection)
  surface.py      the seam: numbered element table, locator resolution, Playwright
  schema.py       Artifact v1 + replay result contract (pydantic)
  replay.py       deterministic engine: waits, checkpoints, error taxonomy, handoff retry
  agent.py        LLM discovery loop + artifact compiler (stuck detection incl. Jev)
  safety.py       allowlist (default-deny) + PII/financial redaction
  escalation.py   intervention requests, control-state machine, operator protocol
  budgets.py      typed runtime budgets (config/budgets.json) — enforced, never advisory
  evidence.py     generated run records (everything persisted is redacted)
  openrouter.py   chat completions + OpenRouter Decisions API (typesafe/jev)
  cli.py          serve / discover / approve / replay / demo / bench
config/allowlist.json   origins, routes, action types, risky-control patterns
config/budgets.json     runtime budgets: timeouts, retries, cost/steps caps (REPORT.md §7)
tests/            76 tests: offline engine suite + real-browser integration
evidence/         generated run records (see evidence/README.md)
REPORT.md         the eight-section design write-up
```

## Configuration

See [`.env.example`](.env.example): `OPENROUTER_API_KEY` (discovery only),
`CUA_MODEL` (default `deepseek/deepseek-v4.1-flash`), optional
`CUA_JEV_MODEL` (`typesafe/jev-1.13`, used for stuck detection), `CUA_APP_URL`,
`CUA_ALLOWLIST`, and the mock app's fault switches. Runtime budgets live in
[`config/budgets.json`](config/budgets.json) and can be overridden per run:
`uv run cua replay ... --budget replay.total_s=30`.

## What is real vs mocked

| Piece | Status |
|---|---|
| Discovery loop, replay engine, taxonomy, guardrails, redaction, escalation mechanism | real (this repo) |
| Target application | mock by design — a deliberate stand-in for the bank back-office surface (brief §4) |
| Live discovery run in `evidence/discovery-live/` | real OpenRouter model run (provenance in `meta.json`) |
| Guardrail-refusal run in `evidence/vet-guard/` | real OpenRouter model run: the model attempts the irreversible freeze, the allowlist refuses, the model declines the unsafe goal |
| Offline discovery (`evidence/discovery-offline/`) | scripted stand-in, clearly labeled — for keyless demos |
| Operator UI | deliberately bare: a command protocol (file/script/terminal), per the spec's scope note |
| Multi-tenant / desktop surfaces | design only (REPORT.md §4) — not built, per the spec |

## License

MIT.
