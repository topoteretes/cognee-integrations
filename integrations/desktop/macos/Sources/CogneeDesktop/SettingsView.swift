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

    private var pollTask: Task<Void, Never>?

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

    func addFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Index"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK else { return }
        let paths = panel.urls.map(\.path)
        startIndex(paths: paths)
    }

    func reindex() {
        startIndex(paths: [])  // empty list = re-run over the roots the backend knows
    }

    private func startIndex(paths: [String]) {
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
                    Button("Add Folder…") { model.addFolder() }
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
            }

            Section("Shortcut") {
                LabeledContent("Open search") { Text("⌥ Space") }
            }
        }
        .formStyle(.grouped)
        .frame(width: 460, height: 380)
        .onAppear { model.refresh() }
    }
}
