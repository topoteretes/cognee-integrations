import AppKit
import SwiftUI

/// ⌥⇧Space: one line into memory. Type the thought, hit ↩, keep working —
/// the note lands in the capture folder and indexes like any document.
@MainActor
final class CaptureModel: ObservableObject {
    @Published var text = "" {
        didSet { scheduleWhisper() }
    }
    @Published var status: String?
    /// Memory talking back while you type: closest known fact, or a conflict.
    @Published var whisper: String?
    @Published var whisperIsConflict = false
    var onDone: (() -> Void)?
    private var whisperTask: Task<Void, Never>?

    /// Debounced /whisper lookup — memory reacts to the note, not keystrokes.
    private func scheduleWhisper() {
        whisperTask?.cancel()
        let note = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard note.count >= 12 else {
            whisper = nil
            whisperIsConflict = false
            return
        }
        whisperTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard let self, !Task.isCancelled,
                let response = try? await BackendClient().whisper(note),
                !Task.isCancelled,
                note == self.text.trimmingCharacters(in: .whitespacesAndNewlines)
            else { return }
            if let conflict = response.conflicts.first {
                self.whisper = "conflicts with memory: \(conflict.a) vs \(conflict.b)"
                self.whisperIsConflict = true
            } else if let related = response.related.first {
                self.whisper = "memory knows: \(related)"
                self.whisperIsConflict = false
            } else {
                self.whisper = nil
                self.whisperIsConflict = false
            }
        }
    }

    func submit() {
        let note = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !note.isEmpty else {
            onDone?()
            return
        }
        status = "Remembering…"
        Task { [weak self] in
            do {
                try await BackendClient().capture(text: note, source: "quick-capture")
                self?.status = "Remembered ✓"
            } catch {
                self?.status = "Backend unreachable"
            }
            try? await Task.sleep(nanoseconds: 700_000_000)
            self?.text = ""
            self?.status = nil
            self?.whisper = nil
            self?.onDone?()
        }
    }
}

struct CaptureView: View {
    @ObservedObject var model: CaptureModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "brain")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Color.cognee)
                TextField("Remember this…", text: $model.text)
                    .textFieldStyle(.plain)
                    .font(.system(size: 16))
                    .focused($focused)
                    .onSubmit { model.submit() }
                if let status = model.status {
                    Text(status).font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 14)
            .frame(height: 44)
            if let whisper = model.whisper {
                Divider().padding(.horizontal, 12).opacity(0.5)
                HStack(spacing: 7) {
                    Image(
                        systemName: model.whisperIsConflict
                            ? "exclamationmark.triangle.fill" : "sparkle"
                    )
                    .font(.system(size: 10))
                    .foregroundStyle(
                        model.whisperIsConflict ? AnyShapeStyle(.orange) : AnyShapeStyle(Color.cognee)
                    )
                    Text(whisper)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
            }
        }
        .frame(width: 460)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(.white.opacity(0.18), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.3), radius: 20, y: 8)
        .padding(24)
        // fixed canvas with headroom for the whisper row — the panel window
        // never resizes (content outside a fitted window clips to nothing)
        .frame(width: 508, height: 140, alignment: .top)
        .onAppear { focused = true }
        .animation(.easeOut(duration: 0.14), value: model.whisper)
    }
}

@MainActor
final class CapturePanelController: NSObject, NSWindowDelegate {
    private let model = CaptureModel()
    private var panel: SearchPanel!
    private var keyMonitor: Any?

    override init() {
        super.init()
        panel = SearchPanel(
            contentRect: NSRect(x: 0, y: 0, width: 508, height: 140),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        let hosting = NSHostingController(rootView: CaptureView(model: model))
        hosting.sizingOptions = []
        panel.contentViewController = hosting
        panel.setContentSize(NSSize(width: 508, height: 140))
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.delegate = self
        model.onDone = { [weak self] in self?.hide() }
    }

    func toggle() {
        panel.isVisible ? hide() : show()
    }

    private func show() {
        let screen =
            NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }
            ?? NSScreen.main
        if let frame = screen?.visibleFrame {
            panel.setFrameOrigin(
                NSPoint(
                    x: frame.midX - panel.frame.width / 2,
                    y: frame.minY + frame.height * 0.78
                ))
        }
        panel.makeKeyAndOrderFront(nil)
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.panel.isKeyWindow else { return event }
            if event.keyCode == 53 {  // esc
                self.hide()
                return nil
            }
            return event
        }
    }

    private func hide() {
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
        panel.orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) {
        hide()
    }
}
