#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
SSL_DIR="$BASE_DIR/ssl"

if [ -d "$SSL_DIR" ] && [ -f "$SSL_DIR/koji_ca_cert.crt" ]; then
    echo "SSL certificates already exist in $SSL_DIR"
    echo "Use --force to regenerate"
    if [ "${1:-}" != "--force" ]; then
        exit 0
    fi
    echo "Regenerating certificates..."
    rm -rf "$SSL_DIR"
fi

mkdir -p "$SSL_DIR"
cd "$SSL_DIR"

DAYS=3650
KEY_SIZE=4096

echo "==> Generating CA certificate..."
openssl genrsa -out koji_ca_cert.key $KEY_SIZE 2>/dev/null
openssl req -new -x509 -days $DAYS \
    -key koji_ca_cert.key \
    -out koji_ca_cert.crt \
    -subj "/C=US/ST=Local/L=Local/O=Koji/OU=CA/CN=Koji CA"

generate_cert() {
    local name="$1"
    local cn="$2"
    local san="${3:-}"

    echo "==> Generating $name certificate (CN=$cn)..."
    openssl genrsa -out "${name}.key" $KEY_SIZE 2>/dev/null
    openssl req -new \
        -key "${name}.key" \
        -out "${name}.csr" \
        -subj "/C=US/ST=Local/L=Local/O=Koji/OU=${name}/CN=${cn}"

    if [ -n "$san" ]; then
        cat > "${name}.ext" <<EXTEOF
[v3_req]
subjectAltName=${san}
EXTEOF
        openssl x509 -req -days $DAYS \
            -in "${name}.csr" \
            -CA koji_ca_cert.crt \
            -CAkey koji_ca_cert.key \
            -CAcreateserial \
            -out "${name}.crt" \
            -extfile "${name}.ext" \
            -extensions v3_req 2>/dev/null
        rm -f "${name}.ext"
    else
        openssl x509 -req -days $DAYS \
            -in "${name}.csr" \
            -CA koji_ca_cert.crt \
            -CAkey koji_ca_cert.key \
            -CAcreateserial \
            -out "${name}.crt" 2>/dev/null
    fi

    cat "${name}.crt" "${name}.key" > "${name}.pem"
    rm -f "${name}.csr"
}

generate_cert "kojihub" "koji-hub" "DNS:localhost,DNS:koji-hub,IP:127.0.0.1"
generate_cert "kojiweb" "koji-web" "DNS:localhost,DNS:koji-hub,IP:127.0.0.1"
generate_cert "kojiadmin" "kojiadmin"
generate_cert "kojibuilder" "kojibuilder"

chmod 644 *.crt *.pem
chmod 600 *.key

rm -f koji_ca_cert.srl

echo ""
echo "SSL certificates generated in $SSL_DIR:"
ls -la "$SSL_DIR"
