import SwiftUI

// The panel's one accent: cognee purple. Everything else is system neutrals,
// so this single hue reads as "knowledge" wherever it appears — the graph
// pulse, semantic sparks, the selection bar, answers.
extension Color {
    static let cognee = Color(red: 0.545, green: 0.361, blue: 1.0)
}

/// The Spotlight-style panel: a fixed transparent canvas with a material card
/// pinned to the top. Typography does the talking — results are set like a
/// system (SF Pro + SF Mono), graph answers like a book (serif).
struct SearchView: View {
    @ObservedObject var model: SearchViewModel
    @FocusState private var fieldFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            searchField
            if let detail = model.connectionDetail {
                divider
                connectionDetailView(detail)
            }
            if let error = model.errorText {
                divider
                statusLine(icon: "bolt.horizontal.circle", text: error)
            } else if let answer = model.answer {
                divider
                answerView(answer)
            } else if !model.results.isEmpty {
                divider
                resultsList
            } else if !model.query.isEmpty, !model.isLoading {
                divider
                statusLine(
                    icon: "circle.dashed",
                    text: "Nothing matches “\(model.query)” yet — ⇧↩ asks your knowledge graph instead."
                )
            }
            if let recipients = model.shareRecipients {
                divider
                HStack(spacing: 8) {
                    Text("send to")
                        .font(.system(size: 11)).foregroundStyle(.tertiary)
                    ForEach(Array(recipients.enumerated()), id: \.offset) { index, name in
                        Text(name)
                            .font(.system(size: 12, weight: index == model.recipientIndex ? .semibold : .regular))
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(
                                index == model.recipientIndex
                                    ? AnyShapeStyle(Color.cognee.opacity(0.25))
                                    : AnyShapeStyle(.quaternary.opacity(0.4)),
                                in: Capsule()
                            )
                            .onTapGesture {
                                model.recipientIndex = index
                                model.confirmShare()
                            }
                    }
                    Spacer()
                    Text("↩ send · esc cancel")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 9)
            }
            if let toast = model.sharedToast {
                divider
                HStack(spacing: 8) {
                    Image(systemName: "paperplane.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Color.cognee)
                    Text(toast).font(.system(size: 12.5, weight: .medium))
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 9)
            }
            if !model.query.isEmpty {
                divider
                hintBar
            }
        }
        .frame(width: 680)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(
                    LinearGradient(
                        colors: [.white.opacity(0.28), .white.opacity(0.06)],
                        startPoint: .top, endPoint: .bottom
                    ),
                    lineWidth: 1
                )
        )
        .shadow(color: .black.opacity(0.38), radius: 34, y: 14)
        .padding(.horizontal, 40)
        .padding(.top, 40)  // shadow headroom
        // Fixed canvas, card pinned to the top: the window never needs to
        // resize when results arrive — growing content would otherwise render
        // beyond the window's fitted bounds and be clipped into invisibility.
        .frame(width: 760, height: 560, alignment: .top)
        .onAppear { fieldFocused = true }
        .onChange(of: model.focusGeneration) { _ in fieldFocused = true }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: model.results)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: model.answer)
    }

    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    // MARK: search field

    private var searchField: some View {
        HStack(spacing: 11) {
            ZStack {
                if model.isLoading || model.isAsking {
                    GraphPulse()  // the graph is thinking
                } else {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 22, height: 22)
            TextField("Search your knowledge…", text: $model.query)
                .textFieldStyle(.plain)
                .font(.system(size: 21))
                .focused($fieldFocused)
            if !model.connections.isEmpty {
                // what this search draws from: one quiet icon per connection;
                // click opens what it indexed and when it last synced
                HStack(spacing: 9) {
                    ForEach(model.connections) { connection in
                        ConnectionBadge(
                            connection: connection,
                            isOpen: model.connectionDetail?.id == connection.id
                        )
                        .onTapGesture { model.toggleConnectionDetail(connection) }
                    }
                }
                .padding(.leading, 4)
            }
        }
        .padding(.horizontal, 16)
        .frame(height: 50)
    }

    /// The clicked chip's contents: every root/document that connection has
    /// indexed, plus its last-sync time — the receipts behind "connected".
    private func connectionDetailView(_ connection: SourceConnection) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                Image(systemName: "point.3.connected.trianglepath.dotted")
                    .font(.system(size: 10))
                    .foregroundStyle(Color.cognee)
                Text("\(connection.label) — \(connection.count ?? 0) item(s)")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                if !connection.lastSyncText.isEmpty {
                    Text(connection.lastSyncText)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
            if let items = connection.items, !items.isEmpty {
                ForEach(items, id: \.self) { item in
                    Text(item)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if let count = connection.count, count > items.count {
                    Text("… and \(count - items.count) more")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("Nothing indexed from this source yet.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: results

    private var resultsList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 1) {
                    ForEach(Array(model.results.enumerated()), id: \.element.id) { index, result in
                        ResultRow(
                            result: result, isSelected: index == model.selectedIndex,
                            query: model.query,
                            originLabel: model.originLabel(for: result.path)
                        )
                            .id(index)
                            .onTapGesture {
                                model.selectedIndex = index
                                _ = model.openSelected()
                            }
                    }
                }
                .padding(10)
            }
            .frame(maxHeight: 372)
            .onChange(of: model.selectedIndex) { index in
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.1)) {
                    proxy.scrollTo(index)
                }
            }
        }
    }

    // MARK: answer — knowledge reads like a book

    private func answerView(_ answer: String) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Label("From your knowledge graph", systemImage: "sparkle")
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .textCase(.uppercase)
                    .kerning(0.8)
                    .foregroundStyle(Color.cognee)
                Text(Self.markdown(answer))
                    .font(.system(size: 15, design: .serif))
                    .lineSpacing(4)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !model.answerSources.isEmpty {
                    HStack(spacing: 5) {
                        Image(systemName: "point.3.connected.trianglepath.dotted")
                            .font(.system(size: 9))
                        Text(sourcesLine(model.answerSources))
                            .font(.system(size: 11))
                        Spacer()
                        if model.experimentsEnabled {
                            Button { model.rateAnswer(5) } label: {
                                Image(systemName: "hand.thumbsup")
                            }
                            .buttonStyle(.plain)
                            .help("Good answer — reinforce this in memory")
                            Button { model.rateAnswer(1) } label: {
                                Image(systemName: "hand.thumbsdown")
                            }
                            .buttonStyle(.plain)
                            .help("Wrong or unhelpful — log for correction")
                        }
                    }
                    .foregroundStyle(.tertiary)
                    .padding(.top, 2)
                }
                if model.experimentsEnabled, !model.contradictions.isEmpty {
                    HStack(spacing: 6) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.system(size: 10))
                            .foregroundStyle(.orange)
                        Text(
                            "conflicting memory: \(model.contradictions[0].a) vs \(model.contradictions[0].b)"
                        )
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    }
                    .padding(.top, 2)
                    .help("cognee never overwrites conflicting facts — it records both and flags the disagreement")
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .frame(maxHeight: 320)
    }

    // MARK: chrome

    private func statusLine(icon: String, text: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon).foregroundStyle(.tertiary)
            Text(text).foregroundStyle(.secondary)
            Spacer()
        }
        .font(.system(size: 12.5))
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var hintBar: some View {
        HStack(spacing: 16) {
            hint("↩", "Open")
            hint("⌘↩", "Reveal")
            hint("⇧↩", "Ask")
            hint("⌘S", "Share")
            hint("⌘⇧S", "Share with note…")
            Spacer()
            hint("esc", "Close")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 8)
    }

    private func hint(_ key: String, _ label: String) -> some View {
        HStack(spacing: 5) {
            // primary text + a real keycap fill: tertiary/quaternary washed
            // out to invisible against the dark translucent material
            Text(key)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(
                    Color.primary.opacity(0.1), in: RoundedRectangle(cornerRadius: 4)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 4)
                        .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
                )
                .foregroundStyle(.primary)
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    private var divider: some View {
        Divider().padding(.horizontal, 14).opacity(0.6)
    }

    /// "from 2 sources · your files + agent session" — where the answer came
    /// from, in memory-layer terms. Clients always ask; now it's on screen.
    private func sourcesLine(_ sources: [AnswerSource]) -> String {
        var layers: [String] = []
        for source in sources where !layers.contains(source.layer) {
            layers.append(source.layer)
        }
        let joined = layers.joined(separator: " + ")
        return sources.count == 1
            ? "from \(joined)"
            : "from \(sources.count) sources · \(joined)"
    }

    /// Graph answers arrive as markdown (**bold**, lists) — render the inline
    /// styles instead of showing asterisks. Falls back to plain text.
    static func markdown(_ text: String) -> AttributedString {
        (try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(text)
    }
}

// MARK: - Connection badge

/// A data source feeding memory: its icon plus a status dot (green =
/// connected, amber = sync error, gray = hasn't synced yet). Hovering
/// expands the chip inline to name it — system tooltips never appear on a
/// non-activating panel (the app stays inactive), so the label must live
/// in the view itself. Icon and label come from the backend's source
/// description, so any new connector renders here untouched.
private struct ConnectionBadge: View {
    let connection: SourceConnection
    var isOpen: Bool = false
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: symbolName)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(isOpen ? AnyShapeStyle(Color.cognee) : AnyShapeStyle(.secondary))
                .overlay(alignment: .bottomTrailing) {
                    Circle()
                        .fill(dotColor)
                        .frame(width: 5, height: 5)
                        .offset(x: 2.5, y: 2.5)
                }
            if hovering {
                Text(hoverText)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, hovering ? 7 : 0)
        .padding(.vertical, hovering ? 3 : 0)
        .background(
            hovering || isOpen
                ? AnyShapeStyle(.quaternary.opacity(0.5)) : AnyShapeStyle(.clear),
            in: Capsule()
        )
        .contentShape(Capsule())
        .onHover { inside in
            withAnimation(.easeOut(duration: 0.12)) { hovering = inside }
        }
        .accessibilityLabel("\(connection.label): \(connection.statusText)")
    }

    private var hoverText: String {
        var parts = ["\(connection.label) · \(connection.statusText)"]
        if !connection.lastSyncText.isEmpty { parts.append(connection.lastSyncText) }
        return parts.joined(separator: " · ")
    }

    /// The backend names an SF Symbol; fall back if this macOS lacks it.
    private var symbolName: String {
        NSImage(systemSymbolName: connection.icon, accessibilityDescription: nil) != nil
            ? connection.icon : "puzzlepiece.extension"
    }

    private var dotColor: Color {
        switch connection.ok {
        case true: return .green
        case false: return .orange
        default: return .gray
        }
    }
}

