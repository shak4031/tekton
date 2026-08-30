"""Tests for ``tekton_runner.context``.

Summary parsing, the ignore precondition and the npx guard are tested against
fixtures. The pack itself runs repomix for real against a tmp repo when Node is
available — the pinned tool is the artifact that matters, and parsing its
output from a doc rather than from the tool is tag-lesson #6.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tekton_runner.context import (
    CHARS_PER_TOKEN,
    CONTEXT_DIR,
    REPOMIX_PIN,
    ContextError,
    ContextTooLargeError,
    SecretsFoundError,
    _measure,
    _parse_summary,
    _repomix_argv,
    ensure_ignored,
    npx_binary,
    pack,
)

HAS_NPX = shutil.which("npx") is not None

SUMMARY = """
📊 Pack Summary:
────────────────
  Total Files: 3 files
 Total Tokens: 412 tokens
  Total Chars: 1,881 chars
       Output: .tekton/context.md
     Security: ✔ No suspicious files detected
"""

SUMMARY_DIRTY = SUMMARY.replace("✔ No suspicious files detected", "1 suspicious file detected")


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in `cwd`, raising if it fails."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603,S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo that ignores .tekton/, like a real project."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# demo project\n")
    (root / ".gitignore").write_text(f"{CONTEXT_DIR}/\n")
    _git("init", "-b", "main", cwd=root)
    _git("add", "-A", cwd=root)
    _git("-c", "user.email=t@x.io", "-c", "user.name=T", "commit", "-m", "init", cwd=root)
    return root


def test_parse_summary_reads_all_three_counts() -> None:
    """The counts repomix prints are the ones the job log records."""
    parsed = _parse_summary(SUMMARY)
    assert parsed == {"files": 3, "tokens": 412, "chars": 1881}


def test_parse_summary_tolerates_missing_fields() -> None:
    """A changed output format degrades to None, it does not crash."""
    assert _parse_summary("nothing useful here") == {
        "files": None,
        "tokens": None,
        "chars": None,
    }


def test_measure_falls_back_to_file_size(tmp_path: Path) -> None:
    """With no parsed token count, size/4 keeps the ceiling enforceable."""
    f = tmp_path / "context.md"
    f.write_text("x" * 400)
    tokens, estimated = _measure(f, {"tokens": None})
    assert estimated is True
    assert tokens == 400 // CHARS_PER_TOKEN


def test_measure_prefers_the_parsed_count(tmp_path: Path) -> None:
    """Repomix's own tokenizer beats a byte-count estimate."""
    f = tmp_path / "context.md"
    f.write_text("x" * 400)
    assert _measure(f, {"tokens": 412}) == (412, False)


def test_argv_carries_the_pin() -> None:
    """The version is pinned on every invocation, never resolved to latest."""
    argv = _repomix_argv(None)
    assert REPOMIX_PIN in argv
    assert "--remote" not in argv


def test_argv_adds_include_when_scoped() -> None:
    """A per-task scope narrows the pack below the repo-level config."""
    argv = _repomix_argv("src/api/**")
    assert argv[argv.index("--include") + 1] == "src/api/**"


def test_ensure_ignored_passes_when_gitignored(repo: Path) -> None:
    """A project that ignores .tekton/ is safe to pack into."""
    ensure_ignored(repo)


def test_ensure_ignored_fails_when_not_ignored(tmp_path: Path) -> None:
    """Without the ignore, the pack would ride into the agent's PR."""
    root = tmp_path / "unguarded"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    (root / "f.txt").write_text("x")
    _git("add", "-A", cwd=root)
    _git("-c", "user.email=t@x.io", "-c", "user.name=T", "commit", "-m", "init", cwd=root)
    with pytest.raises(ContextError, match="does not ignore"):
        ensure_ignored(root)


def test_npx_binary_rejects_the_windows_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tag-lesson #4, encoded as a check instead of a memory."""
    monkeypatch.setattr(
        "tekton_runner.context.shutil.which",
        lambda _: "/mnt/c/Users/x/AppData/Local/hermes/node/npx",
    )
    with pytest.raises(ContextError, match="Windows binary"):
        npx_binary()


def test_npx_binary_reports_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """No npx at all is a clear message, not an obscure FileNotFoundError."""
    monkeypatch.setattr("tekton_runner.context.shutil.which", lambda _: None)
    with pytest.raises(ContextError, match="npx not found"):
        npx_binary()


@pytest.mark.skipif(not HAS_NPX, reason="node/npx not available")
def test_pack_writes_context_into_the_checkout(repo: Path) -> None:
    """The real thing: repomix runs and the agent gets a file it can read."""
    result = pack(repo)
    assert result.path == repo / CONTEXT_DIR / "context.md"
    assert result.path.is_file()
    assert result.tokens > 0
    assert result.estimated is False
    assert "app.py" in result.path.read_text()


@pytest.mark.skipif(not HAS_NPX, reason="node/npx not available")
def test_pack_enforces_the_token_ceiling(repo: Path) -> None:
    """Over the ceiling fails the job rather than truncating silently."""
    with pytest.raises(ContextTooLargeError, match="over the 1 ceiling"):
        pack(repo, max_tokens=1)


def test_secrets_finding_is_fatal(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A context pack is exactly where a stray .env would leak into a prompt."""
    monkeypatch.setattr("tekton_runner.context.run", lambda *_a, **_k: SUMMARY_DIRTY)
    (repo / CONTEXT_DIR).mkdir(exist_ok=True)
    (repo / CONTEXT_DIR / "context.md").write_text("packed")
    with pytest.raises(SecretsFoundError):
        pack(repo)


def test_empty_output_is_fatal(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A zero-byte pack means success was reported over nothing."""
    monkeypatch.setattr("tekton_runner.context.run", lambda *_a, **_k: SUMMARY)
    with pytest.raises(ContextError, match="is empty"):
        pack(repo)
