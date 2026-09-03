import os
import sys
import json
import sqlite3
import threading
import subprocess
import hashlib
import re
import io
import csv
import html
import queue
import urllib.request
import mimetypes
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
AUTO_SYNC_MIXXX_PLAYS = bool(_config.get("auto_sync_mixxx_plays", True))
# How musical keys are shown: "camelot" wheel (1A/3B) or "notation"
# (Am/Bbm). Display-only -- stored keys are never rewritten.
KEY_DISPLAY = _config.get("key_display", "camelot")
# Whether new external installs should auto-install Python if it's missing
# (the installer asks, defaulting to this saved preference).
AUTO_INSTALL_PYTHON = bool(_config.get("auto_install_python", True))


def _config_dict():
    """The settings the frontend needs, as a dict (shared by GET + POST)."""
    return {'music_dir': MUSIC_DIR,
            'rekordbox_export_path': REKORDBOX_EXPORT_PATH,
            'djs_software': DJS_SOFTWARE,
            'auto_sync_mixxx': AUTO_SYNC_MIXXX,
            'auto_sync_mixxx_plays': AUTO_SYNC_MIXXX_PLAYS,
            'key_display': KEY_DISPLAY,
            'auto_install_python': AUTO_INSTALL_PYTHON}


def _save_config(updates):
    """Merges `updates` into config.json (preserving existing keys, including
    the Spotify secrets) and refreshes the affected module-level settings.
    Only settings that can change at runtime are updated here."""
    global _config, REKORDBOX_EXPORT_PATH, DJS_SOFTWARE, AUTO_SYNC_MIXXX, AUTO_SYNC_MIXXX_PLAYS, KEY_DISPLAY, AUTO_INSTALL_PYTHON
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
    if 'auto_sync_mixxx_plays' in updates:
        AUTO_SYNC_MIXXX_PLAYS = bool(updates['auto_sync_mixxx_plays'])
    if 'key_display' in updates:
        KEY_DISPLAY = updates['key_display']
    if 'auto_install_python' in updates:
        AUTO_INSTALL_PYTHON = bool(updates['auto_install_python'])

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
            play_count INTEGER DEFAULT 0,
            file_hash TEXT, spotify_id TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS crates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_smart INTEGER DEFAULT 1,
            rules TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS crate_songs (
            crate_id INTEGER, song_id INTEGER,
            PRIMARY KEY (crate_id, song_id)
        )''')
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
                    'bpm REAL','musical_key TEXT','play_count INTEGER DEFAULT 0',
                    "cleanup_scanned_at TIMESTAMP", "cleanup_decision TEXT"]:
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

def clean_genre(genre):
    """Strips meaningless category tags from a genre string.
    Handles semicolon-separated multi-genre tags (e.g. 'Music; House' -> 'House')
    and returns None if nothing meaningful remains."""
    if not genre:
        return None
    parts = [p.strip() for p in genre.split(';')]
    kept = [p for p in parts if p.lower() not in NOISE_GENRES]
    if not kept:
        return None
    return '; '.join(kept)

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
    # The library changed (adds or purges) -- refresh the on-disk rekordbox.xml.
    if added or purged:
        queue_auto_export()
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
        db.execute('DELETE FROM songs WHERE id=?', (song_id,))

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
        db.commit(); return True

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
                album=None, year=None, subfolder=None, crate_id=None):
    output_dir = os.path.join(MUSIC_DIR, subfolder) if subfolder else MUSIC_DIR
    os.makedirs(output_dir, exist_ok=True)
    job_id = spotify_id or hashlib.md5(query.encode()).hexdigest()[:16]
    download_status[job_id] = {'status':'downloading','query':query}

    if spotify_id:
        with get_db() as db:
            ex = db.execute('SELECT * FROM songs WHERE spotify_id=?',(spotify_id,)).fetchone()
            if ex:
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
        download_status[job_id] = {'status':'success','file':downloaded_path}
        subprocess.run(['osascript','-e',
            f'display notification "Downloaded: {title or query}" with title "DJ Library"'])
    else:
        download_status[job_id] = {'status':'failed','error':result.stderr[:200]}
        subprocess.run(['osascript','-e',
            f'display notification "Failed: {title or query}" with title "DJ Library"'])

# -- FOLDER WATCHER ----------------------------------------------------------

class MusicFolderHandler(FileSystemEventHandler):
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
        if field not in ('bpm','duration_seconds','genre','musical_key','artist','title','year','play_count'): continue
        if op == 'gte':      conditions.append(f'COALESCE({field}, 0) >= ?'); params.append(val)
        elif op == 'lte':    conditions.append(f'COALESCE({field}, 0) <= ?'); params.append(val)
        elif op == 'eq':     conditions.append(f'{field} = ?');  params.append(val)
        elif op == 'contains':
            conditions.append(f'{field} LIKE ?'); params.append(f'%{val}%')
        elif op == 'gt':     conditions.append(f'COALESCE({field}, 0) > ?'); params.append(val)
        elif op == 'lt':     conditions.append(f'COALESCE({field}, 0) < ?'); params.append(val)
        elif op == 'least_played': conditions.append('COALESCE(play_count, 0) <= ?'); params.append(val)
    if not conditions: return []
    where = ' AND '.join(conditions)
    with get_db() as db:
        rows = db.execute(f'SELECT * FROM songs WHERE {where} ORDER BY artist, title', params).fetchall()
    return [dict(r) for r in rows]

# -- MIXXX CRATE SYNC ---------------------------------------------------------
# Mixxx stores its entire library (tracks, crates, playlists, playcounts) in a
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
import datetime

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

    backup_path = db_path + '.backup-' + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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

# Serializes Mixxx-DB writes so the auto-sync (fired from every transition
# add/edit/delete) and the manual button can never race each other.
_MIXXX_SYNC_LOCK = threading.Lock()

def build_transition_comment(transitions):
    """Formats one song's outgoing transitions as Mixxx comment text."""
    lines = [TRANSITION_COMMENT_OPEN]
    for t in transitions:
        if t.get('to_song_id'):
            name = ' - '.join(x for x in (t.get('to_title'), t.get('to_artist')) if x) \
                   or t.get('to_filename') or '?'
        else:
            name = t.get('to_text') or 'Untitled track'
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

    backup_path = db_path + '.backup-' + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
                    s.filepath,
                    ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename
                FROM transitions t
                JOIN songs s ON s.id = t.from_song_id
                LEFT JOIN songs ts ON ts.id = t.to_song_id
                WHERE t.status = 'confirmed'
                ORDER BY t.from_song_id, t.id''').fetchall()
        by_song = {}
        for r in rows:
            by_song.setdefault(r['from_song_id'], []).append(dict(r))

        synced, not_found = 0, []
        touched = set()
        for song_id, ts in by_song.items():
            mrow = cur.execute(
                "SELECT library.id FROM library "
                "JOIN track_locations ON library.location = track_locations.id "
                "WHERE track_locations.location = ?",
                (ts[0]['filepath'],)).fetchone()
            if not mrow:
                not_found.append(ts[0]['filepath'])
                continue
            mid = mrow['id']
            touched.add(mid)
            old = cur.execute('SELECT comment FROM library WHERE id=?', (mid,)).fetchone()
            new_comment = merge_transition_comment(
                old['comment'] if old else None, build_transition_comment(ts))
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

