"""Choose the model tier for a job (ADR-2, as amended at slice 2).

The runner classifies rather than Hermes, because the runner can read
`attempts` off the claim and bump the tier on a retry — ADR-2's "escalation
survives as backstop" obtained for free rather than built later. A tier stamped
at intake would be baked into the row, so the retry would repeat the same
misclassification.

Deterministic on purpose: a rules table over the description, zero tokens,
Hermes-style determinism moved one box right. Flip to an LLM classifier only
if real D4 jobs show the rules are bad — evidence, not vibes.

The agent never sees a model name, only a tier alias; routing stays in LiteLLM
where ADR-2 put it.
"""

from __future__ import annotations

import re

TIER_FAST = "tekton-fast"
TIER_DEFAULT = "tekton-default"
TIER_CLOUD = "tekton-cloud"

# Ordered cheapest first; a retry moves one step right.
TIER_LADDER = (TIER_FAST, TIER_DEFAULT, TIER_CLOUD)

# ADR-2's own list: renames, boilerplate, docstrings, config tweaks, and
# single-file fixes with existing tests.
_MECHANICAL = re.compile(
    r"\b("
    r"rename|renaming|typo|typos|spelling|"
    r"docstring|docstrings|comment|comments|"
    r"format|formatting|reformat|lint|linting|"
    r"bump|version\s+string|changelog|"
    r"boilerplate|scaffold|"
    r"add\s+logging|log\s+line|"
    r"config\s+tweak|whitespace|import\s+order"
    r")\b",
    re.IGNORECASE,
)

# Anything architectural overrides a mechanical-looking word elsewhere in the
# same sentence: "rename the module and split the interface" is not mechanical.
_SUBSTANTIVE = re.compile(
    r"\b("
    r"design|architect|architecture|refactor|redesign|"
    r"migrate|migration|schema|endpoint|api|contract|"
    r"implement|feature|integrate|integration|"
    r"security|auth|concurrency|performance|"
    r"multi-file|across\s+files|new\s+module"
    r")\b",
    re.IGNORECASE,
)


def base_tier(description: str) -> tuple[str, str]:
    """Classify a description, returning the tier and the rule that fired.

    An unmatched description reports `unmatched` rather than `substantive`:
    quality-first means defaulting to the strong model, but the audit trail
    should say the rules had nothing to say, not claim a judgement they never
    made. Counting `unmatched` across real jobs is how the table gets tuned.
    """
    if _SUBSTANTIVE.search(description):
        return TIER_DEFAULT, "substantive"
    if _MECHANICAL.search(description):
        return TIER_FAST, "mechanical"
    return TIER_DEFAULT, "unmatched"


def escalate(tier: str, steps: int = 1) -> str:
    """Move `steps` up the ladder, stopping at the top rung."""
    try:
        index = TIER_LADDER.index(tier)
    except ValueError:
        return TIER_DEFAULT
    return TIER_LADDER[min(index + steps, len(TIER_LADDER) - 1)]


def choose_tier(description: str, attempts: int = 1) -> tuple[str, str]:
    """Pick the tier for this attempt and explain why, for the history note.

    The reason string lands in the job's `planning -> building` transition, so
    the routing decision is legible in the §4 audit trail rather than implicit
    in which model happened to get called.
    """
    chosen, reason = base_tier(description)
    if attempts > 1:
        chosen = escalate(chosen, attempts - 1)
        reason = f"{reason}, escalated on attempt {attempts}"
    return chosen, f"tier={chosen} rule={reason}"
