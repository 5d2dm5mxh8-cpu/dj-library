"""USB export planning and file preparation, independent of Flask routes."""
import csv
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

AUDIO_EXTS = ('.mp3', '.wav', '.flac', '.m4a', '.aiff', '.aif', '.aac', '.ogg')
MAX_NAME = 72
_MOUNT_CACHE_SECONDS = 1.0
_mount_cache_lock = threading.Lock()
_mount_cache = {'checked_at': 0.0, 'paths': set()}


def _parse_mount_table(output):
    """Extract mount points from the platform `mount` command output."""
    paths = set()
    for line in output.splitlines():
        if ' on ' not in line:
            continue
        _, mounted = line.split(' on ', 1)
        if ' type ' in mounted:
            mounted = mounted.split(' type ', 1)[0]
        if ' (' in mounted:
            mounted = mounted.rsplit(' (', 1)[0]
        mounted = re.sub(r'\\([0-7]{3})',
                         lambda match: chr(int(match.group(1), 8)), mounted)
        mounted = mounted.replace('\\\\', '\\')
        paths.add(os.path.normpath(mounted))
    return paths


def _system_mount_points():
    """Read mount points, including APFS volumes Python's ismount can miss."""
    now = time.monotonic()
    with _mount_cache_lock:
        if now - _mount_cache['checked_at'] < _MOUNT_CACHE_SECONDS:
            return set(_mount_cache['paths'])
        try:
            result = subprocess.run(['mount'], capture_output=True, text=True,
                                    timeout=5, check=False)
            paths = _parse_mount_table(result.stdout) if result.returncode == 0 else set()
        except (OSError, subprocess.TimeoutExpired):
            paths = set()
        _mount_cache.update(checked_at=now, paths=paths)
        return set(paths)


def safe_name(value, fallback='untitled'):
    value = unicodedata.normalize('NFKC', str(value or ''))
    value = ''.join(c for c in value if c.isalnum() or c in ' ._-')
    value = re.sub(r'\s+', ' ', value).strip(' .-_')
    return value[:MAX_NAME].rstrip(' .-_') or fallback


def safe_relative(path):
    parts = Path(path).parts
    cleaned = [safe_name(part) for part in parts if part not in ('', '.', os.sep)]
    return os.path.join(*cleaned) if cleaned else 'untitled'


def resolve_volume(raw_path, music_dir, volumes_path='/Volumes'):
    """Accept writable USB/SD media mounted directly under the volumes root."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError('Choose a mounted USB drive or SD card first.')
    requested = os.path.abspath(os.path.expanduser(raw_path.strip()))
    mount_dir = os.path.abspath(os.path.expanduser(volumes_path))
    volumes_root = os.path.realpath(mount_dir)
    if os.path.dirname(requested) != mount_dir or os.path.basename(requested) in ('', '.', '..'):
        raise ValueError('Choose the USB volume itself from /Volumes, not a folder inside it.')
    if os.path.islink(requested) or not os.path.isdir(requested):
        raise ValueError('That volume is not mounted or is not a real volume.')
    root = os.path.realpath(requested)
    mounted = os.path.ismount(requested)
    if not mounted:
        mount_points = _system_mount_points()
        mounted = (os.path.normpath(requested) in mount_points
                   or os.path.normpath(root) in mount_points)
    if os.path.dirname(root) != volumes_root or not mounted:
        raise ValueError('That path is not a mounted external volume.')
    music_root = os.path.realpath(os.path.expanduser(music_dir))
    try:
        common = os.path.commonpath([root, music_root])
    except ValueError:
        common = ''
    if common in (root, music_root):
        raise ValueError('The export drive and music library must be on separate volumes.')
    if not os.access(root, os.W_OK):
        raise ValueError('The selected volume is not writable.')
    return requested, requested


def copy_file(src, dst):
    """Copy audio byte-for-byte and refuse to replace any existing entry."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
        if os.path.getsize(src) != os.path.getsize(dst):
            raise OSError('Copied file size does not match the source.')
    except Exception:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise


