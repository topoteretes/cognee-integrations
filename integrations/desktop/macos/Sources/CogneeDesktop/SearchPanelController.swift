import AppKit
import SwiftUI

/// A borderless, non-activating floating panel -- the launcher look. It takes
/// keyboard input without stealing focus from the frontmost app, floats above
/// full-screen apps, and hides on Escape or when it loses key status.
final class SearchPanel: NSPanel {
    override var canBecomeKey: Bool { true }  // borderless windows refuse key by default
}

@MainActor
final class SearchPanelController: NSObject, NSWindowDelegate {
    let model = SearchViewModel()
    private var panel: SearchPanel!
    private var keyMonitor: Any?

    override init() {
        super.init()
        let hosting = NSHostingController(rootView: SearchView(model: model))
        // The window is a fixed transparent canvas; the SwiftUI card inside
        // grows and shrinks. Without this, AppKit fits the window to the
        // initial content (just the search field) and later results render
        // outside the window bounds — invisible.
        hosting.sizingOptions = []
        panel = SearchPanel(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentViewController = hosting
        panel.setContentSize(NSSize(width: 760, height: 560))
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false  // the SwiftUI card draws its own
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.delegate = self
    }

    var isVisible: Bool { panel.isVisible }

    func toggle() {
        isVisible ? hide() : show()
    }

    func show() {
        model.reset()
        positionOnActiveScreen()
        // A brief fade-in reads as intentional; popping reads as unfinished.
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        panel.alphaValue = reduceMotion ? 1 : 0
        panel.makeKeyAndOrderFront(nil)
        if !reduceMotion {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.13
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().alphaValue = 1
            }
        }
        installKeyMonitor()
    }

    func hide() {
        StarNudge.maybePrompt()
        removeKeyMonitor()
        panel.orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) {
        hide()  // click elsewhere dismisses, launcher-style
    }

    /// Center horizontally on the screen with the mouse, top third vertically.
    private func positionOnActiveScreen() {
        let screen =
            NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }
            ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { return }
        let size = panel.frame.size
        let origin = NSPoint(
            x: frame.midX - size.width / 2,
            y: frame.minY + frame.height * 0.72 - size.height / 2
        )
        panel.setFrameOrigin(origin)
    }

    // -- keyboard ------------------------------------------------------------
    private func installKeyMonitor() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.panel.isKeyWindow else { return event }
            return self.handle(event) ? nil : event
        }
    }

    private func removeKeyMonitor() {
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
    }

    private func handle(_ event: NSEvent) -> Bool {
        // The ⌘S recipient picker owns the keyboard while it is showing.
        if model.shareRecipients != nil {
            switch event.keyCode {
            case 53:  // esc closes the picker, not the panel
                model.cancelShare()
                return true
            case 123: model.moveRecipient(by: -1); return true  // ←
            case 124: model.moveRecipient(by: 1); return true  // →
            case 126: model.moveRecipient(by: -1); return true  // ↑
            case 125: model.moveRecipient(by: 1); return true  // ↓
            case 36, 76:  // return sends
                model.confirmShare()
                return true
            default:
                return true  // swallow typing while picking
            }
        }
        switch event.keyCode {
        case 53:  // esc closes the open connection detail first, then the panel
            if model.connectionDetail != nil {
                model.connectionDetail = nil
                return true
            }
            hide()
            return true
        case 125:  // down
            model.moveSelection(by: 1)
            return true
        case 126:  // up
            model.moveSelection(by: -1)
            return true
        case 36, 76:  // return / keypad enter
            if event.modifierFlags.contains(.shift) {
                model.ask()
                return true
            }
            let reveal = event.modifierFlags.contains(.command)
            if model.openSelected(revealInFinder: reveal) { hide() }
            return true
        case 1 where event.modifierFlags.contains(.command):
            if event.modifierFlags.contains(.shift) {  // ⌘⇧S: full share sheet
                shareCurrent()
            } else if !model.startShare() {  // ⌘S: inline recipient picker
                shareCurrent()  // nothing shareable inline — offer the sheet
            }
            return true
        // The panel is non-activating, so main-menu key equivalents never
        // reach it — route the clipboard shortcuts to the field editor here.
        case 9 where event.modifierFlags.contains(.command):  // ⌘V
            return NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: nil)
        case 8 where event.modifierFlags.contains(.command):  // ⌘C
            return NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: nil)
        case 7 where event.modifierFlags.contains(.command):  // ⌘X
            return NSApp.sendAction(#selector(NSText.cut(_:)), to: nil, from: nil)
        case 0 where event.modifierFlags.contains(.command):  // ⌘A
            return NSApp.sendAction(#selector(NSText.selectAll(_:)), to: nil, from: nil)
        default:
            return false
        }
    }

    /// Hand the current answer (or selected result) to the share window.
    private func shareCurrent() {
        let title: String
        let body: String
        let source: String
        if let answer = model.answer {
            title = model.query
            body = answer
            source = "cognee answer"
        } else if model.results.indices.contains(model.selectedIndex) {
            let result = model.results[model.selectedIndex]
            title = result.title
            body = result.snippet.isEmpty ? result.path : result.snippet
            source = result.path
        } else {
            title = model.query
            body = ""
            source = ""
        }
        hide()
        AppDelegate.shared?.openShare(title: title, body: body, source: source)
    }
}
