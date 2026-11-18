#!/usr/bin/env bash
# Add Developer ID signing arguments to the company packager using a temporary keychain.
set -euo pipefail

: "${BUILD_CERTIFICATE_BASE64:?Set BUILD_CERTIFICATE_BASE64 to the Developer ID Application P12}"
: "${P12_PASSWORD:?Set P12_PASSWORD to the P12 import password}"
: "${TEAM_ID:?Set TEAM_ID to the Apple Developer team ID}"

package_umask="$(umask)"
existing_keychains="$(security list-keychains -d user)"
umask 077
work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/mosswork-signing.XXXXXX")"
keychain="$work/build.keychain-db"
cleanup() {
  if [[ -f "$keychain" ]]; then
    security delete-keychain "$keychain"
  fi
  rm -rf "$work"
}
trap cleanup EXIT

printf '%s' "$BUILD_CERTIFICATE_BASE64" | base64 --decode -o "$work/certificate.p12"
keychain_password="$(openssl rand -hex 24)"
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  echo "::add-mask::$keychain_password"
fi
security create-keychain -p "$keychain_password" "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
security set-keychain-settings "$keychain"
security import "$work/certificate.p12" -k "$keychain" -P "$P12_PASSWORD" -T /usr/bin/codesign
rm "$work/certificate.p12"
unset BUILD_CERTIFICATE_BASE64 P12_PASSWORD

# The G2 issuer URL is in Apple's leaf certificate. Verify the public CA before importing it.
curl --fail --location --retry 3 --max-time 30 --silent --show-error \
  http://certs.apple.com/devidg2.der --output "$work/devidg2.der"
(cd "$work" && printf '%s  devidg2.der\n' f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a | shasum -a 256 --check)
security import "$work/devidg2.der" -k "$keychain"
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain" >/dev/null
# codesign uses the search list to embed the issuer chain.
keychains=("$keychain")
while IFS= read -r path; do
  if [[ -n "$path" ]]; then
    keychains+=("$path")
  fi
done < <(sed -E 's/^[[:space:]]*"//; s/"$//' <<< "$existing_keychains")
security list-keychains -d user -s "${keychains[@]}"

identity="$(security find-identity -v -p codesigning "$keychain" | awk -v team="$TEAM_ID" '
  $3 == "\"Developer" && $4 == "ID" && $5 == "Application:" && $NF == "(" team ")\"" { print $2 }
')"
if [[ ! "$identity" =~ ^[A-F0-9]{40}$ ]]; then
  echo 'Expected one valid Developer ID Application certificate for TEAM_ID in the imported P12' >&2
  exit 1
fi

# Carry file modes from the packager's environment into the application and the DMG.
umask "$package_umask"
"$@" --sign-identity "$identity"
