#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}" "${KOJIWEB_SECRET:?}"

# Credentials come from .env, not from the image
umask 027
printf '[hub]\nDBName = %s\nDBUser = %s\nDBPass = %s\n' \
    "$POSTGRES_DB" "$POSTGRES_USER" "$POSTGRES_PASSWORD" > /etc/koji-hub/hub.conf.d/db.conf
printf '[web]\nSecret = %s\n' "$KOJIWEB_SECRET" > /etc/kojiweb/web.conf.d/secret.conf
chgrp apache /etc/koji-hub/hub.conf.d/db.conf /etc/kojiweb/web.conf.d/secret.conf
umask 022

# The web UI runs as apache and cannot read the 0600 bundle from the host
install -m 0640 -o root -g apache /etc/pki/koji/kojiweb.pem /etc/kojiweb/kojiweb.pem

echo "Waiting for PostgreSQL..."
for i in $(seq 1 60); do
    if pg_isready -h db -U "$POSTGRES_USER" -q 2>/dev/null; then
        echo "PostgreSQL is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "ERROR: PostgreSQL did not become ready in 60 seconds"
        exit 1
    fi
    sleep 1
done

SCHEMA_EXISTS=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h db \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='users')" 2>/dev/null || echo "f")

if [ "$SCHEMA_EXISTS" != "t" ]; then
    echo "Importing Koji database schema..."
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h db \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -f /usr/share/koji/schema.sql
    echo "Schema imported."
else
    echo "Database schema already exists."
fi

echo "Starting Apache httpd..."
exec httpd -D FOREGROUND
