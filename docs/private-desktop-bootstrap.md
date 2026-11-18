# SudoPrivate deployment from the Mosswork desktop

The SudoCode/Nexus deployment component extends the maintained Mosswork company installer.
Build from the complete current public source export and a bundle produced by
SudoStack's `private_macos_candidate.py`:

```sh
python desktop/package-company.py --platform darwin --arch arm64 \
  --runtime-dir /absolute/company-runtime-payloads \
  --bootstrap-bundle /absolute/stack-private-bundle \
  --output /absolute/new-candidate/artifacts
```

This is an engineering candidate until the actual delivered application passes
installation and login acceptance. The command above does not sign or notarize
it. See [macOS distribution verification](macos-company-notarization.md) for the
existing signing operator's release path.

The application remains **mosswork.app**, with the existing application identifier,
company runtime and update artifacts. The deployment component adds the
Stack-produced SudoCode/Nexus payload and pinned standalone Python. Supplying
`--bootstrap-bundle` never removes or replaces the company runtime. A native
`--verify-private-payload` check verifies the packaged files without launching
the GUI, a Node or a model. The packager compiles the exact manifest and
`SHA256SUMS` hashes into the desktop executable before assembling the app. The
verifier rejects modified metadata, modified payloads, symbolic links and
unlisted files or directories. Login setup and assistant reopening apply the
same checks before using the component; the Terminal launcher checks again
when opened. This protects payload integrity relative to that executable.
Authenticating the whole application still requires Developer ID signing,
notarization and Gatekeeper acceptance; an unsigned replacement app could
replace both the verifier and its pins. None of these checks alone qualifies
installation or login acceptance.

## First open

For a managed installation, pass the organization's verified public origin to
`package-company.py --company-url`. A fresh unified client opens that
organization's sign-in page in its existing window when the server advertises
canonical identity. The account page offers the sign-in methods enabled by the
server. Returning users can reopen the same workspace, which checks their
current session. Organization login currently reaches the account screen.
Attaching the user's original SudoCode session and granting local model access
are separate integration steps.

A saved server takes precedence over the deployment default. A server offering
the original employee login keeps its employee form. With no configured or saved
server, the embedded deployment component still offers the original Cloud phone
flow described below. The unified app prepares its full company runtime when
local work first needs it.

Phone sign-in accepts an existing SudoWork account. Code delivery occurs only
after the user presses **Send code**. Native code talks to the existing
`https://agent.sudoprivacy.com`
authority, using `/api/v1/system-config`, `/api/v1/auth/send-code`,
`/api/v1/auth/login` and the authenticated `/api/v1/client/local-runtime`.
These are the current online product's routes, not a new authentication service.

The native client checks the projection's member, organization, ready state and
local-execution permission. Its human session token never reaches the web view,
SudoCode arguments or a saved configuration. Only the selected model projection
goes through stdin to Stack's `Start.command configure-cloud`; Stack validates
and installs the model configuration. The client then invokes `prepare` and
opens the original interactive bootstrap SudoCode in Terminal. Model usage is
billed to the existing account. No credit or account provisioning is implemented
by this adapter.

The owned deployment is `~/.local/share/sudoprivate/personal-macos`. Subsequent
opens offer **Continue this device's setup assistant** on the same login page,
using Stack's existing session catalogue to resume the prior conversation.
The local configured flag is a convenience for reopening, not a fresh server
permission assertion. Account replacement and credential refresh are not yet
implemented in this candidate; a different existing configuration is preserved
and requires the account-transition flow before replacement.

The app runs as the desktop user. System permissions are requested by the
corresponding macOS operation. An existing Nexus owner is preserved and requires
explicit integration; this package does not adopt or replace it silently.

## Packaging and acceptance

The desktop copies the producer's declared payload bytes and original manifest.
It does not rebuild Nexus, create an identity store, or change the checksum list.
The same company stable update feed replaces the whole application, including
both runtime payloads. There is no Private-specific updater or app identifier.
Release acceptance must verify that the common signed release and its update ZIP
both contain the deployment component; simply enabling the existing updater is
not that acceptance. The packager runs both native payload verification helpers
on the matching build host.

Sign executable components before the Stack producer records their final hashes;
do not re-sign them inside a pinned bundle. Qualify all executable code, including
the standalone Python runtime, before submitting the
outer application. An ad-hoc engineering component is not a distribution artifact.

Required end-to-end evidence uses the delivered Mac application: first open on a
clean user environment, real existing-account login, model configuration without
copying an API key, a real bootstrap conversation, and reopen with the same
session. Mock HTTP, browser, component and checksum tests cover narrower scopes.
Developer ID signing, notarization, downloaded-file Gatekeeper checks, Vault
grant enforcement and the A2A fabricated-reply regression have separate gates;
none is implied by successful login.
