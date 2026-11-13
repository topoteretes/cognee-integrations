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
    /// Coding agents connected to the same memory (cloud tenant).
    @Published var agents: [AgentConnection] = []
    /// Provisioned plugin identities (servers on cognee >= 1.5.1).
    @Published var plugins: [PluginStatus] = []
    /// Mirrors whether the "Index in Cognee" Finder Quick Action is installed.
    @Published var finderIntegration: Bool = FinderIntegration.isInstalled

    func setFinderIntegration(_ enabled: Bool) {
        do {
            if enabled {
                try FinderIntegration.install()
                statusText = "Installed — select files in Finder, right-click → Quick Actions → Index in Cognee."
            } else {
                try FinderIntegration.uninstall()
                statusText = ""
            }
            finderIntegration = FinderIntegration.isInstalled
        } catch {
            statusText = "Could not update the Finder Quick Action: \(error.localizedDescription)"
            finderIntegration = FinderIntegration.isInstalled
        }
    }

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
            if let response = try? await BackendClient().agents() {
                agents = response.agents
                plugins = (response.plugins ?? []).filter(\.connected)
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

    /// Pick anything: whole folders or individual files (mixed selections
    /// work), optionally restricted to certain types — "all .pdf and .docx
    /// from this folder" is one picker visit.
    func addPaths() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        panel.prompt = "Index"
        panel.message = "Choose files or folders to index into memory"

        let filterField = NSTextField(string: "")
        filterField.placeholderString = "pdf, docx"
        filterField.frame = NSRect(x: 0, y: 0, width: 160, height: 22)
        let filterLabel = NSTextField(labelWithString: "Only these types (optional):")
        filterLabel.font = .systemFont(ofSize: 11)
        filterLabel.textColor = .secondaryLabelColor
        let scopeLabel = NSTextField(labelWithString: "Label:")
        scopeLabel.font = .systemFont(ofSize: 11)
        scopeLabel.textColor = .secondaryLabelColor
        let scopeBox = NSComboBox(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        scopeBox.addItems(withObjectValues: ["personal", "work"])
        scopeBox.placeholderString = "no label"
        scopeBox.completes = true
        let row = NSStackView(views: [filterLabel, filterField, scopeLabel, scopeBox])
        row.orientation = .horizontal
        row.spacing = 6
        row.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        panel.accessoryView = row
        panel.isAccessoryViewDisclosed = true

        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK else { return }
        let extensions = filterField.stringValue
            .split(whereSeparator: { ", ".contains($0) })
            .map { String($0).trimmingCharacters(in: CharacterSet(charactersIn: ". ")) }
            .filter { !$0.isEmpty }
        let scope = scopeBox.stringValue.trimmingCharacters(in: .whitespaces).lowercased()
        startIndex(paths: panel.urls.map(\.path), extensions: extensions, label: scope)
    }

    func reindex() {
        startIndex(paths: [])  // empty list = re-run over the roots the backend knows
    }

    func togglePause(root: String, pause: Bool) {
        Task {
            do {
                try await BackendClient().pauseRoot(path: root, paused: pause)
                refresh()
            } catch {
                statusText = "Could not change sync for \(root)"
            }
        }
    }

    /// Remove a file or a whole root from the index, after a confirmation
    /// that says exactly what will and will not be deleted.
    func forget(path: String, isRoot: Bool) {
        let alert = NSAlert()
        alert.messageText = isRoot ? "Stop watching this folder?" : "Remove from index?"
        alert.informativeText = isRoot
            ? "\((path as NSString).abbreviatingWithTildeInPath)\n\nIts files leave search results and counts. Copies already in the knowledge graph are kept (bulk graph deletion is a manual operation)."
            : "\((path as NSString).abbreviatingWithTildeInPath)\n\nThe file leaves search results, and its knowledge-graph copy is deleted when it can be identified unambiguously. The file itself is not touched."
        alert.addButton(withTitle: isRoot ? "Stop Watching" : "Remove")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            do {
                let response = try await BackendClient().forget(path: path)
                statusText = response.ok ? response.detail : "Not removed: \(response.detail)"
                refresh()
            } catch {
                statusText = "Could not remove — is the backend running?"
            }
        }
    }

    func startIndex(paths: [String], extensions: [String] = [], label: String = "") {
        Task {
            do {
                try await BackendClient().startIndex(
                    paths: paths, extensions: extensions, label: label)
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

            Section("Finder") {
                Toggle(
                    "Right-click files → “Index in Cognee”",
                    isOn: Binding(
                        get: { model.finderIntegration },
                        set: { model.setFinderIntegration($0) }
                    )
                )
                Text(
                    "Select any files or folders in Finder, right-click → Quick Actions → Index in Cognee — they index without opening this window. Re-toggle after changing the backend URL."
                )
                .font(.caption)
                .foregroundStyle(.tertiary)
            }

            Section("Shortcut") {
                LabeledContent("Open search") { Text("⌥ Space") }
            }
        }
        .formStyle(.grouped)
        .frame(width: 500, height: 420)
        .onAppear { model.refresh() }
    }
}
