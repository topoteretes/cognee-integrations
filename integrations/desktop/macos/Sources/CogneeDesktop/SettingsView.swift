import AppKit
import SwiftUI

/// Settings: which backend to talk to, which folders are indexed, and a
/// reindex trigger with live progress. All state lives in the backend; this
/// view is a remote control for it.
@MainActor
final class SettingsModel: ObservableObject {
    @Published var backendURLText: String = Preferences.backendURL.absoluteString
    @Published var health: Health?
    @Published var progress: IndexProgress?
    @Published var statusText: String = ""
    /// The actual indexed files (newest first), filterable — the answer to
    /// "which files do I have in here?"
    @Published var indexedFiles: [IndexedFile] = []
    @Published var filesTotal: Int = 0
    @Published var filesMatched: Int = 0
    @Published var filesFilter: String = "" {
        didSet { scheduleFilesLoad() }
    }
    /// Every configured data source, as the backend describes it.
    @Published var connections: [SourceConnection] = []

    private var pollTask: Task<Void, Never>?
    private var filesTask: Task<Void, Never>?

    func refresh() {
        Task {
            do {
                health = try await BackendClient().health()
                progress = try await BackendClient().indexStatus()
                statusText = ""
            } catch {
                health = nil
                statusText = "Backend unreachable at \(Preferences.backendURL.absoluteString)"
            }
            if let sources = try? await BackendClient().sources() {
                connections = sources.sources
            }
        }
        loadFiles()
    }

    func loadFiles() {
        Task {
            guard let response = try? await BackendClient().files(q: filesFilter, limit: 200)
            else { return }
            indexedFiles = response.files
            filesTotal = response.total
            filesMatched = response.matched
        }
    }

