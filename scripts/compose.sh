#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
compose_command="${1:-}"

docker compose \
    --project-directory "${repo_root}" \
    --file "${repo_root}/compose.yaml" \
    "$@"

case "${compose_command}" in
    build|up)
        printf 'Pruning images left unused by the successful Compose update...\n'
        docker image prune --all --force \
            --filter 'label=com.docker.compose.project=mlops-platform'
        ;;
esac
