"""Claim one real job from Hermes and print exactly what came back.

Deliberately prints the RAW payload, not the parsed Job: this is the first
run that is not a closed loop (client written against a stub written by the
same author), so the point is to read Hermes' actual field names off the wire
rather than trust a document. Same discipline as probing the registry before
writing a tag into config.

Usage, from ~/tekton, all on one line:

  RUNNER_SECRET='...' HERMES_URL=http://192.168.1.201:8787 \
  TEKTON_WORKER=tekton-rig uv run python probes/claim_probe.py

Create a job from Telegram first: `new job: lifepilot: probe slice 2`
Cancel it afterwards: `/cancel <id>`
"""

import json

from tekton_runner.queue import QueueClient, QueueError


def main() -> int:
    """Claim a job, print the raw payload, then heartbeat once."""
    try:
        client = QueueClient.from_env()
    except QueueError as exc:
        print(f"config error: {exc}")
        return 2

    print(f"POST {client.base_url}/jobs/claim  as worker={client.worker}")
    try:
        job = client.claim()
    except QueueError as exc:
        print(f"claim failed: {type(exc).__name__}: {exc}")
        return 1

    if job is None:
        print("204 - queue is empty. Create a job from Telegram and re-run.")
        return 0

    print("--- raw payload from Hermes ---")
    print(json.dumps(job.raw, indent=2, sort_keys=True))
    print("--- as parsed by Job.from_payload ---")
    print(f"id={job.id!r} project={job.project!r} state={job.state!r} "
          f"attempts={job.attempts!r} worker={job.worker!r}")

    missing = [f for f in ("project", "description", "state") if not getattr(job, f)]
    if missing:
        print(f"NOTE: empty after parse, check field names in raw: {missing}")
    if "attempts" not in job.raw:
        print("NOTE: no 'attempts' field on the wire - the tier-bump-on-retry "
              "decision depends on it")

    try:
        client.heartbeat(job.id)
        print("heartbeat: ok")
    except QueueError as exc:
        print(f"heartbeat failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
