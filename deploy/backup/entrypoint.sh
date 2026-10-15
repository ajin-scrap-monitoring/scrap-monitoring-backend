#!/usr/bin/env bash
# Initialize only a new, empty backup volume; never take ownership of arbitrary existing files.
set -euo pipefail
umask 077
if [[ ! -f /backups/.scrap-backup-store ]]; then
    if [[ -n $(find /backups -mindepth 1 -maxdepth 1 -print -quit) ]]; then
        echo 'BACKUP_FAILED stage=storage_not_empty' >&2
        exit 1
    fi
    chmod 0700 /backups
    chown postgres:postgres /backups
    gosu postgres touch /backups/.scrap-backup-store
fi
exec gosu postgres bash /opt/scrap-backup/worker.sh "${@:-loop}"
