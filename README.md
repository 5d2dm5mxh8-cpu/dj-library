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
  analyzed. Before you hit **GET**, you can pick a crate in the dropdown next
  to the folder field — everything you download (single tracks, pasted lists,
  Spotify playlists) is then added to that crate automatically. Smart crates
  aren't offered there, since their membership comes from their rules.
  `yt-dlp` is bootstrapped and self-updated by the app, so YouTube
  breaking old builds heals itself.
- **Crates** — manual folders plus smart crates (rules on BPM, key, genre, tag,
  …); one-click sync of a crate into Mixxx.
- **Duplicates** — same-name / same-song detection that understands Finder's
  `(1)` suffixes, with a manual merge tool for anything the scan misses.
- **Transitions** — link songs together as "mix A into B" notes (or text-only
  targets), tag them (existing tags autocomplete as you type, and typing a new
  one creates it), see a song's incoming/outgoing transitions, export
  everything as a CSV or printable setlist, and sync the notes into Mixxx track
  comments and each file's ID3 comment tag — automatically.
- **Folder import** — Finder-style folder picker (browse + Spotlight search).
- **Music folder switch** — relocate the library to a different folder: move the
  tracks with you (crates and transition notes survive), adopt a
  folder that already holds the songs, or start a fresh empty library somewhere
  else (⚙ Settings → Music folder).
