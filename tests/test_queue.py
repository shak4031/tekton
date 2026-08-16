"""Tests for ``tekton_runner.queue`` against a stub Hermes.

Covers the happy path for all four endpoints and, more importantly, every
failure path the runner will actually hit in the field: empty queue, wrong
secret, illegal transition, unknown job, unreachable Pi, malformed body.
"""

from __future__ import annotations

import time

import pytest
from stub_hermes import StubHermes

from tekton_runner.queue import (
    OUTCOME_FAILED,
    AuthError,
    IllegalTransitionError,
    Job,
    JobNotFoundError,
    QueueClient,
    QueueError,
    QueueUnavailableError,
    heartbeating,
)

JOB_ID = "lifepilot-a1b2"
MIN_BEATS = 2


def test_claim_returns_a_job(client: QueueClient) -> None:
    """A 200 claim is parsed into a Job with the fields the runner needs."""
    job = client.claim()
    assert job is not None
    assert job.id == JOB_ID
    assert job.project == "lifepilot"
    assert job.attempts == 1
    assert job.state == "planning"


def test_claim_sends_secret_and_worker(client: QueueClient, hermes: StubHermes) -> None:
    """Every request carries the shared secret and identifies this worker."""
    client.claim()
    sent = hermes.last()
    assert sent["headers"]["X-Hermes-Secret"] == client.secret
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["body"] == {"worker": "tekton-rig"}


def test_empty_queue_returns_none(client: QueueClient, hermes: StubHermes) -> None:
    """204 means no work, not an error -- the runner must be able to idle."""
    hermes.script("/jobs/claim", 204, None)
    assert client.claim() is None


def test_wrong_secret_raises_auth_error(hermes: StubHermes) -> None:
    """A rejected secret is distinguishable from every other failure."""
    client = QueueClient(hermes.url, "wrong-secret", "tekton-rig", timeout=5.0)
    with pytest.raises(AuthError):
        client.claim()


def test_illegal_transition_raises(client: QueueClient, hermes: StubHermes) -> None:
    """409 from the slice 1 TRANSITIONS map surfaces as IllegalTransitionError."""
    hermes.script(f"/jobs/{JOB_ID}/state", 409, {"error": "illegal"})
    with pytest.raises(IllegalTransitionError):
        client.set_state(JOB_ID, "done", note="skipping the whole SDLC")


def test_unknown_job_raises_not_found(client: QueueClient, hermes: StubHermes) -> None:
    """404 for an unknown job id maps to JobNotFoundError."""
    hermes.script(f"/jobs/{JOB_ID}/state", 404, {"error": "no job"})
    with pytest.raises(JobNotFoundError):
        client.set_state(JOB_ID, "building")


def test_unexpected_status_raises_unavailable(
    client: QueueClient, hermes: StubHermes
) -> None:
    """A 500 is not silently treated as success."""
    hermes.script(f"/jobs/{JOB_ID}/heartbeat", 500, {"error": "boom"})
    with pytest.raises(QueueUnavailableError):
        client.heartbeat(JOB_ID)


def test_unreachable_hermes_raises_unavailable() -> None:
    """A Pi that is off must fail loudly and quickly, not hang."""
    client = QueueClient("http://127.0.0.1:9", "secret", "tekton-rig", timeout=2.0)
    with pytest.raises(QueueUnavailableError):
        client.claim()


def test_malformed_body_raises_unavailable(
    client: QueueClient, hermes: StubHermes
) -> None:
    """Truncated or non-JSON output is a transport failure, not a Job."""
    hermes.script("/jobs/claim", 200, b'{"id": "lifepi')
    with pytest.raises(QueueUnavailableError):
        client.claim()


def test_non_object_body_raises_unavailable(
    client: QueueClient, hermes: StubHermes
) -> None:
    """Valid JSON of the wrong shape must not reach Job.from_payload."""
    hermes.script("/jobs/claim", 200, "not-json")
    with pytest.raises(QueueUnavailableError):
        client.claim()


def test_claim_payload_without_id_raises(
    client: QueueClient, hermes: StubHermes
) -> None:
    """A job with no id is unusable; fail rather than build an empty Job."""
    hermes.script("/jobs/claim", 200, {"project": "lifepilot"})
    with pytest.raises(QueueUnavailableError):
        client.claim()


def test_set_state_sends_note(client: QueueClient, hermes: StubHermes) -> None:
    """The history note reaches Hermes -- this is the audit trail (Charter 4)."""
    client.set_state(JOB_ID, "building", note="tier=tekton-fast rule=single-file")
    sent = hermes.last()
    assert sent["path"] == f"/jobs/{JOB_ID}/state"
    assert sent["body"]["state"] == "building"
    assert "tekton-fast" in sent["body"]["note"]


def test_report_result_sends_outcome(client: QueueClient, hermes: StubHermes) -> None:
    """A terminal result uses Hermes' own vocabulary, not a boolean."""
    client.report_result(JOB_ID, OUTCOME_FAILED, summary="gates failed: PLR0913")
    body = hermes.last()["body"]
    assert body["outcome"] == "failed"
    assert body["summary"] == "gates failed: PLR0913"


def test_report_result_rejects_unknown_outcome(client: QueueClient) -> None:
    """An unrecognised outcome must not reach Hermes, which would fail the job."""
    with pytest.raises(ValueError, match="outcome must be"):
        client.report_result(JOB_ID, "Done")


def test_job_keeps_unknown_fields(client: QueueClient, hermes: StubHermes) -> None:
    """Extra fields from a future Hermes version survive in ``raw``."""
    hermes.script("/jobs/claim", 200, {"id": JOB_ID, "priority": "high"})
    job = client.claim()
    assert job is not None
    assert job.raw["priority"] == "high"


def test_job_from_payload_defaults() -> None:
    """Missing optional fields default instead of raising."""
    job = Job.from_payload({"id": "x-0001"})
    assert job.attempts == 0
    assert job.worker is None


def test_from_env_requires_secret() -> None:
    """A runner with no RUNNER_SECRET must refuse to start."""
    with pytest.raises(QueueError):
        QueueClient.from_env({})


def test_from_env_reads_config() -> None:
    """Env config wins over defaults; worker falls back to the hostname."""
    built = QueueClient.from_env(
        {
            "RUNNER_SECRET": "s3cret",
            "HERMES_URL": "http://192.168.1.201:8787",
            "TEKTON_WORKER": "tekton-rig",
        }
    )
    assert built.base_url == "http://192.168.1.201:8787"
    assert built.worker == "tekton-rig"


def test_heartbeating_beats_and_stops(client: QueueClient, hermes: StubHermes) -> None:
    """The context manager beats immediately and stops on exit."""
    with heartbeating(client, JOB_ID, seconds=0.05):
        time.sleep(0.3)
    beats = [r for r in hermes.requests if r["path"].endswith("/heartbeat")]
    assert len(beats) >= MIN_BEATS
    after_exit = len(beats)
    time.sleep(0.2)
    still = [r for r in hermes.requests if r["path"].endswith("/heartbeat")]
    assert len(still) == after_exit


def test_heartbeating_stops_on_auth_error(
    client: QueueClient, hermes: StubHermes
) -> None:
    """A 401 ends the beat thread; it will not fix itself by retrying."""
    hermes.script(f"/jobs/{JOB_ID}/heartbeat", 401, {"error": "bad secret"})
    with heartbeating(client, JOB_ID, seconds=0.05):
        time.sleep(0.3)
    beats = [r for r in hermes.requests if r["path"].endswith("/heartbeat")]
    assert len(beats) == 1
