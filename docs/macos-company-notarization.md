# macOS company distribution verification

### GitHub Actions

The macOS job in `mosswork-release.yml` consumes an ARM64 Stack bootstrap
package. The archive holds the bundle with `native-bundle.json` and
`SHA256SUMS` at its root. It is stored on a draft release of this repository
and pinned by [`desktop/stack-input.json`](../desktop/stack-input.json). The
signed Mac job reads it directly from the draft release using its GitHub job
token and keeps the raw archive on the runner. Changing the pin requires a
reviewed pull request. See [Updating the Stack input](stack-input-update.md).

The Mac verifies the archive and its manifest before using the pinned Stack
signer on a fresh staging copy. It embeds the final signed bytes in the company
client.
The signing custodian provisions `BUILD_CERTIFICATE_BASE64`, `P12_PASSWORD`,
`IDENTITY`, `TEAM_ID`, `APPLE_ID` and `APPLE_ID_PASSWORD` in the
`mosswork-macos-release` GitHub environment, with its approved branch and tag
rules. Only the selected Mac job references this environment.

On the default branch, dispatch with both `qualify_clients_only` and
`sign_bootstrap_candidate` enabled. The run produces a signed DMG, matching
update ZIP, distribution checks and separate Apple submission receipts.
For publication, configure `MOSSWORK_SIGNED_BOOTSTRAP_RELEASE_TAG` to the
intended new version tag before creating that tag on the default branch.
The ordinary release workflow publishes the verified artifacts. Keep the
submission receipts when reconciling an interrupted Apple request.

### Operator Mac

Use the existing company packager with the signing operator's Developer ID
identity and an existing `notarytool` Keychain profile:

```sh
python desktop/package-company.py --platform darwin --arch arm64 \
  --runtime-dir /absolute/qualified-runtime \
  --output /absolute/new-release/artifacts \
  --sign-identity 'Developer ID Application: Your Company (TEAMID1234)' \
  --team-id TEAMID1234 --notary-profile company-notary
```

The runtime and application must come from the same complete current source
candidate. Keep the existing runtime smoke test, dashboard build and native
payload checks. A Private payload must be included by its maintained producer
before signing; changing the app or its archives afterwards invalidates this
qualification.

The packager signs the native executables, Python extensions and shared libraries
in a staging copy of the company runtime. On the target Mac it repeats the runtime
smoke test before recording the hashes for the update ZIP. The bootstrap Python
archive is signed in staging as well. Its manifest records the original archive
digest and the delivered digest before the desktop shell embeds the final pins.

The packager verifies the expected team, bundle identifier, version,
architecture and hardened runtime before submission. It submits the signed
application, requires `Accepted`, staples its ticket, and checks Gatekeeper.
Only then does it create the DMG and whole-application update ZIP. It submits
and staples the signed DMG, mounts it read-only, and checks both delivered
applications, including equality of all runtime payload bytes.

`company-package-status.json` includes `macos_distribution` only after all
checks pass. Without `--notary-profile` this field is null: a Developer ID
signature alone is not notarization. The signed/notarized distribution check
does not certify login, installation, first launch or Private functionality.

Artifact hashes and a pending submission record are retained as
`notary-app.json` and `notary-dmg.json` in the owned build staging directory
before uploading. If upload is interrupted before an ID is returned, inspect
`notarytool history` with the signing operator before retrying. An unknown ID
does not mean the submission failed. Returned IDs are recorded before waiting.
On GitHub Actions, supply the UUID as `notary_submission_id` on the default
branch to retrieve Apple's status and any rejection log. Signed jobs allow up
to two hours for each Apple review.
If a wait is interrupted or a submission is rejected, inspect that same ID using
`xcrun notarytool info` and `log` with the existing Keychain profile. The helper
does not automatically resubmit an uncertain operation. Preserve the failed
receipt; no successful package status is emitted on that path.

Credentials stay in the signing operator's Keychain. This command does not
import certificates, read Apple passwords, alter Gatekeeper or publish assets.
The notarization sequence follows Apple's
[custom workflow documentation](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
and the maintained SudoWork candidate signature checks.
