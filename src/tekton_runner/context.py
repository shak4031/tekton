"""Pack task-relevant context for the agent with repomix (D3.3, Charter §4).

Runs on the rig HOST as a pre-task step and writes `.tekton/context.md` into
the mounted checkout, where the agent reads it as an ordinary file. Placement
is deliberate (D3.3 Option C): the workspace is ephemeral, so in-sandbox `npx`
would re-download per task over the deliberately narrow network, and running
it outside keeps a build-time dependency out of the sandbox entirely.

Charter §4 says the developer "reads the map and retrieves only relevant
slices, never the whole codebase." Scope comes from the project's committed
`repomix.config.json` (ADR-3: scope lives with the repo), optionally narrowed
per task. The mechanical backstop is a token ceiling that fails the job rather
than handing the agent a context it cannot hold.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from tekton_runner.checkout import GIT_BASE
from tekton_runner.proc import CommandError, run

# Pinned per TOOL-ADOPTION §2; bump only via PR through the human gate (ADR-9).
REPOMIX_PIN = "repomix@1.16.0"

CONTEXT_DIR = ".tekton"
CONTEXT_FILE = "context.md"
REPOMIX_TIMEOUT_S = 300
IGNORE_CHECK_TIMEOUT_S = 30

# D1.2 runs Ollama at num_ctx 16384. The pack shares that window with the task,
# the tool definitions and the agent's own output, so half of it is the ceiling.
# A number to calibrate against real D4 jobs, not a law (ADR-7).
MAX_CONTEXT_TOKENS = 8000

# Fallback when the summary cannot be parsed; repomix's own ratio is close to 4.
CHARS_PER_TOKEN = 4

# Tag-lesson #4: `npx` in WSL resolved to a vendored WINDOWS Node via PATH
# interop, which cannot handle Linux paths and reported "config file not found".
_WINDOWS_PATH_PREFIX = "/mnt/c/"

_SUMMARY_PATTERNS = {
    "files": re.compile(r"Total Files:\s*([\d,]+)"),
    "tokens": re.compile(r"Total Tokens:\s*([\d,]+)"),
    "chars": re.compile(r"Total Chars:\s*([\d,]+)"),
}
_SECURITY_CLEAN = "No suspicious files detected"


class ContextError(RuntimeError):
    """Base class for every failure packing context."""


class ContextTooLargeError(ContextError):
    """The pack exceeds the token ceiling; the agent could not hold it."""


class SecretsFoundError(ContextError):
    """Repomix flagged suspicious files in the pack.

    A context pack is exactly where a stray `.env` would leak into a prompt,
    so a finding fails the job rather than warning.
    """


@dataclass(frozen=True)
class ContextPack:
    """The result of one pack, for the job's structured log line."""

    path: Path
    tokens: int
    files: int | None
    chars: int | None
    estimated: bool


def npx_binary() -> str:
    """Locate a LINUX npx, refusing the Windows one PATH interop offers.

    Tag-lesson #4: the thing that answers is not always the thing you meant.
    """
    found = shutil.which("npx")
    if found is None:
        raise ContextError("npx not found; install Node inside WSL, not only on Windows")
    if found.startswith(_WINDOWS_PATH_PREFIX):
        raise ContextError(
            f"npx resolved to the Windows binary at {found}; "
            "install Node 22 inside WSL so a Linux npx wins the PATH race"
        )
    return found


def ensure_ignored(checkout: Path) -> None:
    """Fail unless the checkout ignores `.tekton/`.

    Without this the pack lands in the agent's diff and rides into a PR. The
    check asks git rather than reading `.gitignore`, so it is right regardless
    of which ignore file or pattern the project used.
    """
    target = f"{CONTEXT_DIR}/{CONTEXT_FILE}"
    try:
        run(
            (*GIT_BASE, "check-ignore", "-q", target),
            cwd=checkout,
            timeout=IGNORE_CHECK_TIMEOUT_S,
        )
    except CommandError as exc:
        raise ContextError(
            f"{checkout} does not ignore {CONTEXT_DIR}/ — add it to .gitignore, "
            "or the context pack will be committed into a PR"
        ) from exc


def _parse_summary(stdout: str) -> dict[str, int | None]:
    """Pull the Pack Summary counts out of repomix's output."""
    parsed: dict[str, int | None] = {}
    for key, pattern in _SUMMARY_PATTERNS.items():
        match = pattern.search(stdout)
        parsed[key] = int(match.group(1).replace(",", "")) if match else None
    return parsed


def _repomix_argv(include: str | None) -> list[str]:
    """Build the pinned repomix invocation."""
    argv = [
        npx_binary(),
        "-y",
        REPOMIX_PIN,
        "--style",
        "markdown",
        "--compress",
        "--output",
        f"{CONTEXT_DIR}/{CONTEXT_FILE}",
    ]
    if include:
        argv += ["--include", include]
    return argv


def _measure(out_path: Path, summary: dict[str, int | None]) -> tuple[int, bool]:
    """Return (tokens, estimated). Falls back to file size if parsing missed."""
    tokens = summary.get("tokens")
    if tokens is not None:
        return tokens, False
    return out_path.stat().st_size // CHARS_PER_TOKEN, True


def pack(
    checkout: Path,
    include: str | None = None,
    max_tokens: int = MAX_CONTEXT_TOKENS,
) -> ContextPack:
    """Pack the checkout into `.tekton/context.md` and return what it cost.

    Raises rather than truncating: a silently shortened context is the
    anti-hallucination failure Charter §4 exists to prevent.
    """
    ensure_ignored(checkout)
    out_path = checkout / CONTEXT_DIR / CONTEXT_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stdout = run(_repomix_argv(include), cwd=checkout, timeout=REPOMIX_TIMEOUT_S)
    except CommandError as exc:
        raise ContextError(f"repomix failed: {exc}") from exc
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ContextError(f"repomix reported success but {out_path} is empty")

    summary = _parse_summary(stdout)
    if _SECURITY_CLEAN not in stdout:
        raise SecretsFoundError(f"repomix flagged suspicious files in {checkout}; pack not used")
    tokens, estimated = _measure(out_path, summary)
    if tokens > max_tokens:
        raise ContextTooLargeError(
            f"context pack is {tokens} tokens, over the {max_tokens} ceiling — "
            "narrow the scope with repomix.config.json or an --include pattern"
        )
    return ContextPack(
        path=out_path,
        tokens=tokens,
        files=summary.get("files"),
        chars=summary.get("chars"),
        estimated=estimated,
    )
