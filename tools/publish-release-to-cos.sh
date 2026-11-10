#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: publish-release-to-cos.sh ASSETS_DIR RELEASE_TAG [mode]

Modes:
  --release-only   Upload and verify the immutable release directory.
  --latest-only    Verify the release directory, then publish latest aliases.
  --all            Run both phases (default).
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

[[ "$#" -ge 2 && "$#" -le 3 ]] || {
  usage >&2
  exit 1
}

ASSETS_DIR="$1"
RELEASE_TAG="$2"
MODE="${3:---all}"
COS_BUCKET="${COS_BUCKET:-sudowork-release-1309794936}"
COS_ROOT_PATH="${COS_ROOT_PATH:-mosswork/release}"
COS_PUBLIC_BASE_URL="${COS_PUBLIC_BASE_URL:-https://${COS_BUCKET}.cos.accelerate.myqcloud.com}"

case "$RELEASE_TAG" in
  v*) VERSION="${RELEASE_TAG#v}" ;;
  *) echo "FAIL: invalid release tag: $RELEASE_TAG" >&2; exit 1 ;;
esac
case "$VERSION" in
  ''|*[!0-9A-Za-z._+-]*) echo "FAIL: invalid release version: $VERSION" >&2; exit 1 ;;
esac
case "$MODE" in
  --release-only|--latest-only|--all) ;;
  *) echo "FAIL: invalid mode: $MODE" >&2; exit 1 ;;
esac

[[ -d "$ASSETS_DIR" ]] || {
  echo "FAIL: asset directory does not exist: $ASSETS_DIR" >&2
  exit 1
}
command -v coscmd >/dev/null 2>&1 || {
  echo 'FAIL: coscmd is required' >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo 'FAIL: curl is required' >&2
  exit 1
}

required_files=(
  "install-moss-server.sh"
  "moss-server-deploy-${RELEASE_TAG}.tar.gz"
  "moss-server-images-${RELEASE_TAG}-linux-amd64.tar.gz"
  "moss-server-linux-amd64-images.json"
  "mosswork-company-macos-arm64-${VERSION}.dmg"
  "mosswork-company-windows-amd64-${VERSION}.exe"
  "SHA256SUMS.txt"
)
latest_client_sources=(
  "mosswork-company-macos-arm64-${VERSION}.dmg"
  "mosswork-company-windows-amd64-${VERSION}.exe"
)
latest_client_aliases=(
  "mosswork-latest-macos-arm64.dmg"
  "mosswork-latest-windows-amd64.exe"
)
for file_name in "${required_files[@]}"; do
  [[ -f "$ASSETS_DIR/$file_name" ]] || {
    echo "FAIL: missing release asset: $file_name" >&2
    exit 1
  }
done

(cd "$ASSETS_DIR" && sha256sum -c SHA256SUMS.txt)

COS_PUBLIC_BASE_URL="${COS_PUBLIC_BASE_URL%/}"
RELEASE_PATH="$COS_ROOT_PATH/releases/$RELEASE_TAG"
LATEST_PATH="$COS_ROOT_PATH/latest"
RELEASE_URL="$COS_PUBLIC_BASE_URL/$RELEASE_PATH"
LATEST_URL="$COS_PUBLIC_BASE_URL/$LATEST_PATH"
CURL_ARGS=(
  --silent --show-error --fail --location --retry 6 --retry-delay 2
  --retry-all-errors --connect-timeout 15 --max-time 180
)
IMMUTABLE_HEADERS='{"Cache-Control":"public, max-age=31536000, immutable"}'
LATEST_HEADERS='{"Cache-Control":"no-cache, no-store, must-revalidate"}'

asset_files=()
while IFS= read -r -d '' file; do
  asset_files+=("$file")
done < <(find "$ASSETS_DIR" -maxdepth 1 -type f -print0 | sort -z)
[[ "${#asset_files[@]}" -gt 0 ]] || {
  echo "FAIL: no release assets found in $ASSETS_DIR" >&2
  exit 1
}

verify_remote_content() {
  local local_file="$1" remote_url="$2" downloaded
  downloaded="$(mktemp)"
  if ! curl "${CURL_ARGS[@]}" --output "$downloaded" "$remote_url"; then
    rm -f "$downloaded"
    return 1
  fi
  if ! cmp -s "$local_file" "$downloaded"; then
    rm -f "$downloaded"
    echo "FAIL: remote content mismatch: $remote_url" >&2
    exit 1
  fi
  rm -f "$downloaded"
}

verify_remote_size() {
  local local_file="$1" remote_url="$2" expected_size headers remote_size
  expected_size="$(wc -c < "$local_file" | tr -d ' ')"
  headers="$(curl "${CURL_ARGS[@]}" --head "$remote_url")"
  remote_size="$(printf '%s\n' "$headers" | tr -d '\r' \
    | awk 'tolower($1) == "content-length:" { value=$2 } END { print value }')"
  [[ "$remote_size" == "$expected_size" ]] || {
    echo "FAIL: remote size mismatch for $remote_url: expected $expected_size, got ${remote_size:-missing}" >&2
    exit 1
  }
}

verify_release() {
  local base_url="$1" file file_name
  for file in "${asset_files[@]}"; do
    file_name="${file##*/}"
    verify_remote_size "$file" "$base_url/$file_name"
  done
  verify_remote_content "$ASSETS_DIR/SHA256SUMS.txt" "$base_url/SHA256SUMS.txt"
}

upload_directory() {
  local destination="$1" headers="$2" base_url="$3"
  echo "Uploading release assets to $base_url/"
  for file in "${asset_files[@]}"; do
    file_name="${file##*/}"
    echo "  $file_name"
    coscmd upload -H "$headers" "$file" "$destination/$file_name"
  done
  verify_release "$base_url"
}

publish_latest() {
  local index
  verify_release "$RELEASE_URL"
  echo "Publishing latest aliases to $LATEST_URL/"
  for file in "${asset_files[@]}"; do
    file_name="${file##*/}"
    coscmd upload -H "$LATEST_HEADERS" "$file" "$LATEST_PATH/$file_name"
  done
  for index in "${!latest_client_aliases[@]}"; do
    coscmd upload -H "$LATEST_HEADERS" \
      "$ASSETS_DIR/${latest_client_sources[$index]}" "$LATEST_PATH/${latest_client_aliases[$index]}"
  done
  verify_release "$LATEST_URL"
  for index in "${!latest_client_aliases[@]}"; do
    verify_remote_size "$ASSETS_DIR/${latest_client_sources[$index]}" "$LATEST_URL/${latest_client_aliases[$index]}"
  done
}

case "$MODE" in
  --release-only)
    upload_directory "$RELEASE_PATH" "$IMMUTABLE_HEADERS" "$RELEASE_URL"
    ;;
  --latest-only)
    publish_latest
    ;;
  --all)
    upload_directory "$RELEASE_PATH" "$IMMUTABLE_HEADERS" "$RELEASE_URL"
    publish_latest
    ;;
esac

echo "COS release published successfully: $RELEASE_URL/"
echo "COS download URLs:"
for file in "${required_files[@]}"; do
  echo "$RELEASE_URL/$file"
done
if [[ "$MODE" == "--all" || "$MODE" == "--latest-only" ]]; then
  echo "COS latest URLs:"
  for file in "${required_files[@]}"; do
    echo "$LATEST_URL/$file"
  done
  for file in "${latest_client_aliases[@]}"; do
    echo "$LATEST_URL/$file"
  done
fi
