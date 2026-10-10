import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import app
from mutagen.id3 import ID3


def write_test_mp3(path, title=None, artist=None, genre=None):
    """A real-enough MP3 (genuine MPEG frames plus an ID3 tag) so the file side
    of tagging -- TCON and the [DJ Library] comment -- is actually exercised
    rather than mocked away."""
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b'\x00' * 413
    path.write_bytes(frame * 8)
    ID3().save(str(path))
    if title or artist or genre:
        assert app.write_metadata(str(path), title=title, artist=artist, genre=genre)
    return path


def id3_tags(path):
    """{frame id: text} for a file's ID3 tag, so a test can assert on what is
    really on disk."""
    return {str(k): str(v) for k, v in ID3(str(path)).items()}


class TagTestBase(unittest.TestCase):
    """A scratch library: temp DB, temp music folder, three real MP3s and the
    export request mocked so a test can never touch the running app's files."""

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
        # The on-disk rekordbox.xml belongs to the running app: never let a test
        # export into it. Assertions can still count the refresh requests.
        self.export_patch = patch.object(app, 'queue_auto_export')
        self.export_mock = self.export_patch.start()
        self.addCleanup(self.export_patch.stop)
        app.init_db()
        self.files = {}
        for sid in (1, 2, 3):
            path = write_test_mp3(self.music / f'Song {sid}.mp3',
                                  f'Song {sid}', 'Artist', 'Deep House')
            self.files[sid] = path
            with app.get_db() as db:
                db.execute('INSERT INTO songs(id,filename,filepath,title,artist,genre) '
                           'VALUES(?,?,?,?,?,?)',
                           (sid, path.name, str(path), f'Song {sid}', 'Artist', 'Deep House'))
                db.commit()

    def tags_table(self):
        with app.get_db() as db:
            return [(r['name'], r['count']) for r in db.execute(
                '''SELECT t.name, COUNT(st.song_id) AS count FROM tags t
                   LEFT JOIN song_tags st ON st.tag_id=t.id
                   GROUP BY t.id ORDER BY lower(t.name)''').fetchall()]

    def genre(self, song_id):
        with app.get_db() as db:
            return db.execute('SELECT genre FROM songs WHERE id=?', (song_id,)).fetchone()['genre']


