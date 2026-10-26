# mosswork source release notes

[中文](open-source-release.md)

This document is for maintainers and describes source export, verification and release requirements for [mosswork](https://github.com/sudoprivacy/mosswork). The current source version is `0.0.1`; the main project and own communication module use MIT.

This repository provides source code; server images and Windows/macOS enterprise clients are generated according to the corresponding build documents. Release material is distributed through [this repository's Releases](https://github.com/sudoprivacy/mosswork/releases); binary installers require separate target-platform verification and records of signing and notarization status.

## Source scope

- Use Chinese for the default homepage and retain the English edition, English and Chinese employee guides, contribution guide, security policy, and deployment and build instructions.
- Keep the original MIT license and contributor notices in the root and the communication service. See [NOTICE](../NOTICE_EN.md) for third-party sources and distribution scope.
- This distribution disables the online update CLI/API, upstream one-click installers and `make publish` / `publish-test`; use this repository's release images, clients or source.
- The public copy excludes Git history, local tool configuration, runtime environment files, browser sessions, deployment records and build artifacts.
- The `office-automation` expert and its avatars are excluded because bundled document skills restrict redistribution. Tests and examples use experts included publicly; independent document-processing libraries and local tools are unaffected.
- Employee quick start uses [Markdown](user-guide_EN.md); the source release excludes generated `docs/user-guide.html` and the upstream personal-edition introduction `docs/mosswork-introduction.html`.
- The source release excludes FnOS packaging tools and the upstream release-asset download-table generator.
- Upstream workflows remain inactive in `.github/upstream-workflows/` with the `.disabled` extension. Only this project's `mosswork-ci.yml` and `mosswork-release.yml` are preserved as active workflows during export.

## Tag releases

Push a tag exactly matching `v<pyproject.toml version>` after exporting, checking and committing the complete public source. The current version corresponds to `v0.0.1`. The workflow never bumps versions or creates tags; manual runs must select an existing version tag.

The release includes only two client installers: a macOS 13+ ARM64 DMG and a Windows amd64 EXE. Both include the offline local runtime, document tools and checksum-pinned FFmpeg from the public Sudowork mirror. Native runners exercise the packaged runtime before assembly. Windows includes Microsoft's signed offline WebView2 installer. macOS uses ad-hoc signing without notarization; the Windows client is not Authenticode signed.

Server images are distributed as `moss-server-images-<tag>-linux-<arch>.tar.gz` Release assets for Linux amd64 and arm64. Each archive includes moss-server, moss-server-im and PostgreSQL. The installer verifies checksums and imports the images with Docker; Compose uses only local images. GitHub Release publication waits for both clients and both server architectures. Prereleases do not update `latest`; published releases cannot be overwritten. Public source, deployment assets, build records, image IDs and SHA-256 checksums are attached to the same release.

`moss-harness` is maintained in `packages/moss-harness` and built into both server and client runtimes. Its `moss_harness-<version>-py3-none-any.whl` is also attached to the release.

Allow `GITHUB_TOKEN` to write Releases. Packages permissions and registry credentials are not needed. Native Linux runners reload the exported images and verify first installation, health checks and repeat installation. Publication checks that each asset is below GitHub's 2 GiB limit and generates SHA-256 checksums. See the [deployment guide](../docker/enterprise/INSTALL.md) (Chinese) for installation, persistent data and upgrades.

## Export and verification

All changes between formal releases accumulate in the enterprise repository and share one current pending-release entry point. Export candidate source from the complete worktree, including uncommitted changes and new tests from earlier fixes. Record verification status separately instead of splitting fixes into parallel pending releases. Keep older snapshots only for traceability.

Run in the development repository; the output must be a new directory outside the repository that does not already exist:

```sh
python3 tools/export_public_source.py --output /absolute/new-directory/mosswork-Enterprise
python3 tools/check_public_source.py --root /absolute/new-directory/mosswork-Enterprise
```

The export tool uses Git-tracked and unignored files as candidates and copies the current worktree, including uncommitted changes. It does not modify the Git index, branches or remotes. Use a new output directory for each source snapshot.

To refresh the pending-release entry point, export to a new temporary directory and verify it first, then update the source, archive, change list and checksums at that entry point. Retain the previous snapshot as history. Subsequent changes still belong to the same pending release. Before the final build, compare the complete worktree with the candidate manifest again to confirm that nothing is missing; build installers and runtime payloads from that final source together.

`PUBLIC_SOURCE.json` records the source HEAD, `source_worktree_dirty`, export scope and per-file SHA-256. `source_worktree_dirty=true` means the snapshot includes uncommitted changes and must be identified by the per-file manifest and source archive SHA-256. Formal releases export from a clean worktree at a specified commit and keep the corresponding tag, manifest and checksums.

The verification tool checks manifest digests, local links, anchors and images in the main documents, expert prompt files and experts referenced by command examples, and blocks upstream images, download entry points and packaging identity leakage. Archived inactive upstream workflows, provenance acknowledgments and old installers with valid entry guards are not treated as usable distribution entry points. Symbolic links may only point to relative paths inside the release scope; digests cover link text. Built-in credential pattern screening is limited and is not a complete security or license audit.

## Build prerequisites

The server supports Linux + Docker Engine + Compose; commands are in [Enterprise deployment and recovery](../docker/enterprise/README_EN.md). Builds must reach base images and dependencies. The image archives include the application, communication service and PostgreSQL. The destination host must already have Docker Engine, Compose v2 and Python 3.11+; it can then import the downloaded archives and start the stack offline.

Client builds are in [Desktop builds](../desktop/README.md). Release CI targets Windows amd64 and macOS 13+ ARM64. `desktop/package-company.py` reads provenance from `PUBLIC_SOURCE.json` for exports, or Git HEAD/worktree state in a checkout. Exported sources do not need a new Git commit.

Login bundles, `company-dashboard.zip` and generated manifests are not distributed with the source. The full desktop packager builds these from source; building the shell alone requires generating the resources according to the desktop documentation.

## Repository and security entry points

- Issues and suggestions: [this repository's Issues](https://github.com/sudoprivacy/mosswork/issues).
- Security reports: follow [SECURITY](../SECURITY_EN.md) for the private channel; do not post vulnerability details in an issue.
- Contribution branches: open pull requests from a working branch against this repository's `main`; see [Contributing](../CONTRIBUTING_EN.md).
- When creating a new distribution repository, use a verified source snapshot; retain provenance through NOTICE and PUBLIC_SOURCE.

## Release checklist

- Identity of the corresponding commit or candidate snapshot, source archive and SHA-256; directory and archive contents match file by file.
- Version, Harness version, main and communication schema, and device/company-runtime protocol requirements.
- Build platforms and architectures, dependency versions, build commands, server deployment and client installation instructions.
- Platforms and architectures actually verified, plus unverified installation, signing, notarization, capacity, upgrade and recovery items.
- Known limitations, data boundaries, pre-upgrade backup and rollback steps; planned capabilities such as group bots are not listed as delivered.
- LICENSE, NOTICE, communication service and third-party licenses. When distributing installers, include the licenses for FFmpeg, WebView2, Python and other components actually shipped; a source-package check does not replace a binary distribution check.
- If images or installers are provided, publish the unique tag/digest, architectures, SHA-256, signing and notarization status; do not reuse the same formal tag for different source.

Desktop bundles and the Windows assembly use `io.github.sudoprivacy.mosswork`; Docker image metadata points to this repository. Binary releases must specify image addresses, signing identity and upgrade steps; keep employee working folders, account configuration and task receipts before upgrading. Python import paths, CLI commands, environment variables and database names keep the `mosswork` convention.
