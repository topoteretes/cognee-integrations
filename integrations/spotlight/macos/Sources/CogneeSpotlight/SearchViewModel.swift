import AppKit
import Combine
import Foundation

/// Drives the search panel: debounced search-as-you-type against the backend,
/// arrow-key selection, and an explicit "ask" mode (Shift+Return) that trades
/// instant file results for an LLM answer over the knowledge graph.
@MainActor
final class SearchViewModel: ObservableObject {
    @Published var query: String = "" {
        didSet { scheduleSearch() }
    }
    @Published var results: [SearchResult] = []
    @Published var answer: String?
    @Published var selectedIndex: Int = 0
    @Published var isLoading = false
    @Published var isAsking = false
    @Published var errorText: String?
    /// Set briefly after a quick-share so the panel can toast "Shared with …".
    @Published var sharedToast: String?
    /// Which memory layers the current answer drew from.
    @Published var answerSources: [AnswerSource] = []
    /// Conflicting facts touching the answer's topic (experiments only).
    @Published var contradictions: [Contradiction] = []
    /// Whether the backend runs with latent features enabled.
    @Published var experimentsEnabled = false
    /// The data sources feeding memory (folders, Slack, Drive, …), shown as
    /// chips in the panel so it's visible what a search draws from.
    @Published var connections: [SourceConnection] = []
    /// One conversation per panel appearance: follow-up ⇧↩ questions thread.
    private(set) var threadID = UUID().uuidString
    /// Bumped every time the panel is shown so the view can re-grab focus.
    @Published var focusGeneration: Int = 0

    private var searchTask: Task<Void, Never>?
    private var instantTask: Task<Void, Never>?

    /// Breadcrumbs to /tmp when SPOTLIGHT_DEBUG_QUERY is set — the panel has
    /// no console, so silent failures need a paper trail.
    static let debugging = ProcessInfo.processInfo.environment["SPOTLIGHT_DEBUG_QUERY"] != nil

    static func debugLog(_ message: String) {
        guard debugging else { return }
        let line = "\(Date()) \(message)\n"
        if let data = line.data(using: .utf8),
            let handle = FileHandle(forWritingAtPath: "/tmp/cognee-spotlight-app.log") {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? line.write(
                toFile: "/tmp/cognee-spotlight-app.log", atomically: false, encoding: .utf8)
        }
    }

    /// Which connection a result arrived through, derived from its staging
    /// path (`<data_dir>/sources/<name>/…`) and named by the backend's own
    /// source description — no connector names hardcoded here.
    func originLabel(for path: String) -> String? {
        if let range = path.range(of: "/sources/") {
            let name = String(path[range.upperBound...].prefix(while: { $0 != "/" }))
            if !name.isEmpty {
                return connections.first(where: { $0.name == name })?.label ?? name.capitalized
            }
        }
        if path.contains("/.cognee-spotlight/"), path.contains("/capture/") {
            return "Captured"
        }
        return nil
    }

    func reset() {
        query = ""
        results = []
        answer = nil
        answerSources = []
        contradictions = []
        errorText = nil
        selectedIndex = 0
        isAsking = false
        threadID = UUID().uuidString  // a fresh panel is a fresh conversation
        focusGeneration += 1
        Task { [weak self] in
            if let health = try? await BackendClient().health() {
                self?.experimentsEnabled = health.experiments ?? false
            }
            if let sources = try? await BackendClient().sources() {
                self?.connections = sources.sources
            }
        }
    }

