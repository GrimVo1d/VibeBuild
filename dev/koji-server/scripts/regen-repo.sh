#!/usr/bin/env bash
# Manual trigger of the standard koji regen-repo task for the build tag.
# Uses upstream createrepo_c from Fedora (part of the build group, see koji-init.sh).
# Idempotent: if the task is already running, koji returns its task id without starting it again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

source .env

COMPOSE="${COMPOSE:-docker compose}"
if ! $COMPOSE version >/dev/null 2>&1; then
    if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
        COMPOSE="podman compose"
    else
        echo "ERROR: neither '$COMPOSE' nor 'podman compose' is available"
        exit 1
    fi
fi

TAG="${1:-${KOJI_BUILD_TAG}}"

echo "==> koji regen-repo ${TAG}"
$COMPOSE exec -T koji-hub koji regen-repo "${TAG}" --wait
echo "==> repo for ${TAG} regenerated"
