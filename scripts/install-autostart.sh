#!/bin/bash
# Creates a macOS LaunchAgent to start peon-pet on login.

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.peonpet.app.plist"
NODE_PATH="$(which node)"

if [ ! -f "$NODE_PATH" ]; then
  echo "Node.js not found. Please install Node.js."
  exit 1
fi

# Resolve Electron binary path
ELECTRON="$APP_DIR/node_modules/.bin/electron"
if [ ! -f "$ELECTRON" ]; then
  echo "Electron not found. Run: npm install"
  exit 1
fi

# Use Node.js to resolve real path (readlink -f not available on macOS)
ELECTRON_REAL="$(node -e "console.log(require('fs').realpathSync(process.argv[1]))" "$ELECTRON")"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.peonpet.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ELECTRON_REAL</string>
    <string>$APP_DIR</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardErrorPath</key>
  <string>/tmp/peon-pet.log</string>
  <key>StandardOutPath</key>
  <string>/tmp/peon-pet.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"
echo "Auto-start installed. Peon Pet will launch on next login."
echo "To start now: launchctl start com.peonpet.app"
echo "To remove:    launchctl unload -w $PLIST_PATH && rm $PLIST_PATH"
