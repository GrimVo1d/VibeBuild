#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

source .env

SSL_DIR="$BASE_DIR/ssl"
KOJI_DIR="$HOME/.koji"

if [ ! -d "$SSL_DIR" ]; then
    echo "ERROR: SSL directory not found at $SSL_DIR"
    echo "Run 'make certs' first."
    exit 1
fi

mkdir -p "$KOJI_DIR"

if [ -f "$KOJI_DIR/config" ]; then
    BACKUP="$KOJI_DIR/config.bak.$(date +%Y%m%d%H%M%S)"
    cp "$KOJI_DIR/config" "$BACKUP"
    echo "Existing config backed up to $BACKUP"
fi

cat > "$KOJI_DIR/config" <<EOF
[koji]
server = https://localhost:${KOJI_HTTPS_PORT}/kojihub
weburl = https://localhost:${KOJI_HTTPS_PORT}/koji
topurl = https://localhost:${KOJI_HTTPS_PORT}/kojifiles
cert = ${SSL_DIR}/kojiadmin.pem
serverca = ${SSL_DIR}/koji_ca_cert.crt
authtype = ssl
target = ${KOJI_TARGET}
build_tag = ${KOJI_BUILD_TAG}
EOF

echo "Koji client configured at $KOJI_DIR/config"
echo ""
echo "Verify with:"
echo "  koji --noauth list-tags"
echo "  koji list-tags"
