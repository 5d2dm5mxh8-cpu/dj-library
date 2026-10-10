import os
import sys
import json
import sqlite3
import threading
import subprocess
import hashlib
import re
import time
import io
import csv
import html
import queue
import urllib.request
import mimetypes
import shutil
from datetime import datetime
import usb_export as usb_export_module
import usb_export_routes
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file, Response
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TBPM, COMM, ID3NoHeaderError
import requests

app = Flask(__name__, static_folder='static')

_config = json.load(open(os.path.expanduser("~/.dj-library/config.json")))
MUSIC_DIR = _config.get("music_dir", "/Users/you/Music")
DB_PATH   = os.path.expanduser("~/.dj-library.db")
# Spotify app credentials live in config.json now (keys "spotify_client_id" /
# "spotify_client_secret") so you can swap them without touching code.
SPOTIFY_CLIENT_ID     = _config.get("spotify_client_id", "")
SPOTIFY_CLIENT_SECRET = _config.get("spotify_client_secret", "")
# yt-dlp is a self-updating standalone binary kept inside the app folder, so
# the downloader can heal itself when YouTube blocks an outdated build (which
# happens every few weeks). It is always launched via sys.executable (the
# Python this app runs under), so its shebang line is irrelevant.
YT_DLP  = os.path.expanduser("~/.dj-library/bin/yt-dlp")
YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
FFMPEG  = _config.get("ffmpeg_path") or "/opt/homebrew/bin/ffmpeg"
# The rekordbox.xml that DJUCED / Rekordbox / Serato import from is kept
# always-current on disk by an auto-export that fires after every library
# change. Point this at a synced folder (Dropbox / iCloud / network share) if
# the DJ software runs on a different computer.
REKORDBOX_EXPORT_PATH = os.path.expanduser(
    _config.get("rekordbox_export_path", "~/.dj-library/rekordbox.xml"))
# Which DJ software the user performs with ("mixxx", "rekordbox", "serato",
# "djuced", "engine", or "" = not chosen). Drives UI adaptation + workflow
# hints, and is changeable in the Settings tab (stored in config.json).
DJS_SOFTWARE = _config.get("djs_software", "")
# Whether newly-added songs are auto-pushed into Mixxx's library (Settings
# tab toggle; default on).
AUTO_SYNC_MIXXX = bool(_config.get("auto_sync_mixxx", True))
# How musical keys are shown: "camelot" wheel (1A/3B) or "notation"
# (Am/Bbm). Display-only -- stored keys are never rewritten.
KEY_DISPLAY = _config.get("key_display", "camelot")
# Whether new external installs should auto-install Python if it's missing
# (the installer asks, defaulting to this saved preference).
AUTO_INSTALL_PYTHON = bool(_config.get("auto_install_python", True))
# Whether freshly-downloaded tracks get their metadata cleaned up automatically
# against Spotify. Off by default: the first-run onboarding proposes it, and the
# Cleanup tool in Settings can switch it anytime. Only HIGH-confidence matches are
# applied straight away; weaker ones are left for the manual review tool
# (see auto_cleanup_download).
AUTO_CLEANUP_METADATA = bool(_config.get("auto_cleanup_metadata", False))
# Fill empty genre tags for newly added tracks from their matched genre sources
# (Discogs and MusicBrainz, scored together with Spotify as a fallback; see
# resolve_genre). Existing genres and user-entered tags are never replaced by
# this setting.
AUTO_SPOTIFY_GENRE = bool(_config.get("auto_spotify_genre", False))
# Sweep the whole-library genre backfill automatically after each library scan
# (and when new files land in the watched folder), so tracks that arrive with no
# genre are filled without pressing Start. Off by default: the sweep makes a
# paced Discogs/MusicBrainz lookup per outstanding track, so a big library takes
# a while (see start_genre_backfill).
AUTO_GENRE_BACKFILL = bool(_config.get("auto_genre_backfill", False))
# Optional free Discogs personal access token. Discogs needs no key, but a
# token raises its rate limit from 25 to 60 requests/minute for the genre
# lookups (see _discogs_genre).
DISCOGS_TOKEN = str(_config.get("discogs_token", "")).strip()
# Optional display name for the library (shown in the app header and the
# browser tab). Purely cosmetic -- files stay where they are, so DJ software
# that watches the same folder is never affected.
LIBRARY_NAME = str(_config.get("library_name", "")).strip()[:60]


def _config_dict():
    """The settings the frontend needs, as a dict (shared by GET + POST)."""
    return {'music_dir': MUSIC_DIR,
            'music_dir_changing': _music_dir_job['active'],
            'rekordbox_export_path': REKORDBOX_EXPORT_PATH,
            'djs_software': DJS_SOFTWARE,
            'auto_sync_mixxx': AUTO_SYNC_MIXXX,
            'key_display': KEY_DISPLAY,
            'auto_install_python': AUTO_INSTALL_PYTHON,
            'auto_cleanup_metadata': AUTO_CLEANUP_METADATA,
            'auto_spotify_genre': AUTO_SPOTIFY_GENRE,
            'auto_genre_backfill': AUTO_GENRE_BACKFILL,
            'library_name': LIBRARY_NAME}


def _save_config(updates):
    """Merges `updates` into config.json (preserving existing keys, including
    the Spotify secrets) and refreshes the affected module-level settings.
    Only settings that can change at runtime are updated here."""
    global _config, MUSIC_DIR, REKORDBOX_EXPORT_PATH, DJS_SOFTWARE, AUTO_SYNC_MIXXX, KEY_DISPLAY, AUTO_INSTALL_PYTHON, AUTO_CLEANUP_METADATA, AUTO_SPOTIFY_GENRE, AUTO_GENRE_BACKFILL, LIBRARY_NAME
    path = os.path.expanduser("~/.dj-library/config.json")
    data = dict(_config)
    data.update(updates)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    _config = data
    if 'rekordbox_export_path' in updates:
        REKORDBOX_EXPORT_PATH = os.path.expanduser(updates['rekordbox_export_path'])
    if 'djs_software' in updates:
        DJS_SOFTWARE = updates['djs_software']
    if 'auto_sync_mixxx' in updates:
        AUTO_SYNC_MIXXX = bool(updates['auto_sync_mixxx'])
    if 'key_display' in updates:
        KEY_DISPLAY = updates['key_display']
    if 'auto_install_python' in updates:
        AUTO_INSTALL_PYTHON = bool(updates['auto_install_python'])
    if 'auto_cleanup_metadata' in updates:
        AUTO_CLEANUP_METADATA = bool(updates['auto_cleanup_metadata'])
    if 'auto_spotify_genre' in updates:
        AUTO_SPOTIFY_GENRE = bool(updates['auto_spotify_genre'])
    if 'auto_genre_backfill' in updates:
        AUTO_GENRE_BACKFILL = bool(updates['auto_genre_backfill'])
    if 'library_name' in updates:
        LIBRARY_NAME = str(updates['library_name']).strip()[:60]
    if 'music_dir' in updates:
        # music_dir is normally changed through the /api/library/folder switch
        # (which also relocates/relinks the tracks); _save_config just keeps
        # the module global in sync with whatever config.json now says.
        MUSIC_DIR = os.path.expanduser(updates['music_dir'])

# -- WATCHER -----------------------------------------------------------------

_watcher = None
_watcher_lock = threading.Lock()

def _start_watcher():
    """(Re)points the folder watcher at the current MUSIC_DIR. Called at startup
    and again after every music-folder switch, so newly dropped files are
    always picked up no matter where the library lives now."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            try: _watcher.stop()
            except Exception: pass
            try: _watcher.join(timeout=2)
            except Exception: pass
        if not os.path.isdir(MUSIC_DIR):
            _log(f"Watcher: music folder missing, not watching: {MUSIC_DIR}")
            _watcher = None
            return
        _watcher = Observer()
        _watcher.schedule(MusicFolderHandler(), MUSIC_DIR, recursive=True)
        _watcher.start()
        _log(f"Watcher: watching {MUSIC_DIR}")

# -- MUSIC FOLDER SWITCH ------------------------------------------------------
# Lets you relocate the library to a different folder -- either by MOVING the
# files to a new folder, or by ADOPTING a folder that already holds the tracks
# ("this folder has songs in it, make it the library").
#
# Switching is deliberately not just a config edit: the songs table matches
# rows by absolute filepath, so after a plain path change the DB would hold a
# full catalog of ghosts and rescan the new folder from scratch, losing every
# crate membership and transition link. Instead the switch runs in
# a background job that rewrites the DB paths along with the move.
#
# Two switches in a row share the same job id (a constant), so the frontend
# can poll one URL regardless of how many switches happened this session --
# the newest job simply replaces the older one.

MUSIC_DIR_JOB_ID = 'music-dir-switch'

# Dict access stays atomic under CPython's GIL (each handler only mutates
# whole keys), so this needs no extra lock -- same as download_status.
_music_dir_job = {'active': False}


def _find_old_song_prefixes(base_dir):
    """Distinct directory prefixes that DB song paths sit under. Normally just
    the old library folder, but a previous switch that moved only a subset of
    the files can leave rows pointing elsewhere -- those still get the same
    rewrite treatment so no song is ever left behind as a ghost. The folder
    being switched AWAY from arrives as a parameter, because MUSIC_DIR has
    already been flipped to the new folder by the time this runs."""
    base = os.path.abspath(base_dir).rstrip(os.sep)
    with get_db() as db:
        rows = db.execute('SELECT filepath FROM songs').fetchall()
    prefixes = set()
    for r in rows:
        folder = os.path.dirname(os.path.abspath(r['filepath']))
        while True:
            prefixes.add(folder)
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
    # Only folders at or BELOW the old library root qualify. Ancestors of the
    # root (e.g. ~/Music for a library at ~/Music/DJ) must NOT match: with
    # them in the set, a stray row elsewhere in the tree gets rewritten to a
    # garbage path and then purged by the rescan, losing its crate memberships.
    return [p for p in prefixes if p == base or p.startswith(base + os.sep)]


def _music_dir_worker(old_dir, new_dir, mode, create, delete_old):
    """Background worker for a music-folder switch.

    old_dir       -- the folder the library is switching AWAY from (passed
                     explicitly: the route flips config + MUSIC_DIR before
                     this thread starts).
    mode 'move'   -- tracks still live under the old folder: move them to the
                     new one, keeping every relative subfolder, then rewrite
                     the DB paths.
    mode 'adopt'  -- the tracks are already in the new folder: verify they are
                     there, then rewrite the DB paths.
    mode 'fresh'  -- brand-new library: the old files stay where they are, the
                     catalog is cleared (crates and transitions survive), so
                     the new folder starts empty.
    create        -- os.makedirs the new folder first (new-library flow).
    delete_old    -- remove the old folder tree afterwards (move mode only;
                     ignored for fresh, where the old folder still holds files).
    """
    global MUSIC_DIR
    old_dir = os.path.abspath(old_dir).rstrip(os.sep)
    new_dir_abs = os.path.abspath(new_dir).rstrip(os.sep)
    status = download_status[MUSIC_DIR_JOB_ID]
    moved, missing = 0, 0

    try:
        os.makedirs(new_dir_abs, exist_ok=True)

        if mode == 'move' and os.path.isdir(old_dir) and old_dir != new_dir_abs:
            # Precompute the full prefix map once -- Path.replace refuses to
            # overwrite existing files, which is exactly the safety we want.
            renames = []
            for root, dirs, files in os.walk(old_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, old_dir)
                    dst = os.path.join(new_dir_abs, rel)
                    if os.path.exists(dst):
                        status['failed'] = (f"Cannot move: '{rel}' already exists in "
                                            "the new folder. No files were moved -- "
                                            "empty the new folder (or merge the copies "
                                            "by hand) and try again.")
                        return
                    renames.append((src, dst, rel))
            status['total'] = len(renames)
            for src, dst, rel in renames:
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    Path(src).replace(dst)
                    moved += 1
                    status['done'] = moved
                except OSError as e:
                    _log(f"Music move failed {src} -> {dst}: {e}")
                    missing += 1

        # Rewrite the DB paths. Every row under a known old prefix gets the
        # same old->new surgery; rows under prefixes that were never moved
        # (partial previous switches) keep their already-correct paths. The
        # LONGEST matching prefix wins: the candidate set also contains
        # ancestors of old_dir, and matching '/tmp' instead of
        # '/old/library' would send every path to a garbage destination.
        prefix_map = [(p, new_dir_abs + p[len(old_dir):]) for p in _find_old_song_prefixes(old_dir)]
        prefix_map.sort(key=lambda pm: len(pm[0]), reverse=True)
        with get_db() as db:
            rows = db.execute('SELECT id, filepath FROM songs').fetchall()
            updates = []
            for r in rows:
                old_path = os.path.abspath(r['filepath'])
                for p, np in prefix_map:
                    if old_path == p or old_path.startswith(p + os.sep):
                        rel = old_path[len(p):].lstrip(os.sep)
                        updates.append((os.path.join(np, rel), r['id']))
                        break
                else:
                    # Song lived somewhere outside the old prefixes -- leave it
                    # alone; it was never part of the moved set.
                    continue
            for fp, sid in updates:
                db.execute('UPDATE songs SET filepath=? WHERE id=?', (fp, sid))
            db.commit()
        _log(f"Music folder switched: {old_dir} -> {new_dir_abs} "
             f"({moved} files moved, {len(updates)} DB rows rewritten, {missing} failed)")

        if delete_old and mode != 'fresh' and old_dir != new_dir_abs and os.path.isdir(old_dir):
            try:
                shutil.rmtree(old_dir)
            except OSError as e:
                _log(f"Old folder cleanup skipped: {e}")

        # The watcher must follow the new folder before the job is marked
        # done, otherwise files dropped immediately after the switch are missed.
        if mode == 'fresh':
            # Fresh start: nothing was moved, so every rewritten path is now a
            # ghost -- clear the catalog (crate memberships and transition
            # links go with it; the crates and folders themselves stay).
            with get_db() as db:
                ids = [r['id'] for r in db.execute('SELECT id FROM songs').fetchall()]
                _delete_song_rows(db, ids)
                db.commit()
            _log(f"Fresh library in {new_dir_abs}: cleared {len(ids)} songs "
                 "(crates and transitions kept)")

        MUSIC_DIR = new_dir_abs
        _start_watcher()
        queue_auto_export()
        # New files may have been uncovered (adopt) or brought over (move):
        # pull them into the catalog without waiting for the next startup scan.
        threading.Thread(target=scan_library, daemon=True).start()
        status.update({'done': moved, 'moved': moved, 'missing': missing,
                       'status': 'success'})
    except Exception as e:
        _log(f"Music folder switch failed: {e}")
        status.update({'status': 'failed', 'failed': str(e)})
    finally:
        _music_dir_job['active'] = False


@app.route('/api/library/folder', methods=['POST'])
def switch_music_dir():
    """Switches the library to a different folder. Body:

        {"folder": "/new/path",             (required)
         "mode": "move"|"adopt"|"fresh",   (default: move)
         "create": true,                    (create the folder if missing)
         "delete_old": false}               (remove the old folder when done,
                                             move mode only)

    'move'  = take my tracks with me (the files are moved, DB paths rewritten;
              playlists and transition notes all survive).
    'adopt' = this folder already holds the tracks -- just make it the library
              (every DB path is rewritten to the new folder, so nothing is
              re-scanned from scratch and crate memberships survive).
    'fresh' = start a brand-new library here: the old files stay put, the
              catalog is cleared (crates and transitions survive).
    """
    global MUSIC_DIR
    if _music_dir_job['active']:
        return jsonify({'error': 'A music-folder switch is already running'}), 409
    data = request.json or {}
    raw = str(data.get('folder', '')).strip()
    if not raw:
        return jsonify({'error': 'Folder path required'}), 400
    new_dir = os.path.abspath(os.path.expanduser(raw))
    if new_dir == '/':
        return jsonify({'error': 'Refusing to use / as the library'}), 400
    if not os.path.isdir(new_dir):
        if not data.get('create'):
            return jsonify({'error': "Folder does not exist (pick an existing folder, or use 'Start an empty new library' to create one)"}), 400
        try:
            os.makedirs(new_dir)
        except OSError as e:
            return jsonify({'error': f'Could not create folder: {e}'}), 400
    mode = data.get('mode') or 'move'
    if mode not in ('move', 'adopt', 'fresh'):
        return jsonify({'error': 'mode must be "move", "adopt" or "fresh"'}), 400
    if mode == 'adopt' and not any(
            f.lower().endswith(AUDIO_EXTS) for f in os.listdir(new_dir)):
        return jsonify({'error': 'That folder has no audio files in it -- nothing to adopt'}), 400

    old_dir = os.path.abspath(MUSIC_DIR).rstrip(os.sep)
    if new_dir == old_dir:
        return jsonify({'error': 'That is already the library folder'}), 400
    # Guard against nesting: with the new folder inside the old one, moving
    # files into the new folder moves them into themselves; with the old
    # folder inside the new one, "delete old folder" would eat the library.
    if new_dir.startswith(old_dir + os.sep):
        return jsonify({'error': 'The new folder is inside the current library folder'}), 400
    if old_dir.startswith(new_dir + os.sep):
        return jsonify({'error': 'The current library folder is inside the new folder'}), 400

    _music_dir_job['active'] = True
    download_status[MUSIC_DIR_JOB_ID] = {
        'status': 'moving', 'total': 0, 'done': 0,
        'moved': 0, 'missing': 0, 'failed': None
    }
    # Flip config + MUSIC_DIR first: the switch survives even if the process
    # dies mid-move (files can be finished by a second switch or by hand).
    _save_config({'music_dir': new_dir})
    threading.Thread(target=_music_dir_worker,
                     args=(old_dir, new_dir, mode, bool(data.get('create')),
                           bool(data.get('delete_old'))),
                     daemon=True).start()
    return jsonify({'ok': True, 'job_id': MUSIC_DIR_JOB_ID,
                    'music_dir': new_dir, 'old_dir': old_dir})


@app.route('/api/library/switch-status/<job_id>')
def music_dir_switch_status(job_id):
    return jsonify(download_status.get(job_id, {'status': 'unknown'}))

# -- DATABASE --------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT UNIQUE NOT NULL,
            title TEXT, artist TEXT, album TEXT, year TEXT, genre TEXT,
            duration_seconds REAL, file_size_bytes INTEGER,
            bpm REAL, musical_key TEXT,
            file_hash TEXT, spotify_id TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS crates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_smart INTEGER DEFAULT 0,
            rules TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS crate_songs (
            crate_id INTEGER, song_id INTEGER,
            PRIMARY KEY (crate_id, song_id)
        )''')
        # User-controlled crate order in the sidebar (drag & drop). New crates
        # are appended (max+1); a reorder rewrites the whole section.
        try:
            db.execute("ALTER TABLE crates ADD COLUMN sort_order INTEGER DEFAULT 0")
            db.commit()
        except: pass
        # Transition links between songs: a directional "mix A into B" note.
        # to_song_id links to another library song; to_text is used instead for
        # text-only targets (tracks you don't own yet). notes/tag are freeform
        # mix notes (technique, timing, energy level, ...).
        db.execute('''CREATE TABLE IF NOT EXISTS transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_song_id INTEGER NOT NULL,
            to_song_id INTEGER,
            to_text TEXT,
            notes TEXT,
            tag TEXT,
            status TEXT DEFAULT 'confirmed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.commit()
        for col in ['album TEXT','year TEXT','genre TEXT','file_size_bytes INTEGER',
                    'bpm REAL','musical_key TEXT',
                    "cleanup_scanned_at TIMESTAMP", "cleanup_decision TEXT",
                    # Last Spotify proposal for this track (JSON), so reopening
                    # the cleanup panel doesn't re-query the API for matches it
                    # already made.
                    "cleanup_proposal_json TEXT",
                    # When a bulk genre backfill looked this track up (whether or
                    # not it found anything). Marks which tracks are still to do,
                    # so the job resumes after a restart/cancel instead of
                    # starting over -- and a track with no findable genre is not
                    # retried forever.
                    "genre_backfill_at TIMESTAMP"]:
            try:
                db.execute(f'ALTER TABLE songs ADD COLUMN {col}')
                db.commit()
            except: pass
        # Migration for DBs created before the 'status' column existed:
        # add the column, then backfill pre-existing rows as 'confirmed'.
        try:
            db.execute("ALTER TABLE transitions ADD COLUMN status TEXT DEFAULT 'confirmed'")
            db.commit()
            db.execute("UPDATE transitions SET status='confirmed' WHERE status IS NULL")
            db.commit()
        except: pass
        # Folders for grouping transitions in the Transitions tab. A transition
        # lives in at most one folder (folder_id NULL = "All transitions").
        # Single-row state for the resumable "fill every missing genre" job.
        # Persisted so progress survives an app restart: the worker picks up
        # from the un-attempted tracks rather than starting over.
        db.execute('''CREATE TABLE IF NOT EXISTS genre_backfill (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT NOT NULL DEFAULT 'idle',
            total INTEGER NOT NULL DEFAULT 0,
            attempted INTEGER NOT NULL DEFAULT 0,
            filled INTEGER NOT NULL DEFAULT 0,
            current_id INTEGER,
            current_title TEXT,
            started_at TIMESTAMP,
            updated_at TIMESTAMP,
            error TEXT
        )''')
        db.execute('INSERT OR IGNORE INTO genre_backfill(id, status) VALUES(1, \'idle\')')
        db.commit()
        db.execute('''CREATE TABLE IF NOT EXISTS transition_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.commit()
        try:
            db.execute("ALTER TABLE transitions ADD COLUMN folder_id INTEGER")
            db.commit()
        except: pass
        # Mashup tracks: a transition row with is_mashup_track=1 has the mashup
        # itself as its SOURCE; its normal target is the OUTPUT song (where the
        # mashup lands), and mashup_input_* names the INPUT song it blends in
        # from -- a library song id or free text, exactly one of the pair.
        try:
            db.execute("ALTER TABLE transitions ADD COLUMN is_mashup_track INTEGER DEFAULT 0")
            db.execute("ALTER TABLE transitions ADD COLUMN mashup_input_song_id INTEGER")
            db.execute("ALTER TABLE transitions ADD COLUMN mashup_input_text TEXT")
            db.commit()
        except: pass
        # Freeform tags get their own tables -- deliberately NOT the genre
        # column. `genre` stays a real genre (Mixxx's Genre column, the
        # rekordbox.xml Genre attribute, every smart crate built on genre),
        # while tags travel in the file's [DJ Library] comment block. A tag
        # exists only as long as a song carries it, and matching is
        # case-insensitive, so "Tool" and "tool" are one tag.
        db.execute('''CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name ON tags(lower(name))')
        db.execute('''CREATE TABLE IF NOT EXISTS song_tags (
            song_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (song_id, tag_id)
        )''')
        db.commit()

# -- FILE UTILS --------------------------------------------------------------

def file_hash(filepath):
    h = hashlib.md5()
    try:
        with open(filepath,'rb') as f: h.update(f.read(65536))
        return h.hexdigest()
    except: return None

def get_bitrate(filepath):
    """
    Returns the audio bitrate in bits per second -- this is the real measure
    of audio quality, more reliable than file size alone since file size also
    depends on song duration. A 3-minute 320kbps file and a 6-minute 160kbps
    file can end up roughly the same size despite very different quality.
    """
    try:
        audio = MP3(filepath)
        return audio.info.bitrate
    except:
        return 0

# YouTube auto-assigns broad category tags -- not musical genres -- to uploaded
# tracks. "Music" and "People & Blogs" carry no musical meaning, so they are
# dropped when reading tags, otherwise they pollute the genre filter.
NOISE_GENRES = {'music', 'people & blogs'}

def split_genre_tags(raw):
    """Splits a raw genre string into (genre, tags).

    A '#'-prefixed token is a TAG -- the marker the download API and the
    Raycast script use -- and every other token is a genre. This is the single
    place the two are told apart, so a file whose genre still carries
    '#peak-hour' (written back when tags rode on the genre field) lands with a
    clean genre and a real tag instead."""
    if not raw:
        return None, []
    genres, tags = [], []
    for part in str(raw).split(';'):
        part = part.strip()
        if not part:
            continue
        if part.startswith('#'):
            tag = clean_tag(part)
            if tag and tag.lower() not in [t.lower() for t in tags]:
                tags.append(tag)
            continue
        if part.lower() in NOISE_GENRES:
            continue
        if part.lower() not in [g.lower() for g in genres]:
            genres.append(part)
    return ('; '.join(genres) or None), tags

def clean_genre(genre):
    """Strips meaningless category tags and freeform '#tags' from a genre string.
    Handles semicolon-separated multi-genre values (e.g. 'Music; House' ->
    'House') and returns None if nothing meaningful remains. Tags are dropped
    here on purpose: the genre field is for genres only."""
    return split_genre_tags(genre)[0]

def extract_metadata(filepath):
    title=artist=album=year=genre=duration=None
    try:
        audio = MP3(filepath)
        duration = audio.info.length
        try:
            tags = ID3(filepath)
            title  = str(tags.get('TIT2','')).strip() or None
            artist = str(tags.get('TPE1','')).strip() or None
            album  = str(tags.get('TALB','')).strip() or None
            year   = str(tags.get('TDRC','')).strip() or None
            genre  = clean_genre(str(tags.get('TCON','')).strip() or None)
        except: pass
    except: pass
    if not title or not artist:
        name = clean_noise(Path(filepath).stem)
        if ' - ' in name:
            parts = name.split(' - ',1)
            artist = artist or parts[0].strip()
            title  = title  or parts[1].strip()
        else:
            title = title or name
    return title, artist, album, year, genre, duration

def write_metadata(filepath, title=None, artist=None, album=None,
                   year=None, genre=None, bpm=None):
    try:
        try: tags = ID3(filepath)
        except ID3NoHeaderError: tags = ID3()
        if title:  tags['TIT2'] = TIT2(encoding=3, text=title)
        if artist: tags['TPE1'] = TPE1(encoding=3, text=artist)
        if album:  tags['TALB'] = TALB(encoding=3, text=album)
        if year:   tags['TDRC'] = TDRC(encoding=3, text=str(year))
        if genre:  tags['TCON'] = TCON(encoding=3, text=genre)
        if bpm:    tags['TBPM'] = TBPM(encoding=3, text=str(round(float(bpm))))
        tags.save(filepath, v2_version=3)
        return True
    except Exception as e:
        print(f"Tag write failed {filepath}: {e}")
        return False

# -- TAGS --------------------------------------------------------------------
# Tags (peak hour, tool, wip, ...) live in the `tags` / `song_tags` tables and
# in the file's [DJ Library] comment block -- never in the genre field. Genre
# has to stay a real genre: Mixxx reads it as the Genre column, the
# rekordbox.xml Genre attribute comes from it, and smart crates filter on it.
# Nothing here is allowed to write a tag into `genre`, or a genre into a tag.

TAG_MAX_LENGTH = 40

def clean_tag(name):
    """Normalises one tag for storage: a leading '#' -- the marker the download
    API and the Raycast script use -- is dropped, whitespace collapses, and a
    value that reads as nothing ('#', 'Music') returns None."""
    if name is None:
        return None
    tag = str(name).strip()
    while tag.startswith('#'):
        tag = tag[1:].strip()
    tag = re.sub(r'\s+', ' ', tag)
    if not tag or tag.lower() in NOISE_GENRES:
        return None
    return tag[:TAG_MAX_LENGTH]

def parse_tag_string(value):
    """Parses a tag field from the download API: '#house #tool' or 'house,
    tool; peak hour' -> ['house', 'tool', 'peak hour']. Returns []"""
    if not value or not isinstance(value, str):
        return []
    raw = re.split(r'[;,]|\s+#', value)
    return [t.strip() for t in raw if t.strip()]

