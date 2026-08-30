"""Tests for ``tekton_runner.classify`` — the ADR-2 tier decision.

Split out of test_slice5 when that file hit the ADR-7 300-line cap. The budget
governing the code that will later enforce it is the point.
"""

from __future__ import annotations

import pytest

from tekton_runner.classify import (
    TIER_CLOUD,
    TIER_DEFAULT,
    TIER_FAST,
    base_tier,
    choose_tier,
    escalate,
)


@pytest.mark.parametrize(
    "description",
    [
        "rename the store module",
        "fix a typo in the README",
        "add docstrings to the public functions",
        "bump the version string",
    ],
)
def test_mechanical_work_takes_the_fast_lane(description: str) -> None:
    """ADR-2's own examples of fast-lane work."""
    assert base_tier(description) == (TIER_FAST, "mechanical")


@pytest.mark.parametrize(
    "description",
    [
        "add an email digest endpoint",
        "refactor the queue client for concurrency",
        "design the lending loop schema",
    ],
)
def test_substantive_work_gets_the_default_tier(description: str) -> None:
    """Quality-first: anything architectural goes to the strong model."""
    assert base_tier(description) == (TIER_DEFAULT, "substantive")


def test_unmatched_work_defaults_without_claiming_a_judgement() -> None:
    """The note must say the rules had nothing to say, not invent a verdict."""
    tier, reason = base_tier("something unlike anything in the rules table")
    assert tier == TIER_DEFAULT
    assert reason == "unmatched"


def test_substantive_words_override_mechanical_ones() -> None:
    """A rename that is really a redesign is not fast-lane work."""
    assert base_tier("rename the module and refactor its interface")[0] == TIER_DEFAULT


def test_retry_escalates_the_tier() -> None:
    """The reason the runner classifies at all: attempts is readable here."""
    first, _ = choose_tier("fix a typo", attempts=1)
    second, note = choose_tier("fix a typo", attempts=2)
    assert first == TIER_FAST
    assert second == TIER_DEFAULT
    assert "escalated on attempt 2" in note


def test_escalation_stops_at_the_top_rung() -> None:
    """A job on its last life cannot escalate past cloud."""
    assert escalate(TIER_CLOUD) == TIER_CLOUD
    assert escalate(TIER_DEFAULT, steps=5) == TIER_CLOUD


def test_unknown_tier_falls_back_to_default() -> None:
    """A tier that is not on the ladder must not silently become fast."""
    assert escalate("tekton-nonsense") == TIER_DEFAULT


def test_tier_note_is_written_for_the_audit_trail() -> None:
    """The note lands in the planning -> building transition (Charter §4)."""
    _, note = choose_tier("add docstrings", attempts=1)
    assert note == "tier=tekton-fast rule=mechanical"
