"""Slice 5 smoke run: one real task, end to end, in the real sandbox.

Proves the parts the unit tests deliberately cannot: that the pinned image
starts, joins tekton-net, mounts exactly one checkout, reaches the router, and
that the agent edits a file that lands on the host.

Run from ~/tekton after `set -a; . ./.env; set +a`:
    uv run python probes/slice5_smoke.py
"""

import sys
from pathlib import Path

from tekton_runner.checkout import prepare, reclaim
from tekton_runner.classify import choose_tier
from tekton_runner.context import pack
from tekton_runner.conversation import run_task
from tekton_runner.workspace import sandbox_spec

JOB_ID = "sandbox-test-smoke"
DESCRIPTION = (
    "Create the file /workspace/project/SLICE5.md containing exactly one line: "
    "slice 5 lives. Then read it back and confirm the contents."
)


def main() -> int:
    """Prepare, pack, classify, run, and report."""
    checkout = prepare("sandbox-test")
    print(f"checkout: {checkout}")

    context = pack(checkout)
    print(f"context:  {context.tokens} tokens, {context.files} files")

    tier, note = choose_tier(DESCRIPTION, attempts=1)
    print(f"planning: {note}")

    spec = sandbox_spec(checkout)
    print(f"sandbox:  {spec.image} on {spec.network}")
    print(f"mount:    {spec.volumes[0]}")

    outcome = run_task(spec, DESCRIPTION, JOB_ID, tier=tier)
    print(f"outcome:  completed={outcome.completed} events={outcome.events} tier={outcome.tier}")
    print(f"trail:    {outcome.log_path}")
    if outcome.error:
        print(f"error:    {outcome.error}")

    reclaim(checkout)
    print("reclaim:  ownership returned to the host user")

    landed = checkout / "SLICE5.md"
    if landed.is_file():
        print(f"artifact: {landed} -> {landed.read_text().strip()!r}")
        landed.unlink()
    else:
        print("artifact: SLICE5.md did NOT land on the host")
        print(f"          read the trail: {outcome.log_path}")
        return 1
    return 0 if outcome.completed else 1


if __name__ == "__main__":
    sys.exit(main())