class TagStoreContracts(TagTestBase):
    """The tag tables: identity, upsert, replace and orphan cleanup."""

    def test_an_existing_database_opens_with_the_tag_tables_and_init_is_idempotent(self):
        with app.get_db() as db:
            names = {r['name'] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')").fetchall()}
        self.assertIn('tags', names)
        self.assertIn('song_tags', names)
        self.assertIn('idx_tags_name', names)
        app.init_db()            # a second launch must not disturb anything
        self.assertEqual(self.tags_table(), [])

    def test_clean_tag_strips_the_marker_and_rejects_noise(self):
        self.assertEqual(app.clean_tag('  #peak hour '), 'peak hour')
        self.assertEqual(app.clean_tag('##tool'), 'tool')
        self.assertEqual(app.clean_tag('  peak   hour '), 'peak hour')
        self.assertIsNone(app.clean_tag('#'))
        self.assertIsNone(app.clean_tag('   '))
        self.assertIsNone(app.clean_tag('Music'))          # YouTube noise category
        self.assertIsNone(app.clean_tag(None))
        self.assertEqual(len(app.clean_tag('x' * 80)), app.TAG_MAX_LENGTH)

    def test_parse_tag_string_keeps_both_documented_forms(self):
        self.assertEqual(app.parse_tag_string('#house #tool'), ['#house', 'tool'])
        self.assertEqual(app.parse_tag_string('house, tool; peak hour'),
                         ['house', 'tool', 'peak hour'])
        self.assertEqual(app.parse_tag_string(''), [])
        self.assertEqual(app.parse_tag_string(None), [])

    def test_one_tag_row_serves_every_song_and_matching_is_case_insensitive(self):
        app.add_song_tags(1, ['Tool'])
        app.add_song_tags(2, ['#tool'])              # same tag via the marker
        self.assertEqual(self.tags_table(), [('Tool', 2)])
        self.assertEqual(app.get_song_tags(1), ['Tool'])

    def test_add_song_tags_reports_only_what_the_song_gained(self):
        self.assertEqual(app.add_song_tags(1, ['tool', 'peak hour']), ['tool', 'peak hour'])
        self.assertEqual(app.add_song_tags(1, ['tool']), [])
        self.assertEqual(app.add_song_tags(1, ['tool', 'wip']), ['wip'])
        self.assertEqual(app.get_song_tags(1), ['peak hour', 'tool', 'wip'])

    def test_set_song_tags_replaces_what_the_editor_shows(self):
        app.set_song_tags(1, ['tool', 'peak hour'])
        added, removed = app.set_song_tags(1, ['wip'])
        self.assertEqual((added, removed), (['wip'], ['peak hour', 'tool']))
        self.assertEqual(app.get_song_tags(1), ['wip'])
        added, removed = app.set_song_tags(1, [])
        self.assertEqual((added, removed), ([], ['wip']))
        self.assertEqual(self.tags_table(), [])      # nothing left -> no tag rows

    def test_removing_the_last_link_drops_the_tag_row(self):
        app.add_song_tags(1, ['tool'])
        app.add_song_tags(2, ['tool'])
        app.remove_song_tags(1, ['tool'])
        self.assertEqual(self.tags_table(), [('tool', 1)])
        app.remove_song_tags(2, ['TOOL'])            # case-insensitive
        self.assertEqual(self.tags_table(), [])
        self.assertEqual(app.remove_song_tags(2, ['tool']), [])

    def test_tags_for_songs_reads_a_whole_page_at_once(self):
        app.add_song_tags(1, ['tool', 'peak hour'])
        app.add_song_tags(3, ['wip'])
        self.assertEqual(app.tags_for_songs([1, 2, 3]),
                         {1: ['peak hour', 'tool'], 2: [], 3: ['wip']})
        self.assertEqual(app.tags_for_songs([]), {})
        self.assertEqual(app.tags_for_songs([999]), {999: []})

    def test_tag_helpers_never_write_to_the_genre_field(self):
        before = self.genre(1)
        app.tag_song(1, ['#tool', 'peak hour'])
        self.assertEqual(self.genre(1), before)
        app.remove_song_tags(1, ['tool'])
        self.assertEqual(self.genre(1), before)

    def test_song_payloads_carry_their_tags(self):
        app.add_song_tags(2, ['tool'])
        client = app.app.test_client()
        songs = {s['id']: s for s in client.get('/api/songs').get_json()}
        self.assertEqual(songs[2]['tags'], ['tool'])
        self.assertEqual(songs[1]['tags'], [])


class FileTagContracts(TagTestBase):
    """Tags in the file: the [DJ Library] comment block, and TCON kept pure."""

    def setUp(self):
        super().setUp()

    def comment_text(self, song_id):
        frames = [v for k, v in id3_tags(self.files[song_id]).items()
                  if k.startswith('COMM')]
        return '\n'.join(frames)

    def test_tag_song_writes_the_comment_section_and_leaves_the_genre_alone(self):
        app.tag_song(1, ['peak hour', 'tool'])
        comment = self.comment_text(1)
        self.assertIn('[DJ Library]', comment)
        self.assertIn('Tags: peak hour, tool', comment)
        self.assertNotIn('#', comment)
        self.assertEqual(app.extract_metadata(str(self.files[1]))[4], 'Deep House')
        self.assertTrue(self.export_mock.called)     # the XML gets refreshed

    def test_tags_read_back_out_of_the_file(self):
        app.tag_song(2, ['wip'])
        self.assertEqual(app._file_tags(str(self.files[2])), ['wip'])
        self.assertEqual(app._file_tags(str(self.files[1])), [])

    def test_importing_a_file_adopts_the_tags_it_carries(self):
        app.tag_song(3, ['road test'])
        with app.get_db() as db:
            db.execute('DELETE FROM song_tags WHERE song_id=3')
            db.execute('DELETE FROM tags')
            db.execute('DELETE FROM songs WHERE id=3')
            db.commit()
        self.assertTrue(app.add_file_to_db(str(self.files[3])))
        with app.get_db() as db:
            song = db.execute('SELECT id FROM songs WHERE filepath=?',
                              (str(self.files[3]),)).fetchone()
        self.assertEqual(app.get_song_tags(song['id']), ['road test'])

    def test_transitions_and_tags_share_one_comment_section(self):
        with app.get_db() as db:
            db.execute("INSERT INTO transitions (from_song_id,to_text,tag) VALUES(1,'Target','warm')")
            db.commit()
        app.tag_song(1, ['tool'])
        comment = self.comment_text(1)
        self.assertIn('Tags: tool', comment)
        self.assertIn('→ Target (warm)', comment)
        self.assertEqual(comment.count('[DJ Library]'), 1)

    def test_separation_moves_marked_tokens_and_is_idempotent(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='Breaks; #tool' WHERE id=1")
            db.commit()
        app.write_metadata(str(self.files[1]), genre='Breaks; #tool')
        self.assertEqual(app.separate_genre_tags(), (1, 1))
        self.assertEqual(self.genre(1), 'Breaks')
        self.assertEqual(app.get_song_tags(1), ['tool'])
        self.assertEqual(app.extract_metadata(str(self.files[1]))[4], 'Breaks')
        self.assertIn('Tags: tool', self.comment_text(1))
        self.assertEqual(app.separate_genre_tags(), (0, 0))   # nothing left to move

    def test_separation_leaves_an_unmarked_library_untouched(self):
        before = [self.genre(i) for i in (1, 2, 3)]
        self.assertEqual(app.separate_genre_tags(), (0, 0))
        self.assertEqual([self.genre(i) for i in (1, 2, 3)], before)
        self.assertEqual(self.tags_table(), [])
        self.assertNotIn('Tags:', '\n'.join(
            v for k, v in id3_tags(self.files[1]).items() if k.startswith('COMM')))


class GenreStaysAGenre(TagTestBase):
    """The genre field must hold genres: in the DB, in TCON and in the XML."""

    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def genre_of(self, song_id):
        with app.get_db() as db:
            return db.execute('SELECT genre FROM songs WHERE id=?', (song_id,)).fetchone()['genre']

    def test_the_genre_fill_writes_a_genre_and_touches_no_tag(self):
        app.add_song_tags(2, ['tool'])
        with app.get_db() as db:
            db.execute('UPDATE songs SET genre=NULL WHERE id=2')
            db.commit()
        self.assertTrue(app._write_spotify_genre_if_missing(2, str(self.files[2]), 'Tech House'))
        self.assertEqual(self.genre_of(2), 'Tech House')
        self.assertEqual(app.get_song_tags(2), ['tool'])     # tags are structural now
        self.assertEqual(app.extract_metadata(str(self.files[2]))[4], 'Tech House')

    def test_the_backfill_creates_no_tags(self):
        app.add_song_tags(1, ['tool'])
        with app.get_db() as db:
            db.execute('UPDATE songs SET genre=NULL WHERE id=2')
            db.commit()
        with patch.object(app, 'resolve_genre', return_value=('Trance', 'MusicBrainz')), \
             patch.object(app, 'write_metadata'):
            app.start_genre_backfill()
            thread = app._backfill_thread[0]
            if thread:
                thread.join(5)
        self.assertEqual(app.get_song_tags(1), ['tool'])
        self.assertEqual(self.get_tag_names(), ['tool'])

    def get_tag_names(self):
        with app.get_db() as db:
            return [r['name'] for r in db.execute('SELECT name FROM tags ORDER BY name').fetchall()]

    def test_patching_a_genre_keeps_hashed_tokens_out_of_it(self):
        response = self.client.patch('/api/songs/1',
                                     json={'genre': 'Melodic House; #peak hour'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.genre_of(1), 'Melodic House')
        self.assertEqual(app.get_song_tags(1), ['peak hour'])
        self.assertEqual(response.json['tags_extracted'], ['peak hour'])
        self.assertEqual(app.extract_metadata(str(self.files[1]))[4], 'Melodic House')

    def test_a_genre_that_is_only_a_tag_becomes_a_tag(self):
        response = self.client.patch('/api/songs/2', json={'genre': '#tool'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(self.genre_of(2))
        self.assertEqual(app.get_song_tags(2), ['tool'])
        self.assertNotIn('TCON', id3_tags(self.files[2]))

    def test_the_editor_s_field_replaces_tags_on_the_selection(self):
        app.add_song_tags(1, ['old'])
        response = self.client.patch('/api/songs/1', json={'tags': '#tool, peak hour'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(app.get_song_tags(1), ['peak hour', 'tool'])
        self.assertEqual(response.json['tags_removed'], ['old'])
        batch = self.client.patch('/api/songs/batch',
                                  json={'ids': [2, 3], 'tags': ['wip']})
        self.assertEqual(batch.status_code, 200, batch.get_json())
        self.assertEqual(app.get_song_tags(2), ['wip'])
        self.assertEqual(app.get_song_tags(3), ['wip'])
        # A batch edit with no tags field leaves tags alone.
        self.client.patch('/api/songs/batch', json={'ids': [2], 'album': 'Album'})
        self.assertEqual(app.get_song_tags(2), ['wip'])

    def test_clearing_the_editor_s_tags_removes_them(self):
        app.add_song_tags(1, ['tool'])
        self.client.patch('/api/songs/1', json={'tags': ''})
        self.assertEqual(app.get_song_tags(1), [])
        self.assertEqual(self.get_tag_names(), [])

    def test_the_rekordbox_export_keeps_genre_pure_and_carries_tags_in_comments(self):
        with app.get_db() as db:
            db.execute("INSERT INTO crate_songs (crate_id,song_id) VALUES(1,1)")
            db.commit()
        app.add_song_tags(1, ['tool'])
        with app.get_db() as db:
            songs = [dict(r) for r in db.execute('SELECT * FROM songs ORDER BY id').fetchall()]
        xml = app.build_rekordbox_xml(songs, [])
        self.assertIn('Genre="Deep House"', xml)
        self.assertIn('Tags: tool', xml)
        self.assertNotIn('#tool', xml)


class TagApiContracts(TagTestBase):
    """Tags, renaming, deleting and the separation review screen."""

    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def test_listing_tags_reports_usage_most_used_first(self):
        app.add_song_tags(1, ['tool'])
        app.add_song_tags(2, ['tool'])
        app.add_song_tags(1, ['peak hour'])
        listing = self.client.get('/api/tags').get_json()
        self.assertEqual([(t['name'], t['count']) for t in listing],
                         [('tool', 2), ('peak hour', 1)])

    def test_renaming_a_tag_moves_every_song_that_carries_it(self):
        app.add_song_tags(1, ['tool'])
        app.add_song_tags(2, ['tool'])
        tag_id = self.client.get('/api/tags').get_json()[0]['id']
        response = self.client.patch(f'/api/tags/{tag_id}', json={'name': '#peak time'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json['name'], 'peak time')
        self.assertFalse(response.json['merged'])
        self.assertEqual(app.tags_for_songs([1, 2]), {1: ['peak time'], 2: ['peak time']})
        self.assertIn('Tags: peak time', '\n'.join(
            v for k, v in id3_tags(self.files[1]).items() if k.startswith('COMM')))

    def test_renaming_onto_an_existing_tag_merges_instead_of_splitting(self):
        app.add_song_tags(1, ['Tool'])
        app.add_song_tags(2, ['wip'])
        tags = {t['name']: t['id'] for t in self.client.get('/api/tags').get_json()}
        response = self.client.patch(f"/api/tags/{tags['wip']}", json={'name': 'tool'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.json['merged'])
        # The existing row's spelling wins, so both songs read as 'Tool'.
        self.assertEqual(app.tags_for_songs([1, 2]), {1: ['Tool'], 2: ['Tool']})
        self.assertEqual(len(self.client.get('/api/tags').get_json()), 1)

    def test_tag_routes_reject_bad_input_and_unknown_ids(self):
        self.assertEqual(self.client.patch('/api/tags/999', json={'name': 'x'}).status_code, 404)
        self.assertEqual(self.client.delete('/api/tags/999').status_code, 404)
        app.add_song_tags(1, ['tool'])
        tag_id = self.client.get('/api/tags').get_json()[0]['id']
        self.assertEqual(self.client.patch(f'/api/tags/{tag_id}', json={'name': '#'}).status_code, 400)
        self.assertEqual(self.client.post('/api/songs/999/tags', json={'add': ['x']}).status_code, 404)
        self.assertEqual(self.client.post('/api/songs/1/tags', json={'add': 'tool'}).status_code, 400)

    def test_deleting_a_tag_removes_it_from_every_song(self):
        app.add_song_tags(1, ['tool'])
        app.add_song_tags(2, ['tool', 'wip'])
        tag_id = {t['name']: t['id'] for t in self.client.get('/api/tags').get_json()}['tool']
        response = self.client.delete(f'/api/tags/{tag_id}')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json['songs'], 2)
        self.assertEqual(app.tags_for_songs([1, 2]), {1: [], 2: ['wip']})

    def test_the_song_tag_route_adds_and_removes_without_touching_genre(self):
        response = self.client.post('/api/songs/1/tags', json={'add': ['#tool', 'peak hour']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json['added'], ['tool', 'peak hour'])
        with app.get_db() as db:
            genre = db.execute('SELECT genre FROM songs WHERE id=1').fetchone()['genre']
        self.assertEqual(genre, 'Deep House')
        removed = self.client.post('/api/songs/1/tags', json={'remove': ['tool']})
        self.assertEqual(removed.json['removed'], ['tool'])
        self.assertEqual(removed.json['tags'], ['peak hour'])

    def test_the_review_screen_lists_marked_and_rare_tokens(self):
        staged = self.music / 'Extra.mp3'
        write_test_mp3(staged, 'Extra', 'Artist')
        with app.get_db() as db:
            db.execute('INSERT INTO songs(id,filename,filepath,title,artist) VALUES(4,?,?,?,?)',
                       (staged.name, str(staged), 'Extra', 'Artist'))
            db.execute("UPDATE songs SET genre='House' WHERE id IN (1,2)")
            db.execute("UPDATE songs SET genre='House; White girl' WHERE id=3")
            db.execute("UPDATE songs SET genre='#road test' WHERE id=4")
            db.commit()
        candidates = {c['token']: c for c in
                      self.client.get('/api/tag-separation').get_json()['candidates']}
        self.assertIn('White girl', candidates)     # rare -> suggested
        self.assertIn('#road test', candidates)     # the marker -> already a tag
        self.assertTrue(candidates['#road test']['marked'])
        self.assertNotIn('House', candidates)       # a real, shared genre
        self.assertFalse(candidates['White girl']['marked'])

    def test_the_review_screen_moves_a_token_either_way(self):
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='House; White girl' WHERE id=3")
            db.commit()
        response = self.client.post('/api/tag-separation',
                                    json={'token': 'White girl', 'action': 'tag'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.json['songs'], 1)
        with app.get_db() as db:
            genre = db.execute('SELECT genre FROM songs WHERE id=3').fetchone()['genre']
        self.assertEqual(genre, 'House')
        self.assertEqual(app.get_song_tags(3), ['White girl'])

        # And back the other way: a marked token becomes a plain genre.
        with app.get_db() as db:
            db.execute("UPDATE songs SET genre='#tool' WHERE id=1")
            db.commit()
        back = self.client.post('/api/tag-separation', json={'token': '#tool', 'action': 'genre'})
        self.assertEqual(back.status_code, 200, back.get_json())
        with app.get_db() as db:
            genre = db.execute('SELECT genre FROM songs WHERE id=1').fetchone()['genre']
        self.assertEqual(genre, 'tool')

    def test_the_review_screen_rejects_bad_input(self):
        self.assertEqual(self.client.post('/api/tag-separation',
                                          json={'token': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/tag-separation',
                                          json={'action': 'tag'}).status_code, 400)
        self.assertEqual(self.client.post('/api/tag-separation',
                                          json={'token': '#', 'action': 'tag'}).status_code, 400)


class SongLifecycleContracts(TagTestBase):
    """Deleting and merging must never leave tag links or orphan tag rows."""

    def setUp(self):
        super().setUp()

    def counts(self):
        with app.get_db() as db:
            links = db.execute('SELECT COUNT(*) FROM song_tags').fetchone()[0]
            tags = db.execute('SELECT COUNT(*) FROM tags').fetchone()[0]
        return links, tags

    def test_deleting_a_song_takes_its_tag_links_and_orphan_tags_with_it(self):
        app.add_song_tags(1, ['tool'])
        app.add_song_tags(2, ['tool', 'wip'])
        staging = self.music / 'Doomed.mp3'
        write_test_mp3(staging, 'Doomed', 'Artist', 'House')
        with app.get_db() as db:
            db.execute('INSERT INTO songs(filename,filepath,title,artist,genre) VALUES(?,?,?,?,?)',
                       (staging.name, str(staging), 'Doomed', 'Artist', 'House'))
            db.commit()
            doomed = db.execute('SELECT id FROM songs WHERE filepath=?',
                                (str(staging),)).fetchone()['id']
        app.add_song_tags(doomed, ['doomed only'])
        self.assertEqual(self.counts(), (4, 3))
        response = app.app.test_client().delete(f'/api/songs/{doomed}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.counts(), (3, 2))      # tool + wip survive, 'doomed only' goes
        with app.get_db() as db:
            orphans = db.execute('SELECT COUNT(*) FROM song_tags WHERE song_id NOT IN '
                                 '(SELECT id FROM songs)').fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_merging_duplicates_moves_the_tags_onto_the_keeper(self):
        twin = self.music / 'Song 2 (1).mp3'
        twin.write_bytes(self.files[2].read_bytes())
        with app.get_db() as db:
            db.execute('INSERT INTO songs(filename,filepath,title,artist,genre,file_size_bytes) '
                       'VALUES(?,?,?,?,?,?)',
                       (twin.name, str(twin), 'Song 2', 'Artist', 'Deep House', twin.stat().st_size))
            db.commit()
            keeper = 1
            loser = db.execute('SELECT id FROM songs WHERE filepath=?', (str(twin),)).fetchone()['id']
        app.add_song_tags(keeper, ['tool'])
        app.add_song_tags(loser, ['wip', 'tool'])
        with app.get_db() as db:
            db.execute('UPDATE songs SET genre=? WHERE id=?', ('Deep House; #road test', keeper))
            db.commit()
        response = app.app.test_client().post('/api/merge-execute', json={
            'keeper_id': keeper, 'loser_ids': [loser],
            'fields': {'genre': 'Deep House; #road test'}})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(app.get_song_tags(keeper), ['road test', 'tool', 'wip'])
        with app.get_db() as db:
            orphans = db.execute('SELECT COUNT(*) FROM song_tags WHERE song_id NOT IN '
                                 '(SELECT id FROM songs)').fetchone()[0]
        self.assertEqual(orphans, 0)
        with app.get_db() as db:
            genre = db.execute('SELECT genre FROM songs WHERE id=?', (keeper,)).fetchone()['genre']
        self.assertEqual(genre, 'Deep House')                    # '#road test' left the genre

    def test_a_download_with_tags_stores_tags_and_leaves_the_genre_for_the_backfill(self):
        # The downloader's contract: the `tags` field carries tags in either
        # documented form (the '#' marker is stripped when they are stored) and
        # the genre field stays empty for the backfill to fill.
        self.assertEqual(app.parse_tag_string('#house #tool'), ['#house', 'tool'])
        self.assertEqual(app.parse_tag_string('house, tool; peak hour'),
                         ['house', 'tool', 'peak hour'])
        staged = self.music / 'Downloaded.mp3'
        write_test_mp3(staged, 'Downloaded', 'Artist')
        with app.get_db() as db:
            db.execute('INSERT INTO songs(filename,filepath,title,artist) VALUES(?,?,?,?)',
                       (staged.name, str(staged), 'Downloaded', 'Artist'))
            db.commit()
            song_id = db.execute('SELECT id FROM songs WHERE filepath=?', (str(staged),)).fetchone()['id']
        added = app.tag_song_at_path(str(staged), app.parse_tag_string('#peak-hour #tool'))
        self.assertEqual(added, ['peak-hour', 'tool'])       # the '#' never reaches a tag name
        with app.get_db() as db:
            genre = db.execute('SELECT genre FROM songs WHERE id=?', (song_id,)).fetchone()['genre']
        self.assertIsNone(genre)


class DownloadTagPayloadContracts(TagTestBase):
    """Every download entry point must forward tags as tags -- including the
    Raycast script's '#tag' array, which used to be parsed as a plain string and
    silently dropped."""

    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()

    def forwarded(self, route, body):
        calls = []
        with patch.object(app, 'do_download', side_effect=lambda *a, **k: calls.append((a, k))):
            response = self.client.post(route, json=body)
        self.assertIn(response.status_code, (200, 202), response.get_json())
        return calls, response

    def test_the_raycast_tag_array_reaches_the_downloader(self):
        calls, _ = self.forwarded('/api/download-list',
                                  {'text': 'Some Song\nAnother Song',
                                   'tags': ['#peak-hour', '#tool']})
        self.assertTrue(calls)
        for args, kwargs in calls:
            self.assertEqual(kwargs.get('tags', args[-1] if args else None),
                             ['peak-hour', 'tool'])

    def test_a_tag_string_payload_still_works(self):
        calls, _ = self.forwarded('/api/download',
                                  {'query': 'Some Song', 'tags': '#groove, #tool'})
        self.assertEqual(len(calls), 1)          # queued, not downloaded
        args, kwargs = calls[0]
        self.assertEqual(kwargs.get('tags', args[-1] if args else None),
                         ['groove', 'tool'])

    def test_every_tags_shape_parses_the_same_way(self):
        self.assertEqual(app._tags_from_payload(['#peak-hour', 'tool']), ['peak-hour', 'tool'])
        self.assertEqual(app._tags_from_payload('#peak-hour, tool'), ['peak-hour', 'tool'])
        self.assertEqual(app._tags_from_payload(''), [])
        self.assertEqual(app._tags_from_payload(None), [])
        self.assertEqual(app._tags_from_payload(['Music', '#']), [])
        self.assertEqual(app._tags_from_payload(['Tool', 'tool']), ['Tool'])


if __name__ == '__main__':
    unittest.main()
