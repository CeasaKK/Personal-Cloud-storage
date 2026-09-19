import CryptoKit
import Foundation

/// Incremental SHA-256 so multi-gigabyte videos are hashed in constant memory.
public struct StreamingSHA256 {
    private var hasher = SHA256()
    public private(set) var byteCount: Int64 = 0

    public init() {}

    public mutating func update(_ data: Data) {
        hasher.update(data: data)
        byteCount += Int64(data.count)
    }

    public func finalizeHex() -> String {
        Data(hasher.finalize()).hexString
    }

    public static func hash(file: URL, chunkSize: Int = 4 * 1024 * 1024) throws -> (sha256: String, size: Int64) {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var h = StreamingSHA256()
        while true {
            let chunk = handle.readData(ofLength: chunkSize)
            if chunk.isEmpty { break }
            h.update(chunk)
        }
        return (h.finalizeHex(), h.byteCount)
    }
}
