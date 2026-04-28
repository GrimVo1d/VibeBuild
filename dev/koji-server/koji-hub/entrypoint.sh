#!/usr/bin/env bash
set -euo pipefail

echo "Waiting for PostgreSQL..."
for i in $(seq 1 60); do
    if pg_isready -h db -U "${POSTGRES_USER:-koji}" -q 2>/dev/null; then
        echo "PostgreSQL is ready."
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "ERROR: PostgreSQL did not become ready in 60 seconds"
        exit 1
    fi
    sleep 1
done

SCHEMA_EXISTS=$(PGPASSWORD="${POSTGRES_PASSWORD:-kojisecret}" psql -h db \
    -U "${POSTGRES_USER:-koji}" -d "${POSTGRES_DB:-koji}" -tAc \
    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='users')" 2>/dev/null || echo "f")

if [ "$SCHEMA_EXISTS" != "t" ]; then
    echo "Importing Koji database schema..."
    PGPASSWORD="${POSTGRES_PASSWORD:-kojisecret}" psql -h db \
        -U "${POSTGRES_USER:-koji}" -d "${POSTGRES_DB:-koji}" \
        -f /usr/share/koji/schema.sql
    echo "Schema imported."
else
    echo "Database schema already exists."
fi

echo "Starting Apache httpd..."
exec httpd -D FOREGROUND
