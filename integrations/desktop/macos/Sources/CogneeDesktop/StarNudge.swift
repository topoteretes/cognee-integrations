import AppKit

/// A one-time "star us on GitHub" ask, earned rather than begged for:
/// it appears only after the app has actually been useful (25 searches
/// across at least 3 days), fires after the panel closes — never during
/// a search — and both "later" (2 weeks) and "never" are respected.
@MainActor
enum StarNudge {
    static let repoURL = URL(string: "https://github.com/topoteretes/cognee")!

    private static let usesKey = "starNudgeUses"
    private static let firstUseKey = "starNudgeFirstUse"
    private static let stateKey = "starNudgeState"  // "" | "later:<ts>" | "done" | "never"

    private static let minUses = 25
    private static let minDays: Double = 3
    private static let laterDays: Double = 14

    /// Call on every genuinely useful moment (a search that returned
    /// results, an answer, a saved note).
    static func recordUse() {
        let defaults = UserDefaults.standard
        if defaults.double(forKey: firstUseKey) == 0 {
            defaults.set(Date().timeIntervalSince1970, forKey: firstUseKey)
        }
        defaults.set(defaults.integer(forKey: usesKey) + 1, forKey: usesKey)
    }

    /// Call when the panel closes; shows the ask if it has been earned.
    static func maybePrompt() {
        guard isEligible else { return }
        // a beat after the panel fades, so the alert never fights it for key
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { prompt() }
    }

    private static var isEligible: Bool {
        let defaults = UserDefaults.standard
        let state = defaults.string(forKey: stateKey) ?? ""
        if state == "done" || state == "never" { return false }
        if state.hasPrefix("later:") {
            let ts = Double(state.dropFirst("later:".count)) ?? 0
            if Date().timeIntervalSince1970 - ts < laterDays * 86400 { return false }
        }
        guard defaults.integer(forKey: usesKey) >= minUses else { return false }
        let firstUse = defaults.double(forKey: firstUseKey)
        return firstUse > 0 && Date().timeIntervalSince1970 - firstUse >= minDays * 86400
    }

    private static func prompt() {
        let alert = NSAlert()
        alert.messageText = "Enjoying Cognee?"
        alert.informativeText =
            "You've searched your memory \(UserDefaults.standard.integer(forKey: usesKey)) times. "
            + "If it's earning its place on your Mac, a star on GitHub genuinely helps the project."
        alert.addButton(withTitle: "Star on GitHub ★")
        alert.addButton(withTitle: "Maybe Later")
        alert.addButton(withTitle: "Don't Ask Again")
        NSApp.activate(ignoringOtherApps: true)
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            NSWorkspace.shared.open(repoURL)
            UserDefaults.standard.set("done", forKey: stateKey)
        case .alertSecondButtonReturn:
            UserDefaults.standard.set(
                "later:\(Date().timeIntervalSince1970)", forKey: stateKey)
        default:
            UserDefaults.standard.set("never", forKey: stateKey)
        }
    }
}