def merge_genres(existing, genres):
    """Adds genres to a song's ';'-separated genre string without duplicating
    anything it already carries (case-insensitive). Genres only -- a '#tag' is
    never merged into the genre field."""
    parts = [p.strip() for p in (existing or '').split(';') if p.strip()]
    for g in genres or []:
        g = (g or '').strip()
        if g and not g.startswith('#') and not any(p.lower() == g.lower() for p in parts):
            parts.append(g)
    return '; '.join(parts)

def _clean_tag_list(names):
    """Cleaned tags with case-insensitive duplicates removed, order kept."""
    out, seen = [], set()
    for n in (names or []):
        t = clean_tag(n)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out

def _tag_id(db, name):
    """The id of `name`, created on first use. Matching is case-insensitive, so
    an existing 'Tool' is reused for 'tool' and its stored spelling wins."""
    row = db.execute('SELECT id FROM tags WHERE lower(name)=lower(?)', (name,)).fetchone()
    if row:
        return row['id']
    db.execute('INSERT INTO tags (name) VALUES(?)', (name,))
    return db.execute('SELECT last_insert_rowid()').fetchone()[0]

def _drop_orphan_tags(db):
    """Deletes tag rows nothing carries any more -- a tag exists only as long as
    a song is tagged with it."""
    db.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM song_tags)')

def add_song_tags(song_id, names, db=None):
    """Links tags to one song (creating them as needed) and returns the tags the
    song actually gained, in the order given. Idempotent, and never touches the
    genre field."""
    wanted = _clean_tag_list(names)
    if not wanted:
        return []
    added = []
    def run(conn):
        if not conn.execute('SELECT 1 FROM songs WHERE id=?', (song_id,)).fetchone():
            return False
        for name in wanted:
            tag_id = _tag_id(conn, name)
            had = conn.execute('SELECT 1 FROM song_tags WHERE song_id=? AND tag_id=?',
                               (song_id, tag_id)).fetchone()
            conn.execute('INSERT OR IGNORE INTO song_tags (song_id, tag_id) VALUES(?,?)',
                         (song_id, tag_id))
            if not had:
                added.append(name)
        conn.commit()
        return True
    ok = run(db) if db is not None else _with_db(run)
    return added if ok else []

def remove_song_tags(song_id, names, db=None):
    """Unlinks tags from one song (case-insensitive) and drops any tag no song
    carries any more. Returns the tags that were actually removed."""
    unwanted = _clean_tag_list(names)
    if not unwanted:
        return []
    removed = []
    def run(conn):
        for name in unwanted:
            row = conn.execute('SELECT id FROM tags WHERE lower(name)=lower(?)',
                               (name,)).fetchone()
            if not row:
                continue
            cur = conn.execute('DELETE FROM song_tags WHERE song_id=? AND tag_id=?',
                               (song_id, row['id']))
            if cur.rowcount:
                removed.append(name)
        _drop_orphan_tags(conn)
        conn.commit()
        return True
    run(db) if db is not None else _with_db(run)
    return removed

def set_song_tags(song_id, names, db=None):
    """Replaces one song's tags with `names` (the metadata editor's contract:
    what you see in the field is what is saved). Returns (added, removed)."""
    wanted = _clean_tag_list(names)
    have = get_song_tags(song_id)
    keep = [w.lower() for w in wanted]
    removed = remove_song_tags(song_id, [t for t in have if t.lower() not in keep], db)
    held = [h.lower() for h in have]
    added = add_song_tags(song_id, [t for t in wanted if t.lower() not in held], db)
    return added, removed

def get_song_tags(song_id, db=None):
    """One song's tags, alphabetically. Pass db to reuse an open connection
    (callers mid-transaction would otherwise deadlock on the write lock)."""
    def run(conn):
        return [r['name'] for r in conn.execute(
            'SELECT t.name FROM song_tags st JOIN tags t ON t.id=st.tag_id '
            'WHERE st.song_id=? ORDER BY lower(t.name)', (song_id,)).fetchall()]
    return run(db) if db is not None else _with_db(run)

def tags_for_songs(song_ids):
    """{song_id: [tag, ...]} for a page of songs -- one query for the whole
    batch instead of one per row (the song list, the info panel and the
    rekordbox export all need this)."""
    ids = [i for i in (song_ids or []) if i is not None]
    if not ids:
        return {}
    out = {i: [] for i in ids}
    placeholders = ','.join('?' * len(ids))
    with get_db() as db:
        rows = db.execute('SELECT st.song_id, t.name FROM song_tags st '
                          'JOIN tags t ON t.id=st.tag_id WHERE st.song_id IN '
                          f'({placeholders}) ORDER BY lower(t.name)', ids).fetchall()
    for r in rows:
        out.setdefault(r['song_id'], []).append(r['name'])
    return out

def _tags_from_payload(value):
    """Tags from an API payload: a list of tags, or a string like
    '#tool, peak hour; wip'."""
    if value is None:
        return []
    raw = list(value) if isinstance(value, (list, tuple)) else parse_tag_string(str(value))
    return _clean_tag_list(raw)

def _with_tags(songs):
    """Attaches each song's tags to its dict, one query for the whole page."""
    tags = tags_for_songs([s['id'] for s in songs])
    for s in songs:
        s['tags'] = tags.get(s['id'], [])
    return songs

def tag_song(song_id, names):
    """Tags one song and refreshes its file comment, so the tag also reaches
    the file (and Mixxx's comment column). Returns the tags gained."""
    added = add_song_tags(song_id, names)
    if added:
        sync_song_comment(song_id)
        queue_auto_export()
    return added

def write_file_genre(filepath, genre):
    """Writes the cleaned genre to the file's TCON, clearing the frame when the
    track has no genre left. write_metadata skips empty values, which would
    otherwise leave a stale genre -- or an old '#tag' -- on disk."""
    if genre:
        write_metadata(filepath, genre=genre)
        return
    try:
        try: id3 = ID3(filepath)
        except ID3NoHeaderError: id3 = ID3()
        if 'TCON' in id3:
            del id3['TCON']
            id3.save(filepath, v2_version=3)
    except Exception as e:
        print(f"Genre clear failed {filepath}: {e}")

def tag_song_at_path(filepath, names):
    """tag_song for a caller that only knows the path (the downloader's
    already-owned shortcut)."""
    with get_db() as db:
        row = db.execute('SELECT id FROM songs WHERE filepath=?', (filepath,)).fetchone()
    return tag_song(row['id'], names) if row else []

def separate_genre_tags():
    """Moves '#tag' tokens out of the genre field into the tag store.

    Run once at startup (and from the Tools review screen). Idempotent, and a
    no-op for a library whose genres are already genres: a token only moves
    when it carries the '#' marker. Returns (songs_changed, tags_moved)."""
    try:
        with get_db() as db:
            rows = db.execute("SELECT id, filepath, genre FROM songs "
                              "WHERE genre LIKE '%#%'").fetchall()
        songs_changed = tags_moved = 0
        for row in rows:
            genre, tags = split_genre_tags(row['genre'])
            if not tags:
                continue
            with get_db() as db:
                db.execute('UPDATE songs SET genre=? WHERE id=?', (genre, row['id']))
                db.commit()
            add_song_tags(row['id'], tags)
            write_file_genre(row['filepath'], genre)
            sync_song_comment(row['id'])
            songs_changed += 1
            tags_moved += len(tags)
        if songs_changed:
            queue_auto_export()
            print(f"Tag separation: {tags_moved} tag(s) moved off {songs_changed} song(s)")
        return songs_changed, tags_moved
    except Exception as e:
        print(f"Tag separation failed: {e}")
        return 0, 0

# -- SPOTIFY API -------------------------------------------------------------
# -- OAUTH USER AUTHENTICATION -----------------------------------------------

OAUTH_TOKEN_PATH = os.path.expanduser('~/.dj-library/spotify_oauth.json')
SCOPES = 'playlist-read-private playlist-read-collaborative'

_oauth = {'access_token': None, 'refresh_token': None, 'expires': 0}

def load_oauth_tokens():
    global _oauth
    if os.path.exists(OAUTH_TOKEN_PATH):
        try:
            with open(OAUTH_TOKEN_PATH) as f:
                _oauth.update(json.load(f))
        except: pass

def save_oauth_tokens():
    with open(OAUTH_TOKEN_PATH, 'w') as f:
        json.dump(_oauth, f)

def get_oauth_token():
    import time
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    if _oauth.get('access_token') and time.time() < _oauth.get('expires', 0):
        return _oauth['access_token']
    if _oauth.get('refresh_token'):
        resp = requests.post('https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': _oauth['refresh_token']
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
        data = resp.json()
        if 'access_token' in data:
            _oauth['access_token'] = data['access_token']
            _oauth['expires'] = time.time() + data.get('expires_in', 3600) - 60
            save_oauth_tokens()
            return _oauth['access_token']
    return None

def is_oauth_connected():
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and _oauth.get('refresh_token'))

@app.route('/auth/login')
def spotify_login():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return '<script>window.location="/?auth=error"</script>'
    import urllib.parse, secrets
    state = secrets.token_hex(8)
    params = {
        'client_id': SPOTIFY_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': 'http://127.0.0.1:3000/callback',
        'scope': SCOPES,
        'state': state,
    }
    url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode(params)
    from flask import redirect
    return redirect(url)

@app.route('/callback')
def spotify_callback():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return '<script>window.location="/?auth=error"</script>'
    import time
    code = request.args.get('code')
    error = request.args.get('error')
    if error or not code:
        return '<script>window.location="/?auth=error"</script>'
    resp = requests.post('https://accounts.spotify.com/api/token',
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': 'http://127.0.0.1:3000/callback',
        },
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
    data = resp.json()
    if 'access_token' in data:
        _oauth['access_token'] = data['access_token']
        _oauth['refresh_token'] = data.get('refresh_token')
        _oauth['expires'] = time.time() + data.get('expires_in', 3600) - 60
        save_oauth_tokens()
        return '<script>window.location="/?auth=success"</script>'
    return '<script>window.location="/?auth=error"</script>'

@app.route('/api/auth/status')
def auth_status():
    return jsonify({'connected': is_oauth_connected()})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    _oauth['access_token'] = None
    _oauth['refresh_token'] = None
    _oauth['expires'] = 0
    if os.path.exists(OAUTH_TOKEN_PATH):
        os.remove(OAUTH_TOKEN_PATH)
    return jsonify({'ok': True})

_token_cache = {'token': None, 'expires': 0}

def get_spotify_token():
    """Returns a Spotify API token. Prefers the user's OAuth token (the only
    kind that can read playlists, including private ones); falls back to a
    client-credentials token (public lookups only) when no user is connected."""
    import time
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    user_token = get_oauth_token()
    if user_token:
        return user_token
    if _token_cache['token'] and time.time() < _token_cache['expires']:
        return _token_cache['token']
    resp = requests.post('https://accounts.spotify.com/api/token',
        data={'grant_type':'client_credentials'},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
    data = resp.json()
    token = data.get('access_token')
    _token_cache['token'] = token
    _token_cache['expires'] = time.time() + data.get('expires_in', 3600) - 60
    return token

def get_track_info(spotify_url):
    track_id = re.search(r'track/([A-Za-z0-9]+)', spotify_url)
    if not track_id: return None
    token = get_spotify_token()
    if not token: return None
    resp = requests.get(f'https://api.spotify.com/v1/tracks/{track_id.group(1)}',
        headers={'Authorization': f'Bearer {token}'})
    data = resp.json()
    if 'error' in data: return None
    artists = ', '.join(a['name'] for a in data.get('artists',[]))
    return {
        'id': data.get('id'),
        'title': data.get('name'),
        'artist': artists,
        'album': data.get('album',{}).get('name'),
        'year': str(data.get('album',{}).get('release_date',''))[:4],
        'duration_ms': data.get('duration_ms'),
    }

def get_track_audio_features(spotify_id):
    token = get_spotify_token()
    if not token: return None, None
    resp = requests.get(f'https://api.spotify.com/v1/audio-features/{spotify_id}',
        headers={'Authorization': f'Bearer {token}'})
    data = resp.json()
    if 'error' in data or 'tempo' not in data: return None, None
    bpm = round(data['tempo'], 1)
    key_names = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
    key_idx = data.get('key', -1)
    mode    = data.get('mode', -1)
    if key_idx >= 0 and mode >= 0:
        key = f"{key_names[key_idx]} {'maj' if mode==1 else 'min'}"
    else:
        key = None
    return bpm, key

def get_playlist_tracks(spotify_url):
    """Returns (tracks, playlist_name, status). status is 'ok' when the
    playlist was read successfully (tracks may still be empty), 'blocked'
    when Spotify refuses to serve the playlist's tracks to third-party apps
    (popular or other users' playlists can be off-limits), or 'error'."""
    playlist_id = re.search(r'playlist/([A-Za-z0-9]+)', spotify_url)
    if not playlist_id: return [], None, 'error'
    token = get_spotify_token()
    if not token: return [], None, 'error'
    meta = requests.get(f'https://api.spotify.com/v1/playlists/{playlist_id.group(1)}?fields=name',
        headers={'Authorization': f'Bearer {token}'}).json()
    if 'error' in meta: return [], None, 'error'
    playlist_name = meta.get('name','Playlist')
    # Spotify's API now serves playlist tracks from /items, with each entry's
    # track nested under 'item' (older accounts still use /tracks + 'track').
    # Try the current endpoint first and fall back to the legacy one.
    last_status = None
    for endpoint, track_key in (('items', 'item'), ('tracks', 'track')):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id.group(1)}/{endpoint}?limit=100'
        status, tracks = None, []
        while url:
            resp = requests.get(url, headers={'Authorization': f'Bearer {token}'})
            data = resp.json()
            if 'error' in data:
                status, tracks = data['error'].get('status'), []
                break
            for entry in data.get('items', []):
                t = entry.get(track_key)
                if t and t.get('id'):
                    artists = ', '.join(a['name'] for a in t.get('artists', []))
                    tracks.append({
                        'id': t['id'],
                        'title': t.get('name'),
                        'artist': artists,
                        'album': t.get('album', {}).get('name'),
                        'year': str(t.get('album', {}).get('release_date', ''))[:4],
                        'duration_ms': t.get('duration_ms'),
                    })
            url = data.get('next')
        if status is None:
            return tracks, playlist_name, 'ok'
        last_status = status
    if last_status == 403:
        return [], playlist_name, 'blocked'
    return [], playlist_name, 'error'

# -- BPM / KEY ANALYSIS -------------------------------------------------------

KEY_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']

def analyze_bpm_key_librosa(filepath):
    try:
        import librosa, numpy as np
        y, sr = librosa.load(filepath, sr=22050, duration=30, mono=True)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(round(float(tempo[0]) if hasattr(tempo,'__len__') else float(tempo), 1))
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        cm = chroma.mean(axis=1)
        major = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        minor = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
        mc = [np.corrcoef(np.roll(major,i),cm)[0,1] for i in range(12)]
        nc = [np.corrcoef(np.roll(minor,i),cm)[0,1] for i in range(12)]
        bm = max(range(12),key=lambda i:mc[i])
        bn = max(range(12),key=lambda i:nc[i])
        key = f"{KEY_NAMES[bm]} maj" if mc[bm]>=nc[bn] else f"{KEY_NAMES[bn]} min"
        return bpm, key
    except Exception as e:
        print(f"Librosa analysis failed: {e}")
        return None, None

def analyze_and_store(filepath, song_id, spotify_id=None, queue_export=True):
    bpm, key = None, None
    if spotify_id:
        bpm, key = get_track_audio_features(spotify_id)
        print(f"Spotify BPM for {Path(filepath).name}: {bpm} {key}")
    if not bpm:
        bpm, key = analyze_bpm_key_librosa(filepath)
        print(f"Librosa BPM for {Path(filepath).name}: {bpm} {key}")
    if bpm or key:
        with get_db() as db:
            db.execute('UPDATE songs SET bpm=?, musical_key=? WHERE id=?', (bpm,key,song_id))
            db.commit()
        if bpm:
            write_metadata(filepath, bpm=bpm)
        # Keys/BPM ride in the rekordbox.xml -- refresh the on-disk export. The
        # library-wide pass disables this per-song and queues once at the end.
        if queue_export:
            queue_auto_export()

_bpm_analysis_lock = threading.Lock()

def analyze_library_bpm():
    # Only one analysis pass at a time -- the startup thread and a manual
    # "Analyze BPM" click could otherwise both scan the library at once.
    if not _bpm_analysis_lock.acquire(blocking=False):
        print("BPM analysis already running, skipping...")
        return
    try:
        with get_db() as db:
            songs = db.execute('SELECT id, filepath, spotify_id FROM songs WHERE bpm IS NULL').fetchall()
        print(f"Analyzing BPM for {len(songs)} songs...")
        for row in songs:
            if os.path.exists(row['filepath']):
                analyze_and_store(row['filepath'], row['id'], row['spotify_id'],
                                  queue_export=False)
        print("BPM analysis complete")
        if songs:
            # Analysis may have changed keys/BPM -- refresh the on-disk export.
            queue_auto_export()
    finally:
        _bpm_analysis_lock.release()

# -- NOISE / DUPLICATE --------------------------------------------------------

NOISE = [r'\(spotisaver\)',r'\(official.*?\)',r'\[official.*?\]',r'\[.*?video.*?\]',
         r'\(.*?video.*?\)',r'\(.*?audio.*?\)',r'\[.*?audio.*?\]',r'\(.*?lyric.*?\)',
         r'\(.*?remix.*?\)',r'\(.*?edit.*?\)',r'\(.*?mix.*?\)',r'\(.*?radio.*?\)',
         r'\(.*?extended.*?\)',r'\(.*?remaster.*?\)',r'\(.*?version.*?\)',
         r'\(feat\.?.*?\)',r'\(ft\.?.*?\)',r'feat\.?\s+\S+',r'ft\.?\s+\S+',
         r'\s*-\s*(radio edit|extended mix|club mix|original mix|album version)$']

def strip_finder_suffix(s):
    """Removes Finder/OS-style duplicate suffixes from the END of a name, e.g.
    'Animals (1)' -> 'Animals', 'Animals 1' -> 'Animals', 'Animals copy 2'
    -> 'Animals'. Finder appends ' (1)' (then ' (2)'...) when you copy a file
    into a folder that already contains it, so the two copies otherwise look
    like different tracks even though they're the same song.

    Deliberately aggressive: a bare trailing number ('Song 2' -> 'song') is
    also stripped so re-downloaded copies whose ID3 title carries the ' 1'
    suffix still match. That can in principle over-match a legitimate 'Part 2'
    title, but duplicate grouping always requires matching artist/duration or
    filename afterwards, so false groups are rare in practice."""
    t = re.sub(r'\s*\(\d{1,3}\)\s*$', ' ', s)             # (1)
    t = re.sub(r'\s*\[\d{1,3}\]\s*$', ' ', t)             # [2]
    t = re.sub(r'\s*\(\s*copy\s*\)\s*$', ' ', t)         # (copy)
    t = re.sub(r'\s*-\s*\d{1,3}\s*$', ' ', t)              # - 3
    t = re.sub(r'\s+(copy|copy\s+of)(\s+\d+)?\s*$', ' ', t)  # copy 2 / copy of
    t = re.sub(r'\s+\d{1,3}\s*$', ' ', t)                   # bare trailing 1
    t = re.sub(r'\s*[-_]\s*$', ' ', t)                       # leftover ' -'/' _'
    return t.rstrip()

def clean_noise(text):
    if not text: return ''
    t = text.lower()
    for p in NOISE: t = re.sub(p,'',t,flags=re.IGNORECASE)
    t = strip_finder_suffix(t)
    return re.sub(r'\s+',' ',re.sub(r'[^\w\s]','',t)).strip()

def clean_filename(filename):
    """Normalized filename for duplicate grouping: strips the extension and any
    Finder-style duplicate suffix, so 'Animals (1).mp3' and 'Animals.mp3' both
    normalize to 'animals'."""
    return clean_noise(Path(filename).stem)

# A song can match another several ways at once; keep the strongest label per
# pair. Shared by find_duplicates_for_file and get_duplicates so the watcher,
# folder import and the review modal all judge matches the same way.
TYPE_PRIORITY = {'exact': 3, 'fuzzy': 2, 'name': 1}

def titles_match(a,b): return clean_noise(a)==clean_noise(b)

def artists_match(a,b):
    if not a or not b: return False
    def sp(s): return {clean_noise(p) for p in re.split(r'[,&/]|\band\b',s.lower()) if clean_noise(p)}
    return bool(sp(a)&sp(b))

def artists_compatible(a,b):
    """Artist check for duplicate candidates. If both tracks carry artist
    tags they must overlap; if either side is missing its tag the check
    passes -- title + duration already matched, and untagged tracks are
    common (many library rows here have no artist tag at all)."""
    if not a or not b: return True
    return artists_match(a,b)

def durations_close(d1,d2,tol=15):
    if d1 is None or d2 is None: return False
    return abs(d1-d2)<=tol

def find_duplicates_for_file(filepath, title, artist, duration):
    fhash = file_hash(filepath)
    fname = clean_filename(Path(filepath).name)
    results = []
    with get_db() as db:
        songs = [dict(r) for r in db.execute('SELECT * FROM songs WHERE filepath!=?',(filepath,)).fetchall()]
    for row in songs:
        if fhash and row.get('file_hash')==fhash:
            results.append((row,'exact')); continue
        if (title and row.get('title') and titles_match(title,row['title'])
            and artists_compatible(artist or '',row.get('artist',''))
            and durations_close(duration,row.get('duration_seconds'))):
            results.append((row,'fuzzy')); continue
        # Finder copy: same normalized filename (duplicate suffix stripped) with
        # a matching duration or title -- "Animals (1).mp3" vs "Animals.mp3"
        # even when the re-copied file has different audio bytes and tags.
        if fname and row.get('filename') and clean_filename(row['filename'])==fname \
                and (durations_close(duration,row.get('duration_seconds'))
                     or (title and row.get('title') and titles_match(title,row['title']))):
            results.append((row,'name'))
    return results

# -- SCAN ----------------------------------------------------------------

def scan_library():
    added = 0
    new_paths = []
    for root,_,files in os.walk(MUSIC_DIR):
        for fname in files:
            if Path(fname).suffix.lower() in ('.mp3','.wav','.flac','.m4a'):
                fp = os.path.join(root,fname)
                if add_file_to_db(fp):
                    added += 1
                    new_paths.append(fp)
    # Purge database rows whose files no longer exist on disk -- files deleted
    # in Finder (or moved out of the library) otherwise leave ghost entries
    # forever, and folders that should be empty keep showing up in the sidebar.
    # Only purge when the library folder itself is reachable; if it's ever
    # missing (drive not mounted etc.) we must not wipe the whole catalog.
    purged = 0
    if os.path.isdir(MUSIC_DIR):
        with get_db() as db:
            rows = db.execute('SELECT id, filepath FROM songs').fetchall()
            missing = [r for r in rows if not os.path.exists(r['filepath'])]
            _delete_song_rows(db, [r['id'] for r in missing])
            db.commit()
        purged = len(missing)
    # Songs discovered by this scan (added while the app was closed, or picked
    # up by the manual rescan button) get pushed into Mixxx in the background.
    if new_paths:
        auto_sync_new_songs(new_paths)
        for filepath in new_paths:
            schedule_auto_cleanup_for_path(filepath)
    # The library changed (adds or purges) -- refresh the on-disk rekordbox.xml.
    if added or purged:
        queue_auto_export()
    # With "sweep genres after scans" on, a scan is the cue to fill any track
    # still missing a genre. A no-op when the setting is off or nothing is left.
    maybe_auto_genre_backfill('scan')
    return added, purged

def _delete_song_rows(db, song_ids):
    """Deletes song rows and their crate memberships and transition links from
    the database. Does not touch files on disk -- callers remove those
    separately. Transitions are removed in both directions so no orphaned
    "from"/"to" links ever point at a deleted song."""
    for song_id in song_ids:
        db.execute('DELETE FROM crate_songs WHERE song_id=?', (song_id,))
        db.execute('DELETE FROM transitions WHERE from_song_id=? OR to_song_id=?',
            (song_id, song_id))
        # The song may also be named as a mashup input on other rows -- the
        # ingredient is simply unset there (the row itself stays).
        db.execute('UPDATE transitions SET mashup_input_song_id=NULL WHERE mashup_input_song_id=?',
            (song_id,))
        db.execute('DELETE FROM song_tags WHERE song_id=?', (song_id,))
        db.execute('DELETE FROM songs WHERE id=?', (song_id,))
    # Tags exist only while some song carries them, so drop any left behind.
    _drop_orphan_tags(db)

