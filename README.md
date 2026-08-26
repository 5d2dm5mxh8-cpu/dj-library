# DJ Library

A self-hosted DJ music library manager that runs on your Mac and talks to
[Spotify](https://developer.spotify.com/) (via the Web API + `yt-dlp`) and
[Mixxx](https://mixxx.org/). Built as a single Flask backend with a vanilla
JS frontend — no Node, no build step, no database server (SQLite).

## Features

- **Library** — scan, browse, search, filter and sort your music folder; BPM
  and musical-key analysis (shown in Camelot notation, e.g. `6A`); ID3 tag
  editing straight from the table.
- **Downloader** — paste a Spotify link, a song name, or a whole list; tracks
  are downloaded via `yt-dlp`, converted to MP3 with ffmpeg, tagged, and
  analyzed. `yt-dlp` is bootstrapped and self-updated by the app, so YouTube
  breaking old builds heals itself.
- **Crates** — manual folders plus smart crates (rules on BPM, key, genre, …);
  one-click sync of a crate into Mixxx.
- **Duplicates** — same-name / same-song detection that understands Finder's
  `(1)` suffixes, with a manual merge tool for anything the scan misses.
- **Transitions** — link songs together as "mix A into B" notes (or text-only
  targets), see a song's incoming/outgoing transitions, export everything as a
  CSV or printable setlist, and sync the notes into Mixxx track comments and
  each file's ID3 comment tag — automatically.
- **Folder import** — Finder-style folder picker (browse + Spotlight search).
- **Auto-scan** — watches your music folder and picks up new files instantly.

## Requirements

- macOS (uses `osascript` notifications and Spotlight search; the LaunchAgent
  auto-start is macOS-specific)
- Python 3.11+ with Flask, `mutagen`, `watchdog`, and `requests`
  (`pip install flask mutagen watchdog requests`)
- [ffmpeg](https://ffmpeg.org/) for download conversion

## Setup

The app reads its settings from `~/.dj-library/config.json` — copy the
template and fill it in:

```bash
cp config.example.json ~/.dj-library/config.json
```

| Key | Purpose |
| --- | --- |
| `music_dir` | Absolute path to your music folder (the app scans and watches this) |
| `spotify_client_id` | Spotify app Client ID — **optional**; only the downloader needs it |
| `spotify_client_secret` | Spotify app Client Secret — **optional** |
| `ffmpeg_path` | Path to ffmpeg; defaults to `/opt/homebrew/bin/ffmpeg` |

To create a Spotify app for the downloader: log in at
https://developer.spotify.com/dashboard → *Create app* → copy the Client ID /
Client Secret into the config. Leave them empty to run without Spotify
features (the app prints a warning at startup).

## Run

```bash
python3 app.py
```

Then open **http://localhost:3000**. The app scans the music folder on
startup, watches it for changes, and can auto-start via LaunchAgent:

```bash
./install.sh        # installs app files + a login LaunchAgent
```

## Mixxx integration

- **Crate sync / transition sync** write into Mixxx's `mixxxdb.sqlite`, which
  lives inside Mixxx's sandboxed app container — macOS blocks access by
  default. Grant **Full Disk Access** to the Python that runs the app
  (System Settings → Privacy & Security → Full Disk Access) and keep Mixxx
  closed while syncing (the app refuses otherwise, and always backs up the DB
  before writing).

## Serato, Rekordbox & DJUCED

Export your library (or a single crate — button on each crate) as the standard
**rekordbox.xml** — Tools → "Export library (rekordbox.xml)". The file carries
BPM, the Camelot key, crates/playlists, and your transition notes (in each
track's Comments field).

- **Rekordbox**: File → Import → *rekordbox.xml*, or live-link it under
  File → Preferences → Bridge → "Imported Library" so it auto-refreshes.
- **Serato DJ Pro**: File → Import → Rekordbox XML (playlists become crates).
- **DJUCED** (6.3+): Library → Import Rekordbox xml, then pick which playlists to bring in.
- Engine DJ and djay import the same format.

Writing directly into Rekordbox's own database is deliberately not supported —
its schema is undocumented and changes between versions, and Serato has no
writable database at all. The XML route is the safe, version-proof standard.

## Security

- `config.json` and `spotify_oauth.json` contain credentials (Spotify client
  secret, OAuth tokens). They are **gitignored** — never commit them.
- The app binds to `localhost` only.

## Project layout

```
app.py               Flask backend (API, scanning, downloader, Mixxx sync)
static/index.html    Single-page frontend (no build step)
install.sh           LaunchAgent installer for auto-start
config.example.json  Sanitized config template
```
