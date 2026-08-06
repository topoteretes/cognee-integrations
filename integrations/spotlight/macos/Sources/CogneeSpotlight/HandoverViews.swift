import AppKit
import SwiftUI
import UserNotifications

// MARK: - Inbox

/// Learnings other people handed to you, across your layers (direct / team /
/// org). Backed entirely by the backend's /inbox; opening an item marks it seen.
@MainActor
final class InboxModel: ObservableObject {
    @Published var items: [HandoverItem] = []
    @Published var unseen: Int = 0
    @Published var enabled = true
    @Published var selectedID: String?
    @Published var statusText = ""

    func refresh() {
        Task {
            do {
                let response = try await BackendClient().inbox()
                items = response.items
                unseen = response.unseen
                enabled = response.enabled ?? true
                statusText = enabled ? "" : "Handover not configured — set SPOTLIGHT_USER and COGNEE_HUB_URL for the backend."
            } catch {
                statusText = "Backend unreachable."
            }
        }
    }

    func select(_ item: HandoverItem) {
        selectedID = item.id
        guard !item.seen else { return }
        Task {
            try? await BackendClient().markSeen(ids: [item.id])
            refresh()
        }
    }

    var selected: HandoverItem? { items.first { $0.id == selectedID } }
}

struct InboxView: View {
    @ObservedObject var model: InboxModel

    var body: some View {
        HSplitView {
            List(model.items, selection: $model.selectedID) { item in
                HStack {
                    Circle()
                        .fill(item.seen ? Color.clear : Color.accentColor)
                        .frame(width: 7, height: 7)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.title).font(.system(size: 13, weight: item.seen ? .regular : .semibold))
                        Text("from \(item.sender)").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    layerBadge(item.layer)
                }
                .contentShape(Rectangle())
                .onTapGesture { model.select(item) }
            }
            .frame(minWidth: 240)

            Group {
                if let item = model.selected {
                    noteDetail(item)
                } else if !model.statusText.isEmpty {
                    Text(model.statusText).foregroundStyle(.secondary).padding()
                } else {
                    Text(model.items.isEmpty ? "Nothing shared with you yet." : "Select a learning.")
                        .foregroundStyle(.secondary)
                }
            }
            .frame(minWidth: 320, maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: 720, height: 420)
        .onAppear { model.refresh() }
    }

    /// The note, read like a note: a header card (sender, layer, when) with a
    /// copy button, then the body as rendered markdown instead of a raw dump.
    private func noteDetail(_ item: HandoverItem) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                senderAvatar(item.sender)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title)
                        .font(.system(size: 15, weight: .semibold))
                        .lineLimit(1)
                    HStack(spacing: 5) {
                        Text("from \(item.sender)")
                        if let when = Self.relativeDate(item.created_at) {
                            Text("· \(when)")
                        }
                    }
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                }
                Spacer()
                layerBadge(item.layer)
                CopyButton(text: item.body, help: "Copy note")
            }
            .padding(14)
            Divider()
            ScrollView {
                Text(SearchView.markdown(item.body.isEmpty ? "*(no content)*" : item.body))
                    .font(.system(size: 13.5, design: .serif))
                    .lineSpacing(4)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
            }
        }
    }

    private func senderAvatar(_ sender: String) -> some View {
        Circle()
            .fill(Color.cognee.opacity(0.18))
            .frame(width: 30, height: 30)
            .overlay(
                Text(String(sender.prefix(1)).uppercased())
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.cognee)
            )
    }

    /// "2 days ago" from the tenant's ISO timestamp; nil when unparseable.
    static func relativeDate(_ iso: String) -> String? {
        let withFractional = ISO8601DateFormatter()
        withFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        guard let date = withFractional.date(from: iso) ?? plain.date(from: iso) else {
            return nil
        }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private func layerBadge(_ layer: String) -> some View {
        Text(layer)
            .font(.system(size: 9, weight: .semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color(for: layer).opacity(0.2), in: Capsule())
            .foregroundStyle(color(for: layer))
    }

    private func color(for layer: String) -> Color {
        switch layer {
        case "org": return .blue
        case "team": return .green
        default: return .purple
        }
    }
}

// MARK: - Share

@MainActor
final class ShareModel: ObservableObject {
    @Published var to = ""
    @Published var title = ""
    @Published var body = ""
    @Published var source = ""
    @Published var statusText = ""
    @Published var sending = false

    func send(onDone: @escaping () -> Void) {
        let to = to.trimmingCharacters(in: .whitespaces)
        let title = title.trimmingCharacters(in: .whitespaces)
        guard !to.isEmpty, !title.isEmpty, !body.isEmpty else {
            statusText = "Recipient, title and content are all required."
            return
        }
        sending = true
        Task {
            defer { sending = false }
            do {
                try await BackendClient().share(to: to, title: title, body: body, source: source)
                // the sheet's recipient becomes the ⌘S quick-share default
                if !to.hasPrefix("team:"), to.lowercased() != "org" {
                    Preferences.defaultShareRecipient = to
                }
                statusText = ""
                onDone()
            } catch {
                statusText = "Could not send — is the backend running and the hub configured?"
            }
        }
    }
}

struct ShareView: View {
    @ObservedObject var model: ShareModel
    var onSent: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Share a learning").font(.title3.weight(.semibold))
            Text("Send distilled knowledge to a teammate's memory. They get a notification, and it becomes searchable in their Spotlight.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Form {
                TextField("To (username, team:<name>, or org)", text: $model.to)
                TextField("Title", text: $model.title)
            }
            TextEditor(text: $model.body)
                .font(.system(size: 13))
                .frame(minHeight: 160)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.quaternary))
            if !model.statusText.isEmpty {
                Text(model.statusText).font(.callout).foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                if model.sending { ProgressView().controlSize(.small) }
                Button("Send") { model.send(onDone: onSent) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.sending)
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}

// MARK: - Notifications

/// Polls the inbox and raises a native notification once per new learning.
@MainActor
final class HandoverNotifier {
    private var timer: Timer?
    private let notifiedKey = "notifiedHandoverIDs"
    var onUnseenCount: ((Int) -> Void)?

    func start() {
        guard Bundle.main.bundleIdentifier != nil else { return }  // needs a real .app
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        timer = Timer.scheduledTimer(withTimeInterval: 45, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
        poll()
    }

    private func poll() {
        Task {
            guard let response = try? await BackendClient().inbox() else { return }
            onUnseenCount?(response.unseen)
            var notified = Set(UserDefaults.standard.stringArray(forKey: notifiedKey) ?? [])
            for item in response.items where !item.seen && !notified.contains(item.id) {
                notified.insert(item.id)
                let content = UNMutableNotificationContent()
                content.title = "New learning from \(item.sender)"
                content.body = item.title
                content.sound = .default
                try? await UNUserNotificationCenter.current().add(
                    UNNotificationRequest(
                        identifier: "handover-\(item.id)", content: content, trigger: nil
                    )
                )
            }
            UserDefaults.standard.set(Array(notified), forKey: notifiedKey)
        }
    }
}
