import CloudSyncKit
import CoreData
import Foundation
import Photos
import UniformTypeIdentifiers

/// PhotoKit access: incremental scanning, original-resource hashing and export (TDD §16).
///
/// * Scans use `fetchPersistentChanges(since:)` so a sync after the first one only
///   looks at assets added/removed since the last token — not the whole library.
/// * Hashing and upload always use the **original** resource bytes via
///   `PHAssetResourceManager`, never an exported/transcoded rendition, so the
///   SHA-256 is stable (a HEIC stays HEIC) and matches what the server stores.
/// * `isNetworkAccessAllowed` lets iCloud-optimised originals download on demand.
enum PhotoLibrary {
    static func requestAccess() async -> PHAuthorizationStatus {
        await PHPhotoLibrary.requestAuthorization(for: .readWrite)
    }

    static var authorization: PHAuthorizationStatus {
        PHPhotoLibrary.authorizationStatus(for: .readWrite)
    }

    /// Original resources worth backing up for an asset (edits/adjustment data skipped).
    static func originalResources(for asset: PHAsset) -> [PHAssetResource] {
        let all = PHAssetResource.assetResources(for: asset)
        let wanted: Set<PHAssetResourceType> = [.photo, .video, .pairedVideo, .alternatePhoto, .audio]
        return all.filter { wanted.contains($0.type) }
    }

    static func resourceKey(_ localIdentifier: String, _ type: PHAssetResourceType) -> String {
        "\(localIdentifier)|\(type.rawValue)"
    }

    // MARK: scanning

    struct ScanResult {
        var discovered = 0
        var deleted = 0
        var fullScan = false
    }

    static func scan(context: NSManagedObjectContext, settings: AppSettings) throws -> ScanResult {
        var result = ScanResult()
        let library = PHPhotoLibrary.shared()
        var upsertIDs: [String] = []
        var deletedIDs: Set<String> = []

        if let tokenData = settings.changeToken,
           let token = try? NSKeyedUnarchiver.unarchivedObject(ofClass: PHPersistentChangeToken.self, from: tokenData),
           let changes = try? library.fetchPersistentChanges(since: token) {
            for change in changes {
                guard let details = try? change.changeDetails(for: .asset) else { continue }
                upsertIDs += details.insertedLocalIdentifiers
                upsertIDs += details.updatedLocalIdentifiers
                deletedIDs.formUnion(details.deletedLocalIdentifiers)
            }
        } else {
            // first run, or the token expired: full scan and reconcile deletions
            result.fullScan = true
            let fetched = PHAsset.fetchAssets(with: fetchOptions(settings))
            var present = Set<String>()
            fetched.enumerateObjects { asset, _, _ in
                present.insert(asset.localIdentifier)
                upsertIDs.append(asset.localIdentifier)
            }
            let known = try context.fetch(SyncItem.fetch(NSPredicate(format: "statusRaw != %@", SyncStatus.deleted.rawValue)))
            deletedIDs = Set(known.map(\.localIdentifier)).subtracting(present)
        }

        // upsert in batches (fetchAssets by identifiers is O(n) per call)
        let unique = Array(Set(upsertIDs))
        for start in stride(from: 0, to: unique.count, by: 500) {
            let ids = Array(unique[start..<min(start + 500, unique.count)])
            let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: fetchOptions(settings))
            let existing = try context.fetch(SyncItem.fetch(NSPredicate(format: "localIdentifier IN %@", ids)))
            var byKey = Dictionary(existing.map { ($0.resourceKey, $0) }, uniquingKeysWith: { a, _ in a })
            assets.enumerateObjects { asset, _, _ in
                for res in originalResources(for: asset) {
                    let key = resourceKey(asset.localIdentifier, res.type)
                    if let item = byKey[key] {
                        if item.status == .deleted { item.status = .new }
                        continue
                    }
                    let item = SyncItem(context: context)
                    item.resourceKey = key
                    item.localIdentifier = asset.localIdentifier
                    item.resourceType = Int32(res.type.rawValue)
                    item.resourceName = res.originalFilename
                    item.mediaType = Int16(asset.mediaType.rawValue)
                    item.creationDate = asset.creationDate
                    item.status = .new
                    byKey[key] = item
                    result.discovered += 1
                }
            }
        }
        if !deletedIDs.isEmpty {
            let gone = try context.fetch(SyncItem.fetch(NSPredicate(format: "localIdentifier IN %@", Array(deletedIDs))))
            for item in gone where item.status != .deleted {
                item.status = .deleted
                result.deleted += 1
            }
        }
        try context.save()
        settings.changeToken = try? NSKeyedArchiver.archivedData(withRootObject: library.currentChangeToken,
                                                                 requiringSecureCoding: true)
        return result
    }

    static func fetchOptions(_ settings: AppSettings) -> PHFetchOptions {
        let o = PHFetchOptions()
        o.includeHiddenAssets = false
        o.predicate = settings.includeVideos
            ? NSPredicate(format: "mediaType == %d OR mediaType == %d", PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
            : NSPredicate(format: "mediaType == %d", PHAssetMediaType.image.rawValue)
        o.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        return o
    }

    // MARK: resource access

    static func resource(for item: SyncItem) -> PHAssetResource? {
        guard let asset = PHAsset.fetchAssets(withLocalIdentifiers: [item.localIdentifier], options: nil).firstObject
        else { return nil }
        return PHAssetResource.assetResources(for: asset).first { $0.type.rawValue == Int(item.resourceType) }
    }

    /// Stream the original bytes through SHA-256 without holding them in memory.
    static func hash(_ resource: PHAssetResource) async throws -> (sha256: String, size: Int64) {
        final class Box: @unchecked Sendable { var h = StreamingSHA256() }
        let box = Box()
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        return try await withCheckedThrowingContinuation { cont in
            PHAssetResourceManager.default().requestData(for: resource, options: options) { data in
                box.h.update(data)
            } completionHandler: { error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: (box.h.finalizeHex(), box.h.byteCount)) }
            }
        }
    }

    /// Write the original bytes to a temporary file for upload.
    static func export(_ resource: PHAssetResource) async throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("uploads", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let ext = (resource.originalFilename as NSString).pathExtension
        let url = dir.appendingPathComponent(UUID().uuidString).appendingPathExtension(ext)
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        try await PHAssetResourceManager.default().writeData(for: resource, toFile: url, options: options)
        return url
    }

    static func mimeType(for filename: String) -> String? {
        UTType(filenameExtension: (filename as NSString).pathExtension)?.preferredMIMEType
    }
}
