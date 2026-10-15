#!/usr/bin/env bash
# Only generated, complete backup bundles are eligible for retention cleanup.
set -euo pipefail
umask 077
interval=${BACKUP_INTERVAL_SECONDS:-86400}
retain=${BACKUP_RETAIN_COUNT:-7}
deadline=${BACKUP_TIMEOUT_SECONDS:-300}
for number in "$interval" "$retain" "$deadline"; do
    [[ $number =~ ^[1-9][0-9]{0,6}$ ]] || exit 1
done
(( interval <= 604800 && retain >= 2 && retain <= 365 && deadline <= 3600 )) || exit 1

health() {
    local state started last now
    read -r state started < /backups/status || return 1
    read -r last < /backups/last-success || return 1
    [[ $started =~ ^[0-9]+$ && $last =~ ^[0-9]+$ ]] || return 1
    now=$(date +%s)
    (( now >= last && now - last <= interval + deadline + 60 )) || return 1
    case "$state" in
        OK) return 0 ;;
        RUN) (( now >= started && now - started <= deadline + 30 )) ;;
        *) return 1 ;;
    esac
}

once() (
    exec 9>/backups/.lock
    if ! flock -n 9; then
        echo 'BACKUP_SKIPPED reason=already_running' >&2
        exit 75
    fi
    local pending='' diagnostics='' reader='' stage=prepare
    finish() {
        local result=$?
        trap - EXIT
        if [[ -n $reader ]]; then kill "$reader" 2>/dev/null || true; wait "$reader" 2>/dev/null || true; fi
        if (( result != 0 )); then
            printf 'FAIL %s\n' "$(date +%s)" > /backups/status || true
            echo "BACKUP_FAILED stage=$stage" >&2
        fi
        [[ -z $pending ]] || rm -rf -- "$pending"
        [[ -z $diagnostics ]] || rm -rf -- "$diagnostics"
        exit "$result"
    }
    trap finish EXIT
    trap 'exit 143' TERM INT
    printf 'RUN %s\n' "$(date +%s)" > /backups/status
    stage=source_check
    local revision_count
    revision_count=$(timeout 10 psql --no-password --no-psqlrc --tuples-only --no-align \
        --set=ON_ERROR_STOP=1 --command='SELECT count(*) FROM public.alembic_version' 2>/dev/null)
    [[ $revision_count == 1 ]]
    pending=$(mktemp -d /backups/.partial.XXXXXXXXXX)
    diagnostics=$(mktemp -d /tmp/scrap-backup.XXXXXXXXXX)
    mkfifo "$diagnostics/errors"
    # Drain diagnostics without persisting/printing possible SQL rows or credentials.
    # Any diagnostic, including a warning with exit 0, prevents success publication.
    awk 'END {exit (NR > 0)}' < "$diagnostics/errors" &
    reader=$!
    stage=dump
    local dump_result=0 diagnostic_result=0
    timeout "$deadline" pg_dump --no-password --format=custom \
        --file="$pending/database.dump" 2> "$diagnostics/errors" || dump_result=$?
    wait "$reader" || diagnostic_result=$?
    reader=''
    (( dump_result == 0 && diagnostic_result == 0 )) || exit 1
    stage=archive_check
    pg_restore --list "$pending/database.dump" > /dev/null 2>/dev/null
    (cd "$pending" && sha256sum database.dump > database.dump.sha256 && sha256sum -c database.dump.sha256 > /dev/null)
    printf 'scrap-backup-v1\n' > "$pending/format"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$pending/completed-at"
    sync -f "$pending/database.dump"
    local counter=0
    if [[ -e /backups/counter ]]; then
        read -r counter < /backups/counter
        [[ $counter =~ ^[0-9]{1,10}$ ]]
    fi
    counter=$((10#$counter + 1))
    (( counter <= 9999999999 ))
    printf '%s\n' "$counter" > /backups/counter.next
    mv /backups/counter.next /backups/counter
    sync -f /backups/counter
    local final
    final=$(printf '/backups/backup-%010d-%s' "$counter" "$(date -u +%Y%m%dT%H%M%SZ)")
    [[ ! -e $final ]]
    mv "$pending" "$final"
    pending=''
    sync -f /backups
    stage=retention
    local bundle count=0
    while IFS= read -r bundle; do
        [[ ${bundle##*/} =~ ^backup-[0-9]{10}-[0-9]{8}T[0-9]{6}Z$ ]] || continue
        [[ ! -L $bundle && -f $bundle/format && ! -L $bundle/format ]] || continue
        [[ $(cat "$bundle/format") == scrap-backup-v1 ]] || continue
        count=$((count + 1))
        if (( count > retain )); then rm -rf -- "$bundle"; fi
    done < <(find /backups -mindepth 1 -maxdepth 1 -type d -name 'backup-*' | sort -r)
    stage=status
    printf '%s\n' "$(date +%s)" > /backups/last-success
    printf 'OK %s\n' "$(date +%s)" > /backups/status
    echo 'BACKUP_OK archive=custom checksum=verified'
)

case "${1:-loop}" in
    health) health ;;
    once) once ;;
    loop)
        trap 'exit 0' TERM INT
        while true; do
            # A separate shell preserves errexit; calling a function in `||` would disable it.
            bash "$0" once || true
            sleep "$interval" &
            wait $! || true
        done
        ;;
    *) exit 1 ;;
esac
