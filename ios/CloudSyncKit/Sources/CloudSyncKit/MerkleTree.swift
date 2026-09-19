import CryptoKit
import Foundation

/// Fixed-depth 16-ary Merkle trie over SHA-256 content hashes (TDD §7).
///
/// Must stay bit-identical to `server/cloudstore/sync/merkle.py`:
///  * leaf (path of `depth` hex digits) = SHA-256(sorted raw 32-byte member hashes)
///  * internal node = SHA-256(concatenation of its 16 children)
///  * an empty subtree = 32 zero bytes
/// Verified against `spec/merkle_vectors.json` in the package tests.
public final class MerkleTree {
    public static let depth = 3
    public static let empty = Data(count: 32)
    static let hexDigits = Array("0123456789abcdef")

    private var buckets: [String: Set<Data>] = [:]
    private var cache: [String: Data] = [:]
    private let lock = NSLock()

    public init<S: Sequence>(_ hashes: S = [String]()) where S.Element == String {
        for h in hashes { add(h) }
    }

    public var count: Int {
        lock.lock(); defer { lock.unlock() }
        return buckets.values.reduce(0) { $0 + $1.count }
    }

    @discardableResult
    public func add(_ hex: String) -> Bool {
        let h = hex.lowercased()
        guard let raw = Data(hexString: h), raw.count == 32 else { return false }
        let key = String(h.prefix(Self.depth))
        lock.lock(); defer { lock.unlock() }
        let inserted = buckets[key, default: []].insert(raw).inserted
        if inserted { invalidate(key) }
        return inserted
    }

    @discardableResult
    public func remove(_ hex: String) -> Bool {
        let h = hex.lowercased()
        guard let raw = Data(hexString: h) else { return false }
        let key = String(h.prefix(Self.depth))
        lock.lock(); defer { lock.unlock() }
        guard buckets[key]?.remove(raw) != nil else { return false }
        if buckets[key]?.isEmpty == true { buckets[key] = nil }
        invalidate(key)
        return true
    }

    public func contains(_ hex: String) -> Bool {
        let h = hex.lowercased()
        guard let raw = Data(hexString: h) else { return false }
        lock.lock(); defer { lock.unlock() }
        return buckets[String(h.prefix(Self.depth))]?.contains(raw) ?? false
    }

    private func invalidate(_ path: String) {
        for i in 0...Self.depth { cache[String(path.prefix(i))] = nil }
    }

    public func node(_ path: String) -> Data {
        lock.lock(); defer { lock.unlock() }
        return nodeLocked(path)
    }

    private func nodeLocked(_ path: String) -> Data {
        if let c = cache[path] { return c }
        let value: Data
        if path.count == Self.depth {
            if let members = buckets[path], !members.isEmpty {
                var hasher = SHA256()
                for m in members.sorted(by: { $0.lexicographicallyPrecedes($1) }) { hasher.update(data: m) }
                value = Data(hasher.finalize())
            } else {
                value = Self.empty
            }
        } else {
            let kids = Self.hexDigits.map { nodeLocked(path + String($0)) }
            if kids.allSatisfy({ $0 == Self.empty }) {
                value = Self.empty
            } else {
                var hasher = SHA256()
                for k in kids { hasher.update(data: k) }
                value = Data(hasher.finalize())
            }
        }
        cache[path] = value
        return value
    }

    public var root: Data { node("") }

    public func children(_ path: String) -> [Data] {
        Self.hexDigits.map { node(path + String($0)) }
    }

    public func bucket(_ path: String) -> [String] {
        lock.lock(); defer { lock.unlock() }
        return (buckets[path] ?? []).map(\.hexString).sorted()
    }

    public var allHashes: [String] {
        lock.lock(); defer { lock.unlock() }
        return buckets.values.flatMap { $0.map(\.hexString) }
    }
}

extension Data {
    public init?(hexString: String) {
        let chars = Array(hexString.utf8)
        guard chars.count % 2 == 0 else { return nil }
        var out = Data(capacity: chars.count / 2)
        var i = 0
        while i < chars.count {
            guard let hi = Self.nibble(chars[i]), let lo = Self.nibble(chars[i + 1]) else { return nil }
            out.append(hi << 4 | lo)
            i += 2
        }
        self = out
    }

    private static func nibble(_ c: UInt8) -> UInt8? {
        switch c {
        case 48...57: return c - 48
        case 97...102: return c - 87
        case 65...70: return c - 55
        default: return nil
        }
    }

    public var hexString: String {
        let digits = Array("0123456789abcdef".utf8)
        var out = [UInt8]()
        out.reserveCapacity(count * 2)
        for b in self {
            out.append(digits[Int(b >> 4)])
            out.append(digits[Int(b & 0x0f)])
        }
        return String(decoding: out, as: UTF8.self)
    }
}
