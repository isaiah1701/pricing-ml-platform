#!/usr/bin/env bash
set -euo pipefail
umask 077

# Install only the pinned tool; this script never contacts Kubernetes or AWS.
helmfile_version=1.5.0
tool_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/mlops-helmfile"
mkdir -p "$tool_dir"
cd "$tool_dir"
archive="helmfile_${helmfile_version}_linux_amd64.tar.gz"
base_url="https://github.com/helmfile/helmfile/releases/download/v${helmfile_version}"
curl --fail --location --retry 2 --connect-timeout 5 --max-time 120 "$base_url/$archive" -o "$archive"
curl --fail --location --retry 2 --connect-timeout 5 --max-time 30 "$base_url/helmfile_${helmfile_version}_checksums.txt" -o checksums.txt
# Match one exact archive entry; fail if it is missing, duplicated or invalid.
python - "$archive" <<'PY'
import hashlib
import sys
from pathlib import Path
archive = sys.argv[1]
entries = [line.split() for line in Path('checksums.txt').read_text().splitlines()]
matches = [parts[0] for parts in entries if len(parts) == 2 and parts[1].lstrip('*') == archive]
if len(matches) != 1 or hashlib.sha256(Path(archive).read_bytes()).hexdigest() != matches[0]:
    raise SystemExit('Helmfile archive checksum verification failed')
PY
tar -xzf "$archive" helmfile
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$tool_dir" >> "$GITHUB_PATH"
fi