def add_file_to_db(filepath):
    if not os.path.exists(filepath): return False
    title,artist,album,year,genre,duration = extract_metadata(filepath)
    fhash = file_hash(filepath)
    fsize = os.path.getsize(filepath)
    filename = Path(filepath).name
    with get_db() as db:
        ex = db.execute('SELECT id FROM songs WHERE filepath=?',(filepath,)).fetchone()
        if ex:
            db.execute('''UPDATE songs SET file_hash=?,duration_seconds=?,title=?,
                artist=?,album=?,year=?,genre=?,file_size_bytes=? WHERE filepath=?''',
                (fhash,duration,title,artist,album,year,genre,fsize,filepath))
            db.commit(); return False
        db.execute('''INSERT OR IGNORE INTO songs
            (filename,filepath,title,artist,album,year,genre,duration_seconds,file_size_bytes,file_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (filename,filepath,title,artist,album,year,genre,duration,fsize,fhash))
        db.commit()
        row = db.execute('SELECT id FROM songs WHERE filepath=?', (filepath,)).fetchone()
    # A track arriving from another machine keeps the tags this app wrote into
    # its [DJ Library] comment block (or its old genre-field '#tags').
    if row:
        add_song_tags(row['id'], _file_tags(filepath, genre))
    return True

def _file_tags(filepath, raw_genre=None):
    """Tags carried by a file: the [DJ Library] comment block's Tags line, plus
    any '#tag' token still sitting in its genre (pre-separation libraries). """
    tags = list(split_genre_tags(raw_genre)[1]) if raw_genre else []
    try:
        try: id3 = ID3(filepath)
        except ID3NoHeaderError: id3 = ID3()
        for key in list(id3.keys()):
            frame = id3[key]
            if isinstance(frame, COMM) and frame.desc == 'DJ Library':
                for line in str(frame).splitlines():
                    if line.strip().lower().startswith('tags:'):
                        tags += [t.strip() for t in line.split(':', 1)[1].split(',') if t.strip()]
    except Exception:
        pass
    return _clean_tag_list(tags)

# -- DOWNLOAD --------------------------------------------------------------

download_status = {}

def _run_ytdlp(args):
    """Runs yt-dlp with the same interpreter this app is running under, so the
    binary's own shebang (#!/usr/bin/env python3) never matters -- on this Mac
    that env python is an old Xcode shim that yt-dlp no longer supports."""
    return subprocess.run([sys.executable, YT_DLP] + args,
                          capture_output=True, text=True)

def _log(msg):
    with open(os.path.expanduser('~/.dj-library/dj-library.log'), 'a') as lf:
        lf.write(msg + "\n")

def ensure_ytdlp():
    """Downloads the standalone yt-dlp binary into the app folder on first use.
    Returns True if a usable binary is present afterwards."""
    if os.path.exists(YT_DLP) and os.path.getsize(YT_DLP) > 100_000:
        return True
    try:
        os.makedirs(os.path.dirname(YT_DLP), exist_ok=True)
        _log(f"YT-DLP: downloading standalone binary from {YT_DLP_URL}")
        urllib.request.urlretrieve(YT_DLP_URL, YT_DLP + '.tmp')
        os.chmod(YT_DLP + '.tmp', 0o755)
        os.replace(YT_DLP + '.tmp', YT_DLP)
        _log(f"YT-DLP: downloaded {os.path.getsize(YT_DLP)} bytes")
        return True
    except Exception as e:
        _log(f"YT-DLP: bootstrap download failed: {e}")
        return False

def update_ytdlp():
    """Self-updates the standalone binary (YouTube blocks outdated builds with
    403s, so the downloader refreshes yt-dlp and retries once on failure)."""
    if not os.path.exists(YT_DLP): return False
    try:
        r = _run_ytdlp(['--update'])
        _log(f"YT-DLP: update rc={r.returncode} out={r.stdout[:200]!r} err={r.stderr[:200]!r}")
        return r.returncode == 0
    except Exception as e:
        _log(f"YT-DLP: update failed: {e}")
        return False

def refresh_ytdlp():
    """Startup refresh: makes sure the yt-dlp binary exists and is the latest
    build so downloads never begin with a version YouTube already blocks.
    Runs in a background thread -- when the binary is current, --update is a
    fast no-op; any failure is logged and the download-time heal path (update
    + retry once) still covers it later."""
    try:
        if ensure_ytdlp() and update_ytdlp():
            _log("YT-DLP: startup refresh OK")
    except Exception as e:
        _log(f"YT-DLP: startup refresh failed: {e}")

def do_download(query, spotify_id=None, title=None, artist=None,
                album=None, year=None, subfolder=None, crate_id=None, tags=None):
    output_dir = os.path.join(MUSIC_DIR, subfolder) if subfolder else MUSIC_DIR
    os.makedirs(output_dir, exist_ok=True)
    job_id = spotify_id or hashlib.md5(query.encode()).hexdigest()[:16]
    download_status[job_id] = {'status':'downloading','query':query}

    if spotify_id:
        with get_db() as db:
            ex = db.execute('SELECT * FROM songs WHERE spotify_id=?',(spotify_id,)).fetchone()
            if ex:
                # Already owned -- still stamp requested tags onto it so
                # re-downloading with a tag always tags, never no-ops.
                tag_song_at_path(ex['filepath'], tags)
                download_status[job_id] = {'status':'duplicate','existing':dict(ex)}
                return

    if not ensure_ytdlp():
        download_status[job_id] = {'status':'failed','error':'could not fetch yt-dlp binary'}
        return

    def run():
        return _run_ytdlp([
            f"ytsearch1:{query} official audio",
            '--extract-audio',
            '--audio-format', 'mp3',
            '--audio-quality', '0',
            '--format', 'bestaudio/best',
            '-o', os.path.join(output_dir,'%(title)s.%(ext)s'),
            '--no-update', '--js-runtimes', 'node',
            '--ffmpeg-location', FFMPEG,
            '--no-warnings',
            '--print', 'after_move:filepath'
        ])

    result = run()
    # YouTube regularly blocks outdated yt-dlp builds with HTTP 403 -- refresh
    # the binary and retry once before giving up, so downloads heal themselves.
    if result.returncode != 0:
        _log(f"YT-DLP: first attempt failed (rc={result.returncode}), updating + retrying")
        _log(f"YT-DLP stderr: {result.stderr[:300]}")
        if update_ytdlp():
            result = run()
        _log(f"YT-DLP: retry rc={result.returncode} out={result.stdout[:200]!r} err={result.stderr[:200]!r}")

    if result.returncode == 0:
        downloaded_path = result.stdout.strip()
        if downloaded_path and os.path.exists(downloaded_path):
            write_metadata(downloaded_path, title=title, artist=artist,
                          album=album, year=year)
            if add_file_to_db(downloaded_path):
                if crate_id:
                    with get_db() as db:
                        db.execute('INSERT OR IGNORE INTO crate_songs (crate_id, song_id) '
                                   'SELECT ?, id FROM songs WHERE filepath=?',
                                   (crate_id, downloaded_path))
                        db.commit()
                if tags:
                    tag_song_at_path(downloaded_path, tags)
                # Newly downloaded song -- push it into Mixxx in the background.
                auto_sync_new_songs([downloaded_path])
                queue_auto_export()
            with get_db() as db:
                if spotify_id:
                    db.execute('UPDATE songs SET spotify_id=? WHERE filepath=?',
                        (spotify_id,downloaded_path))
                    db.commit()
                row = db.execute('SELECT id FROM songs WHERE filepath=?',(downloaded_path,)).fetchone()
                if row:
                    threading.Thread(target=analyze_and_store,
                        args=[downloaded_path, row['id'], spotify_id], daemon=True).start()
                    # Best-effort metadata cleanup: apply only high-confidence
                    # Spotify matches straight away; weaker ones wait for review.
                    schedule_auto_cleanup(row['id'], cleanup_download=True)
        download_status[job_id] = {'status':'success','file':downloaded_path}
        subprocess.run(['osascript','-e',
            f'display notification "Downloaded: {title or query}" with title "DJ Library"'])
    else:
        download_status[job_id] = {'status':'failed','error':result.stderr[:200]}
        subprocess.run(['osascript','-e',
            f'display notification "Failed: {title or query}" with title "DJ Library"'])

# -- FOLDER WATCHER ----------------------------------------------------------

class MusicFolderHandler(FileSystemEventHandler):
    # While a music-folder switch is moving files the old watcher is already
    # detached, so this guard is normally redundant -- it just makes the short
    # window between "folder changed in config" and "watcher restarted" safe.
    def on_any_event(self, event):
        if _music_dir_job['active']:
            return

    def on_created(self, event):
        if event.is_directory: return
        if Path(event.src_path).suffix.lower() not in ('.mp3','.wav','.flac','.m4a'): return
        threading.Timer(2.0, self._process, args=[event.src_path]).start()
    def on_deleted(self, event):
        if event.is_directory: return
        if Path(event.src_path).suffix.lower() not in ('.mp3','.wav','.flac','.m4a'): return
        threading.Timer(1.0, self._remove, args=[event.src_path]).start()
    def on_moved(self, event):
        # A move inside the library is a delete of the old path + create of the
        # new one; watchdog only reports the destination to on_created, so we
        # remove the old path from the database ourselves.
        if event.is_directory: return
        if Path(event.src_path).suffix.lower() not in ('.mp3','.wav','.flac','.m4a'): return
        threading.Timer(1.0, self._remove, args=[event.src_path]).start()
    def _process(self, filepath):
        title,artist,_,_,_,duration = extract_metadata(filepath)
        dupes = find_duplicates_for_file(filepath,title,artist,duration)
        if dupes:
            dupe_name = dupes[0][0].get('filename', '?')
            subprocess.run(['osascript','-e',
                f'display notification "Possible duplicate of: {dupe_name}" with title "DJ Library"'])
        if add_file_to_db(filepath):
            # Genuinely new song (Finder drop, download, or import landing in the
            # watched folder) -- push it into Mixxx's library in the background.
            auto_sync_new_songs([filepath])
            queue_auto_export()
            schedule_auto_cleanup_for_path(filepath)
            # A download or Finder drop that lands with no genre gets swept up
            # too (no-op unless the setting is on).
            maybe_auto_genre_backfill('new file')
    def _remove(self, filepath):
        """Removes a song row (and its crate memberships) when its file is
        deleted or moved out of the watched library folder in Finder."""
        with get_db() as db:
            row = db.execute('SELECT id FROM songs WHERE filepath=?', (filepath,)).fetchone()
            if row:
                _delete_song_rows(db, [row['id']])
                db.commit()
                queue_auto_export()

# -- SMART CRATE EVALUATION ---------------------------------------------------

def evaluate_smart_crate(rules):
    conditions, params = [], []
    for r in rules:
        field = r.get('field')
        op    = r.get('op')
        val   = r.get('value')
        # Tags are their own field: a rule can filter on one (exact or
        # containing) alongside the genre rules.
        if field == 'tag':
            if op == 'eq':
                conditions.append('EXISTS (SELECT 1 FROM song_tags st JOIN tags t '
                                  'ON t.id=st.tag_id WHERE st.song_id=songs.id '
                                  'AND lower(t.name)=lower(?))')
                params.append(str(val).strip())
            elif op == 'contains':
                conditions.append('EXISTS (SELECT 1 FROM song_tags st JOIN tags t '
                                  'ON t.id=st.tag_id WHERE st.song_id=songs.id '
                                  'AND lower(t.name) LIKE ?)')
                params.append(f'%{str(val).strip().lower()}%')
            continue
        if field not in ('bpm','duration_seconds','genre','musical_key','artist','title','year'): continue
        if op == 'gte':      conditions.append(f'COALESCE({field}, 0) >= ?'); params.append(val)
        elif op == 'lte':    conditions.append(f'COALESCE({field}, 0) <= ?'); params.append(val)
        elif op == 'eq':     conditions.append(f'{field} = ?');  params.append(val)
        elif op == 'contains':
            conditions.append(f'{field} LIKE ?'); params.append(f'%{val}%')
        elif op == 'gt':     conditions.append(f'COALESCE({field}, 0) > ?'); params.append(val)
        elif op == 'lt':     conditions.append(f'COALESCE({field}, 0) < ?'); params.append(val)
    if not conditions: return []
    where = ' AND '.join(conditions)
    with get_db() as db:
        rows = db.execute(f'SELECT * FROM songs WHERE {where} ORDER BY artist, title', params).fetchall()
    return [dict(r) for r in rows]

# -- MIXXX CRATE SYNC ---------------------------------------------------------
# Mixxx stores its entire library (tracks, crates, playlists) in a
# single SQLite database file. A "crate" in Mixxx is really just a table that
# links crate IDs to track IDs from Mixxx's own `library` table -- so for us to
# add a song to a Mixxx crate, that song must already exist in Mixxx's library
# (meaning you've opened Mixxx and it scanned the file at least once).
#
# We NEVER write to this database while Mixxx is running -- SQLite doesn't
# handle two different programs writing to the same file well, and Mixxx
# keeps its own connection open the whole time it's running. Writing at the
# same time risks corrupting your entire Mixxx library, so we check for the
# running process first and refuse if it's open.

import glob
import shutil
# (datetime comes from the top-of-file "from datetime import datetime")

def find_mixxx_db():
    """Mixxx stores its database in a per-user Application Support folder."""
    candidates = glob.glob(os.path.expanduser(
        "~/Library/Containers/org.mixxx.mixxx/Data/Library/Application Support/Mixxx/mixxxdb.sqlite"
    ))
    if not candidates:
        candidates = glob.glob(os.path.expanduser(
            "~/Library/Application Support/Mixxx/mixxxdb.sqlite"
        ))
    return candidates[0] if candidates else None

def is_mixxx_running():
    result = subprocess.run(['pgrep', '-x', 'mixxx'], capture_output=True, text=True)
    return bool(result.stdout.strip())

def sync_crate_to_mixxx(crate_name, song_filepaths):
    """
    Creates or updates a Mixxx crate with the given name containing the given songs.
    Returns a dict describing what happened, including any errors.

    We NEVER write to this database while Mixxx is running -- SQLite doesn't
    handle two different programs writing to the same file well, and Mixxx
    keeps its own connection open the whole time it's running. Writing at the
    same time risks corrupting your entire Mixxx library, so we check for the
    running process first and refuse if it's open. The automatic backup below
    is a safety net for failures mid-write, not a substitute for this check.
    """
    if is_mixxx_running():
        return {'ok': False, 'error': "Mixxx is currently running. Close Mixxx before syncing a crate."}

    db_path = find_mixxx_db()
    if not db_path:
        return {'ok': False, 'error': "Could not find Mixxx's database. Open Mixxx at least once first."}
    if not _mixxx_db_accessible(db_path):
        return {'ok': False, 'error': _mixxx_error_hint(OSError('unable to open database file'))}

    backup_path = db_path + '.backup-' + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(db_path, backup_path)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        track_ids = []
        not_found = []
        for filepath in song_filepaths:
            row = cur.execute(
                "SELECT library.id FROM library "
                "JOIN track_locations ON library.location = track_locations.id "
                "WHERE track_locations.location = ?",
                (filepath,)
            ).fetchone()
            if row:
                track_ids.append(row['id'])
            else:
                not_found.append(filepath)

        existing = cur.execute('SELECT id FROM crates WHERE name = ?', (crate_name,)).fetchone()
        if existing:
            crate_id = existing['id']
            cur.execute('DELETE FROM crate_tracks WHERE crate_id = ?', (crate_id,))
        else:
            cur.execute(
                'INSERT INTO crates (name, count, show, locked, autodj_source) VALUES (?, 0, 1, 0, 0)',
                (crate_name,)
            )
            crate_id = cur.lastrowid

        for tid in track_ids:
            cur.execute(
                'INSERT OR IGNORE INTO crate_tracks (crate_id, track_id) VALUES (?, ?)',
                (crate_id, tid)
            )

        cur.execute('UPDATE crates SET count = ? WHERE id = ?', (len(track_ids), crate_id))
        conn.commit()
        conn.close()
        os.remove(backup_path)

        return {
            'ok': True,
            'synced': len(track_ids),
            'not_in_mixxx_library': len(not_found),
            'not_found_files': not_found[:10]
        }
    except Exception as e:
        conn.close()
        shutil.copy2(backup_path, db_path)
        return {'ok': False, 'error': 'Sync failed, restored backup: ' + _mixxx_error_hint(e) + str(e)}

def _mixxx_error_hint(e):
    """Actionable message when macOS sandbox protection blocks access to
    Mixxx's database (its container is protected like Desktop/Documents)."""
    s = str(e)
    if any(k in s for k in ('unable to open database file', 'authorization denied',
                            'not permitted', 'Operation not permitted')):
        return ("macOS is blocking access to Mixxx's database (its app sandbox). "
                "Grant Full Disk Access to the Python that runs this app: "
                "System Settings → Privacy & Security → Full Disk Access → "
                "add /opt/homebrew/bin/python3.11, then retry. ")
    return ''

def _mixxx_db_accessible(db_path):
    """Pre-flight check that Mixxx's DB can actually be opened, so the sandbox
    denial surfaces as a clear message instead of a raw sqlite error."""
    try:
        c = sqlite3.connect(db_path, timeout=2)
        c.execute('SELECT 1').fetchone()
        c.close()
        return True
    except Exception:
        return False

# -- TRANSITIONS → MIXXX COMMENTS ---------------------------------------------
# Mixxx stores a free-text `comment` per track in its library table. We write
# each song's outgoing transitions there so the mix notes are visible inside
# Mixxx. The generated text lives inside [DJ Library] ... [/DJ Library]
# markers: a re-sync replaces only that section and leaves any other text the
# user wrote in the comment untouched, and tracks whose transitions were all
# deleted get the section removed. Same safety rules as crate sync: refuse
# while Mixxx is open, back up the DB before writing, restore on failure.
TRANSITION_COMMENT_OPEN  = '[DJ Library]'
TRANSITION_COMMENT_CLOSE = '[/DJ Library]'
# Tags ride in the same block: one labelled line above the transition notes, so
# a single comment section keeps Mixxx, the rekordbox.xml Comments field and the
# file itself in step with one writer.
TAG_COMMENT_PREFIX = 'Tags:'

# Serializes Mixxx-DB writes so the auto-sync (fired from every transition
# add/edit/delete) and the manual button can never race each other.
_MIXXX_SYNC_LOCK = threading.Lock()

def _mashup_input_name(t):
    """Resolved input-ingredient name for a mashup row: 'Artist - Title' when
    the input is a library song (mi_title/mi_artist joined by the query), the
    typed text otherwise."""
    if t.get('mashup_input_song_id'):
        n = ' - '.join(x for x in (t.get('mi_title'), t.get('mi_artist')) if x)
        return n or '?'
    return t.get('mashup_input_text') or '?'

def build_library_comment(transitions, tags=None):
    """One song's [DJ Library] comment section: a Tags line followed by its
    outgoing transitions. Tags live here rather than in the genre field, so
    Mixxx shows them in its comment column while the Genre column keeps a real
    genre. Mashup rows render as "→ ⇄ Mashup (in: X) → Target"."""
    lines = [TRANSITION_COMMENT_OPEN]
    if tags:
        lines.append(f"{TAG_COMMENT_PREFIX} {', '.join(tags)}")
    for t in transitions:
        if t.get('to_song_id'):
            name = ' - '.join(x for x in (t.get('to_title'), t.get('to_artist')) if x) \
                   or t.get('to_filename') or '?'
        else:
            name = t.get('to_text') or 'Untitled track'
        if t.get('is_mashup_track'):
            name = f"⇄ Mashup (in: {_mashup_input_name(t)}) → {name}"
        suffix = ''
        if t.get('tag') and t.get('notes'):
            suffix = f" ({t['tag']}): {t['notes']}"
        elif t.get('tag'):
            suffix = f" ({t['tag']})"
        elif t.get('notes'):
            suffix = f": {t['notes']}"
        lines.append(f"→ {name}{suffix}")
    lines.append(TRANSITION_COMMENT_CLOSE)
    return '\n'.join(lines)

def merge_transition_comment(existing, new_section):
    """Puts our [DJ Library] section into a Mixxx comment while preserving any
    other text the user wrote: replaces the old section in place, or appends it
    when the comment has no section yet."""
    existing = existing or ''
    if TRANSITION_COMMENT_OPEN in existing:
        existing = re.sub(re.escape(TRANSITION_COMMENT_OPEN) + '.*?' + re.escape(TRANSITION_COMMENT_CLOSE),
                          new_section, existing, flags=re.DOTALL).strip()
    elif existing.strip():
        existing = existing.rstrip() + '\n' + new_section
    else:
        existing = new_section
    return existing

def sync_transitions_to_mixxx():
    """Writes each song's outgoing transition notes into its Mixxx comment.
    Returns a summary dict; never writes while Mixxx is running."""
    if is_mixxx_running():
        return {'ok': False, 'error': "Mixxx is currently running. Close Mixxx before syncing transitions."}
    db_path = find_mixxx_db()
    if not db_path:
        return {'ok': False, 'error': "Could not find Mixxx's database. Open Mixxx at least once first."}
    if not _mixxx_db_accessible(db_path):
        return {'ok': False, 'error': _mixxx_error_hint(OSError('unable to open database file'))}

    backup_path = db_path + '.backup-' + datetime.now().strftime("%Y%m%d-%H%M%S")
    conn = None
    try:
        shutil.copy2(db_path, backup_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cols = [r[1] for r in cur.execute('PRAGMA table_info(library)')]
        if 'comment' not in cols:
            conn.close(); conn = None
            os.remove(backup_path)
            return {'ok': False, 'error': "This Mixxx database has no comment column."}

        # Gather our songs' outgoing transitions (with target names) by song.
        # Transitions marked 'to_try' are HELD BACK: untested mixes never reach
        # Mixxx until they are confirmed. A song whose transitions are all
        # to_try is treated as having none here, so the cleanup pass below
        # strips any stale section from its Mixxx comment.
        with get_db() as db:
            rows = db.execute('''SELECT t.from_song_id, t.to_song_id, t.to_text, t.notes, t.tag,
                    t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                    s.filepath,
                    ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename,
                    tis.title AS mi_title, tis.artist AS mi_artist
                FROM transitions t
                JOIN songs s ON s.id = t.from_song_id
                LEFT JOIN songs ts ON ts.id = t.to_song_id
                LEFT JOIN songs tis ON tis.id = t.mashup_input_song_id
                WHERE t.status = 'confirmed'
                ORDER BY t.from_song_id, t.id''').fetchall()
            tagged = db.execute('''SELECT DISTINCT s.id, s.filepath FROM song_tags st
                JOIN songs s ON s.id = st.song_id''').fetchall()
        by_song = {}
        for r in rows:
            by_song.setdefault(r['from_song_id'], []).append(dict(r))
        # A song with tags but no transitions still needs its comment section:
        # that is where its tags live.
        tagged_paths = {r['id']: r['filepath'] for r in tagged}
        tags_by_song = tags_for_songs(list(tagged_paths))

        synced, not_found = 0, []
        touched = set()
        for song_id in list(by_song) + [i for i in tagged_paths if i not in by_song]:
            ts = by_song.get(song_id, [])
            filepath = ts[0]['filepath'] if ts else tagged_paths[song_id]
            mrow = cur.execute(
                "SELECT library.id FROM library "
                "JOIN track_locations ON library.location = track_locations.id "
                "WHERE track_locations.location = ?",
                (filepath,)).fetchone()
            if not mrow:
                not_found.append(filepath)
                continue
            mid = mrow['id']
            touched.add(mid)
            old = cur.execute('SELECT comment FROM library WHERE id=?', (mid,)).fetchone()
            new_comment = merge_transition_comment(
                old['comment'] if old else None,
                build_library_comment(ts, tags_by_song.get(song_id)))
            cur.execute('UPDATE library SET comment=? WHERE id=?', (new_comment, mid))
            synced += 1

        # Remove our section from Mixxx tracks that no longer have transitions.
        cleared = 0
        for row in cur.execute(
                "SELECT id, comment FROM library WHERE comment LIKE ?",
                ('%' + TRANSITION_COMMENT_OPEN + '%',)).fetchall():
            if row['id'] in touched: continue
            c = re.sub(re.escape(TRANSITION_COMMENT_OPEN) + '.*?' + re.escape(TRANSITION_COMMENT_CLOSE),
                       '', row['comment'] or '', flags=re.DOTALL).strip()
            cur.execute('UPDATE library SET comment=? WHERE id=?', (c or None, row['id']))
            cleared += 1

        conn.commit()
        conn.close(); conn = None
        os.remove(backup_path)
        return {'ok': True, 'synced': synced, 'cleared': cleared,
                'not_in_mixxx_library': len(not_found), 'not_found_files': not_found[:10]}
    except Exception as e:
        if conn is not None:
            try: conn.close()
            except Exception: pass
        if os.path.exists(backup_path):
            try: shutil.copy2(backup_path, db_path)
            except Exception: pass
        return {'ok': False, 'error': 'Sync failed, restored backup: ' + _mixxx_error_hint(e) + str(e)}

# -- NEW SONGS → MIXXX LIBRARY -----------------------------------------------
# When a song is added to DJ Library (Finder drop, download, folder import, or
# startup scan), we push it into Mixxx's library too so it shows up there
# without waiting for Mixxx's own rescan. Matching is by filepath: if the track
# already exists in Mixxx we leave it alone (Mixxx may already know it, and we
# never want to create duplicate rows). Same safety rules as every other Mixxx
# write: refuse while Mixxx is open, back up the DB before writing, restore it
# on any failure.

def sync_new_songs_to_mixxx(filepaths):
    """Inserts DJ Library songs that Mixxx doesn't know yet into Mixxx's
    library, matched by filepath. Takes a list so the startup scan can batch
    many files under a single backup + transaction. Returns a summary dict."""
    if is_mixxx_running():
        return {'ok': False, 'error': "Mixxx is currently running. Close Mixxx before syncing."}
    db_path = find_mixxx_db()
    if not db_path:
        return {'ok': False, 'error': "Could not find Mixxx's database. Open Mixxx at least once first."}
    if not _mixxx_db_accessible(db_path):
        return {'ok': False, 'error': _mixxx_error_hint(OSError('unable to open database file'))}

    with get_db() as db:
        songs = []
        for fp in filepaths:
            row = db.execute('SELECT * FROM songs WHERE filepath=?', (fp,)).fetchone()
            if row:
                songs.append(row)
    if not songs:
        return {'ok': True, 'synced': 0, 'already_in_mixxx': 0}

    backup_path = db_path + '.backup-' + datetime.now().strftime("%Y%m%d-%H%M%S")
    conn = None
    try:
        shutil.copy2(db_path, backup_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cols = [r[1] for r in cur.execute('PRAGMA table_info(library)')]

        already, synced = 0, 0
        for song in songs:
            fp = song['filepath']
            existing = cur.execute(
                "SELECT library.id FROM library "
                "JOIN track_locations ON library.location = track_locations.id "
                "WHERE track_locations.location = ?", (fp,)).fetchone()
            if existing:
                already += 1
                continue
            fname = Path(fp).name
            cur.execute(
                "INSERT INTO track_locations "
                "(location, filename, directory, filesize, fs_deleted, needs_verification) "
                "VALUES (?,?,?,?,0,1)",
                (fp, fname, str(Path(fp).parent),
                 os.path.getsize(fp) if os.path.exists(fp) else 0))
            loc_id = cur.lastrowid
            fields = {
                'artist': song['artist'], 'title': song['title'],
                'album': song['album'], 'year': song['year'],
                'genre': song['genre'], 'location': loc_id,
                'duration': song['duration_seconds'],
                'filetype': Path(fp).suffix.lower().lstrip('.'),
                'datetime_added': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mixxx_deleted': 0, 'header_parsed': 1,
            }
            fields = {k: v for k, v in fields.items() if k in cols}
            cur.execute(
                'INSERT INTO library (%s) VALUES (%s)' % (
                    ','.join(fields), ','.join('?' * len(fields))),
                list(fields.values()))
            synced += 1

        conn.commit()
        conn.close(); conn = None
        os.remove(backup_path)
        return {'ok': True, 'synced': synced, 'already_in_mixxx': already}
    except Exception as e:
        if conn is not None:
            try: conn.close()
            except Exception: pass
        if os.path.exists(backup_path):
            try: shutil.copy2(backup_path, db_path)
            except Exception: pass
        return {'ok': False, 'error': 'Sync failed, restored backup: ' + _mixxx_error_hint(e) + str(e)}

def auto_sync_new_songs(filepaths):
    """Background auto-sync of newly-added songs into Mixxx, fired from the
    folder watcher and the library scan. Failures are logged, never surfaced
    -- Mixxx being open or unreachable just logs and skips. Disabled when the
    Settings toggle is off."""
    if not filepaths:
        return
    if not AUTO_SYNC_MIXXX:
        print("Auto Mixxx song sync: disabled in Settings", flush=True)
        return
    def work():
        try:
            with _MIXXX_SYNC_LOCK:
                result = sync_new_songs_to_mixxx(filepaths)
            if result.get('ok'):
                print(f"Auto Mixxx song sync: {result.get('synced')} added, "
                      f"{result.get('already_in_mixxx')} already in Mixxx", flush=True)
            else:
                print(f"Auto Mixxx song sync skipped: {result.get('error')}", flush=True)
        except Exception as e:
            print(f"Auto Mixxx song sync failed: {e}", flush=True)
    threading.Thread(target=work, daemon=True).start()


def outgoing_transitions_for(song_id):
    """One song's outgoing transitions with target names resolved, in the same
    order the Mixxx comment and ID3 tag render them. Transitions marked
    'to_try' are held back -- untested mixes stay out of Mixxx comments,
    file ID3 tags and the rekordbox export until they are confirmed."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.to_song_id, t.to_text, t.notes, t.tag,
                t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename,
                tis.title AS mi_title, tis.artist AS mi_artist
            FROM transitions t
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs tis ON tis.id = t.mashup_input_song_id
            WHERE t.from_song_id = ? AND t.status = 'confirmed'
            ORDER BY t.created_at, t.id''', (song_id,)).fetchall()
    return [dict(r) for r in rows]


