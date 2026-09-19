import Foundation

public struct Tokens: Codable, Sendable, Equatable {
    public var accessToken: String
    public var refreshToken: String

    public init(accessToken: String, refreshToken: String) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
    }
}

public struct ReconcileResult: Codable, Sendable, Equatable {
    public var linked: Int
    public var removed: Int
    public var needUpload: [String]
    public var processing: [String]

    public init(linked: Int, removed: Int, needUpload: [String], processing: [String]) {
        self.linked = linked
        self.removed = removed
        self.needUpload = needUpload
        self.processing = processing
    }

    enum CodingKeys: String, CodingKey {
        case linked, removed, processing
        case needUpload = "need_upload"
    }
}

public enum APIError: Error, LocalizedError, Equatable {
    case unauthorized
    case http(Int, String)
    case badURL
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .unauthorized: return "Signed out — please sign in again."
        case .http(let code, let msg): return "Server error \(code): \(msg)"
        case .badURL: return "The server address is not a valid URL."
        case .invalidResponse: return "Unexpected response from the server."
        }
    }
}

/// Where tokens live. The app uses the Keychain (shared with the share extension via
/// an access group); tests use `InMemoryTokenStore`.
public protocol TokenStore: AnyObject {
    func load() -> Tokens?
    func save(_ tokens: Tokens?)
}

public final class InMemoryTokenStore: TokenStore {
    private var tokens: Tokens?
    public init(_ tokens: Tokens? = nil) { self.tokens = tokens }
    public func load() -> Tokens? { tokens }
    public func save(_ tokens: Tokens?) { self.tokens = tokens }
}

/// HTTP client for the Cloudstore API (bearer auth, rotating refresh tokens).
public final class APIClient: SyncRemote, @unchecked Sendable {
    public let baseURL: URL
    public let session: URLSession
    public let tokens: TokenStore
    private let refresher = RefreshCoalescer()

    public init(baseURL: URL, tokens: TokenStore, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.tokens = tokens
        self.session = session
    }

    // MARK: auth

    public func login(password: String) async throws {
        struct Body: Encodable { let password: String; let client = "ios" }
        struct Resp: Decodable { let access_token: String; let refresh_token: String }
        let r: Resp = try await send("POST", "/api/auth/login", json: Body(password: password), auth: false)
        tokens.save(Tokens(accessToken: r.access_token, refreshToken: r.refresh_token))
    }

    public func logout() async {
        if let t = tokens.load() {
            struct Body: Encodable { let refresh_token: String }
            _ = try? await send("POST", "/api/auth/logout", json: Body(refresh_token: t.refreshToken), auth: false) as EmptyResponse
        }
        tokens.save(nil)
    }

    public var isSignedIn: Bool { tokens.load() != nil }

    func refresh() async throws {
        try await refresher.run { [self] in
            guard let t = tokens.load() else { throw APIError.unauthorized }
            struct Body: Encodable { let refresh_token: String }
            struct Resp: Decodable { let access_token: String; let refresh_token: String }
            do {
                let r: Resp = try await send("POST", "/api/auth/refresh", json: Body(refresh_token: t.refreshToken),
                                             auth: false)
                tokens.save(Tokens(accessToken: r.access_token, refreshToken: r.refresh_token))
            } catch APIError.http(401, _) {
                tokens.save(nil)
                throw APIError.unauthorized
            }
        }
    }

    /// Authorised request with one transparent refresh on 401.
    public func authorizedRequest(_ build: (String) -> URLRequest) async throws -> (Data, HTTPURLResponse) {
        for attempt in 0..<2 {
            guard let t = tokens.load() else { throw APIError.unauthorized }
            var req = build(t.accessToken)
            req.setValue("Bearer \(t.accessToken)", forHTTPHeaderField: "Authorization")
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse else { throw APIError.invalidResponse }
            if http.statusCode == 401 && attempt == 0 {
                try await refresh()
                continue
            }
            if http.statusCode == 401 { throw APIError.unauthorized }
            return (data, http)
        }
        throw APIError.unauthorized
    }

    struct EmptyResponse: Decodable {}

    func send<T: Decodable>(_ method: String, _ path: String, json: Encodable? = nil, auth: Bool = true) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL) else { throw APIError.badURL }
        func build(_ token: String?) -> URLRequest {
            var req = URLRequest(url: url)
            req.httpMethod = method
            req.timeoutInterval = 60
            if let json {
                req.httpBody = try? JSONEncoder().encode(AnyEncodable(json))
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            }
            return req
        }
        let data: Data
        let http: HTTPURLResponse
        if auth {
            (data, http) = try await authorizedRequest { build($0) }
        } else {
            let (d, r) = try await session.data(for: build(nil))
            guard let h = r as? HTTPURLResponse else { throw APIError.invalidResponse }
            (data, http) = (d, h)
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
            throw APIError.http(http.statusCode, detail ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode))
        }
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        return try JSONDecoder().decode(T.self, from: data)
    }

    // MARK: devices + sync

    public func registerDevice(name: String, id: String?) async throws -> String {
        struct Body: Encodable { let name: String; let platform = "ios"; let device_id: String? }
        struct Resp: Decodable { let device_id: String }
        let r: Resp = try await send("POST", "/api/devices", json: Body(name: name, device_id: id))
        return r.device_id
    }

    public func syncRoot(device: String) async throws -> (root: Data, count: Int) {
        struct Resp: Decodable { let root: String; let count: Int }
        let r: Resp = try await send("GET", "/api/sync/\(device)/root")
        return (Data(hexString: r.root) ?? MerkleTree.empty, r.count)
    }

    public func syncNodes(device: String, paths: [String]) async throws -> [String: [Data]] {
        struct Resp: Decodable { let nodes: [String: [String]] }
        let r: Resp = try await send("POST", "/api/sync/\(device)/nodes", json: ["paths": paths])
        return r.nodes.mapValues { $0.map { Data(hexString: $0) ?? MerkleTree.empty } }
    }

    public func syncBuckets(device: String, paths: [String]) async throws -> [String: [String]] {
        struct Resp: Decodable { let buckets: [String: [String]] }
        let r: Resp = try await send("POST", "/api/sync/\(device)/buckets", json: ["paths": paths])
        return r.buckets
    }

    public func reconcile(device: String, add: [String], remove: [String]) async throws -> ReconcileResult {
        try await send("POST", "/api/sync/\(device)/reconcile", json: ["add": add, "remove": remove])
    }

    public enum RemoteStatus: Equatable { case stored, processing, failed(String), unknown }

    public func status(sha256: String) async throws -> RemoteStatus {
        guard let url = URL(string: "/api/files/by-hash/\(sha256)", relativeTo: baseURL) else { throw APIError.badURL }
        let (data, http) = try await authorizedRequest { _ in URLRequest(url: url) }
        if http.statusCode == 404 { return .unknown }
        struct Resp: Decodable { let status: String; let error: String? }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        switch r.status {
        case "stored": return .stored
        case "failed": return .failed(r.error ?? "ingest failed")
        default: return .processing
        }
    }
}

struct AnyEncodable: Encodable {
    let value: Encodable
    init(_ value: Encodable) { self.value = value }
    func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
}

/// Coalesces concurrent token refreshes into one request: the server rotates the
/// refresh token, and replaying an already-rotated token would (correctly) revoke
/// the whole session.
actor RefreshCoalescer {
    private var inFlight: Task<Void, Error>?

    func run(_ body: @escaping @Sendable () async throws -> Void) async throws {
        if let t = inFlight { return try await t.value }
        let t = Task { try await body() }
        inFlight = t
        defer { inFlight = nil }
        try await t.value
    }
}