// MARK: - Result row

private struct ResultRow: View {
    let result: SearchResult
    let isSelected: Bool
    let query: String
    /// Which connection this memory arrived through (named by the backend).
    let originLabel: String?

    var body: some View {
        HStack(spacing: 11) {
            fileIcon
                .frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 2.5) {
                HStack(spacing: 6) {
                    Text(result.title)
                        .font(.system(size: 13.5, weight: .semibold))
                        .lineLimit(1)
                    if result.source == "semantic" {
                        // a quiet spark: this row was found by meaning
                        Image(systemName: "sparkle")
                            .font(.system(size: 9.5))
                            .foregroundStyle(Color.cognee.opacity(0.9))
                    }
                    if let origin = originLabel {
                        // which connection this memory arrived through
                        Text(origin)
                            .font(.system(size: 9, weight: .semibold))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1.5)
                            .background(Color.cognee.opacity(0.12), in: Capsule())
                            .foregroundStyle(Color.cognee)
                    }
                }
                if !result.snippet.isEmpty {
                    Text(highlighted(result.snippet))
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if !result.path.isEmpty {
                    Text(abbreviatedPath)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(isSelected ? Color.cognee.opacity(0.13) : .clear)
        )
        .overlay(alignment: .leading) {
            if isSelected {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.cognee)
                    .frame(width: 3, height: 22)
                    .padding(.leading, 2)
            }
        }
        .contentShape(Rectangle())
    }

