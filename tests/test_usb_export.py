import os
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import usb_export


class UsbExportContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.music = self.root / 'music'
        self.mounts = self.root / 'volumes'
        self.volume = self.mounts / 'USB'
        self.music.mkdir()
        self.volume.mkdir(parents=True)
        self.db_path = self.root / 'library.sqlite'
        self.music_patch = patch.object(app, 'MUSIC_DIR', str(self.music))
        self.db_patch = patch.object(app, 'DB_PATH', str(self.db_path))
        self.music_patch.start(); self.db_patch.start()
        self.addCleanup(self.music_patch.stop); self.addCleanup(self.db_patch.stop)
        app.init_db()
        self.files = {}
        for sid, (relative, data) in enumerate((('House/a.mp3', b'audio-a'),
                                                 ('House/shared.wav', b'audio-shared'),
                                                 ('Unsorted/free.flac', b'audio-free')), 1):
            path = self.music / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.files[sid] = path
        with app.get_db() as db:
            for sid, path in self.files.items():
                db.execute('INSERT INTO songs(id,filename,filepath,title,artist,file_size_bytes) VALUES(?,?,?,?,?,?)',
                           (sid, path.name, str(path), f'Track {sid}', 'Artist', path.stat().st_size))
            db.execute("UPDATE songs SET genre='House' WHERE id IN (1,2)")
            db.execute("INSERT INTO crates(id,name,is_smart,rules) VALUES(1,'House',0,'[]')")
            db.executemany('INSERT INTO crate_songs VALUES(1,?)', [(1,), (2,)])
            db.execute("INSERT INTO crates(id,name,is_smart,rules) VALUES(2,'Tech',0,'[]')")
            db.execute('INSERT INTO crate_songs VALUES(2,2)')
            db.execute("INSERT INTO crates(id,name,is_smart,rules) VALUES(4,'Electro',1,?)",
                       ('[{"field":"genre","op":"eq","value":"House"}]',))
            db.execute('INSERT INTO crate_songs VALUES(4,1)')
            db.execute("INSERT INTO crates(id,name,is_smart,rules) VALUES(3,'Empty',0,'[]')")
            db.commit()
        self.service = usb_export.USBExporter(
            app.get_db, lambda: app.MUSIC_DIR, app.crate_song_ids,
            app.evaluate_smart_crate, app.build_rekordbox_xml, app.to_camelot,
            volume_validator=self.volume_validator, volumes_path=str(self.mounts))
        self.exporter_patch = patch.dict(app.app.extensions, {'usb_exporter': self.service})
        self.exporter_patch.start()
        self.addCleanup(self.exporter_patch.stop)

    def volume_validator(self, path, music_dir):
        if os.path.abspath(path) != str(self.volume):
            raise ValueError('Choose a mounted USB volume first.')
        return str(self.volume), str(self.volume)

    def plan(self, scope, layout='library', crate_id=None, mode='files', crate_ids=None):
        return self.service.plan(mode, scope, str(self.volume), crate_id, layout, crate_ids)

    def export(self, scope, layout='library', crate_id=None, mode='files', crate_ids=None):
        plan = self.plan(scope, layout, crate_id, mode, crate_ids)
        job_id = self.service.start(plan)
        for _ in range(1000):
            status = self.service.status(job_id)
            if status['status'] not in ('starting', 'copying', 'writing'):
                break
            import time
            time.sleep(.005)
        self.assertEqual(status['status'], 'complete', status)
        return pathlib.Path(status['path']), status

    def test_layout_scope_contracts_general_playlists_and_empty_crates(self):
        cases = [
            ('library', 'library', 3, 3),
            ('library', 'crates', 3, 6),
            ('crate', 'library', 2, 2),
            ('crate', 'crates', 2, 2),
            ('selected_crates', 'library', 2, 2),
            ('selected_crates', 'crates', 2, 3),
            ('crates', 'library', 2, 2),
            ('crates', 'crates', 2, 5),
        ]
        for scope, layout, tracks, copies in cases:
            with self.subTest(scope=scope, layout=layout):
                crate_ids = [1, 2] if scope == 'selected_crates' else None
                plan = self.plan(scope, layout, 1 if scope == 'crate' else None, crate_ids=crate_ids)
                self.assertEqual((plan['track_count'], plan['copy_count']), (tracks, copies))
                if scope == 'selected_crates':
                    self.assertEqual([crate['id'] for crate in plan['crates']], [1, 2])
        selected = self.plan('selected_crates', layout='crates', crate_ids=[2, 1])
        self.assertEqual([crate['id'] for crate in selected['crates']], [1, 2])
        self.assertEqual(selected['track_count'], 2)
        self.assertEqual(selected['copy_count'], 3)
        with self.assertRaisesRegex(ValueError, 'Choose one or more crates'):
            self.plan('selected_crates', crate_ids=[])
        with self.assertRaisesRegex(ValueError, 'no longer exist'):
            self.plan('selected_crates', crate_ids=[999])
        full_library_plan = self.plan('library', layout='crates')
        self.assertEqual((full_library_plan['track_count'], full_library_plan['copy_count']), (3, 6))
        output, status = self.export('library', layout='crates')
        self.assertEqual(status['empty_crates'], ['Empty'])
        self.assertFalse((output / 'Music/Crates/Empty [3]').exists())
        self.assertEqual(sum(len(list((output / 'Music/Crates').rglob(f'*.{ext}')))
                             for ext in ('mp3', 'wav', 'flac')), 6)
        uncrated_files = list((output / 'Music/Crates/Uncrated').iterdir())
        self.assertEqual([file.name for file in uncrated_files], ['free.flac'])
        self.assertEqual(uncrated_files[0].read_bytes(), b'audio-free')
        lists = list((output / 'Playlists').glob('*.m3u8'))
        self.assertTrue(any(p.name.startswith('All Tracks') for p in lists))
        self.assertTrue(any(p.name.startswith('Empty [3]') for p in lists))
        self.assertTrue(any(p.name.startswith('Electro [4]') for p in lists))
        for playlist in lists:
            for line in playlist.read_text().splitlines():
                if line and not line.startswith('#'):
                    self.assertTrue((playlist.parent / line).resolve().is_file(), line)

    def test_one_copy_layout_empty_single_crate_and_job_isolation(self):
        output, _ = self.export('library')
        self.assertEqual(len([p for p in (output / 'Music').rglob('*') if p.is_file()]), 3)
        self.assertTrue((output / 'Music/House/a.mp3').is_file())
        empty = self.plan('crate', crate_id=3)
        self.assertEqual(empty['track_count'], 0)
        output, result = self.export('crate', crate_id=3)
        self.assertEqual(result['empty_crates'], ['Empty'])
        self.assertFalse((output / 'Music/Crates/Empty [3]').exists())
        self.assertEqual(len(list((output / 'Playlists').glob('*.m3u8'))), 2)
        self.assertIsNone(self.service.status('not-this-job'))

    def test_handoff_modes_write_xml_using_copied_audio_paths(self):
        for mode in ('rekordbox', 'engine'):
            with self.subTest(mode=mode):
                output, status = self.export('library', mode=mode)
                xml = (output / 'rekordbox.xml').read_text()
                self.assertIn('<DJ_PLAYLISTS', xml)
                self.assertIn('Track 1', xml)
                self.assertEqual(status['status'], 'complete')
        for sid, path in self.files.items():
            expected = {1: b'audio-a', 2: b'audio-shared', 3: b'audio-free'}[sid]
            self.assertEqual(path.read_bytes(), expected)

    def test_http_plan_and_background_job_routes(self):
        client = app.app.test_client()
        page = client.get('/')
        try:
            self.assertEqual(page.status_code, 200)
            self.assertIn('/static/usb-export.js', page.get_data(as_text=True))
        finally:
            page.close()
        asset = client.get('/static/usb-export.js')
        try:
            self.assertEqual(asset.status_code, 200)
            asset_script = asset.get_data(as_text=True)
            self.assertIn('function startUsbExport()', asset_script)
            self.assertIn("fetch('/api/export/usb/volumes')", asset_script)
        finally:
            asset.close()
        self.assertIs(app._usb_export_service(), self.service)
        body = {'mode': 'files', 'scope': 'crate', 'crate_id': 1,
                'layout': 'library', 'volume': str(self.volume)}
        response = client.post('/api/export/usb/plan', json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual((response.json['layout'], response.json['track_count'], response.json['copy_count']),
                         ('library', 2, 2))
        response = client.post('/api/export/usb', json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        job_id = response.json['job_id']
        import time
        for _ in range(1000):
            status = client.get(f'/api/export/usb/status/{job_id}').json
            if status['status'] not in ('starting', 'copying', 'writing'):
                break
            time.sleep(.005)
        self.assertEqual(status['status'], 'complete', status)
        self.assertEqual(os.path.dirname(status['path']), str(self.volume))
        self.assertTrue(os.path.basename(status['path']).startswith('DJ Library Export '))

        selected_body = {**body, 'scope': 'selected_crates', 'crate_ids': [1, 2]}
        selected_plan = client.post('/api/export/usb/plan', json=selected_body)
        self.assertEqual(selected_plan.status_code, 200, selected_plan.get_json())
        self.assertEqual((selected_plan.json['crate_ids'], selected_plan.json['layout'],
                          selected_plan.json['track_count'], selected_plan.json['copy_count']),
                         ([1, 2], 'library', 2, 2))
        invalid_selection = client.post('/api/export/usb/plan', json={**selected_body, 'crate_ids': '1,2'})
        self.assertEqual(invalid_selection.status_code, 400)
        selected_start = client.post('/api/export/usb', json=selected_body)
        self.assertEqual(selected_start.status_code, 200, selected_start.get_json())
        selected_job = selected_start.json['job_id']
        for _ in range(1000):
            selected_status = client.get(f'/api/export/usb/status/{selected_job}').json
            if selected_status['status'] not in ('starting', 'copying', 'writing'):
                break
            time.sleep(.005)
        self.assertEqual(selected_status['status'], 'complete', selected_status)
        self.assertEqual(selected_status['playlist_count'], 3)
        self.assertFalse((pathlib.Path(selected_status['path']) / 'Music/Crates').exists())
        folder_body = {**selected_body, 'layout': 'crates'}
        folder_plan = client.post('/api/export/usb/plan', json=folder_body)
        self.assertEqual(folder_plan.status_code, 200, folder_plan.get_json())
        self.assertEqual((folder_plan.json['layout'], folder_plan.json['copy_count']), ('crates', 3))
        folder_start = client.post('/api/export/usb', json=folder_body)
        self.assertEqual(folder_start.status_code, 200, folder_start.get_json())
        folder_job = folder_start.json['job_id']
        for _ in range(1000):
            folder_status = client.get(f'/api/export/usb/status/{folder_job}').json
            if folder_status['status'] not in ('starting', 'copying', 'writing'):
                break
            time.sleep(.005)
        self.assertEqual(folder_status['status'], 'complete', folder_status)
        crate_folders = list((pathlib.Path(folder_status['path']) / 'Music/Crates').iterdir())
        self.assertEqual({folder.name.split(' [')[0] for folder in crate_folders}, {'House', 'Tech'})
        self.assertEqual(client.get('/api/export/usb/status/not-this-job').status_code, 404)

    def test_usb_volume_route_filters_mounts_with_the_production_validator(self):
        recovery = self.mounts / 'Recovery'
        recovery.mkdir()
        self.service.volume_validator = lambda path, music_dir: usb_export.resolve_volume(
            path, music_dir, volumes_path=str(self.mounts))
        realpath = os.path.realpath
        with patch('usb_export.os.path.ismount', side_effect=lambda path: path in (str(self.volume), str(recovery))), \
             patch('usb_export.os.access', side_effect=lambda path, mode: realpath(path) == realpath(self.volume)):
            client = app.app.test_client()
            response = client.get('/api/export/usb/volumes')
            plan = client.post('/api/export/usb/plan', json={
                'mode': 'files', 'scope': 'library', 'layout': 'library',
                'volume': str(self.volume)})
            recovery_plan = client.post('/api/export/usb/plan', json={
                'mode': 'files', 'scope': 'library', 'layout': 'library',
                'volume': str(recovery)})
            recovery_start = client.post('/api/export/usb', json={
                'mode': 'files', 'scope': 'library', 'layout': 'library',
                'volume': str(recovery)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['volumes'], [{'name': 'USB', 'path': str(self.volume)}])
        self.assertEqual(plan.status_code, 200, plan.get_json())
        self.assertEqual(recovery_plan.status_code, 400, recovery_plan.get_json())
        self.assertEqual(recovery_start.status_code, 400, recovery_start.get_json())

    def test_exports_create_a_new_directory_instead_of_reusing_an_existing_one(self):
        existing = self.volume / 'DJ Library Export fixed-time'
        existing.mkdir()
        sentinel = existing / 'do-not-overwrite.txt'
        sentinel.write_text('keep this old export')
        with patch('usb_export.datetime') as clock:
            clock.now.return_value.strftime.return_value = 'fixed-time'
            output, status = self.export('library')
        self.assertEqual(status['status'], 'complete', status)
        self.assertEqual(output.parent, self.volume)
        self.assertEqual(output.name, 'DJ Library Export fixed-time (1)')
        self.assertEqual(sentinel.read_text(), 'keep this old export')

    def test_export_cancels_if_mount_device_changes_after_planning(self):
        plan = self.plan('library')
        with patch('usb_export.os.stat') as stat:
            stat.return_value.st_dev = plan['volume_device'] + 1
            with self.assertRaisesRegex(RuntimeError, 'volume changed'):
                self.service._assert_volume(plan)

    def test_export_stops_before_copying_when_mount_disappears_during_worker(self):
        plan = self.plan('library')
        validator = self.service.volume_validator
        calls = 0

        def disappear_after_worker_starts(path, music_dir):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ValueError('volume disappeared')
            return validator(path, music_dir)

        with patch.object(self.service, 'volume_validator', side_effect=disappear_after_worker_starts):
            job_id = self.service.start(plan)
            for _ in range(1000):
                status = self.service.status(job_id)
                if status['status'] not in ('starting', 'copying', 'writing'):
                    break
                import time
                time.sleep(.005)

        self.assertEqual(status['status'], 'failed', status)
        self.assertIn('no longer available', status['error'])
        self.assertEqual(status['copied'], 0)
        self.assertEqual(calls, 2)
        output = pathlib.Path(status['path'])
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.rglob('*.mp3')) + list(output.rglob('*.wav')) + list(output.rglob('*.flac')), [])

    def test_production_volume_validator_rejects_unmounted_directory_and_symlink(self):
        mount_root = self.root / 'mounts'
        drive = mount_root / 'drive'
        drive.mkdir(parents=True)
        with patch('usb_export._system_mount_points', return_value=set()), \
             patch('usb_export.os.path.ismount', return_value=False):
            with self.assertRaisesRegex(ValueError, 'mounted external volume'):
                usb_export.resolve_volume(str(drive), str(self.music), str(mount_root))
            alias = mount_root / 'alias'
            alias.symlink_to(drive)
            with self.assertRaisesRegex(ValueError, 'not a real volume'):
                usb_export.resolve_volume(str(alias), str(self.music), str(mount_root))

    def test_sd_card_mounts_are_discovered_and_accepted(self):
        mount_root = self.root / 'mounts'
        canonical_mount_root = self.root / 'canonical-mounts'
        canonical_mount_root.mkdir()
        mount_root.symlink_to(canonical_mount_root)
        card = mount_root / 'DJ SD Card'
        canonical_card_path = canonical_mount_root / card.name
        canonical_card_path.mkdir()
        sd_music = self.root / 'sd-card-library'
        sd_music.mkdir()
        mount_line = f"/dev/disk4s1 on {card} (exfat, local, nodev, nosuid, read-write)"
        mount_points = usb_export._parse_mount_table(mount_line)
        self.assertEqual(mount_points, {os.path.normpath(str(card))})
        canonical_card = os.path.realpath(canonical_card_path)
        with patch('usb_export._system_mount_points', return_value={canonical_card}), \
             patch('usb_export.os.path.ismount', return_value=False), \
             patch('usb_export.os.access', return_value=True):
            accepted, root = usb_export.resolve_volume(str(card), str(sd_music), str(mount_root))
            self.assertEqual(accepted, str(card))
            self.assertEqual(root, str(card))
            exporter = usb_export.USBExporter(
                app.get_db, lambda: str(sd_music), app.crate_song_ids,
                app.evaluate_smart_crate, app.build_rekordbox_xml, app.to_camelot,
                volumes_path=str(mount_root))
            self.assertIn({'name': 'DJ SD Card', 'path': os.path.abspath(str(card))},
                          exporter.available_volumes())

    def test_mount_table_unescapes_spaces_and_accepts_apfs_mount_format(self):
        mounts = usb_export._parse_mount_table(
            '/dev/disk7s1 on /Volumes/DJ\\040Card type exfat (local, nodev, nosuid)\n'
            '/dev/disk2s5 on /System/Volumes/Data (apfs, local, read-only)')
        self.assertIn('/Volumes/DJ Card', mounts)
        self.assertIn('/System/Volumes/Data', mounts)


class AutoSpotifyGenreContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.music = self.root / 'music'
        self.music.mkdir()
        self.db_path = self.root / 'library.sqlite'
        self.music_patch = patch.object(app, 'MUSIC_DIR', str(self.music))
        self.db_patch = patch.object(app, 'DB_PATH', str(self.db_path))
        self.music_patch.start(); self.db_patch.start()
        self.addCleanup(self.music_patch.stop); self.addCleanup(self.db_patch.stop)
        app.init_db()
        self.audio = self.music / 'New Song.mp3'
        self.audio.write_bytes(b'test audio')
        with app.get_db() as db:
            db.execute('INSERT INTO songs(filename,filepath,title,artist,duration_seconds) VALUES(?,?,?,?,?)',
                       (self.audio.name, str(self.audio), 'New Song', 'DJ Artist', 180))
            db.commit()
            self.song_id = db.execute('SELECT id FROM songs WHERE filepath=?', (str(self.audio),)).fetchone()['id']

    def test_genre_only_mode_fills_missing_genre_without_applying_other_tags_or_deciding_track(self):
        config_patch = patch.multiple(app, AUTO_CLEANUP_METADATA=False, AUTO_SPOTIFY_GENRE=True)
        config_patch.start(); self.addCleanup(config_patch.stop)
        match_patch = patch.object(app, '_spotify_search_track', return_value={
            'confidence': 'high', 'artist_id': 'artist-1', 'title': 'Corrected Title',
            'artist': 'Corrected Artist', 'album': 'Corrected Album', 'year': '2025',
            'spotify_id': 'spotify-1'})
        genre_patch = patch.object(app, 'resolve_genre', return_value=('House', 'Discogs'))
        write_patch = patch.object(app, 'write_metadata')
        export_patch = patch.object(app, 'queue_auto_export')
        match_mock = match_patch.start(); genre_patch.start()
        write_mock = write_patch.start(); export_mock = export_patch.start()
        self.addCleanup(match_patch.stop); self.addCleanup(genre_patch.stop)
        self.addCleanup(write_patch.stop); self.addCleanup(export_patch.stop)

        app.auto_cleanup_download(self.song_id, cleanup_download=False)

        with app.get_db() as db:
            song = db.execute('SELECT * FROM songs WHERE id=?', (self.song_id,)).fetchone()
        self.assertEqual(song['genre'], 'House')
        self.assertEqual(song['title'], 'New Song')
        self.assertEqual(song['artist'], 'DJ Artist')
        self.assertIsNone(song['cleanup_scanned_at'])
        write_mock.assert_called_once_with(str(self.audio), genre='House')
        export_mock.assert_called_once_with()
        self.assertEqual(match_mock.call_count, 1)

    def test_existing_auto_cleanup_switch_still_applies_full_high_confidence_match_on_download(self):
        config_patch = patch.multiple(app, AUTO_CLEANUP_METADATA=True, AUTO_SPOTIFY_GENRE=False)
        config_patch.start(); self.addCleanup(config_patch.stop)
        match_patch = patch.object(app, '_spotify_search_track', return_value={
            'confidence': 'high', 'artist_id': 'artist-1', 'title': 'Corrected Title',
            'artist': 'Corrected Artist', 'album': 'Corrected Album', 'year': '2025',
            'spotify_id': 'spotify-1'})
        genre_patch = patch.object(app, 'resolve_genre', return_value=('House', 'Discogs'))
        fix_patch = patch.object(app, '_write_cleanup_fix', return_value=True)
        match_patch.start(); genre_patch.start(); fix_mock = fix_patch.start()
        self.addCleanup(match_patch.stop); self.addCleanup(genre_patch.stop); self.addCleanup(fix_patch.stop)

        app.auto_cleanup_download(self.song_id, cleanup_download=True)

        fix_mock.assert_called_once_with(self.song_id,
            {'title': 'Corrected Title', 'artist': 'Corrected Artist',
             'album': 'Corrected Album', 'year': '2025', 'genre': 'House'}, 'spotify-1')

    def test_spotify_genre_never_replaces_existing_or_user_tags(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='Breaks; #tool' WHERE id=?", (self.song_id,))
            db.commit()
        config_patch = patch.multiple(app, AUTO_CLEANUP_METADATA=False, AUTO_SPOTIFY_GENRE=True)
        config_patch.start(); self.addCleanup(config_patch.stop)
        search_patch = patch.object(app, '_spotify_search_track')
        write_patch = patch.object(app, 'write_metadata')
        search_mock = search_patch.start(); write_mock = write_patch.start()
        self.addCleanup(search_patch.stop); self.addCleanup(write_patch.stop)

        app.auto_cleanup_download(self.song_id, cleanup_download=False)

        search_mock.assert_not_called()
        write_mock.assert_not_called()
        with app.get_db() as db:
            self.assertEqual(db.execute('SELECT genre FROM songs WHERE id=?', (self.song_id,)).fetchone()['genre'],
                             'Breaks; #tool')

    def test_low_confidence_match_is_not_used_for_genre_fill(self):
        config_patch = patch.multiple(app, AUTO_CLEANUP_METADATA=False, AUTO_SPOTIFY_GENRE=True)
        config_patch.start(); self.addCleanup(config_patch.stop)
        search_patch = patch.object(app, '_spotify_search_track', return_value={
            'confidence': 'medium', 'artist_id': 'artist-1'})
        genre_patch = patch.object(app, 'resolve_genre')
        search_patch.start(); genre_mock = genre_patch.start()
        self.addCleanup(search_patch.stop); self.addCleanup(genre_patch.stop)

        app.auto_cleanup_download(self.song_id, cleanup_download=False)

        genre_mock.assert_not_called()
        with app.get_db() as db:
            self.assertIsNone(db.execute('SELECT genre FROM songs WHERE id=?', (self.song_id,)).fetchone()['genre'])

    def test_new_song_workflows_schedule_spotify_automation_when_enabled(self):
        config_patch = patch.multiple(app, AUTO_CLEANUP_METADATA=False, AUTO_SPOTIFY_GENRE=True)
        config_patch.start(); self.addCleanup(config_patch.stop)
        with patch.object(app.threading, 'Thread') as thread:
            app.schedule_auto_cleanup(self.song_id)
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs['target'], app.auto_cleanup_download)
        self.assertEqual(thread.call_args.kwargs['args'], [self.song_id, False])
        with patch.multiple(app, AUTO_CLEANUP_METADATA=True, AUTO_SPOTIFY_GENRE=False):
            with patch.object(app.threading, 'Thread') as genre_only_thread:
                app.schedule_auto_cleanup(self.song_id)
            genre_only_thread.assert_not_called()
            with patch.object(app.threading, 'Thread') as cleanup_thread:
                app.schedule_auto_cleanup(self.song_id, cleanup_download=True)
            cleanup_thread.assert_called_once()
            self.assertEqual(cleanup_thread.call_args.kwargs['args'], [self.song_id, True])

    def test_spotify_genre_setting_is_saved_and_returned_by_config_api(self):
        config_patch = patch.multiple(app, _config={}, AUTO_SPOTIFY_GENRE=False)
        config_patch.start(); self.addCleanup(config_patch.stop)
        save_patch = patch.object(app, '_save_config',
            side_effect=lambda updates: setattr(app, 'AUTO_SPOTIFY_GENRE', bool(updates['auto_spotify_genre'])))
        save_mock = save_patch.start(); self.addCleanup(save_patch.stop)
        response = app.app.test_client().post('/api/config', json={'auto_spotify_genre': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.json['auto_spotify_genre'])
        self.assertTrue(app.AUTO_SPOTIFY_GENRE)
        save_mock.assert_called_once_with({'auto_spotify_genre': True})

    def test_apply_genre_endpoint_fills_a_missing_genre_without_recording_a_decision(self):
        """The reviewed genre fill writes only the genre and leaves the track
        undecided, so a later full metadata cleanup still sees it."""
        write_patch = patch.object(app, 'write_metadata')
        export_patch = patch.object(app, 'queue_auto_export')
        write_mock = write_patch.start(); export_patch.start()
        self.addCleanup(write_patch.stop); self.addCleanup(export_patch.stop)

        response = app.app.test_client().post(
            f'/api/cleanup/apply-genre/{self.song_id}', json={'genre': 'House'})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.json['applied'])
        with app.get_db() as db:
            song = db.execute('SELECT * FROM songs WHERE id=?', (self.song_id,)).fetchone()
        self.assertEqual(song['genre'], 'House')
        self.assertIsNone(song['cleanup_scanned_at'])
        self.assertIsNone(song['cleanup_decision'])
        write_mock.assert_called_once_with(str(self.audio), genre='House')

    def test_apply_genre_endpoint_never_replaces_an_existing_genre(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='Breaks; #tool' WHERE id=?", (self.song_id,))
            db.commit()
        write_patch = patch.object(app, 'write_metadata')
        write_mock = write_patch.start(); self.addCleanup(write_patch.stop)

        response = app.app.test_client().post(
            f'/api/cleanup/apply-genre/{self.song_id}', json={'genre': 'House'})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(response.json['applied'])
        write_mock.assert_not_called()
        with app.get_db() as db:
            self.assertEqual(db.execute('SELECT genre FROM songs WHERE id=?',
                                        (self.song_id,)).fetchone()['genre'], 'Breaks; #tool')

    def test_apply_genre_endpoint_rejects_a_blank_genre(self):
        response = app.app.test_client().post(
            f'/api/cleanup/apply-genre/{self.song_id}', json={'genre': '   '})

        self.assertEqual(response.status_code, 400)
        with app.get_db() as db:
            self.assertIsNone(db.execute('SELECT genre FROM songs WHERE id=?',
                                         (self.song_id,)).fetchone()['genre'])

    def test_apply_genre_endpoint_404s_for_an_unknown_song(self):
        response = app.app.test_client().post(
            '/api/cleanup/apply-genre/99999', json={'genre': 'House'})

        self.assertEqual(response.status_code, 404)


class GenreSourceContracts(unittest.TestCase):
    """resolve_genre's source order plus the Discogs / MusicBrainz parsers.

    Every HTTP call is stubbed, so these tests never touch the network."""

    def setUp(self):
        # Discogs lookups are paced under its rate limit; clear the timer so the
        # stubbed tests below never actually sleep.
        app._discogs_last_call[0] = 0.0

    @staticmethod
    def _response(payload=None, status=200):
        return type('FakeResponse', (), {
            'status_code': status,
            'json': lambda self: payload})()

    def test_discogs_lookups_are_paced_under_the_rate_limit(self):
        """A whole-library fill must not trip Discogs' rate limit."""
        with patch.object(app, 'DISCOGS_MIN_INTERVAL', 2.4), \
             patch.object(app.requests, 'get', return_value=self._response({})), \
             patch.object(app.time, 'sleep') as sleep:
            app._discogs_last_call[0] = app.time.time()   # as if just called
            app._discogs_request({'q': 'x'})
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 2.4, places=1)

    def test_resolve_genre_queries_both_databases_and_returns_an_agreement(self):
        """When Discogs and MusicBrainz agree, the shared genre wins and both
        are credited; Spotify is never touched."""
        with patch.object(app, '_discogs_genre', return_value='Drum & Bass') as dg, \
             patch.object(app, '_musicbrainz_genre', return_value='drum and bass') as mb, \
             patch.object(app, '_spotify_artist_genre', return_value='Edm') as sp:
            self.assertEqual(app.resolve_genre('T', 'A', 'aid'),
                             ('Drum & Bass', 'Discogs + MusicBrainz'))
            dg.assert_called_once()
            mb.assert_called_once()
            sp.assert_not_called()

    def test_resolve_genre_prefers_the_more_specific_dj_genre_on_disagreement(self):
        """Both databases are consulted and the more specific DJ genre wins --
        not simply whichever source answered first."""
        with patch.object(app, '_discogs_genre', return_value='House'), \
             patch.object(app, '_musicbrainz_genre', return_value='Progressive House') as mb:
            self.assertEqual(app.resolve_genre('T', 'A', 'aid'),
                             ('Progressive House', 'MusicBrainz'))
            mb.assert_called_once()

    def test_resolve_genre_breaks_an_equal_score_toward_discogs(self):
        """Two equally DJ-recognisable genres ('Techno' vs 'House') fall back to
        the release-level source, which is attached per track."""
        with patch.object(app, '_discogs_genre', return_value='Techno'), \
             patch.object(app, '_musicbrainz_genre', return_value='House'):
            self.assertEqual(app.resolve_genre('T', 'A', 'aid'), ('Techno', 'Discogs'))

    def test_resolve_genre_falls_back_to_musicbrainz_alone(self):
        with patch.object(app, '_discogs_genre', return_value=None), \
             patch.object(app, '_musicbrainz_genre', return_value='Deep House'), \
             patch.object(app, '_spotify_artist_genre', return_value='Edm') as sp:
            self.assertEqual(app.resolve_genre('T', 'A', 'aid'), ('Deep House', 'MusicBrainz'))
            sp.assert_not_called()

    def test_resolve_genre_uses_spotify_only_when_both_databases_are_empty(self):
        with patch.object(app, '_discogs_genre', return_value=None), \
             patch.object(app, '_musicbrainz_genre', return_value=None), \
             patch.object(app, '_spotify_artist_genre', return_value='House') as sp:
            self.assertEqual(app.resolve_genre('T', 'A', 'aid'), ('House', 'Spotify'))
            sp.assert_called_once_with('aid')

    def test_resolve_genre_skips_a_source_that_raises(self):
        """One flaky API must never block the fill."""
        with patch.object(app, '_discogs_genre', side_effect=RuntimeError('boom')), \
             patch.object(app, '_musicbrainz_genre', return_value='Trance'), \
             patch.object(app, '_spotify_artist_genre', return_value='Edm') as sp:
            self.assertEqual(app.resolve_genre('T', 'A'), ('Trance', 'MusicBrainz'))
            sp.assert_not_called()

    def test_resolve_genre_returns_nothing_when_every_source_is_empty(self):
        with patch.object(app, '_discogs_genre', return_value=None), \
             patch.object(app, '_musicbrainz_genre', return_value=None), \
             patch.object(app, '_spotify_artist_genre', return_value=None):
            self.assertEqual(app.resolve_genre('T', 'A'), (None, None))

    def test_discogs_genre_prefers_a_release_credited_to_the_searched_artist(self):
        """A same-titled release by another act must not win over the artist's own."""
        payload = {'results': [
            {'title': 'Someone Else - Levels', 'style': ['Techno']},
            {'title': 'Avicii - Levels', 'style': ['Electronic', 'Progressive House']}]}
        with patch.object(app.requests, 'get', return_value=self._response(payload)) as get:
            self.assertEqual(app._discogs_genre('Levels - Radio Edit', 'Avicii'),
                             'Progressive House')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['type'], 'release')
        self.assertIn('Levels', params['q'])

    def test_discogs_genre_ignores_releases_with_a_different_track_title(self):
        payload = {'results': [{'title': 'Avicii - Wake Me Up', 'style': ['House']}]}
        with patch.object(app.requests, 'get', return_value=self._response(payload)):
            self.assertIsNone(app._discogs_genre('Levels', 'Avicii'))

    def test_discogs_genre_skips_the_generic_electronic_umbrella_style(self):
        """'Electronic' alone says nothing -- better to try the next source."""
        payload = {'results': [{'title': 'Avicii - Levels', 'style': ['Electronic']}]}
        with patch.object(app.requests, 'get', return_value=self._response(payload)):
            self.assertIsNone(app._discogs_genre('Levels', 'Avicii'))

    def test_musicbrainz_genre_reads_the_matching_artists_genres(self):
        recordings = {'recordings': [{
            'title': 'Levels',
            'artist-credit': [{'artist': {'id': 'mb-artist-1', 'name': 'Avicii'}}]}]}
        artist = {'genres': [{'name': 'Electronic'}, {'name': 'progressive house'}]}
        with patch.object(app, '_musicbrainz_get', side_effect=[recordings, artist]) as mb:
            self.assertEqual(app._musicbrainz_genre('Levels - Radio Edit', 'Avicii'),
                             'Progressive House')
        self.assertEqual(mb.call_args_list[1].args[0], 'artist/mb-artist-1')

    def test_musicbrainz_genre_rejects_a_recording_credited_to_another_artist(self):
        recordings = {'recordings': [{
            'title': 'Will You Ever Just',
            'artist-credit': [{'artist': {'id': 'mb-other', 'name': 'Some Rock Band'}}]}]}
        with patch.object(app, '_musicbrainz_get', return_value=recordings) as mb:
            self.assertIsNone(app._musicbrainz_genre('Will You Ever Just?', 'Kompa55'))
        self.assertEqual(mb.call_count, 1)   # wrong artist's genres never fetched

    def test_pick_dj_genre_prefers_a_real_style_over_an_umbrella_term(self):
        self.assertEqual(app._pick_dj_genre(['Electronic', 'Tech House']), 'Tech House')
        self.assertIsNone(app._pick_dj_genre(['Electronic']))

    def test_pick_dj_genre_falls_back_to_a_catch_all_only_when_alone(self):
        self.assertEqual(app._pick_dj_genre(['edm', 'house']), 'House')
        self.assertEqual(app._pick_dj_genre(['edm']), 'Edm')

    def test_genre_key_normalises_ampersands_and_punctuation(self):
        self.assertEqual(app._genre_key('Drum & Bass'), app._genre_key('drum and bass'))
        self.assertEqual(app._genre_key('Tech-House'), 'tech house')
        self.assertEqual(app._genre_key(None), '')

    def test_genre_rank_prefers_specific_dj_genres_over_umbrellas(self):
        # Both read as DJ genres, but the subtype is the more useful filter.
        self.assertGreater(app._genre_rank('Progressive House'), app._genre_rank('House'))
        self.assertGreater(app._genre_rank('Melodic Techno'), app._genre_rank('Edm'))
        self.assertLess(app._genre_rank('Edm'), app._genre_rank('Techno'))
        self.assertLess(app._genre_rank(''), app._genre_rank('Techno'))

    def test_combine_genres_returns_nothing_without_a_proposal(self):
        self.assertEqual(app._combine_genres(('Discogs', None), ('MusicBrainz', '')),
                         (None, None))

    def test_combine_genres_credits_both_sources_when_they_agree(self):
        self.assertEqual(app._combine_genres(('Discogs', 'House'),
                                             ('MusicBrainz', 'house')),
                         ('House', 'Discogs + MusicBrainz'))

    def test_combine_genres_picks_the_highest_scoring_genre_on_disagreement(self):
        self.assertEqual(app._combine_genres(('Discogs', 'House'),
                                             ('MusicBrainz', 'Progressive House')),
                         ('Progressive House', 'MusicBrainz'))
        # Equal scores keep the earlier, more precise (release-level) source.
        self.assertEqual(app._combine_genres(('Discogs', 'Techno'),
                                             ('MusicBrainz', 'House')),
                         ('Techno', 'Discogs'))

    def test_artist_keys_strip_youtube_channel_suffixes(self):
        self.assertIn('the temper trap', app._artist_keys('The Temper Trap - Topic'))
        self.assertEqual(app._artist_keys('Nasso, Mez (EG)'), ['nasso', 'mez'])


