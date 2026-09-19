import CloudSyncKit
import Foundation

/// Settings shared with the share extension through the App Group.
final class AppSettings: ObservableObject {
    static let appGroup = "group.app.cloudstore"
    static let shared = AppSettings()

    private let defaults: UserDefaults

    init(defaults: UserDefaults = UserDefaults(suiteName: AppSettings.appGroup) ?? .standard) {
        self.defaults = defaults
        serverURL = defaults.string(forKey: "serverURL") ?? ""
        deviceID = defaults.string(forKey: "deviceID")
        wifiOnly = defaults.object(forKey: "wifiOnly") as? Bool ?? true
        includeVideos = defaults.object(forKey: "includeVideos") as? Bool ?? true
    }

    @Published var serverURL: String { didSet { defaults.set(serverURL, forKey: "serverURL") } }
    @Published var deviceID: String? { didSet { defaults.set(deviceID, forKey: "deviceID") } }
    @Published var wifiOnly: Bool { didSet { defaults.set(wifiOnly, forKey: "wifiOnly") } }
    @Published var includeVideos: Bool { didSet { defaults.set(includeVideos, forKey: "includeVideos") } }

    var lastSyncAt: Date? {
        get { defaults.object(forKey: "lastSyncAt") as? Date }
        set { defaults.set(newValue, forKey: "lastSyncAt") }
    }

    /// Archived `PHPersistentChangeToken` for incremental library scans.
    var changeToken: Data? {
        get { defaults.data(forKey: "photoChangeToken") }
        set { defaults.set(newValue, forKey: "photoChangeToken") }
    }

    var baseURL: URL? {
        var s = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        if !s.contains("://") { s = "https://" + s }
        return URL(string: s)
    }

    /// Keychain access group shared by app + extension, resolved from Info.plist
    /// (`$(AppIdentifierPrefix)app.cloudstore.shared` at build time).
    static var keychainGroup: String? {
        Bundle.main.object(forInfoDictionaryKey: "KeychainAccessGroup") as? String
    }

    func makeAPI() -> APIClient? {
        guard let url = baseURL else { return nil }
        let config = URLSessionConfiguration.default
        config.allowsExpensiveNetworkAccess = !wifiOnly
        config.allowsConstrainedNetworkAccess = !wifiOnly
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 120
        return APIClient(baseURL: url, tokens: KeychainTokenStore(accessGroup: Self.keychainGroup),
                         session: URLSession(configuration: config))
    }
}
