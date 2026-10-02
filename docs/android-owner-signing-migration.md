# One-time owner signing migration

This procedure exists only for owner devices that installed the ephemeral-signed
Street Story builds from the 2026-10-01 signing incident, especially `0.1.407`
(and, if installed manually, `0.1.410`). Android cannot update those packages in
place because their private signing keys are not recoverable.

The permanent owner signing identity starts with `android-v415`. Its APK SHA-256
is:

`700e4ea197e0cde67ee7e185641f6cef8a27535ff69fb337245574ba025aa665`

After a device reaches this canonical identity, normal in-app updates are
expected and CI continuously proves previous canonical release -> candidate
`adb install -r` compatibility.

## Helper

Use `scripts/migrate_owner_signing.py`. It is fail-closed and runs in
**backup-only mode by default**.

The helper:

1. requires exactly one ready physical ADB device unless `--serial` is given;
2. verifies the installed build is one of the known affected versions;
3. verifies the public Street Story backend is healthy;
4. force-stops the old app and checks `run-as` access before destructive work;
5. backs up only durable local state:
   - `databases/street-story.db` (+ WAL/SHM when present);
   - `files/stories`;
   - `files/audio`;
   - non-secret topic/config/research/draft preferences;
6. explicitly excludes `street_story_secrets.xml`;
7. reads the backup tar back, requires `street-story.db`, writes a SHA-256
   receipt, downloads exactly `android-v415`, and verifies its pinned SHA-256;
8. performs **no uninstall** unless `--execute` was supplied and a valid token
   file already exists;
9. on execute: uninstalls the incompatible package, installs canonical
   `0.1.415`, restores durable data under the new app UID, provisions the
   device token through the existing staged ADB flow, verifies the staged
   plaintext was consumed, grants normal runtime permissions, and opens the app.

The backup directory is intentionally retained after success.

## Commands

First prove that backup is possible without changing the phone:

```bash
python3 scripts/migrate_owner_signing.py
```

Review the printed backup path and `BACKUP_CONTENTS.txt`. It must not contain
`street_story_secrets.xml`.

Before execute, obtain the same Street Story device token from the already
authorized provisioning source used for the first phone install. Write it to a
temporary local file without printing it and restrict permissions:

```bash
chmod 600 /path/to/street-story-device-token
python3 scripts/migrate_owner_signing.py \
  --execute \
  --token-file /path/to/street-story-device-token
```

When migration reports `migration_complete`, delete the temporary token file.
Do **not** delete the retained migration backup until the owner has completed a
real phone acceptance run.

## Why the secret is provisioned again

`SecretStore` encrypts the device token with an Android Keystore AES key.
Android intentionally removes that key when the old package is uninstalled.
Copying the encrypted preference would therefore create undecryptable state; the
migration excludes it and provisions the same server-side device credential
again after the canonical package is installed.

## Failure rule

If any precondition, backup verification, APK checksum, install, restore, or
provisioning check fails, stop and keep the backup. Never retry by deleting
local backup data, never use `adb install -r` across the old ephemeral signing
identity, and never print the device token into logs or command history.