def write_library_comment(filepath, transitions, tags=None):
    """Ensures the file's ID3 comment contains exactly the [DJ Library] section
    built from `transitions` and `tags` -- neither leaves the section removed.
    The section lives in a dedicated COMM frame (desc='DJ Library') so it never
    touches the user's ordinary comment. Returns True if the file was rewritten,
    False if it was already up to date (or the write failed)."""
    section = build_library_comment(transitions, tags) if (transitions or tags) else None
    try:
        try: tags = ID3(filepath)
        except ID3NoHeaderError: tags = ID3()
        had_frame, existing_text = False, None
        for key in list(tags.keys()):
            f = tags[key]
            if isinstance(f, COMM) and f.desc == 'DJ Library':
                had_frame = True
                existing_text = str(f).strip()
                del tags[key]
        want = section.strip() if section else ''
        if had_frame and existing_text == want:
            return False
        if not had_frame and not want:
            return False
        if want:
            tags.add(COMM(encoding=3, lang='eng', desc='DJ Library', text=section))
        tags.save(filepath, v2_version=3)
        return True
    except Exception as e:
        print(f"Transition tag write failed {filepath}: {e}")
        return False


def sync_song_comment(song_id):
    """Rebuilds one song's file comment section from the DB -- its outgoing
    transitions and its tags -- removing the section when it has neither."""
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
    if not row:
        return False
    return write_library_comment(row['filepath'], outgoing_transitions_for(song_id),
                                 get_song_tags(song_id))


def sync_song_transition_tags(song_id):
    """Called from every transition change. The same comment section also
    carries the song's tags, so one writer keeps both in step."""
    return sync_song_comment(song_id)


def backfill_transition_tags():
    """Startup pass: makes sure every song's file comment matches the DB
    (writes missing sections, removes stale ones). Covers tagged songs as well
    as transition sources, since both live in that section. Idempotent -- files
    that already match are left untouched. Runs in the background."""
    try:
        with get_db() as db:
            rows = db.execute('''SELECT DISTINCT s.id, s.filepath FROM transitions t
                JOIN songs s ON s.id = t.from_song_id
                UNION
                SELECT DISTINCT s.id, s.filepath FROM song_tags st
                JOIN songs s ON s.id = st.song_id''').fetchall()
        written = 0
        for r in rows:
            if write_library_comment(r['filepath'], outgoing_transitions_for(r['id']),
                                     get_song_tags(r['id'])):
                written += 1
        print(f"Comment backfill: {written} file(s) updated")
    except Exception as e:
        print(f"Comment backfill failed: {e}")


def sync_transitions_after_change(song_id):
    """Fired from every transition add/edit/delete. In the background it (1)
    updates the source song's ID3 comment tag and (2) re-syncs Mixxx comments
    so the app and Mixxx both stay in step with the DB. Failures are logged,
    never surfaced -- Mixxx being open or unreachable just logs and skips."""
    def work():
        # Transition notes ride in the rekordbox.xml Comments field -- refresh
        # the on-disk export so DJUCED / Rekordbox / Serato get the new text.
        queue_auto_export()
        try:
            sync_song_transition_tags(song_id)
        except Exception as e:
            print(f"Transition tag sync failed: {e}", flush=True)
        if not AUTO_SYNC_MIXXX:
            return  # Mixxx syncing is turned off in Settings
        try:
            with _MIXXX_SYNC_LOCK:
                result = sync_transitions_to_mixxx()
            if result.get('ok'):
                print(f"Auto Mixxx sync: {result.get('synced')} synced, "
                      f"{result.get('cleared')} cleared, "
                      f"{result.get('not_in_mixxx_library')} not in Mixxx", flush=True)
            else:
                print(f"Auto Mixxx sync skipped: {result.get('error')}", flush=True)
        except Exception as e:
            print(f"Auto Mixxx sync failed: {e}", flush=True)
    threading.Thread(target=work, daemon=True).start()

# -- API -----------------------------------------------------------------

@app.route('/api/config')
def api_config():
    """Exposes the configured music folder, auto-export path and DJ-software
    choice so the frontend never hardcodes them."""
    return jsonify(_config_dict())


@app.route('/api/config', methods=['POST'])
def update_config():
    """Runtime settings saved from the Settings tab: the chosen DJ software,
    the rekordbox.xml export path, and the Mixxx auto-sync toggle. Writing
    straight into config.json means every change survives restarts."""
    data = request.json or {}
    updates = {}
    if 'djs_software' in data:
        sw = str(data['djs_software']).strip()
        # '' = not chosen yet (first-run state, Mixxx-style buttons shown)
        if sw not in ('', 'mixxx', 'rekordbox', 'serato', 'djuced', 'engine', 'none'):
            return jsonify({'error': 'unknown software'}), 400
        updates['djs_software'] = sw
    if 'rekordbox_export_path' in data:
        p = str(data['rekordbox_export_path']).strip()
        if not p:
            return jsonify({'error': 'path required'}), 400
        # A folder picked in the browse dialog (no .xml suffix) becomes
        # <folder>/rekordbox.xml -- the filename Bridge / Serato / DJUCED expect.
        if not p.lower().endswith('.xml'):
            p = os.path.join(p.rstrip('/'), 'rekordbox.xml')
        updates['rekordbox_export_path'] = p
    if 'auto_sync_mixxx' in data:
        updates['auto_sync_mixxx'] = bool(data['auto_sync_mixxx'])
    if 'key_display' in data:
        kd = str(data['key_display']).strip()
        if kd not in ('camelot', 'notation'):
            return jsonify({'error': 'unknown key_display'}), 400
        updates['key_display'] = kd
    if 'auto_install_python' in data:
        updates['auto_install_python'] = bool(data['auto_install_python'])
    if 'auto_cleanup_metadata' in data:
        updates['auto_cleanup_metadata'] = bool(data['auto_cleanup_metadata'])
    if 'auto_spotify_genre' in data:
        updates['auto_spotify_genre'] = bool(data['auto_spotify_genre'])
    if 'auto_genre_backfill' in data:
        updates['auto_genre_backfill'] = bool(data['auto_genre_backfill'])
    if 'library_name' in data:
        # Display name for the header/tab; empty string falls back to the
        # default "DJ / LIB" branding. Clamped so the header can't overflow.
        updates['library_name'] = str(data['library_name']).strip()[:60]
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400
    _save_config(updates)
    if 'rekordbox_export_path' in updates:
        # Regenerate the file at the new location immediately.
        queue_auto_export()
    return jsonify(_config_dict())

@app.route('/api/songs')
def get_songs():
    q = request.args.get('q','')
    with get_db() as db:
        if q:
            rows = db.execute('''SELECT * FROM songs
                WHERE title LIKE ? OR artist LIKE ? OR filename LIKE ? OR genre LIKE ?
                ORDER BY artist,title''', (f'%{q}%',)*4).fetchall()
        else:
            rows = db.execute('SELECT * FROM songs ORDER BY artist,title').fetchall()
    return jsonify(_with_tags([dict(r) for r in rows]))

# Browsers can't play every extension DJ software uses; map the ones that work
# in an <audio> element and fall back to Python's guess for the rest.
_AUDIO_MIME = {
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.mp4': 'audio/mp4',
    '.aac': 'audio/aac', '.flac': 'audio/flac', '.wav': 'audio/wav',
    '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg',
    '.webm': 'audio/webm',
}

@app.route('/api/songs/<int:song_id>/audio')
def stream_song_audio(song_id):
    """Streams a song's file for in-browser preview. conditional=True lets the
    <audio> element issue HTTP range requests, so seeking works."""
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    filepath = row['filepath']
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'file missing'}), 404
    ext = os.path.splitext(filepath)[1].lower()
    mimetype = _AUDIO_MIME.get(ext) or mimetypes.guess_type(filepath)[0] or 'application/octet-stream'
    return send_file(filepath, mimetype=mimetype, conditional=True)

# -- METADATA CLEANUP ------------------------------------------------------------
# Downloads arrive with YouTube-scrape tags (missing artists, "... (Official
# Audio)" titles, empty genres). These endpoints match each track against
# Spotify's catalogue so the UI can propose proper tags — nothing is written
# until the user reviews the diff and confirms.

def _norm_text(s):
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower())

def _spotify_search_track(title, artist, duration_s=None):
    """Best Spotify track for a title/artist pair, scored by name similarity
    and duration proximity. Tries 'title artist' first, then title alone.
    Returns a proposal dict or None."""
    token = get_spotify_token()
    if not token:
        return None
    headers = {'Authorization': f'Bearer {token}'}
    candidates = []
    for q in (f'{title} {artist}'.strip(), (title or '').strip()):
        if not q:
            continue
        resp = requests.get('https://api.spotify.com/v1/search',
            params={'q': q, 'type': 'track', 'limit': 10}, headers=headers)
        if resp.status_code == 200:
            candidates = resp.json().get('tracks', {}).get('items', [])
            if candidates:
                break
    if not candidates:
        return None
    t_norm = _norm_text(title)
    best, best_score = None, -1
    for tr in candidates:
        score = 0
        if _norm_text(tr.get('name')) == t_norm:
            score += 2
        elif t_norm and t_norm in _norm_text(tr.get('name')):
            score += 1
        if duration_s:
            delta = abs(tr.get('duration_ms', 0) / 1000.0 - duration_s)
            if delta <= 3:   score += 2
            elif delta <= 8: score += 1
        if score > best_score:
            best, best_score = tr, score
    tr = best
    dur_delta = abs(tr.get('duration_ms', 0) / 1000.0 - duration_s) if duration_s else 99
    confidence = 'high' if best_score >= 3 else 'medium' if best_score >= 1 else 'low'
    return {
        'spotify_id': tr.get('id'),
        'artist_id': (tr.get('artists') or [{}])[0].get('id'),
        'title': tr.get('name'),
        'artist': ', '.join(a['name'] for a in tr.get('artists', [])),
        'album': tr.get('album', {}).get('name'),
        'year': str(tr.get('album', {}).get('release_date', ''))[:4],
        'confidence': confidence,
        'duration_delta': round(dur_delta, 1) if duration_s else None,
    }

# -- GENRE SOURCES ---------------------------------------------------------------
# Spotify's genres are artist-level, broad ("edm", "dance pop") and frequently
# empty for underground or remix releases, so they are the LAST resort here.
# The two databases that know a track are BOTH queried -- Discogs release
# *styles* (the vocabulary DJs actually filter on, attached per release) and
# MusicBrainz artist genres (free, no key) -- and their answers are scored
# together (see _combine_genres). Agreeing sources share the credit; when they
# disagree the proposal DJs are most likely to filter on wins, rather than
# whichever source happened to answer first. Spotify's broad artist genres are
# the last resort. A source that errors is skipped, so one flaky API never
# blocks the fill.
#
# Discogs needs no key either -- unauthenticated lookups are limited to 25
# requests/minute, and the optional free personal access token (config
# "discogs_token", read into DISCOGS_TOKEN above) raises that to 60.
MB_USER_AGENT = 'dj-library/1.0 (self-hosted DJ library; +https://musicbrainz.org)'
# Discogs allows 25 requests/minute without a token and 60 with one. The bulk
# "fill missing genres" action deliberately paces its lookups under that limit:
# a 429 halfway through would silently fall back to a weaker source, giving the
# same track a different genre depending on when it was scanned. Add a free
# "discogs_token" to config.json to roughly halve a whole-library fill.
DISCOGS_MIN_INTERVAL = 60.0 / (60.0 if DISCOGS_TOKEN else 25.0)
_discogs_lock = threading.Lock()
_discogs_last_call = [0.0]

# Words that name a genre DJ software users filter on. Matched on whole words,
# so 'electro' does not match the umbrella term 'Electronic'.
_DJ_GENRE_WORDS = ('house', 'techno', 'trance', 'disco', 'drum and bass', 'dnb',
                   'dubstep', 'funk', 'downtempo', 'breakbeat', 'jungle',
                   'garage', 'hardstyle', 'psytrance', 'amapiano', 'afro',
                   'electro', 'rave', 'minimal', 'acid', 'bass')
# Catch-alls that are only worth proposing when nothing more specific came back,
# so ['edm', 'house'] reads as 'House' but ['edm'] alone still gives 'Edm'.
_CATCH_ALL_GENRES = ('edm',)
# Umbrella terms sources return for everything (Discogs labels every electronic
# release 'Electronic'). A result made only of these is not worth proposing, so
# it is skipped and the next source gets a turn.
_GENERIC_GENRES = {'electronic', 'electronica', 'dance', 'pop', 'music', 'rock',
                   'alternative', 'various'}


def _key_text(s):
    """Case/punctuation-insensitive text for matching titles and artists
    across sources. Unlike _norm_text it keeps word boundaries (a hyphen
    becomes a space), so 'girl$ - Remix' still lines up with 'girl$ (Remix)'."""
    spaced = re.sub(r'[^a-z0-9]+', ' ', (s or '').lower())
    return spaced.strip()


def _title_keys(title):
    """Candidate normalised forms of a track title for cross-source matching:
    the whole title, then the part before any '(...)' or ' - ' descriptor.
    Sources often list the base title only ('Levels' for 'Levels - Radio
    Edit'), so the shorter forms are tried only after the full title fails."""
    base = re.split(r'\s*[\(\[]', (title or ''))[0]
    keys = (_key_text(title), _key_text(base), _key_text(base.split(' - ')[0]))
    return [k for k in dict.fromkeys(keys) if k]


# Trailing words YouTube channels leave on artist tags ('... - Topic'), which
# must be stripped before the name can be matched against a music database.
_ARTIST_NOISE = ('topic', 'official', 'vevo', 'music', 'records', 'tv', 'hq',
                 'audio', 'channel', 'network')


def _artist_keys(artist):
    """Candidate act names from a tag like 'Anyma, Chris Avantgarde' or
    'Monolink feat. Zola', for verifying that a web match credits the act we
    searched for -- so a same-titled track by another artist can't supply the
    genre. Parentheticals and YouTube channel suffixes are discarded."""
    text = re.sub(r'\([^)]*\)', ' ', artist or '')
    keys = []
    for part in re.split(r'[,/&]|\bfeat\.?\b|\bft\.?\b|\bwith\b|\sx\s',
                         text, flags=re.IGNORECASE):
        key = _key_text(part)
        if len(key) <= 2:
            continue
        keys.append(key)
        words = key.split()
        while words and words[-1] in _ARTIST_NOISE:
            words.pop()
        if len(words) > 1:
            keys.append(' '.join(words))
    return list(dict.fromkeys(keys))


def _pick_dj_genre(genres):
    """The most DJ-recognisable genre from a source's list, e.g. 'Tech House'
    out of ['Electronic', 'Tech House']. Falls back to the first entry when
    nothing reads like a genre DJ software users filter on."""
    cleaned = []
    for g in genres or []:
        c = clean_genre(str(g).title()) if g else None
        if c and c not in cleaned:
            cleaned.append(c)
    if not cleaned:
        return None
    preferred = [g for g in cleaned
                 if any(re.search(rf'\b{re.escape(w)}\b', g.lower())
                        for w in _DJ_GENRE_WORDS)]
    if preferred:
        return preferred[0]
    catch_all = [g for g in cleaned
                 if any(re.search(rf'\b{re.escape(w)}\b', g.lower())
                        for w in _CATCH_ALL_GENRES)]
    if catch_all:
        return catch_all[0]
    informative = [g for g in cleaned if g.lower() not in _GENERIC_GENRES]
    return informative[0] if informative else None


def _genre_key(genre):
    """Text-insensitive form of a genre, so Discogs' 'Drum & Bass' and
    MusicBrainz' 'drum and bass' are recognised as the same answer."""
    key = re.sub(r'&', ' and ', (genre or '').lower())
    return re.sub(r'[^a-z0-9]+', ' ', key).strip()


def _genre_rank(genre):
    """Rough 'how likely is this a genre DJs filter on' score for a proposal.

    A whole-word hit on the DJ vocabulary (house, techno, trance, ...) is the
    strongest signal; a catch-all like 'Edm' counts against it. Specificity
    (word count) then separates names that both read as DJ genres, so
    'Progressive House' edges out the bare umbrella 'House'."""
    if not genre:
        return -1
    key = _genre_key(genre)
    rank = len(key.split())
    if any(re.search(rf'\b{re.escape(w)}\b', key) for w in _DJ_GENRE_WORDS):
        rank += 10
    if any(re.search(rf'\b{re.escape(w)}\b', key) for w in _CATCH_ALL_GENRES):
        rank -= 5
    return rank


def _combine_genres(*proposals):
    """The most likely DJ genre from per-source proposals.

    Each proposal is (source_name, genre); empty genres are dropped. Sources
    that agree are the strongest signal, so they share the credit
    ('Discogs + MusicBrainz'). On a disagreement the genre itself is scored --
    not the source order -- and the most DJ-recognisable / most specific name
    wins; a genuine tie keeps the earlier (more precise) source. Returns
    (genre, source_name) or (None, None)."""
    seen = [(name, g) for name, g in proposals if g]
    if not seen:
        return None, None
    if len(seen) == 1:
        return seen[0][1], seen[0][0]
    if len({_genre_key(g) for _, g in seen}) == 1:
        return seen[0][1], ' + '.join(name for name, _ in seen)
    # max() keeps the first element on a tie, i.e. the earlier/precise source.
    source, genre = max(seen, key=lambda p: _genre_rank(p[1]))
    return genre, source


def _spotify_artist_genre(artist_id):
    """A DJ-usable genre string from the artist's Spotify profile, e.g.
    'Melodic Techno'. Last-resort source -- see resolve_genre."""
    token = get_spotify_token()
    if not token or not artist_id:
        return None
    resp = requests.get(f'https://api.spotify.com/v1/artists/{artist_id}',
        headers={'Authorization': f'Bearer {token}'})
    if resp.status_code != 200:
        return None
    return _pick_dj_genre(resp.json().get('genres'))


def _discogs_request(params):
    """Paced GET against the Discogs search API (see DISCOGS_MIN_INTERVAL)."""
    headers = {'User-Agent': MB_USER_AGENT}
    if DISCOGS_TOKEN:
        headers['Authorization'] = f'Discogs token={DISCOGS_TOKEN}'
    with _discogs_lock:
        wait = DISCOGS_MIN_INTERVAL - (time.time() - _discogs_last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get('https://api.discogs.com/database/search',
                                params=params, headers=headers, timeout=10)
        finally:
            _discogs_last_call[0] = time.time()


def _discogs_genre(title, artist):
    """Genre from the Discogs release styles of the best-matching release.

    Discogs lists a release's styles in relevance order and only releases whose
    title contains this track's title are considered, so a search that drifts
    to another song contributes nothing. One request; no key required."""
    if not title:
        return None
    try:
        resp = _discogs_request({'q': f'{artist} {title}'.strip(),
                                 'type': 'release', 'per_page': 10})
    except requests.RequestException as e:
        _log(f"Discogs genre lookup failed for {title!r}: {e}")
        return None
    if resp.status_code != 200:
        _log(f"Discogs genre lookup HTTP {resp.status_code} for {title!r}")
        return None
    try:
        results = resp.json().get('results') or []
    except ValueError:
        return None
    # Discogs search titles read 'Artist - Title'. Prefer a release that names
    # BOTH the track and the act; only if none exists fall back to a title-only
    # match, and always try the fullest title first so 'Levels - Radio Edit'
    # prefers an actual 'Levels' release over whatever a looser match hits.
    artists = _artist_keys(artist)
    for require_artist in (True, False):
        for key in _title_keys(title):
            for r in results:
                discogs_title = _key_text(r.get('title'))
                if key not in discogs_title:
                    continue
                if require_artist and artists and not any(a in discogs_title for a in artists):
                    continue
                genre = _pick_dj_genre(r.get('style'))
                if genre:
                    return genre
    return None


# MusicBrainz asks all clients to identify themselves and to stay under one
# request per second; both are enforced here. Repeated probes showed it also
# drops connections temporarily when that is exceeded, so this is deliberately
# the fallback rather than the primary source.
_mb_lock = threading.Lock()
_mb_last_call = [0.0]


def _musicbrainz_get(path, params):
    """GET a MusicBrainz JSON endpoint, throttled to 1 req/sec with one retry
    for the transient connection drops it occasionally returns."""
    with _mb_lock:
        for attempt in range(2):
            wait = 1.05 - (time.time() - _mb_last_call[0])
            if wait > 0:
                time.sleep(wait)
            try:
                resp = requests.get('https://musicbrainz.org/ws/2/' + path,
                    params={**params, 'fmt': 'json'},
                    headers={'User-Agent': MB_USER_AGENT}, timeout=10)
                _mb_last_call[0] = time.time()
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code != 429:
                    _log(f"MusicBrainz {path} HTTP {resp.status_code}")
                    return None
            except (requests.RequestException, ValueError) as e:
                _mb_last_call[0] = time.time()
                _log(f"MusicBrainz {path} failed: {e}")
                if attempt:
                    return None
            time.sleep(1.5)
    return None


def _musicbrainz_genre(title, artist):
    """Genre from the artist behind the matching MusicBrainz recording. The
    match is verified on BOTH the track title and the credited act, so a
    same-named unrelated band can never supply the genre."""
    if not title:
        return None
    data = _musicbrainz_get('recording',
        {'query': f'{title} {artist}'.strip(), 'limit': 5})
    title_keys = _title_keys(title)
    artists = _artist_keys(artist)
    artist_id = None
    for rec in (data or {}).get('recordings') or []:
        rec_title = _key_text(rec.get('title'))
        if title_keys and not any(k in rec_title for k in title_keys):
            continue
        credit = rec.get('artist-credit') or []
        name = _key_text(credit[0]['artist']['name']) if credit else ''
        if artists and not any(a in name or name in a for a in artists):
            continue
        artist_id = credit[0]['artist']['id'] if credit else None
        break
    if not artist_id:
        return None
    data = _musicbrainz_get(f'artist/{artist_id}', {'inc': 'genres'})
    return _pick_dj_genre([g.get('name') for g in (data or {}).get('genres') or []])


def resolve_genre(title, artist, artist_id=None):
    """Genre proposal for one track, scored across the known sources.

    Discogs release styles and MusicBrainz genres are BOTH looked up, then
    combined (see _combine_genres): a genre both sources agree on wins, and on
    a disagreement the most DJ-recognisable / most specific name wins rather
    than whichever source answered first. Spotify's broad artist genres are
    only consulted when neither database has anything. Returns (genre,
    source_name) or (None, None)."""
    sources = (('Discogs', lambda: _discogs_genre(title, artist)),
               ('MusicBrainz', lambda: _musicbrainz_genre(title, artist)))
    proposals = []
    for name, lookup in sources:
        try:
            genre = lookup()
        except Exception as e:
            _log(f"Genre source {name} failed for {title!r}: {e}")
            continue
        if genre:
            proposals.append((name, genre))
    if not proposals:      # last resort: Spotify's artist-level genres
        try:
            genre = _spotify_artist_genre(artist_id)
        except Exception as e:
            _log(f"Genre source Spotify failed for {title!r}: {e}")
        else:
            if genre:
                proposals.append(('Spotify', genre))
    return _combine_genres(*proposals)

@app.route('/api/cleanup/match/<int:song_id>')
def cleanup_match(song_id):
    """Proposes proper tags for one track from Spotify, with a genre from the
    most precise available source (see resolve_genre). Read-only.
    Reuses the last stored proposal when it is still valid (track undecided,
    identifying tags unchanged) so reopening the cleanup panel doesn't
    re-query Spotify for matches it already made. Pass ?refresh=1 to force a
    fresh API lookup."""
    with get_db() as db:
        s = db.execute('SELECT * FROM songs WHERE id=?', (song_id,)).fetchone()
    if not s:
        return jsonify({'error': 'not found'}), 404
    title = s['title']
    artist = s['artist']
    if not title:
        # Fall back to the filename the way the scanner reads tags.
        name = clean_noise(Path(s['filepath']).stem)
        if ' - ' in name and not artist:
            artist, title = [p.strip() for p in name.split(' - ', 1)]
        else:
            title = name
    # Cache hit: an undecided track whose title/artist haven't changed since
    # the match was made reuses the stored proposal.
    if request.args.get('refresh') != '1' and not s['cleanup_scanned_at']:
        cached = None
        try:
            cached = json.loads(s['cleanup_proposal_json']) if s['cleanup_proposal_json'] else None
        except (ValueError, TypeError):
            cached = None
        if cached and cached.get('_src_title') == title \
                and cached.get('_src_artist') == artist:
            cached.pop('_src_title', None)
            cached.pop('_src_artist', None)
            return jsonify({'found': True, 'song_id': song_id, **cached})
    m = _spotify_search_track(title, artist, s['duration_seconds'])
    if not m:
        return jsonify({'found': False, 'song_id': song_id})
    # Genre comes from the most precise source available (Discogs / MusicBrainz
    # first, Spotify last) rather than the Spotify artist alone.
    m['genre'], m['genre_source'] = resolve_genre(
        m.get('title') or title, m.get('artist') or artist, m.pop('artist_id', None))
    m['song_id'] = song_id
    # Persist for reuse (the _src fields let the cache detect stale matches).
    with get_db() as db:
        db.execute('UPDATE songs SET cleanup_proposal_json=? WHERE id=?',
                   (json.dumps({**m, '_src_title': title, '_src_artist': artist}), song_id))
        db.commit()
    return jsonify({'found': True, **m})

CLEANUP_FIELDS = ('title', 'artist', 'album', 'year', 'genre')

def _with_db(fn):
    """Runs fn(db) with a fresh connection when none was supplied."""
    if fn is None:
        return None
    db = get_db()
    try:
        return fn(db)
    finally:
        db.close()

def _record_cleanup_decision(song_id, decision, db=None):
    """Marks one track as decided ('applied' / 'rejected' / 'no_match' /
    'skipped'). Pass db=None to use a fresh connection."""
    def run(conn):
        row = conn.execute('SELECT 1 FROM songs WHERE id=?', (song_id,)).fetchone()
        if not row:
            return False
        conn.execute('''UPDATE songs SET cleanup_scanned_at=CURRENT_TIMESTAMP,
            cleanup_decision=? WHERE id=?''', (decision, song_id))
        conn.commit()
        return True
    return run(db) if db is not None else _with_db(run)

def _write_cleanup_fix(song_id, updates, spotify_id=None, db=None):
    """Writes one track's cleaned-up tags to its ID3 file and DB row, and
    records the decision as 'applied' so future "only never-scanned" cleanups
    skip this track and manual tag edits are never silently overwritten.
    Shared by the review apply endpoint and the automatic cleanup on download.
    Returns True if the song existed and was updated."""
    # A '#tag' in a proposed genre is a tag, not a genre.
    extra_tags = []
    if updates and 'genre' in updates:
        genre, extra_tags = split_genre_tags(updates['genre'])
        updates = {**updates, 'genre': genre}
    def run(conn):
        row = conn.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
        if not row:
            return False
        filepath = row['filepath']
        if updates:
            sets = ','.join(f'{k}=?' for k in updates)
            conn.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(), song_id))
        if spotify_id:
            conn.execute('UPDATE songs SET spotify_id=? WHERE id=?', (spotify_id, song_id))
        conn.execute('''UPDATE songs SET cleanup_scanned_at=CURRENT_TIMESTAMP,
            cleanup_decision='applied' WHERE id=?''', (song_id,))
        conn.commit()
        return filepath
    filepath = run(db) if db is not None else _with_db(run)
    if not filepath:
        return False
    if updates:
        write_metadata(filepath, **updates)
    if updates and 'genre' in updates:
        write_file_genre(filepath, updates['genre'])
    if extra_tags:
        # Recognised as tags rather than dropped, so nothing typed is lost.
        add_song_tags(song_id, extra_tags)
        sync_song_comment(song_id)
    queue_auto_export()
    return True

