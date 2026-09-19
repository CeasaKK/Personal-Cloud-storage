// swift-tools-version:5.9
// Platform-independent sync logic for the Cloudstore iOS app (TDD §16):
// Merkle tree (bit-identical to the server), API client, tus client, sync planner.
// Unit-tested on macOS with `swift test`; linked by the app and share extension.
import PackageDescription

let package = Package(
    name: "CloudSyncKit",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "CloudSyncKit", targets: ["CloudSyncKit"])],
    targets: [
        .target(name: "CloudSyncKit"),
        .testTarget(
            name: "CloudSyncKitTests",
            dependencies: ["CloudSyncKit"],
            resources: [.copy("Resources/merkle_vectors.json")]
        ),
    ]
)
