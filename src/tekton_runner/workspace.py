"""The ONE place the image pin, the sandbox network, and the mount live.

Every sandbox guarantee the platform has is either enforced here or not at all:

- **The pin.** `DockerWorkspace.server_image` defaults to `latest-python` in the
  SDK's own field metadata (D3.2 finding, re-read from the installed signature
  at slice 5). Passing it explicitly on every construction is the only thing
  standing between us and whatever shipped upstream that week.
- **The network.** `network=None` puts the workspace on Docker's default bridge,
  which NATs to the whole LAN. `tekton-net` is what makes the sandbox
  *distinguishable* to the DOCKER-USER rules — the precondition ADR-6 identifies.
- **The mount.** Exactly one project checkout, per ADR-3. Not two, not the home
  directory, not the runner's own source tree.

Config is separated from construction on purpose: building a `DockerWorkspace`
shells out to Docker immediately, so a spec that cannot be inspected without a
running daemon is a spec nobody tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhands.workspace import DockerWorkspace

# Registry-resolved at D3.1 by `docker manifest inspect`, never a guessed tag.
AGENT_IMAGE = "ghcr.io/openhands/agent-server:1.36.1-python"

# Created by `docker network create --subnet 172.28.0.0/24 tekton-net` (D3.2b).
SANDBOX_NETWORK = "tekton-net"

# The agent's cwd inside the container; the checkout hangs off it.
CONTAINER_WORKDIR = "/workspace"
CONTAINER_PROJECT = f"{CONTAINER_WORKDIR}/project"

# Writable by design: D3.2's T6 ACL fix exists so the agent can edit the mount.
MOUNT_MODE = "rw"

# The workspace container is torn down per task; a slow pull should not look
# like a hang, but it must not wait forever either.
HEALTH_CHECK_TIMEOUT_S = 300.0


class SandboxError(RuntimeError):
    """The sandbox could not be specified or started safely."""


@dataclass(frozen=True)
class SandboxSpec:
    """A fully-resolved sandbox configuration, inspectable without Docker."""

    checkout: Path
    image: str = AGENT_IMAGE
    network: str = SANDBOX_NETWORK

    @property
    def volumes(self) -> list[str]:
        """The single bind mount, in Docker's host:container:mode form."""
        return [f"{self.checkout}:{CONTAINER_PROJECT}:{MOUNT_MODE}"]

    def as_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for `DockerWorkspace(**spec.as_kwargs())`."""
        return {
            "server_image": self.image,
            "network": self.network,
            "volumes": self.volumes,
            "working_dir": CONTAINER_WORKDIR,
            "health_check_timeout": HEALTH_CHECK_TIMEOUT_S,
        }


def sandbox_spec(checkout: Path) -> SandboxSpec:
    """Build and validate the spec for one task's sandbox.

    Validation is here rather than at the call site because this is the only
    module that knows what a safe sandbox looks like.
    """
    resolved = Path(checkout).expanduser().resolve()
    if not resolved.is_dir():
        raise SandboxError(f"checkout {resolved} is not a directory")
    if not (resolved / ".git").is_dir():
        raise SandboxError(f"{resolved} is not a git checkout; refusing to mount it")
    spec = SandboxSpec(checkout=resolved)
    assert_safe(spec)
    return spec


def assert_safe(spec: SandboxSpec) -> None:
    """Refuse a spec that would weaken a wall proven in D3.2 or D3.2b."""
    if ":latest" in spec.image or spec.image.endswith("latest-python"):
        raise SandboxError(f"refusing an unpinned image: {spec.image}")
    if "@" not in spec.image and ":" not in spec.image:
        raise SandboxError(f"image has no tag or digest: {spec.image}")
    if not spec.network:
        raise SandboxError("no network set — the default bridge reaches the whole LAN")
    if len(spec.volumes) != 1:
        raise SandboxError(f"ADR-3 allows exactly one mount, got {len(spec.volumes)}")


def open_sandbox(spec: SandboxSpec) -> DockerWorkspace:
    """Start the sandbox described by `spec`, as a context manager.

    Constructing a DockerWorkspace shells out to `docker` immediately, which is
    why the spec above is a separate, inspectable object: every guarantee can
    be asserted on a host with no daemon, and only this line needs one.
    """
    assert_safe(spec)
    return DockerWorkspace(**spec.as_kwargs())