@app.route('/api/cleanup/apply/<int:song_id>', methods=['POST'])
def cleanup_apply(song_id):
    """Writes reviewed tags to one track's ID3 file tags and DB row."""
    data = request.json or {}
    updates = {k: data[k] for k in CLEANUP_FIELDS if data.get(k)}
    if not _write_cleanup_fix(song_id, updates, data.get('spotify_id')):
        return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True, 'applied': list(updates)})

@app.route('/api/cleanup/apply-genre/<int:song_id>', methods=['POST'])
def cleanup_apply_genre(song_id):
    """Fills one track's missing genre from a reviewed Spotify proposal.

    Deliberately narrower than /api/cleanup/apply: it writes ONLY the genre,
    only when the track has none (an existing or user-entered genre is never
    replaced), and it records NO cleanup decision -- the track keeps its
    "never scanned" status, so a later full metadata cleanup of its
    title/artist still sees it. Nothing else about the file is touched."""
    data = request.json or {}
    # The genre field is genres only; a '#tag' typed into it is kept as a tag.
    genre, extra_tags = split_genre_tags((data.get('genre') or '').strip())
    if not genre:
        return jsonify({'error': 'A genre is required'}), 400
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    applied = _write_spotify_genre_if_missing(song_id, row['filepath'], genre)
    if extra_tags:
        add_song_tags(song_id, extra_tags)
        sync_song_comment(song_id)
    return jsonify({'ok': True, 'applied': applied, 'genre': genre,
                    'tags_extracted': extra_tags})

@app.route('/api/cleanup/decide/<int:song_id>', methods=['POST'])
def cleanup_decide(song_id):
    """Records the user's decision on one proposed fix: 'rejected' (accept my
    current tags) or 'applied' with a (possibly user-edited) field set --
    letting the user tweak the metadata before it is written. The song is
    marked decided either way so it never comes back in later scans."""
    data = request.json or {}
    decision = data.get('decision')
    if decision not in ('rejected', 'applied'):
        return jsonify({'error': "decision must be 'applied' or 'rejected'"}), 400
    if decision == 'rejected':
        if not _record_cleanup_decision(song_id, 'rejected'):
            return jsonify({'error': 'not found'}), 404
        return jsonify({'ok': True, 'decision': 'rejected'})
    updates = {k: data[k] for k in CLEANUP_FIELDS if data.get(k)}
    if not _write_cleanup_fix(song_id, updates, data.get('spotify_id')):
        return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True, 'decision': 'applied', 'applied': list(updates)})

@app.route('/api/cleanup/mark-scanned', methods=['POST'])
def cleanup_mark_scanned():
    """Records a batch cleanup decision for tracks that were scanned but not
    applied: 'no_match' (Spotify had nothing), 'rejected' (the user pressed
    Reject — keep my tags) or 'skipped' (saw the proposal, left it undecided).
    Lets later "only never-scanned" cleanups skip decided tracks; undecided
    ones come back."""
    data = request.json or {}
    decision = data.get('decision') if data.get('decision') in ('no_match', 'rejected', 'skipped') else 'no_match'
    ids = [i for i in data.get('ids', []) if isinstance(i, int)]
    if not ids:
        return jsonify({'ok': True, 'marked': 0})
    with get_db() as db:
        db.executemany(
            '''UPDATE songs SET cleanup_scanned_at=CURRENT_TIMESTAMP,
               cleanup_decision=? WHERE id=?''',
            [(decision, i) for i in ids])
        db.commit()
    return jsonify({'ok': True, 'marked': len(ids), 'decision': decision})

def schedule_auto_cleanup(song_id, cleanup_download=False):
    """Queue enabled Spotify metadata automation for a newly added track."""
    if AUTO_SPOTIFY_GENRE or (cleanup_download and AUTO_CLEANUP_METADATA):
        threading.Thread(target=auto_cleanup_download,
                         args=[song_id, cleanup_download], daemon=True).start()


def schedule_auto_cleanup_for_path(filepath):
    """Resolve a newly-added filepath to its song row and queue enabled automation."""
    if not AUTO_SPOTIFY_GENRE:
        return
    with get_db() as db:
        row = db.execute('SELECT id FROM songs WHERE filepath=?', (filepath,)).fetchone()
    if row:
        schedule_auto_cleanup(row['id'])


def _write_spotify_genre_if_missing(song_id, filepath, genre):
    """Fill an empty genre only; a concurrent or user-written genre wins."""
    genre = clean_genre(genre)
    if not genre:
        return False
    with get_db() as db:
        cursor = db.execute('''UPDATE songs SET genre=? WHERE id=?
            AND (genre IS NULL OR TRIM(genre)='')''', (genre, song_id))
        db.commit()
        applied = cursor.rowcount == 1
    if applied:
        write_metadata(filepath, genre=genre)
        queue_auto_export()
    return applied


# -- GENRE BACKFILL -----------------------------------------------------------
# "Fill every missing genre" as a resumable background job. The review panel
# proposes genres one track at a time; this is the unattended whole-library
# version. It is deliberately:
#   * paced -- every lookup goes through the same paced Discogs limiter as the
#     review tool (DISCOGS_MIN_INTERVAL), so it can never trip the rate limit
#     however large the library is;
#   * resumable -- every attempted track is stamped with genre_backfill_at, so
#     Cancel, quit, or a crash keeps the work done so far and the next run
#     continues with the tracks still outstanding (no duplicate lookups);
#   * conservative -- it writes a genre only where the track has none
#     (_write_spotify_genre_if_missing) and never touches any other tag.
#
# One row of the genre_backfill table holds the job's progress, so the UI can
# report it (and survive a restart) without any in-memory bookkeeping.
_backfill_lock = threading.Lock()
_backfill_cancel = threading.Event()
_backfill_thread = [None]


def _backfill_state():
    """The persisted genre-backfill progress row, or None."""
    with get_db() as db:
        return db.execute('SELECT * FROM genre_backfill WHERE id=1').fetchone()


def _backfill_update(**fields):
    """Updates the progress row (stamping updated_at) and commits. A field
    whose value is the literal 'CURRENT_TIMESTAMP' is written as SQL now(), so
    started_at can be a column assignment rather than a Python timestamp."""
    if not fields:
        return
    fields['updated_at'] = 'CURRENT_TIMESTAMP'
    assignments, values = [], []
    for key, value in fields.items():
        if value == 'CURRENT_TIMESTAMP':
            assignments.append(f'{key}=CURRENT_TIMESTAMP')
        else:
            assignments.append(f'{key}=?')
            values.append(value)
    with get_db() as db:
        db.execute(f'UPDATE genre_backfill SET {", ".join(assignments)} WHERE id=1', values)
        db.commit()


def _backfill_remaining():
    """Tracks still to look up: no genre and not yet attempted by a backfill."""
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM songs WHERE (genre IS NULL OR TRIM(genre)='') "
            "AND genre_backfill_at IS NULL").fetchone()[0]


def _backfill_next_song():
    """The next track to look up, oldest first, or None when there is none."""
    with get_db() as db:
        return db.execute(
            "SELECT id, filepath, title, artist FROM songs "
            "WHERE (genre IS NULL OR TRIM(genre)='') AND genre_backfill_at IS NULL "
            "ORDER BY id LIMIT 1").fetchone()


def _genre_backfill_worker():
    """Walks the outstanding tracks, writing a genre wherever one is found.

    Runs in a daemon thread. Cancellation is cooperative: the flag is checked
    between tracks, so at most the lookup already in flight finishes after
    Cancel. Every attempted track is stamped, so nothing is looked up twice.
    """
    state = _backfill_state()
    attempted = (state['attempted'] if state else 0) or 0
    filled = (state['filled'] if state else 0) or 0
    try:
        while not _backfill_cancel.is_set():
            song = _backfill_next_song()
            if not song:
                break
            title = song['title'] or clean_noise(Path(song['filepath']).stem)
            genre = source = None
            try:
                genre, source = resolve_genre(title, song['artist'])
            except Exception as e:      # one bad track must never kill the job
                _log(f"Genre backfill lookup failed for song {song['id']}: {e}")
            applied = False
            if genre:
                try:
                    applied = _write_spotify_genre_if_missing(song['id'], song['filepath'], genre)
                except Exception as e:
                    _log(f"Genre backfill write failed for song {song['id']}: {e}")
                if applied:
                    _log(f"Genre backfill song {song['id']}: {genre!r} from {source}")
            # Stamp the track whether or not a genre was found: it has been
            # looked up. Without this a genreless track would be retried forever.
            with get_db() as db:
                db.execute('UPDATE songs SET genre_backfill_at=CURRENT_TIMESTAMP WHERE id=?',
                           (song['id'],))
                db.commit()
            attempted += 1
            if applied:
                filled += 1
            _backfill_update(status='running', attempted=attempted, filled=filled,
                             current_id=song['id'], current_title=title)
    except Exception as e:
        _log(f"Genre backfill stopped unexpectedly: {e}")
        _backfill_update(status='error', error=str(e), current_id=None, current_title=None)
        _backfill_thread[0] = None
        return
    remaining = _backfill_remaining()
    if _backfill_cancel.is_set() and remaining:
        _backfill_update(status='cancelled', current_id=None, current_title=None)
        _log(f"Genre backfill cancelled with {remaining} track(s) left to look up")
    else:
        _backfill_update(status='done', attempted=attempted, filled=filled,
                         current_id=None, current_title=None)
        _log(f"Genre backfill finished: filled {filled} of {attempted} looked up")
    # Cleared last: a caller that sees no thread knows the final status is written.
    _backfill_thread[0] = None


def start_genre_backfill():
    """Starts or resumes the whole-library genre backfill; returns its status.

    A finished or fresh job starts from scratch; a cancelled or interrupted one
    continues with the tracks it has not attempted yet. Ignored (a no-op) when
    a worker is already running, so double-pressing the button is harmless.
    """
    with _backfill_lock:
        if _backfill_thread[0] and _backfill_thread[0].is_alive():
            return 'running'
        state = _backfill_state()
        status = (state['status'] if state else 'idle') or 'idle'
        remaining = _backfill_remaining()
        if not remaining:
            _backfill_update(status='done', current_id=None, current_title=None)
            return 'done'
        # A cancelled/interrupted job keeps its counters and picks up the rest.
        resume = status in ('running', 'cancelled')
        attempted = ((state['attempted'] if state else 0) or 0) if resume else 0
        filled = ((state['filled'] if state else 0) or 0) if resume else 0
        _backfill_cancel.clear()
        started = {} if resume else {'started_at': 'CURRENT_TIMESTAMP'}
        _backfill_update(status='running', attempted=attempted, filled=filled,
                         total=attempted + remaining, current_id=None,
                         current_title=None, error=None, **started)
        thread = threading.Thread(target=_genre_backfill_worker, daemon=True)
        _backfill_thread[0] = thread
        thread.start()
        return 'running'


def cancel_genre_backfill():
    """Asks a running backfill to stop after the lookup in flight. Attempted
    tracks stay stamped, so pressing Start again resumes from there."""
    _backfill_cancel.set()


def resume_genre_backfill():
    """Restarts a job the previous process left running, at startup, so a quit
    or restart continues the backfill instead of silently abandoning it."""
    state = _backfill_state()
    if state and state['status'] == 'running' and _backfill_remaining():
        _log('Resuming interrupted genre backfill')
        start_genre_backfill()


def maybe_auto_genre_backfill(reason='scan'):
    """Starts the whole-library genre backfill automatically when the
    "sweep genres after scans" setting is on (AUTO_GENRE_BACKFILL). Called after
    each library scan and when new files land, so tracks that arrive without a
    genre get filled without pressing Start. A no-op when the setting is off, a
    worker is already running, or no track is outstanding -- so it is safe to
    call on every scan."""
    if not AUTO_GENRE_BACKFILL:
        return
    thread = _backfill_thread[0]
    if thread and thread.is_alive():
        return
    remaining = _backfill_remaining()
    if not remaining:
        return
    _log(f"Auto-starting genre backfill after {reason}: {remaining} track(s) outstanding")
    start_genre_backfill()


@app.route('/api/genre-backfill')
def genre_backfill_status():
    """Progress of the resumable genre backfill, for the UI to poll."""
    state = _backfill_state()
    thread = _backfill_thread[0]
    running = bool(thread and thread.is_alive())
    status = (state['status'] if state else 'idle') or 'idle'
    if status == 'running' and not running:
        # A 'running' row with no live worker (the process restarted mid-job).
        status = 'idle'
    remaining = _backfill_remaining()
    return jsonify({
        'status': status,
        'running': running,
        'cancelling': running and _backfill_cancel.is_set(),
        'total': (state['total'] if state else 0) or 0,
        'attempted': (state['attempted'] if state else 0) or 0,
        'filled': (state['filled'] if state else 0) or 0,
        'remaining': remaining,
        'current_id': state['current_id'] if state else None,
        'current_title': state['current_title'] if state else None,
        'error': state['error'] if state else None,
        'discogs_token': bool(DISCOGS_TOKEN),
        'requests_per_minute': round(60.0 / DISCOGS_MIN_INTERVAL),
        # Lower bound: every track costs at least one paced Discogs lookup.
        'eta_seconds': int(remaining * DISCOGS_MIN_INTERVAL),
    })


@app.route('/api/genre-backfill/start', methods=['POST'])
def genre_backfill_start():
    """Starts or resumes the whole-library genre backfill."""
    return jsonify({'ok': True, 'status': start_genre_backfill()})


@app.route('/api/genre-backfill/cancel', methods=['POST'])
def genre_backfill_cancel():
    """Stops the running backfill after the lookup in flight (resumable)."""
    cancel_genre_backfill()
    return jsonify({'ok': True})


def auto_cleanup_download(song_id, cleanup_download=False):
    """Background metadata cleanup for a newly-added track. Matches it
    against Spotify and, when the match is HIGH confidence, writes the proper
    tags straight through (same path as the review tool, so the fix is recorded
    'applied' and never re-proposed). The independent genre-only setting fills
    blank genres on these downloads without replacing any other tags. Medium /
    low / no match are left for review rather than guessed."""
    do_cleanup = cleanup_download and AUTO_CLEANUP_METADATA
    if not do_cleanup and not AUTO_SPOTIFY_GENRE:
        return
    try:
        with get_db() as db:
            s = db.execute('SELECT * FROM songs WHERE id=?', (song_id,)).fetchone()
        if not s:
            return
        # A previous (manual) cleanup already decided this track -- don't
        # overwrite tags the user may have set by hand.
        if s['cleanup_scanned_at']:
            return
        if not do_cleanup and (not AUTO_SPOTIFY_GENRE or s['genre']):
            return
        title = s['title'] or clean_noise(Path(s['filepath']).stem)
        m = _spotify_search_track(title, s['artist'], s['duration_seconds'])
        if not m:
            if do_cleanup:
                _log(f"Auto cleanup song {song_id}: no Spotify match ({title!r}) -- left for review")
            return
        if m.get('confidence') != 'high':
            if do_cleanup:
                _log(f"Auto cleanup song {song_id}: {m['confidence']} confidence ({title!r}) -- left for review")
            return
        updates = {k: m[k] for k in ('title','artist','album','year') if do_cleanup and m.get(k)}
        genre = None
        if do_cleanup or (AUTO_SPOTIFY_GENRE and not s['genre']):
            genre, source = resolve_genre(
                m.get('title') or title, m.get('artist') or s['artist'],
                m.get('artist_id'))
            if genre:
                _log(f"Genre for song {song_id}: {genre!r} from {source}")
        if do_cleanup:
            if genre:
                updates['genre'] = genre
            if _write_cleanup_fix(song_id, updates, m.get('spotify_id')):
                _log(f"Auto cleanup song {song_id}: applied {list(updates)} (high confidence)")
        elif AUTO_SPOTIFY_GENRE and not s['genre']:
            if _write_spotify_genre_if_missing(song_id, s['filepath'], genre):
                _log(f"Spotify genre song {song_id}: applied {genre!r}")
    except Exception as e:
        _log(f"Auto cleanup failed for song {song_id}: {e}")

@app.route('/api/songs/<int:song_id>', methods=['PATCH'])
def update_song(song_id):
    data = request.json or {}
    allowed = ['title','artist','album','year','genre','bpm','musical_key']
    updates = {k:v for k,v in data.items() if k in allowed}
    wants_tags = 'tags' in data
    if not updates and not wants_tags: return jsonify({'error':'no valid fields'}),400
    # The genre field holds genres only: a '#tag' typed here is saved as a tag
    # instead (and the response names it, so nothing disappears silently).
    extracted = []
    if 'genre' in updates:
        updates['genre'], extracted = split_genre_tags(updates['genre'])
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?',(song_id,)).fetchone()
        if not row: return jsonify({'error':'not found'}),404
        filepath = row['filepath']
        if updates:
            sets = ','.join(f'{k}=?' for k in updates)
            db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(),song_id))
            db.commit()
    file_fields = {k:v for k,v in updates.items() if k != 'genre'}
    if file_fields:
        write_metadata(filepath,
            title=file_fields.get('title'), artist=file_fields.get('artist'),
            album=file_fields.get('album'), year=file_fields.get('year'),
            bpm=file_fields.get('bpm'))
    if 'genre' in updates:
        write_file_genre(filepath, updates['genre'])
    added, removed = [], []
    if wants_tags:
        added, removed = set_song_tags(song_id, _tags_from_payload(data.get('tags')))
    if extracted:
        added = added + add_song_tags(song_id, extracted)
    if added or removed:
        sync_song_comment(song_id)
        queue_auto_export()
    return jsonify({'ok':True, 'tags_added':added, 'tags_removed':removed,
                    'tags_extracted':extracted})

@app.route('/api/songs/batch', methods=['PATCH'])
def batch_update():
    """Edits the selection. Blank fields keep each song's own value; a genre
    stays a genre (a '#tag' typed in the genre field becomes a tag) and the
    Tags field replaces the selection's tags."""
    data    = request.json or {}
    ids     = data.get('ids',[])
    allowed = ['title','artist','album','year','genre','bpm','musical_key']
    updates = {k:v for k,v in data.items() if k in allowed and v not in (None,'')}
    wants_tags = 'tags' in data
    if not ids or (not updates and not wants_tags):
        return jsonify({'error':'missing ids or fields'}),400
    extracted = []
    if 'genre' in updates:
        updates['genre'], extracted = split_genre_tags(updates['genre'])
    with get_db() as db:
        for song_id in ids:
            row = db.execute('SELECT filepath FROM songs WHERE id=?',(song_id,)).fetchone()
            if not row: continue
            if updates:
                sets = ','.join(f'{k}=?' for k in updates)
                db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(),song_id))
            if 'genre' in updates:
                write_file_genre(row['filepath'], updates['genre'])
            file_fields = {k:v for k,v in updates.items() if k != 'genre'}
            if file_fields:
                write_metadata(row['filepath'],
                    title=file_fields.get('title'), artist=file_fields.get('artist'),
                    album=file_fields.get('album'), year=file_fields.get('year'),
                    bpm=file_fields.get('bpm'))
        db.commit()
    tagged = 0
    if wants_tags or extracted:
        wanted = _tags_from_payload(data.get('tags')) if wants_tags else []
        for song_id in ids:
            changed = []
            if wants_tags:
                added, _ = set_song_tags(song_id, wanted)
                changed += added
            if extracted:
                changed += add_song_tags(song_id, extracted)
            if changed:
                sync_song_comment(song_id)
                tagged += len(changed)
    if extracted or tagged:
        queue_auto_export()
    return jsonify({'ok':True,'updated':len(ids),'tags_added':tagged,
                    'tags_extracted':extracted})

@app.route('/api/songs/<int:song_id>', methods=['DELETE'])
def delete_song(song_id):
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?',(song_id,)).fetchone()
        if not row: return jsonify({'error':'not found'}),404
        filepath = row['filepath']
        if os.path.exists(filepath): os.remove(filepath)
        _delete_song_rows(db, [song_id])
        db.commit()
    queue_auto_export()
    return jsonify({'deleted':filepath})

@app.route('/api/folder/delete', methods=['POST'])
def delete_folder():
    """
    Deletes an entire subfolder of the library in one shot: every song in it is
    removed from disk and from the database (including crate memberships), then
    the now-empty folder itself is deleted. This is the way to get rid of an
    imported folder -- per-song deletion also works, but this clears everything
    at once, including songs whose files were already deleted outside the app
    (which otherwise linger as ghost entries).
    """
    folder = (request.json or {}).get('folder','').strip()
    if not folder or '/' in folder or folder in ('.','..'):
        return jsonify({'error':'Invalid folder name'}), 400
    folder_path = os.path.join(MUSIC_DIR, folder)
    # Never treat the library root itself as deletable (a path mixup could
    # otherwise wipe the whole music collection).
    if os.path.abspath(folder_path) == os.path.abspath(MUSIC_DIR).rstrip(os.sep):
        return jsonify({'error': 'Refusing to delete the library root'}), 400
    if _music_dir_job['active']:
        return jsonify({'error': 'A music-folder switch is running'}), 409
    prefix = folder_path.rstrip(os.sep) + os.sep
    with get_db() as db:
        rows = db.execute('SELECT id, filepath FROM songs').fetchall()
        targets = [r for r in rows if r['filepath'].startswith(prefix)]
        for r in targets:
            if os.path.exists(r['filepath']):
                try: os.remove(r['filepath'])
                except OSError: pass
        _delete_song_rows(db, [r['id'] for r in targets])
        db.commit()
    if targets:
        # Library changed -- refresh the on-disk rekordbox.xml.
        queue_auto_export()
    # Only succeeds once every file inside is gone -- leaves the folder alone
    # if the user added non-audio files to it that we shouldn't touch.
    try: os.rmdir(folder_path)
    except OSError: pass
    return jsonify({'deleted': len(targets)})