    /// Two-phase search, so typing always feels instant even though semantic
    /// retrieval costs seconds per query (cognee boots a DB worker each time):
    /// every keystroke fetches filename matches (milliseconds); a longer
    /// debounce then runs the full search and replaces the list when its
    /// richer results (snippets, semantic hits) arrive for the same query.
    private func scheduleSearch() {
        answer = nil
        answerSources = []
        searchTask?.cancel()
        instantTask?.cancel()
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else {
            results = []
            isLoading = false
            errorText = nil
            return
        }
        Self.debugLog("scheduleSearch q=\(q)")
        // "who knows …" routes to expert finding instead of file search
        if q.lowercased().hasPrefix("who knows") {
            instantTask = Task { [weak self] in
                guard let self,
                    let response = try? await BackendClient().experts(q),
                    !Task.isCancelled, self.isCurrent(q)
                else { return }
                self.results = response.experts.map { expert in
                    SearchResult(
                        kind: "person", title: expert.name,
                        path: "",
                        snippet: "\(expert.evidence) matching memories",
                        score: Double(expert.evidence), source: "experts")
                }
                self.selectedIndex = 0
                self.errorText = nil
            }
            return
        }
        instantTask = Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await BackendClient().search(q, semantic: false)
                Self.debugLog(
                    "instant q=\(q) results=\(response.results.count) cancelled=\(Task.isCancelled) current=\(self.isCurrent(q))"
                )
                guard !Task.isCancelled, self.isCurrent(q) else { return }
                self.results = response.results
                Self.debugLog("instant applied; model.results=\(self.results.count)")
                self.selectedIndex = 0
                self.errorText = nil
            } catch {
                Self.debugLog("instant q=\(q) ERROR \(error)")
            }
        }
        searchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)  // settle before the heavy query
            guard let self, !Task.isCancelled else { return }
            self.requestFull(q)
        }
    }

    /// Single-flight semantic search: at most one full query runs at a time;
    /// while one is in flight, newer queries just replace the "next up" slot.
    /// Firing one per keystroke would abandon requests whose semantic work
    /// keeps running on the backend, starving the query the user actually
    /// wants an answer to.
    private var fullTask: Task<Void, Never>?
    private var pendingFullQuery: String?

    private func requestFull(_ q: String) {
        guard fullTask == nil else {
            pendingFullQuery = q
            return
        }
        isLoading = true
        fullTask = Task { [weak self] in
            let response = try? await BackendClient().search(q)
            guard let self else { return }
            Self.debugLog("full q=\(q) results=\(response?.results.count ?? -1) current=\(self.isCurrent(q))")
            self.fullTask = nil
            if let response, self.isCurrent(q) {
                self.results = response.results
                self.selectedIndex = min(self.selectedIndex, max(response.results.count - 1, 0))
                self.errorText = nil
            } else if response == nil, self.isCurrent(q), self.results.isEmpty {
                self.errorText = "Backend unreachable — start it with scripts/run_backend.sh"
            }
            if let pending = self.pendingFullQuery {
                self.pendingFullQuery = nil
                if pending != q, self.isCurrent(pending) {
                    self.requestFull(pending)
                    return
                }
            }
            self.isLoading = false
        }
    }

    private func isCurrent(_ q: String) -> Bool {
        q == query.trimmingCharacters(in: .whitespaces)
    }

    /// Shift+Return: ask cognee for an answer instead of file matches.
    func ask() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        searchTask?.cancel()
        isAsking = true
        isLoading = true
        answer = nil
        searchTask = Task { [weak self] in
            guard let self else { return }
            defer { self.isLoading = false }
            do {
                let response = try await BackendClient().search(
                    q, mode: "answer", thread: self.threadID)
                guard !Task.isCancelled else { return }
                self.answer = response.answer ?? "No answer found in the index yet."
                self.answerSources = response.sources ?? []
                self.contradictions = response.contradictions ?? []
                self.errorText = nil
            } catch {
                guard !Task.isCancelled else { return }
                self.errorText = "Backend unreachable — start it with scripts/run_backend.sh"
            }
        }
    }

    // -- inline share picker ---------------------------------------------------
    /// Non-nil while the ⌘S recipient picker is showing.
    @Published var shareRecipients: [String]?
    @Published var recipientIndex: Int = 0

    /// ⌘S: open the inline recipient picker for the current answer / selected
    /// result. Returns false when there is nothing to share (caller can fall
    /// back to the full share sheet).
    func startShare() -> Bool {
        guard sharePayload() != nil else { return false }
        let recipients = Preferences.shareRecipients
        shareRecipients = recipients
        recipientIndex = max(recipients.firstIndex(of: Preferences.defaultShareRecipient) ?? 0, 0)
        return true
    }

    func moveRecipient(by delta: Int) {
        guard let recipients = shareRecipients, !recipients.isEmpty else { return }
        recipientIndex = (recipientIndex + delta + recipients.count) % recipients.count
    }

    func cancelShare() {
        shareRecipients = nil
    }

    /// ↩ in the picker: send to the highlighted recipient, right here.
    func confirmShare() {
        guard let recipients = shareRecipients,
            recipients.indices.contains(recipientIndex),
            let payload = sharePayload()
        else {
            shareRecipients = nil
            return
        }
        let recipient = recipients[recipientIndex]
        shareRecipients = nil
        Preferences.defaultShareRecipient = recipient
        sharedToast = "Sharing with \(recipient)…"
        Task { [weak self] in
            do {
                try await BackendClient().share(
                    to: recipient, title: payload.title, body: payload.body,
                    source: payload.source)
                self?.sharedToast = "Shared with \(recipient) ✓"
            } catch {
                self?.sharedToast = "Share failed — is the backend up?"
            }
            try? await Task.sleep(nanoseconds: 2_200_000_000)
            self?.sharedToast = nil
        }
    }

    /// 👍/👎 under an answer (experiments): positive ratings reinforce memory.
    func rateAnswer(_ rating: Int) {
        guard let answer = answer else { return }
        let q = query
        sharedToast = rating >= 4 ? "Reinforcing memory…" : "Noted 👍"
        Task { [weak self] in
            try? await BackendClient().sendFeedback(query: q, answer: answer, rating: rating)
            self?.sharedToast = rating >= 4 ? "Confirmed — memory reinforced ✓" : "Feedback logged ✓"
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self?.sharedToast = nil
        }
    }

    private func sharePayload() -> (title: String, body: String, source: String)? {
        if let answer = answer {
            return (query, answer, "spotlight answer")
        }
        if results.indices.contains(selectedIndex) {
            let result = results[selectedIndex]
            return (
                result.title,
                result.snippet.isEmpty ? result.path : result.snippet,
                result.path
            )
        }
        return nil
    }

    // -- keyboard navigation -------------------------------------------------
    func moveSelection(by delta: Int) {
        guard !results.isEmpty else { return }
        selectedIndex = min(max(selectedIndex + delta, 0), results.count - 1)
    }

    /// Return key. Reports whether the panel should close.
    func openSelected(revealInFinder: Bool = false) -> Bool {
        guard results.indices.contains(selectedIndex) else { return false }
        let result = results[selectedIndex]
        guard !result.path.isEmpty else { return false }
        let url = URL(fileURLWithPath: result.path)
        if revealInFinder {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(url)
        }
        return true
    }
}
