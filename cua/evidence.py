"""Evidence: structured, generated (never hand-written) run records.

Every discovery and replay run writes a self-contained folder:

  evidence/<run_id>/
    meta.json          goal/artifact/params (redacted), model, config snapshot
    run.log            human-readable line log
    steps/NNN.json     pre-snapshot (redacted), decision/action, guard verdict,
                       post-url, wait outcome — one file per executed step
    steps/NNN.png      screenshot (failures and final state)
    artifact.json      the compiled artifact (discovery runs)
    result.json        replay result (redacted)
    intervention.json  escalation request, when one was raised

Everything persisted passes through :func:`cua.safety.redact`. Outputs
returned to the *caller* in-process are not redacted — the caller is entitled
to the answer; the evidence trail is not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .safety import redact


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunLog:
    def __init__(self, root: str | Path, run_id: str):
        self.dir = Path(root) / run_id
        self.steps_dir = self.dir / "steps"
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self._log_lines: list[str] = []
        self._meta: dict = {}
        self.started_at = _ts()

    # -- writing ----------------------------------------------------------------

    def meta(self, **kv) -> None:
        self._meta.update(kv, started_at=self.started_at)
        self._write("meta.json", self._meta)

    def finish(self, **end) -> None:
        self._meta.update(end, finished_at=_ts())
        self._write("meta.json", self._meta)
        return str(self.dir)

    def step(self, n: int, record: dict) -> None:
        record["at"] = _ts()
        self._write(f"steps/{n:03d}.json", record)

    def line(self, msg: str) -> None:
        self._log_lines.append(f"{_ts()} {msg}")
        (self.dir / "run.log").write_text("\n".join(self._log_lines) + "\n")

    def artifact(self, artifact_json: str) -> None:
        (self.dir / "artifact.json").write_text(artifact_json)

    def result(self, result: dict) -> None:
        self._write("result.json", result)

    # -- internals --------------------------------------------------------------

    def _write(self, name: str, payload) -> None:
        path = self.dir / name
        path.write_text(json.dumps(redact(payload), indent=2))
