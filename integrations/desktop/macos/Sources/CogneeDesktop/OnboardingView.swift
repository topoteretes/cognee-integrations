import AppKit
import SwiftUI

/// First-run setup: pick where cognee runs, hand over the credentials, and
/// the app writes the backend config and starts it. Reachable later from the
/// menu (Setup…) to switch modes.
@MainActor
final class OnboardingModel: ObservableObject {
    enum Mode: String, CaseIterable, Identifiable {
        case cloud, local
        var id: String { rawValue }
    }

    @Published var mode: Mode = .cloud
    // cloud
    @Published var tenantURL = "https://api.cognee.ai"
    @Published var cloudAPIKey = ""
    // local
    @Published var llmAPIKey = ""
    @Published var llmModel = ""
    // identity (optional — powers team handover)
    @Published var userName = NSUserName()
    @Published var teamName = ""

    @Published var statusText = ""
    @Published var busy = false
    @Published var done = false

    /// Which profile this Setup window edits; follows the active profile.
    var profile: String { Profiles.active }

    var configPath: String { Profiles.configPath(profile) }

    static var isConfigured: Bool {
        Profiles.isConfigured(Profiles.active)
    }

    func loadExisting() {
        guard let text = try? String(contentsOfFile: configPath, encoding: .utf8) else {
            return
        }
        var values: [String: String] = [:]
        for line in text.split(separator: "\n") {
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<eq])
            var value = String(line[line.index(after: eq)...])
            if value.hasPrefix("'") && value.hasSuffix("'") && value.count >= 2 {
                value = String(value.dropFirst().dropLast()).replacingOccurrences(
                    of: "'\\''", with: "'")
            }
            values[key] = value
        }
        mode = values["COGNEE_MODE"] == "local" ? .local : .cloud
        tenantURL = values["COGNEE_CLOUD_URL"] ?? tenantURL
        cloudAPIKey = values["COGNEE_CLOUD_API_KEY"] ?? ""
        llmAPIKey = values["LLM_API_KEY"] ?? ""
        llmModel = values["LLM_MODEL"] ?? ""
        userName = values["COGNEE_DESKTOP_USER"] ?? userName
        teamName = values["COGNEE_DESKTOP_TEAM"] ?? ""
    }

    func saveAndStart() {
        statusText = ""
        switch mode {
        case .cloud:
            if tenantURL.trimmingCharacters(in: .whitespaces).isEmpty {
                tenantURL = "https://api.cognee.ai"
            }
            guard !cloudAPIKey.trimmingCharacters(in: .whitespaces).isEmpty,
                URL(string: tenantURL)?.scheme != nil
            else {
                statusText = "Enter your tenant URL and API key."
                return
            }
        case .local:
            guard !llmAPIKey.trimmingCharacters(in: .whitespaces).isEmpty else {
                statusText = "Enter the LLM API key cognee should use."
                return
            }
        }
        busy = true
        Task {
            defer { busy = false }
            do {
                try writeConfig()
            } catch {
                statusText = "Could not write \(configPath)."
                return
            }
            statusText = "Starting the backend…"
            let started = await BackendLauncher.restart(profile: profile)
            if !started {
                statusText =
                    "Config saved. Could not start the backend automatically — run scripts/run_backend.sh, then press ⌥Space."
                return
            }
            statusText = ""
            done = true
        }
    }

    private func writeConfig() throws {
        var lines = [
            "# Written by Cognee (menu bar → Setup…). Sourced by run_backend.sh;",
            "# values here win over the repo .env.",
            "COGNEE_MODE=\(mode.rawValue)",
            "COGNEE_DESKTOP_PORT=\(Profiles.port(profile))",
        ]
        func add(_ key: String, _ value: String) {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { return }
            let escaped = trimmed.replacingOccurrences(of: "'", with: "'\\''")
            lines.append("\(key)='\(escaped)'")
        }
        switch mode {
        case .cloud:
            add("COGNEE_CLOUD_URL", tenantURL)
            add("COGNEE_CLOUD_API_KEY", cloudAPIKey)
        case .local:
            add("LLM_API_KEY", llmAPIKey)
            add("LLM_MODEL", llmModel)
        }
        add("COGNEE_DESKTOP_USER", userName)
        add("COGNEE_DESKTOP_TEAM", teamName)

        let path = configPath
        try FileManager.default.createDirectory(
            atPath: (path as NSString).deletingLastPathComponent,
            withIntermediateDirectories: true
        )
        try (lines.joined(separator: "\n") + "\n").write(
            toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: path)
    }
}