def sync_play_counts_from_mixxx():
    """Imports Mixxx's cumulative play counts without modifying its database.
    Mixxx stores the count on library.played; some versions use date_played as
    well, but played is the stable cumulative field."""
    db_path = find_mixxx_db()
    if not db_path:
        return {'ok': False, 'error': "Could not find Mixxx's database. Open Mixxx at least once first."}
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute('PRAGMA table_info(library)').fetchall()}
        if 'played' not in cols:
            conn.close()
            return {'ok': False, 'error': "This Mixxx database does not expose play counts."}
        rows = conn.execute("""SELECT tl.location, COALESCE(l.played, 0) AS played
            FROM library l JOIN track_locations tl ON l.location=tl.id
            WHERE tl.location IS NOT NULL""").fetchall()
        conn.close()
        updated = 0
        with get_db() as db:
            for row in rows:
                cur = db.execute('UPDATE songs SET play_count=? WHERE filepath=? AND play_count<>?',
                                 (int(row['played'] or 0), row['location'], int(row['played'] or 0)))
                updated += cur.rowcount
            db.commit()
        return {'ok': True, 'updated': updated, 'tracks_read': len(rows)}
    except Exception as e:
        return {'ok': False, 'error': _mixxx_error_hint(e) + str(e)}


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

    backup_path = db_path + '.backup-' + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
                'datetime_added': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename
            FROM transitions t
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            WHERE t.from_song_id = ? AND t.status = 'confirmed'
            ORDER BY t.created_at, t.id''', (song_id,)).fetchall()
    return [dict(r) for r in rows]


def write_transition_comment_tag(filepath, transitions):
    """Ensures the file's ID3 comment contains exactly the [DJ Library] section
    built from `transitions` -- an empty list removes the section. The section
    lives in a dedicated COMM frame (desc='DJ Library') so it never touches the
    user's ordinary comment. Returns True if the file was rewritten, False if
    it was already up to date (or the write failed)."""
    section = build_transition_comment(transitions) if transitions else None
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


def sync_song_transition_tags(song_id):
    """Rebuilds one song's ID3 transition comment from the DB (removes it when
    the song has no transitions left)."""
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
    if not row:
        return
    write_transition_comment_tag(row['filepath'], outgoing_transitions_for(song_id))


def backfill_transition_tags():
    """Startup pass: makes sure every song's file tag matches the DB (writes
    missing sections, removes stale ones). Idempotent -- files that already
    match are left untouched. Runs in the background so startup stays fast."""
    try:
        with get_db() as db:
            rows = db.execute('''SELECT DISTINCT s.id, s.filepath FROM transitions t
                JOIN songs s ON s.id = t.from_song_id''').fetchall()
        written = 0
        for r in rows:
            if write_transition_comment_tag(r['filepath'], outgoing_transitions_for(r['id'])):
                written += 1
        print(f"Transition tag backfill: {written} file(s) updated")
    except Exception as e:
        print(f"Transition tag backfill failed: {e}")


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
    if 'auto_sync_mixxx_plays' in data:
        updates['auto_sync_mixxx_plays'] = bool(data['auto_sync_mixxx_plays'])
    if 'key_display' in data:
        kd = str(data['key_display']).strip()
        if kd not in ('camelot', 'notation'):
            return jsonify({'error': 'unknown key_display'}), 400
        updates['key_display'] = kd
    if 'auto_install_python' in data:
        updates['auto_install_python'] = bool(data['auto_install_python'])
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400
    _save_config(updates)
    if 'rekordbox_export_path' in updates:
        # Regenerate the file at the new location immediately.
        queue_auto_export()
    return jsonify(_config_dict())

@app.route('/api/songs')
def get_songs():
    maybe_sync_mixxx_play_counts()
    q = request.args.get('q','')
    with get_db() as db:
        if q:
            rows = db.execute('''SELECT * FROM songs
                WHERE title LIKE ? OR artist LIKE ? OR filename LIKE ? OR genre LIKE ?
                ORDER BY artist,title''', (f'%{q}%',)*4).fetchall()
        else:
            rows = db.execute('SELECT * FROM songs ORDER BY artist,title').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sync/mixxx-play-counts', methods=['POST'])
def sync_mixxx_play_counts():
    return jsonify(sync_play_counts_from_mixxx())

_PLAY_SYNC_LOCK = threading.Lock()
_last_play_sync = 0

def maybe_sync_mixxx_play_counts():
    """Activity-triggered, throttled read-only Mixxx sync. It never touches the
    Mixxx database while Mixxx is running and runs at most once per interval."""
    global _last_play_sync
    if not AUTO_SYNC_MIXXX_PLAYS or is_mixxx_running():
        return
    import time
    if time.time() - _last_play_sync < 60:
        return
    if not _PLAY_SYNC_LOCK.acquire(blocking=False):
        return
    _last_play_sync = time.time()
    def work():
        try:
            result = sync_play_counts_from_mixxx()
            if result.get('ok') and result.get('updated'):
                print(f"Auto Mixxx play sync: {result['updated']} track(s) updated", flush=True)
        finally:
            _PLAY_SYNC_LOCK.release()
    threading.Thread(target=work, daemon=True).start()

@app.route('/api/songs/<int:song_id>/played', methods=['POST'])
def mark_song_played(song_id):
    """Records one play for a song; smart crates can use this for rankings."""
    with get_db() as db:
        row = db.execute('SELECT id FROM songs WHERE id=?', (song_id,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        db.execute('UPDATE songs SET play_count=COALESCE(play_count, 0)+1 WHERE id=?', (song_id,))
        db.commit()
        count = db.execute('SELECT play_count FROM songs WHERE id=?', (song_id,)).fetchone()['play_count']
    return jsonify({'ok': True, 'play_count': count})

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

def _spotify_artist_genre(artist_id):
    """A DJ-usable genre string from the artist's Spotify profile, e.g.
    'Melodic Techno'. Prefers genre words DJ software users recognise."""
    token = get_spotify_token()
    if not token or not artist_id:
        return None
    resp = requests.get(f'https://api.spotify.com/v1/artists/{artist_id}',
        headers={'Authorization': f'Bearer {token}'})
    if resp.status_code != 200:
        return None
    genres = resp.json().get('genres', [])
    if not genres:
        return None
    preferred = [g for g in genres if any(
        w in g for w in ('house','techno','trance','disco','drum and bass','dnb',
                         'dubstep','funk','edm','downtempo','breakbeat','jungle'))]
    pick = preferred[0] if preferred else genres[0]
    return clean_genre(pick.title())

@app.route('/api/cleanup/match/<int:song_id>')
def cleanup_match(song_id):
    """Proposes proper tags for one track from Spotify. Read-only."""
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
    m = _spotify_search_track(title, artist, s['duration_seconds'])
    if not m:
        return jsonify({'found': False, 'song_id': song_id})
    m['genre'] = _spotify_artist_genre(m.pop('artist_id', None))
    m['song_id'] = song_id
    return jsonify({'found': True, **m})

@app.route('/api/cleanup/apply/<int:song_id>', methods=['POST'])
def cleanup_apply(song_id):
    """Writes reviewed tags to one track's ID3 file tags and DB row."""
    data = request.json or {}
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?', (song_id,)).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404
        filepath = row['filepath']
        updates = {k: data[k] for k in ('title','artist','album','year','genre')
                   if data.get(k)}
        if updates:
            sets = ','.join(f'{k}=?' for k in updates)
            db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(), song_id))
        if data.get('spotify_id'):
            db.execute('UPDATE songs SET spotify_id=? WHERE id=?', (data['spotify_id'], song_id))
        # Applying a fix records the decision: future "only never-scanned"
        # cleanups skip this track, so manual tag edits made afterwards are
        # never silently overwritten by Spotify data.
        db.execute('''UPDATE songs SET cleanup_scanned_at=CURRENT_TIMESTAMP,
            cleanup_decision='applied' WHERE id=?''', (song_id,))
        db.commit()
    write_metadata(filepath, **updates)
    queue_auto_export()
    return jsonify({'ok': True, 'applied': list(updates)})

@app.route('/api/cleanup/mark-scanned', methods=['POST'])
def cleanup_mark_scanned():
    """Records a cleanup decision for tracks that were scanned but not applied:
    'no_match' (Spotify had nothing) or 'skipped' (the user saw the proposal in
    the review panel and unchecked it — keep my tags). Lets later "only
    never-scanned" cleanups skip decided tracks; undecided ones come back."""
    data = request.json or {}
    decision = data.get('decision') if data.get('decision') in ('no_match', 'skipped') else 'no_match'
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

@app.route('/api/songs/<int:song_id>', methods=['PATCH'])
def update_song(song_id):
    data = request.json
    allowed = ['title','artist','album','year','genre','bpm','musical_key']
    updates = {k:v for k,v in data.items() if k in allowed}
    if not updates: return jsonify({'error':'no valid fields'}),400
    with get_db() as db:
        row = db.execute('SELECT filepath FROM songs WHERE id=?',(song_id,)).fetchone()
        if not row: return jsonify({'error':'not found'}),404
        filepath = row['filepath']
        sets = ','.join(f'{k}=?' for k in updates)
        db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(),song_id))
        db.commit()
    write_metadata(filepath,
        title=updates.get('title'), artist=updates.get('artist'),
        album=updates.get('album'), year=updates.get('year'),
        genre=updates.get('genre'), bpm=updates.get('bpm'))
    return jsonify({'ok':True})

@app.route('/api/songs/batch', methods=['PATCH'])
def batch_update():
    data    = request.json
    ids     = data.get('ids',[])
    allowed = ['title','artist','album','year','genre','bpm','musical_key']
    updates = {k:v for k,v in data.items() if k in allowed and v not in (None,'')}
    if not ids or not updates: return jsonify({'error':'missing ids or fields'}),400
    with get_db() as db:
        for song_id in ids:
            row = db.execute('SELECT filepath FROM songs WHERE id=?',(song_id,)).fetchone()
            if not row: continue
            sets = ','.join(f'{k}=?' for k in updates)
            db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(),song_id))
            write_metadata(row['filepath'],
                title=updates.get('title'), artist=updates.get('artist'),
                album=updates.get('album'), year=updates.get('year'),
                genre=updates.get('genre'), bpm=updates.get('bpm'))
        db.commit()
    return jsonify({'ok':True,'updated':len(ids)})

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
                        write_metadata(dest_path, genre=parent_folder_name)
                        db.execute('UPDATE songs SET genre=? WHERE id=?',
                            (parent_folder_name, song_id))
                        db.commit()

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
                crate_id=crate_id
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
            do_download(query, subfolder=subfolder, crate_id=crate_id)
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

    if 'open.spotify.com/track/' in query:
        info = get_track_info(query)
        if info:
            download_status[info['id']] = {'status':'downloading','query':query}
            threading.Thread(target=do_download, args=[
                f"{info['artist']} {info['title']}", info['id'],
                info['title'], info['artist'], info.get('album'), info.get('year'), subfolder, crate_id
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
                do_download(            f"{t['artist']} {t['title']}", t['id'],
                    t['title'], t['artist'], t.get('album'), t.get('year'), subfolder, crate_id)
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
        threading.Thread(target=do_download, args=[query,None,None,None,None,None,subfolder,crate_id]).start()
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
        if updates:
            sets = ','.join(f'{k}=?' for k in updates)
            db.execute(f'UPDATE songs SET {sets} WHERE id=?', (*updates.values(), keeper_id))
            db.commit()
            write_metadata(keeper['filepath'],
                title=updates.get('title'), artist=updates.get('artist'),
                album=updates.get('album'), year=updates.get('year'),
                genre=updates.get('genre'), bpm=updates.get('bpm'))

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

        # Repoint transitions involving losers to the keeper (deduplicated) so
        # mix notes survive duplicate cleanup -- mirroring the crate transfer.
        for loser_id in loser_ids:
            # outgoing: loser is the source -- becomes keeper -> target (dropped
            # if the target is the keeper itself, which would be a self-link)
            for row in db.execute('SELECT id, to_song_id, to_text FROM transitions WHERE from_song_id=?',
                    (loser_id,)).fetchall():
                if row['to_song_id'] == keeper_id:
                    db.execute('DELETE FROM transitions WHERE id=?', (row['id'],))
                    continue
                dup = db.execute('SELECT 1 FROM transitions WHERE from_song_id=? AND to_song_id IS ? AND to_text IS ?',
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

# -- CRATES API ----------------------------------------------------------

@app.route('/api/crates')
def get_crates():
    with get_db() as db:
        rows = db.execute('SELECT * FROM crates ORDER BY name').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/crates', methods=['POST'])
def create_crate():
    data  = request.json
    name  = data.get('name','New Crate')
    smart = data.get('is_smart', True)
    rules = json.dumps(data.get('rules',[]))
    with get_db() as db:
        db.execute('INSERT INTO crates (name,is_smart,rules) VALUES(?,?,?)',
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
        return jsonify(evaluate_smart_crate(rules))
    else:
        with get_db() as db:
            rows = db.execute('''SELECT s.* FROM songs s
                JOIN crate_songs cs ON s.id=cs.song_id
                WHERE cs.crate_id=? ORDER BY s.artist,s.title''',(crate_id,)).fetchall()
        return jsonify([dict(r) for r in rows])


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
        outgoing = db.execute('''SELECT t.id, t.to_song_id, t.to_text, t.notes, t.tag, t.status,
                s.title AS to_title, s.artist AS to_artist,
                s.filename AS to_filename, s.bpm AS to_bpm, s.musical_key AS to_key
            FROM transitions t
            LEFT JOIN songs s ON s.id = t.to_song_id
            WHERE t.from_song_id = ?
            ORDER BY t.created_at, t.id''', (song_id,)).fetchall()
        incoming = db.execute('''SELECT t.id, t.from_song_id, t.notes, t.tag, t.status,
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
    or a text-only track name (to_text). Exactly one of the two is required."""
    data = request.json or {}
    to_song_id = data.get('to_song_id')
    to_text = (data.get('to_text') or '').strip()
    notes = (data.get('notes') or '').strip()
    tag = (data.get('tag') or '').strip()
    status = (data.get('status') or 'confirmed').strip()
    if status not in ('confirmed', 'to_try'):
        status = 'confirmed'

    if to_song_id is not None and to_text:
        return jsonify({'error': 'Pick either a library song OR a text target, not both'}), 400
    if to_song_id is None and not to_text:
        return jsonify({'error': 'Pick a target song or enter a track name'}), 400

    try:
        to_song_id = int(to_song_id) if to_song_id is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid target song'}), 400

    with get_db() as db:
        if not db.execute('SELECT id FROM songs WHERE id=?', (song_id,)).fetchone():
            return jsonify({'error': 'Source song not found'}), 404
        if to_song_id is not None:
            if to_song_id == song_id:
                return jsonify({'error': 'A song cannot transition into itself'}), 400
            if not db.execute('SELECT id FROM songs WHERE id=?', (to_song_id,)).fetchone():
                return jsonify({'error': 'Target song not found'}), 404
        db.execute('''INSERT INTO transitions (from_song_id, to_song_id, to_text, notes, tag, status)
            VALUES (?, ?, ?, ?, ?, ?)''', (song_id, to_song_id, to_text or None, notes or None, tag or None, status))
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
                fs.title AS from_title, fs.artist AS from_artist, fs.filename AS from_filename,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            ORDER BY t.created_at DESC, t.id DESC''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/transitions/<int:tid>', methods=['PATCH'])
def update_transition(tid):
    """Edits a transition's notes/tag in place."""
    data = request.json or {}
    with get_db() as db:
        row = db.execute('SELECT from_song_id FROM transitions WHERE id=?', (tid,)).fetchone()
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
                fs.title AS from_title, fs.artist AS from_artist, fs.filename AS from_filename,
                ts.title AS to_title, ts.artist AS to_artist, ts.filename AS to_filename
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
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
                t.tag, t.notes
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            ORDER BY lower(fs.artist), lower(fs.title), t.created_at, t.id''').fetchall()
    buf = io.StringIO()
    buf.write('\ufeff')
    w = csv.writer(buf)
    w.writerow(['Source Artist', 'Source Title', 'Source Key', 'Source BPM',
                'Target', 'Target Artist', 'Target Title', 'Target Key', 'Target BPM',
                'Target Type', 'Tag', 'Notes'])
    for r in rows:
        if r['to_song_id']:
            target, t_artist, t_title = r['to_title'], r['to_artist'], r['to_title']
        else:
            target = r['to_text'] or 'Untitled track'
            t_artist = t_title = None
        w.writerow([r['from_artist'], r['from_title'], r['from_key'], r['from_bpm'],
                    target, t_artist, t_title, r['to_key'], r['to_bpm'],
                    'Song' if r['to_song_id'] else 'Text', r['tag'], r['notes']])
    resp = Response(buf.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = 'attachment; filename=transitions.csv'
    return resp


@app.route('/api/transitions/setlist')
def transitions_setlist():
    """A printable setlist page: transitions grouped by source song, with keys
    and BPMs, styled for printing (open it and hit Cmd+P)."""
    with get_db() as db:
        rows = db.execute('''SELECT t.id, t.from_song_id, t.to_song_id, t.to_text,
                t.tag, t.notes,
                fs.title AS from_title, fs.artist AS from_artist,
                fs.musical_key AS from_key, fs.bpm AS from_bpm,
                ts.title AS to_title, ts.artist AS to_artist,
                ts.musical_key AS to_key, ts.bpm AS to_bpm
            FROM transitions t
            JOIN songs fs ON fs.id = t.from_song_id
            LEFT JOIN songs ts ON ts.id = t.to_song_id
            ORDER BY lower(fs.artist), lower(fs.title), t.created_at, t.id''').fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r['from_song_id'], []).append(dict(r))
    e = html.escape
    date_str = datetime.datetime.now().strftime('%B %d, %Y')
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
            cells = [f"<td class='arr'>→</td><td class='tgt'>{target}</td>"]
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
  .empty {{ color: #666; }}
  @media print {{ body {{ margin: 10mm; }} .grp {{ page-break-inside: avoid; }} }}
</style></head>
<body>
<h1>DJ Setlist</h1>
<div class="sub">{date_str} — {len(groups)} source song(s), {len(rows)} transition(s)</div>
{body}
</body></html>'''
    return Response(page, mimetype='text/html')

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
        if ts:
            comments = build_transition_comment(ts)
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
        crates = db.execute('SELECT id, name FROM crates ORDER BY name').fetchall()
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
    observer = Observer()
    observer.schedule(MusicFolderHandler(), MUSIC_DIR, recursive=True)
    observer.start()
    print(f"Watching: {MUSIC_DIR}")
    print("Open http://localhost:3000")
    threading.Thread(target=analyze_library_bpm,daemon=True).start()
    app.run(port=3000,debug=False)
