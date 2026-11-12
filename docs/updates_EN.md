# mosswork update procedures

[中文](updates.md)

mosswork is built and upgraded by administrators from this repository's source. Configuration updates refresh permissions, model access and feature settings; software updates upgrade the server, client interface and tool environment.

## Routine permission and feature configuration

After an administrator saves an employee's role, permissions or policy, the system notifies that employee automatically. After saving model access or other feature settings, the administrator can switch to the **App Updates** tab under **Admin → Application Settings** and publish a notice in the **Employee client configuration updates** area.

When an employee receives **Company configuration update**, they select **Update settings** when no task is executing or being prepared, no tool approval is pending, no local message is being edited, no attachment is uploading, no attachment is unsent or message queued, and no recording, transcription, browser recording or playback is in progress. The app refreshes the current interface, keeps the site sign-in state and saved drafts, and does not require restarting the client. The server always checks requests against the latest permissions; a delayed refresh does not retain revoked permissions. Manually hidden menus are not forced open by new grants.

This channel applies to clients that already include configuration refresh and only applies permissions, model access and feature settings supported by the current code. The notice does not save configuration for the administrator and does not enable features that are not deployed; see [Client configuration updates](client-config-update_EN.md).

## Server software upgrades

Manual administrator upgrades are current:

1. Read the target release's version, compatibility requirements and database migration notes; confirm whether clients must also be updated.
2. Wait for tasks to finish; in a maintenance window, complete and verify a backup of the main database, communication database, files, keys and isolated workspaces.
3. Build new base, enterprise and communication images from the target source, using independent tags or digests; retain the old images and configuration.
4. Stop the app and communication service, run the new communication image's migration command, then start the new services; main-database migrations run when the app starts.
5. Check readiness, sign-in, model requests, message attachments and local client tasks, then resume normal use.

Commands and rollback conditions are in [Enterprise deployment and recovery](../docker/enterprise/README_EN.md#upgrade-and-rollback). Upgrades do not run `prepare.sh`, do not run `mosswork init` again, and do not replace existing communication keys. The in-product "System backup" is not a backup of every enterprise component.

Web users can load newly deployed web code after refreshing. The employee client interface is bundled in the installer; updating only the server does not replace its page assets. See the target release notes for client compatibility.

## Employee client software upgrades

Stable company clients with the native updater check the official channel at startup and every six hours. You can also select **Check for updates** in the system tray's desktop settings, including before sign-in.

When a new version is available, select **Download update**. Once the complete download passes SHA-256 verification, save unsent edits, select **Restart and install** and confirm. Checking and downloading do not quit the client. Running tasks, pending approvals, unresolved results and unsynced results block the restart; the verified download remains ready until those tasks are handled.

An update replaces the whole native client, bundled interface and matching Python/document/FFmpeg environment. Account settings, working folders and saved task receipts are preserved. On macOS, move the application into a writable Applications folder before updating; do not update from a read-only installation image. The stable update targets are macOS 13+ ARM64 and Windows x64, selected by their actual architecture.

Older clients without an updater need one manual installation of an updater-capable stable release. Complete native installers remain available for offline use.

Publishers must build the web app, desktop shell and matching offline tool payload together, and publish architectures, checksums, signing and real-device verification status; see [Desktop builds](../desktop/README.md). The configuration-update channel cannot deliver new interface code or native programs.

Local projects stay in the employee working folder; before installing or upgrading, keep that folder, the existing account configuration and task receipts. Viewing history across devices does not automatically copy projects or take over running tasks.

## Update sources

Native clients use this repository's official COS stable channel. Startup and manual checks share one manifest; downloads stay pinned to the selected immutable release and its checksum. Server and Python package upgrades remain administrator-managed: `mosswork update` and the online update API stay disabled. Upstream PyPI packages with the same name and personal-edition installers must not replace the enterprise distribution.
