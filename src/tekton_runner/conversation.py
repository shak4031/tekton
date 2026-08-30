"""Drive one agent conversation against one sandbox (D3.4 slice 5).

The agent sees a tier alias, never a vendor or a model name — routing lives in
LiteLLM on the Pi (ADR-2). Browser tools are off by default (D3.3): the agent
has headless Chromium plus public-internet egress, which together are the
widest prompt-injection-to-exfiltration path in the system. Opt in per job so
any exception is visible in the job record.

Config is separated from execution for the same reason as `workspace.py`: an
`LLM` or an `Agent` can be built and asserted on without Docker, so the
guarantees are testable. Only `run_task` needs a daemon.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhands.sdk import LLM, Agent, Conversation
from openhands.tools.preset.default import get_default_agent

from tekton_runner.classify import TIER_DEFAULT
from tekton_runner.workspace import (
    CONTAINER_PROJECT,
    CONTAINER_WORKDIR,
    SandboxSpec,
    open_sandbox,
)

# D3.2b standing rule: address the Pi by IP, never by name. Router DNS has
# pointed `hermes` at a dead address before.
ROUTER_URL = "http://192.168.1.201:4000"

# How much of an event to keep in the trail. Enough to see which file the
# agent touched and what it ran; not so much that a log becomes a transcript.
EVENT_SUMMARY_CHARS = 400

# Field names read off the installed SDK event classes at slice 5, not guessed:
# ActionEvent has tool_name/action, ObservationEvent has tool_name/observation,
# MessageEvent has llm_message (a Message whose .content is a list of
# TextContent). Anything else degrades to None rather than failing a job.

# The SDK's LiteLLM-proxy prefix; everything after it is a tier alias.
MODEL_PREFIX = "litellm_proxy/"

# Conversation defaults to 500 iterations. That is a runaway budget for an
# unattended runner billing tokens; ADR-2's loop-count backstop wants a number.
MAX_ITERATIONS = 60

# repomix writes here (D3.3); the agent reads it as an ordinary file.
CONTEXT_IN_CONTAINER = f"{CONTAINER_PROJECT}/.tekton/context.md"

# ADR-5 wanted the event-sourced conversation as a §4 audit trail. The SDK
# refuses `persistence_dir` on a RemoteConversation, and a DockerWorkspace is
# remote — its state would live inside a container that is deleted at task end.
# So the runner keeps the trail itself, on the host, where it survives.
JOB_LOG_ROOT = Path.home() / ".tekton" / "conversations"

_BROWSER_TOOL_MARKER = "browser"


class ConversationError(RuntimeError):
    """The agent could not be built or the conversation could not run."""


@dataclass(frozen=True)
class TaskOutcome:
    """What one conversation cost and whether it ran to completion.

    Deliberately not a verdict on the *work*: judging that is Ibn al-Haytham's
    job (tests) and al-Ghazali's (review), in D4. This says only that the agent
    ran and stopped.
    """

    completed: bool
    events: int
    tier: str
    log_path: Path
    error: str | None = None


def build_llm(tier: str = TIER_DEFAULT, api_key: str | None = None) -> LLM:
    """Build an LLM pointed at the router, using a tier alias only."""
    key = api_key or os.environ.get("LITELLM_API_KEY")
    if not key:
        raise ConversationError("LITELLM_API_KEY is not set")
    return LLM(model=f"{MODEL_PREFIX}{tier}", base_url=ROUTER_URL, api_key=key)


def build_agent(llm: LLM, enable_browser: bool = False) -> Agent:
    """Build the agent via the preset proven at D3.1, browser off by default.

    Deliberately `get_default_agent` rather than constructing `Agent` directly.
    The preset also sets `system_prompt_kwargs={"cli_mode": ...}` and attaches
    a condenser; a bare Agent leaves `cli_mode` unset in the prompt template,
    and the first slice 5 smoke run showed the result — the agent wrote a task
    plan naming the right file and then stopped without executing anything.

    `cli_mode=True` is what disables browsing upstream, so the two are one
    switch; the assertion below checks the outcome rather than trusting it.
    """
    agent = get_default_agent(llm=llm, cli_mode=not enable_browser)
    if not enable_browser:
        assert_no_browser(agent.tools)
    return agent


def assert_no_browser(tools: list[Any]) -> None:
    """Verify the browser tool is absent rather than trusting the flag.

    D3.3 turned browsing off by a parameter. This checks the result, because a
    default that changes upstream would otherwise reopen the widest
    exfiltration path in the system without anyone noticing.
    """
    offenders = [t.name for t in tools if _BROWSER_TOOL_MARKER in t.name.lower()]
    if offenders:
        raise ConversationError(f"browser tools present with browsing disabled: {offenders}")


def task_prompt(description: str) -> str:
    """Wrap the job description with the context pointer and the boundaries.

    The cwd warning is load-bearing. The agent-server starts its shell and its
    file editor in {CONTAINER_WORKDIR}, one level ABOVE the mount, and keeps its
    own bookkeeping there. A file written to a relative path lands in the
    container and is destroyed with it — which is exactly how the first smoke
    run reported success while producing nothing on the host.
    """
    return (
        f"A project is mounted at {CONTAINER_PROJECT}. That directory, and only\n"
        f"that directory, persists after this session ends.\n\n"
        f"Your shell and editor start in {CONTAINER_WORKDIR}, which is NOT the\n"
        f"project. Always write and read using absolute paths beginning\n"
        f"{CONTAINER_PROJECT}/. Anything written elsewhere is discarded.\n\n"
        f"A packed overview of the codebase is at {CONTEXT_IN_CONTAINER} — read it first.\n"
        "Do not run git; the runner handles version control outside this sandbox.\n\n"
        f"Task: {description}\n"
    )


def _join(parts: Any) -> str | None:
    """Join non-empty text fragments, returning None if nothing survives."""
    joined = " ".join(part for part in parts if part)
    return joined or None


def _text_of_object(value: Any) -> str | None:
    """Read `.text` or `.content` off an SDK content object."""
    for attr in ("text", "content"):
        inner = getattr(value, attr, None)
        if inner is not None and inner is not value:
            return text_of(inner)
    return None


def text_of(value: Any) -> str | None:
    """Pull plain text out of a Message, a TextContent, or a list of them."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        return _join(text_of(item) for item in value)
    return _text_of_object(value)


