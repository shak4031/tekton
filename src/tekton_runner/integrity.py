"""Cheap mechanical checks run after the agent exits, before reporting success.

Two jobs, both deliberately dumb and deterministic:

**The `.git` guard (ADR-10 property 4).** `.git/hooks/` executes on the HOST with
the runner's privileges, and the checkout is mounted writable, so an agent that
writes `pre-commit` gets host code execution the next time the runner commits —
right where the deploy key lives. Neither the filesystem wall nor the network
wall can see that path; it crosses the boundary through a file the runner itself
later runs.

**The diff check (ADR-8).** At slice 5 the agent emitted a `FinishAction`
claiming "Successfully created ... and confirmed its contents" for a file it had
never written. Nothing verified the claim. A reviewer is the real answer, but a
job that changed no files should never reach one — cheap gate first, expensive
judgment second, the same shape as ADR-4.

Scope note: the guard watches `.git/hooks/` and `.git/config`, not all of `.git`.
Those are the paths ADR-10 names as dangerous, and a broader walk would produce
false positives from ordinary git housekeeping. A probe that cries wolf gets
ignored, and an ignored probe is a dead probe (D3.2b).
"""

from __future__ import annotations

from pathlib import Path

from tekton_runner.checkout import GIT_BASE
from tekton_runner.proc import CommandError, run

# Relative to `.git/`. Everything an agent could turn into host execution.
GIT_WATCHED = ("hooks", "config")

STATUS_TIMEOUT_S = 60


class IntegrityError(RuntimeError):
    """The checkout came back from the agent in a state we refuse to commit."""


def _fingerprint(path: Path) -> tuple[int, int]:
    """Size and mtime, enough to notice a rewrite without reading contents."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def snapshot(checkout: Path) -> dict[str, tuple[int, int]]:
    """Record the watched `.git` paths before the agent is given the mount."""
    git_dir = checkout / ".git"
    marks: dict[str, tuple[int, int]] = {}
    for name in GIT_WATCHED:
        target = git_dir / name
        if target.is_dir():
            for item in sorted(target.rglob("*")):
                if item.is_file():
                    marks[str(item.relative_to(git_dir))] = _fingerprint(item)
        elif target.is_file():
            marks[name] = _fingerprint(target)
    return marks


def assert_unchanged(checkout: Path, before: dict[str, tuple[int, int]]) -> None:
    """Fail the job if the agent touched a `.git` path that runs on the host.

    The agent has no legitimate reason to write there — it has no git tool, no
    remote and no credential (ADR-10 property 4), so any change is either a bug
    or an attack, and both should stop the job.
    """
    after = snapshot(checkout)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if added or removed or modified:
        raise IntegrityError(
            f".git was modified during the task — added={added} "
            f"removed={removed} modified={modified}; refusing to commit"
        )


def has_changes(checkout: Path) -> bool:
    """Return whether the worktree differs from HEAD.

    Uses `git status --porcelain`, which reports tracked modifications and
    untracked files alike, so a brand-new file the agent created counts.
    """
    try:
        out = run(
            (*GIT_BASE, "status", "--porcelain"),
            cwd=checkout,
            timeout=STATUS_TIMEOUT_S,
        )
    except CommandError as exc:
        raise IntegrityError(f"could not read git status in {checkout}: {exc}") from exc
    return bool(out.strip())
