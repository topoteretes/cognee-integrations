import AppKit
import SwiftUI

/// The state of your memory, in one window: what's indexed, what's
/// connected, which agents are wired in. Settings keeps configuration;
/// this is the observability side, split out so neither crowds the other.
struct StatusView: View {
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
            Section("Indexed folders") {
                let roots = model.progress?.roots ?? []
                if roots.isEmpty {
                    Text("Nothing indexed yet. Add a folder to get started.")
                        .foregroundStyle(.secondary)
                }
                ForEach(roots, id: \.self) { root in
                    let paused = model.progress?.paused_roots?.contains(root) == true
                    HStack {
                        Text((root as NSString).abbreviatingWithTildeInPath)
                            .font(.system(.body, design: .monospaced))
                            .foregroundStyle(paused ? .secondary : .primary)
                        if let scope = model.progress?.root_labels?[root], !scope.isEmpty {
                            Text(scope)
                                .font(.system(size: 10, weight: .semibold))
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1.5)
                                .background(MemoryLabel.color(scope).opacity(0.15), in: Capsule())
                                .foregroundStyle(MemoryLabel.color(scope))
                        }
                        if let filter = model.progress?.root_filters?[root], !filter.isEmpty {
                            Text(filter.joined(separator: " "))
                                .font(.system(size: 10, weight: .semibold))
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1.5)
                                .background(Color.cognee.opacity(0.12), in: Capsule())
                                .foregroundStyle(Color.cognee)
                                .help("Only these types index under this folder")
                        }
                        if paused {
                            Text("paused")
                                .font(.system(size: 10, weight: .semibold))
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1.5)
                                .background(Color.orange.opacity(0.15), in: Capsule())
                                .foregroundStyle(Color.orange)
                        }
                        Spacer()
                        Button {
                            model.togglePause(root: root, pause: !paused)
                        } label: {
                            Image(systemName: paused ? "play.circle" : "pause.circle")
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .help(
                            paused
                                ? "Resume live sync — new and changed files index again"
                                : "Pause live sync — stays searchable, stops updating")
                        Button {
                            model.forget(path: root, isRoot: true)
                        } label: {
                            Image(systemName: "xmark.circle")
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .help("Stop watching this folder")
                    }
                }
                HStack {
                    Button("Add Files or Folders…") { model.addPaths() }
                    Button("Reindex") { model.reindex() }
                        .disabled((model.progress?.roots ?? []).isEmpty)
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
                                    Button {
                                        model.forget(path: file.path, isRoot: false)
                                    } label: {
                                        Image(systemName: "xmark.circle")
                                            .font(.system(size: 11))
                                    }
                                    .buttonStyle(.plain)
                                    .foregroundStyle(.tertiary)
                                    .help("Remove from index")
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

            Section("Connected agents") {
                ForEach(model.plugins) { plugin in
                    HStack(spacing: 9) {
                        Image(systemName: "key.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(Color.cognee)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 6) {
                                Text(plugin.label)
                                    .font(.system(size: 12.5, weight: .medium))
                                Circle().fill(Color.green).frame(width: 6, height: 6)
                            }
                            Text("own identity (\(plugin.source))")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(.vertical, 1)
                }
                if model.agents.isEmpty, model.plugins.isEmpty {
                    Text(
                        "No coding agents connected. Claude Code and Codex sessions using the cognee plugin appear here."
                    )
                    .foregroundStyle(.secondary)
                    .font(.callout)
                }
                ForEach(model.agents.prefix(6)) { agent in
                    HStack(spacing: 9) {
                        Image(systemName: "cpu")
                            .font(.system(size: 13))
                            .foregroundStyle(Color.cognee)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 6) {
                                Text(agent.label)
                                    .font(.system(size: 12.5, weight: .medium))
                                Circle()
                                    .fill(agent.status == "active" ? Color.green : Color.gray)
                                    .frame(width: 6, height: 6)
                            }
                            Text(
                                agent.datasets.isEmpty
                                    ? agent.session
                                    : "writes to " + agent.datasets.joined(separator: ", ")
                            )
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        }
                        Spacer()
                        Text(agent.lastActiveText)
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 1)
                }
                if model.agents.count > 6 {
                    Text("… and \(model.agents.count - 6) more sessions")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 640)
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

/// Colors for memory-scope labels: the two suggestions get stable brand-ish
/// hues; any custom label gets a deterministic hue from its own name.
enum MemoryLabel {
    static func color(_ label: String) -> Color {
        switch label.lowercased() {
        case "work": return .blue
        case "personal": return .green
        default:
            let hue = Double(abs(label.lowercased().hashValue) % 360) / 360.0
            return Color(hue: hue, saturation: 0.55, brightness: 0.72)
        }
    }
}
