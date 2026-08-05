#!/usr/bin/env bash
# Build "Cognee Spotlight.app" from the Swift package — no Xcode needed,
# Command Line Tools are enough. Output: macos/dist/Cognee Spotlight.app
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> swift build -c release"
swift build -c release

APP="dist/Cognee Spotlight.app"
BIN=".build/release/CogneeSpotlight"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/CogneeSpotlight"
cp Resources/MenuIcon.png "$APP/Contents/Resources/MenuIcon.png"
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>ai.cognee.spotlight</string>
    <key>CFBundleName</key>
    <string>Cognee Spotlight</string>
    <key>CFBundleDisplayName</key>
    <string>Cognee Spotlight</string>
    <key>CFBundleExecutable</key>
    <string>CogneeSpotlight</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough to run locally on this machine.
codesign --force --sign - "$APP"

echo "==> built $PWD/$APP"
echo "    launch it with:  open \"$PWD/$APP\""
