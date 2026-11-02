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
Requires Linux, Bash, curl, tar, sha256sum and Docker Engine + Compose v2.
No Python, Node or package manager is needed on the deployment host.
  --install-dir PATH   Persistent installation root (default: /opt/moss-server)
  --bind ADDRESS       Listen address (new installation: 0.0.0.0)
  --port PORT          Published port (new installation: 8088)
  --socket PATH        Local Docker socket (default: /var/run/docker.sock)
  --upgrade            Upgrade an existing installation; retain data and keys
Initial credentials are stored in <install-dir>/initial-admin.json (mode 0600).
USAGE
      exit 0 ;;
    *) arguments+=("$1"); shift ;;
  esac
done

[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || {
  echo 'Run on the Linux Docker host with sudo bash.' >&2; exit 1;
}
for tool in curl tar sha256sum docker; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
docker info >/dev/null
docker compose version >/dev/null

case "$(uname -m)" in
  x86_64|amd64) image_arch=amd64 ;;
  aarch64|arm64) image_arch=arm64 ;;
  *) echo 'Only Linux amd64 and arm64 are supported.' >&2; exit 1 ;;
esac

if [[ "$release_tag" == @* ]]; then
  latest_url="$(curl --fail --silent --show-error --location \
    --write-out '%{url_effective}' --output /dev/null \
    https://github.com/sudoprivacy/mosswork/releases/latest)"
  release_tag="${latest_url##*/}"
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

verify_checksum() {
  name=$1
  expected=$(awk -v target="$name" '$2 == target || $2 == "*" target {print $1; exit}' \
    "$download_dir/SHA256SUMS.txt")
  actual=$(sha256sum "$download_dir/$name" | cut -d ' ' -f 1)
  [ -n "$expected" ] && [ "$expected" = "$actual" ] || {
    echo "Download checksum mismatch: $name" >&2
    exit 1
  }
}
verify_checksum "$asset"
verify_checksum "$images"

docker image load --input "$download_dir/$images"
tar -xzf "$download_dir/$asset" -C "$download_dir"
bash "$download_dir/moss-server/deploy.sh" --version "$release_tag" "${arguments[@]+"${arguments[@]}"}"
