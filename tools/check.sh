#!/usr/bin/env bash
#
# Every gate, in the order CI runs them.
#
#   tools/check.sh              # run them all, stop at the first failure
#   tools/check.sh --tracked    # run against a clean copy of tracked files only
#
# One definition of "green", called by .github/workflows/ci.yml, by
# release.yml, and by hand. The workflows used to list the gates themselves and
# drifted: the dead-code check and `ruff format --check` were added to CI and
# never to release, so a tag could publish an image CI would have rejected.
#
# ## --tracked exists because the working tree lies
#
# Gitignored scratch files are invisible to CI but present locally, and they cut
# both ways: a stray file can fail a gate that CI would pass, and — worse — the
# working tree can pass one CI will fail, which has happened here. `--tracked`
# copies out exactly what a fresh clone would contain and runs there, so a green
# result means green on CI.
#
# Note this is *not* the same as `git stash`-ing: untracked-but-not-ignored
# files are included, because CI would see them once committed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "${1:-}" = "--tracked" ]; then
    WORKTREE="$(mktemp -d)"
    trap 'rm -rf "$WORKTREE"' EXIT
    git ls-files -c -o --exclude-standard \
      | while IFS= read -r f; do [ -e "$f" ] && printf '%s\n' "$f"; done \
      | tar -cf - -T - | tar -xf - -C "$WORKTREE"
    echo "→ running against $(git ls-files -c -o --exclude-standard | wc -l) tracked files in a clean copy"
    echo
    cd "$WORKTREE"
fi

# Prefer the venv when there is one; CI installs into the runner's Python and
# has no .venv, where a hardcoded path fails with something that does not point
# at the cause.
if [ -x "$REPO_ROOT/.venv/bin/python" ] && [ -z "${CI:-}" ]; then
    export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

step "lint"
ruff check .
ruff format --check .

# Called by relative path on purpose. Both of these cd to their own directory's
# parent, so invoking $REPO_ROOT's copy under --tracked would silently audit the
# working tree instead of the clean one — which is exactly the false pass this
# mode exists to prevent, and did happen.
step "dead code"
./tools/check-dead-code.sh

# The spec is someone else's contract, so the models are derived from it rather
# than maintained alongside it. This fails if the checked-in copy has drifted,
# which is how a revision upstream announces itself.
step "models match the spec"
./tools/generate-models.sh --check

step "tests"
pytest -q

printf '\n\033[32mall gates passed\033[0m\n'
