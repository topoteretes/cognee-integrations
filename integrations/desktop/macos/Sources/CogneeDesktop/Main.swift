import AppKit

// Menu-bar app (no dock icon): the panel appears over whatever the user is
// doing, like any launcher, so the app itself never takes over the screen.
@main
struct Main {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
