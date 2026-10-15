#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
clean_docker=false
clean_global_cache=false

usage() {
    printf '%s\n' \
        'Usage: scripts/cleanup_local_artifacts.sh [--docker] [--global-cache]' \
        '' \
        'Removes reproducible repository caches and Terraform working directories.' \
        '  --docker        prune unused images belonging to this Compose project' \
        '  --global-cache  clear the global uv package cache'
}

while (($# > 0)); do
    case "$1" in
        --docker)
            clean_docker=true
            ;;
        --global-cache)
            clean_global_cache=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

printf 'Removing repository Python/tool caches...\n'
find "${repo_root}" \
    -path "${repo_root}/.git" -prune -o \
    -path "${repo_root}/.venv" -prune -o \
    -type d \( \
        -name __pycache__ -o \
        -name .mypy_cache -o \
        -name .pytest_cache -o \
        -name .ruff_cache -o \
        -name '*.egg-info' \
    \) -prune -exec rm -rf -- {} +

printf 'Removing Terraform working directories...\n'
find "${repo_root}/terraform" -type d -name .terraform -prune -exec rm -rf -- {} +

if [[ "${clean_global_cache}" == true ]]; then
    if command -v uv >/dev/null 2>&1; then
        printf 'Clearing the reinstallable uv package cache...\n'
        uv cache clean
    else
        printf 'Skipping uv cache: uv is not installed.\n' >&2
    fi
fi

if [[ "${clean_docker}" == true ]]; then
    if docker info >/dev/null 2>&1; then
        printf 'Pruning unused mlops-platform images...\n'
        docker image prune --all --force \
            --filter 'label=com.docker.compose.project=mlops-platform'
    else
        printf '%s\n' \
            'Docker is unavailable in the selected context; repository cleanup completed.' \
            'Select the running context with DOCKER_CONTEXT and rerun --docker.' >&2
        exit 1
    fi
fi

printf 'Cleanup complete. MLflow runs, datasets, virtual environments, volumes, and active images were preserved.\n'
