#!/bin/bash
# Creates the app folder, copies files, and sets up auto-start

APP_DIR="$HOME/.dj-library"
PLIST="$HOME/Library/LaunchAgents/com.milan.djlibrary.plist"

mkdir -p "$APP_DIR/static"
cp "$(dirname "$0")/app.py" "$APP_DIR/app.py"
cp "$(dirname "$0")/static/index.html" "$APP_DIR/static/index.html"
for f in manifest.webmanifest icon.svg sw.js; do
  [ -f "$(dirname "$0")/static/$f" ] && cp "$(dirname "$0")/static/$f" "$APP_DIR/static/$f"
done

# LaunchAgent — this is macOS's built-in way to run something automatically
# at login, in the background, without needing a separate app.
# It's the same system that things like Dropbox and 1Password use.
cat > "$PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.milan.djlibrary</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3.11</string>
    <string>$APP_DIR/app.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$APP_DIR/dj-library.log</string>
  <key>StandardErrorPath</key>
  <string>$APP_DIR/dj-library.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "✅ DJ Library installed and started!"
echo "🌐 Open http://localhost:3000 in your browser"
