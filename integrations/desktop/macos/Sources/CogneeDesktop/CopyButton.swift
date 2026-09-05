import AppKit
import SwiftUI

/// A quiet copy-to-clipboard button: doc icon that flips to a green
/// checkmark for a beat, so the copy is visibly confirmed without a toast.
struct CopyButton: View {
    let text: String
    var help: String = "Copy"
    @State private var copied = false

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            withAnimation(.easeOut(duration: 0.12)) { copied = true }
            Task {
                try? await Task.sleep(nanoseconds: 1_300_000_000)
                withAnimation(.easeOut(duration: 0.2)) { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(copied ? AnyShapeStyle(Color.green) : AnyShapeStyle(.secondary))
                .frame(width: 22, height: 22)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(copied ? "Copied" : help)
    }
}