class USBExporter:
    """Owns one export job and coordinates planning, background copies, and status."""
    def __init__(self, get_db, get_music_dir, crate_song_ids, evaluate_smart_crate,
                 build_rekordbox_xml, to_camelot, volume_validator=None,
                 volumes_path='/Volumes'):
        self.get_db = get_db
        self.get_music_dir = get_music_dir
        self.crate_song_ids = crate_song_ids
        self.evaluate_smart_crate = evaluate_smart_crate
        self.build_rekordbox_xml = build_rekordbox_xml
        self.to_camelot = to_camelot
        self.volumes_path = os.path.abspath(os.path.expanduser(volumes_path))
        self.volume_validator = volume_validator or (
            lambda path, music_dir: resolve_volume(path, music_dir, self.volumes_path))
        self._lock = threading.Lock()
        self._job = {'status': 'idle'}

    def available_volumes(self):
        """Return mount roots that pass the same checks used by export planning."""
        music_dir = self.get_music_dir()
        try:
            names = set(os.listdir(self.volumes_path))
        except OSError:
            names = set()
        mount_roots = {
            os.path.normpath(self.volumes_path),
            os.path.normpath(os.path.realpath(self.volumes_path)),
        }
        for mount_point in _system_mount_points():
            normalized_mount = os.path.normpath(mount_point)
            if os.path.dirname(normalized_mount) in mount_roots:
                names.add(os.path.basename(normalized_mount))
        volumes = []
        for name in sorted(names):
            if name.startswith('.'):
                continue
            candidate = os.path.join(self.volumes_path, name)
            try:
                volume, _ = self.volume_validator(candidate, music_dir)
            except (OSError, ValueError):
                continue
            volumes.append({'name': os.path.basename(volume.rstrip(os.sep)), 'path': volume})
        return volumes

    def _rows_for_ids(self, song_ids):
        rows = []
        ids = sorted(set(song_ids))
        with self.get_db() as db:
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                if batch:
                    marks = ','.join('?' for _ in batch)
                    rows.extend(dict(r) for r in db.execute(
                        f'SELECT * FROM songs WHERE id IN ({marks})', batch).fetchall())
        return sorted(rows, key=lambda s: ((s.get('artist') or '').lower(),
                                           (s.get('title') or '').lower(), s['id']))

    def _selection(self, scope, crate_id, crate_ids=None):
        with self.get_db() as db:
            crates = [dict(r) for r in db.execute(
                'SELECT * FROM crates ORDER BY sort_order, name, id').fetchall()]
            songs = [dict(r) for r in db.execute(
                'SELECT * FROM songs ORDER BY artist, title, id').fetchall()] if scope == 'library' else None
            if scope == 'crate':
                if crate_id is None:
                    raise ValueError('Choose a crate to export.')
                row = db.execute('SELECT * FROM crates WHERE id=?', (crate_id,)).fetchone()
                if not row:
                    raise ValueError('That crate no longer exists.')
                selected = dict(row)
            else:
                selected = None

        if scope == 'library':
            members = {c['id']: set(self.crate_song_ids(c['id'])) for c in crates}
            return songs, crates, None, members
        if scope == 'crate':
            if selected['is_smart']:
                selected_songs = self.evaluate_smart_crate(json.loads(selected.get('rules') or '[]'))
                ids = {s['id'] for s in selected_songs}
            else:
                ids = set(self.crate_song_ids(selected['id']))
                selected_songs = self._rows_for_ids(ids)
            return selected_songs, [selected], selected['name'], {selected['id']: ids}
        if scope == 'selected_crates':
            if not crate_ids:
                raise ValueError('Choose one or more crates to export.')
            available = {c['id'] for c in crates}
            if not set(crate_ids).issubset(available):
                raise ValueError('One or more selected crates no longer exist.')
            crates = [c for c in crates if c['id'] in set(crate_ids)]
        members = {c['id']: set(self.crate_song_ids(c['id'])) for c in crates}
        ids = set().union(*members.values()) if members else set()
        return self._rows_for_ids(ids), crates, None, members

    def plan(self, mode, scope, volume_path, crate_id=None, layout='library', crate_ids=None):
        if mode not in ('rekordbox', 'engine', 'files'):
            raise ValueError('Choose Rekordbox, Engine DJ, or direct audio files as the export mode.')
        if scope not in ('library', 'crate', 'selected_crates', 'crates'):
            raise ValueError('Choose the full library, one crate, selected crates, or all crates.')
        if layout not in ('library', 'crates'):
            raise ValueError('Choose one copy per track or audio folders by crate.')
        if mode != 'files':
            layout = 'library'

        music_dir = self.get_music_dir()
        volume, root = self.volume_validator(volume_path, music_dir)
        volume_device = os.stat(root).st_dev
        songs, crates, crate_name, members = self._selection(scope, crate_id, crate_ids)
        if scope in ('crate', 'selected_crates', 'crates') and not crates:
            raise ValueError('There are no crates to export yet.')
        if scope == 'library' and not songs:
            raise ValueError('The library has no tracks to export.')
        empty_selection = scope in ('crate', 'selected_crates', 'crates') and not songs
        files, missing, unsupported = [], [], 0
        for song in songs:
            original = os.path.expanduser(str(song.get('filepath') or ''))
            if os.path.splitext(original)[1].lower() not in AUDIO_EXTS:
                unsupported += 1
                continue
            source = os.path.realpath(original)
            if not os.path.isfile(source):
                missing.append({'id': song['id'], 'filepath': song.get('filepath')})
                continue
            try:
                relative = os.path.relpath(source, os.path.realpath(music_dir))
                if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                    raise ValueError
            except ValueError:
                relative = os.path.join('Outside Library', os.path.basename(source))
            if os.path.commonpath([root, source]) == root:
                raise ValueError('A selected music file is already on the destination drive. Choose a different drive to avoid copying onto itself.')
            files.append({'id': song['id'], 'filepath': source, 'relative': safe_relative(relative),
                          'size': os.path.getsize(source), 'song': song})
        if not files and not empty_selection:
            raise ValueError('No supported audio files were found in this selection.')

        file_ids = {f['id'] for f in files}
        if mode == 'files' and layout == 'crates':
            copies = sum(1 for crate in crates for file in files
                         if file['id'] in members.get(crate['id'], set()))
            uncrated = set()
            if scope == 'library':
                assigned = set().union(*members.values()) if members else set()
                uncrated = file_ids - assigned
                copies += len(uncrated)
            total_bytes = sum(file['size'] for crate in crates for file in files
                              if file['id'] in members.get(crate['id'], set()))
            total_bytes += sum(file['size'] for file in files if file['id'] in uncrated)
        else:
            copies = len(files)
            total_bytes = sum(file['size'] for file in files)
        empty_crates = [c['name'] for c in crates
                        if not (members.get(c['id'], set()) & file_ids)]
        try:
            free = shutil.disk_usage(root).free
        except OSError as error:
            raise ValueError(f'Cannot check free space on the selected USB: {error}') from error
        needed = total_bytes + max(8 * 1024 * 1024, int(total_bytes * 0.02))
        if needed > free:
            raise ValueError(f'Not enough free space: need about {total_bytes} bytes plus a safety margin, have {free} bytes free.')
        return {'mode': mode, 'scope': scope, 'layout': layout, 'crate_id': crate_id,
                'crate_name': crate_name, 'volume': volume, 'volume_root': root,
                'volume_device': volume_device,
                'songs': [f['song'] for f in files], 'files': files, 'crates': crates,
                'crate_members': members, 'empty_crates': empty_crates,
                'missing': missing, 'unsupported': unsupported, 'track_count': len(files),
                'copy_count': copies, 'total_bytes': total_bytes, 'free_bytes': free}

    def _assert_volume(self, plan):
        try:
            volume, root = self.volume_validator(plan['volume'], self.get_music_dir())
            device = os.stat(root).st_dev
        except (OSError, ValueError) as error:
            raise RuntimeError('The selected volume is no longer available; export cancelled.') from error
        if root != plan['volume_root'] or device != plan['volume_device']:
            raise RuntimeError('The selected volume changed; export cancelled.')
        return volume, root

    def start(self, plan):
        with self._lock:
            if self._job.get('status') in ('starting', 'copying', 'writing'):
                raise RuntimeError('A USB export is already running.')
            job_id = secrets.token_hex(8)
            self._job = {'id': job_id, 'status': 'starting', 'total': plan['copy_count'] or 1,
                         'done': 0, 'copied': 0, 'failed': 0, 'missing': len(plan['missing']),
                         'bytes_total': plan['total_bytes'], 'bytes_done': 0, 'errors': [],
                         'empty_crates': list(plan['empty_crates'])}
        threading.Thread(target=self._worker, args=(plan, job_id), daemon=True).start()
        return job_id

    def status(self, job_id):
        with self._lock:
            if self._job.get('id') != job_id:
                return None
            return dict(self._job)

    def _update(self, job_id, **updates):
        with self._lock:
            if self._job.get('id') == job_id:
                self._job.update(updates)

    def _worker(self, plan, job_id):
        errors, copied = [], []
        export_dir = None
        try:
            volume, root = self._assert_volume(plan)
            base = 'DJ Library Export ' + datetime.now().strftime('%Y-%m-%d %H-%M-%S')
            export_dir = os.path.join(volume, base)
            suffix = 1
            while os.path.lexists(export_dir):
                export_dir = os.path.join(volume, f'{base} ({suffix})')
                suffix += 1
            os.mkdir(export_dir)

            files_by_id = {f['id']: f for f in plan['files']}
            tasks = []
            by_crate_layout = plan['mode'] == 'files' and plan['layout'] == 'crates'
            if by_crate_layout:
                for crate in plan['crates']:
                    folder = f"{safe_name(crate['name'], 'crate')} [{crate['id']}]"
                    for song_id in sorted(plan['crate_members'].get(crate['id'], set())):
                        file = files_by_id.get(song_id)
                        if file:
                            tasks.append((file, os.path.join('Music', 'Crates', folder, file['relative']), crate['id']))
                if plan['scope'] == 'library':
                    assigned = set().union(*plan['crate_members'].values()) if plan['crate_members'] else set()
                    for file in plan['files']:
                        if file['id'] not in assigned:
                            filename = os.path.basename(file['relative'])
                            tasks.append((file, os.path.join('Music', 'Crates', 'Uncrated', filename), None))
            else:
                tasks.extend((file, os.path.join('Music', file['relative']), None) for file in plan['files'])

            resolved, used = [], set()
            for file, relative, crate_id in tasks:
                relative = os.path.normpath(relative)
                if os.path.isabs(relative) or relative == '..' or relative.startswith('..' + os.sep):
                    relative = os.path.join('Music', 'Outside Library', os.path.basename(relative))
                stem, ext = os.path.splitext(relative)
                candidate, suffix = relative, 1
                while candidate.casefold() in used:
                    tag = f' [{file["id"]}]' if suffix == 1 else f' [{file["id"]}-{suffix}]'
                    candidate = f'{stem}{tag}{ext}'
                    suffix += 1
                used.add(candidate.casefold())
                resolved.append((file, candidate, crate_id))

            self._update(job_id, status='copying', total=len(resolved), done=0, copied=0,
                         failed=0, bytes_total=plan['total_bytes'], bytes_done=0,
                         path=export_dir, errors=[])
            bytes_done = 0
            for index, (file, relative, crate_id) in enumerate(resolved, 1):
                try:
                    self._assert_volume(plan)
                    destination = os.path.join(export_dir, relative)
                    remaining = sum(task[0]['size'] for task in resolved[index - 1:])
                    safety = max(1024 * 1024, int(remaining * 0.02))
                    if shutil.disk_usage(plan['volume_root']).free < remaining + safety:
                        raise OSError('The destination drive no longer has enough free space; remaining tracks were not copied.')
                    copy_file(file['filepath'], destination)
                    copied.append((file, relative, crate_id))
                    bytes_done += file['size']
                    self._update(job_id, done=index, copied=len(copied), bytes_done=bytes_done)
                except RuntimeError:
                    raise
                except Exception as error:
                    errors.append(f'{os.path.basename(file["filepath"])}: {error}')
                    self._update(job_id, done=index, failed=len(errors), errors=errors[:30])

            self._assert_volume(plan)
            copied_by_crate, copied_any = {}, {}
            for file, relative, crate_id in copied:
                copied_any.setdefault(file['id'], relative)
                if crate_id is not None:
                    copied_by_crate.setdefault(crate_id, {})[file['id']] = relative
            playlists = [('All Tracks', [f['id'] for f in plan['files']], None)]
            playlists.extend((crate['name'], sorted(plan['crate_members'].get(crate['id'], set())), crate['id'])
                             for crate in plan['crates'])
            playlist_dir = os.path.join(export_dir, 'Playlists')
            self._assert_volume(plan)
            os.makedirs(playlist_dir, exist_ok=True)
            for name, ids, crate_id in playlists:
                self._assert_volume(plan)
                lookup = copied_by_crate.get(crate_id, {}) if crate_id is not None and by_crate_layout else copied_any
                suffix = crate_id if crate_id is not None else 'all'
                path = os.path.join(playlist_dir, f'{safe_name(name, "playlist")} [{suffix}].m3u8')
                with open(path, 'x', encoding='utf-8', newline='\n') as playlist:
                    playlist.write('#EXTM3U\n')
                    for song_id in ids:
                        relative = lookup.get(song_id)
                        file = files_by_id.get(song_id)
                        if not relative or not file:
                            continue
                        label = ' - '.join(x for x in (file['song'].get('artist'), file['song'].get('title')) if x)
                        label = re.sub(r'[\r\n]+', ' ', label)
                        playlist.write('#EXTINF:-1,' + (label or os.path.basename(relative)) + '\n')
                        playlist.write(os.path.relpath(os.path.join(export_dir, relative), playlist_dir).replace(os.sep, '/') + '\n')

            xml_path = None
            if plan['mode'] in ('rekordbox', 'engine'):
                xml_songs, seen = [], set()
                for file, relative, _ in copied:
                    if file['id'] not in seen:
                        seen.add(file['id'])
                        xml_songs.append({**file['song'], 'filepath': os.path.join(export_dir, relative)})
                xml_path = os.path.join(export_dir, 'rekordbox.xml')
                self._assert_volume(plan)
                with open(xml_path, 'x', encoding='utf-8', newline='\n') as out:
                    out.write(self.build_rekordbox_xml(
                        xml_songs, [(c['name'], sorted(plan['crate_members'].get(c['id'], set())))
                                    for c in plan['crates']]))

            self._assert_volume(plan)
            with open(os.path.join(export_dir, 'Tracks.csv'), 'x', encoding='utf-8-sig', newline='') as out:
                writer = csv.writer(out)
                writer.writerow(['Crate', 'Title', 'Artist', 'Album', 'Genre', 'BPM', 'Key (Camelot)', 'Exported path'])
                crate_names = {c['id']: c['name'] for c in plan['crates']}
                for file, relative, crate_id in copied:
                    song = file['song']
                    writer.writerow([crate_names.get(crate_id, ''), song.get('title') or '',
                                     song.get('artist') or '', song.get('album') or '',
                                     song.get('genre') or '', song.get('bpm') or '',
                                     self.to_camelot(song.get('musical_key')) or '', relative.replace(os.sep, '/')])
            self._assert_volume(plan)
            with open(os.path.join(export_dir, 'README.txt'), 'x', encoding='utf-8', newline='\n') as out:
                out.write('DJ Library USB export\n=====================\n\n')
                out.write('Audio files were copied without transcoding. Portable M3U8 playlists and Tracks.csv are included.\n')
                if plan['mode'] in ('rekordbox', 'engine'):
                    out.write('rekordbox.xml references these copies. Import it in the selected desktop software, then prepare its native USB library there.\n')
                    out.write('This package is not a Rekordbox/OneLibrary or Engine DJ device database.\n')
                else:
                    out.write('No Rekordbox/OneLibrary or Engine DJ database is created.\n')
                    out.write('Direct file/folder browsing and M3U8 support depend on player model and firmware.\n')
                out.write(f'Audio copies: {len(copied)}\n')
                if errors or plan['missing']:
                    out.write(f'Copy failures: {len(errors)}; missing before export: {len(plan["missing"])}\n')

            complete = not errors and not plan['missing'] and not plan['unsupported']
            if plan['unsupported']:
                errors.append(f"{plan['unsupported']} unsupported track(s) were skipped.")
            self._update(job_id, status='complete' if complete else 'partial', total=len(resolved),
                         done=len(resolved), copied=len(copied), missing=len(plan['missing']),
                         failed=len(errors), errors=errors[:30], path=export_dir,
                         playlist_count=len(playlists), bytes_done=bytes_done,
                         bytes_total=plan['total_bytes'], empty_crates=plan['empty_crates'], xml_path=xml_path)
        except Exception as error:
            self._update(job_id, status='failed', failed=len(errors) + 1,
                         error=str(error), errors=errors[:30], path=export_dir)
