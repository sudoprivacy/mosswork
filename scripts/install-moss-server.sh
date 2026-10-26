#!/usr/bin/env bash
set -euo pipefail

release_tag="${MOSSWORK_RELEASE_TAG:-@@MOSSWORK_RELEASE_TAG@@}"
arguments=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || { echo '--version requires a tag' >&2; exit 1; }
      release_tag="$2"; shift 2 ;;
    --help|-h)
      cat <<'USAGE'
Usage: install-moss-server.sh [--version vX.Y.Z] [options]
Requires Linux, root, Python 3.11+, curl, tar and Docker Engine + Compose v2.
  --install-dir PATH   Persistent installation root (default: /opt/moss-server)
  --bind IPV4          Listen address (new installation: 0.0.0.0)
  --port PORT          Published port (new installation: 8088)
  --socket PATH        Local Docker socket (default: /var/run/docker.sock)
  --upgrade            Upgrade after taking a backup; retain data and keys
Initial credentials are stored in <install-dir>/initial-admin.json (mode 0600).
USAGE
      exit 0 ;;
    *) arguments+=("$1"); shift ;;
  esac
done
[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || {
  echo 'Run on the Linux Docker host with sudo bash.' >&2; exit 1;
}
for tool in python3 curl tar docker; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
docker info >/dev/null
docker compose version >/dev/null
case "$(uname -m)" in
  x86_64|amd64) image_arch=amd64 ;;
  aarch64|arm64) image_arch=arm64 ;;
  *) echo 'Only Linux amd64 and arm64 are supported.' >&2; exit 1 ;;
esac
if [[ "$release_tag" == @* ]]; then
  release_tag="$(curl --fail --silent --show-error --location --retry 3 \
    https://api.github.com/repos/sudoprivacy/mosswork/releases/latest \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
fi
[[ "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*$ ]] || {
  echo 'Invalid release tag' >&2; exit 1;
}
download_dir="$(mktemp -d)"
trap 'rm -rf "$download_dir"' EXIT
asset="moss-server-deploy-${release_tag}.tar.gz"
images="moss-server-images-${release_tag}-linux-${image_arch}.tar.gz"
base="https://github.com/sudoprivacy/mosswork/releases/download/${release_tag}"
for name in "$asset" "$images" SHA256SUMS.txt; do
  curl --fail --silent --show-error --location --retry 3 "$base/$name" -o "$download_dir/$name"
done
python3 - "$download_dir" "$asset" "$images" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
records = dict(line.split(maxsplit=1)[::-1] for line in (root / 'SHA256SUMS.txt').read_text().splitlines())
for name in sys.argv[2:]:
    with (root / name).open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != records.get(name):
        raise SystemExit(f'Download checksum mismatch: {name}')
PY
docker image load --input "$download_dir/$images"
tar -xzf "$download_dir/$asset" -C "$download_dir"
python3 "$download_dir/moss-server/deploy.py" --version "$release_tag" "${arguments[@]}"
