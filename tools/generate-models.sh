#!/usr/bin/env bash
#
# Regenerate retina_telemetry/wire/models.py from the ingest spec.
#
#   tools/generate-models.sh            # regenerate in place
#   tools/generate-models.sh --check    # fail if the checked-in copy is stale
#
# The spec is someone else's contract, so the models are derived from it rather
# than written alongside it. When the server author revises the YAML, this shows
# exactly which fields moved instead of someone diffing by eye.
#
# The three flags matter:
#
#   --collapse-root-models   without it every scalar $ref becomes a RootModel
#                            wrapper, so config_version would be ConfigVersion(root=7)
#                            at construction and .root at every read
#   --use-annotated          puts constraints in Annotated[...] rather than the
#                            deprecated conint/constr forms
#   --field-constraints      keeps ge=/pattern= so the spec's own validation fires
#                            — this is what makes node_id="Unknown" impossible to send

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$REPO_ROOT/docs/node-ingest-v1.yml"
TARGET="$REPO_ROOT/retina_telemetry/wire/models.py"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"

generate() {
  "$PYTHON" -m datamodel_code_generator \
    --input "$SPEC" \
    --input-file-type openapi \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.11 \
    --collapse-root-models \
    --use-annotated \
    --field-constraints \
    --formatters ruff-format \
    --custom-file-header "$(cat <<'EOF'
# GENERATED FILE — do not edit by hand.
#
# Regenerate with tools/generate-models.sh after any change to
# docs/node-ingest-v1.yml. The spec is the contract; this is derived from it.
EOF
)" \
    --output "$1"
}

if [[ "${1:-}" == "--check" ]]; then
  # Staged inside the repo, not in /tmp: datamodel-codegen runs ruff-format on
  # its output, and ruff resolves line-length from the nearest pyproject.toml.
  # Generating elsewhere silently reformats at ruff's default 88 and every run
  # looks stale.
  STAGED="$REPO_ROOT/.models-check.py"
  trap 'rm -f "$STAGED"' EXIT
  generate "$STAGED"
  if diff -q "$TARGET" "$STAGED" >/dev/null; then
    echo "models.py is up to date with the spec"
  else
    echo "models.py is STALE — run tools/generate-models.sh" >&2
    diff "$TARGET" "$STAGED" | head -40 >&2
    exit 1
  fi
else
  generate "$TARGET"
  echo "regenerated $TARGET"
fi
