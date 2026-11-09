#!/usr/bin/env bash
set -euo pipefail

release_tag="${MOSSWORK_RELEASE_TAG:-@@MOSSWORK_RELEASE_TAG@@}"
offline_dir=
arguments=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || { echo '--version requires a tag' >&2; exit 1; }
      release_tag="$2"; shift 2 ;;
    --offline)
      offline_dir=.; shift ;;
    --offline-dir|--local-dir)
      [[ $# -ge 2 && -n "$2" ]] || { echo "$1 requires a directory" >&2; exit 1; }
      offline_dir="$2"; shift 2 ;;
    --help|-h)
      cat <<'USAGE'
Usage: install-moss-server.sh [--version vX.Y.Z] [options]
Requires Linux, Bash, tar, sha256sum and Docker Engine + Compose v2.
Online installation also requires curl.
No Python, Node or package manager is needed on the deployment host.
  --install-dir PATH   Persistent installation root (default: /opt/moss-server)
  --bind ADDRESS       Listen address (new installation: 0.0.0.0)
  --port PORT          Published port (new installation: 8088)
  --socket PATH        Local Docker socket (default: /var/run/docker.sock)
  --upgrade            Upgrade an existing installation; retain data and keys
  --offline            Use release files from the current directory
  --offline-dir PATH    Use release files from PATH; --local-dir is an alias
                        Put the deployment archive and SHA256SUMS.txt there.
                        The image archive is optional when images are already loaded.
Initial credentials are stored in <install-dir>/initial-admin.json (mode 0600).
USAGE
      exit 0 ;;
    *) arguments+=("$1"); shift ;;
  esac
done

[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || {
  echo 'Run on the Linux Docker host with sudo bash.' >&2; exit 1;
}
for tool in tar sha256sum docker; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
if [ -z "$offline_dir" ]; then
  command -v curl >/dev/null || { echo 'Missing required tool: curl' >&2; exit 1; }
else
  [ -d "$offline_dir" ] || { echo "Offline directory does not exist: $offline_dir" >&2; exit 1; }
  offline_dir=$(CDPATH='' cd -- "$offline_dir" && pwd)
fi
docker info >/dev/null
docker compose version >/dev/null

case "$(uname -m)" in
  x86_64|amd64) image_arch=amd64 ;;
  aarch64|arm64) image_arch=arm64 ;;
  *) echo 'Only Linux amd64 and arm64 are supported.' >&2; exit 1 ;;
esac

if [[ "$release_tag" == @* && -n "$offline_dir" ]]; then
  offline_deploy_candidates=("$offline_dir"/moss-server-deploy-v*.tar.gz)
  if [ "${#offline_deploy_candidates[@]}" -eq 1 ] && [ -f "${offline_deploy_candidates[0]}" ]; then
    offline_name=${offline_deploy_candidates[0]##*/}
    release_tag=${offline_name#moss-server-deploy-}
    release_tag=${release_tag%.tar.gz}
  else
    echo 'Offline installation needs --version when the deployment archive is not uniquely named.' >&2
    exit 1
  fi
fi
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
asset_dir=$download_dir
if [ -n "$offline_dir" ]; then
  asset_dir=$offline_dir
  for name in "$asset" SHA256SUMS.txt; do
    [ -f "$asset_dir/$name" ] || {
      echo "Offline file is missing: $asset_dir/$name" >&2
      exit 1
    }
  done
  if [ ! -f "$asset_dir/$images" ]; then
    echo "Offline image archive not found; using already-loaded Docker images: $images" >&2
  fi
else
  base="https://github.com/sudoprivacy/mosswork/releases/download/${release_tag}"
  for name in "$asset" "$images" SHA256SUMS.txt; do
    curl --fail --silent --show-error --location --retry 3 "$base/$name" -o "$download_dir/$name"
  done
fi

verify_checksum() {
  name=$1
  expected=$(awk -v target="$name" '$2 == target || $2 == "*" target {print $1; exit}' \
    "$asset_dir/SHA256SUMS.txt")
  actual=$(sha256sum "$asset_dir/$name" | cut -d ' ' -f 1)
  if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
    echo "Download checksum mismatch: $name" >&2
    exit 1
  fi
}
verify_checksum "$asset"
if [ -f "$asset_dir/$images" ]; then
  verify_checksum "$images"
  docker image load --input "$asset_dir/$images"
fi
tar -xzf "$asset_dir/$asset" -C "$download_dir"
bash "$download_dir/moss-server/deploy.sh" --version "$release_tag" "${arguments[@]+"${arguments[@]}"}"