    /// The file's real Finder icon — what makes rows read as native macOS.
    /// Snippets (passages without a local file) keep a quiet glyph tile.
    @ViewBuilder private var fileIcon: some View {
        if result.kind == "person" {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color.cognee.opacity(isSelected ? 0.25 : 0.12))
                .overlay(
                    Image(systemName: "person.crop.circle")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(Color.cognee)
                )
        } else if !result.path.isEmpty, FileManager.default.fileExists(atPath: result.path) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: result.path))
                .resizable()
                .interpolation(.high)
                .scaledToFit()
        } else {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(isSelected ? Color.cognee.opacity(0.18) : Color.primary.opacity(0.05))
                .overlay(
                    Image(systemName: "text.quote")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(isSelected ? Color.cognee : Color.secondary)
                )
        }
    }

    private var abbreviatedPath: String {
        (result.path as NSString).abbreviatingWithTildeInPath
    }

    /// Bold the query's words inside the snippet — semantic hits become
    /// scannable instead of a wall of context.
    private func highlighted(_ text: String) -> AttributedString {
        var attributed = AttributedString(text)
        let terms = query.lowercased().split(separator: " ").map(String.init)
            .filter { $0.count > 2 }
        for term in terms {
            var start = attributed.startIndex
            while start < attributed.endIndex,
                let found = attributed[start...].range(of: term, options: .caseInsensitive) {
                attributed[found].font = .system(size: 12, weight: .bold)
                attributed[found].foregroundColor = .primary
                start = found.upperBound
            }
        }
        return attributed
    }
}

// MARK: - Graph pulse

/// Three nodes and their edges, breathing while cognee works: retrieval is a
/// walk over the knowledge graph, and this is that walk, miniaturized.
private struct GraphPulse: View {
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            Canvas { canvas, size in
                let points = [
                    CGPoint(x: size.width * 0.22, y: size.height * 0.72),
                    CGPoint(x: size.width * 0.55, y: size.height * 0.2),
                    CGPoint(x: size.width * 0.82, y: size.height * 0.68),
                ]
                let phase = { (offset: Double) -> Double in
                    0.45 + 0.55 * (0.5 + 0.5 * sin(t * 2.6 + offset))
                }
                var edges = Path()
                edges.move(to: points[0])
                edges.addLine(to: points[1])
                edges.addLine(to: points[2])
                edges.addLine(to: points[0])
                canvas.stroke(
                    edges, with: .color(Color.cognee.opacity(0.35 * phase(1.2))),
                    lineWidth: 1
                )
                for (i, point) in points.enumerated() {
                    let r = 2.6 + 0.9 * phase(Double(i) * 2.1)
                    let rect = CGRect(x: point.x - r, y: point.y - r, width: r * 2, height: r * 2)
                    canvas.fill(
                        Path(ellipseIn: rect),
                        with: .color(Color.cognee.opacity(phase(Double(i) * 2.1)))
                    )
                }
            }
        }
        .frame(width: 24, height: 24)
        .accessibilityLabel("Searching the knowledge graph")
    }
}
