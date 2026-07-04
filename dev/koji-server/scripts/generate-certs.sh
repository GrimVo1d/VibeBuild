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
    -subj "/C=US/ST=Local/L=Local/O=Koji/OU=CA/CN=Koji CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash"

generate_cert() {
    local name="$1"
    local cn="$2"
    local eku="$3"
    local san="${4:-}"

    echo "==> Generating $name certificate (CN=$cn)..."
    openssl genrsa -out "${name}.key" $KEY_SIZE 2>/dev/null
    openssl req -new \
        -key "${name}.key" \
        -out "${name}.csr" \
        -subj "/C=US/ST=Local/L=Local/O=Koji/OU=${name}/CN=${cn}"

    {
        echo "[v3_req]"
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature,keyEncipherment"
        echo "extendedKeyUsage=${eku}"
        echo "subjectKeyIdentifier=hash"
        echo "authorityKeyIdentifier=keyid,issuer"
        if [ -n "$san" ]; then
            echo "subjectAltName=${san}"
        fi
    } > "${name}.ext"
    openssl x509 -req -days $DAYS \
        -in "${name}.csr" \
        -CA koji_ca_cert.crt \
        -CAkey koji_ca_cert.key \
        -CAcreateserial \
        -out "${name}.crt" \
        -extfile "${name}.ext" \
        -extensions v3_req 2>/dev/null
    rm -f "${name}.ext"

    cat "${name}.crt" "${name}.key" > "${name}.pem"
    rm -f "${name}.csr"
}

generate_cert "kojihub" "koji-hub" "serverAuth" "DNS:localhost,DNS:koji-hub,IP:127.0.0.1"
generate_cert "kojiweb" "koji-web" "serverAuth,clientAuth" "DNS:localhost,DNS:koji-hub,IP:127.0.0.1"
generate_cert "kojiadmin" "kojiadmin" "clientAuth"
generate_cert "kojibuilder" "kojibuilder" "clientAuth"

chmod 644 *.crt
chmod 600 *.key *.pem

rm -f koji_ca_cert.srl

echo ""
echo "SSL certificates generated in $SSL_DIR:"
ls -la "$SSL_DIR"
