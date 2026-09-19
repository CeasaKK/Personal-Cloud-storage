import CloudSyncKit
import CoreData
import Foundation
import Photos

/// Orchestrates one backup pass (TDD §10 sync pipeline, §16):
///
/// 1. scan the library for assets new since the last sync (persistent change token)
/// 2. SHA-256 each new original resource on the phone
/// 3. Merkle-compare this device's hash set with the server's manifest; reconcile
///    (already-stored content is linked without upload — cross-device dedup)
/// 4. upload what the server lacks with resumable tus
/// 5. mark items synced in Core Data so they are never re-hashed or re-scanned
///
/// Runs on open ("sync-on-open" is the primary UX) and opportunistically from
/// BGTaskScheduler; every step is resumable, so being killed mid-pass is harmless.
@MainActor
final class SyncEngine: ObservableObject {
    enum Phase: Equatable {
        case idle
        case scanning
        case hashing(done: Int, total: Int)
        case checking
        case uploading(done: Int, total: Int, fraction: Double)
        case waitingForServer
        case finished
        case failed(String)

        var label: String {
            switch self {
            case .idle: return "Idle"
            case .scanning: return "Looking for new photos…"
            case .hashing(let d, let t): return "Fingerprinting \(d) of \(t)…"
            case .checking: return "Comparing with your server…"
            case .uploading(let d, let t, _): return "Backing up \(d + 1) of \(t)…"
            case .waitingForServer: return "Server is processing uploads…"
            case .finished: return "Up to date"
            case .failed(let m): return m
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var total = 0
    @Published private(set) var synced = 0
    @Published private(set) var pending = 0
    @Published private(set) var failedCount = 0
    @Published private(set) var lastSync: Date?
    @Published private(set) var lastPlanMode: String?

    let settings: AppSettings
    let persistence: Persistence
    private var current: Task<Void, Never>?

    init(settings: AppSettings = .shared, persistence: Persistence = .shared) {
        self.settings = settings
        self.persistence = persistence
        self.lastSync = settings.lastSyncAt
        refreshCounts()
    }

    var isRunning: Bool { current != nil }

    /// Start a pass unless one is running. Returns the task so background handlers can await/cancel it.
    @discardableResult
    func syncNow(reason: String) -> Task<Void, Never> {
        if let current { return current }
        let t = Task { [weak self] in
            guard let self else { return }
            await self.run()
            self.current = nil
        }
        current = t
        return t
    }

    func cancel() {
        current?.cancel()
    }

    func refreshCounts() {
        let ctx = persistence.container.viewContext
        func count(_ statuses: [SyncStatus]) -> Int {
            (try? ctx.count(for: SyncItem.fetch(NSPredicate(format: "statusRaw IN %@", statuses.map(\.rawValue))))) ?? 0
        }
        synced = count([.synced])
        pending = count([.new, .hashed, .needsUpload, .uploading])
        failedCount = count([.failed])
        total = synced + pending + failedCount
    }

    // MARK: the pass

    private func run() async {
        guard let api = settings.makeAPI(), api.isSignedIn else {
            phase = .failed("Sign in to start backing up.")
            return
        }
        let status = PhotoLibrary.authorization
        guard status == .authorized || status == .limited else {
            phase = .failed("Allow photo library access in Settings to back up.")
            return
        }
        do {
            let deviceID = try await ensureDevice(api)
            phase = .scanning
            let ctx = persistence.background()
            let settings = self.settings
            _ = try await ctx.perform { try PhotoLibrary.scan(context: ctx, settings: settings) }
            refreshCounts()

            try await hashNewItems(ctx)
            try Task.checkCancellation()

            phase = .checking
            let hashes: [String] = try await ctx.perform {
                let items = try ctx.fetch(SyncItem.fetch(NSPredicate(format: "sha256 != nil AND statusRaw != %@",
                                                                     SyncStatus.deleted.rawValue)))
                return items.compactMap(\.sha256)
            }
            let plan = try await SyncPlanner().plan(local: MerkleTree(hashes), device: deviceID, remote: api)
            lastPlanMode = plan.mode.rawValue
            let need = Set(plan.needUpload)
            let processing = Set(plan.processing)
            try await ctx.perform {
                let items = try ctx.fetch(SyncItem.fetch(NSPredicate(format: "sha256 != nil AND statusRaw IN %@",
                    [SyncStatus.hashed, .needsUpload, .uploading, .failed, .synced].map(\.rawValue))))
                for item in items {
                    guard let sha = item.sha256 else { continue }
                    if need.contains(sha) {
                        if item.status != .uploading { item.status = .needsUpload }
                    } else if !processing.contains(sha) || item.status == .synced {
                        item.status = .synced
                        item.lastError = nil
                    }
                }
                try ctx.save()
            }
            refreshCounts()

            try await uploadPending(ctx, api: api, deviceID: deviceID)
            settings.lastSyncAt = Date()
            lastSync = settings.lastSyncAt
            refreshCounts()
            phase = pending > 0 ? .waitingForServer : .finished
        } catch is CancellationError {
            phase = .idle
        } catch APIError.unauthorized {
            phase = .failed("Your session expired — please sign in again.")
        } catch {
            phase = .failed(error.localizedDescription)
        }
        refreshCounts()
    }

    private func ensureDevice(_ api: APIClient) async throws -> String {
        let name = DeviceInfo.name
        // re-registering is idempotent and refreshes the server's last-seen time
        let id = try await api.registerDevice(name: name, id: settings.deviceID)
        settings.deviceID = id
        return id
    }

    private func hashNewItems(_ ctx: NSManagedObjectContext) async throws {
        let ids: [NSManagedObjectID] = try await ctx.perform {
            try ctx.fetch(SyncItem.fetch(NSPredicate(format: "statusRaw == %@", SyncStatus.new.rawValue),
                                         sort: [NSSortDescriptor(key: "creationDate", ascending: false)]))
                .map(\.objectID)
        }
        for (i, oid) in ids.enumerated() {
            try Task.checkCancellation()
            phase = .hashing(done: i, total: ids.count)
            let resource: PHAssetResource? = await ctx.perform {
                guard let item = try? ctx.existingObject(with: oid) as? SyncItem else { return nil }
                return PhotoLibrary.resource(for: item)
            }
            do {
                guard let resource else { throw CocoaError(.fileNoSuchFile) }
                let (sha, size) = try await PhotoLibrary.hash(resource)
                try await ctx.perform {
                    guard let item = try ctx.existingObject(with: oid) as? SyncItem else { return }
                    item.sha256 = sha
                    item.size = size
                    item.status = .hashed
                    if i % 50 == 0 { try ctx.save() }
                }
            } catch {
                try await ctx.perform {
                    guard let item = try ctx.existingObject(with: oid) as? SyncItem else { return }
                    // an asset deleted between scan and hash simply disappears
                    item.status = resource == nil ? .deleted : .failed
                    item.lastError = error.localizedDescription
                }
            }
        }
        try await ctx.perform { try ctx.save() }
    }

    private func uploadPending(_ ctx: NSManagedObjectContext, api: APIClient, deviceID: String) async throws {
        let ids: [NSManagedObjectID] = try await ctx.perform {
            try ctx.fetch(SyncItem.fetch(
                NSPredicate(format: "statusRaw IN %@", [SyncStatus.needsUpload, .uploading].map(\.rawValue)),
                sort: [NSSortDescriptor(key: "creationDate", ascending: false)])).map(\.objectID)
        }
        let tus = TusClient(api: api)
        for (i, oid) in ids.enumerated() {
            try Task.checkCancellation()
            phase = .uploading(done: i, total: ids.count, fraction: 0)
            struct Job { let resource: PHAssetResource?; let meta: UploadMetadata; let resume: URL? }
            let job: Job? = try await ctx.perform {
                guard let item = try ctx.existingObject(with: oid) as? SyncItem, let sha = item.sha256 else { return nil }
                item.status = .uploading
                try ctx.save()
                return Job(resource: PhotoLibrary.resource(for: item),
                           meta: UploadMetadata(filename: item.resourceName, sha256: sha,
                                                mimeType: PhotoLibrary.mimeType(for: item.resourceName),
                                                deviceID: deviceID, createdAt: item.creationDate,
                                                localID: item.localIdentifier),
                           resume: item.uploadURL.flatMap(URL.init(string:)))
            }
            guard let job, let resource = job.resource else { continue }
            var file: URL?
            do {
                file = try await PhotoLibrary.export(resource)
                // the bytes must still be the ones we hashed (an edit may have replaced them)
                let (sha, _) = try StreamingSHA256.hash(file: file!)
                guard sha == job.meta.sha256 else { throw TusError.checksumMismatch }
                let total = ids.count
                let uploadURL = try await tus.upload(file: file!, metadata: job.meta, resumeFrom: job.resume) { f in
                    Task { @MainActor in self.phase = .uploading(done: i, total: total, fraction: f) }
                }
                try await ctx.perform {
                    guard let item = try ctx.existingObject(with: oid) as? SyncItem else { return }
                    item.uploadURL = uploadURL.absoluteString
                    item.status = .synced  // server verified the SHA-256 on completion
                    item.lastError = nil
                    try ctx.save()
                }
            } catch TusError.checksumMismatch {
                try await markFailed(ctx, oid, rehash: true, message: "File changed; will re-read it.")
            } catch APIError.unauthorized {
                throw APIError.unauthorized
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                try await markFailed(ctx, oid, rehash: false, message: error.localizedDescription)
            }
            if let file { try? FileManager.default.removeItem(at: file) }
            refreshCounts()
        }
    }

    private func markFailed(_ ctx: NSManagedObjectContext, _ oid: NSManagedObjectID, rehash: Bool, message: String) async throws {
        try await ctx.perform {
            guard let item = try ctx.existingObject(with: oid) as? SyncItem else { return }
            item.attempts += 1
            item.lastError = message
            item.status = rehash ? .new : (item.attempts >= 5 ? .failed : .needsUpload)
            if rehash {
                item.sha256 = nil
                item.uploadURL = nil
            }
            try ctx.save()
        }
    }

    /// Recent failures for the status screen.
    func recentErrors(limit: Int = 5) -> [(String, String)] {
        let r = SyncItem.fetch(NSPredicate(format: "lastError != nil AND statusRaw != %@", SyncStatus.synced.rawValue),
                               limit: limit, sort: [NSSortDescriptor(key: "updatedAt", ascending: false)])
        return ((try? persistence.container.viewContext.fetch(r)) ?? []).map { ($0.resourceName, $0.lastError ?? "") }
    }
}
