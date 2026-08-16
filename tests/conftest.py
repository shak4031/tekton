"""Shared fixtures for the queue-client tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from stub_hermes import DEFAULT_SECRET, StubHermes

from tekton_runner.queue import QueueClient


@pytest.fixture
def hermes() -> Iterator[StubHermes]:
    """A stub Hermes on an ephemeral port, torn down after the test."""
    stub = StubHermes()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def client(hermes: StubHermes) -> QueueClient:
    """A queue client pointed at the stub, with the matching secret."""
    return QueueClient(
        base_url=hermes.url,
        secret=DEFAULT_SECRET,
        worker="tekton-rig",
        timeout=5.0,
    )
