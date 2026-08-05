#!/usr/bin/env bash
set -euo pipefail

# Point the koji client at koji-hub over the docker network (not at localhost).
cat > "$HOME/.koji/config" <<CONFIG
[koji]
server = https://koji-hub/kojihub
weburl = https://koji-hub/koji
topurl = https://koji-hub/kojifiles
cert = /etc/pki/koji/kojiadmin.pem
serverca = /etc/pki/koji/koji_ca_cert.crt
authtype = ssl
target = ${KOJI_TARGET:-f44}
build_tag = ${KOJI_BUILD_TAG:-f44-build}
CONFIG

# Keep the container alive so that docker compose exec works
echo "=== vibebuild-dev ready: docker compose exec vibebuild-dev bash ==="
exec sleep infinity
