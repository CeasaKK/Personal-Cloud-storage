# Cloudstore for iOS

Backs up the camera roll to your Cloudstore server (TDD §16).

```
ios/
  CloudSyncKit/     Swift package: Merkle tree, API + tus clients, sync planner (tested with `swift test`)
  CloudstoreApp/    SwiftUI app + share extension, XcodeGen spec (project.yml)
```

## Build and run

1. Install Xcode 15+ and XcodeGen: `brew install xcodegen`
2. `cd ios/CloudstoreApp && xcodegen` → opens as `Cloudstore.xcodeproj`
3. In Xcode, set your **Team** for both targets (Signing & Capabilities). The App Group
   `group.app.cloudstore` and the keychain group are declared in the generated entitlements;
   if your team already uses those IDs, change them in `project.yml` and `AppSettings.swift`.
4. Run on your iPhone. Sign in with your server's Tailscale name, for example
   `minipc.tail1234.ts.net`, and the password you set with `cloudstore set-password`.

Tailscale must be connected on the phone. The server is exposed with `tailscale serve`,
which gives it an HTTPS certificate for its MagicDNS name, so App Transport Security
needs no exceptions.

## How backup works

| Step | Code |
|---|---|
| Incremental library scan with `PHPhotoLibrary.fetchPersistentChanges(since:)`; the first run does a full scan | `PhotoLibrary.scan` |
| SHA-256 of each **original** resource, streamed from `PHAssetResourceManager`, iCloud originals downloaded on demand | `PhotoLibrary.hash` |
| Merkle comparison with the server, or a bulk list on first sync / large changes | `CloudSyncKit.SyncPlanner` |
| Resumable tus upload; the server re-verifies the SHA-256 | `CloudSyncKit.TusClient` |
| Per-item state in Core Data, so synced items are never re-hashed | `Persistence`, `SyncEngine` |

**Sync-on-open is the primary path.** iOS doesn't allow always-on background backup.
The app registers a `BGAppRefreshTask` for a quick check and a `BGProcessingTask` for
bulk uploads while charging. iOS alone decides when these run, and the UI says so.
The **share extension** ("Share → Cloudstore") is the manual path for immediate uploads.

Live Photos upload as two files, the still and the paired video. Edits aren't synced in
v1; the untouched original is what gets backed up.

## Tests

```bash
cd ios/CloudSyncKit && swift test
```

`merkleMatchesServerVectors` checks that the Swift Merkle tree is bit-identical to the
Python server, using `spec/merkle_vectors.json`. An opt-in end-to-end test runs the real
client against a live server:

```bash
CLOUDSTORE_E2E_URL=http://127.0.0.1:8000 CLOUDSTORE_E2E_PASSWORD=demo-password-123 swift test
```
