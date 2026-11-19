#!/usr/bin/env bash
# Configure the disposable GitHub-hosted Mac using the team's Apple identity.
set -euo pipefail
set +x
umask 077
: "${RUNNER_TEMP:?}" "${BUILD_CERTIFICATE_BASE64:?}" "${P12_PASSWORD:?}"
: "${IDENTITY:?}" "${TEAM_ID:?}" "${APPLE_ID:?}" "${APPLE_ID_PASSWORD:?}"

cert=$(mktemp "$RUNNER_TEMP/mosswork-certificate.XXXXXX")
issuer="$cert.devidg2.der"
trap 'rm -f "$cert" "$issuer"' EXIT
keychain="$RUNNER_TEMP/mosswork-release.keychain-db"
keychain_password=$(openssl rand -hex 32)
printf '%s' "$BUILD_CERTIFICATE_BASE64" | base64 -D > "$cert"
unset BUILD_CERTIFICATE_BASE64
security create-keychain -p "$keychain_password" "$keychain"
security set-keychain-settings -lut 10800 "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
security import "$cert" -f pkcs12 -k "$keychain" -P "$P12_PASSWORD" -T /usr/bin/codesign >/dev/null
unset P12_PASSWORD
rm -f "$cert"
# Apple's leaf certificate names this public G2 issuer. Verify its DER bytes before import.
curl --fail --location --retry 3 --max-time 30 --silent --show-error \
  http://certs.apple.com/devidg2.der --output "$issuer"
printf '%s  %s\n' f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a "$issuer" | shasum -a 256 --check
security import "$issuer" -f x509 -k "$keychain" >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain" >/dev/null
unset keychain_password
previous=()
while IFS= read -r line; do
  entry=${line#*\"}
  previous+=("${entry%\"*}")
done < <(security list-keychains -d user)
security list-keychains -d user -s "$keychain" "${previous[@]}"
# Keep notarytool's default credential store consistent with the submit and wait steps.
xcrun notarytool store-credentials mosswork-release \
  --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APPLE_ID_PASSWORD"
