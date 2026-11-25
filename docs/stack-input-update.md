# Updating the Stack input for the signed Mac client

This guide is written for AI coding agents (Claude Code, Codex and similar) and
the maintainers who review their work. It covers replacing the Stack bootstrap
package that the signed macOS client embeds.

## How the input reaches a release

The signed Mac client embeds one Stack bootstrap package produced by SudoStack's
`private_macos_candidate.py`. The package is stored as an asset on a **draft**
release of this repository, so it stays private.

[`desktop/stack-input.json`](../desktop/stack-input.json) pins that asset: its
draft release tag, asset ID, name, size, SHA-256, the SHA-256 of the package's
`native-bundle.json`, and the qualification and source commits recorded in that
manifest. The pin is the only place these values are configured.

When `mosswork-release.yml` builds a signed Mac client, the `stack-input` job
reads the pinned asset with a write-scoped job token (GitHub only serves draft
assets to writers), checks it against the pin and passes it to the Mac job as a
one-day workflow artifact. The Mac job keeps read-only access. It checks the
SHA-256 again, verifies the manifest against the pin and verifies every bundled
file before signing. No download URL, secret or per-run setup is involved.

## When a new package is needed

The package changes only when its contents must change:

- a SudoCode (`scode`) fix or feature must ship;
- a Nexus daemon or enrollment binary changes;
- the standalone Python runtime or grpcurl is updated;
- the bootstrap scripts or private cloud setup change;
- the bundle format or the pinned Stack signer changes.

Mosswork application changes alone never need a new package.

## Agent workflow

An agent prepares the change; a human approves it. The agent works with the
repository access of the person running it. Write access is enough.

1. **Build the package** with the SudoStack producer at the source commits you
   were asked to use. Record the producer command and the commits.
2. **Check it locally** before uploading. Extract it to a new empty directory and
   run, from the repository root:

   ```sh
   uv run --no-project --python 3.12 python desktop/extract_bootstrap_release.py \
     /abs/stack.tar.gz "$(shasum -a 256 /abs/stack.tar.gz | cut -d' ' -f1)" /abs/new-dir
   ```

   The archive must have `native-bundle.json` and `SHA256SUMS` at its root, and
   the manifest platform must be `macos-arm64`.
3. **Upload it to a new draft release.** Use one draft per package so earlier
   packages remain available for rollback:

   ```sh
   gh release create bootstrap-input-macos-arm64-YYYYMMDD --repo sudoprivacy/mosswork \
     --draft --title "Internal ARM64 bootstrap signing input YYYYMMDD" \
     --notes "Private signing input. Do not publish." /abs/stack.tar.gz
   ```

   If that tag is taken, add a `-2` suffix. Read the new asset's ID and digest:

   ```sh
   gh api repos/sudoprivacy/mosswork/releases \
     --jq '.[] | select(.tag_name=="bootstrap-input-macos-arm64-YYYYMMDD") | .assets[] | {id,name,size,digest}'
   ```

4. **Update `desktop/stack-input.json`** on a new branch. Fill `release_tag`,
   `asset_id`, `asset_name`, `size` and `sha256` from the release asset. Copy
   `qualification` and `sources` from the package's `native-bundle.json`, and set
   `manifest_sha256` to that file's SHA-256. Then run:

   ```sh
   uv run --no-project --python 3.12 python -I desktop/stack_input.py verify /abs/stack.tar.gz --bundle /abs/new-dir
   uv run pytest desktop/tests/test_stack_input.py desktop/tests/test_bootstrap_release.py -q
   ```

5. **Open a pull request** containing only the pin change. In its description,
   state why the package changes, which components changed (old and new versions
   or commits), the producer command, and the draft release tag. The
   `mosswork stack input` check downloads the pinned asset, repeats every check
   and writes a field-by-field comparison with the current `main` pin plus the
   component sources recorded in the package manifest.
6. **Stop and hand over for review.** Do not merge your own pull request.

### What an agent must not do

- Merge the pin pull request, or push the pin change directly to `main`.
- Create, change or delete repository or environment variables and secrets,
  including `MOSSWORK_BOOTSTRAP_ARM64_DRAFT_ASSET_ID`, which stays in place.
- Publish, edit or delete a draft input release. Publishing one would make its
  binaries public.
- Point the pin at an asset outside this repository or reuse a deleted asset ID.
- Start a signed release run unless a maintainer asks for it.

## Human review

The SHA-256 check proves that a release uses the package the pin names. It does
not prove that the package is the right one, so the review is the approval step.
Before approving, the reviewer confirms that:

- the `mosswork stack input` check passed for the latest commit;
- the component sources in the check summary are the intended versions;
- the qualification level fits the planned release;
- the pull request changes nothing besides `desktop/stack-input.json`, unless the
  extra change was requested.

After merging, the next signed release run on `main` uses the new package.

## Rollback

Revert the pin pull request. The earlier draft release and its asset are
unchanged, so the previous package is used again.
