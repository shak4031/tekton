"""Tests for slice 5: sandbox spec, tier classification, agent construction.

Every guarantee that can be asserted without a Docker daemon is asserted here
— the pin, the network, the single mount, the absent browser tool. Starting a
container is left to the smoke run, because a test that needs a daemon is a
test that stops being run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tekton_runner.classify import TIER_FAST
from tekton_runner.conversation import (
    CONTEXT_IN_CONTAINER,
    MAX_ITERATIONS,
    MODEL_PREFIX,
    ROUTER_URL,
    ConversationError,
    EventLog,
    assert_no_browser,
    build_agent,
    build_llm,
    summarize_event,
    task_prompt,
    text_of,
)
from tekton_runner.workspace import (
    AGENT_IMAGE,
    CONTAINER_PROJECT,
    SANDBOX_NETWORK,
    SandboxError,
    SandboxSpec,
    assert_safe,
    sandbox_spec,
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A directory that looks like a git checkout."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S603,S607
    return root


class _Tool:
    """Stand-in for an SDK tool spec, which only needs a name here."""

    def __init__(self, name: str) -> None:
        self.name = name


# --------------------------------------------------------------- sandbox spec


def test_spec_mounts_exactly_one_checkout(checkout: Path) -> None:
    """ADR-3: one repo, one sandbox, one task."""
    spec = sandbox_spec(checkout)
    assert spec.volumes == [f"{checkout.resolve()}:{CONTAINER_PROJECT}:rw"]


def test_spec_carries_the_pin_and_the_network(checkout: Path) -> None:
    """The two defaults that would silently weaken the sandbox if omitted."""
    kwargs = sandbox_spec(checkout).as_kwargs()
    assert kwargs["server_image"] == AGENT_IMAGE
    assert kwargs["network"] == SANDBOX_NETWORK


def test_spec_refuses_a_non_directory(tmp_path: Path) -> None:
    """A path that is not there fails before Docker is ever contacted."""
    with pytest.raises(SandboxError, match="not a directory"):
        sandbox_spec(tmp_path / "nope")


def test_spec_refuses_a_directory_that_is_not_a_checkout(tmp_path: Path) -> None:
    """Mounting an arbitrary directory would widen ADR-3's boundary."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SandboxError, match="not a git checkout"):
        sandbox_spec(plain)


def test_assert_safe_rejects_latest_python(checkout: Path) -> None:
    """The SDK's own default is latest-python; drifting to it is the failure."""
    spec = SandboxSpec(
        checkout=checkout, image="ghcr.io/openhands/agent-server:latest-python"
    )
    with pytest.raises(SandboxError, match="unpinned"):
        assert_safe(spec)


def test_assert_safe_rejects_a_bare_latest_tag(checkout: Path) -> None:
    """Any :latest tag, not just the SDK's specific default."""
    with pytest.raises(SandboxError, match="unpinned"):
        assert_safe(SandboxSpec(checkout=checkout, image="some/image:latest"))


def test_assert_safe_rejects_an_empty_network(checkout: Path) -> None:
    """No network means the default bridge, which reaches the whole LAN."""
    with pytest.raises(SandboxError, match="default bridge"):
        assert_safe(SandboxSpec(checkout=checkout, network=""))


def test_pinned_image_has_no_v_prefix() -> None:
    """D3.1 resolved the tag by registry probe: org openhands, no `v`."""
    assert AGENT_IMAGE == "ghcr.io/openhands/agent-server:1.36.1-python"


# --------------------------------------------------------------- conversation


def test_llm_uses_a_tier_alias_and_the_router() -> None:
    """The agent sees a role, never a vendor or a model name (ADR-2)."""
    llm = build_llm(TIER_FAST, api_key="dummy")
    assert llm.model == f"{MODEL_PREFIX}{TIER_FAST}"
    assert llm.base_url == ROUTER_URL


