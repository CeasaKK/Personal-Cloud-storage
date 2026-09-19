#if os(iOS)
import CloudSyncKit
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// "Share → Cloudstore": uploads the shared photos, videos or files immediately,
/// using the app's session (Keychain access group) and server settings (App Group).
/// A useful manual path given iOS's limits on background execution (TDD §9).
final class ShareViewController: UIViewController {
    private let model = ShareModel()

    override func viewDidLoad() {
        super.viewDidLoad()
        let host = UIHostingController(rootView: ShareView(model: model) { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        })
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
        let providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? []).flatMap { $0.attachments ?? [] }
        Task { await model.upload(providers) }
    }
}

@MainActor
final class ShareModel: ObservableObject {
    @Published var status = "Preparing…"
    @Published var progress = 0.0
    @Published var done = false
    @Published var failed = false

    func upload(_ providers: [NSItemProvider]) async {
        let settings = AppSettings()
        guard let api = settings.makeAPI(), api.isSignedIn else {
            status = "Open Cloudstore and sign in first."
            failed = true
            done = true
            return
        }
        let tus = TusClient(api: api)
        var uploaded = 0
        for (i, provider) in providers.enumerated() {
            guard let type = [UTType.image, .movie, .data].first(where: { provider.hasItemConformingToTypeIdentifier($0.identifier) })
            else { continue }
            status = "Uploading \(i + 1) of \(providers.count)…"
            do {
                let file = try await copy(provider, type: type)
                defer { try? FileManager.default.removeItem(at: file) }
                let (sha, _) = try StreamingSHA256.hash(file: file)
                if case .stored = try await api.status(sha256: sha) {
                    uploaded += 1
                    continue
                }
                let meta = UploadMetadata(filename: file.lastPathComponent, sha256: sha,
                                          mimeType: UTType(filenameExtension: file.pathExtension)?.preferredMIMEType,
                                          deviceID: settings.deviceID, createdAt: Date())
                let n = Double(providers.count)
                let base = Double(i) / n
                _ = try await tus.upload(file: file, metadata: meta) { f in
                    Task { @MainActor in self.progress = base + f / n }
                }
                uploaded += 1
            } catch {
                status = error.localizedDescription
                failed = true
            }
        }
        progress = 1
        if !failed { status = uploaded == 1 ? "Saved to your server" : "\(uploaded) items saved to your server" }
        done = true
    }

    private func copy(_ provider: NSItemProvider, type: UTType) async throws -> URL {
        try await withCheckedThrowingContinuation { cont in
            provider.loadFileRepresentation(forTypeIdentifier: type.identifier) { url, error in
                guard let url else { return cont.resume(throwing: error ?? CocoaError(.fileReadUnknown)) }
                // the provided URL is only valid inside this callback: copy it out
                let dest = FileManager.default.temporaryDirectory
                    .appendingPathComponent(UUID().uuidString, isDirectory: true)
                do {
                    try FileManager.default.createDirectory(at: dest, withIntermediateDirectories: true)
                    let target = dest.appendingPathComponent(url.lastPathComponent)
                    try FileManager.default.copyItem(at: url, to: target)
                    cont.resume(returning: target)
                } catch {
                    cont.resume(throwing: error)
                }
            }
        }
    }
}

struct ShareView: View {
    @ObservedObject var model: ShareModel
    let close: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: model.failed ? "exclamationmark.icloud" : (model.done ? "checkmark.icloud" : "icloud.and.arrow.up"))
                .font(.system(size: 48))
                .foregroundStyle(model.failed ? Color.red : Color.accentColor)
            Text(model.status).font(.headline).multilineTextAlignment(.center)
            ProgressView(value: model.progress).opacity(model.done ? 0 : 1)
            Button(model.done ? "Done" : "Cancel", action: close).buttonStyle(.borderedProminent)
        }
        .padding(32)
    }
}
#endif