# -- FOLDER BROWSER / SEARCH --------------------------------------------------
# Lets the frontend offer a Finder-style picker for the Import Folder tool:
# browse the filesystem by directory, or search folder names via Spotlight.
# Both endpoints are read-only -- they never create or modify anything.

AUDIO_EXTS = ('.mp3', '.wav', '.flac', '.m4a')


def _natural_key(name):
    """Sort names the way Finder does: 'Folder 2' before 'Folder 10'."""
    import re as _re
    return [_re.sub(r'\d+', lambda m: m.group(0).zfill(8), part) for part in _re.split(r'(\d+)', name.lower())]


def _list_subfolders(path):
    """Directories directly inside `path`, Finder-style sorted, hidden skipped."""
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.name.startswith('.'):
                    continue
                if e.is_dir(follow_symlinks=False):
                    entries.append(e.name)
            except OSError:
                continue
    return sorted(entries, key=_natural_key)


@app.route('/api/folders/browse')
def browse_folder():
    """List the subfolders of a path. Returns the path, its parent, and the
    immediate child folders -- enough to render a navigable breadcrumb tree."""
    raw = (request.args.get('path') or '').strip()
    path = os.path.abspath(os.path.expanduser(raw or '~'))
    if not os.path.isdir(path):
        return jsonify({'error': 'Folder not found'}), 400
    try:
        os.listdir(path)  # permission probe -- raises OSError if not readable
    except OSError:
        return jsonify({'error': 'No permission to read that folder'}), 403

    subdirs = _list_subfolders(path)
    # Flag folders that directly contain audio, so the picker can hint where the music is.
    audio_hits = {}
    for name in subdirs:
        try:
            with os.scandir(os.path.join(path, name)) as it:
                audio_hits[name] = any(
                    e.is_file(follow_symlinks=False) and e.name.lower().endswith(AUDIO_EXTS)
                    for e in it
                )
        except OSError:
            audio_hits[name] = False

    parent = os.path.dirname(path)

    # Quick-jump roots, Finder sidebar style: home + common folders + external drives.
    home = os.path.expanduser('~')
    roots = [{'label': '⌂ Home', 'path': home},
             {'label': '🎵 Music', 'path': os.path.join(home, 'Music')},
             {'label': '⬇ Downloads', 'path': os.path.join(home, 'Downloads')},
             {'label': '🖥 Desktop', 'path': os.path.join(home, 'Desktop')}]
    roots = [r for r in roots if os.path.isdir(r['path'])]
    try:
        for v in sorted(os.listdir('/Volumes')):
            if not v.startswith('.'):
                roots.append({'label': '💾 ' + v, 'path': os.path.join('/Volumes', v)})
    except OSError:
        pass

    return jsonify({
        'path': path,
        'name': os.path.basename(path) or path,
        'parent': parent if parent != path else None,
        'roots': roots,
        'entries': [{'name': n, 'path': os.path.join(path, n), 'has_audio': bool(audio_hits.get(n))}
                    for n in subdirs]
    })


def _mdfind_folders(q, limit=50):
    """Search folder names through the Spotlight index (fast, Finder-like)."""
    safe_q = q.replace('"', '\\"')
    query = f'kMDItemContentType == "public.folder" && kMDItemFSName == "*{safe_q}*"cd'
    try:
        out = subprocess.run(['mdfind', query], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    results = []
    for line in out.splitlines():
        line = line.strip()
        if line and os.path.isdir(line):
            results.append(line)
        if len(results) >= limit:
            break
    return results


def _walk_folders(q, limit=50, max_depth=4):
    """Slow fallback for machines without Spotlight: walk home + /Volumes."""
    ql = q.lower()
    results = []
    for root in (os.path.expanduser('~'), '/Volumes'):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            # Don't descend into hidden/system dirs or the walk gets huge.
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            depth = dirpath[len(root):].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
            for d in list(dirnames):
                if ql in d.lower():
                    p = os.path.join(dirpath, d)
                    if p not in results:
                        results.append(p)
                    if len(results) >= limit:
                        return results
    return results


@app.route('/api/folders/search')
def search_folder():
    """Finder-style name search over folders. Uses Spotlight when available."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    results = _mdfind_folders(q)
    if results is None:
        results = _walk_folders(q)
    return jsonify({'results': results[:50], 'used': 'spotlight' if results is not None else 'walk'})


@app.route('/api/folders/peek')
def peek_folder():
    """Quick look inside a folder for the music-folder switcher: how many audio
    files it holds (top level + all subfolders) and whether it is empty. Lets
    the UI say 'this folder already has songs in it' before anything changes."""
    raw = (request.args.get('path') or '').strip()
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        return jsonify({'error': 'Folder not found'}), 400
    top, total = 0, 0
    for root, _, files in os.walk(path):
        n = sum(1 for f in files if f.lower().endswith(AUDIO_EXTS))
        total += n
        if root == path:
            top = n
    return jsonify({'path': path, 'audio_files': total, 'top_level': top,
                    'empty': total == 0})


@app.route('/api/folders/create', methods=['POST'])
def create_folder():
    """Creates a new folder (parents included) for the 'new library here' flow.
    Returns the created path so the UI can pass it straight to the switch."""
    raw = (request.json or {}).get('folder', '').strip()
    if not raw:
        return jsonify({'error': 'Folder path required'}), 400
    path = os.path.abspath(os.path.expanduser(raw))
    if path == '/':
        return jsonify({'error': 'Refusing to create /'}), 400
    if os.path.isdir(path):
        return jsonify({'path': path, 'existed': True})
    try:
        os.makedirs(path)
    except OSError as e:
        return jsonify({'error': f'Could not create folder: {e}'}), 400
    return jsonify({'path': path, 'existed': False})


# -- EXTERNAL FOLDER IMPORT ---------------------------------------------------
# Lets you point at any folder on your Mac (not just the main library folder)
# and pull its audio files into your library. Useful for songs you downloaded
# elsewhere, an old collection on an external drive, etc.
#
# Each file gets COPIED (not moved) into your main music folder so the
# original location is untouched -- if something goes wrong you still have
# your source files. Duplicate detection runs on each file exactly like the
# folder watcher does, so re-importing a folder you've already imported
# won't create copies.

def get_or_create_crate(name, db):
    """Finds a manual crate by name, or creates it if it doesn't exist yet."""
    row = db.execute('SELECT id FROM crates WHERE name = ?', (name,)).fetchone()
    if row:
        return row['id']
    db.execute('INSERT INTO crates (name, is_smart, rules) VALUES (?, 0, ?)', (name, '[]'))
    db.commit()
    return db.execute('SELECT last_insert_rowid()').fetchone()[0]

@app.route('/api/import-folder', methods=['POST'])
def import_folder():
    data = request.json
    source_folder = data.get('folder', '').strip()
    subfolder = data.get('subfolder')       # optional destination subfolder in your library
    crate_id = data.get('crate_id')         # optional: add ALL imported songs to this one crate
    auto_crate_by_subfolder = data.get('auto_crate_by_subfolder', False)
    auto_genre_by_subfolder = data.get('auto_genre_by_subfolder', False)
    force_duplicates = data.get('force_duplicates', False)
    # When auto_crate_by_subfolder is True, each song's immediate parent folder
    # name (relative to source_folder) becomes its crate name -- e.g. importing
    # ~/Downloads/NewMusic/House/track.mp3 creates/uses a crate called "House".
    # When auto_genre_by_subfolder is True, that same folder name is written
    # into the song's genre tag (TCON) instead of / as well as being used as
    # a crate -- these two options are independent, you can use either, both,
    # or neither. Files sitting directly in source_folder with no subfolder
    # get neither unless crate_id is set as an explicit fallback.
    # force_duplicates skips the duplicate check entirely -- useful when you
    # deliberately want a second copy, e.g. before running the merge tool.

    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({'error': 'Folder not found'}), 400

    audio_files = []
    for root, _, files in os.walk(source_folder):
        for fname in files:
            if Path(fname).suffix.lower() in ('.mp3', '.wav', '.flac', '.m4a'):
                audio_files.append(os.path.join(root, fname))

    if not audio_files:
        return jsonify({'error': 'No audio files found in that folder'}), 400

    job_id = hashlib.md5(source_folder.encode()).hexdigest()[:8]
    download_status[job_id] = {
        'status': 'importing',
        'total': len(audio_files),
        'done': 0,
        'skipped_duplicate': 0,
        'added_song_ids': [],
        'crates_used': []
    }

    def do_import():
        import shutil as _shutil
        dest_dir = os.path.join(MUSIC_DIR, subfolder) if subfolder else MUSIC_DIR
        os.makedirs(dest_dir, exist_ok=True)

        # Track song IDs per crate name so we only touch the DB once per crate
        crate_assignments = {}  # crate_name -> [song_id, ...]

        for src_path in audio_files:
            title, artist, album, year, genre, duration = extract_metadata(src_path)

            if not force_duplicates:
                dupes = find_duplicates_for_file(src_path, title, artist, duration)
                if dupes:
                    download_status[job_id]['skipped_duplicate'] += 1
                    download_status[job_id]['done'] += 1
                    continue

            dest_path = os.path.join(dest_dir, Path(src_path).name)
            counter = 1
            base_dest = dest_path
            while os.path.exists(dest_path):
                stem = Path(base_dest).stem
                ext = Path(base_dest).suffix
                dest_path = os.path.join(dest_dir, f"{stem} ({counter}){ext}")
                counter += 1

            _shutil.copy2(src_path, dest_path)
            if add_file_to_db(dest_path):
                # Newly imported song -- push it into Mixxx in the background.
                auto_sync_new_songs([dest_path])
                queue_auto_export()

            with get_db() as db:
                row = db.execute('SELECT id FROM songs WHERE filepath=?', (dest_path,)).fetchone()
                if row:
                    song_id = row['id']
                    download_status[job_id]['added_song_ids'].append(song_id)
                    threading.Thread(target=analyze_and_store,
                        args=[dest_path, song_id, None], daemon=True).start()

                    # Figure out the immediate parent folder name once --
                    # used for BOTH auto-crate and auto-genre, independently
                    parent_folder_name = None
                    rel = os.path.relpath(src_path, source_folder)
                    parts = rel.split(os.sep)
                    if len(parts) > 1:
                        parent_folder_name = parts[-2]

                    if auto_crate_by_subfolder and parent_folder_name:
                        crate_assignments.setdefault(parent_folder_name, []).append(song_id)
                    elif crate_id:
                        crate_assignments.setdefault('__explicit__', []).append(song_id)

                    if auto_genre_by_subfolder and parent_folder_name:
                        # A folder called '#demos' means a tag, not a genre.
                        folder_genre, folder_tags = split_genre_tags(parent_folder_name)
                        write_file_genre(dest_path, folder_genre)
                        db.execute('UPDATE songs SET genre=? WHERE id=?',
                            (folder_genre, song_id))
                        db.commit()
                        if folder_tags:
                            add_song_tags(song_id, folder_tags, db)
                    schedule_auto_cleanup(song_id)

            download_status[job_id]['done'] += 1

        # Now create/find each needed crate and assign songs
        with get_db() as db:
            for crate_name, song_ids in crate_assignments.items():
                if crate_name == '__explicit__':
                    cid = crate_id
                    label = None
                else:
                    cid = get_or_create_crate(crate_name, db)
                    label = crate_name
                for sid in song_ids:
                    db.execute('INSERT OR IGNORE INTO crate_songs (crate_id, song_id) VALUES (?, ?)',
                        (cid, sid))
                if label:
                    download_status[job_id]['crates_used'].append(f"{label} ({len(song_ids)})")
            db.commit()

        download_status[job_id]['status'] = 'success'
        # Imported tracks missing a genre are swept up by the same auto-sweep
        # that follows a scan (a no-op unless the setting is on).
        maybe_auto_genre_backfill('folder import')
        added = len(download_status[job_id]['added_song_ids'])
        dupes = download_status[job_id]['skipped_duplicate']
        crates_note = ''
        if download_status[job_id]['crates_used']:
            crates_note = ' into ' + ', '.join(download_status[job_id]['crates_used'])
        subprocess.run(['osascript', '-e',
            f'display notification "{added} imported{crates_note}, {dupes} already owned" with title "DJ Library — Folder import"'])

    threading.Thread(target=do_import, daemon=True).start()

    return jsonify({
        'status': 'queued',
        'job_id': job_id,
        'total': len(audio_files)
    })

@app.route('/api/import-folder-status/<job_id>')
def get_import_status(job_id):
    return jsonify(download_status.get(job_id, {'status': 'unknown'}))

@app.route('/api/scan', methods=['POST'])
def scan():
    added, purged = scan_library()
    return jsonify({'scanned':added, 'purged':purged})

@app.route('/api/download-spotify-list', methods=['POST'])
def download_spotify_list():
    data = request.json
    urls = data.get('urls', [])
    subfolder = data.get('subfolder')
    crate_id = data.get('crate_id')
    # Accepts both shapes callers use: a list (what the Raycast script sends,
    # e.g. ['#peak-hour', '#tool']) and a string ('#peak-hour #tool').
    tags = _tags_from_payload(data.get('tags'))

    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400

    batch_id = hashlib.md5(str(urls).encode()).hexdigest()[:8]
    download_status[batch_id] = {
        'status': 'downloading',
        'total': len(urls),
        'done': 0,
        'failed': 0,
        'duplicate': 0
    }

    def process_list():
        for url in urls:
            url = url.strip()
            if not url:
                continue
            info = get_track_info(url)
            if not info:
                download_status[batch_id]['failed'] += 1
                continue

            job_id = info['id']

            with get_db() as db:
                existing = db.execute(
                    'SELECT id FROM songs WHERE spotify_id=?', (job_id,)
                ).fetchone()
            if existing:
                download_status[batch_id]['duplicate'] += 1
                continue

            do_download(
                f"{info['artist']} {info['title']}",
                spotify_id=job_id,
                title=info['title'],
                artist=info['artist'],
                album=info.get('album'),
                year=info.get('year'),
                subfolder=subfolder,
                crate_id=crate_id,
                tags=tags
            )
            result = download_status.get(job_id, {})
            if result.get('status') == 'failed':
                download_status[batch_id]['failed'] += 1
            else:
                download_status[batch_id]['done'] += 1

        download_status[batch_id]['status'] = 'success'
        done = download_status[batch_id]['done']
        dupes = download_status[batch_id]['duplicate']
        failed = download_status[batch_id]['failed']
        msg = f"{done} downloaded, {dupes} already owned, {failed} failed"
        subprocess.run(['osascript', '-e',
            f'display notification "{msg}" with title "DJ Library"'])

    threading.Thread(target=process_list, daemon=True).start()

    return jsonify({
        'status': 'queued',
        'batch_id': batch_id,
        'total': len(urls)
    })

@app.route('/api/download-list', methods=['POST'])
def download_list():
    data = request.json
    text = data.get('text', '').strip()
    subfolder = data.get('subfolder')
    crate_id = data.get('crate_id')
    tags = _tags_from_payload(data.get('tags'))

    if not text:
        return jsonify({'error': 'No text provided'}), 400

    tracks = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith('title') and ('artist' in line.lower() or ',' in line):
            continue
        if '\t' in line:
            parts = line.split('\t')
            query = f"{parts[1].strip()} {parts[0].strip()}" if len(parts) >= 2 else parts[0]
        elif ' - ' in line:
            parts = line.split(' - ', 1)
            query = f"{parts[0].strip()} {parts[1].strip()}"
        elif ',' in line:
            parts = [p.strip().strip('"') for p in line.split(',')]
            query = f"{parts[1]} {parts[0]}" if len(parts) >= 2 else parts[0]
        else:
            query = line
        if query:
            tracks.append(query)

    if not tracks:
        return jsonify({'error': 'No tracks found in text'}), 400

    batch_id = hashlib.md5(text.encode()).hexdigest()[:8]
    download_status[batch_id] = {
        'status': 'downloading',
        'total': len(tracks),
        'done': 0,
        'failed': 0
    }

    def download_batch():
        for query in tracks:
            do_download(query, subfolder=subfolder, crate_id=crate_id, tags=tags)
            st = download_status[batch_id]
            job_id = hashlib.md5(query.encode()).hexdigest()[:16]
            result = download_status.get(job_id, {})
            if result.get('status') == 'failed':
                st['failed'] += 1
            else:
                st['done'] += 1
        download_status[batch_id]['status'] = 'success'
        done_count = st['done']
        fail_count = st['failed']
        subprocess.run(['osascript', '-e',
            f'display notification "{done_count} downloaded, {fail_count} failed" with title "DJ Library"'])

    threading.Thread(target=download_batch, daemon=True).start()

    return jsonify({
        'status': 'queued',
        'batch_id': batch_id,
        'tracks': len(tracks),
        'preview': tracks[:5]
    })

@app.route('/api/download-status/batch/<batch_id>')
def get_batch_status(batch_id):
    return jsonify(download_status.get(batch_id, {'status': 'unknown'}))

@app.route('/api/download', methods=['POST'])
def download():
    data     = request.json
    query    = data.get('query','')
    subfolder = data.get('subfolder')
    crate_id = data.get('crate_id')
    tags     = _tags_from_payload(data.get('tags'))

    if 'open.spotify.com/track/' in query:
        info = get_track_info(query)
        if info:
            download_status[info['id']] = {'status':'downloading','query':query}
            threading.Thread(target=do_download, args=[
                f"{info['artist']} {info['title']}", info['id'],
                info['title'], info['artist'], info.get('album'), info.get('year'), subfolder, crate_id, tags
            ]).start()
            return jsonify({'status':'queued','track':info})
        return jsonify({'status':'failed','error':'Could not fetch Spotify info'})

    elif 'open.spotify.com/playlist/' in query:
        tracks, playlist_name, fetch_status = get_playlist_tracks(query)
        if not tracks:
            if fetch_status == 'blocked':
                return jsonify({'status':'failed','error':"Spotify doesn't allow apps to read this playlist (popular or other users' playlists can be restricted). Copy it into your own Spotify library first, then try again."})
            if fetch_status == 'error':
                return jsonify({'status':'failed','error':'Could not fetch playlist'})
            return jsonify({'status':'failed','error':'Playlist has no downloadable tracks'})
        already_have, missing = [], []
        with get_db() as db:
            for t in tracks:
                ex = db.execute('SELECT id FROM songs WHERE spotify_id=?',(t['id'],)).fetchone()
                (already_have if ex else missing).append(t)
        def dl_playlist():
            for t in missing:
                do_download(f"{t['artist']} {t['title']}", t['id'],
                    t['title'], t['artist'], t.get('album'), t.get('year'), subfolder, crate_id, tags)
        threading.Thread(target=dl_playlist).start()
        return jsonify({
            'status':'queued',
            'playlist_name': playlist_name,
            'total': len(tracks),
            'already_have': len(already_have),
            'downloading': len(missing)
        })

    else:
        job_id = hashlib.md5(query.encode()).hexdigest()[:16]
        download_status[job_id] = {'status':'downloading','query':query}
        threading.Thread(target=do_download, args=[query,None,None,None,None,None,subfolder,crate_id,tags]).start()
        return jsonify({'status':'queued','query':query,'job_id':job_id})

@app.route('/api/download-status/<path:job_id>')
def get_download_status(job_id):
    from urllib.parse import unquote
    return jsonify(download_status.get(unquote(job_id),{'status':'unknown'}))

@app.route('/api/duplicates')
def get_duplicates():
    with get_db() as db:
        all_songs = [dict(r) for r in db.execute('SELECT * FROM songs').fetchall()]

    # Backfill hashes for rows that predate the file_hash column (or were ever
    # added without one) so they still participate in exact matching. The value
    # is persisted once so this pass is cheap on every later scan.
    with get_db() as db:
        for s in all_songs:
            if not s.get('file_hash') and os.path.exists(s['filepath']):
                s['file_hash'] = file_hash(s['filepath'])
                if s['file_hash']:
                    db.execute('UPDATE songs SET file_hash=? WHERE id=?',
                        (s['file_hash'], s['id']))
        db.commit()

    # Index songs by hash, cleaned title, and cleaned filename so detection is
    # one pass over the library instead of the old O(n^2) scan (n full-library
    # queries). The filename index catches Finder copies -- same track under
    # "Animals.mp3" and "Animals (1).mp3" -- that differ in audio bytes and
    # tags but keep the same name and duration.
    by_hash, by_title, by_name = {}, {}, {}
    for s in all_songs:
        if s.get('file_hash'):
            by_hash.setdefault(s['file_hash'], []).append(s)
        if s.get('title'):
            by_title.setdefault(clean_noise(s['title']), []).append(s)
        if s.get('filename'):
            by_name.setdefault(clean_filename(s['filename']), []).append(s)

    groups, grouped = [], set()
    for song in all_songs:
        if song['id'] in grouped: continue
        fhash = song.get('file_hash')
        best = {}   # matched song id -> strongest match type
        def note(s, t):
            if s['id'] in best and TYPE_PRIORITY[best[s['id']]] >= TYPE_PRIORITY[t]:
                return
            best[s['id']] = t
        if fhash:
            for s in by_hash.get(fhash, []):
                if s['id'] != song['id']:
                    note(s, 'exact')
        if song.get('title'):
            for s in by_title.get(clean_noise(song['title']), []):
                if s['id'] == song['id']: continue
                if s.get('file_hash') and s['file_hash'] == fhash:
                    note(s, 'exact'); continue   # same file -- a true copy
                if (artists_compatible(song.get('artist', ''), s.get('artist', ''))
                        and durations_close(song.get('duration_seconds'),
                                            s.get('duration_seconds'))):
                    note(s, 'fuzzy')
        if song.get('filename'):
            for s in by_name.get(clean_filename(song['filename']), []):
                if s['id'] == song['id']: continue
                if s.get('file_hash') and s['file_hash'] == fhash:
                    note(s, 'exact'); continue   # already a true copy
                if (durations_close(song.get('duration_seconds'),
                                    s.get('duration_seconds'))
                        or titles_match(song.get('title', ''), s.get('title', ''))):
                    note(s, 'name')
        if best:
            members = [song] + [s for s in all_songs if s['id'] in best]
            ids = {s['id'] for s in members}
            if not ids.issubset(grouped):
                gtype = max((best[m['id']] for m in members[1:]), key=TYPE_PRIORITY.get)
                # Attach bitrate to each song so the frontend can show which
                # file is actually higher quality, not just which is bigger
                for s in members:
                    s['bitrate'] = get_bitrate(s['filepath']) if os.path.exists(s['filepath']) else 0
                groups.append({'type': gtype, 'songs': members})
                grouped.update(ids)
    return jsonify(groups)

def _merge_field_values(keeper, others, field):
    """
    Returns the best value for a field during a merge:
    prefer the keeper's own value if it has one, otherwise fall back to
    the first non-empty value found among the other (lower quality) songs.
    This matches the common case of a high quality file with missing tags
    being merged with a lower quality file that already has good tags.
    """
    val = keeper.get(field)
    if val not in (None, ''):
        return val
    for o in others:
        v = o.get(field)
        if v not in (None, ''):
            return v
    return None

@app.route('/api/merge-preview', methods=['POST'])
def merge_preview():
    """
    Given a list of song IDs that represent the same track, figures out
    which file is the highest quality (by bitrate) and proposes merged
    metadata -- but does NOT write anything yet. The frontend shows this
    to the user for review/editing before calling /api/merge-execute.
    """
    data = request.json
    song_ids = data.get('song_ids', [])
    if len(song_ids) < 2:
        return jsonify({'error': 'Need at least 2 songs to merge'}), 400

    with get_db() as db:
        songs = [dict(r) for r in db.execute(
            f"SELECT * FROM songs WHERE id IN ({','.join('?'*len(song_ids))})", song_ids
        ).fetchall()]

    if len(songs) < 2:
        return jsonify({'error': 'Songs not found'}), 404

    for s in songs:
        s['bitrate'] = get_bitrate(s['filepath']) if os.path.exists(s['filepath']) else 0

    # Highest bitrate wins; ties broken by larger file size
    songs.sort(key=lambda s: (s['bitrate'], s.get('file_size_bytes') or 0), reverse=True)
    keeper = songs[0]
    losers = songs[1:]

    merged_fields = {}
    for field in ('title', 'artist', 'album', 'year', 'genre', 'bpm', 'musical_key'):
        merged_fields[field] = _merge_field_values(keeper, losers, field)

    # Find which crates the losing songs belong to -- these will be
    # transferred to the keeper so you don't lose crate membership
    with get_db() as db:
        crate_rows = db.execute(
            f"SELECT DISTINCT crates.id, crates.name FROM crate_songs "
            f"JOIN crates ON crates.id = crate_songs.crate_id "
            f"WHERE crate_songs.song_id IN ({','.join('?'*len(losers))})",
            [l['id'] for l in losers]
        ).fetchall() if losers else []

    return jsonify({
        'keeper': keeper,
        'losers': losers,
        'merged_fields': merged_fields,
        'crates_to_transfer': [dict(r) for r in crate_rows]
    })

@app.route('/api/merge-execute', methods=['POST'])
def merge_execute():
    """
    Performs the actual merge: writes the (possibly user-edited) metadata
    onto the keeper file, moves crate memberships from the losing songs to
    the keeper, then deletes the losing files and database rows.
    """
    data = request.json
    keeper_id = data.get('keeper_id')
    loser_ids = data.get('loser_ids', [])
    fields = data.get('fields', {})

    if not keeper_id or not loser_ids:
        return jsonify({'error': 'Missing keeper_id or loser_ids'}), 400

    with get_db() as db:
        keeper = db.execute('SELECT * FROM songs WHERE id=?', (keeper_id,)).fetchone()
        if not keeper:
            return jsonify({'error': 'Keeper song not found'}), 404
        keeper = dict(keeper)

        # Write merged metadata into the DB and the actual file
        allowed = ['title', 'artist', 'album', 'year', 'genre', 'bpm', 'musical_key']
        updates = {k: v for k, v in fields.items() if k in allowed and v not in (None, '')}
        # A '#tag' in the merged genre is a tag, not a genre.
        merged_tags = []
        if 'genre' in updates:
            updates['genre'], merged_tags = split_genre_tags(updates['genre'])
        if updates:
            sets = ','.join(f'{k}=?' for k in updates)
            db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(), keeper_id))
            db.commit()
            write_file_genre(keeper['filepath'], updates.get('genre'))
            write_metadata(keeper['filepath'],
                title=updates.get('title'), artist=updates.get('artist'),
                album=updates.get('album'), year=updates.get('year'),
                bpm=updates.get('bpm'))
            if merged_tags:
                add_song_tags(keeper_id, merged_tags, db)
            db.commit()

        # Transfer crate memberships from losers to the keeper
        for loser_id in loser_ids:
            crate_rows = db.execute(
                'SELECT crate_id FROM crate_songs WHERE song_id=?', (loser_id,)
            ).fetchall()
            for row in crate_rows:
                db.execute('INSERT OR IGNORE INTO crate_songs (crate_id, song_id) VALUES (?, ?)',
                    (row['crate_id'], keeper_id))
            db.execute('DELETE FROM crate_songs WHERE song_id=?', (loser_id,))
        db.commit()

        # Tags move to the keeper the same way crate memberships do, so merging
        # duplicates never silently drops a tag.
        for loser_id in loser_ids:
            inherited = get_song_tags(loser_id, db)
            if inherited:
                add_song_tags(keeper_id, inherited, db)

        # Repoint transitions involving losers to the keeper (deduplicated) so
        # mix notes survive duplicate cleanup -- mirroring the crate transfer.
        for loser_id in loser_ids:
            # A mashup row SOURCED by the loser is re-based onto the keeper
            # (dropped if its output would become the keeper itself).
            for row in db.execute('SELECT id, to_song_id, to_text FROM transitions WHERE from_song_id=? AND is_mashup_track=1',
                    (loser_id,)).fetchall():
                if row['to_song_id'] == keeper_id:
                    db.execute('DELETE FROM transitions WHERE id=?', (row['id'],))
                    continue
                dup = db.execute('SELECT 1 FROM transitions WHERE from_song_id=? AND is_mashup_track=1 AND to_song_id IS ? AND to_text IS ?',
                    (keeper_id, row['to_song_id'], row['to_text'])).fetchone()
                if dup:
                    db.execute('DELETE FROM transitions WHERE id=?', (row['id'],))
                else:
                    db.execute('UPDATE transitions SET from_song_id=? WHERE id=?', (keeper_id, row['id']))
            # incoming: loser is the target -- becomes source -> keeper (dropped
            # if the source is the keeper itself, which would be a self-link)
            for row in db.execute('SELECT id, from_song_id, to_text FROM transitions WHERE to_song_id=?',
                    (loser_id,)).fetchall():
                if row['from_song_id'] == keeper_id:
                    db.execute('DELETE FROM transitions WHERE id=?', (row['id'],))
                    continue
                dup = db.execute('SELECT 1 FROM transitions WHERE to_song_id=? AND from_song_id IS ? AND to_text IS ?',
                    (keeper_id, row['from_song_id'], row['to_text'])).fetchone()
                if dup:
                    db.execute('DELETE FROM transitions WHERE id=?', (row['id'],))
                else:
                    db.execute('UPDATE transitions SET to_song_id=? WHERE id=?', (keeper_id, row['id']))
        db.commit()

        # Delete the losing files and their database rows (including crate
        # memberships and transition links pointing at them)
        for loser_id in loser_ids:
            row = db.execute('SELECT filepath FROM songs WHERE id=?', (loser_id,)).fetchone()
            if row and os.path.exists(row['filepath']):
                os.remove(row['filepath'])
            # Repoint mashup-ingredient references first (the delete below
            # would NULL them) so a kept mashup stays consistent with the
            # kept track instead of silently losing its input.
            db.execute('UPDATE transitions SET mashup_input_song_id=? '
                       'WHERE is_mashup_track=1 AND mashup_input_song_id=?',
                       (keeper_id, loser_id))
            _delete_song_rows(db, [loser_id])
        db.commit()

    return jsonify({'ok': True, 'kept': keeper_id, 'deleted': loser_ids})

