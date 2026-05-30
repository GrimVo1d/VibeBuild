#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

source .env

COMPOSE="docker compose"

# Detect host architecture so the dev stand works on both amd64 and arm64.
# Override with KOJI_ARCH=<arch> in environment if a cross-arch koji is wanted.
KOJI_ARCH="${KOJI_ARCH:-$(uname -m)}"
echo "==> Using KOJI_ARCH=${KOJI_ARCH}"

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
" 2>/dev/null || echo "    (admin user may already exist)"

echo "==> Adding builder host..."
koji_exec add-host kojibuilder "${KOJI_ARCH}" 2>/dev/null || echo "    (host may already exist)"
koji_exec add-host-to-channel kojibuilder createrepo 2>/dev/null || echo "    (channel assignment may already exist)"

echo "==> Creating tags..."
koji_exec add-tag ${KOJI_TAG} --arches="${KOJI_ARCH}" 2>/dev/null || echo "    (tag ${KOJI_TAG} may already exist)"
koji_exec add-tag ${KOJI_BUILD_TAG} --parent=${KOJI_TAG} --arches="${KOJI_ARCH}" 2>/dev/null || echo "    (tag ${KOJI_BUILD_TAG} may already exist)"

echo "==> Creating build target..."
koji_exec add-target ${KOJI_TARGET} ${KOJI_BUILD_TAG} ${KOJI_TAG} 2>/dev/null || echo "    (target may already exist)"

echo "==> Adding build groups to ${KOJI_BUILD_TAG}..."
koji_exec add-group ${KOJI_BUILD_TAG} build 2>/dev/null || true
koji_exec add-group ${KOJI_BUILD_TAG} srpm-build 2>/dev/null || true

BUILD_PKGS="bash bzip2 coreutils cpio diffutils fedora-release findutils gawk gcc gcc-c++ \
grep gzip info make patch rpm-build redhat-rpm-config sed shadow-utils tar unzip util-linux \
which xz"

SRPM_BUILD_PKGS="bash fedora-release rpm-build redhat-rpm-config shadow-utils"

for pkg in $BUILD_PKGS; do
    koji_exec add-group-pkg ${KOJI_BUILD_TAG} build "$pkg" 2>/dev/null || true
done

for pkg in $SRPM_BUILD_PKGS; do
    koji_exec add-group-pkg ${KOJI_BUILD_TAG} srpm-build "$pkg" 2>/dev/null || true
done

echo "==> Adding external Fedora repositories..."
koji_exec add-external-repo -t ${KOJI_BUILD_TAG} \
    f42-releases \
    'https://dl.fedoraproject.org/pub/fedora/linux/releases/42/Everything/$arch/os/' \
    2>/dev/null || echo "    (repo f42-releases may already exist)"

koji_exec add-external-repo -t ${KOJI_BUILD_TAG} \
    f42-updates \
    'https://dl.fedoraproject.org/pub/fedora/linux/updates/42/Everything/$arch/' \
    2>/dev/null || echo "    (repo f42-updates may already exist)"

echo "==> Regenerating repository (this may take a while)..."
koji_exec regen-repo ${KOJI_BUILD_TAG} 2>/dev/null || echo "    (regen-repo may already be running)"

echo ""
echo "=== Koji initialization complete ==="
echo "Try: koji --noauth list-tags"
