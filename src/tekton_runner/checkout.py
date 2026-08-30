"""Prepare a project checkout on the rig host, ready to be mounted (ADR-10).

Resolve a project name against the registry, clone it if absent or refresh it
to match origin if present, then apply the ADR-3 ACL pair so both the agent
(uid 10001 inside the image) and the human (the invoking uid) can write.

Runs on the rig HOST, never inside the sandbox. The deploy key lives in
`~/.ssh/` where D3.2's T3 proved the container cannot see it, so the agent
edits ordinary files and never touches git or a credential (ADR-10 property 4).

Every git invocation disables hooks. `.git/hooks/` executes on the host with
this process's privileges, and the checkout is mounted writable, so a hook
written by the agent would be host code execution right where the deploy key
lives. Neither the filesystem wall nor the network wall sees that path.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGISTRY = Path("~/tekton/projects.toml")
DEFAULT_BRANCH = "main"

# uid the agent runs as inside ghcr.io/openhands/agent-server (D3.2 T6 finding).
AGENT_UID = 10001

GIT_TIMEOUT_S = 300
ACL_TIMEOUT_S = 60

# Hooks off on every call: see the module docstring.
_GIT_BASE = ("git", "-c", "core.hooksPath=/dev/null")


class CheckoutError(RuntimeError):
    """Base class for every failure preparing a checkout."""


class UnknownProjectError(CheckoutError):
    """The job named a project that is not in the registry."""


class GitError(CheckoutError):
    """A git command failed, timed out, or could not be run."""


class AclError(CheckoutError):
    """The ADR-3 ACL pair could not be applied."""


@dataclass(frozen=True)
class Project:
    """One registry entry: where a project lives locally and remotely."""

    name: str
    url: str
    path: Path
    branch: str = DEFAULT_BRANCH


def _git_env() -> dict[str, str]:
    """Environment that makes git fail fast instead of prompting.

    The runner is unattended: a git command that stops to ask for a password
    or to confirm a host key would hang the job until the reaper kills it.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def _run(args: tuple[str, ...], cwd: Path | None = None, timeout: int = GIT_TIMEOUT_S) -> str:
    """Run a command, returning stdout; raise GitError on any failure."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input in argv[0]
            args,
            cwd=None if cwd is None else str(cwd),
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError(f"{args[0]} is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{' '.join(args)} timed out after {timeout}s") from exc
    if done.returncode != 0:
        raise GitError(f"{' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout


def load_registry(path: Path | None = None) -> dict[str, Project]:
    """Read projects.toml into a name -> Project map.

    An unparseable or incomplete registry is fatal rather than partially
    loaded: the runner must never guess a path or invent a repo (ADR-10).
    """
    registry_path = (path or DEFAULT_REGISTRY).expanduser()
    try:
        raw = tomllib.loads(registry_path.read_text())
    except FileNotFoundError as exc:
        raise CheckoutError(f"no project registry at {registry_path}") from exc
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise CheckoutError(f"cannot read {registry_path}: {exc}") from exc
    return {name: _project_from(name, entry) for name, entry in raw.items()}


def _project_from(name: str, entry: object) -> Project:
    """Build one Project from a registry table, rejecting incomplete entries."""
    if not isinstance(entry, dict):
        raise CheckoutError(f"registry entry '{name}' is not a table")
    missing = [k for k in ("url", "path") if not entry.get(k)]
    if missing:
        raise CheckoutError(f"registry entry '{name}' is missing: {', '.join(missing)}")
    return Project(
        name=name,
        url=str(entry["url"]),
        path=Path(str(entry["path"])).expanduser(),
        branch=str(entry.get("branch", DEFAULT_BRANCH)),
    )


def resolve(name: str, registry: dict[str, Project]) -> Project:
    """Look up a project by name, failing loudly on an unknown one.

    Hermes intake accepts any string as a project name, so a typo arrives here
    rather than at intake. Failing fast and naming the known projects is the
    whole handling (ADR-10).
    """
    try:
        return registry[name]
    except KeyError as exc:
        known = ", ".join(sorted(registry)) or "(registry is empty)"
        raise UnknownProjectError(f"unknown project '{name}'; known: {known}") from exc


def _clone(project: Project) -> None:
    """Clone a project that has no checkout yet."""
    project.path.parent.mkdir(parents=True, exist_ok=True)
    _run((*_GIT_BASE, "clone", "--branch", project.branch, project.url, str(project.path)))


def _refresh(project: Project) -> None:
    """Reset an existing checkout to match origin.

    Each task starts from origin. Uncommitted work is discarded on purpose:
    the runner commits and pushes at the end of a task, so anything still
    loose in the tree is debris from a job that died. `clean -fd` leaves
    gitignored files alone, so a project's own `.env` survives.
    """
    at = project.path
    _run((*_GIT_BASE, "fetch", "--prune", "origin"), cwd=at)
    _run((*_GIT_BASE, "checkout", project.branch), cwd=at)
    _run((*_GIT_BASE, "reset", "--hard", f"origin/{project.branch}"), cwd=at)
    _run((*_GIT_BASE, "clean", "-fd"), cwd=at)


def apply_acls(path: Path, agent_uid: int = AGENT_UID) -> None:
    """Grant both the agent uid and the invoking uid write access (ADR-3).

    D3.2's T6 finding: the agent runs as uid 10001 while host checkouts are
    owned by uid 1000, and Docker passes the numeric uid straight through the
    bind mount. Overriding the container user was probed and disproven, so the
    fix lives here. The default entries (`d:`) carry the grant to files the
    agent creates later.
    """
    for uid in (agent_uid, os.getuid()):
        try:
            _run(
                ("setfacl", "-R", "-m", f"u:{uid}:rwX", "-m", f"d:u:{uid}:rwX", str(path)),
                timeout=ACL_TIMEOUT_S,
            )
        except GitError as exc:
            raise AclError(f"setfacl failed for uid {uid}: {exc}") from exc


def prepare(name: str, registry_path: Path | None = None) -> Path:
    """Resolve, clone or refresh, ACL, and return the checkout path.

    The single entry point for slice 3; the runner calls this before mounting.
    """
    project = resolve(name, load_registry(registry_path))
    if (project.path / ".git").is_dir():
        _refresh(project)
    elif project.path.exists():
        raise CheckoutError(f"{project.path} exists but is not a git checkout")
    else:
        _clone(project)
    apply_acls(project.path)
    return project.path


def head_sha(path: Path) -> str:
    """Return the checked-out commit, for the job's structured log line."""
    return _run((*_GIT_BASE, "rev-parse", "HEAD"), cwd=path).strip()