/// Starts / restarts the backend via the repo script and waits for /health.
enum BackendLauncher {
    static var scriptPath: String {
        UserDefaults.standard.string(forKey: "backendScriptPath")
            ?? defaultScriptPath
    }

    private static var defaultScriptPath: String {
        // dist app lives at <integration>/macos/dist/…; walk up to scripts/.
        let bundle = Bundle.main.bundlePath as NSString
        let integration = bundle.deletingLastPathComponent  // dist
            .split(separator: "/").dropLast(2).joined(separator: "/")
        return "/\(integration)/scripts/run_backend.sh"
    }

    static func restart(profile: String = "default") async -> Bool {
        let port = Profiles.port(profile)
        let script = scriptPath
        guard FileManager.default.isExecutableFile(atPath: script) else { return false }
        let command =
            "lsof -ti TCP:\(port) -sTCP:LISTEN | xargs kill 2>/dev/null; sleep 1; "
            + "COGNEE_DESKTOP_PROFILE='\(profile)' nohup '\(script)' "
            + "> /tmp/cognee-desktop-backend-\(profile).log 2>&1 &"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-lc", command]
        do { try process.run() } catch { return false }
        // uv sync on a cold environment can take a while; poll generously.
        let client = BackendClient(baseURL: Profiles.url(profile))
        for _ in 0..<60 {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if (try? await client.health()) != nil { return true }
        }
        return false
    }
}

struct OnboardingView: View {
    @ObservedObject var model: OnboardingModel
    var onFinished: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Set up Cognee")
                        .font(.system(size: 20, weight: .semibold))
                    Spacer()
                    Text("profile: \(model.profile)")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(.quaternary.opacity(0.6), in: Capsule())
                        .foregroundStyle(.secondary)
                }
                Text("Pick where your knowledge graph lives. You can change this any time from the menu bar (Setup…).")
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
            }

            Picker("", selection: $model.mode) {
                Text("Cognee Cloud").tag(OnboardingModel.Mode.cloud)
                Text("On this Mac").tag(OnboardingModel.Mode.local)
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            Group {
                if model.mode == .cloud {
                    VStack(alignment: .leading, spacing: 10) {
                        labeled("Tenant URL") {
                            TextField("https://api.cognee.ai", text: $model.tenantURL)
                        }
                        labeled("API key") {
                            SecureField("ck_…", text: $model.cloudAPIKey)
                        }
                        Text("Indexing and search run on your cognee tenant; this Mac only uploads files and reads results.")
                            .font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                } else {
                    VStack(alignment: .leading, spacing: 10) {
                        labeled("LLM API key") {
                            SecureField("sk-…", text: $model.llmAPIKey)
                        }
                        labeled("Model (optional)") {
                            TextField("gpt-4o-mini", text: $model.llmModel)
                        }
                        Text("Everything stays on this Mac; the LLM key is used to extract knowledge while indexing and to answer questions.")
                            .font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                }
            }
            .textFieldStyle(.roundedBorder)

            VStack(alignment: .leading, spacing: 10) {
                Text("Sharing (optional)")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    labeled("Your name") { TextField("vasilije", text: $model.userName) }
                    labeled("Team") { TextField("core", text: $model.teamName) }
                }
                .textFieldStyle(.roundedBorder)
            }

            if !model.statusText.isEmpty {
                Text(model.statusText)
                    .font(.system(size: 12))
                    .foregroundStyle(model.busy ? .secondary : Color.orange)
            }
            if model.done {
                Label("Connected. Press ⌥ Space to search.", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(.green)
            }

            HStack {
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                if model.done {
                    Button("Done") { onFinished() }.keyboardShortcut(.defaultAction)
                } else {
                    Button("Save & Start") { model.saveAndStart() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(model.busy)
                }
            }
        }
        .padding(24)
        .frame(width: 480)
        .onAppear { model.loadExisting() }
    }

    private func labeled<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 11)).foregroundStyle(.secondary)
            content()
        }
    }
}