- **Auto-scan** — watches your music folder and picks up new files instantly.
- **Tags and genres, kept apart** — the genre field holds genres, so Mixxx's
  Genre column, the rekordbox import and genre smart crates all read a real
  genre. Freeform tags (`peak hour`, `tool`, `wip`) live on their own, set from
  the metadata editor's **Tags** field, from `#tag` tokens on a download, or by
  dropping songs on a tag-based smart crate. See
  [Tags and genres](#tags-and-genres).
- **Automatic genre tags** — optionally fills missing genre tags on new library
  tracks; existing genre tags are never replaced. For the library you already
  have, **Tools → Clean up metadata → “♪ Fill missing genres…”** scans every
  untagged track in one click and shows the proposed genre for review, with the
  same Min-confidence choice as the metadata cleanup (it starts at high) — you
  accept each genre before it is written. Genres come from the most precise
  source available (see [Genre sources](#genre-sources)); each proposal shows
  which one it came from. It fills blank genres only and records no cleanup
  decision, so a later full metadata cleanup still revisits those tracks. To fill
  the whole library without reviewing each one, the **Background genre backfill**
  does the same lookups in the background and writes genres as it goes (paced,
  cancellable, and resumable).
- **DJ-software picker** — tell the app which DJ software you use (⚙ Settings)
  and the UI, sync buttons, workflow hints and the intro guide adapt to it.
- **USB/SD export modes** — choose a Rekordbox handoff, Engine DJ handoff or a
  software-free audio copy; export the full library, one crate or every crate
  to a writable mounted USB drive or SD card. Existing media is never erased.
- **Auto-export** — a `rekordbox.xml` file is kept always-current on disk after
  every change, ready to import into Rekordbox, Serato, DJUCED or Engine DJ.
- **Relationships rail** — **◇ Relationships** in the header opens a resizable
  panel beside the table that draws your collection as one graph: genres, tags,
  artists, albums, crates, keys and transitions as separate node types, sized by
  how many tracks carry them and joined where two values share tracks. Two
  arrangements (clusters or a Camelot wheel), per-layer on/off, and findings
  computed from your own counts — each one a button, not a lecture. See
  [Relationships rail](#relationships-rail).

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

This section explains **every** step — no GitHub or terminal experience needed.
For each step, copy the command into the **Terminal** app
(`Applications → Utilities → Terminal`) and press Enter.

### What is GitHub, and what are we doing?

**GitHub** is a website where programmers keep the "source code" of a project
(the plain-text instructions that make up the app). This project's code lives
at **https://github.com/5d2dm5mxh8-cpu/dj-library**.

Here's the plan:
1. download the app's code onto your computer,
2. install the two helper programs it needs (Python and ffmpeg),
3. tell the app where your music is (one small text file),
4. start it — it then runs in the background on your Mac forever.

You don't need a GitHub account to download or run it. (If you ever want to,
"Star" the repo on the GitHub page to bookmark it.)

### The quick way (one double-click)

The app comes with a helper file called **`install.command`** that does almost
everything for you. Depending on how you got it:

- **You downloaded the ZIP** (green **Code** button → **Download ZIP**): you'll
  find `install.command` inside the unzipped `dj-library-main` folder
  (probably `~/Downloads/dj-library-main`).
- **Someone shared `install.command` with you directly**: put it anywhere handy
  (say, `~/Downloads`). The script downloads the app's code itself, so you
  don't need the ZIP at all.

Then just **double-click `install.command`**. macOS may ask “open programs
downloaded from the internet?” — click *Open*. It installs the small Python
packages the app needs, creates the settings file, starts the app in the
background, and opens it in your browser — all in one go.

Two things it can't install for you, and only once each: **Python** (Step 2)
and **ffmpeg** (Step 4). If either is missing, the script will tell you — or
just do them first using the steps below.

Prefer to know exactly what's happening under the hood? The rest of this
section walks through every step by hand.

### Step 1 — Download the app's code

What you're downloading is simply a folder of files — the app's code. You only
do this once. Pick whichever option feels easier:

- **Easy (no terminal):** open https://github.com/5d2dm5mxh8-cpu/dj-library in
  your browser and click the green **Code** button → **Download ZIP**. Your
  browser saves `dj-library-main.zip` to your Downloads folder. Double-click
  the ZIP in Finder to unzip it — you now have a `dj-library-main` folder
  containing the whole app.
- **With the terminal (makes updating easier later):**
  ```bash
  cd ~/Downloads
  git clone https://github.com/5d2dm5mxh8-cpu/dj-library.git
  cd dj-library
  ```
  (If `git` isn't installed, macOS offers to install Xcode Command Line Tools
  the first time you use it — click **Install** and wait a few minutes.)

The steps below assume your copy lives in `~/Downloads/dj-library` — if you
unzipped somewhere else (or your folder is called `dj-library-main`), just use
your folder's path instead.

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

New versions land on GitHub from time to time. To update your copy:

```bash
cd ~/Downloads/dj-library
git pull
./install.sh        # copies the new files + restarts the service
```

Your library, crates, transitions and settings are all stored outside the code
folder (`~/.dj-library.db` and `~/.dj-library/config.json`), so updating never
touches your data.

If you pulled the new code but the running app still behaves like the old one,
**restart it** — a server that is already running keeps answering with the code
it started with, even though the page you are looking at came from disk. That is
what a new feature looks like when half of it has arrived: the panel is there,
its endpoint is not. The rail says so itself ("the running app is older than this
page... restart DJ Library") instead of failing quietly. To restart the
background service without logging out:

```bash
launchctl kickstart -k gui/$(id -u)/com.milan.djlibrary
```

### Something doesn't work?

- The app logs everything to `~/.dj-library/dj-library.log` — the error message
  is usually the last line. If you ask for help online, include that line.
- "python3.11: command not found" → Python didn't finish installing (Step 2).
- "ModuleNotFoundError" → you missed a package in Step 3.
- The app opens but shows no songs → `music_dir` in the config points at the
  wrong folder, or the folder has no MP3/WAV/FLAC/M4A files.
- A new feature is half there (a panel with nothing in it, a button that does
  nothing) → the running server started before the update. Restart it:
  `launchctl kickstart -k gui/$(id -u)/com.milan.djlibrary`.

---

## Configuration

The app reads its settings from `~/.dj-library/config.json`. Most settings are
managed from the app's **⚙ Settings** tab; the file only needs manual editing
for the values below.

| Key | Purpose |
| --- | --- |
| `music_dir` | Absolute path to your music folder (the app scans and watches this). Change it in ⚙ Settings → **Music folder** — tracks are moved or the library is re-pointed, and database paths are rewritten so nothing becomes a ghost. Editing the file by hand just changes where the app looks next launch. |
| `library_name` | Optional display name shown in the app header and browser tab (changeable in ⚙ Settings → **Library name**). Cosmetic only — files stay put, so DJ software watching the folder is unaffected. |
| `spotify_client_id` | Spotify app Client ID — **optional**; only the downloader needs it |
| `spotify_client_secret` | Spotify app Client Secret — **optional** |
| `discogs_token` | Discogs personal access token — **optional**; genres work without it, but a free token raises the Discogs rate limit from 25 to 60 requests/minute |
| `ffmpeg_path` | Path to ffmpeg; defaults to `/opt/homebrew/bin/ffmpeg` |
| `rekordbox_export_path` | Where the always-current `rekordbox.xml` is written (changeable in ⚙ Settings) |
| `djs_software` | Your DJ software: `mixxx`, `rekordbox`, `serato`, `djuced`, `engine`, `none`, or `""` (not chosen yet) — set from the ⚙ Settings tab |
| `auto_sync_mixxx` | `true`/`false` — whether new songs are automatically pushed into Mixxx (toggle in ⚙ Settings) |
| `auto_genre_backfill` | `true`/`false` — whether the whole-library genre backfill runs automatically after each library scan and when new files land (toggle in ⚙ Settings) |
| `key_display` | How keys are shown: `camelot` (1A/3B) or `notation` (Am/Bbm) — display-only, stored keys are untouched (⚙ Settings) |
| `auto_install_python` | `true`/`false` — whether install.command offers to auto-install Python when it's missing (⚙ Settings) |

## Tags and genres

These are two different things, and the app keeps them apart.

A **genre** is what DJ software filters on, so it is what lands in each file's
genre tag (`TCON`): Mixxx shows it in its Genre column, and the
`rekordbox.xml` export writes it as the `Genre` attribute that Rekordbox,
Serato, DJUCED and Engine DJ import. The library's genre filter and any smart
crate rule on the genre field read that same value.

A **tag** is a freeform label you put on tracks yourself — `peak hour`, `tool`,
`wip`. Tags never go into the genre field. They live in the app's own tag
tables and travel inside the `[DJ Library]` block of each file's comment,
alongside that track's transition notes:

```
[DJ Library]
Tags: peak hour, tool
→ Another Track — Artist (6A, 124 BPM)
[/DJ Library]
```

That block is what Mixxx shows in its **Comments** column, so your tags are
visible while you play without ever masquerading as a genre.

Where tags come from:

- the metadata editor's **Tags** field (one track or a whole selection; the
  field shows the song's own tags, so clearing it removes them),
- `#tag` tokens on a download — the Raycast script's `#peak hour #tool` syntax,
  or the download API's `tags` field,
- dropping songs onto a **smart crate whose rules use tags** (its genres go to
  the genre field, its tags to the tags), and
- importing a file that already carries a `Tags:` line (so tags survive moving
  a track to another machine).

**Sorting out what you already have.** **Tools → Genres & tags → Separate tags
from genres** lists tokens that look like tags rather than genres: anything
still marked with `#`, plus genres carried by only one or two tracks — usually a
one-off or a typo. Each row offers **Move to tags** or **Keep as genre**, and
nothing moves until you choose. A library whose tags used to sit on the genre
field (`Deep House; #tool`) is separated automatically at startup: the marked
token becomes a tag and the genre field keeps the genre.

## Genre sources

Missing genres are looked up in the two keyless databases that know a track —
**Discogs release styles** and **MusicBrainz genres** — and their answers are
scored together rather than the first one that responds winning:

1. **Discogs release styles** — the vocabulary DJs actually filter on
   (*Tech House*, *Progressive House*, *Melodic Techno*), attached per release
   rather than per artist. Free and keyless; add a `discogs_token` for a higher
   rate limit.
2. **MusicBrainz** — free, keyless, curated genres. Sparser for underground and
   remix releases, and rate-limited to one request per second.
3. **Spotify artist genres** — the broadest fallback (*edm*, *house*), consulted
   only when neither database has anything for the track, and the only source
   that needs Spotify credentials.

When Discogs and MusicBrainz agree, the shared genre wins and the proposal is
credited to both (*Discogs + MusicBrainz*). When they disagree, the most
DJ-recognisable and then most specific name wins — a subtype like *Progressive
House* beats the bare umbrella *House* — with the per-release Discogs style
breaking an exact tie. Every proposal shows its source, so you can judge how
much to trust it before accepting.

### Whole-library backfill

Reviewing hundreds of proposals by hand is slow, so **Tools → Clean up metadata
→ Background genre backfill → Start** works through every track with no genre on
its own. It is deliberately:

- **paced** — lookups go through the same rate limiter as the review tool, so a
  large library never trips Discogs' limit (add a `discogs_token` to roughly
  halve the time);
- **resumable** — a track is marked once it has been looked up, so Cancel,
  quitting the app, or a crash keeps the work already done and the next Start
  continues with the tracks still outstanding — nothing is looked up twice;
- **conservative** — it writes a genre only where one is missing and never
  touches any other tag, and a track with no findable genre is left alone rather
  than guessed at.

The panel shows progress (looked up / filled / left, plus a rough time estimate)
and a Cancel button; the job runs in the background, so you can keep using the
app while it works.

**Sweeping automatically.** Turn on **Clean up metadata → Sweep genres after
scans** (config `auto_genre_backfill`) and the backfill starts on its own
after every library scan — including the scan at launch — and whenever a new
file lands in the watched folder or a folder import finishes. Every track still
missing a genre is swept up, so downloads and Finder drops get tagged without
pressing Start. It is a no-op while a sweep is already running or when nothing
is outstanding, and a sweep it starts can still be Canceled. Off by default:
since each outstanding track costs a paced Discogs/MusicBrainz lookup, a large
library takes a while to work through.

## Relationships rail

**◇ Relationships** in the header opens a panel on the right. It answers the
question the table cannot: *how is this collection put together?* Every value in
your library is a node, and two values are joined where they ride the same
track.

| Node type | One node per | Its weight |
| --- | --- | --- |
| Genres | genre value (a `House; Tech House` track makes two) | tracks carrying it |
| Tags | tag in your tag tables | tracks carrying it |
| Artists / Albums | artist / album name | tracks credited to it |
| Crates | crate, smart crates evaluated through their rules | member tracks |
| Keys | Camelot key (`6A`, `11B`) | tracks in that key |
| Transitions | track taking part in a confirmed mix | always one track |

The **Clusters** arrangement gives each layer its own region, heaviest values in
the middle. The **Camelot wheel** puts the twelve keys and their two rings on the
real wheel and orbits everything else around the key its tracks mostly sit in,
so distance from the ring means something. The `+`, `−` and `…` buttons add a
layer, remove one, or hold the arrangement switch, "show only this layer",
"reset rail width", "count the library again" and "hide the rail". The strip on
the rail's left edge resizes it (double-click resets); **⤢** lays it over the
library instead of beside it, and **✕** hides it. The width, arrangement and
layer set are remembered. On a window too narrow to hold both, the rail
automatically uses the overlay and says so rather than squeezing the table.

Click a node for what it is, how heavy it is, which key it sits in, and what it
shares tracks with — plus **Select these N tracks**, which selects them in the
table so the selection toolbar can edit, crate or delete them. Clicking empty
space clears the focus; hovering names the node under the pointer.

**Findings** are read off the same counts, never hard-coded, and each one acts — a
finding may carry more than one button when there is more than one useful next
step:

| Finding | It says | Its buttons do |
| --- | --- | --- |
| Genres carrying one track | a genre used once is usually a flavour, not a genre | opens Tools → Genres & tags |
| Filing accidents | a genre value that is a number or punctuation (`70`) | selects those tracks |
| Tracks with no genre | they show blank in Mixxx and match no genre rule | selects those tracks |
| No tags / no crates / no transitions | that layer is empty yet, and what it is for | opens that tab |
| Crates holding fewer than 3 tracks | a one-track crate is usually a slip, not a set | selects those tracks, or goes to the crates |
| Transitions pointing outside the library | a mix into a track you do not own | opens the Transitions tab, or selects the tracks whose mix leaves |
| Tracks with no key | the Camelot wheel and harmonic findings stay half-blind | starts BPM & key analysis |
| Keys that mix nowhere | 8+ tracks in a key whose neighbouring Camelot keys are empty | selects those tracks |
| Two genres holding N% of the library | where the collection actually sits | focuses both in the rail |
| Artists on exactly one track | why the artist layer is thinned | explains the count |

The graph is built by `GET /api/relationships` (read-only; it never writes to
your library), which accepts `layers=` and `arrangement=`. It is computed in one
pass over the library — every count is exact, and the panel says out loud when
it has left weaker links out to stay legible. Two rules keep a 600-track library
readable: a **node** needs at least two tracks on the artist and album layers
(one-hit artists are a finding, not 474 dots), and the strongest links win the
draw — the number left out is printed at the bottom of the panel.

## Spotify setup (optional, for the downloader)

Want the Spotify-powered downloader? Here's how to switch it on: log in at
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

## USB export modes

Open **Tools → USB export** and choose the mode that matches your setup, what
part of the library to take, and a writable mounted USB drive or SD card under
`/Volumes`. The app creates a new
`DJ Library Export …` folder; it never formats the media or replaces an older
export.

- **Pioneer / AlphaTheta — Rekordbox XML handoff:** copies the selected audio
  and writes a `rekordbox.xml` pointing at the USB copies. Crates in this XML
  become playlists in Rekordbox, not physical crate folders. Import the XML,
  then export/prepare the USB in Rekordbox. Use Rekordbox 7.2.11+ when you need
  both older Device Library and newer OneLibrary-compatible players; the model
  and firmware decide which library format it reads. DJ Library does not create
  cue points, grids, or the proprietary player database.
- **Denon / Engine DJ — XML handoff:** the Rekordbox XML is an import starting
  point; its crates become Engine playlists, not guaranteed audio folders.
  Import the collection in Engine DJ, then use Sync Manager to prepare the
  Engine drive. Choose one crate or import all crates according to the workflow
  you want. DJ Library never writes Engine's proprietary database.
- **Audio files only:** copies audio without a DJ desktop app, with M3U8
  playlists, a CSV catalogue and a README. Choose either one copy per track in
  library-relative folders, or real folders per crate (shared tracks are
  duplicated; uncrated full-library tracks are grouped together in
  `Crates/Uncrated`). One-crate,
  selected-crate and all-crates exports can choose either layout; crate-folder
  layout is preselected for crate exports. The **Export entire library** action
  keeps the chosen layout and, in crate-folder mode, puts all uncrated songs
  together in `Crates/Uncrated`. Folder browsing and M3U8 playback depend
  on the player; compatible CDJs can browse folders without a Rekordbox
  database, but playlists, search and performance data vary by model/firmware.

Choose **the entire library**, **one crate**, or **all crates**. Smart crates
are evaluated when the export is made; empty crates are reported in results
without creating empty audio folders. Physical crate-folder layouts are only for
audio-copy exports and are not guaranteed by native-software exports. A general
M3U8 and one M3U8 per crate reference the copied files. These are portable
playlists, not a format every player supports. Space estimates include duplicate
copies before confirmation. Empty crates produce playlists but no empty audio
folders. Nothing is converted, formatted, or overwritten.

For the supported workflows, see [AlphaTheta’s current USB library format
compatibility notice](https://alphatheta.com/en/information/important-notice-for-customers-using-usb-devices-with-our-dj-equipment/),
[Denon DJ’s Rekordbox-to-Engine import guide](https://support.denondj.com/en/support/solutions/articles/69000798942-how-to-import-a-rekordbox-collection-into-engine-dj-playlists-hot-cues-loops-beat-grid-)
and [Engine DJ’s USB export guide](https://support.enginedj.com/support/solutions/articles/69000844056-engine-dj-adding-music-and-exporting-to-a-drive).

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

**You never have to download-and-replace this file by hand.** In the Tools tab,
**↻ Refresh live rekordbox.xml** rewrites it in place right now and tells you
where it landed; **⬇ Download a copy** is only for carrying the file to another
machine. Since the file itself is always current, the only thing your DJ
software ever needs is a path pointing at it.

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

## Raycast quick download (optional)

Use [Raycast](https://www.raycast.com)? The included `raycast/spotify-dl.sh`
script downloads a song in a few keystrokes — paste a Spotify link, a song
name, a whole pasted list of Spotify track URLs, or a Spotify playlist link
(the full playlist downloads), plus an optional subfolder and crate name. Name a crate that doesn't exist yet and the script
**creates it** (as a manual crate) and downloads into it; naming a smart crate
shows a warning instead, since downloads can't be added to one.

You can also add **tags**: put `#tag` tokens in either optional field
(e.g. `Demos #wip` as the folder, or `#peak-hour #tool #groove` as the
crate field). Tags are stored as tags — never as the genre (see
[Tags and genres](#tags-and-genres)) — so they stay out of Mixxx's Genre
column, smart crates can filter on them by name, and no metadata cleanup can
ever overwrite them. Re-downloading something you already own with a tag
stamps the tag onto it.

To install:

1. Copy the script into your Raycast scripts folder:
   ```bash
   mkdir -p ~/.raycast-scripts
   cp raycast/spotify-dl.sh ~/.raycast-scripts/
   ```
2. In Raycast, open **Extensions → Scripts → Add Script Directory** and pick
   `~/.raycast-scripts`.
3. Run **spotify-dl**, paste your input, then optionally a subfolder and a
   crate name.

The script talks to the app's API on `localhost:3000`, so the DJ Library app
must be running (it is, if you installed the LaunchAgent).

## Security

- `config.json` and `spotify_oauth.json` contain credentials (Spotify client
  secret, OAuth tokens). They are **gitignored** — never commit them.
- The app binds to `localhost` only.

## Project layout

```
app.py               Flask application, library APIs, scanning, downloader and DJ-software sync
usb_export_routes.py USB-export HTTP boundary; delegates to the app-owned exporter service
usb_export.py        USB-export planning, mounted-volume checks, file writing and job lifecycle
relationships_routes.py  Relationship-graph HTTP boundary (read-only /api/relationships)
relationships.py     Node/edge weights, hubs and the findings, computed from the library rows
static/index.html    Single-page frontend shell and existing application UI (no build step)
static/tag-utils.js  Tag helpers shared by the UI (parsing, crate rules, genre merge)
static/usb-export.js USB-export UI controller and progress polling
static/relationships-layout.js  Rail geometry: layers, widths, Camelot slots, cluster and wheel layout
static/relationships.js         Relationships rail UI: fetch, canvas drawing, controls, findings
install.sh           LaunchAgent installer for auto-start

USB export data flows from the UI controller to the Flask blueprint, then to a
single lazily initialized `USBExporter` stored in `app.extensions`. The service
is the sole owner of the in-process export job state; routes only parse requests
and shape responses.

The relationships rail is split the same way: `relationships.py` builds the graph
from rows (injected `get_db`, `to_camelot` and `crate_song_ids`, so it is testable
without Flask), `relationships_routes.py` only parses the query string, and the
drawing is split into the pure geometry in `static/relationships-layout.js` and
the panel in `static/relationships.js`.
config.example.json  Sanitized config template
raycast/spotify-dl.sh  Raycast script: quick download with crate auto-create and #tags
```
