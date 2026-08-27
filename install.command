#!/bin/bash
# DJ Library — one-click installer for macOS.
# Double-click this file (or run:  bash install.command  in a terminal).
# It sets up Python packages, installs the app, starts it in the background,
# and opens it in your browser. Everything it installs goes into your own
# home folder — nothing needs admin rights and nothing modifies the system.
#
# If this app already runs on your Mac, you can just double-click this to
# update it to the newest version.

set -e
cd "$(dirname "$0")"

echo ""
echo "  DJ Library installer"
echo "  --------------------"

# ---------------------------------------------------------------------------
# 1. Find Python
# ---------------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "❌ No Python 3 found. Install it from https://www.python.org/downloads/macos/"
  echo "   (Download → open the installer → click through), then run this again."
  read -n 1 -s -r -p "   Press any key to close…"; echo; exit 1
fi
echo "✓ Found Python: $PY"

# ---------------------------------------------------------------------------
# 2. Make sure the app's Python packages are installed
# ---------------------------------------------------------------------------
echo "✓ Installing required packages (flask, mutagen, watchdog, requests)…"
"$PY" -m pip install --quiet --upgrade flask mutagen watchdog requests 2>&1 | tail -2 || true

# ---------------------------------------------------------------------------
# 3. Make sure ffmpeg is available (used by the downloader)
# ---------------------------------------------------------------------------
if command -v ffmpeg >/dev/null 2>&1 || "$PY" -c 'import shutil,sys; sys.exit(0 if shutil.which("ffmpeg") else 1)'; then
  echo "✓ ffmpeg found"
else
  echo "⚠ ffmpeg not found — the downloader won't convert audio until it's installed."
  echo "    Run:  brew install ffmpeg"
fi

# ---------------------------------------------------------------------------
# 4. Install the app files
# ---------------------------------------------------------------------------
mkdir -p "$HOME/.dj-library/static"
cp app.py "$HOME/.dj-library/app.py"
cp static/index.html "$HOME/.dj-library/static/index.html"
for f in manifest.webmanifest icon.svg sw.js; do
  [ -f "static/$f" ] && cp "static/$f" "$HOME/.dj-library/static/$f"
done

# Create config.json the first time (never overwrite an existing one)
if [ ! -f "$HOME/.dj-library/config.json" ]; then
  if [ -f config.example.json ]; then
    cp config.example.json "$HOME/.dj-library/config.json"
    echo "✓ Created ~/.dj-library/config.json"
    echo "    edit /Users/$(whoami)/.dj-library/config.json and set music_dir to your songs folder"
  fi
fi
if [ -f "$HOME/.dj-library/config.json" ]; then
  # Put the existing library folder under ~/Music if music_dir is still the placeholder.
  if grep -q '/Users/you' "$HOME/.dj-library/config.json" 2>/dev/null; then
    defaults_music="$HOME/Music"
    "$PY" - "$HOME/.dj-library/config.json" "$defaults_music" <<'PYEOF'
import json, os, sys
p, music = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception:
    d = {}
if str(d.get('music_dir','/Users/you')).startswith('/Users/you'):
    d['music_dir'] = music
    json.dump(d, open(p,'w'), indent=2)
    print(f"✓ music_dir set to: {music}  (edit config.json to change it)")
PYEOF
  fi
fi

# ---------------------------------------------------------------------------
# 5. Start the app and open it
# ---------------------------------------------------------------------------
PLIST="$HOME/Library/LaunchAgents/com.milan.djlibrary.plist"
if [ -f "$PLIST" ] && launchctl list | grep -q com.milan.djlibrary; then
  echo "✓ Restarting DJ Library…"
  launchctl kickstart -k "gui/$(id -u)/com.milan.djlibrary" 2>/dev/null || true
else
  "$PY" "$HOME/.dj-library/app.py" > "$HOME/.dj-library/dj-library.log" 2>&1 &
  sleep 2
  echo "✓ DJ Library started in the background"
fi

sleep 2
open "http://localhost:3000"

echo ""
echo "✅ Done! DJ Library should be open in your browser now."
echo "   It keeps running in the background and starts automatically at login."
echo "   Close this window whenever you like."
read -n 1 -s -r -p "   Press any key to close…"; echo
