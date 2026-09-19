import CryptoKit
import Foundation
import Testing
@testable import CloudSyncKit

// MARK: - Merkle: bit-identical to the Python server

struct Vectors: Decodable {
    struct Case: Decodable {
        let name: String
        let hashes: [String]
        let root: String
        let nodes: [String: String]
        let children_of_root: [String]
        let bucket: [String: [String]]
    }
    let depth: Int
    let empty: String
    let cases: [Case]
}

func loadVectors() throws -> Vectors {
    let url = try #require(Bundle.module.url(forResource: "merkle_vectors", withExtension: "json"))
    return try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: url))
}

@Test func merkleMatchesServerVectors() throws {
    let v = try loadVectors()
    #expect(v.depth == MerkleTree.depth)
    #expect(v.empty == MerkleTree.empty.hexString)
    for c in v.cases {
        let t = MerkleTree(c.hashes)
        #expect(t.root.hexString == c.root, "root mismatch for \(c.name)")
        for (path, want) in c.nodes {
            #expect(t.node(path).hexString == want, "node \(path) mismatch for \(c.name)")
        }
        #expect(t.children("").map(\.hexString) == c.children_of_root)
        for (path, members) in c.bucket {
            #expect(t.bucket(path) == members)
        }
    }
}

@Test func merkleIncrementalEqualsRebuild() {
    let hashes = (0..<200).map { sha("x\($0)") }
    let t = MerkleTree(hashes)
    _ = t.root
    t.add(sha("new"))
    t.remove(hashes[3])
    let fresh = MerkleTree(hashes.filter { $0 != hashes[3] } + [sha("new")])
    #expect(t.root == fresh.root)
    #expect(t.count == 200)
}

func sha(_ s: String) -> String { Data(SHA256.hash(data: Data(s.utf8))).hexString }

// MARK: - Sync planner against an in-memory server

final class FakeServer: SyncRemote, @unchecked Sendable {
    var tree: MerkleTree
    var stored: Set<String>
    var calls: [String] = []

    init(device items: [String], stored: Set<String>) {
        tree = MerkleTree(items)
        self.stored = stored
    }

    func syncRoot(device: String) async throws -> (root: Data, count: Int) {
        calls.append("root")
        return (tree.root, tree.count)
    }

    func syncNodes(device: String, paths: [String]) async throws -> [String: [Data]] {
        calls.append("nodes")
        return Dictionary(uniqueKeysWithValues: paths.map { ($0, tree.children($0)) })
    }

    func syncBuckets(device: String, paths: [String]) async throws -> [String: [String]] {
        calls.append("buckets")
        return Dictionary(uniqueKeysWithValues: paths.map { ($0, tree.bucket($0)) })
    }

    func reconcile(device: String, add: [String], remove: [String]) async throws -> ReconcileResult {
        calls.append("reconcile(\(add.count),\(remove.count))")
        let known = add.filter { stored.contains($0) }
        known.forEach { tree.add($0) }
        remove.forEach { tree.remove($0) }
        return ReconcileResult(linked: known.count, removed: remove.count,
                               needUpload: add.filter { !stored.contains($0) }, processing: [])
    }
}

@Test func plannerUpToDateIsOneCall() async throws {
    let items = (0..<500).map { sha("p\($0)") }
    let server = FakeServer(device: items, stored: Set(items))
    let plan = try await SyncPlanner().plan(local: MerkleTree(items), device: "d", remote: server)
    #expect(plan.mode == .upToDate)
    #expect(server.calls == ["root"])
}

@Test func plannerMerkleWalkFindsExactDiff() async throws {
    let items = (0..<2000).map { sha("p\($0)") }
    let newOnPhone = (0..<5).map { sha("new\($0)") }
    let deletedOnPhone = Array(items.prefix(3))
    let server = FakeServer(device: items, stored: Set(items))
    let local = MerkleTree(items.dropFirst(3) + newOnPhone)
    let plan = try await SyncPlanner().plan(local: local, device: "d", remote: server)
    #expect(plan.mode == .merkle)
    #expect(Set(plan.needUpload) == Set(newOnPhone))
    #expect(plan.removed == 3)
    #expect(server.calls == ["root", "nodes", "nodes", "nodes", "buckets", "reconcile(5,3)"])
    // after uploads land, the trees converge
    newOnPhone.forEach { server.tree.add($0) }
    #expect(server.tree.root == local.root)
    _ = deletedOnPhone
}

@Test func plannerFirstSyncUsesBulk() async throws {
    let items = (0..<300).map { sha("p\($0)") }
    let alreadyStored = Set(items.prefix(100))  // e.g. uploaded earlier from another device
    let server = FakeServer(device: [], stored: alreadyStored)
    let plan = try await SyncPlanner().plan(local: MerkleTree(items), device: "d", remote: server)
    #expect(plan.mode == .bulk)
    #expect(plan.linked == 100)
    #expect(plan.needUpload.count == 200)
}

// MARK: - tus metadata + hashing

@Test func tusMetadataHeaderIsBase64Pairs() {
    let m = UploadMetadata(filename: "IMG_0001.HEIC", sha256: "ab", deviceID: "dev",
                           createdAt: Date(timeIntervalSince1970: 1_700_000_000))
    let pairs = Dictionary(uniqueKeysWithValues: m.header.split(separator: ",").map { p -> (String, String) in
        let kv = p.split(separator: " ")
        return (String(kv[0]), String(decoding: Data(base64Encoded: String(kv[1]))!, as: UTF8.self))
    })
    #expect(pairs["filename"] == "IMG_0001.HEIC")
    #expect(pairs["sha256"] == "ab")
    #expect(pairs["device_id"] == "dev")
    #expect(pairs["created_at"] == "1700000000.000")
}

@Test func streamingHashMatchesOneShot() throws {
    let data = Data((0..<3_000_000).map { UInt8($0 % 251) })
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try data.write(to: url)
    defer { try? FileManager.default.removeItem(at: url) }
    let (h, size) = try StreamingSHA256.hash(file: url, chunkSize: 100_000)
    #expect(h == Data(SHA256.hash(data: data)).hexString)
    #expect(size == Int64(data.count))
}

@Test func hexRoundTrip() {
    let d = Data([0x00, 0x0f, 0xa0, 0xff])
    #expect(d.hexString == "000fa0ff")
    #expect(Data(hexString: "000FA0ff") == d)
    #expect(Data(hexString: "abc") == nil)
}
