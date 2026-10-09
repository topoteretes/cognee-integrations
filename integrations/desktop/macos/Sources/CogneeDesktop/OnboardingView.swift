import AppKit
import SwiftUI

/// First-run setup: pick where cognee runs, hand over the credentials, and
/// the app writes the backend config and starts it. Reachable later from the
/// menu (Setup…) to switch modes.
@MainActor
final class OnboardingModel: ObservableObject {
    /// The three honest answers to "where does my knowledge graph live?".
    enum Mode: String, CaseIterable, Identifiable {
        case cloud  // hosted cognee tenant
        case apiKey  // local engine + a hosted LLM provider
        case ollama  // local engine + local LLM: nothing leaves the Mac
        var id: String { rawValue }
    }

    /// Hosted LLM providers for the "your own API key" path.
    enum Provider: String, CaseIterable, Identifiable {
        case openai, anthropic, gemini, openrouter
        var id: String { rawValue }

        var label: String {
            switch self {
            case .openai: return "OpenAI"
            case .anthropic: return "Anthropic"
            case .gemini: return "Google Gemini"
            case .openrouter: return "OpenRouter"
            }
        }

        var keyPlaceholder: String {
            switch self {
            case .openai: return "sk-…"
            case .anthropic: return "sk-ant-…"
            case .gemini: return "AIza…"
            case .openrouter: return "sk-or-…"
            }
        }

        var defaultModel: String {
            switch self {
            case .openai: return "gpt-4o-mini"
            case .anthropic: return "claude-sonnet-4-5"
            case .gemini: return "gemini-2.0-flash"
            case .openrouter: return "openai/gpt-4o-mini"
            }
        }

        var keySource: String {
            switch self {
            case .openai: return "platform.openai.com/api-keys"
            case .anthropic: return "console.anthropic.com"
            case .gemini: return "aistudio.google.com/apikey"
            case .openrouter: return "openrouter.ai/keys"
            }
        }
    }

