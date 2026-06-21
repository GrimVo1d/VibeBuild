#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

source .env

COMPOSE="docker compose"

FEDORA_VERSION="${FEDORA_VERSION:-44}"

echo "==> Waiting for Koji Hub to be ready..."
for i in $(seq 1 60); do
    if $COMPOSE exec -T koji-hub curl -sk https://localhost/kojihub >/dev/null 2>&1; then
        echo "    Hub is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "ERROR: Hub did not become ready in 60 seconds"
        exit 1
    fi
    sleep 1
done

koji_exec() {
    $COMPOSE exec -T koji-hub koji "$@"
}

# Run a koji command that creates something. "Already exists" counts as success,
# so the script can be re-run; any other error stops it.
koji_ensure() {
    local out
    if out=$(koji_exec "$@" 2>&1); then
        return 0
    fi
    if grep -qE "already (exists|in the database|subscribed|associated)" <<<"$out"; then
        echo "    (already set up: koji $1 ${*:2})"
        return 0
    fi
    echo "ERROR: koji $*" >&2
    echo "$out" >&2
    exit 1
}

# Architecture as the containers see it (on Apple Silicon the host says arm64,
# koji and rpm say aarch64). Override with KOJI_ARCH=<arch>.
KOJI_ARCH="${KOJI_ARCH:-$($COMPOSE exec -T koji-hub uname -m | tr -d '\r')}"
echo "==> Using KOJI_ARCH=${KOJI_ARCH}, Fedora ${FEDORA_VERSION}"

echo "==> Creating admin user..."
$COMPOSE exec -T koji-hub python3 -c "
import psycopg2
conn = psycopg2.connect(host='db', dbname='${POSTGRES_DB}', user='${POSTGRES_USER}', password='${POSTGRES_PASSWORD}')
cur = conn.cursor()
cur.execute(\"SELECT id FROM users WHERE name='kojiadmin'\")
if cur.fetchone() is None:
    cur.execute(\"INSERT INTO users (name, status, usertype) VALUES ('kojiadmin', 0, 0)\")
    cur.execute(\"SELECT id FROM users WHERE name='kojiadmin'\")
    uid = cur.fetchone()[0]
    cur.execute(\"SELECT id FROM permissions WHERE name='admin'\")
    pid = cur.fetchone()[0]
    cur.execute(\"INSERT INTO user_perms (user_id, perm_id, creator_id, active) VALUES (%s, %s, %s, TRUE)\", (uid, pid, uid))
    conn.commit()
    print('    Admin user created.')
else:
    print('    Admin user already exists.')
conn.close()
"

echo "==> Adding builder host..."
koji_ensure add-host kojibuilder "${KOJI_ARCH}"
koji_ensure add-host-to-channel kojibuilder createrepo

echo "==> Creating tags..."
koji_ensure add-tag "${KOJI_TAG}" --arches="${KOJI_ARCH}"
koji_ensure add-tag "${KOJI_BUILD_TAG}" --parent="${KOJI_TAG}" --arches="${KOJI_ARCH}"

echo "==> Creating build target..."
koji_ensure add-target "${KOJI_TARGET}" "${KOJI_BUILD_TAG}" "${KOJI_TAG}"

echo "==> Adding build groups to ${KOJI_BUILD_TAG}..."
koji_ensure add-group "${KOJI_BUILD_TAG}" build
koji_ensure add-group "${KOJI_BUILD_TAG}" srpm-build

BUILD_PKGS="bash bzip2 coreutils cpio diffutils fedora-release findutils gawk \
gcc gcc-c++ \
golang \
rust cargo \
cmake meson ninja-build \
autoconf automake libtool pkgconf-pkg-config \
grep gzip info make patch rpm-build redhat-rpm-config sed shadow-utils tar unzip util-linux \
which xz createrepo_c"

SRPM_BUILD_PKGS="bash fedora-release rpm-build redhat-rpm-config shadow-utils"

for pkg in $BUILD_PKGS; do
    koji_ensure add-group-pkg "${KOJI_BUILD_TAG}" build "$pkg"
done

for pkg in $SRPM_BUILD_PKGS; do
    koji_ensure add-group-pkg "${KOJI_BUILD_TAG}" srpm-build "$pkg"
done

echo "==> Adding external Fedora repositories..."
# Create first, then attach: a re-run attaches a repo that exists but is not on the tag yet.
FEDORA_MIRROR="https://dl.fedoraproject.org/pub/fedora/linux"
koji_ensure add-external-repo "f${FEDORA_VERSION}-releases" \
    "${FEDORA_MIRROR}/releases/${FEDORA_VERSION}/Everything/\$arch/os/"
koji_ensure add-external-repo "f${FEDORA_VERSION}-updates" \
    "${FEDORA_MIRROR}/updates/${FEDORA_VERSION}/Everything/\$arch/"
koji_ensure add-external-repo -t "${KOJI_BUILD_TAG}" -p 5 "f${FEDORA_VERSION}-releases"
koji_ensure add-external-repo -t "${KOJI_BUILD_TAG}" -p 10 "f${FEDORA_VERSION}-updates"

echo "==> Regenerating repository (needs koji-builder running, takes a few minutes)..."
# --make-task: plain regen-repo only files a request for kojira, which the stand does not run.
koji_exec regen-repo --make-task --nowait "${KOJI_BUILD_TAG}"

echo ""
echo "=== Koji initialization complete ==="
echo "Try: docker compose exec koji-hub koji list-tags"
