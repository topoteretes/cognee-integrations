import Foundation

// Mirrors the backend's JSON (see backend/desktop_backend/server.py).

struct SearchResult: Decodable, Identifiable, Equatable {
    let kind: String
    let title: String
    let path: String
    let snippet: String
    let score: Double
    let source: String

    var id: String { path.isEmpty ? title + snippet : path }
}

struct AnswerSource: Decodable, Equatable {
    let dataset: String
    let layer: String
}

struct Contradiction: Decodable, Equatable {
    let a: String
    let b: String
    let relation: String
    let dataset: String
}

struct Expert: Decodable, Identifiable {
    let name: String
    let evidence: Int
    var id: String { name }
}

struct ExpertsResponse: Decodable {
    let query: String
    let experts: [Expert]
}

struct SearchResponse: Decodable {
    let query: String
    let answer: String?
    let sources: [AnswerSource]?
    let contradictions: [Contradiction]?
    let results: [SearchResult]
}

struct IndexProgress: Decodable {
    let state: String
    let total: Int
    let done: Int
    let error: String
    let skipped: Int?
    let last_skip: String?
    let roots: [String]?
    let indexed_files: Int?
}

/// One connected data source, exactly as the backend describes it — the
/// backend owns the catalog of connectors (name, display label, SF Symbol),
/// so new sources appear in the panel without app changes.
struct SourceConnection: Decodable, Identifiable, Equatable {
    let name: String  // backend connector name: folders | slack | gdrive | …
    let label: String
    let icon: String  // SF Symbol name reported by the backend
    let ok: Bool?  // nil until the first sync reports in
    let detail: String?
    let at: Double?  // unix time of the last sync
    let scope: [String]?  // what "connected" covers: channels, repos, roots
    let items: [String]?  // what it indexed: folder roots / staged documents
    let count: Int?  // total item count (items is display-capped)

    var id: String { name }

    var statusText: String {
        switch ok {
        case true: return "connected"
        case false: return "sync error"
        default: return "waiting for first sync"
        }
    }

    /// "synced just now" / "synced 4m ago" — the last-sync timestamp,
    /// humanized. Empty until the first sync reports.
    var lastSyncText: String {
        guard let at else { return "" }
        let seconds = max(0, Date().timeIntervalSince1970 - at)
        switch seconds {
        case ..<60: return "synced just now"
        case ..<3600: return "synced \(Int(seconds / 60))m ago"
        case ..<86400: return "synced \(Int(seconds / 3600))h ago"
        default: return "synced \(Int(seconds / 86400))d ago"
        }
    }
}

struct SourcesResponse: Decodable {
    let sources: [SourceConnection]
    let interval: Double
}

/// Memory's reaction to a note being typed: closest known facts + conflicts.
struct WhisperResponse: Decodable {
    let related: [String]
    let conflicts: [Contradiction]
}

/// What the agent-session layer learned since a given moment.
struct DigestResponse: Decodable {
    let count: Int
    let titles: [String]
}

struct Health: Decodable {
    let status: String
    let mode: String
    let dataset: String
    let indexed_files: Int
    let experiments: Bool?
    let handover: HandoverIdentity?
}

struct HandoverIdentity: Decodable {
    let user: String
    let team: String
}

struct HandoverItem: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let layer: String  // inbox | team | org
    let created_at: String
    let seen: Bool
    let body: String

    /// "20260728-171530--from-vasilije--deploy-runbook.md" -> ("deploy runbook", "vasilije")
    var title: String {
        let stem = name.hasSuffix(".md") ? String(name.dropLast(3)) : name
        let parts = stem.components(separatedBy: "--")
        let raw = parts.last ?? stem
        return raw.replacingOccurrences(of: "-", with: " ")
    }

    var sender: String {
        let part = name.components(separatedBy: "--").first { $0.hasPrefix("from-") }
        return part.map { String($0.dropFirst(5)) } ?? "?"
    }
}