def test_llm_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key is a clear failure, not a silent unauthenticated request."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(ConversationError, match="LITELLM_API_KEY"):
        build_llm(TIER_FAST)


def test_agent_has_no_browser_tool() -> None:
    """D3.3: browsing is the widest exfiltration path; off by default."""
    agent = build_agent(build_llm(TIER_FAST, api_key="dummy"))
    names = [t.name for t in agent.tools]
    assert names == ["terminal", "file_editor", "task_tracker"]


def test_agent_carries_the_cli_mode_prompt_kwarg() -> None:
    """A bare Agent leaves cli_mode unset and the agent plans without acting."""
    agent = build_agent(build_llm(TIER_FAST, api_key="dummy"))
    assert agent.system_prompt_kwargs.get("cli_mode") is True


def test_agent_gets_a_condenser() -> None:
    """The preset attaches one; constructing Agent directly silently drops it."""
    agent = build_agent(build_llm(TIER_FAST, api_key="dummy"))
    assert agent.condenser is not None


def test_assert_no_browser_catches_an_upstream_default_change() -> None:
    """Checks the result, not the flag — a default could change upstream."""
    with pytest.raises(ConversationError, match="browser tools present"):
        assert_no_browser([_Tool("terminal"), _Tool("browser_tool_set")])


def test_prompt_points_at_the_context_pack_and_forbids_git() -> None:
    """ADR-10 property 4: the agent never runs git."""
    prompt = task_prompt("add a health endpoint")
    assert CONTEXT_IN_CONTAINER in prompt
    assert "Do not run git" in prompt
    assert "add a health endpoint" in prompt


def test_prompt_warns_that_the_shell_starts_outside_the_mount() -> None:
    """The first smoke run wrote to the container and reported success."""
    prompt = task_prompt("anything")
    assert "is NOT the" in prompt
    assert "absolute paths" in prompt
    assert "discarded" in prompt


def test_event_summary_names_the_tool_and_the_action() -> None:
    """An ActionEvent must show which tool ran and with what."""

    class _Ev:
        tool_name = "file_editor"
        action = "create /workspace/project/SLICE5.md"

    assert "file_editor" in str(summarize_event(_Ev()))


def test_event_summary_reads_a_message_through_nested_content() -> None:
    """MessageEvent wraps text two levels deep; the first run logged null."""

    class _Text:
        text = "I have created the file."

    class _Message:
        content = [_Text()]

    class _Ev:
        llm_message = _Message()

    assert summarize_event(_Ev()) == "I have created the file."


def test_text_of_handles_a_plain_string() -> None:
    """The simplest shape still works."""
    assert text_of("  hello  ") == "hello"


def test_event_summary_gives_up_quietly() -> None:
    """A log line must never be the reason a job fails."""
    assert summarize_event(object()) is None


SDK_DEFAULT_ITERATIONS = 500
TWO_EVENTS = 2


def test_iteration_budget_is_far_below_the_sdk_default() -> None:
    """500 iterations is a runaway token budget for an unattended runner."""
    assert MAX_ITERATIONS < SDK_DEFAULT_ITERATIONS // 5


# ------------------------------------------------------------------ event log


def test_event_log_streams_one_line_per_event(tmp_path: Path) -> None:
    """Streamed, not buffered: a job that dies still leaves its trail."""
    with EventLog("lifepilot-a8b9", root=tmp_path) as log:
        log.record(_Tool("MessageEvent"))
        log.record(_Tool("ActionEvent"))
        assert log.path.read_text().count("\n") == TWO_EVENTS
    assert log.count == TWO_EVENTS


def test_event_log_records_the_correlation_id(tmp_path: Path) -> None:
    """Charter §4: the job id prefixes every line."""
    with EventLog("sandbox-test-786a", root=tmp_path) as log:
        log.record(_Tool("x"))
    first = json.loads(log.path.read_text().splitlines()[0])
    assert first["cid"] == "sandbox-test-786a"
    assert first["seq"] == 1
