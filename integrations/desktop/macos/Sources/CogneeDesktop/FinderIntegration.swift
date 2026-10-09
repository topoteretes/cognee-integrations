import Foundation

/// The Finder right-click path: select files, right-click → Quick Actions →
/// "Index in Cognee" — indexed without ever opening this app's UI.
///
/// Installed as a macOS Services workflow the app writes itself (no helper
/// scripts to run first). The workflow's shell step builds a JSON body from
/// the selected paths and POSTs it straight to the backend's /index, so it
/// keeps working even when the app isn't running — only the backend must be.
@MainActor
enum FinderIntegration {
    static var workflowURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Services/Index in Cognee.workflow")
    }

    static var isInstalled: Bool {
        FileManager.default.fileExists(atPath: workflowURL.path)
    }

    static func install() throws {
        let contents = workflowURL.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        try infoPlist.write(
            to: contents.appendingPathComponent("Info.plist"), atomically: true, encoding: .utf8)
        try documentWflow.write(
            to: contents.appendingPathComponent("document.wflow"), atomically: true,
            encoding: .utf8)
        flushServices()
    }

    static func uninstall() throws {
        guard isInstalled else { return }
        try FileManager.default.removeItem(at: workflowURL)
        flushServices()
    }

    /// Make the Services menu notice the change without a logout.
    private static func flushServices() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/System/Library/CoreServices/pbs")
        process.arguments = ["-flush"]
        try? process.run()
    }

    private static var infoPlist: String {
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>NSServices</key>
            <array>
                <dict>
                    <key>NSMenuItem</key>
                    <dict><key>default</key><string>Index in Cognee</string></dict>
                    <key>NSMessage</key><string>runWorkflowAsService</string>
                    <key>NSRequiredContext</key><dict/>
                    <key>NSSendFileTypes</key><array><string>public.item</string></array>
                </dict>
            </array>
        </dict>
        </plist>
        """
    }

    private static var documentWflow: String {
        // Selected paths arrive as shell arguments; escape each into a JSON
        // string and POST the batch to /index. The backend URL is baked in at
        // install time (re-toggle the setting after changing backends).
        let backend = Preferences.backendURL.absoluteString
        let command = """
        json="["
        for f in "$@"; do
          esc=${f//\\\\/\\\\\\\\}
          esc=${esc//\\"/\\\\\\"}
          json+="\\"$esc\\","
        done
        json="${json%,}]"
        [ "$json" = "]" ] && exit 0
        curl -s -m 15 -X POST "\(backend)/index" \\
          -H 'Content-Type: application/json' \\
          -d "{\\"paths\\":$json}" > /dev/null
        """
        let escaped =
            command
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>AMApplicationBuild</key><string>528</string>
            <key>AMApplicationVersion</key><string>2.10</string>
            <key>AMDocumentVersion</key><string>2</string>
            <key>actions</key>
            <array>
                <dict>
                    <key>action</key>
                    <dict>
                        <key>AMAccepts</key>
                        <dict>
                            <key>Container</key><string>List</string>
                            <key>Optional</key><true/>
                            <key>Types</key><array><string>com.apple.cocoa.string</string></array>
                        </dict>
                        <key>AMActionVersion</key><string>2.0.3</string>
                        <key>AMParameterProperties</key><dict/>
                        <key>ActionBundlePath</key>
                        <string>/System/Library/Automator/Run Shell Script.action</string>
                        <key>ActionName</key><string>Run Shell Script</string>
                        <key>ActionParameters</key>
                        <dict>
                            <key>COMMAND_STRING</key>
                            <string>\(escaped)</string>
                            <key>CheckedForUserDefaultShell</key><true/>
                            <key>inputMethod</key><integer>1</integer>
                            <key>shell</key><string>/bin/bash</string>
                            <key>source</key><string></string>
                        </dict>
                        <key>BundleIdentifier</key><string>com.apple.RunShellScript</string>
                        <key>CFBundleVersion</key><string>2.0.3</string>
                        <key>CanShowSelectedItemsWhenRun</key><false/>
                        <key>CanShowWhenRun</key><true/>
                        <key>Class Name</key><string>RunShellScriptAction</string>
                        <key>InputUUID</key><string>5C0FEE01-0000-0000-0000-000000000001</string>
                        <key>Keywords</key><array/>
                        <key>OutputUUID</key><string>5C0FEE01-0000-0000-0000-000000000002</string>
                        <key>UUID</key><string>5C0FEE01-0000-0000-0000-000000000003</string>
                        <key>isViewVisible</key><integer>1</integer>
                    </dict>
                </dict>
            </array>
            <key>workflowMetaData</key>
            <dict>
                <key>serviceInputTypeIdentifier</key>
                <string>com.apple.Automator.fileSystemObject</string>
                <key>serviceProcessesInput</key><integer>0</integer>
                <key>workflowTypeIdentifier</key>
                <string>com.apple.Automator.servicesMenu</string>
            </dict>
        </dict>
        </plist>
        """
    }
}
