#!/usr/bin/env bash
# ADR-7 modularity budget. Any breach exits non-zero = build FAILS.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
say(){ printf '%-28s %s\n' "$1" "$2"; }

# File length <= 300 lines (Ruff has no native rule for this)
long=$(find ./src ./tests -name '*.py' -not -path './.venv/*' -not -path './.git/*' \
       -exec awk 'END{if(NR>300) print FILENAME" ("NR" lines)"}' {} \;)
if [ -n "$long" ]; then say "file length (<=300)" "FAIL"; echo "$long"; fail=1; else say "file length (<=300)" "ok"; fi

uv run ruff check src tests >/tmp/ruff.out 2>&1 \
  && say "ruff (size/complexity/docs/dead)" "ok" \
  || { say "ruff (size/complexity/docs/dead)" "FAIL"; cat /tmp/ruff.out; fail=1; }

uv run radon cc -n C -s src 2>/dev/null | grep -v '^$' >/tmp/radon.out
if [ -s /tmp/radon.out ]; then say "radon (complexity C+)" "FAIL"; cat /tmp/radon.out; fail=1; else say "radon (complexity C+)" "ok"; fi

uv run vulture src --min-confidence 80 >/tmp/vulture.out 2>&1
if [ -s /tmp/vulture.out ]; then say "vulture (dead code)" "FAIL"; cat /tmp/vulture.out; fail=1; else say "vulture (dead code)" "ok"; fi

[ "$fail" -eq 0 ] && echo "GATES: PASS" || echo "GATES: FAIL"
exit $fail
