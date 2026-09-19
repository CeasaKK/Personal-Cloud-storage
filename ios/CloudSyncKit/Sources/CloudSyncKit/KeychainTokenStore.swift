import Foundation
import Security

/// Tokens in the Keychain. Pass the shared access group so the share extension can
/// upload with the app's session (both targets list it in keychain-access-groups).
public final class KeychainTokenStore: TokenStore {
    let service: String
    let account = "tokens"
    let accessGroup: String?

    public init(service: String = "app.cloudstore.tokens", accessGroup: String? = nil) {
        self.service = service
        self.accessGroup = accessGroup
    }

    private var query: [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup { q[kSecAttrAccessGroup as String] = accessGroup }
        return q
    }

    public func load() -> Tokens? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(Tokens.self, from: data)
    }

    public func save(_ tokens: Tokens?) {
        SecItemDelete(query as CFDictionary)
        guard let tokens, let data = try? JSONEncoder().encode(tokens) else { return }
        var q = query
        q[kSecValueData as String] = data
        // readable after first unlock so background tasks can sync while the phone is locked
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }
}
