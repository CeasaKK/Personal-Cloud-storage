import CoreData
import Foundation

/// Per-device sync state (TDD §16). One `SyncItem` per original asset *resource*
/// (a Live Photo is two: the still and its paired video). The model is built in
/// code, so there is no .xcdatamodeld to keep in step.
enum SyncStatus: String {
    case new            // discovered, not hashed yet
    case hashed         // SHA-256 known, not yet compared with the server
    case needsUpload    // server says it does not have these bytes
    case uploading
    case synced         // server has it (uploaded, or deduplicated against another device)
    case failed         // last attempt failed; retried on the next sync
    case deleted        // gone from the library; removed from this device's server manifest
}

@objc(SyncItem)
final class SyncItem: NSManagedObject {
    @NSManaged var resourceKey: String      // "<localIdentifier>|<resource type>"
    @NSManaged var localIdentifier: String
    @NSManaged var resourceType: Int32
    @NSManaged var resourceName: String
    @NSManaged var mediaType: Int16         // PHAssetMediaType raw value
    @NSManaged var creationDate: Date?
    @NSManaged var sha256: String?
    @NSManaged var size: Int64
    @NSManaged var statusRaw: String
    @NSManaged var lastError: String?
    @NSManaged var uploadURL: String?       // tus resource, for resume
    @NSManaged var attempts: Int16
    @NSManaged var updatedAt: Date

    var status: SyncStatus {
        get { SyncStatus(rawValue: statusRaw) ?? .new }
        set {
            statusRaw = newValue.rawValue
            updatedAt = Date()
        }
    }

    static func fetch(_ predicate: NSPredicate? = nil, limit: Int = 0,
                      sort: [NSSortDescriptor] = []) -> NSFetchRequest<SyncItem> {
        let r = NSFetchRequest<SyncItem>(entityName: "SyncItem")
        r.predicate = predicate
        r.fetchLimit = limit
        r.sortDescriptors = sort
        return r
    }
}

final class Persistence {
    static let shared = Persistence()
    let container: NSPersistentContainer

    init(inMemory: Bool = false) {
        container = NSPersistentContainer(name: "CloudstoreSync", managedObjectModel: Self.model)
        if inMemory {
            container.persistentStoreDescriptions.first?.url = URL(fileURLWithPath: "/dev/null")
        }
        container.loadPersistentStores { _, error in
            if let error { fatalError("Core Data store failed to load: \(error)") }
        }
        container.viewContext.automaticallyMergesChangesFromParent = true
        container.viewContext.mergePolicy = NSMergeByPropertyObjectTrumpMergePolicy
    }

    func background() -> NSManagedObjectContext {
        let c = container.newBackgroundContext()
        c.mergePolicy = NSMergeByPropertyObjectTrumpMergePolicy
        return c
    }

    static let model: NSManagedObjectModel = {
        let entity = NSEntityDescription()
        entity.name = "SyncItem"
        entity.managedObjectClassName = NSStringFromClass(SyncItem.self)
        func attr(_ name: String, _ type: NSAttributeType, optional: Bool = false, def: Any? = nil) -> NSAttributeDescription {
            let a = NSAttributeDescription()
            a.name = name
            a.attributeType = type
            a.isOptional = optional
            a.defaultValue = def
            return a
        }
        entity.properties = [
            attr("resourceKey", .stringAttributeType),
            attr("localIdentifier", .stringAttributeType),
            attr("resourceType", .integer32AttributeType, def: 0),
            attr("resourceName", .stringAttributeType, def: ""),
            attr("mediaType", .integer16AttributeType, def: 0),
            attr("creationDate", .dateAttributeType, optional: true),
            attr("sha256", .stringAttributeType, optional: true),
            attr("size", .integer64AttributeType, def: 0),
            attr("statusRaw", .stringAttributeType, def: SyncStatus.new.rawValue),
            attr("lastError", .stringAttributeType, optional: true),
            attr("uploadURL", .stringAttributeType, optional: true),
            attr("attempts", .integer16AttributeType, def: 0),
            attr("updatedAt", .dateAttributeType, def: Date(timeIntervalSince1970: 0)),
        ]
        entity.uniquenessConstraints = [["resourceKey"]]
        let byStatus = NSFetchIndexDescription(name: "byStatus", elements: [
            NSFetchIndexElementDescription(property: entity.propertiesByName["statusRaw"]!, collationType: .binary),
        ])
        let byLocal = NSFetchIndexDescription(name: "byLocalIdentifier", elements: [
            NSFetchIndexElementDescription(property: entity.propertiesByName["localIdentifier"]!, collationType: .binary),
        ])
        entity.indexes = [byStatus, byLocal]
        let model = NSManagedObjectModel()
        model.entities = [entity]
        return model
    }()
}
