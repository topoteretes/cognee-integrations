import AppKit
import Carbon.HIToolbox
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static private(set) weak var shared: AppDelegate?

    private var statusItem: NSStatusItem!
    private var panelController: SearchPanelController!
    private var hotKey: GlobalHotKey?
    private var captureHotKey: GlobalHotKey?
    private var capturePanelController: CapturePanelController?
    private var settingsWindow: NSWindow?
    private var inboxWindow: NSWindow?
    private var shareWindow: NSWindow?
    private var onboardingWindow: NSWindow?
    private let settingsModel = SettingsModel()
    private let inboxModel = InboxModel()
    private let shareModel = ShareModel()
    private let onboardingModel = OnboardingModel()
    private let notifier = HandoverNotifier()

    func applicationDidFinishLaunching(_ notification: Notification) {
        Self.shared = self
        installMainMenu()
        panelController = SearchPanelController()

        hotKey = GlobalHotKey { [weak self] in
            self?.panelController.toggle()
        }

        // ⌥⇧Space: quick capture — one line straight into memory.
        capturePanelController = CapturePanelController()
        captureHotKey = GlobalHotKey(
            modifiers: UInt32(optionKey | shiftKey), id: 2
        ) { [weak self] in
            self?.capturePanelController?.toggle()
        }

        notifier.onUnseenCount = { [weak self] unseen in
            self?.statusItem?.button?.toolTip =
                unseen > 0 ? "Cognee Spotlight — \(unseen) new learnings" : "Cognee Spotlight"
            self?.statusItem?.button?.image = Self.menuBarIcon(unread: unseen > 0)
        }
        notifier.start()

        // New status items appear at the left end of the status area — on
        // notched MacBooks with a full menu bar that spot is silently hidden.
        // Seed a position near the clock on first launch so the icon is visible;
        // after that the user's own drag position wins.
        let autosaveName = "CogneeSpotlight"
        let positionKey = "NSStatusItem Preferred Position \(autosaveName)"
        if UserDefaults.standard.object(forKey: positionKey) == nil {
            UserDefaults.standard.set(160, forKey: positionKey)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.autosaveName = autosaveName
        statusItem.button?.image = Self.menuBarIcon()
        statusItem.menu = buildMenu()

        if hotKey == nil {
            // Extremely rare (another app owns the combo); the menu item still works.
            statusItem.button?.toolTip = "Hotkey ⌥Space unavailable — use this menu to search"
        }

        // Headless debugging: SPOTLIGHT_DEBUG_QUERY=<q> auto-opens the panel and
        // runs a search, so panel rendering can be verified via screenshot.
        if let debugQuery = ProcessInfo.processInfo.environment["SPOTLIGHT_DEBUG_QUERY"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                self?.panelController.show()
                self?.panelController.model.query = debugQuery
            }
        }

        // First launch (or forced via env for testing): walk through setup.
        let forceOnboarding =
            ProcessInfo.processInfo.environment["SPOTLIGHT_DEBUG_ONBOARDING"] != nil
        if forceOnboarding || !OnboardingModel.isConfigured {
            Task { @MainActor in
                // If a configured backend is already answering, don't nag.
                if !forceOnboarding, (try? await BackendClient().health()) != nil { return }
                self.openSetup()
            }
        }
    }

    /// Menu-bar apps get no main menu by default — and without an Edit menu,
    /// ⌘C/⌘V/⌘X/⌘A do nothing in any text field. Install a minimal one.
    private func installMainMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(
            NSMenuItem(
                title: "Quit Cognee Spotlight",
                action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        NSApp.mainMenu = mainMenu
    }

    /// The cognee waveform mark as a template image (renders correctly on
    /// light and dark menu bars); SF Symbols as fallback for `swift run`
    /// launches outside the bundle. With ``unread`` a small dot joins the
    /// mark — the "you have learnings waiting" signal.
    private static func menuBarIcon(unread: Bool = false) -> NSImage? {
        guard let url = Bundle.main.url(forResource: "MenuIcon", withExtension: "png"),
            let mark = NSImage(contentsOf: url)
        else {
            return NSImage(
                systemSymbolName: "brain.filled.head.profile",
                accessibilityDescription: "Cognee Spotlight"
            ) ?? NSImage(
                systemSymbolName: "magnifyingglass", accessibilityDescription: "Cognee Spotlight")
        }
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            mark.draw(in: rect)
            if unread {
                let dot = NSRect(x: rect.maxX - 5.5, y: rect.maxY - 5.5, width: 5, height: 5)
                NSBezierPath(ovalIn: dot).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        let searchItem = NSMenuItem(
            title: "Search…", action: #selector(togglePanel), keyEquivalent: " "
        )
        searchItem.keyEquivalentModifierMask = [.option]
        searchItem.target = self
        menu.addItem(searchItem)
        menu.addItem(.separator())

        let inboxItem = NSMenuItem(
            title: "Inbox…", action: #selector(openInbox), keyEquivalent: "i"
        )
        inboxItem.target = self
        menu.addItem(inboxItem)

        let graphItem = NSMenuItem(
            title: "Knowledge Graph…", action: #selector(openGraph), keyEquivalent: "g"
        )
        graphItem.target = self
        menu.addItem(graphItem)

        let shareItem = NSMenuItem(
            title: "Share a Learning…", action: #selector(openShareEmpty), keyEquivalent: "s"
        )
        shareItem.target = self
        menu.addItem(shareItem)
        menu.addItem(.separator())

        let indexItem = NSMenuItem(
            title: "Index a Folder…", action: #selector(indexFolder), keyEquivalent: ""
        )
        indexItem.target = self
        menu.addItem(indexItem)

        let settingsItem = NSMenuItem(
            title: "Settings…", action: #selector(openSettings), keyEquivalent: ","
        )
        settingsItem.target = self
        menu.addItem(settingsItem)

        let setupItem = NSMenuItem(
            title: "Setup…", action: #selector(openSetupItem), keyEquivalent: ""
        )
        setupItem.target = self
        menu.addItem(setupItem)

        let profileItem = NSMenuItem(title: "Profile", action: nil, keyEquivalent: "")
        let profileMenu = NSMenu(title: "Profile")
        for name in Profiles.list() {
            let item = NSMenuItem(
                title: name, action: #selector(switchProfile(_:)), keyEquivalent: ""
            )
            item.target = self
            item.state = name == Profiles.active ? .on : .off
            profileMenu.addItem(item)
        }
        profileMenu.addItem(.separator())
        let newProfile = NSMenuItem(
            title: "New Profile…", action: #selector(createProfile), keyEquivalent: ""
        )
        newProfile.target = self
        profileMenu.addItem(newProfile)
        profileItem.submenu = profileMenu
        menu.addItem(profileItem)

        menu.addItem(.separator())
        menu.addItem(
            NSMenuItem(title: "Quit Cognee Spotlight", action: #selector(NSApp.terminate(_:)),
                       keyEquivalent: "q")
        )
        return menu
    }

    @objc private func togglePanel() {
        panelController.toggle()
    }

    @objc private func indexFolder() {
        settingsModel.addFolder()
    }

    @objc private func openSetupItem() {
        openSetup()
    }

    @objc private func switchProfile(_ sender: NSMenuItem) {
        Profiles.activate(sender.title)
        statusItem.menu = buildMenu()  // refresh checkmarks
        settingsModel.refresh()
        onboardingModel.loadExisting()
        Task { @MainActor in
            if Profiles.isConfigured(sender.title) {
                // make sure this profile's backend is up
                if (try? await BackendClient().health()) == nil {
                    _ = await BackendLauncher.restart(profile: sender.title)
                }
            } else {
                self.openSetup()  // brand-new profile: walk through setup
            }
        }
    }

    @objc private func createProfile() {
        let alert = NSAlert()
        alert.messageText = "New profile"
        alert.informativeText =
            "A profile is a separate person or cloud account on this Mac — its own backend, index, and inbox."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        field.placeholderString = "boris"
        alert.accessoryView = field
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
            .lowercased().replacingOccurrences(of: " ", with: "-")
        guard !name.isEmpty, name != "default" else { return }
        try? Profiles.create(name)
        Profiles.activate(name)
        statusItem.menu = buildMenu()
        onboardingModel.loadExisting()
        openSetup()
    }

    private func openSetup() {
        if onboardingWindow == nil {
            onboardingWindow = makeWindow(
                title: "Cognee Spotlight Setup",
                content: NSHostingController(
                    rootView: OnboardingView(model: onboardingModel) { [weak self] in
                        self?.onboardingWindow?.orderOut(nil)
                        self?.settingsModel.refresh()
                    }
                )
            )
        }
        NSApp.activate(ignoringOtherApps: true)
        onboardingWindow?.makeKeyAndOrderFront(nil)
    }

    @objc private func openGraph() {
        NSWorkspace.shared.open(Preferences.backendURL.appendingPathComponent("graph"))
    }

    @objc private func openInbox() {
        if inboxWindow == nil {
            inboxWindow = makeWindow(
                title: "Handover Inbox",
                content: NSHostingController(rootView: InboxView(model: inboxModel))
            )
        }
        inboxModel.refresh()
        NSApp.activate(ignoringOtherApps: true)
        inboxWindow?.makeKeyAndOrderFront(nil)
    }

    @objc private func openShareEmpty() {
        openShare(title: "", body: "", source: "")
    }

    /// Also reachable from the search panel (⌘S) with the selection prefilled.
    func openShare(title: String, body: String, source: String) {
        if !title.isEmpty || !body.isEmpty {
            shareModel.title = title
            shareModel.body = body
            shareModel.source = source
        }
        if shareWindow == nil {
            shareWindow = makeWindow(
                title: "Share a Learning",
                content: NSHostingController(
                    rootView: ShareView(model: shareModel) { [weak self] in
                        self?.shareWindow?.orderOut(nil)
                        self?.shareModel.title = ""
                        self?.shareModel.body = ""
                    }
                )
            )
        }
        NSApp.activate(ignoringOtherApps: true)
        shareWindow?.makeKeyAndOrderFront(nil)
    }

    private func makeWindow(title: String, content: NSViewController) -> NSWindow {
        let window = NSWindow(
            contentRect: .zero,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.contentViewController = content
        window.isReleasedWhenClosed = false
        window.center()
        return window
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            let window = NSWindow(
                contentRect: .zero,
                styleMask: [.titled, .closable, .miniaturizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Cognee Spotlight Settings"
            window.contentViewController = NSHostingController(
                rootView: SettingsView(model: settingsModel)
            )
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        settingsModel.refresh()
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }
}
