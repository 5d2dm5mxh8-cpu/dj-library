# DJ Library

A self-hosted DJ music library manager that runs on your Mac and talks to
[Spotify](https://developer.spotify.com/) (via the Web API + `yt-dlp`) and to
your DJ software — Mixxx, Rekordbox, Serato DJ Pro, DJUCED or Engine DJ.
Built as a single Flask backend with a vanilla JS frontend — no Node, no build
step, no database server (SQLite).

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
- **DJ-software picker** — tell the app which DJ software you use (⚙ Settings)
  and the UI, sync buttons, workflow hints and the intro guide adapt to it.
- **Auto-export** — a `rekordbox.xml` file is kept always-current on disk after
  every change, ready to import into Rekordbox, Serato, DJUCED or Engine DJ.

## Choose your DJ software

On first launch the app shows a short intro guide asking which DJ software you
use. You can change it anytime in **⚙ Settings**. The choice changes:

- which sync buttons appear (Mixxx-only buttons hide if you pick something else),
- the workflow hints in the Tools tab and the intro guide,
- how new songs reach your software (see per-app steps below).

Mixxx is the only app that watches your music folder itself; the others import
a `rekordbox.xml` file instead — see [Syncing with your DJ software](#syncing-with-your-dj-software).

## Requirements

- macOS (uses `osascript` notifications and Spotlight search; the LaunchAgent
  auto-start is macOS-specific)
- Python 3.11+ with Flask, `mutagen`, `watchdog`, and `requests`
  (`pip install flask mutagen watchdog requests`)
- [ffmpeg](https://ffmpeg.org/) for download conversion

---

## For complete beginners — install from scratch

This section explains **every** step, assuming you've never used GitHub or a
terminal before. Read each step, copy the command into the **Terminal** app
(`Applications → Utilities → Terminal`), and press Enter.

### What is GitHub, and what are we doing?

**GitHub** is a website where programmers keep the "source code" of a project
(the plain-text instructions that make up the app). This project's code lives
at **https://github.com/5d2dm5mxh8-cpu/dj-library**.

We're going to:
1. copy the code from GitHub onto your computer,
2. install the two helper programs it needs (Python and ffmpeg),
3. tell the app where your music is (one small text file),
4. start it — it then runs in the background on your Mac forever.

You don't need a GitHub account to download or run it. (If you ever want to,
"Star" the repo on the GitHub page to bookmark it.)

### The quick way (one double-click)

If the autor has shared a pre-built `install.command` file with you (or you
downloaded the ZIP and see `install.command` inside the `dj-library-main`
folder):

1. Put `install.command` inside the `dj-library-main` folder you unzipped
   (`~/Downloads/dj-library-main`).
2. **Double-click `install.command`.** macOS may ask “open programs downloaded
   from the internet?” — click *Open*. It downloads the code you're reading
   right now, installs the small Python packages it needs, creates the settings
   file, starts the app in the background, and opens it in your browser in one
   go. You only need to do Step 2 (install Python) and Step 4 (install ffmpeg)
   below first — and only once.

The rest of this section is the fully-manual route, so you understand exactly
what's happening.

### Step 1 — Download the code

You have two options; pick one:

- **Easy (no terminal):** on the GitHub page, click the green **Code** button
  → **Download ZIP**. Your browser downloads `dj-library-main.zip`. Double-click
  it in Finder to unzip. Open the resulting `dj-library-main` folder.
- **With the terminal (allows easy updates later):**
  ```bash
  cd ~/Downloads
  git clone https://github.com/5d2dm5mxh8-cpu/dj-library.git
  cd dj-library
  ```
  (If `git` isn't installed, macOS offers to install Xcode Command Line Tools
  the first time you use it — click **Install** and wait a few minutes.)

From now on these instructions assume your copy is in
`~/Downloads/dj-library` (adjust the path if you unzipped elsewhere).

### Step 2 — Install Python 3.11+

The app is written in Python, so the Mac needs Python to run it.

- **Option A (recommended):** install [Homebrew](https://brew.sh) (paste the
  one-liner from their site into Terminal, then follow the on-screen
  instructions), then:
  ```bash
  brew install python@3.11
  ```
- **Option B (no Homebrew):** download the installer from
  https://www.python.org/downloads/macos/ (the macOS 64-bit universal2
  installer), open it, and click through — **make sure "Install" finishes
  completely**. Note that the app expects the command `python3.11`; if you only
  get `python3`, adapt the commands below accordingly.

### Step 3 — Install the app's Python packages

One command installs the four helper libraries the app needs
(Flask = the web server, mutagen = music tags, watchdog = folder watching,
requests = internet access):

```bash
pip3 install flask mutagen watchdog requests
```

On some systems you'll need `pip3 install --user ...` instead, or `python3 -m pip install ...` — use whichever variant your Mac accepts.

### Step 4 — Install ffmpeg

ffmpeg converts downloaded YouTube/Spotify audio into clean MP3 files. The
easiest way is Homebrew (see Step 2):

```bash
brew install ffmpeg
```

If you're not using Homebrew, download the "ffmpeg" build for macOS from
https://ffmpeg.org/download.html, unzip it, and move the `ffmpeg` file into
`/usr/local/bin` (Finder → Go → Go to Folder → `/usr/local/bin`).

### Step 5 — Tell the app where your music is

The app reads a small settings file from `~/.dj-library/config.json`
(the `~` means *your home folder*). Create it like this:

```bash
mkdir -p ~/.dj-library
cp ~/Downloads/dj-library/config.example.json ~/.dj-library/config.json
```

Now edit `~/.dj-library/config.json` in any text editor (TextEdit works — just
make sure it's in plain-text mode) and change `music_dir` to the absolute path
of your music folder, e.g. `/Users/yourname/Music/Mixxx/Dj Music`. Keep the
quotes and the comma exactly as shown. This is the only file you ever need to
edit by hand — everything else is managed from the app's interface.

The Spotify fields (`spotify_client_id` / `spotify_client_secret`) are
**optional** — leave them empty and the app runs fine; you just won't have the
Spotify-powered downloader (see [Spotify setup](#spotify-setup-optional-for-the-downloader)).

### Step 6 — Run the app (first time)

```bash
cd ~/Downloads/dj-library
python3.11 app.py
```

Leave that terminal window open. Open your browser and go to
**http://localhost:3000** — you should see the app. The first launch scans your
music folder, so give it a minute if you have a big library.

### Bonus — install it as a real app (PWA)

Once it's running, you can make it open in its own window with a Dock icon
(no address bar), just like a normal Mac app:

- **Safari:** with http://localhost:3000 open, click the **Share** button in the
  toolbar → **Add to Dock**. Done — a “DJ Library” app with its own icon now
  launches the library in a standalone window.
- **Chrome / Edge:** in the address bar there's an **install icon** (or the app
  shows an “Install App” button in the header). Click it to install. The app
  then opens from your Applications / Dock and works even when the server
  isn't reachable (it shows your last-loaded library).

The Flask server still needs to be running for the live features (search,
downloads, sync) — the PWA is the window, the server is the engine.

### Step 7 — Make it start automatically (optional but recommended)

Instead of running it by hand every time, install a **LaunchAgent** — macOS's
built-in system for running things in the background at login (the same
mechanism Dropbox and 1Password use). One command does it:

```bash
cd ~/Downloads/dj-library
./install.sh
```

The app is now installed in `~/.dj-library` and starts on every login,
automatically. Open **http://localhost:3000** whenever you want it.

### Updating the app

The app improves over time. To get the latest version:

```bash
cd ~/Downloads/dj-library
git pull
./install.sh        # copies the new files + restarts the service
```

Your library, crates, transitions and settings are all stored outside the code
folder (`~/.dj-library.db` and `~/.dj-library/config.json`), so updating never
touches your data.

### Something doesn't work?

- The app logs everything to `~/.dj-library/dj-library.log` — the error message
  is usually the last line. If you ask for help online, include that line.
- "python3.11: command not found" → Python didn't finish installing (Step 2).
- "ModuleNotFoundError" → you missed a package in Step 3.
- The app opens but shows no songs → `music_dir` in the config points at the
  wrong folder, or the folder has no MP3/WAV/FLAC/M4A files.

---

## Configuration

The app reads its settings from `~/.dj-library/config.json`. Most settings are
managed from the app's **⚙ Settings** tab; the file only needs manual editing
for the values below.

| Key | Purpose |
| --- | --- |
| `music_dir` | Absolute path to your music folder (the app scans and watches this) |
| `spotify_client_id` | Spotify app Client ID — **optional**; only the downloader needs it |
| `spotify_client_secret` | Spotify app Client Secret — **optional** |
| `ffmpeg_path` | Path to ffmpeg; defaults to `/opt/homebrew/bin/ffmpeg` |
| `rekordbox_export_path` | Where the always-current `rekordbox.xml` is written (changeable in ⚙ Settings) |
| `djs_software` | Your DJ software: `mixxx`, `rekordbox`, `serato`, `djuced`, `engine`, `none`, or `""` (not chosen yet) — set from the ⚙ Settings tab |
| `auto_sync_mixxx` | `true`/`false` — whether new songs are automatically pushed into Mixxx (toggle in ⚙ Settings) |

## Spotify setup (optional, for the downloader)

To enable the downloader: log in at
https://developer.spotify.com/dashboard → *Create app* → copy the Client ID /
Client Secret into `config.json`. Leave them empty to run without Spotify
features (the app prints a warning at startup).

## Run (manual)

```bash
python3 app.py
```

Then open **http://localhost:3000**. The app scans the music folder on
startup, watches it for changes, and can auto-start via LaunchAgent:

```bash
./install.sh        # installs app files + a login LaunchAgent
```

## Mixxx integration

- **Crate sync / transition sync / auto-add of new songs** write into Mixxx's
  `mixxxdb.sqlite`, which lives inside Mixxx's sandboxed app container — macOS
  blocks access by default. Grant **Full Disk Access** to the Python that runs
  the app (System Settings → Privacy & Security → Full Disk Access) and keep
  Mixxx closed while syncing (the app refuses otherwise, and always backs up
  the DB before writing).
- New songs are pushed into Mixxx automatically (matched by file path — never
  duplicated); turn this off in ⚙ Settings if you prefer.
- Transition notes sync into Mixxx track comments automatically as you add or
  edit them.

## Syncing with your DJ software

The app keeps a **`rekordbox.xml` file that is always current** — every change
(download, import, transition edit, crate change, deletion, BPM/key analysis)
triggers a background rewrite, and it regenerates on startup. This file is the
standard interchange format that all the major DJ apps import, carrying BPM,
the Camelot key, crates/playlists, and your transition notes (in each track's
Comments field).

Where it lives: `~/.dj-library/rekordbox.xml` by default — change it anytime in
**⚙ Settings → Rekordbox export file** (pick a folder; the app writes
`rekordbox.xml` inside it). Point it at a synced folder (Dropbox / iCloud /
network share) when your DJ software runs on a different computer.

Per-software workflow (also shown inside the app in ⚙ Settings → guide, and in
the first-run intro):

- **Mixxx** — nothing to do: Mixxx watches the same music folder, so your
  songs and crates are already there. Transition notes and new songs sync
  automatically.
- **Rekordbox** — *File → Import →* pick the `rekordbox.xml` once, then enable
  **Bridge** (*File → Preferences → Bridge → Imported Library*) pointed at the
  same file for fully automatic refreshes. This is the only app with a true
  live-link.
- **Serato DJ Pro** — *File → Import → Rekordbox XML* (playlists become
  crates). Re-import after adding songs; it merges by file path, so old tracks
  are never duplicated.
- **DJUCED** (6.3+) — *Library → Import Rekordbox xml*, then tick the
  playlists to bring in. DJUCED's library lives *inside* the app (not in a
  folder), so this file is the door in.
- **Engine DJ / djay** — import the same `rekordbox.xml`.

Writing directly into Rekordbox's own database is deliberately not supported —
its schema is undocumented and changes between versions, and Serato has no
writable database at all. The XML route is the safe, version-proof standard.

## Security

- `config.json` and `spotify_oauth.json` contain credentials (Spotify client
  secret, OAuth tokens). They are **gitignored** — never commit them.
- The app binds to `localhost` only.

## Project layout

```
app.py               Flask backend (API, scanning, downloader, DJ-software sync)
static/index.html    Single-page frontend (no build step)
install.sh           LaunchAgent installer for auto-start
config.example.json  Sanitized config template
```
