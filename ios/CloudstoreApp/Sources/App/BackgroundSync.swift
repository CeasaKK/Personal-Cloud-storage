import Foundation
#if os(iOS)
import BackgroundTasks
import UIKit
#endif

enum DeviceInfo {
    static var name: String {
        #if os(iOS)
        UIDevice.current.name
        #else
        Host.current().localizedName ?? "Mac"
        #endif
    }
}

/// Best-effort background backup via BGTaskScheduler (TDD §9 constraint, §16).
///
/// iOS decides *when* these run (typically overnight while charging on Wi-Fi for the
/// processing task), so the UI never promises background parity with Android — the
/// primary path is sync-on-open. Both identifiers are listed in Info.plist under
/// BGTaskSchedulerPermittedIdentifiers.
enum BackgroundSync {
    static let refreshID = "app.cloudstore.refresh"     // short: scan + Merkle check + a few uploads
    static let processingID = "app.cloudstore.backup"   // long: bulk hashing + uploads, needs power

    #if os(iOS)
    @MainActor
    static func register(engine: SyncEngine) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshID, using: nil) { task in
            Task { @MainActor in handle(task, engine: engine) }
        }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: processingID, using: nil) { task in
            Task { @MainActor in handle(task, engine: engine) }
        }
    }

    @MainActor
    private static func handle(_ task: BGTask, engine: SyncEngine) {
        schedule()  // always queue the next opportunity first
        let run = engine.syncNow(reason: task.identifier)
        task.expirationHandler = { Task { @MainActor in engine.cancel() } }
        Task {
            await run.value
            if case .failed = engine.phase {
                task.setTaskCompleted(success: false)
            } else {
                task.setTaskCompleted(success: true)
            }
        }
    }

    static func schedule() {
        let refresh = BGAppRefreshTaskRequest(identifier: refreshID)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(refresh)

        let processing = BGProcessingTaskRequest(identifier: processingID)
        processing.requiresNetworkConnectivity = true
        processing.requiresExternalPower = true
        processing.earliestBeginDate = Date(timeIntervalSinceNow: 60 * 60)
        try? BGTaskScheduler.shared.submit(processing)
    }

    /// When the app is backgrounded mid-upload, ask for the usual ~30 s grace period so
    /// the current chunk finishes cleanly; tus resumes anything unfinished next time.
    @MainActor
    static func finishCurrentWork(engine: SyncEngine) {
        guard engine.isRunning else { return }
        var id = UIBackgroundTaskIdentifier.invalid
        id = UIApplication.shared.beginBackgroundTask(withName: "finish-upload") {
            engine.cancel()
            UIApplication.shared.endBackgroundTask(id)
        }
        Task { @MainActor in
            while engine.isRunning { try? await Task.sleep(nanoseconds: 500_000_000) }
            UIApplication.shared.endBackgroundTask(id)
        }
    }
    #else
    @MainActor static func register(engine: SyncEngine) {}
    static func schedule() {}
    @MainActor static func finishCurrentWork(engine: SyncEngine) {}
    #endif
}
