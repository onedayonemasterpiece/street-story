# Android release signing

Street Story owner-installable APKs use one canonical signing identity. Signing
continuity is a product invariant: Android will not update an installed package
when the new APK has the same application id but a different signing identity.

Canonical certificate SHA-256:

`fdff25f36f5504174f13639d741a440ef76b5be0b97039ad41123ad36d3b0e94`

The private PKCS#12 bundle is not stored in Git. The canonical backup is retained
on DevCoveer at `~/.config/street-story/signing/owner-signing.p12` with private
filesystem permissions. GitHub Actions receives the same bundle only through
repository secrets `STREET_STORY_SIGNING_KEYSTORE_B64` and
`STREET_STORY_SIGNING_STORE_PASSWORD`.

Push/workflow-dispatch Android builds restore that bundle before Gradle. Gradle
uses it for the debug owner build so ADB provisioning remains available. Before
an artifact can proceed toward the GitHub Release job, CI verifies the APK
certificate digest against the canonical fingerprint above. A missing secret,
missing bundle, wrong password or certificate mismatch must fail closed.

Pull-request checks may use an ephemeral debug key because they never publish an
owner release. Never use `actions/cache` as storage for an application signing
key and never silently generate a replacement key in a release job.

## 2026-10-01 signing incident

`android-v407` was signed by an ephemeral Android Debug key whose certificate
SHA-256 was `89332a9e116ad47f2acafdd7c92c553681815e037e9df24349fd6d27b13c3908`.
`android-v410` was later signed by another ephemeral key
(`b4552e838d6517a04ed2611016942aba3deeefc095401040ba30fd9733b25347`).
The attempted `actions/cache` key contained no recoverable cache entry when the
incident was investigated, so the v407 private key cannot be used for a normal
in-place upgrade.

Existing v407 owner devices therefore require one controlled migration to the
canonical signing identity. Preserve local non-secret app state, reinstall the
canonical build, and provision the device token again because Android Keystore
material is intentionally destroyed by uninstall. All later owner releases must
remain on the canonical signing identity and update in place.