struct InboxResponse: Decodable {
    let items: [HandoverItem]
    let unseen: Int
    let enabled: Bool?
}

/// Thin async client for the local backend. The base URL is user-configurable
/// (Settings window), so a backend on another port -- or another machine -- works.
struct BackendClient {
    var baseURL: URL

    init(baseURL: URL? = nil) {
        self.baseURL = baseURL ?? Preferences.backendURL
    }

    func search(
        _ query: String, mode: String = "files", semantic: Bool = true, thread: String = ""
    ) async throws -> SearchResponse {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("search"), resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "mode", value: mode),
            URLQueryItem(name: "semantic", value: semantic ? "1" : "0"),
        ]
        if !thread.isEmpty {
            components.queryItems?.append(URLQueryItem(name: "thread", value: thread))
        }
        return try await get(components.url!)
    }

    func experts(_ query: String) async throws -> ExpertsResponse {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("experts"), resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "q", value: query)]
        return try await get(components.url!)
    }

    func sendFeedback(query: String, answer: String, rating: Int) async throws {
        try await post("feedback", body: ["query": query, "answer": answer, "rating": rating])
    }

    func capture(text: String, title: String = "", source: String = "") async throws {
        try await post("capture", body: ["text": text, "title": title, "source": source])
    }

    func health() async throws -> Health {
        try await get(baseURL.appendingPathComponent("health"))
    }

    func sources() async throws -> SourcesResponse {
        try await get(baseURL.appendingPathComponent("sources"))
    }

    func whisper(_ text: String) async throws -> WhisperResponse {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("whisper"), resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "q", value: text)]
        return try await get(components.url!)
    }

    func digest(since: Double) async throws -> DigestResponse {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("digest"), resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "since", value: String(since))]
        return try await get(components.url!)
    }

    func indexStatus() async throws -> IndexProgress {
        try await get(baseURL.appendingPathComponent("index/status"))
    }

    func startIndex(paths: [String]) async throws {
        try await post("index", body: ["paths": paths])
    }

    func share(to: String, title: String, body: String, source: String = "") async throws {
        try await post(
            "share", body: ["to": to, "title": title, "body": body, "source": source]
        )
    }

    func inbox() async throws -> InboxResponse {
        try await get(baseURL.appendingPathComponent("inbox"))
    }

    func markSeen(ids: [String]) async throws {
        try await post("inbox/seen", body: ["ids": ids])
    }

    private func post(_ path: String, body: [String: Any]) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 120
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (_, response) = try await URLSession.shared.data(for: request)
        try Self.check(response)
    }

    private func get<T: Decodable>(_ url: URL) async throws -> T {
        var request = URLRequest(url: url)
        request.timeoutInterval = 120
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.check(response)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private static func check(_ response: URLResponse) throws {
        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            throw URLError(.badServerResponse)
        }
    }
}

enum Preferences {
    private static let backendURLKey = "backendURL"

    /// Who the ⌘S picker highlights first; updated on every send.
    static var defaultShareRecipient: String {
        get { UserDefaults.standard.string(forKey: "defaultShareRecipient") ?? "boris" }
        set { UserDefaults.standard.set(newValue, forKey: "defaultShareRecipient") }
    }

    /// The ⌘S picker's choices. People first, then scopes.
    static var shareRecipients: [String] {
        get {
            UserDefaults.standard.stringArray(forKey: "shareRecipients")
                ?? ["boris", "alex", "priya", "team:core", "org"]
        }
        set { UserDefaults.standard.set(newValue, forKey: "shareRecipients") }
    }

    static var backendURL: URL {
        get {
            if let raw = UserDefaults.standard.string(forKey: backendURLKey),
               let url = URL(string: raw) {
                return url
            }
            return URL(string: "http://127.0.0.1:8765")!
        }
        set { UserDefaults.standard.set(newValue.absoluteString, forKey: backendURLKey) }
    }
}
