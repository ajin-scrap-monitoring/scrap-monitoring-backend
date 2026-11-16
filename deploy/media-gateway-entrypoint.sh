#!/bin/sh
# Validate before envsubst. Never print credentials or the rendered Nginx configuration.
set -eu
MEDIA_AUTH_MODE=${MEDIA_AUTH_MODE:-bearer}
export MEDIA_AUTH_MODE
case "$MEDIA_AUTH_MODE" in
    bearer)
        case "${MEDIA_API_TOKEN:-}" in
            *[!A-Za-z0-9._~+/=-]*)
                echo 'MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION' >&2
                exit 1
                ;;
        esac
        if ! printf '%s' "${MEDIA_API_TOKEN:-}" | grep -Eq '^[A-Za-z0-9._~+/-]+=*$'; then
            echo 'MEDIA_GATEWAY_INVALID_BEARER_CONFIGURATION' >&2
            exit 1
        fi
        ;;
    internal)
        # Do not send even a configured legacy credential to a tokenless upstream.
        MEDIA_API_TOKEN=
        export MEDIA_API_TOKEN
        ;;
    *)
        echo 'MEDIA_GATEWAY_INVALID_AUTH_MODE' >&2
        exit 1
        ;;
esac
exec /docker-entrypoint.sh "$@"
