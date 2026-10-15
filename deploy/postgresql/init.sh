#!/usr/bin/env bash
# First boot only. Existing volumes and passwords are never changed by this script.
set -euo pipefail

verifier_pattern='^SCRAM-SHA-256\$[1-9][0-9]*:[A-Za-z0-9+/]+={0,2}\$[A-Za-z0-9+/]+={0,2}:[A-Za-z0-9+/]+={0,2}$'
for setting in POSTGRES_PASSWORD DB_OWNER_SCRAM DB_APP_SCRAM DB_BACKUP_SCRAM; do
    if [[ ! ${!setting:-} =~ $verifier_pattern ]]; then
        echo "PostgreSQL initialization requires a SCRAM verifier for $setting" >&2
        exit 1
    fi
done

# psql quotes each verifier as a SQL literal. No plaintext password is written or echoed.
psql --no-password --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=admin_verifier="$POSTGRES_PASSWORD" \
    --set=owner_verifier="$DB_OWNER_SCRAM" \
    --set=app_verifier="$DB_APP_SCRAM" \
    --set=backup_verifier="$DB_BACKUP_SCRAM" \
    --file=/opt/scrap-postgresql/roles.sql
touch "$PGDATA/.scrap-initialized"