class GenreBackfillContracts(unittest.TestCase):
    """The resumable whole-library genre backfill: worker, resume and routes.

    resolve_genre is stubbed, so no test touches the network. The worker is
    started through start_genre_backfill / resume_genre_backfill and then joined.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.music = self.root / 'music'
        self.music.mkdir()
        self.db_path = self.root / 'library.sqlite'
        self.music_patch = patch.object(app, 'MUSIC_DIR', str(self.music))
        self.db_patch = patch.object(app, 'DB_PATH', str(self.db_path))
        self.music_patch.start(); self.db_patch.start()
        self.addCleanup(self.music_patch.stop); self.addCleanup(self.db_patch.stop)
        app.init_db()
        # Never let one test leave a worker running against a torn-down DB.
        self.addCleanup(self._settle_backfill)
        app._backfill_cancel.clear()
        app._backfill_thread[0] = None
        for sid in (1, 2, 3, 4):
            path = self.music / f'Song {sid}.mp3'
            path.write_bytes(b'audio')
            with app.get_db() as db:
                db.execute('INSERT INTO songs(id,filename,filepath,title,artist) VALUES(?,?,?,?,?)',
                           (sid, path.name, str(path), f'Song {sid}', 'Artist'))
                db.commit()
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='House' WHERE id=4")
            db.commit()

    def _settle_backfill(self):
        app._backfill_cancel.set()
        thread = app._backfill_thread[0]
        if thread:
            thread.join(5)
        app._backfill_thread[0] = None
        app._backfill_cancel.clear()

    def _wait_backfill(self, timeout=5):
        thread = app._backfill_thread[0]
        if thread:
            thread.join(timeout)
        self.assertFalse(thread and thread.is_alive(), 'backfill worker did not finish')

    def _genres(self):
        with app.get_db() as db:
            return {r['id']: (r['genre'], r['genre_backfill_at'])
                    for r in db.execute('SELECT id, genre, genre_backfill_at FROM songs')}

    def test_backfill_fills_genreless_tracks_and_marks_each_attempted(self):
        with patch.object(app, 'resolve_genre', return_value=('Tech House', 'Discogs')) as resolve, \
             patch.object(app, 'write_metadata') as write, \
             patch.object(app, 'queue_auto_export'):
            app.start_genre_backfill()
            self._wait_backfill()
        self.assertEqual(resolve.call_count, 3)          # the track with a genre is skipped
        self.assertEqual(write.call_count, 3)
        write.assert_any_call(str(self.music / 'Song 1.mp3'), genre='Tech House')
        genres = self._genres()
        for sid in (1, 2, 3):
            self.assertEqual(genres[sid][0], 'Tech House')
            self.assertIsNotNone(genres[sid][1])         # stamped -> not looked up again
        self.assertEqual(genres[4], ('House', None))     # untouched, not even stamped
        state = app._backfill_state()
        self.assertEqual(state['status'], 'done')
        self.assertEqual((state['total'], state['attempted'], state['filled']), (3, 3, 3))

    def test_resume_continues_from_the_unattempted_tracks_only(self):
        # As if a previous run stopped after the first track.
        with app.get_db() as db:
            db.execute('UPDATE songs SET genre_backfill_at=CURRENT_TIMESTAMP WHERE id=1')
            db.commit()
        app._backfill_update(status='cancelled', attempted=1, filled=0, total=3)
        with patch.object(app, 'resolve_genre', return_value=('House', 'Discogs')) as resolve, \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            app.start_genre_backfill()
            self._wait_backfill()
        self.assertEqual(resolve.call_count, 2)          # songs 2 and 3 only
        genres = self._genres()
        self.assertIsNone(genres[1][0])                  # never re-looked-up
        self.assertIsNotNone(genres[1][1])               # and stays marked
        self.assertEqual(genres[2][0], 'House')
        self.assertEqual(genres[3][0], 'House')
        state = app._backfill_state()
        self.assertEqual(state['status'], 'done')
        self.assertEqual(state['attempted'], 3)          # 1 from before + 2 now
        self.assertEqual(state['total'], 3)

    def test_cancel_stops_after_the_track_in_flight_and_stays_resumable(self):
        def stop_and_fill(title, artist, artist_id=None):
            app._backfill_cancel.set()   # the user pressed Cancel mid-lookup
            return 'House', 'Discogs'
        with patch.object(app, 'resolve_genre', side_effect=stop_and_fill), \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            app.start_genre_backfill()
            self._wait_backfill()
        state = app._backfill_state()
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['attempted'], 1)
        genres = self._genres()
        self.assertEqual(genres[1][0], 'House')          # the in-flight track finished
        self.assertIsNone(genres[2][1])                  # the rest were never reached
        self.assertIsNone(genres[3][1])
        self.assertEqual(app._backfill_remaining(), 2)
        # Starting again picks up exactly the two that are left.
        with patch.object(app, 'resolve_genre', return_value=('Trance', 'MusicBrainz')) as resolve, \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            self.assertEqual(app.start_genre_backfill(), 'running')
            self._wait_backfill()
        self.assertEqual(resolve.call_count, 2)
        genres = self._genres()
        self.assertEqual(genres[2][0], 'Trance')
        self.assertEqual(genres[3][0], 'Trance')
        self.assertEqual(app._backfill_state()['status'], 'done')

    def test_start_while_a_run_is_in_flight_does_not_spawn_a_second_worker(self):
        gate = threading.Event()

        def blocked(title, artist, artist_id=None):
            gate.wait(5)
            return 'House', 'Discogs'
        with patch.object(app, 'resolve_genre', side_effect=blocked), \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            self.assertEqual(app.start_genre_backfill(), 'running')
            first = app._backfill_thread[0]
            self.assertEqual(app.start_genre_backfill(), 'running')
            self.assertIs(app._backfill_thread[0], first)
            gate.set()
            self._wait_backfill()

    def test_resume_restarts_a_job_left_running_by_a_previous_process(self):
        app._backfill_update(status='running', attempted=0, filled=0, total=3)
        app._backfill_thread[0] = None
        with patch.object(app, 'resolve_genre', return_value=('House', 'Discogs')) as resolve, \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            app.resume_genre_backfill()
            self._wait_backfill()
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(app._backfill_state()['status'], 'done')

    def test_resume_does_nothing_when_the_last_job_finished(self):
        app._backfill_update(status='done', attempted=3, filled=3, total=3)
        with app.get_db() as db:
            db.execute('UPDATE songs SET genre_backfill_at=CURRENT_TIMESTAMP')
            db.commit()
        with patch.object(app, 'resolve_genre') as resolve:
            app.resume_genre_backfill()
        resolve.assert_not_called()
        self.assertIsNone(app._backfill_thread[0])

    def test_start_reports_done_when_every_track_already_has_a_genre(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='House'")
            db.commit()
        with patch.object(app, 'resolve_genre') as resolve:
            self.assertEqual(app.start_genre_backfill(), 'done')
        resolve.assert_not_called()

    def test_a_worker_crash_is_recorded_as_an_error_not_a_silent_stop(self):
        with patch.object(app, '_backfill_next_song', side_effect=RuntimeError('db gone')), \
             patch.object(app, 'resolve_genre'):
            app.start_genre_backfill()
            self._wait_backfill()
        state = app._backfill_state()
        self.assertEqual(state['status'], 'error')
        self.assertIn('db gone', state['error'])

    def test_auto_sweep_starts_when_enabled_and_tracks_are_outstanding(self):
        with patch.object(app, 'AUTO_GENRE_BACKFILL', True), \
             patch.object(app, 'resolve_genre', return_value=('House', 'Discogs')) as resolve, \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            app.maybe_auto_genre_backfill('scan')
            self._wait_backfill()
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(app._backfill_state()['status'], 'done')

    def test_auto_sweep_is_a_no_op_when_the_setting_is_off(self):
        with patch.object(app, 'AUTO_GENRE_BACKFILL', False), \
             patch.object(app, 'resolve_genre') as resolve:
            app.maybe_auto_genre_backfill('scan')
        resolve.assert_not_called()
        self.assertIsNone(app._backfill_thread[0])

    def test_auto_sweep_is_a_no_op_when_nothing_is_outstanding(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='House'")
            db.commit()
        with patch.object(app, 'AUTO_GENRE_BACKFILL', True), \
             patch.object(app, 'resolve_genre') as resolve:
            app.maybe_auto_genre_backfill('scan')
        resolve.assert_not_called()
        self.assertIsNone(app._backfill_thread[0])

    def test_auto_sweep_leaves_an_already_running_worker_alone(self):
        gate = threading.Event()

        def blocked(title, artist, artist_id=None):
            gate.wait(5)
            return 'House', 'Discogs'
        with patch.object(app, 'AUTO_GENRE_BACKFILL', True), \
             patch.object(app, 'resolve_genre', side_effect=blocked), \
             patch.object(app, 'write_metadata'), patch.object(app, 'queue_auto_export'):
            app.start_genre_backfill()
            first = app._backfill_thread[0]
            app.maybe_auto_genre_backfill('new file')
            self.assertIs(app._backfill_thread[0], first)
            gate.set()
            self._wait_backfill()

    def test_a_library_scan_triggers_the_auto_sweep(self):
        with patch.object(app, 'add_file_to_db', return_value=False), \
             patch.object(app, 'queue_auto_export'), \
             patch.object(app, 'maybe_auto_genre_backfill') as auto:
            app.scan_library()
        auto.assert_called_once_with('scan')

    def test_status_route_reports_progress_and_pacing(self):
        with app.app.test_client() as client:
            data = client.get('/api/genre-backfill').get_json()
        self.assertEqual(data['status'], 'idle')
        self.assertEqual(data['remaining'], 3)
        self.assertEqual(data['attempted'], 0)
        self.assertIn('discogs_token', data)
        self.assertGreater(data['requests_per_minute'], 0)
        self.assertGreater(data['eta_seconds'], 0)      # every track costs a paced lookup


if __name__ == '__main__':
    unittest.main()
