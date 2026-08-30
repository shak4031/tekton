"""Run external commands the way every runner call needs them run.

Extracted at slice 4. `checkout.py` had a subprocess wrapper and `context.py`
was about to grow an identical one — the accretion ADR-7's duplication rule
exists to catch, before a third caller made it obvious.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_TIMEOUT_S = 60


class CommandError(RuntimeError):
    """A command failed, timed out, or is not installed on this host."""


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Run argv and return stdout; raise CommandError on any failure.

    No shell, ever: argv is passed as a list so nothing in a job description
    can become a shell metacharacter. A timeout is mandatory because the runner
    is unattended — a command that hangs would hold the job until the Hermes
    reaper kills it 900 seconds later.
    """
    try:
        done = subprocess.run(  # noqa: S603 - list argv, no shell
            list(args),
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"{args[0]} is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{' '.join(args)} timed out after {timeout}s") from exc
    if done.returncode != 0:
        raise CommandError(f"{' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout
