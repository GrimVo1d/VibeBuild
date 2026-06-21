#!/usr/bin/env bash
# Manual trigger of a newRepo task for the build tag. --make-task creates the task
# directly: plain `regen-repo` only files a request for kojira, which the stand does not run.
# Uses upstream createrepo_c from Fedora (part of the build group, see koji-init.sh).
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
$COMPOSE exec -T koji-hub koji regen-repo "${TAG}" --make-task --wait
echo "==> repo for ${TAG} regenerated"
