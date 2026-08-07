import Foundation

/// Profiles let two people (or two cloud accounts) use the app on one Mac:
/// each profile owns a backend config, data dir, and port. "default" is the
/// original single-user layout; extra profiles live under
/// ~/.cognee-spotlight/profiles/<name>/.
enum Profiles {
    static let basePort = 8765

    static var root: String {
        (NSHomeDirectory() as NSString).appendingPathComponent(".cognee-spotlight/profiles")
    }

    static var active: String {
        get { UserDefaults.standard.string(forKey: "activeProfile") ?? "default" }
        set { UserDefaults.standard.set(newValue, forKey: "activeProfile") }
    }

    static func list() -> [String] {
        let extras =
            (try? FileManager.default.contentsOfDirectory(atPath: root))?
            .filter { name in
                var isDir: ObjCBool = false
                FileManager.default.fileExists(
                    atPath: (root as NSString).appendingPathComponent(name), isDirectory: &isDir)
                return isDir.boolValue && !name.hasPrefix(".")
            }
            .sorted() ?? []
        return ["default"] + extras
    }

    static func configPath(_ name: String) -> String {
        name == "default"
            ? (NSHomeDirectory() as NSString).appendingPathComponent(
                ".cognee-spotlight/backend.env")
            : (root as NSString).appendingPathComponent("\(name)/backend.env")
    }

    static func port(_ name: String) -> Int {
        if let text = try? String(contentsOfFile: configPath(name), encoding: .utf8) {
            for line in text.split(separator: "\n") where line.hasPrefix("SPOTLIGHT_PORT=") {
                let value = line.dropFirst("SPOTLIGHT_PORT=".count)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "'\" "))
                if let port = Int(value) { return port }
            }
        }
        // deterministic assignment: default 8765, extras 8766+ in list order
        if name == "default" { return basePort }
        let extras = list().filter { $0 != "default" }
        let index = extras.firstIndex(of: name) ?? extras.count
        return basePort + 1 + index

    }

    static func url(_ name: String) -> URL {
        URL(string: "http://127.0.0.1:\(port(name))")!
    }

    /// Create the profile directory (config comes later via Setup).
    static func create(_ name: String) throws {
        try FileManager.default.createDirectory(
            atPath: (root as NSString).appendingPathComponent(name),
            withIntermediateDirectories: true
        )
    }

    static func isConfigured(_ name: String) -> Bool {
        FileManager.default.fileExists(atPath: configPath(name))
    }

    /// Switch the app to a profile: point the client at its backend.
    static func activate(_ name: String) {
        active = name
        Preferences.backendURL = url(name)
    }
}
