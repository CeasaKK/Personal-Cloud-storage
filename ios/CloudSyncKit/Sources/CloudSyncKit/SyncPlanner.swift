import Foundation

/// The server side of the sync protocol, abstracted so the planner is testable
/// (the real implementation is `APIClient`).
public protocol SyncRemote {
    func syncRoot(device: String) async throws -> (root: Data, count: Int)
    func syncNodes(device: String, paths: [String]) async throws -> [String: [Data]]
    func syncBuckets(device: String, paths: [String]) async throws -> [String: [String]]
    func reconcile(device: String, add: [String], remove: [String]) async throws -> ReconcileResult
}

public struct SyncPlan: Equatable, Sendable {
    public var needUpload: [String]
    public var processing: [String]
    public var linked: Int
    public var removed: Int
    public var mode: Mode
    public var bytesExchangedEstimate: Int

    public enum Mode: String, Sendable { case upToDate, merkle, bulk }
}

/// Client half of Merkle reconciliation (TDD §7).
///
/// 1. compare roots (one tiny request; the common "nothing new" case ends here)
/// 2. if the difference is large (first sync, or > `bulkThreshold` of the library),
///    skip the walk and send the whole local hash list — cheaper than three levels of
///    16 child hashes per changed leaf (see docs/benchmarks.md §6)
/// 3. otherwise descend level by level into differing children, fetch differing
///    buckets, and send exactly the diff to `reconcile`.
public struct SyncPlanner {
    public var bulkThreshold: Double

    public init(bulkThreshold: Double = 0.05) {
        self.bulkThreshold = bulkThreshold
    }

    public func plan(local: MerkleTree, device: String, remote: SyncRemote) async throws -> SyncPlan {
        let (serverRoot, serverCount) = try await remote.syncRoot(device: device)
        if serverRoot == local.root {
            return SyncPlan(needUpload: [], processing: [], linked: 0, removed: 0, mode: .upToDate,
                            bytesExchangedEstimate: 100)
        }
        let localCount = local.count
        let delta = abs(localCount - serverCount)
        if serverCount == 0 || Double(delta) > bulkThreshold * Double(max(localCount, 1)) {
            let all = local.allHashes
            let r = try await remote.reconcile(device: device, add: all, remove: [])
            return SyncPlan(needUpload: r.needUpload, processing: r.processing, linked: r.linked, removed: 0,
                            mode: .bulk, bytesExchangedEstimate: all.count * 67)
        }
        var frontier = [""]
        var bytes = 100
        for _ in 0..<MerkleTree.depth {
            let remoteKids = try await remote.syncNodes(device: device, paths: frontier)
            bytes += frontier.count * (16 * 67 + 10)
            var next: [String] = []
            for p in frontier {
                guard let kids = remoteKids[p] else { continue }
                for (i, digit) in MerkleTree.hexDigits.enumerated() where local.node(p + String(digit)) != kids[i] {
                    next.append(p + String(digit))
                }
            }
            frontier = next
            if frontier.isEmpty { break }
        }
        var missing: [String] = []
        var extra: [String] = []
        if !frontier.isEmpty {
            let remoteBuckets = try await remote.syncBuckets(device: device, paths: frontier)
            for p in frontier {
                let mine = Set(local.bucket(p))
                let theirs = Set(remoteBuckets[p] ?? [])
                bytes += theirs.count * 67
                missing += mine.subtracting(theirs)
                extra += theirs.subtracting(mine)
            }
        }
        let r = try await remote.reconcile(device: device, add: missing.sorted(), remove: extra.sorted())
        return SyncPlan(needUpload: r.needUpload, processing: r.processing, linked: r.linked, removed: r.removed,
                        mode: .merkle, bytesExchangedEstimate: bytes + (missing.count + extra.count) * 67)
    }
}
