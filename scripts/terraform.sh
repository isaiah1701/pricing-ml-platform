#!/usr/bin/env bash

set -euo pipefail

# Terraform otherwise downloads a separate, very large provider package into
# every root module. The shared cache lets Terraform link the same package into
# each working directory instead.
cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}"
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-${cache_root}/terraform/plugin-cache}"
mkdir -p -- "${TF_PLUGIN_CACHE_DIR}"

exec terraform "$@"
