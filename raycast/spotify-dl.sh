#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title spotify-dl
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🎵
# @raycast.argument1 { "type": "text", "placeholder": "Spotify link, song name, or paste multiple links" }
# @raycast.argument2 { "type": "text", "placeholder": "Folder (optional)", "optional": true }
# @raycast.argument3 { "type": "text", "placeholder": "Crate (optional)", "optional": true }
# @raycast.packageName Music

INPUT="$1"
SUBFOLDER="$2"
CRATE="$3"

# Check if the DJ Library app is running
if ! curl -s http://localhost:3000/api/auth/status > /dev/null 2>&1; then
  osascript -e 'display notification "DJ Library app is not running" with title "❌ spotify-dl"'
  exit 1
fi

# Resolve an exact crate name to its id. If the crate doesn't exist yet it is
# created automatically as a manual crate, so naming a new crate just works.
# Smart crates are refused (their membership is computed from rules, so adding
# songs to one would be a no-op).
CRATE_ID="null"
if [ -n "$CRATE" ]; then
  CRATE_ID=$(curl -s http://localhost:3000/api/crates | python3 -c "
import sys, json
name = '''$CRATE'''.strip().lower()
try:
    crates = json.load(sys.stdin)
except Exception:
    print('null'); raise SystemExit
for c in crates:
    if c.get('name','').strip().lower() == name:
        print('smart' if c.get('is_smart') else c['id'])
        break
else:
    print('missing')
")
  if [ "$CRATE_ID" = "smart" ]; then
    osascript -e "display notification \"“$CRATE” is a smart crate — downloads can't be added to it\" with title \"⚠️ spotify-dl\""
    CRATE_ID="null"
  elif [ "$CRATE_ID" = "missing" ]; then
    CRATE_JSON=$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1], 'is_smart': False, 'rules': []}))" "$CRATE")
    CRATE_ID=$(curl -s -X POST "http://localhost:3000/api/crates" \
      -H "Content-Type: application/json" \
      -d "$CRATE_JSON" \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('id','null'))" 2>/dev/null)
    if [ "$CRATE_ID" != "null" ] && [ -n "$CRATE_ID" ]; then
      osascript -e "display notification \"Created crate “$CRATE” — downloading into it\" with title \"✅ spotify-dl\""
    else
      osascript -e "display notification \"Could not create crate “$CRATE” — downloading without a crate\" with title \"⚠️ spotify-dl\""
      CRATE_ID="null"
    fi
  fi
fi

# Detect input type:
# - 2+ Spotify track URLs (on any number of lines) = list mode
# - Single Spotify URL = single track or playlist
# - Plain text = YouTube search
# Raycast can collapse a pasted list onto one line, so extract the URLs with
# grep rather than counting lines.

TRACK_URLS=$(echo "$INPUT" | grep -o "https://open.spotify.com/track/[A-Za-z0-9]*")
URL_COUNT=$(echo "$TRACK_URLS" | sed '/^$/d' | wc -l | tr -d ' ')

if [ "$URL_COUNT" -gt 1 ]; then
  # --- SPOTIFY TRACK LIST MODE ---
  # Multiple Spotify track URLs pasted at once (e.g. copied from a playlist),
  # whether they arrive on separate lines or all on one line. We send them
  # all to the batch endpoint which processes them one by one.
  URLS_JSON=$(echo "$TRACK_URLS" | sed '/^$/d' | python3 -c "
import sys, json
lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
print(json.dumps(lines))
")
  SUBFOLDER_JSON=$(echo "$SUBFOLDER" | python3 -c "
import sys, json
v = sys.stdin.read().strip()
print(json.dumps(v) if v else 'null')
")
  RESPONSE=$(curl -s -X POST "http://localhost:3000/api/download-spotify-list" \
    -H "Content-Type: application/json" \
    -d "{\"urls\": $URLS_JSON, \"subfolder\": $SUBFOLDER_JSON, \"crate_id\": $CRATE_ID}")

  BATCH_ID=$(echo "$RESPONSE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('batch_id',''))
" 2>/dev/null)
  TOTAL=$(echo "$RESPONSE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('total','?'))
" 2>/dev/null)

  osascript -e "display notification \"Queued $TOTAL tracks — downloading in background\" with title \"⬇️ spotify-dl\""

  # Live progress: poll the batch status and notify whenever the processed
  # count moves. Stops at the success notification (or after ~100 minutes).
  if [ -n "$BATCH_ID" ]; then
    LAST_PROGRESS=0
    POLLS=0
    while [ "$POLLS" -lt 600 ]; do
      sleep 10
      POLLS=$((POLLS+1))
      STATUS_JSON=$(curl -s "http://localhost:3000/api/download-status/batch/$BATCH_ID")
      DONE=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('done',0))" 2>/dev/null)
      DUPE=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('duplicate',0))" 2>/dev/null)
      FAIL=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('failed',0))" 2>/dev/null)
      BSTATUS=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

      if [ "$BSTATUS" = "success" ]; then
        osascript -e "display notification \"Done — $DONE downloaded, $DUPE already owned, $FAIL failed\" with title \"✅ spotify-dl\""
        break
      fi

      PROCESSED=$((DONE+DUPE))
      if [ "$PROCESSED" != "$LAST_PROGRESS" ]; then
        osascript -e "display notification \"$PROCESSED / $TOTAL tracks processed ($DUPE already owned)\" with title \"⬇️ spotify-dl\""
        LAST_PROGRESS="$PROCESSED"
      fi
    done
  fi

else
  # --- SINGLE TRACK / SEARCH MODE ---
  # Single Spotify URL or plain text search
  QUERY_JSON=$(echo "$INPUT" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().strip()))")
  SUBFOLDER_JSON=$(echo "$SUBFOLDER" | python3 -c "
import sys, json
v = sys.stdin.read().strip()
print(json.dumps(v) if v else 'null')
")
  RESPONSE=$(curl -s -X POST "http://localhost:3000/api/download" \
    -H "Content-Type: application/json" \
    -d "{\"query\": $QUERY_JSON, \"subfolder\": $SUBFOLDER_JSON, \"crate_id\": $CRATE_ID}")

  STATUS=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null)
  TITLE=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('track',{}); print(t.get('title','') if t else d.get('query',''))" 2>/dev/null)
  ERROR_MSG=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
  TOTAL=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('total',''))" 2>/dev/null)
  MISSING=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('downloading',''))" 2>/dev/null)
  OWNED=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('already_have',''))" 2>/dev/null)
  PNAME=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('playlist_name','Playlist'))" 2>/dev/null)

  if [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ] 2>/dev/null; then
    osascript -e "display notification \"$MISSING new tracks, $OWNED already owned\" with title \"⬇️ $PNAME\""
  elif [ "$STATUS" = "queued" ]; then
    osascript -e "display notification \"${TITLE:-$INPUT}\" with title \"⬇️ Downloading…\""
  elif [ "$STATUS" = "failed" ]; then
    osascript -e "display notification \"${ERROR_MSG:-Could not download}\" with title \"❌ spotify-dl\""
  else
    osascript -e "display notification \"Could not connect to DJ Library\" with title \"❌ spotify-dl\""
  fi
fi