@app.route('/api/reveal', methods=['POST'])
def reveal_in_finder():
    filepath = request.json.get('filepath')
    if filepath and os.path.exists(filepath):
        subprocess.run(['open','-R',filepath])
        return jsonify({'ok':True})
    return jsonify({'error':'File not found'}),404

@app.route('/api/analyze-bpm', methods=['POST'])
def trigger_bpm_analysis():
    if _bpm_analysis_lock.locked():
        return jsonify({'status':'already-running'})
    threading.Thread(target=analyze_library_bpm,daemon=True).start()
    with get_db() as db:
        total = db.execute('SELECT COUNT(*) FROM songs WHERE bpm IS NULL').fetchone()[0]
    return jsonify({'status':'started','songs_to_analyze':total})

@app.route('/api/python-status')
def python_status():
    """Report whether a usable Python 3 is available on the system PATH (what
    install.command looks for) versus only the interpreter this app happens to
    be running under. Lets the Settings 'Install Python' button show the real
    status."""
    found = None
    for c in ['python3.13','python3.12','python3.11','python3']:
        try:
            out = subprocess.run([c,'-c',"import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                vs = subprocess.run([c,'-c','import sys;print(sys.version.split()[0])'],
                                    capture_output=True, text=True)
                found = {'cmd': c, 'version': vs.stdout.strip()}
                break
        except (FileNotFoundError, OSError):
            continue

    brew = False
    try:
        brew = subprocess.run(['brew','--version'], capture_output=True, text=True).returncode == 0
    except (FileNotFoundError, OSError):
        pass

    running = None
    try:
        import platform as _p
        running = {'path': sys.executable, 'version': _p.python_version()}
    except Exception:
        pass

    return jsonify({'found': found, 'brew_available': brew, 'running': running})

@app.route('/api/genres')
def get_genres():
    with get_db() as db:
        rows = db.execute('SELECT DISTINCT genre FROM songs WHERE genre IS NOT NULL AND genre!=""').fetchall()
    genres = set()
    for row in rows:
        for g in row['genre'].split(';'):
            g = g.strip()
            if g: genres.add(g)
    return jsonify(sorted(genres))

# -- TAGS API -------------------------------------------------------------
# Tags have their own tables and their own endpoints; the genre field is never
# used to carry one. These are the only ways a tag is created, renamed or
# removed, and every change also refreshes the files' [DJ Library] comments.

@app.route('/api/tags')
def list_tags():
    """Every tag in use with how many songs carry it, most-used first -- feeds
    the tag autocomplete and the review screen."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.name, COUNT(st.song_id) AS count
            FROM tags t LEFT JOIN song_tags st ON st.tag_id = t.id
            GROUP BY t.id ORDER BY count DESC, lower(t.name)''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/tags/<int:tag_id>', methods=['PATCH'])
def rename_tag(tag_id):
    """Renames a tag everywhere, or merges it into an existing tag when the new
    name is already taken -- so a typo never splits one tag in two."""
    data = request.json or {}
    name = clean_tag(data.get('name'))
    if not name:
        return jsonify({'error': 'A tag name is required'}), 400
    with get_db() as db:
        row = db.execute('SELECT id FROM tags WHERE id=?', (tag_id,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        other = db.execute('SELECT id FROM tags WHERE lower(name)=lower(?) AND id!=?',
                           (name, tag_id)).fetchone()
        merged = False
        if other:
            db.executemany('INSERT OR IGNORE INTO song_tags (song_id, tag_id) VALUES(?,?)',
                [(r['song_id'], other['id']) for r in
                 db.execute('SELECT song_id FROM song_tags WHERE tag_id=?', (tag_id,)).fetchall()])
            db.execute('DELETE FROM song_tags WHERE tag_id=?', (tag_id,))
            db.execute('DELETE FROM tags WHERE id=?', (tag_id,))
            tag_id, merged = other['id'], True
            name = db.execute('SELECT name FROM tags WHERE id=?', (tag_id,)).fetchone()['name']
        else:
            db.execute('UPDATE tags SET name=? WHERE id=?', (name, tag_id))
        db.commit()
        song_ids = [r['song_id'] for r in
                    db.execute('SELECT song_id FROM song_tags WHERE tag_id=?', (tag_id,)).fetchall()]
    for song_id in song_ids:
        sync_song_comment(song_id)
    if song_ids:
        queue_auto_export()
    return jsonify({'ok': True, 'id': tag_id, 'name': name,
                    'merged': merged, 'songs': len(song_ids)})

@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
def delete_tag(tag_id):
    """Removes a tag from every song it is on and deletes the tag itself."""
    with get_db() as db:
        row = db.execute('SELECT name FROM tags WHERE id=?', (tag_id,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        song_ids = [r['song_id'] for r in
                    db.execute('SELECT song_id FROM song_tags WHERE tag_id=?', (tag_id,)).fetchall()]
        db.execute('DELETE FROM song_tags WHERE tag_id=?', (tag_id,))
        db.execute('DELETE FROM tags WHERE id=?', (tag_id,))
        db.commit()
    for song_id in song_ids:
        sync_song_comment(song_id)
    if song_ids:
        queue_auto_export()
    return jsonify({'ok': True, 'name': row['name'], 'songs': len(song_ids)})

@app.route('/api/songs/<int:song_id>/tags', methods=['POST'])
def update_song_tags(song_id):
    """Adds and/or removes tags on one song without touching its genre -- what
    the metadata editor and the smart-crate stamping use. Body:
    {'add': [...], 'remove': [...]}; an absent list changes nothing."""
    data = request.json or {}
    for key in ('add', 'remove'):
        if key in data and not isinstance(data[key], list):
            return jsonify({'error': f'{key} must be a list of tags'}), 400
    with get_db() as db:
        exists = db.execute('SELECT 1 FROM songs WHERE id=?', (song_id,)).fetchone()
    if not exists:
        return jsonify({'error': 'not found'}), 404
    added = add_song_tags(song_id, data.get('add'))
    removed = remove_song_tags(song_id, data.get('remove'))
    if added or removed:
        sync_song_comment(song_id)
        queue_auto_export()
    return jsonify({'ok': True, 'added': added, 'removed': removed,
                    'tags': get_song_tags(song_id)})

@app.route('/api/tag-separation')
def tag_separation_status():
    """Candidates for the Tools review screen: every genre token still carrying
    the '#' marker, plus tokens on two songs or fewer (usually a tag rather than
    a genre). Read-only -- nothing moves until the POST below."""
    with get_db() as db:
        rows = db.execute("SELECT genre FROM songs "
                          "WHERE genre IS NOT NULL AND trim(genre)!=''").fetchall()
    counts = {}
    for row in rows:
        for token in str(row['genre']).split(';'):
            token = token.strip()
            if not token:
                continue
            entry = counts.setdefault(token.lower(),
                                      {'token': token, 'count': 0, 'marked': token.startswith('#')})
            entry['count'] += 1
    candidates = [c for c in counts.values() if c['marked'] or c['count'] <= 2]
    candidates.sort(key=lambda c: (not c['marked'], c['count'], c['token'].lower()))
    return jsonify({'candidates': candidates,
                    'marked': sum(1 for c in candidates if c['marked'])})

@app.route('/api/tag-separation', methods=['POST'])
def apply_tag_separation():
    """Moves one token between genre and tags across the whole library, from the
    review screen. Nothing else about a song changes."""
    data = request.json or {}
    token = str(data.get('token') or '').strip()
    action = data.get('action')
    if not token or action not in ('tag', 'genre'):
        return jsonify({'error': "token and action ('tag' or 'genre') are required"}), 400
    as_tag = action == 'tag'
    name = clean_tag(token)
    if as_tag and not name:
        return jsonify({'error': 'That token cannot be a tag'}), 400
    with get_db() as db:
        rows = db.execute('SELECT id, filepath, genre FROM songs WHERE genre LIKE ?',
                          (f'%{token}%',)).fetchall()
    moved = []
    for row in rows:
        parts = [p.strip() for p in (row['genre'] or '').split(';') if p.strip()]
        if not any(p.lower() == token.lower() for p in parts):
            continue                      # a longer token merely contains it
        genre = clean_genre('; '.join(p for p in parts if p.lower() != token.lower()))
        if not genre and not as_tag:
            genre = token.lstrip('#').strip() or None
        with get_db() as db:
            db.execute('UPDATE songs SET genre=? WHERE id=?', (genre, row['id']))
            db.commit()
        if as_tag:
            add_song_tags(row['id'], [name])
        write_file_genre(row['filepath'], genre)
        sync_song_comment(row['id'])
        moved.append(row['id'])
    if moved:
        queue_auto_export()
    return jsonify({'ok': True, 'action': action, 'token': token, 'songs': len(moved)})

# -- CRATES API ----------------------------------------------------------

@app.route('/api/crates')
def get_crates():
    # sort_order first (drag-and-drop reorder), name as the tiebreak for
    # crates that were never reordered.
    with get_db() as db:
        rows = db.execute('SELECT * FROM crates ORDER BY sort_order, name').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/crates', methods=['POST'])
def create_crate():
    data  = request.json
    name  = data.get('name','New Crate')
    smart = data.get('is_smart', False)
    rules = json.dumps(data.get('rules',[]))
    with get_db() as db:
        # New crates land at the end of the list; reordering rewrites the
        # whole section's sort_order so the max+1 here stays unambiguous.
        db.execute('INSERT INTO crates (name,is_smart,rules,sort_order) '
                   'VALUES(?,?,?,COALESCE((SELECT MAX(sort_order)+1 FROM crates),1))',
            (name, 1 if smart else 0, rules))
        db.commit()
        cid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    queue_auto_export()
    return jsonify({'id':cid,'name':name})

@app.route('/api/crates/<int:crate_id>', methods=['PATCH'])
def update_crate(crate_id):
    data = request.json
    with get_db() as db:
        if 'name' in data:
            db.execute('UPDATE crates SET name=? WHERE id=?',(data['name'],crate_id))
        if 'rules' in data:
            db.execute('UPDATE crates SET rules=? WHERE id=?',
                (json.dumps(data['rules']),crate_id))
        db.commit()
    queue_auto_export()
    return jsonify({'ok':True})

@app.route('/api/crates/<int:crate_id>', methods=['DELETE'])
def delete_crate(crate_id):
    with get_db() as db:
        db.execute('DELETE FROM crates WHERE id=?',(crate_id,))
        db.execute('DELETE FROM crate_songs WHERE crate_id=?',(crate_id,))
        db.commit()
    queue_auto_export()
    return jsonify({'ok':True})

@app.route('/api/crates/<int:crate_id>/songs')
def get_crate_songs(crate_id):
    with get_db() as db:
        crate = db.execute('SELECT * FROM crates WHERE id=?',(crate_id,)).fetchone()
        if not crate: return jsonify([])
        crate = dict(crate)
    if crate['is_smart']:
        rules = json.loads(crate['rules'] or '[]')
        return jsonify(_with_tags(evaluate_smart_crate(rules)))
    else:
        with get_db() as db:
            rows = db.execute('''SELECT s.* FROM songs s
                JOIN crate_songs cs ON s.id=cs.song_id
                WHERE cs.crate_id=? ORDER BY s.artist,s.title''',(crate_id,)).fetchall()
        return jsonify(_with_tags([dict(r) for r in rows]))


@app.route('/api/crates/reorder', methods=['POST'])
def reorder_crates():
    """Persists the sidebar's crate order: assigns sequential sort_order values
    to the given crate IDs (the frontend sends one full section -- manual or
    smart -- in its new visual order). Crates not in the list keep their
    current sort_order, so the other section is untouched."""
    data = request.json or {}
    ordered_ids = data.get('crate_ids', [])
    if not ordered_ids:
        return jsonify({'error': 'No crate IDs provided'}), 400
    with get_db() as db:
        for i, cid in enumerate(ordered_ids, start=1):
            db.execute('UPDATE crates SET sort_order=? WHERE id=?', (i, cid))
        db.commit()
    return jsonify({'ok': True, 'reordered': len(ordered_ids)})


@app.route('/api/crates/<int:crate_id>/add-songs', methods=['POST'])
def add_songs_to_crate(crate_id):
    """
    Adds a list of song IDs to a manual crate.
    Only makes sense for non-smart crates -- smart crates compute their
    membership from rules, so manually adding songs to one would have no effect.
    """
    data = request.json
    song_ids = data.get('song_ids', [])
    if not song_ids:
        return jsonify({'error': 'No song IDs provided'}), 400
    with get_db() as db:
        crate = db.execute('SELECT * FROM crates WHERE id=?', (crate_id,)).fetchone()
        if not crate:
            return jsonify({'error': 'Crate not found'}), 404
        if crate['is_smart']:
            # Smart crates compute membership from rules -- manually inserting
            # rows here would create dead crate_songs entries that never show.
            return jsonify({'error': 'Smart crates fill themselves from rules — you can\'t add songs manually.'}), 400
        for song_id in song_ids:
            db.execute('INSERT OR IGNORE INTO crate_songs (crate_id, song_id) VALUES (?, ?)',
                (crate_id, song_id))
        db.commit()
    queue_auto_export()
    return jsonify({'ok': True, 'added': len(song_ids)})

@app.route('/api/crates/<int:crate_id>/remove-songs', methods=['POST'])
def remove_songs_from_crate(crate_id):
    """Removes specific songs from a manual crate without deleting the songs themselves."""
    data = request.json
    song_ids = data.get('song_ids', [])
    with get_db() as db:
        for song_id in song_ids:
            db.execute('DELETE FROM crate_songs WHERE crate_id=? AND song_id=?',
                (crate_id, song_id))
        db.commit()
    queue_auto_export()
    return jsonify({'ok': True})

@app.route('/api/crates/<int:crate_id>/sync-mixxx', methods=['POST'])
def sync_crate_mixxx(crate_id):
    """
    Syncs a smart or manual crate from our app into Mixxx as a real crate.
    Refuses to run if Mixxx is currently open, to protect your library
    from corruption. Always backs up Mixxx's database before writing.
    """
    with get_db() as db:
        crate = db.execute('SELECT * FROM crates WHERE id=?', (crate_id,)).fetchone()
        if not crate:
            return jsonify({'ok': False, 'error': 'Crate not found'}), 404
        crate = dict(crate)

    if crate['is_smart']:
        rules = json.loads(crate['rules'] or '[]')
        songs = evaluate_smart_crate(rules)
    else:
        with get_db() as db:
            songs = [dict(r) for r in db.execute(
                "SELECT s.* FROM songs s JOIN crate_songs cs ON s.id=cs.song_id WHERE cs.crate_id=?",
                (crate_id,)
            ).fetchall()]

    filepaths = [s['filepath'] for s in songs]
    result = sync_crate_to_mixxx(crate['name'], filepaths)
    return jsonify(result)

# -- TRANSITIONS API ----------------------------------------------------------
# A transition is a directional mix note: "this song goes INTO that one". The
# target can be another song in the library (to_song_id) or a text-only track
# name (to_text) for things you don't own yet. notes/tag hold the actual mix
# info (technique, timing, energy level...).

@app.route('/api/songs/<int:song_id>/transitions')
def get_transitions(song_id):
    """Returns the outgoing and incoming transitions for one song.
    Outgoing rows carry the target's metadata (for in-library targets) or the
    text name (for text-only targets); incoming rows carry the source's info."""
    with get_db() as db:
        if not db.execute('SELECT id FROM songs WHERE id=?', (song_id,)).fetchone():
            return jsonify({'error': 'Song not found'}), 404
        outgoing = db.execute('''SELECT t.id, t.to_song_id, t.to_text, t.notes, t.tag, t.status, t.folder_id,
                t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                s.title AS to_title, s.artist AS to_artist,
                s.filename AS to_filename, s.bpm AS to_bpm, s.musical_key AS to_key,
                mis.title AS mi_title, mis.artist AS mi_artist
            FROM transitions t
            LEFT JOIN songs s ON s.id = t.to_song_id
            LEFT JOIN songs mis ON mis.id = t.mashup_input_song_id
            WHERE t.from_song_id = ?
            ORDER BY t.created_at, t.id''', (song_id,)).fetchall()
        incoming = db.execute('''SELECT t.id, t.from_song_id, t.notes, t.tag, t.status, t.folder_id,
                t.is_mashup_track,
                s.title AS from_title, s.artist AS from_artist, s.filename AS from_filename
            FROM transitions t
            JOIN songs s ON s.id = t.from_song_id
            WHERE t.to_song_id = ?
            ORDER BY t.created_at, t.id''', (song_id,)).fetchall()
    return jsonify({'outgoing': [dict(r) for r in outgoing],
                    'incoming': [dict(r) for r in incoming]})

@app.route('/api/songs/<int:song_id>/transitions', methods=['POST'])
def add_transition(song_id):
    """Creates a transition from song_id to either a library song (to_song_id)
    or a text-only track name (to_text). Exactly one of the two is required.
    With is_mashup_track=1 the row instead records a MASHUP whose source is
    song_id: the target is the output song it lands into, and
    mashup_input_song_id / mashup_input_text (exactly one) name the input
    song it blends in from."""
    data = request.json or {}
    to_song_id = data.get('to_song_id')
    to_text = (data.get('to_text') or '').strip()
    notes = (data.get('notes') or '').strip()
    tag = (data.get('tag') or '').strip()
    status = (data.get('status') or 'confirmed').strip()
    if status not in ('confirmed', 'to_try'):
        status = 'confirmed'
    is_mashup = 1 if data.get('is_mashup_track') else 0
    mi_song_id = data.get('mashup_input_song_id')
    mi_text = (data.get('mashup_input_text') or '').strip()
    folder_id = data.get('folder_id')
    if folder_id is not None:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid folder'}), 400

    if to_song_id is not None and to_text:
        return jsonify({'error': 'Pick either a library song OR a text target, not both'}), 400
    if to_song_id is None and not to_text:
        return jsonify({'error': 'Pick a target song or enter a track name'}), 400
    if is_mashup and mi_song_id is None and not mi_text:
        return jsonify({'error': 'A mashup needs an input song — pick one or enter its name'}), 400

    try:
        to_song_id = int(to_song_id) if to_song_id is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid target song'}), 400
    try:
        mi_song_id = int(mi_song_id) if mi_song_id is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid input song'}), 400

    with get_db() as db:
        if not db.execute('SELECT id FROM songs WHERE id=?', (song_id,)).fetchone():
            return jsonify({'error': 'Source song not found'}), 404
        if folder_id is not None and not db.execute('SELECT id FROM transition_folders WHERE id=?', (folder_id,)).fetchone():
            return jsonify({'error': 'Folder not found'}), 404
        if to_song_id is not None:
            if to_song_id == song_id:
                return jsonify({'error': 'A song cannot transition into itself'}), 400
            if not db.execute('SELECT id FROM songs WHERE id=?', (to_song_id,)).fetchone():
                return jsonify({'error': 'Target song not found'}), 404
        if mi_song_id is not None and not db.execute('SELECT id FROM songs WHERE id=?', (mi_song_id,)).fetchone():
            return jsonify({'error': 'Input song not found'}), 404
        db.execute('''INSERT INTO transitions (from_song_id, to_song_id, to_text, notes, tag, status, folder_id,
                is_mashup_track, mashup_input_song_id, mashup_input_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (song_id, to_song_id, to_text or None, notes or None, tag or None, status, folder_id,
             is_mashup, mi_song_id if is_mashup else None, (mi_text or None) if is_mashup else None))
        db.commit()
        tid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    sync_transitions_after_change(song_id)
    return jsonify({'ok': True, 'id': tid})

@app.route('/api/transitions')
def list_transitions():
    """Every transition in the library with source and target info joined in,
    newest first -- used by the Transitions sidebar tab."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.from_song_id, t.to_song_id, t.to_text, t.notes, t.tag, t.status,
                t.folder_id, t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                tf.name AS folder_name,
                fs.title AS from_title, fs.artist AS from_artist, fs.filename AS from_filename,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename,
                tis.title AS mi_title, tis.artist AS mi_artist
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs tis ON tis.id = t.mashup_input_song_id
            LEFT JOIN transition_folders tf ON tf.id = t.folder_id
            ORDER BY t.created_at DESC, t.id DESC''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/transitions/<int:tid>', methods=['PATCH'])
def update_transition(tid):
    """Edits a transition's notes/tag in place, or its mashup input/output
    (only accepted on mashup rows; either part may be a library song or text)."""
    data = request.json or {}
    with get_db() as db:
        row = db.execute('SELECT from_song_id, is_mashup_track FROM transitions WHERE id=?', (tid,)).fetchone()
        if not row:
            return jsonify({'error': 'Transition not found'}), 404
        if 'notes' in data:
            db.execute('UPDATE transitions SET notes=? WHERE id=?',
                ((data.get('notes') or '').strip() or None, tid))
        if 'tag' in data:
            db.execute('UPDATE transitions SET tag=? WHERE id=?',
                ((data.get('tag') or '').strip() or None, tid))
        if 'status' in data and data.get('status') in ('confirmed', 'to_try'):
            db.execute('UPDATE transitions SET status=? WHERE id=?',
                (data['status'], tid))
        if 'folder_id' in data:
            fid = data.get('folder_id')
            if fid in (None, ''):
                db.execute('UPDATE transitions SET folder_id=NULL WHERE id=?', (tid,))
            else:
                try:
                    fid = int(fid)
                except (TypeError, ValueError):
                    return jsonify({'error': 'Invalid folder'}), 400
                if not db.execute('SELECT id FROM transition_folders WHERE id=?', (fid,)).fetchone():
                    return jsonify({'error': 'Folder not found'}), 404
                db.execute('UPDATE transitions SET folder_id=? WHERE id=?', (fid, tid))
        if row['is_mashup_track'] and ('mashup_input_song_id' in data or 'mashup_input_text' in data
                                       or 'to_song_id' in data or 'to_text' in data):
            cur = db.execute('SELECT to_song_id, to_text, mashup_input_song_id, mashup_input_text FROM transitions WHERE id=?', (tid,)).fetchone()
            out_id = data.get('to_song_id', cur['to_song_id'])
            out_text = (data.get('to_text') if 'to_text' in data else cur['to_text']) or None
            if out_text is not None:
                out_text = str(out_text).strip() or None
            in_id = data.get('mashup_input_song_id', cur['mashup_input_song_id'])
            in_text = (data.get('mashup_input_text') if 'mashup_input_text' in data else cur['mashup_input_text']) or None
            if in_text is not None:
                in_text = str(in_text).strip() or None
            try:
                out_id = int(out_id) if out_id is not None else None
                in_id = int(in_id) if in_id is not None else None
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid song'}), 400
            if bool(out_id) == bool(out_text):
                return jsonify({'error': 'Pick either a library song OR a text target, not both'}), 400
            if bool(in_id) == bool(in_text):
                return jsonify({'error': 'Pick either a library song OR a text input, not both'}), 400
            if out_id is not None and not db.execute('SELECT id FROM songs WHERE id=?', (out_id,)).fetchone():
                return jsonify({'error': 'Output song not found'}), 404
            if in_id is not None and not db.execute('SELECT id FROM songs WHERE id=?', (in_id,)).fetchone():
                return jsonify({'error': 'Input song not found'}), 404
            db.execute('''UPDATE transitions SET to_song_id=?, to_text=?,
                mashup_input_song_id=?, mashup_input_text=? WHERE id=?''',
                (out_id, out_text, in_id, in_text, tid))
        db.commit()
        from_song_id = row['from_song_id']
    sync_transitions_after_change(from_song_id)
    return jsonify({'ok': True})

@app.route('/api/transitions/<int:tid>', methods=['DELETE'])
def delete_transition(tid):
    with get_db() as db:
        row = db.execute('SELECT from_song_id FROM transitions WHERE id=?', (tid,)).fetchone()
        if row:
            db.execute('DELETE FROM transitions WHERE id=?', (tid,))
            db.commit()
            from_song_id = row['from_song_id']
        else:
            from_song_id = None
    if from_song_id is not None:
        sync_transitions_after_change(from_song_id)
    return jsonify({'ok': True})

@app.route('/api/transitions/sync-mixxx', methods=['POST'])
def sync_transitions_mixxx():
    """Writes outgoing transition notes into Mixxx track comments."""
    with _MIXXX_SYNC_LOCK:
        return jsonify(sync_transitions_to_mixxx())

@app.route('/api/transitions/to-try')
def list_transitions_to_try():
    """All transitions marked as 'to_try' (untested), newest first."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.from_song_id, t.to_song_id, t.to_text, t.notes, t.tag, t.status,
                t.folder_id, t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                tf.name AS folder_name,
                fs.title AS from_title, fs.artist AS from_artist, fs.filename AS from_filename,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename,
                tis.title AS mi_title, tis.artist AS mi_artist
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs tis ON tis.id = t.mashup_input_song_id
            LEFT JOIN transition_folders tf ON tf.id = t.folder_id
            WHERE t.status = 'to_try'
            ORDER BY t.created_at DESC, t.id DESC''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/transitions/<int:tid>/confirm', methods=['POST'])
def confirm_transition(tid):
    """Marks a 'to_try' transition as confirmed (tested and working)."""
    with get_db() as db:
        row = db.execute('SELECT from_song_id FROM transitions WHERE id=?', (tid,)).fetchone()
        if not row:
            return jsonify({'error': 'Transition not found'}), 404
        db.execute("UPDATE transitions SET status='confirmed' WHERE id=?", (tid,))
        db.commit()
        from_song_id = row['from_song_id']
    sync_transitions_after_change(from_song_id)
    return jsonify({'ok': True})


@app.route('/api/mashups')
def list_mashups():
    """All mashup transition rows (is_mashup_track=1) with source (the mashup
    file), output (where it lands) and input (what it blends in from) resolved,
    newest first. Powers the sidebar Mashups tab."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.from_song_id, t.to_song_id, t.to_text,
                t.notes, t.tag, t.status, t.folder_id, tf.name AS folder_name,
                t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                fs.title AS from_title, fs.artist AS from_artist, fs.filename AS from_filename,
                fs.bpm AS from_bpm, fs.musical_key AS from_key,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename,
                tis.title AS mi_title, tis.artist AS mi_artist
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs tis ON tis.id = t.mashup_input_song_id
            LEFT JOIN transition_folders tf ON tf.id = t.folder_id
            WHERE t.is_mashup_track = 1
            ORDER BY t.created_at DESC, t.id DESC''').fetchall()
    return jsonify([dict(r) for r in rows])

# -- TRANSITION FOLDERS -------------------------------------------------------
# Optional grouping for transitions (like crates for songs). Deleting a folder
# never deletes transitions -- they fall back to "All transitions".

@app.route('/api/transition-folders')
def list_transition_folders():
    """Every folder with the number of transitions it contains."""
    with get_db() as db:
        rows = db.execute('''SELECT f.id, f.name, COUNT(t.id) AS count
            FROM transition_folders f
            LEFT JOIN transitions t ON t.folder_id = f.id
            GROUP BY f.id ORDER BY lower(f.name), f.id''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/transition-folders', methods=['POST'])
def create_transition_folder():
    name = ((request.json or {}).get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Folder name is required'}), 400
    if len(name) > 80:
        name = name[:80]
    with get_db() as db:
        if db.execute('SELECT id FROM transition_folders WHERE lower(name)=lower(?)', (name,)).fetchone():
            return jsonify({'error': f'A folder named "{name}" already exists'}), 409
        db.execute('INSERT INTO transition_folders (name) VALUES (?)', (name,))
        db.commit()
        fid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return jsonify({'ok': True, 'id': fid})

@app.route('/api/transition-folders/<int:fid>', methods=['PATCH'])
def rename_transition_folder(fid):
    name = ((request.json or {}).get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Folder name is required'}), 400
    if len(name) > 80:
        name = name[:80]
    with get_db() as db:
        if not db.execute('SELECT id FROM transition_folders WHERE id=?', (fid,)).fetchone():
            return jsonify({'error': 'Folder not found'}), 404
        if db.execute('SELECT id FROM transition_folders WHERE lower(name)=lower(?) AND id!=?',
                      (name, fid)).fetchone():
            return jsonify({'error': f'A folder named "{name}" already exists'}), 409
        db.execute('UPDATE transition_folders SET name=? WHERE id=?', (name, fid))
        db.commit()
    return jsonify({'ok': True})

@app.route('/api/transition-folders/<int:fid>', methods=['DELETE'])
def delete_transition_folder(fid):
    """Deletes the folder only; its transitions move back to "All transitions"."""
    with get_db() as db:
        if not db.execute('SELECT id FROM transition_folders WHERE id=?', (fid,)).fetchone():
            return jsonify({'error': 'Folder not found'}), 404
        db.execute('UPDATE transitions SET folder_id=NULL WHERE folder_id=?', (fid,))
        db.execute('DELETE FROM transition_folders WHERE id=?', (fid,))
        db.commit()
    return jsonify({'ok': True})


# -- TRANSITION TAGS ----------------------------------------------------------
# Tags are freeform labels on a transition ("peak hour", "energy +1", ...).
# There is no tag table: a tag "exists" the moment a transition carries it, so
# "creating" one is just saving it on a transition. This endpoint feeds the
# autocomplete with the tags already in use, most-used first.

@app.route('/api/transition-tags')
def list_transition_tags():
    """Every tag already used on a transition, with how many use it. Sorted by
    usage (descending) then name, so the autocomplete surfaces the staples."""
    with get_db() as db:
        rows = db.execute('''SELECT tag, COUNT(*) AS count FROM transitions
            WHERE tag IS NOT NULL AND trim(tag) != ''
            GROUP BY tag ORDER BY count DESC, lower(tag)''').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transitions/export')
def export_transitions():
    """All transitions as a flat CSV (Excel-friendly, UTF-8 BOM) with source and
    target keys/BPMs so it doubles as a set-planning worksheet."""
    with get_db() as db:
        rows = db.execute('''SELECT
                fs.artist AS from_artist, fs.title AS from_title,
                fs.musical_key AS from_key, fs.bpm AS from_bpm,
                t.to_song_id, t.to_text,
                ts.artist AS to_artist, ts.title AS to_title,
                ts.musical_key AS to_key, ts.bpm AS to_bpm,
                t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                mis.artist AS mi_artist, mis.title AS mi_title,
                t.tag, t.notes
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs mis ON mis.id = t.mashup_input_song_id
            ORDER BY lower(fs.artist), lower(fs.title), t.created_at, t.id''').fetchall()
    buf = io.StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf)
    w.writerow(['Source Artist', 'Source Title', 'Source Key', 'Source BPM',
                'Target', 'Target Artist', 'Target Title', 'Target Key', 'Target BPM',
                'Target Type', 'Mashup Input', 'Tag', 'Notes'])
    for r in rows:
        if r['is_mashup_track']:
            ttype = 'Mashup'
            if r['mashup_input_song_id']:
                minput = ' - '.join(x for x in (r['mi_artist'], r['mi_title']) if x) \
                    or 'Song #' + str(r['mashup_input_song_id'])
            else:
                minput = r['mashup_input_text'] or ''
        else:
            ttype = 'Song' if r['to_song_id'] else 'Text'
            minput = ''
        if r['to_song_id']:
            target, t_artist, t_title = r['to_title'], r['to_artist'], r['to_title']
        else:
            target = r['to_text'] or 'Untitled track'
            t_artist = t_title = None
        w.writerow([r['from_artist'], r['from_title'], r['from_key'], r['from_bpm'],
                    target, t_artist, t_title, r['to_key'], r['to_bpm'],
                    ttype, minput, r['tag'], r['notes']])
    resp = Response(buf.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = 'attachment; filename=transitions.csv'
    return resp


@app.route('/api/transitions/setlist')
def transitions_setlist():
    """A printable setlist page: transitions grouped by source song, with keys
    and BPMs, styled for printing (open it and hit Cmd+P)."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.from_song_id, t.to_song_id, t.to_text,
                t.tag, t.notes, t.is_mashup_track, t.mashup_input_song_id, t.mashup_input_text,
                mis.artist AS mi_artist, mis.title AS mi_title,
                fs.title AS from_title, fs.artist AS from_artist,
                fs.musical_key AS from_key, fs.bpm AS from_bpm,
                ts.title AS to_title, ts.artist AS to_artist,
                ts.musical_key AS to_key, ts.bpm AS to_bpm
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            LEFT JOIN songs mis ON mis.id = t.mashup_input_song_id
            ORDER BY lower(fs.artist), lower(fs.title), t.created_at, t.id''').fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r['from_song_id'], []).append(dict(r))
    e = html.escape
    date_str = datetime.now().strftime('%B %d, %Y')
    sections = []
    for ts in groups.values():
        src = ts[0]
        head = f"{e(src['from_artist'] or '?')} — {e(src['from_title'] or '?')}"
        meta = []
        if src.get('from_key'): meta.append(f"{e(src['from_key'])}")
        if src.get('from_bpm'): meta.append(f"{round(float(src['from_bpm']))} BPM")
        rows_html = []
        for t in ts:
            if t['to_song_id']:
                target = f"{e(t['to_title'] or '?')}"
                if t.get('to_artist'): target += f" — {e(t['to_artist'])}"
                tmeta = []
                if t.get('to_key'): tmeta.append(e(t['to_key']))
                if t.get('to_bpm'): tmeta.append(f"{round(float(t['to_bpm']))} BPM")
                if tmeta: target += f" <span class='m'>({', '.join(tmeta)})</span>"
            else:
                target = e(t['to_text'] or 'Untitled track')
            arrow = '→'
            if t['is_mashup_track']:
                minput = ' - '.join(x for x in (t.get('mi_artist'), t.get('mi_title')) if x) \
                    or t.get('mashup_input_text') or '?'
                target = f"<span class='mash'>⇄ Mashup</span> (in: {e(minput)}) → {target}"
                arrow = '↣'
            cells = [f"<td class='arr'>{arrow}</td><td class='tgt'>{target}</td>"]
            if t.get('tag'): cells.append(f"<td class='tag'>{e(t['tag'])}</td>")
            else: cells.append('<td class="tag"></td>')
            if t.get('notes'): cells.append(f"<td class='note'>{e(t['notes'])}</td>")
            else: cells.append('<td class="note"></td>')
            rows_html.append('<tr>' + ''.join(cells) + '</tr>')
        sections.append(f"<div class='grp'><h2>{head} <span class='m'>{' · '.join(meta)}</span></h2>"
                        f"<table>{''.join(rows_html)}</table></div>")
    body = '\n'.join(sections) if sections else \
        '<p class="empty">No transitions yet — add some from a song\'s right-click menu.</p>'
    page = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>DJ Setlist — {date_str}</title>
