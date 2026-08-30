"""Tests for ``tekton_runner.checkout``.

Git operations run against a local bare repo created in a tmp dir, so the
clone and refresh paths are exercised for real without touching GitHub or
needing the network. The ACL step is skipped where `setfacl` is unavailable;
it is proven on the rig instead, which is the only place it matters.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tekton_runner import checkout as mod
from tekton_runner.checkout import (
    AGENT_UID,
    GIT_BASE,
    CheckoutError,
    GitError,
    Project,
    UnknownProjectError,
    apply_acls,
    head_sha,
    load_registry,
    prepare,
    reclaim,
    resolve,
)

HAS_SETFACL = shutil.which("setfacl") is not None


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in `cwd`, raising if it fails."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603,S607


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo with one commit on `main`, standing in for GitHub."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / "README.md").write_text("# guinea pig\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    bare = tmp_path / "origin.git"
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def registry_file(tmp_path: Path, origin: Path) -> Path:
    """A projects.toml pointing at the bare repo."""
    path = tmp_path / "projects.toml"
    path.write_text(
        f'[sandbox-test]\nurl = "{origin}"\npath = "{tmp_path / "checkouts" / "sandbox-test"}"\n'
    )
    return path


def test_load_registry_parses_entries(registry_file: Path) -> None:
    """A well-formed registry yields Project objects keyed by name."""
    registry = load_registry(registry_file)
    assert set(registry) == {"sandbox-test"}
    assert registry["sandbox-test"].branch == "main"
    assert registry["sandbox-test"].path.is_absolute()


def test_missing_registry_is_fatal(tmp_path: Path) -> None:
    """No registry means the runner cannot know where anything lives."""
    with pytest.raises(CheckoutError, match="no project registry"):
        load_registry(tmp_path / "nope.toml")


def test_malformed_registry_is_fatal(tmp_path: Path) -> None:
    """A broken registry must not load partially."""
    bad = tmp_path / "bad.toml"
    bad.write_text("[sandbox-test\nurl = ")
    with pytest.raises(CheckoutError, match="cannot read"):
        load_registry(bad)


def test_incomplete_entry_is_fatal(tmp_path: Path) -> None:
    """An entry without a url would leave the runner guessing."""
    bad = tmp_path / "bad.toml"
    bad.write_text('[sandbox-test]\npath = "/tmp/x"\n')
    with pytest.raises(CheckoutError, match="missing: url"):
        load_registry(bad)


def test_unknown_project_names_the_known_ones(registry_file: Path) -> None:
    """A typo'd project name fails fast and says what is available."""
    registry = load_registry(registry_file)
    with pytest.raises(UnknownProjectError, match="sandbox-test"):
        resolve("sandbox-tset", registry)


def test_unknown_project_on_empty_registry(tmp_path: Path) -> None:
    """An empty registry still produces a usable error."""
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    with pytest.raises(UnknownProjectError, match="registry is empty"):
        resolve("anything", load_registry(empty))


@pytest.mark.skipif(not HAS_SETFACL, reason="setfacl not available")
def test_prepare_clones_when_absent(registry_file: Path) -> None:
    """The clone path: no checkout yet, so one is created from origin."""
    path = prepare("sandbox-test", registry_file)
    assert (path / ".git").is_dir()
    assert (path / "README.md").read_text() == "# guinea pig\n"


@pytest.mark.skipif(not HAS_SETFACL, reason="setfacl not available")
def test_prepare_refreshes_when_present(registry_file: Path) -> None:
    """The refresh path: a second call resets an existing checkout."""
    path = prepare("sandbox-test", registry_file)
    first = head_sha(path)
    (path / "README.md").write_text("agent scribbled here\n")
    (path / "junk.txt").write_text("debris from a dead job\n")
    path2 = prepare("sandbox-test", registry_file)
    assert path2 == path
    assert head_sha(path) == first
    assert (path / "README.md").read_text() == "# guinea pig\n"
    assert not (path / "junk.txt").exists()


def test_prepare_refuses_a_non_git_directory(tmp_path: Path, origin: Path) -> None:
    """A directory that is not a checkout is an error, not something to clone into."""
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "stuff").write_text("x")
    reg = tmp_path / "r.toml"
    reg.write_text(f'[p]\nurl = "{origin}"\npath = "{target}"\n')
    with pytest.raises(CheckoutError, match="not a git checkout"):
        prepare("p", reg)


def test_clone_failure_surfaces_as_git_error(tmp_path: Path) -> None:
    """An unreachable remote fails the job with git's own message attached."""
    reg = tmp_path / "r.toml"
    reg.write_text(f'[p]\nurl = "{tmp_path / "does-not-exist.git"}"\npath = "{tmp_path / "c"}"\n')
    with pytest.raises(GitError):
        prepare("p", reg)


def test_hooks_are_disabled_on_every_git_call() -> None:
    """A hook in .git would run on the HOST with the runner's privileges."""
    assert "core.hooksPath=/dev/null" in GIT_BASE


@pytest.mark.skipif(not HAS_SETFACL, reason="setfacl not available")
def test_apply_acls_grants_the_agent_uid(tmp_path: Path) -> None:
    """The agent's uid must appear in the ACL, or it cannot write to the mount."""
    target = tmp_path / "checkout"
    target.mkdir()
    apply_acls(target)
    cmd = ["getfacl", "-p", str(target)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603
    assert f"user:{AGENT_UID}:rwx" in out
    assert f"default:user:{AGENT_UID}:rwx" in out


def test_project_defaults_to_main() -> None:
    """A registry entry without an explicit branch tracks main."""
    assert Project(name="p", url="u", path=Path("/srv/p")).branch == "main"


def test_reclaim_reuses_the_pinned_image_and_runs_as_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One pin, one place: reclaim must not name its own image."""
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(mod, "run", lambda args, **_k: seen.append(tuple(args)) or "")
    monkeypatch.setattr(mod, "apply_acls", lambda _p: None)
    reclaim(tmp_path)
    argv = seen[0]
    assert argv[0] == "docker"
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "0"
    assert mod.AGENT_IMAGE in argv
    assert "sudo" not in " ".join(argv)


def test_reclaim_reapplies_the_acl_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chown alone leaves mask::--- behind; setfacl is what recalculates it."""
    applied: list[Path] = []
    monkeypatch.setattr(mod, "run", lambda *_a, **_k: "")
    monkeypatch.setattr(mod, "apply_acls", applied.append)
    reclaim(tmp_path)
    assert applied == [tmp_path]
