#!/usr/bin/env bash
# Install a macOS Quick Action: right-click any file (or selected text) in
# any app -> Services -> "Send to cognee". Wraps scripts/cognee-send.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKFLOW="$HOME/Library/Services/Send to cognee.workflow"
mkdir -p "$WORKFLOW/Contents"

cat > "$WORKFLOW/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>NSServices</key>
    <array>
        <dict>
            <key>NSMenuItem</key>
            <dict><key>default</key><string>Send to cognee</string></dict>
            <key>NSMessage</key><string>runWorkflowAsService</string>
            <key>NSRequiredContext</key><dict/>
            <key>NSSendFileTypes</key><array><string>public.item</string></array>
            <key>NSSendTypes</key><array><string>public.utf8-plain-text</string></array>
        </dict>
    </array>
</dict>
</plist>
PLIST

cat > "$WORKFLOW/Contents/document.wflow" <<PLIST
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
                    <string>for f in "\$@"; do
  if [ -e "\$f" ]; then "$ROOT/scripts/cognee-send" "\$f"; else echo "\$f" | "$ROOT/scripts/cognee-send" --source share-sheet; fi
done</string>
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
                <key>InputUUID</key><string>2A9F4F26-0000-0000-0000-000000000001</string>
                <key>Keywords</key><array/>
                <key>OutputUUID</key><string>2A9F4F26-0000-0000-0000-000000000002</string>
                <key>UUID</key><string>2A9F4F26-0000-0000-0000-000000000003</string>
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
PLIST

chmod +x "$ROOT/scripts/cognee-send"
/System/Library/CoreServices/pbs -flush 2>/dev/null || true
echo "Installed: right-click a file -> Quick Actions -> 'Send to cognee'"
