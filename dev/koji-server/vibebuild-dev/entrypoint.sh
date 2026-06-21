#!/usr/bin/env bash
set -euo pipefail

# Configure the koji client inside the container to point at koji-hub
# over the docker network (not at localhost).
mkdir -p /root/.koji
cat > /root/.koji/config <<EOF
[koji]
server = https://koji-hub/kojihub
weburl = https://koji-hub/koji
topurl = https://koji-hub/kojifiles
cert = /etc/pki/koji/kojiadmin.pem
serverca = /etc/pki/koji/koji_ca_cert.crt
authtype = ssl
target = ${KOJI_TARGET:-f44}
build_tag = ${KOJI_BUILD_TAG:-f44-build}
EOF

# Trust the CA for curl/wget/requests
cp /etc/pki/koji/koji_ca_cert.crt /etc/pki/ca-trust/source/anchors/koji_ca_cert.crt
update-ca-trust extract

# Install vibebuild in editable mode from the mounted /workspace.
# Use --no-deps for ml (sklearn/joblib are already installed system-wide), the rest goes through pip.
if [ -f /workspace/pyproject.toml ]; then
    cd /workspace
    pip install --quiet --no-build-isolation -e ".[dev]" \
        || pip install --no-build-isolation -e ".[dev]"
    echo "vibebuild installed in /workspace"
else
    echo "WARNING: /workspace/pyproject.toml not found, vibebuild is not installed"
fi

# Do not fail: keep the container alive so that docker compose exec works
echo ""
echo "=== vibebuild-dev ready ==="
echo "Enter with: docker compose exec vibebuild-dev bash"
echo "Koji config: /root/.koji/config (server = https://koji-hub/kojihub)"
echo ""
exec sleep infinity
