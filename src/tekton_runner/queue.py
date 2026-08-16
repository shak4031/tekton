"""HTTP client for the Hermes job queue contract (D3.4 slice 1).

Transport only. This module moves jobs and state across the wire and owns no
SDLC policy: which state a job moves to next, and which model tier it gets,
are decided by the caller. Keeping policy out of here is what lets slices 5-6
change the SDLC without touching the wire format.

Design rule inherited from slice 1: Hermes is the ONLY writer of its own
database. The runner never opens the SQLite file; it knocks on HTTP doors.

Stdlib only, deliberately. Four POSTs at a few requests per minute do not
justify a third-party HTTP dependency -- every adopted package is permanent
surface area (ADR-9 anti-accretion clause): a pin, a monthly re-vet, and
supply-chain exposure. Reversible in one commit if async or pooling is ever
needed.

Address Hermes by IP, not by name: router DNS may still resolve `hermes` to
the dead .198 address retired at D3.2b.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

DEFAULT_HERMES_URL = "http://192.168.1.201:8787"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_HEARTBEAT_SECONDS = 60.0

OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
VALID_OUTCOMES = frozenset({OUTCOME_DONE, OUTCOME_FAILED})

_HTTP_NO_CONTENT = 204
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409


class QueueError(RuntimeError):
    """Base class for every failure of the queue client."""


class AuthError(QueueError):
    """Hermes rejected the runner secret (401)."""


class JobNotFoundError(QueueError):
    """Hermes has no job with that id (404)."""


class IllegalTransitionError(QueueError):
    """Hermes refused the state transition as illegal (409)."""


class QueueUnavailableError(QueueError):
    """Hermes was unreachable, or answered with an unexpected status."""


@dataclass(frozen=True)
class Job:
    """A single job as returned by ``POST /jobs/claim``."""

    id: str
    project: str
    description: str
    state: str
    attempts: int
    worker: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Job:
        """Build a Job from Hermes JSON, tolerating unknown extra fields.

        ``raw`` keeps the whole payload so a later slice can read a field this
        dataclass does not know about yet without a wire-format change.
        """
        if not isinstance(payload, Mapping):
            raise QueueUnavailableError(f"claim payload is {type(payload).__name__}, not an object")
        try:
            job_id = str(payload["id"])
        except KeyError as exc:
            raise QueueUnavailableError("claim payload has no job id") from exc
        return cls(
            id=job_id,
            project=str(payload.get("project", "")),
            description=str(payload.get("description", "")),
            state=str(payload.get("state", "")),
            attempts=int(payload.get("attempts", 0)),
            worker=payload.get("worker"),
            raw=dict(payload),
        )


def _decode(raw: bytes) -> Any:
    """Decode a JSON response body, returning None for an empty body."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QueueUnavailableError(f"malformed JSON from Hermes: {exc}") from exc


def _error_for(status: int, url: str) -> QueueError:
    """Map an HTTP error status onto the matching QueueError subclass."""
    if status == _HTTP_UNAUTHORIZED:
        return AuthError(f"runner secret rejected by {url}")
    if status == _HTTP_NOT_FOUND:
        return JobNotFoundError(f"no such job at {url}")
    if status == _HTTP_CONFLICT:
        return IllegalTransitionError(f"transition refused by {url}")
    return QueueUnavailableError(f"POST {url} returned {status}")


@dataclass
class QueueClient:
    """Client for the four Hermes queue endpoints.

    One instance per runner process; ``worker`` is stamped on every request so
    Hermes can attribute claims, heartbeats and results to this box.
    """

    base_url: str
    secret: str
    worker: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> QueueClient:
        """Build a client from HERMES_URL, RUNNER_SECRET and TEKTON_WORKER."""
        env = os.environ if env is None else env
        secret = env.get("RUNNER_SECRET", "")
        if not secret:
            raise QueueError("RUNNER_SECRET is not set")
        return cls(
            base_url=env.get("HERMES_URL", DEFAULT_HERMES_URL),
            secret=secret,
            worker=env.get("TEKTON_WORKER") or socket.gethostname(),
        )

    def _post(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        """POST JSON to Hermes and return the status and decoded body."""
        url = f"{self.base_url.rstrip('/')}{path}"
        request = urllib.request.Request(  # noqa: S310 - fixed http scheme, LAN
            url,
            data=json.dumps(dict(payload)).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Hermes-Secret", self.secret)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as exc:
            raise _error_for(exc.code, url) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise QueueUnavailableError(f"POST {url} failed: {exc}") from exc

    def claim(self) -> Job | None:
        """Claim the oldest queued job, or return None if the queue is empty."""
        status, payload = self._post("/jobs/claim", {"worker": self.worker})
        if status == _HTTP_NO_CONTENT or payload is None:
            return None
        return Job.from_payload(payload)

    def set_state(self, job_id: str, state: str, note: str = "") -> None:
        """Move a job to ``state``, appending ``note`` to its history."""
        self._post(
            f"/jobs/{job_id}/state",
            {"state": state, "note": note, "worker": self.worker},
        )

    def report_result(self, job_id: str, outcome: str, summary: str = "") -> None:
        """Report the terminal outcome of this attempt at ``job_id``.

        Hermes compares this field against "done" and routes everything else
        to `failed`, so an unrecognised value is rejected here rather than
        silently failing the job on the Pi.
        """
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(VALID_OUTCOMES)}")
        self._post(
            f"/jobs/{job_id}/result",
            {"outcome": outcome, "summary": summary, "worker": self.worker},
        )

    def heartbeat(self, job_id: str) -> None:
        """Tell Hermes this worker is still alive on ``job_id``."""
        self._post(f"/jobs/{job_id}/heartbeat", {"worker": self.worker})


@contextmanager
def heartbeating(
    client: QueueClient,
    job_id: str,
    seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> Iterator[threading.Event]:
    """Beat for ``job_id`` on a daemon thread for the life of the block.

    Required, not optional: a real build outruns Hermes' JOB_STALE_SECONDS
    (900) and would be reaped mid-flight. Transient reachability failures are
    swallowed -- reap_stale is the real backstop -- but a 401/404/409 stops the
    thread, because those will not fix themselves.
    """
    stop = threading.Event()

    def loop() -> None:
        while True:
            try:
                client.heartbeat(job_id)
            except QueueUnavailableError:
                pass
            except QueueError:
                return
            if stop.wait(seconds):
                return

    thread = threading.Thread(target=loop, name=f"heartbeat-{job_id}", daemon=True)
    thread.start()
    try:
        yield stop
    finally:
        stop.set()
        thread.join(timeout=seconds)