    @Published var mode: Mode = .cloud
    // cloud
    @Published var tenantURL = "https://api.cognee.ai"
    @Published var cloudAPIKey = ""
    // apiKey
    @Published var provider: Provider = .openai {
        didSet { if !modelEdited { llmModel = provider.defaultModel } }
    }
    @Published var llmAPIKey = ""
    @Published var llmModel = Provider.openai.defaultModel {
        didSet { modelEdited = true }
    }
    private var modelEdited = false
    // ollama
    @Published var ollamaModel = "llama3.1:8b"
    @Published var ollamaEmbeddingModel = "avr/sfr-embedding-mistral:latest"
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
        let savedProvider = values["LLM_PROVIDER"] ?? ""
        if values["COGNEE_MODE"] == "local" {
            if savedProvider == "ollama" {
                mode = .ollama
                ollamaModel = values["LLM_MODEL"] ?? ollamaModel
                ollamaEmbeddingModel = values["EMBEDDING_MODEL"] ?? ollamaEmbeddingModel
            } else {
                mode = .apiKey
                if values["LLM_ENDPOINT"]?.contains("openrouter") == true {
                    provider = .openrouter
                } else if let known = Provider(rawValue: savedProvider) {
                    provider = known
                }
                llmAPIKey = values["LLM_API_KEY"] ?? ""
                if let saved = values["LLM_MODEL"], !saved.isEmpty { llmModel = saved }
            }
        } else {
            mode = .cloud
        }
        tenantURL = values["COGNEE_CLOUD_URL"] ?? tenantURL
        cloudAPIKey = values["COGNEE_CLOUD_API_KEY"] ?? ""
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
        case .apiKey:
            guard !llmAPIKey.trimmingCharacters(in: .whitespaces).isEmpty else {
                statusText = "Enter your \(provider.label) API key (from \(provider.keySource))."
                return
            }
        case .ollama:
            break  // nothing secret needed — Ollama runs keyless on this Mac
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
            "COGNEE_MODE=\(mode == .cloud ? "cloud" : "local")",
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
        case .apiKey:
            switch provider {
            case .openrouter:
                // OpenRouter speaks the OpenAI protocol on its own endpoint
                add("LLM_PROVIDER", "custom")
                add("LLM_ENDPOINT", "https://openrouter.ai/api/v1")
            default:
                add("LLM_PROVIDER", provider.rawValue)
            }
            add("LLM_API_KEY", llmAPIKey)
            add("LLM_MODEL", llmModel)
        case .ollama:
            add("LLM_PROVIDER", "ollama")
            add("LLM_API_KEY", "ollama")  // required to be non-empty, unused
            add("LLM_MODEL", ollamaModel)
            add("LLM_ENDPOINT", "http://localhost:11434/v1")
            add("EMBEDDING_PROVIDER", "ollama")
            add("EMBEDDING_MODEL", ollamaEmbeddingModel)
            add("EMBEDDING_ENDPOINT", "http://localhost:11434/api/embeddings")
            add("EMBEDDING_DIMENSIONS", "4096")
            add("HUGGINGFACE_TOKENIZER", "Salesforce/SFR-Embedding-Mistral")
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
        VStack(alignment: .leading, spacing: 16) {
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

            // The three setups, each with what it actually means.
            VStack(spacing: 6) {
                modeCard(
                    .cloud, title: "Cognee Cloud",
                    detail:
                        "Hosted by cognee — easiest. Indexing and search run on your tenant; this Mac only uploads files and reads results. Team sharing works out of the box. Needs a tenant URL and API key from your cognee.ai dashboard."
                )
                modeCard(
                    .apiKey, title: "On this Mac, with your own AI provider",
                    detail:
                        "Your files and the knowledge graph stay on this Mac. Only the text being analyzed is sent to the AI provider you pick (OpenAI, Anthropic, Gemini, OpenRouter) using your own API key."
                )
                modeCard(
                    .ollama, title: "On this Mac, fully private (local AI)",
                    detail:
                        "Nothing ever leaves this Mac: a local AI model via the free Ollama app does the reading and answering. No account, no API key — but slower and less capable than hosted models."
                )
            }

            Group {
                switch model.mode {
                case .cloud:
                    VStack(alignment: .leading, spacing: 10) {
                        labeled("Tenant URL") {
                            TextField("https://api.cognee.ai", text: $model.tenantURL)
                        }
                        labeled("API key") {
                            SecureField("ck_…", text: $model.cloudAPIKey)
                        }
                    }
                case .apiKey:
                    VStack(alignment: .leading, spacing: 10) {
                        labeled("Provider") {
                            Picker("", selection: $model.provider) {
                                ForEach(OnboardingModel.Provider.allCases) { p in
                                    Text(p.label).tag(p)
                                }
                            }
                            .labelsHidden()
                        }
                        labeled("API key — get one at \(model.provider.keySource)") {
                            SecureField(model.provider.keyPlaceholder, text: $model.llmAPIKey)
                        }
                        labeled("Model") {
                            TextField(model.provider.defaultModel, text: $model.llmModel)
                        }
                    }
                case .ollama:
                    VStack(alignment: .leading, spacing: 8) {
                        Text("One-time preparation, in Terminal:")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text(
                            """
                            1.  brew install ollama        (or download from ollama.com)
                            2.  ollama pull \(model.ollamaModel)
                            3.  ollama pull \(model.ollamaEmbeddingModel)
                            """
                        )
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
                        HStack(spacing: 10) {
                            labeled("Chat model") {
                                TextField("llama3.1:8b", text: $model.ollamaModel)
                            }
                            labeled("Embedding model") {
                                TextField(
                                    "avr/sfr-embedding-mistral:latest",
                                    text: $model.ollamaEmbeddingModel)
                            }
                        }
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
        .frame(width: 520)
        .onAppear { model.loadExisting() }
    }

    /// A selectable card: radio dot, name, and an honest description of the
    /// trade-off — the "what does each of these mean" the setup was missing.
    private func modeCard(_ mode: OnboardingModel.Mode, title: String, detail: String) -> some View {
        let selected = model.mode == mode
        return HStack(alignment: .top, spacing: 9) {
            Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                .font(.system(size: 13))
                .foregroundStyle(selected ? Color.cognee : Color.secondary)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(
            selected ? Color.cognee.opacity(0.08) : Color.primary.opacity(0.03),
            in: RoundedRectangle(cornerRadius: 8, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(
                    selected ? Color.cognee.opacity(0.5) : Color.primary.opacity(0.06),
                    lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onTapGesture { model.mode = mode }
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
