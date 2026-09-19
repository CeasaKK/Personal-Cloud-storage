import CloudSyncKit
import Photos
import SwiftUI

struct RootView: View {
    @EnvironmentObject var session: Session

    var body: some View {
        if session.signedIn {
            StatusView()
        } else {
            LoginView()
        }
    }
}

// MARK: - Sign in

struct LoginView: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var session: Session
    @EnvironmentObject var engine: SyncEngine
    @State private var server = AppSettings.shared.serverURL
    @State private var password = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Image(systemName: "icloud.and.arrow.up")
                            .font(.system(size: 40))
                            .foregroundStyle(.tint)
                        Text("Cloudstore").font(.largeTitle.bold())
                        Text("Back up your photos to your own server.")
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 8)
                }
                Section {
                    TextField("minipc.your-tailnet.ts.net", text: $server)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        #endif
                    SecureField("Password", text: $password)
                        .textContentType(.password)
                } header: {
                    Text("Server")
                } footer: {
                    Text("Your server's Tailscale name. Tailscale must be connected on this iPhone; the server is never exposed to the public internet.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button {
                        Task { await signIn() }
                    } label: {
                        HStack {
                            Spacer()
                            if busy { ProgressView() } else { Text("Sign in").bold() }
                            Spacer()
                        }
                    }
                    .disabled(busy || server.isEmpty || password.isEmpty)
                }
            }
            .navigationTitle("Sign in")
        }
    }

    private func signIn() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await session.signIn(server: server, password: password, settings: settings)
            password = ""
            _ = await PhotoLibrary.requestAccess()
            engine.syncNow(reason: "first sign-in")
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Status

struct StatusView: View {
    @EnvironmentObject var engine: SyncEngine
    @EnvironmentObject var settings: AppSettings
    @State private var showSettings = false
    @State private var access = PhotoLibrary.authorization

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(spacing: 16) {
                        ProgressRing(fraction: fraction, running: engine.isRunning)
                            .frame(width: 150, height: 150)
                        VStack(spacing: 4) {
                            Text(headline).font(.title3.bold()).multilineTextAlignment(.center)
                            Text(engine.phase.label)
                                .font(.subheadline)
                                .foregroundStyle(isError ? .red : .secondary)
                                .multilineTextAlignment(.center)
                        }
                        Button {
                            engine.syncNow(reason: "manual")
                        } label: {
                            Label(engine.isRunning ? "Backing up…" : "Back up now", systemImage: "arrow.triangle.2.circlepath")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(engine.isRunning)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                }

                if access == .limited {
                    Section {
                        Label("Only the photos you selected are visible to Cloudstore. Allow full access in Settings to back up everything.",
                              systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                } else if access == .denied || access == .restricted {
                    Section {
                        Label("Photo access is off. Enable it in Settings › Privacy › Photos › Cloudstore.",
                              systemImage: "lock")
                            .foregroundStyle(.red)
                    }
                }

                Section("Library") {
                    row("Backed up", "\(engine.synced)")
                    row("Waiting", "\(engine.pending)")
                    if engine.failedCount > 0 { row("Failed (will retry)", "\(engine.failedCount)") }
                    row("Last backup", engine.lastSync.map { $0.formatted(.relative(presentation: .named)) } ?? "never")
                    if let mode = engine.lastPlanMode { row("Last sync check", mode) }
                }

                let errors = engine.recentErrors()
                if !errors.isEmpty {
                    Section("Recent problems") {
                        ForEach(errors, id: \.0) { name, message in
                            VStack(alignment: .leading) {
                                Text(name).font(.subheadline)
                                Text(message).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                Section {
                    if let url = settings.baseURL {
                        Link(destination: url) {
                            Label("Open your library in the browser", systemImage: "safari")
                        }
                    }
                } footer: {
                    Text("Backups run whenever you open Cloudstore. iOS may also run them in the background — usually overnight while charging — but it decides when, so open the app to be sure the latest photos are safe.")
                }
            }
            .navigationTitle("Backup")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .refreshable { await engine.syncNow(reason: "pull").value }
            .task {
                if PhotoLibrary.authorization == .notDetermined { access = await PhotoLibrary.requestAccess() }
                access = PhotoLibrary.authorization
                engine.refreshCounts()
            }
        }
    }

    private var fraction: Double {
        guard engine.total > 0 else { return engine.phase == .finished ? 1 : 0 }
        return Double(engine.synced) / Double(engine.total)
    }

    private var headline: String {
        if engine.total == 0 { return engine.isRunning ? "Getting ready" : "Nothing to back up yet" }
        return "\(engine.synced.formatted()) of \(engine.total.formatted()) backed up"
    }

    private var isError: Bool {
        if case .failed = engine.phase { return true }
        return false
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k)
            Spacer()
            Text(v).foregroundStyle(.secondary).monospacedDigit()
        }
    }
}

struct ProgressRing: View {
    let fraction: Double
    let running: Bool

    var body: some View {
        ZStack {
            Circle().stroke(Color.secondary.opacity(0.2), lineWidth: 12)
            Circle()
                .trim(from: 0, to: max(0.001, min(1, fraction)))
                .stroke(fraction >= 1 ? Color.green : Color.accentColor,
                        style: StrokeStyle(lineWidth: 12, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.easeOut, value: fraction)
            Image(systemName: fraction >= 1 && !running ? "checkmark" : "icloud.and.arrow.up")
                .font(.system(size: 44, weight: .semibold))
                .foregroundStyle(fraction >= 1 && !running ? Color.green : Color.accentColor)
        }
        .accessibilityElement()
        .accessibilityLabel("\(Int(fraction * 100)) percent backed up")
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var settings: AppSettings
    @EnvironmentObject var session: Session
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Backup") {
                    Toggle("Wi-Fi only", isOn: $settings.wifiOnly)
                    Toggle("Include videos", isOn: $settings.includeVideos)
                }
                Section("Server") {
                    LabeledContent("Address", value: settings.serverURL)
                    LabeledContent("This device", value: DeviceInfo.name)
                    if let id = settings.deviceID {
                        LabeledContent("Device ID", value: String(id.prefix(12)) + "…")
                    }
                }
                Section {
                    Button("Sign out", role: .destructive) {
                        Task {
                            await session.signOut(settings: settings)
                            dismiss()
                        }
                    }
                } footer: {
                    Text("Signing out keeps everything already on your server. Photos are uploaded as their untouched originals (HEIC stays HEIC) and identified by SHA-256, so nothing is ever uploaded twice.")
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
