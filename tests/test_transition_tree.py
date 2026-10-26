import pathlib
import tempfile
import unittest
from unittest.mock import patch

import app

app.AUTO_SYNC_MIXXX = False


def add_song(db, sid, title, artist='Artist', bpm=None, key=None, filename=None):
    fn = filename or f'Song {sid}.mp3'
    db.execute('INSERT INTO songs(id,filename,filepath,title,artist,bpm,musical_key) '
               'VALUES(?,?,?,?,?,?,?)',
               (sid, fn, f'/music/{fn}', title, artist, bpm, key))
    db.commit()


def add_hop(db, from_id, to_id=None, to_text=None, notes=None, tag=None,
            status='confirmed', mash=False, mi_id=None, mi_text=None):
    db.execute('''INSERT INTO transitions (from_song_id, to_song_id, to_text,
            notes, tag, status, is_mashup_track, mashup_input_song_id,
            mashup_input_text) VALUES (?,?,?,?,?,?,?,?,?)''',
               (from_id, to_id, to_text, notes, tag, status,
                1 if mash else 0, mi_id, mi_text))
    db.commit()


class TransitionTreeTest(unittest.TestCase):
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
        with app.get_db() as db:
            add_song(db, 1, 'One', bpm=124.0, key='6A')
            add_song(db, 2, 'Two', bpm=125.5, key='8A')
            add_song(db, 3, 'Three', bpm=126.0, key='9A')
            add_song(db, 4, 'Four', bpm=127.0, key='10A')
            # Chain 1 -> 2 -> 3, branch 1 -> 4, cycle 3 -> 1, text leaf 2 -> ?
            add_hop(db, 1, to_id=2, notes='blend on the break', tag='peak')
            add_hop(db, 1, to_id=4, notes='second option')
            add_hop(db, 2, to_id=3, notes='keep it rolling', status='to_try')
            add_hop(db, 2, to_text='Unowned Dub', notes='want this')
            add_hop(db, 3, to_id=1, notes='back around')
        self.client = app.app.test_client()

    def test_tree_has_names_bpm_key_and_notes(self):
        resp = self.client.get('/api/songs/1/transition-tree?depth=3')
        self.assertEqual(resp.status_code, 200)
        tree = resp.get_json()
        self.assertEqual(tree['title'], 'One')
        self.assertEqual(tree['bpm'], 124.0)
        self.assertEqual(tree['musical_key'], '6A')
        self.assertEqual(len(tree['children']), 2)
        first = tree['children'][0]
        self.assertEqual(first['notes'], 'blend on the break')
        self.assertEqual(first['tag'], 'peak')
        two = first['target']
        self.assertEqual(two['title'], 'Two')
        self.assertEqual(two['bpm'], 125.5)
        self.assertEqual(two['musical_key'], '8A')

    def test_tree_follows_multiple_levels_and_marks_to_try(self):
        tree = self.client.get('/api/songs/1/transition-tree?depth=3').get_json()
        two = tree['children'][0]['target']
        hop = next(h for h in two['children'] if h['target'].get('title') == 'Three')
        self.assertEqual(hop['status'], 'to_try')
        self.assertEqual(hop['notes'], 'keep it rolling')

    def test_tree_keeps_text_only_leaves(self):
        tree = self.client.get('/api/songs/1/transition-tree?depth=3').get_json()
        two = tree['children'][0]['target']
        texts = [h for h in two['children'] if h['target'].get('kind') == 'text']
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]['target']['text'], 'Unowned Dub')

    def test_tree_ends_cycles_with_a_revisit_stub(self):
        tree = self.client.get('/api/songs/1/transition-tree?depth=5').get_json()
        two = tree['children'][0]['target']
        three = next(h['target'] for h in two['children']
                     if h['target'].get('title') == 'Three')
        back = next(h for h in three['children']
                    if h['target'].get('id') == 1)
        self.assertTrue(back['target'].get('revisit'))
        self.assertEqual(back['target'].get('children'), [])

    def test_tree_depth_is_clamped_and_missing_songs_404(self):
        shallow = self.client.get('/api/songs/1/transition-tree?depth=1').get_json()
        two = shallow['children'][0]['target']
        self.assertEqual(two.get('children'), [])
        resp = self.client.get('/api/songs/999/transition-tree')
        self.assertEqual(resp.status_code, 404)

    def test_tree_signals_has_more_at_the_depth_limit(self):
        shallow = self.client.get('/api/songs/1/transition-tree?depth=1').get_json()
        by_title = {c['target'].get('title'): c['target'] for c in shallow['children']}
        self.assertEqual(by_title['Two']['children'], [])
        self.assertTrue(by_title['Two']['has_more'])    # Two -> Three + text leaf
        self.assertFalse(by_title['Four']['has_more'])  # Four has no outgoing
        deep = self.client.get('/api/songs/1/transition-tree?depth=3').get_json()
        four = next(c['target'] for c in deep['children']
                    if c['target'].get('title') == 'Four')
        self.assertFalse(four['has_more'])

    def test_exclude_folds_ids_into_the_visited_set(self):
        plain = self.client.get('/api/songs/3/transition-tree?depth=2').get_json()
        self.assertFalse(plain['children'][0]['target'].get('revisit'))
        excl = self.client.get('/api/songs/3/transition-tree?depth=2&exclude=1').get_json()
        self.assertTrue(excl['children'][0]['target'].get('revisit'))
        self.assertEqual(excl['children'][0]['target'].get('children'), [])
        junk = self.client.get('/api/songs/3/transition-tree?depth=2&exclude=abc,,').get_json()
        self.assertFalse(junk['children'][0]['target'].get('revisit'))

    def test_tree_truncates_beyond_ten_hops_per_song(self):
        with app.get_db() as db:
            for sid in range(10, 22):
                add_song(db, sid, f'S{sid}')
                add_hop(db, 4, to_id=sid)
        tree = self.client.get('/api/songs/1/transition-tree?depth=2').get_json()
        four = next(c['target'] for c in tree['children']
                    if c['target'].get('title') == 'Four')
        self.assertEqual(len(four['children']), 10)
        self.assertTrue(four['truncated'])
        self.assertEqual(four['omitted'], 2)

    def test_tree_includes_mashup_input_names(self):
        with app.get_db() as db:
            db.execute('INSERT INTO songs(id,filename,filepath,title,artist,bpm,musical_key) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (9, 'Mash.mp3', '/music/Mash.mp3', 'Mashup', 'DJ', 128.0, '11A'))
            db.commit()
            add_hop(db, 9, to_id=2, mash=True, mi_id=3, notes='mash in')
        tree = self.client.get('/api/songs/9/transition-tree').get_json()
        self.assertEqual(len(tree['children']), 1)
        hop = tree['children'][0]
        self.assertTrue(hop['is_mashup_track'])
        self.assertEqual(hop['mashup_input_title'], 'Three')


if __name__ == '__main__':
    unittest.main()
