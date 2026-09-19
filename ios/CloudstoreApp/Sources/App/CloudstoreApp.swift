import CloudSyncKit
import SwiftUI

@main
struct CloudstoreApp: App {
    @StateObject private var settings = AppSettings.shared
    @StateObject private var engine: SyncEngine
    @StateObject private var session = Session()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let engine = SyncEngine()
        _engine = StateObject(wrappedValue: engine)
        BackgroundSync.register(engine: engine)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(settings)
                .environmentObject(engine)
                .environmentObject(session)
        }
        .onChange(of: scenePhase) { phase in
            switch phase {
            case .active:
                // primary UX: open the app → it syncs
                if session.signedIn { engine.syncNow(reason: "foreground") }
            case .background:
                BackgroundSync.schedule()
                BackgroundSync.finishCurrentWork(engine: engine)
            default:
                break
            }
        }
    }
}

/// Signed-in state, derived from whether tokens exist in the Keychain.
@MainActor
final class Session: ObservableObject {
    @Published var signedIn: Bool

    init(settings: AppSettings = .shared) {
        signedIn = settings.makeAPI()?.isSignedIn ?? false
    }

    func signIn(server: String, password: String, settings: AppSettings) async throws {
        settings.serverURL = server
        guard let api = settings.makeAPI() else { throw APIError.badURL }
        try await api.login(password: password)
        settings.deviceID = try await api.registerDevice(name: DeviceInfo.name, id: settings.deviceID)
        signedIn = true
    }

    func signOut(settings: AppSettings) async {
        await settings.makeAPI()?.logout()
        signedIn = false
    }
}
