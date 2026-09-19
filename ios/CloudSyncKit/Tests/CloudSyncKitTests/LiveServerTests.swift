import CryptoKit
import Foundation
import Testing
@testable import CloudSyncKit

/// End-to-end against a running server (opt-in):
///   CLOUDSTORE_E2E_URL=http://127.0.0.1:8000 CLOUDSTORE_E2E_PASSWORD=... swift test
/// Exercises the exact client code the app ships: login → register device →
/// Merkle plan → tus upload → server ingest → converged roots.
@Test(.enabled(if: ProcessInfo.processInfo.environment["CLOUDSTORE_E2E_URL"] != nil))
func liveServerRoundTrip() async throws {
    let env = ProcessInfo.processInfo.environment
    let api = APIClient(baseURL: URL(string: env["CLOUDSTORE_E2E_URL"]!)!, tokens: InMemoryTokenStore())
    try await api.login(password: env["CLOUDSTORE_E2E_PASSWORD"] ?? "demo-password-123")
    let device = try await api.registerDevice(name: "swift-e2e", id: "swift-e2e-\(UUID().uuidString.prefix(8))")

    // a fresh "photo" (random bytes named .txt so it takes the non-media path cheaply)
    let payload = Data((0..<200_000).map { _ in UInt8.random(in: 0...255) })
    let file = FileManager.default.temporaryDirectory.appendingPathComponent("e2e-\(UUID().uuidString).txt")
    try payload.write(to: file)
    defer { try? FileManager.default.removeItem(at: file) }
    let (sha, _) = try StreamingSHA256.hash(file: file)

    let local = MerkleTree([sha])
    let plan = try await SyncPlanner().plan(local: local, device: device, remote: api)
    #expect(plan.needUpload == [sha])

    let tus = TusClient(api: api)
    _ = try await tus.upload(file: file, metadata: UploadMetadata(filename: file.lastPathComponent, sha256: sha,
                                                                  deviceID: device, createdAt: Date()))
    var status = APIClient.RemoteStatus.unknown
    for _ in 0..<60 {
        status = try await api.status(sha256: sha)
        if status == .stored { break }
        try await Task.sleep(nanoseconds: 500_000_000)
    }
    #expect(status == .stored)
    let (root, count) = try await api.syncRoot(device: device)
    #expect(count == 1)
    #expect(root == local.root)
    let again = try await SyncPlanner().plan(local: local, device: device, remote: api)
    #expect(again.mode == .upToDate)
}
