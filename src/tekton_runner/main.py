"""Tekton runner entry point: claim a job, run it, report back.

Deliberately the shortest module in the package (ADR-7). It owns sequencing and
nothing else — every decision it makes was already made somewhere with tests:
the registry in `checkout`, the token ceiling in `context`, the tier in
`classify`, the pin and the walls in `workspace`, the wire format in `queue`.

The pipeline, in order:

    claim → prepare checkout → snapshot .git → pack context → classify tier
    → planning:building → run the agent in the sandbox → reclaim ownership
    → verify .git → verify the diff is non-empty → report

Run as a service:  `uv run python -m tekton_runner.main`
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from tekton_runner.checkout import prepare, reclaim
from tekton_runner.classify import choose_tier
from tekton_runner.context import pack
from tekton_runner.conversation import run_task
from tekton_runner.integrity import assert_unchanged, has_changes, snapshot
from tekton_runner.queue import (
    OUTCOME_DONE,
    OUTCOME_FAILED,
    Job,
    QueueClient,
    QueueError,
    QueueUnavailableError,
    heartbeating,
)
from tekton_runner.workspace import sandbox_spec

ENV_PATH = Path("~/tekton/.env")
STATE_BUILDING = "building"
POLL_SECONDS = 15
IDLE_SECONDS = 60
SUMMARY_CHARS = 500


class RunnerError(RuntimeError):
    """The job ran but its result is not acceptable."""


def log(event: str, **fields: object) -> None:
    """Emit one structured JSON line (Charter §4)."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    line.update(fields)
    print(json.dumps(line, default=str), flush=True)


def load_env(path: Path = ENV_PATH) -> int:
    """Load `KEY=value` lines into the environment; return how many were set.

    Neither `uv run` nor a systemd unit reads `.env` — the Pi containers only get
    theirs because Compose injects it, and a service has no shell. Fifteen
    lines here rather than a dependency, for the same reason `queue.py` uses
    stdlib for four POSTs (ADR-9's anti-accretion clause).
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        return 0
    count = 0
    for raw in resolved.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        count += 1
    return count


def execute(client: QueueClient, job: Job) -> str:
    """Run one job to completion. Returns a summary; raises on any failure."""
    checkout = prepare(job.project)
    guard = snapshot(checkout)
    packed = pack(checkout)
    tier, note = choose_tier(job.description, job.attempts)
    client.set_state(job.id, STATE_BUILDING, note)
    log("planned", cid=job.id, tier=tier, context_tokens=packed.tokens)

    spec = sandbox_spec(checkout)
    with heartbeating(client, job.id):
        outcome = run_task(spec, job.description, job.id, tier=tier)

    reclaim(checkout)
    assert_unchanged(checkout, guard)
    if outcome.error:
        raise RunnerError(outcome.error)
    if not has_changes(checkout):
        raise RunnerError("agent finished but changed no files")
    return f"{tier}, {outcome.events} events, {packed.tokens} context tokens"


def run_job(client: QueueClient, job: Job) -> bool:
    """Execute a job and report its outcome. Returns whether it succeeded.

    Every failure path reports `failed` rather than leaving the job to be
    reaped: a job that fails loudly at attempt 1 can be retried at a higher
    tier, and a job that goes silent burns 900 seconds first.
    """
    try:
        summary = execute(client, job)
    except Exception as exc:  # noqa: BLE001 - one job's failure is not the runner's
        log("job_failed", cid=job.id, error=str(exc))
        client.report_result(job.id, OUTCOME_FAILED, str(exc)[:SUMMARY_CHARS])
        return False
    log("job_done", cid=job.id, summary=summary)
    client.report_result(job.id, OUTCOME_DONE, summary[:SUMMARY_CHARS])
    return True


def poll(client: QueueClient) -> bool:
    """Claim and run one job if the queue has work. Returns whether it did."""
    job = client.claim()
    if job is None:
        return False
    log("claimed", cid=job.id, project=job.project, attempts=job.attempts)
    run_job(client, job)
    return True


def stop_event() -> threading.Event:
    """An Event that SIGTERM and SIGINT set, so systemd can stop us cleanly."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def main() -> int:
    """Poll Hermes for work until asked to stop."""
    load_env()
    client = QueueClient.from_env()
    log("runner_start", worker=client.worker, hermes=client.base_url)
    stop = stop_event()
    while not stop.is_set():
        try:
            worked = poll(client)
        except QueueUnavailableError as exc:
            log("queue_unavailable", error=str(exc))
            stop.wait(IDLE_SECONDS)
            continue
        except QueueError as exc:
            log("queue_error", error=str(exc))
            stop.wait(POLL_SECONDS)
            continue
        if not worked:
            stop.wait(POLL_SECONDS)
    log("runner_stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