def summarize_event(event: Any) -> str | None:
    """Extract a short, human-readable gist of one SDK event.

    Defensive by design: the SDK's event classes are not part of our contract,
    so a shape this does not recognise degrades to None rather than failing a
    job over a log line.
    """
    tool = getattr(event, "tool_name", None)
    action = getattr(event, "action", None)
    if action is not None:
        return f"{tool or 'action'} {action!r}"[:EVENT_SUMMARY_CHARS]
    observation = getattr(event, "observation", None)
    if observation is not None:
        body = text_of(observation) or repr(observation)
        return f"{tool or 'observation'} {body}"[:EVENT_SUMMARY_CHARS]
    for attr in ("llm_message", "system_prompt"):
        body = text_of(getattr(event, attr, None))
        if body:
            return body[:EVENT_SUMMARY_CHARS]
    return None


class EventLog:
    """A host-side JSONL record of one conversation.

    Streamed rather than written at the end, so a job that dies mid-flight
    still leaves the trail that explains why.
    """

    def __init__(self, job_id: str, root: Path = JOB_LOG_ROOT) -> None:
        """Open the log for `job_id` under `root`, creating the directory."""
        self.job_id = job_id
        self.path = root / job_id / "events.jsonl"
        self.count = 0
        self._handle: Any = None

    def __enter__(self) -> EventLog:
        """Open the file handle."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the file handle."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def record(self, event: Any) -> None:
        """Append one event. Used as an SDK conversation callback."""
        self.count += 1
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cid": self.job_id,
            "seq": self.count,
            "event": type(event).__name__,
            "summary": summarize_event(event),
        }
        if self._handle is not None:
            self._handle.write(json.dumps(line) + "\n")
            self._handle.flush()


def run_task(
    spec: SandboxSpec,
    description: str,
    job_id: str,
    tier: str = TIER_DEFAULT,
    enable_browser: bool = False,
) -> TaskOutcome:
    """Run one task to completion in an ephemeral sandbox.

    The workspace is a context manager: the container is created for this task
    and removed on exit, which is the ADR-3 per-task sandbox shape observed
    live at D3.1.
    """
    agent = build_agent(build_llm(tier), enable_browser=enable_browser)
    with EventLog(job_id) as log:
        try:
            with open_sandbox(spec) as workspace:
                convo = Conversation(
                    agent,
                    workspace=workspace,
                    max_iteration_per_run=MAX_ITERATIONS,
                    visualizer=None,
                    callbacks=[log.record],
                )
                convo.send_message(task_prompt(description))
                convo.run()
        except Exception as exc:  # noqa: BLE001 - one job's failure is not the runner's
            return TaskOutcome(False, log.count, tier, log.path, str(exc))
        return TaskOutcome(True, log.count, tier, log.path)
