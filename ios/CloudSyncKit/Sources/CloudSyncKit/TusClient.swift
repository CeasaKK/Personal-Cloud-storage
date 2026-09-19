import Foundation

/// tus 1.0.0 client (TDD §8).
///
/// Foreground uploads send 8 MiB PATCH chunks and, after any failure, ask the server
/// for its `Upload-Offset` and continue from there. For background uploads the app
/// hands `patchRequest(...)` + a file to a background `URLSession` upload task,
/// which iOS keeps running after the app is suspended.
public struct UploadMetadata: Sendable, Equatable {
    public var filename: String
    public var sha256: String
    public var mimeType: String?
    public var deviceID: String?
    public var createdAt: Date?
    public var localID: String?

    public init(filename: String, sha256: String, mimeType: String? = nil, deviceID: String? = nil,
                createdAt: Date? = nil, localID: String? = nil) {
        self.filename = filename
        self.sha256 = sha256
        self.mimeType = mimeType
        self.deviceID = deviceID
        self.createdAt = createdAt
        self.localID = localID
    }

    /// `Upload-Metadata` header: comma-separated `key base64(value)` pairs.
    public var header: String {
        var pairs: [(String, String)] = [("filename", filename), ("sha256", sha256)]
        if let mimeType { pairs.append(("mimetype", mimeType)) }
        if let deviceID { pairs.append(("device_id", deviceID)) }
        if let createdAt { pairs.append(("created_at", String(format: "%.3f", createdAt.timeIntervalSince1970))) }
        if let localID { pairs.append(("local_id", localID)) }
        return pairs.map { "\($0.0) \(Data($0.1.utf8).base64EncodedString())" }.joined(separator: ",")
    }
}

public enum TusError: Error, LocalizedError, Equatable {
    case unexpectedStatus(Int)
    case checksumMismatch
    case missingLocation
    case tooManyFailures

    public var errorDescription: String? {
        switch self {
        case .unexpectedStatus(let s): return "Upload failed (HTTP \(s))"
        case .checksumMismatch: return "The server's checksum did not match; the file will be re-read."
        case .missingLocation: return "Server did not return an upload location."
        case .tooManyFailures: return "Upload kept failing; will retry on the next sync."
        }
    }
}

public final class TusClient: @unchecked Sendable {
    public static let chunkSize = 8 * 1024 * 1024
    let api: APIClient

    public init(api: APIClient) {
        self.api = api
    }

    private func url(_ path: String) -> URL {
        URL(string: path, relativeTo: api.baseURL)!.absoluteURL
    }

    /// POST: create an upload resource, returning its absolute URL.
    public func create(length: Int64, metadata: UploadMetadata) async throws -> URL {
        let target = url("/api/tus")
        let (_, http) = try await api.authorizedRequest { _ in
            var r = URLRequest(url: target)
            r.httpMethod = "POST"
            r.setValue("1.0.0", forHTTPHeaderField: "Tus-Resumable")
            r.setValue(String(length), forHTTPHeaderField: "Upload-Length")
            r.setValue(metadata.header, forHTTPHeaderField: "Upload-Metadata")
            return r
        }
        guard http.statusCode == 201 else { throw TusError.unexpectedStatus(http.statusCode) }
        guard let loc = http.value(forHTTPHeaderField: "Location") else { throw TusError.missingLocation }
        return url(loc)
    }

    /// HEAD: how many bytes the server already has.
    public func offset(of upload: URL) async throws -> Int64 {
        let (_, http) = try await api.authorizedRequest { _ in
            var r = URLRequest(url: upload)
            r.httpMethod = "HEAD"
            r.setValue("1.0.0", forHTTPHeaderField: "Tus-Resumable")
            return r
        }
        guard http.statusCode == 200 else { throw TusError.unexpectedStatus(http.statusCode) }
        return Int64(http.value(forHTTPHeaderField: "Upload-Offset") ?? "0") ?? 0
    }

    /// A PATCH request for a background upload task (the body is supplied as a file).
    public func patchRequest(upload: URL, offset: Int64, accessToken: String) -> URLRequest {
        var r = URLRequest(url: upload)
        r.httpMethod = "PATCH"
        r.setValue("1.0.0", forHTTPHeaderField: "Tus-Resumable")
        r.setValue(String(offset), forHTTPHeaderField: "Upload-Offset")
        r.setValue("application/offset+octet-stream", forHTTPHeaderField: "Content-Type")
        r.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        return r
    }

    /// Foreground chunked upload of a file with resume-after-failure.
    public func upload(file: URL, metadata: UploadMetadata, resumeFrom existing: URL? = nil,
                       progress: @escaping @Sendable (Double) -> Void = { _ in }) async throws -> URL {
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        let length = Int64(try handle.seekToEnd())
        let upload: URL
        var offset: Int64 = 0
        if let existing, let o = try? await self.offset(of: existing) {
            upload = existing
            offset = o
        } else {
            upload = try await create(length: length, metadata: metadata)
        }
        var failures = 0
        while offset < length {
            try Task.checkCancellation()
            try handle.seek(toOffset: UInt64(offset))
            let chunk = handle.readData(ofLength: Self.chunkSize)
            do {
                let (_, http) = try await api.authorizedRequest { _ in
                    var r = URLRequest(url: upload)
                    r.httpMethod = "PATCH"
                    r.httpBody = chunk
                    r.setValue("1.0.0", forHTTPHeaderField: "Tus-Resumable")
                    r.setValue(String(offset), forHTTPHeaderField: "Upload-Offset")
                    r.setValue("application/offset+octet-stream", forHTTPHeaderField: "Content-Type")
                    r.timeoutInterval = 300
                    return r
                }
                if http.statusCode == 460 { throw TusError.checksumMismatch }
                guard http.statusCode == 204 else { throw TusError.unexpectedStatus(http.statusCode) }
                offset = Int64(http.value(forHTTPHeaderField: "Upload-Offset") ?? "") ?? offset + Int64(chunk.count)
                failures = 0
                progress(Double(offset) / Double(max(length, 1)))
            } catch TusError.checksumMismatch {
                throw TusError.checksumMismatch
            } catch APIError.unauthorized {
                throw APIError.unauthorized
            } catch {
                failures += 1
                if failures > 5 { throw TusError.tooManyFailures }
                try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(failures))) * 500_000_000)
                offset = (try? await self.offset(of: upload)) ?? offset
            }
        }
        if length == 0 { progress(1) }
        return upload
    }
}