    private func scheduleFilesLoad() {
        filesTask?.cancel()
        filesTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard !Task.isCancelled else { return }
            self?.loadFiles()
        }
    }

    func saveBackendURL() {
        guard let url = URL(string: backendURLText), url.scheme != nil else {
            statusText = "Not a valid URL"
            return
        }
        Preferences.backendURL = url
        refresh()
    }

    /// Pick anything: whole folders or individual files (mixed selections work).
    func addPaths() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        panel.prompt = "Index"
        panel.message = "Choose files or folders to index into memory"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK else { return }
        startIndex(paths: panel.urls.map(\.path))
    }

    func reindex() {
        startIndex(paths: [])  // empty list = re-run over the roots the backend knows
    }

    func startIndex(paths: [String]) {
        Task {
            do {
                try await BackendClient().startIndex(paths: paths)
                pollProgress()
            } catch {
                statusText = "Could not start indexing — is the backend running?"
            }
        }
    }

    private func pollProgress() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                do {
                    let p = try await BackendClient().indexStatus()
                    progress = p
                    if p.state == "idle" || p.state == "error" { break }
                } catch { break }
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
            refresh()
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    private var skippedSuffix: String {
        let skipped = model.progress?.skipped ?? 0
        return skipped > 0 ? " · \(skipped) skipped" : ""
    }

    /// The done/total counter only tracks the "adding" phase; cognify is one
    /// long opaque LLM pipeline, so give it words instead of a stuck counter.
    private func progressLabel(_ p: IndexProgress) -> String {
        switch p.state {
        case "scanning": return "scanning folders…"
        case "adding": return "adding \(p.done)/\(p.total)\(skippedSuffix)"
        case "cognifying":
            return "building knowledge graph for \(p.total) files\(skippedSuffix) — this can take a while"
        default: return p.state
        }
    }

    var body: some View {
        Form {
            Section("Backend") {
                HStack {
                    TextField("Backend URL", text: $model.backendURLText)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { model.saveBackendURL() }
                    Button("Save") { model.saveBackendURL() }
                }
                LabeledContent("Status") {
                    if let health = model.health {
                        Label(
                            "\(health.mode) · \(health.indexed_files) files indexed",
                            systemImage: "circle.fill"
                        )
                        .foregroundStyle(.green)
                    } else {
                        Label("offline", systemImage: "circle.fill").foregroundStyle(.red)
                    }
                }
                if !model.statusText.isEmpty {
                    Text(model.statusText).font(.callout).foregroundStyle(.orange)
                }
            }

            Section("Indexed folders") {
                let roots = model.progress?.roots ?? []
                if roots.isEmpty {
                    Text("Nothing indexed yet. Add a folder to get started.")
                        .foregroundStyle(.secondary)
                }
                ForEach(roots, id: \.self) { root in
                    Text((root as NSString).abbreviatingWithTildeInPath)
                        .font(.system(.body, design: .monospaced))
                }
                HStack {
                    Button("Add Files or Folders…") { model.addPaths() }
                    Button("Reindex") { model.reindex() }
                        .disabled(roots.isEmpty)
                    Spacer()
                    if let p = model.progress, p.state != "idle" {
                        if p.state == "error" {
                            Text("Error: \(p.error)").foregroundStyle(.red).font(.callout)
                        } else {
                            ProgressView().controlSize(.small)
                            Text(progressLabel(p))
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    } else if let p = model.progress, (p.skipped ?? 0) > 0 {
                        Text("\(p.skipped!) unsupported files skipped")
                            .font(.callout).foregroundStyle(.orange)
                            .help(p.last_skip ?? "")
                    }
                }
                Text("Tip: you can also drop files or folders anywhere on this window.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }

            Section("Indexed files") {
                HStack {
                    TextField("Filter by name or path…", text: $model.filesFilter)
                        .textFieldStyle(.roundedBorder)
                    Text(
                        model.filesFilter.isEmpty
                            ? "\(model.filesTotal) files"
                            : "\(model.filesMatched) of \(model.filesTotal)"
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
                if model.indexedFiles.isEmpty {
                    Text(model.filesFilter.isEmpty ? "No files indexed yet." : "No matches.")
                        .foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 3) {
                            ForEach(model.indexedFiles) { file in
                                HStack(spacing: 7) {
                                    Image(
                                        nsImage: NSWorkspace.shared.icon(forFile: file.path)
                                    )
                                    .resizable()
                                    .frame(width: 14, height: 14)
                                    Text(file.name)
                                        .font(.system(size: 11.5))
                                        .lineLimit(1)
                                    Text(
                                        (file.path as NSString).abbreviatingWithTildeInPath
                                    )
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                    Spacer(minLength: 0)
                                }
                                .contentShape(Rectangle())
                                .onTapGesture(count: 2) {
                                    NSWorkspace.shared.activateFileViewerSelecting(
                                        [URL(fileURLWithPath: file.path)])
                                }
                                .help("Double-click to reveal in Finder")
                            }
                        }
                    }
                    .frame(height: 150)
                }
            }

            Section("Connections") {
                if model.connections.isEmpty {
                    Text("No data sources configured.").foregroundStyle(.secondary)
                }
                ForEach(model.connections) { connection in
                    HStack(spacing: 9) {
                        Image(systemName: connection.icon)
                            .font(.system(size: 13))
                            .foregroundStyle(Color.cognee)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 6) {
                                Text(connection.label)
                                    .font(.system(size: 12.5, weight: .medium))
                                Circle()
                                    .fill(connection.ok == true ? Color.green : Color.orange)
                                    .frame(width: 6, height: 6)
                            }
                            Text(
                                (connection.scope ?? []).map {
                                    ($0 as NSString).abbreviatingWithTildeInPath
                                }.joined(separator: " · ")
                            )
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 1) {
                            if let count = connection.count {
                                Text("\(count) item\(count == 1 ? "" : "s")")
                                    .font(.system(size: 11))
                            }
                            Text(connection.lastSyncText)
                                .font(.system(size: 10))
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.vertical, 1)
                }
                Text(
                    "Sources are configured in the backend's env file for now — Slack (SLACK_TOKEN), Google Drive (GDRIVE_ACCESS_TOKEN), GitHub (GITHUB_REPOS)."
                )
                .font(.caption)
                .foregroundStyle(.tertiary)
            }

            Section("Shortcut") {
                LabeledContent("Open search") { Text("⌥ Space") }
            }
        }
        .formStyle(.grouped)
        .frame(width: 500, height: 700)
        .onAppear { model.refresh() }
        // Drag files/folders from Finder anywhere onto the window to index them.
        .onDrop(of: [.fileURL], isTargeted: nil) { providers in
            Task { @MainActor in
                var paths: [String] = []
                for provider in providers {
                    if let data = try? await provider.loadItem(
                        forTypeIdentifier: "public.file-url", options: nil) as? Data,
                        let url = URL(dataRepresentation: data, relativeTo: nil)
                    {
                        paths.append(url.path)
                    }
                }
                if !paths.isEmpty { model.startIndex(paths: paths) }
            }
            return true
        }
    }
}