<style>
  body {{ font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif; margin: 28px; color: #111; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 12px; margin-bottom: 22px; }}
  .grp {{ margin-bottom: 22px; page-break-inside: avoid; }}
  h2 {{ font-size: 14px; margin: 0 0 4px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }}
  .m {{ color: #666; font-weight: normal; font-size: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: 3px 6px; font-size: 12px; vertical-align: top; }}
  .arr {{ width: 18px; color: #999; }}
  .tag {{ width: 90px; color: #555; font-style: italic; }}
  .note {{ color: #333; }}
  .mash {{ color: #0a7d68; font-style: italic; }}
  .empty {{ color: #666; }}
  @media print {{ body {{ margin: 10mm; }} .grp {{ page-break-inside: avoid; }} }}
</style></head>
<body>
<h1>DJ Setlist</h1>
<div class="sub">{date_str} — {len(groups)} source song(s), {len(rows)} transition(s)</div>
{body}
</body></html>'''
    return Response(page, mimetype='text/html')

# -- USB EXPORT SERVICE WIRING -------------------------------------------------
# Flask's application extensions own the lazily constructed service; job state
# itself remains private to USBExporter. Late-bound helpers are resolved on use.
_usb_exporter_lock = threading.Lock()


def _usb_export_service():
    with _usb_exporter_lock:
        exporter = app.extensions.get('usb_exporter')
        if exporter is None:
            exporter = usb_export_module.USBExporter(
                get_db=get_db, get_music_dir=lambda: MUSIC_DIR, crate_song_ids=crate_song_ids,
                evaluate_smart_crate=evaluate_smart_crate,
                build_rekordbox_xml=build_rekordbox_xml, to_camelot=to_camelot)
            app.extensions['usb_exporter'] = exporter
        return exporter


app.register_blueprint(usb_export_routes.create_usb_export_blueprint(_usb_export_service))


# -- DJ SOFTWARE EXPORT (Rekordbox XML) ----------------------------------------
# The industry-standard interchange format: Rekordbox, Serato DJ Pro, DJUCED
# (6.3+), Engine DJ and djay all import it natively (File -> Import). BPM, key
# (Camelot) and crates/playlists travel in one file; transition notes ride
# along in each track's Comments field.

CAMELOT_MAP = {
    'min': {'C':'5A','C#':'12A','D':'7A','D#':'2A','E':'9A','F':'4A','F#':'11A','G':'6A','G#':'1A','A':'8A','A#':'3A','B':'10A'},
    'maj': {'C':'8B','C#':'3B','D':'10B','D#':'5B','E':'12B','F':'7B','F#':'2B','G':'9B','G#':'4B','A':'11B','A#':'6B','B':'1B'},
}
_FLAT_TO_SHARP = {'bb':'A#','db':'C#','eb':'D#','gb':'F#','ab':'G#'}

def to_camelot(raw):
    """'G min' -> '6A', 'C# maj' -> '3B', 'f#minor' -> '11A'. Already-camelot
    values pass through; unparseable values return None."""
    if not raw:
        return None
    s = str(raw).strip()
    if re.match(r'^\d{1,2}[AB]$', s.upper()):
        return s.upper()
    m = re.match(r'^([a-gA-G])([#b]?)[ \t]*(maj|min|major|minor|m)?', s)
    if not m:
        return None
    root = (m.group(1).upper() + (m.group(2) or '')).lower()
    if root in _FLAT_TO_SHARP:
        root = _FLAT_TO_SHARP[root]
    root = root[0].upper() + root[1:]
    mode = (m.group(3) or '').lower()
    is_min = mode in ('min', 'minor', 'm')
    is_maj = mode in ('maj', 'major')
    if not is_min and not is_maj:
        return None
    return CAMELOT_MAP['min' if is_min else 'maj'].get(root)


def crate_song_ids(crate_id):
    """Resolves a crate's member song IDs (smart crates evaluate their rules)."""
    with get_db() as db:
        crate = db.execute('SELECT * FROM crates WHERE id=?', (crate_id,)).fetchone()
        if not crate:
            return []
        crate = dict(crate)
    if crate['is_smart']:
        rules = json.loads(crate['rules'] or '[]')
        return [s['id'] for s in evaluate_smart_crate(rules)]
    with get_db() as db:
        rows = db.execute('''SELECT s.id FROM songs s
            JOIN crate_songs cs ON s.id=cs.song_id
            WHERE cs.crate_id=?''', (crate_id,)).fetchall()
    return [r['id'] for r in rows]


def build_rekordbox_xml(songs, playlists):
    """Renders the standard rekordbox XML. `songs` are full song dicts (from the
    songs table); `playlists` is a list of (name, [song_id, ...])."""
    from urllib.parse import quote
    from xml.sax.saxutils import escape as _xesc

    track_ids = {s['id']: i + 1 for i, s in enumerate(songs)}
    tags_by_song = tags_for_songs([s['id'] for s in songs])
    kinds = {'.mp3': 'MP3 File', '.wav': 'WAV File', '.aif': 'AIFF File',
             '.aiff': 'AIFF File', '.flac': 'FLAC File', '.m4a': 'M4A File',
             '.aac': 'AAC File'}

    def attr(name, value):
        if value is None or value == '':
            return ''
        return f' {name}="{_xesc(str(value), {chr(34): "&quot;"})}"'

    tracks = []
    for s in songs:
        dur = s.get('duration_seconds')
        bitrate = ''
        if dur and s.get('file_size_bytes'):
            bitrate = str(int(s['file_size_bytes'] * 8 / dur / 1000))
        ext = Path(s['filepath']).suffix.lower()
        comments = ''
        ts = outgoing_transitions_for(s['id'])
        # Tags ride in Comments (where every DJ app shows them) while the Genre
        # attribute stays a real genre.
        song_tags_list = tags_by_song.get(s['id']) or []
        if ts or song_tags_list:
            comments = build_library_comment(ts, song_tags_list)
        tracks.append(
            '<TRACK' + attr('TrackID', track_ids[s['id']])
            + attr('Name', s.get('title') or Path(s['filepath']).stem)
            + attr('Artist', s.get('artist'))
            + attr('Album', s.get('album'))
            + attr('Genre', s.get('genre'))
            + attr('Year', s.get('year'))
            + attr('AverageBpm', f"{s['bpm']:.2f}" if s.get('bpm') else None)
            + attr('Key', to_camelot(s.get('musical_key')))
            + attr('TotalTime', int(round(dur * 1000)) if dur else None)
            + attr('BitRate', bitrate or None)
            + attr('DateAdded', (s.get('added_at') or '')[:10])
            + attr('Comments', comments or None)
            + attr('Kind', kinds.get(ext, 'MP3 File'))
            + attr('Location', 'file://localhost' + quote(s['filepath'], safe='/'))
            + '/>')

    nodes = []
    for name, ids in playlists:
        if not ids:
            continue
        rows = ''.join(f'<TRACK Key="{track_ids[i]}"/>' for i in ids if i in track_ids)
        nodes.append(f'<NODE Name="{_xesc(name)}" Type="1" Count="{len(ids)}">{rows}</NODE>')
    playlists_xml = (f'<NODE Name="ROOT" Type="0" Count="{len(nodes)}">'
                     + ''.join(nodes) + '</NODE>') if nodes else '<NODE Name="ROOT" Type="0" Count="0"/>'

    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<DJ_PLAYLISTS Version="1.0.0">\n'
            '  <PRODUCT Name="rekordbox" Version="6.0.0" Company="Pioneer DJ"/>\n'
            f'  <COLLECTION Entries="{len(songs)}">\n'
            + '\n'.join('    ' + t for t in tracks) + '\n'
            '  </COLLECTION>\n'
            '  <PLAYLISTS>\n'
            f'    {playlists_xml}\n'
            '  </PLAYLISTS>\n'
            '</DJ_PLAYLISTS>\n')


# -- AUTO-EXPORT (rekordbox.xml stays current on disk) ----------------------
# Every library change (download, import, transition, crate, delete, analysis)
# queues a background rewrite of REKORDBOX_EXPORT_PATH so DJUCED / Rekordbox /
# Serato always have a fresh file to import. Writes are coalesced and skip
# when the content is unchanged, so a burst of triggers costs one build.

_rekordbox_export_q = queue.Queue(maxsize=1)


def _whole_library_export():
    """(songs, playlists) for the full-library rekordbox export -- shared by the
    on-disk auto-export and the manual download endpoint."""
    with get_db() as db:
        rows = db.execute('SELECT * FROM songs ORDER BY artist, title').fetchall()
        crates = db.execute('SELECT id, name FROM crates ORDER BY sort_order, name').fetchall()
    songs = [dict(r) for r in rows]
    playlists = [(c['name'], crate_song_ids(c['id'])) for c in crates]
    return songs, playlists


def write_rekordbox_xml_file():
    """Writes the whole-library rekordbox XML to REKORDBOX_EXPORT_PATH. Skips
    the write when the content is unchanged, so repeated triggers are cheap.
    Never raises -- failures are logged so auto-export can never break a
    request. Returns True if the file was written."""
    try:
        songs, playlists = _whole_library_export()
        xml = build_rekordbox_xml(songs, playlists)
        os.makedirs(os.path.dirname(REKORDBOX_EXPORT_PATH) or '.', exist_ok=True)
        try:
            with open(REKORDBOX_EXPORT_PATH, 'r', encoding='utf-8') as f:
                if f.read() == xml:
                    return False
        except OSError:
            pass
        tmp = REKORDBOX_EXPORT_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(xml)
        os.replace(tmp, REKORDBOX_EXPORT_PATH)
        print(f"Auto rekordbox export: {len(songs)} tracks, "
              f"{len(playlists)} playlists -> {REKORDBOX_EXPORT_PATH}", flush=True)
        return True
    except Exception as e:
        print(f"Auto rekordbox export failed: {e}", flush=True)
        return False


def _rekordbox_export_worker():
    while True:
        _rekordbox_export_q.get()
        try:
            write_rekordbox_xml_file()
        except Exception as e:
            print(f"Auto rekordbox export failed: {e}", flush=True)
        _rekordbox_export_q.task_done()


threading.Thread(target=_rekordbox_export_worker, daemon=True).start()


def queue_auto_export():
    """Requests a background rewrite of the on-disk rekordbox.xml. Coalesces:
    if a rewrite is already pending or running, this request is dropped -- the
    pending write will produce the same file anyway. Non-blocking; call it
    from any route after a change is committed."""
    try:
        _rekordbox_export_q.put_nowait(True)
    except queue.Full:
        pass


@app.route('/api/export/rekordbox-xml')
def export_rekordbox_xml():
    """Whole library, or a single crate (?crate_id=N), as the standard
    rekordbox XML that Rekordbox and Serato DJ Pro both import (File -> Import)."""
    crate_id = request.args.get('crate_id', type=int)
    if crate_id is not None:
        ids = crate_song_ids(crate_id)
        if not ids:
            return jsonify({'error': 'Crate not found or empty'}), 404
        with get_db() as db:
            crate = db.execute('SELECT name FROM crates WHERE id=?', (crate_id,)).fetchone()
            name = crate['name'] if crate else 'crate'
        placeholders = ','.join('?' * len(ids))
        with get_db() as db:
            rows = db.execute(f'SELECT * FROM songs WHERE id IN ({placeholders})', ids).fetchall()
        songs = [dict(r) for r in rows]
        order = {i: pos for pos, i in enumerate(ids)}
        songs.sort(key=lambda s: order.get(s['id'], 999))
        xml = build_rekordbox_xml(songs, [(name, ids)])
        fname = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'crate'
    else:
        songs, playlists = _whole_library_export()
        xml = build_rekordbox_xml(songs, playlists)
        # Keep the on-disk file (that Bridge / DJUCED / Serato import from)
        # fresh even when the user triggers a manual export.
        write_rekordbox_xml_file()
        fname = 'rekordbox'
    resp = Response(xml, mimetype='application/xml')
    resp.headers['Content-Disposition'] = f'attachment; filename={fname}.xml'
    return resp


@app.route('/api/export/rekordbox-refresh', methods=['POST'])
def refresh_rekordbox_export():
    """Rewrites the always-current on-disk rekordbox.xml right now (the file
    Rekordbox Bridge / Serato / DJUCED import from). Auto-export already runs
    after every change -- this is the manual 'do it now' button, so the user
    never has to download a copy and swap it in by hand. Returns the path plus
    whether bytes actually changed, so the UI can say 'written' vs 'already up
    to date'."""
    written = write_rekordbox_xml_file()
    return jsonify({'path': REKORDBOX_EXPORT_PATH,
                    'written': bool(written),
                    'exists': os.path.isfile(REKORDBOX_EXPORT_PATH)})


@app.route('/manifest.webmanifest')
def manifest():
    return send_from_directory('static', 'manifest.webmanifest',
                               mimetype='application/manifest+json')

@app.route('/icon.svg')
def app_icon():
    return send_from_directory('static', 'icon.svg', mimetype='image/svg+xml')

@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript',
                               max_age=0)  # never cache the SW itself

@app.route('/')
def index():
    return send_from_directory('static','index.html')

# -- STARTUP ---------------------------------------------------------------

if __name__ == '__main__':
    load_oauth_tokens()
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        print("⚠  Spotify credentials not set in config.json — Spotify features disabled")
    print("DJ Library starting...")
    init_db()
    # Move any '#tag' still sitting on a genre field into the tag store, before
    # anything reads or rewrites genres (a no-op once a library is separated).
    separate_genre_tags()
    # Pick a whole-library genre backfill back up where a previous run left off
    # (started here, before the scan, so the paced lookups run while it works).
    resume_genre_backfill()
    # Refresh the yt-dlp binary in the background (while the library scans) so
    # the downloader never starts with a stale build that YouTube blocks.
    threading.Thread(target=refresh_ytdlp, daemon=True).start()
    # Make sure every song's file tag matches its transitions in the DB
    # (writes missing [DJ Library] sections, removes stale ones).
    threading.Thread(target=backfill_transition_tags, daemon=True).start()
    print("Scanning...")
    count, purged = scan_library()
    msg = f"{count} new songs"
    if purged: msg += f", {purged} missing removed"
    print(msg)
    # Guarantee the on-disk rekordbox.xml exists and is current (a no-op when
    # it already matches, so this is cheap on every launch).
    queue_auto_export()
    _start_watcher()
    print(f"Watching: {MUSIC_DIR}")
    print("Open http://localhost:3000")
    threading.Thread(target=analyze_library_bpm,daemon=True).start()
    app.run(port=3000,debug=False)
