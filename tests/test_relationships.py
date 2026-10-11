"""The relationship graph (`/api/relationships`): the data behind the rail.

Every node here is a value the library actually holds -- a genre, tag, artist,
album, crate, key or transition -- and every edge means two of those values
ride the same track. So these tests are mostly about arithmetic on the library
that the panel is not allowed to get wrong: weights that must equal the tracks
carrying a value, edges that must come from shared tracks, findings that must
be computed from the rows rather than hard-coded, and honest caps on a library
the size of the real one.

Everything runs against a scratch database in a temporary directory; the live
~/.dj-library.db, the music folder and the on-disk export are never touched.
"""
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import app
import relationships


class RelationshipTestBase(unittest.TestCase):
    """A scratch library: a temp database, a temp music folder, and the export
    request mocked so nothing here can reach the running app's files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.music = self.root / 'music'
        self.music.mkdir()
        self.db_path = self.root / 'library.sqlite'
        self.db_patch = patch.object(app, 'DB_PATH', str(self.db_path))
        self.music_patch = patch.object(app, 'MUSIC_DIR', str(self.music))
        self.export_patch = patch.object(app, 'queue_auto_export')
        # Keys read as Camelot here whatever the machine's config.json says, so
        # the node labels in these tests are not at the mercy of a setting.
        self.key_patch = patch.object(app, 'KEY_DISPLAY', 'camelot')
        for started in (self.db_patch, self.music_patch, self.export_patch, self.key_patch):
            started.start()
            self.addCleanup(started.stop)
        app.init_db()
        self.client = app.app.test_client()

    # -- seeding ------------------------------------------------------------

    def add_song(self, song_id, title=None, artist='Artist', album=None, genre='House',
                 key=None, bpm=None):
        with app.get_db() as db:
            db.execute('INSERT INTO songs(id,filename,filepath,title,artist,album,genre,'
                       'musical_key,bpm) VALUES(?,?,?,?,?,?,?,?,?)',
                       (song_id, f'{song_id}.mp3', str(self.music / f'{song_id}.mp3'),
                        title or f'Song {song_id}', artist, album, genre, key, bpm))
            db.commit()
        return song_id

    def add_songs(self, rows):
        """rows: (id, dict-of-songs-columns) -- one commit for a big fixture."""
        with app.get_db() as db:
            db.executemany(
                'INSERT INTO songs(id,filename,filepath,title,artist,album,genre,'
                'musical_key,bpm) VALUES(?,?,?,?,?,?,?,?,?)',
                [(song_id, f'{song_id}.mp3', str(self.music / f'{song_id}.mp3'),
                  row.get('title') or f'Song {song_id}', row.get('artist'),
                  row.get('album'), row.get('genre'), row.get('musical_key'),
                  row.get('bpm')) for song_id, row in rows])
            db.commit()

    def add_transition(self, source, target=None, text=None, notes=None, tag=None,
                       status='confirmed'):
        with app.get_db() as db:
            db.execute('INSERT INTO transitions(from_song_id,to_song_id,to_text,notes,tag,status) '
                       'VALUES(?,?,?,?,?,?)', (source, target, text, notes, tag, status))
            db.commit()

    def add_crate(self, crate_id, name, smart=False, rules=None, song_ids=()):
        with app.get_db() as db:
            db.execute('INSERT INTO crates(id,name,is_smart,rules) VALUES(?,?,?,?)',
                       (crate_id, name, 1 if smart else 0, json.dumps(rules or [])))
            for song_id in song_ids:
                db.execute('INSERT INTO crate_songs(crate_id,song_id) VALUES(?,?)',
                           (crate_id, song_id))
            db.commit()

    # -- reading the graph --------------------------------------------------

    def graph(self, **params):
        """The endpoint's payload, with the status checked so a 500 shows up as
        a failure here rather than as a confusing KeyError further down."""
        response = self.client.get('/api/relationships', query_string=params)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:600])
        return response.get_json()

    def service(self, **kwargs):
        """The graph built straight from the app's own helpers -- what the
        blueprint does, minus HTTP."""
        return relationships.Relationships(
            get_db=app.get_db, to_camelot=app.to_camelot,
            crate_song_ids=app.crate_song_ids, **kwargs)

    def nodes_of(self, payload, layer):
        return [n for n in payload['nodes'] if n['type'] == layer]

    def node(self, payload, node_id):
        for node in payload['nodes']:
            if node['id'] == node_id:
                return node
        self.fail(f'{node_id} is not in the graph; nodes were '
                  f'{[n["id"] for n in payload["nodes"]]}')

    def finding(self, payload, finding_id):
        for find in payload['findings']:
            if find['id'] == finding_id:
                return find
        return None

    def layer_option(self, payload, layer):
        return {option['key']: option for option in payload['layer_options']}[layer]

    def dump_db(self):
        """Every table's rows, so a read-only claim can be proved by comparison
        instead of assumed."""
        with app.get_db() as db:
            tables = [r['name'] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            return {table: [tuple(row) for row in db.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()]
                for table in tables}


class GraphShapeContracts(RelationshipTestBase):
    """What the nodes and edges are, and the weights behind them."""

    def setUp(self):
        super().setUp()
        self.add_song(1, title='Alpha', artist='Same Artist', album='Album One',
                      genre='House; Tech House', key='G min')
        self.add_song(2, title='Beta', artist='Same Artist', album='Album One',
                      genre='House', key='G min')
        self.add_song(3, title='Gamma', artist='Solo Artist', album='Album Two',
                      genre='Deep House; #road test', key='C maj')
        app.add_song_tags(1, ['peak hour'])
        app.add_song_tags(2, ['peak hour'])
        self.payload = self.graph()

    def test_a_node_weight_is_the_number_of_tracks_carrying_that_value(self):
        self.assertEqual(self.node(self.payload, 'genre:house')['weight'], 2)
        self.assertEqual(self.node(self.payload, 'genre:house')['songs'], 2)
        self.assertEqual(self.node(self.payload, 'genre:tech-house')['weight'], 1)
        self.assertEqual(self.node(self.payload, 'artist:same-artist')['weight'], 2)
        self.assertEqual(self.node(self.payload, 'album:album-one')['weight'], 2)
        self.assertEqual(self.node(self.payload, 'tag:peak-hour')['weight'], 2)
        self.assertEqual(self.node(self.payload, 'key:6a')['weight'], 2)
        # A node's song ids are the tracks themselves, sorted, and the count is
        # derivable from them.
        self.assertEqual(self.node(self.payload, 'genre:house')['song_ids'], [1, 2])

    def test_the_compound_genre_lands_as_two_genre_nodes(self):
        labels = {node['label'] for node in self.nodes_of(self.payload, 'genre')}
        self.assertEqual(labels, {'House', 'Tech House', 'Deep House'})

    def test_a_hash_tag_left_in_a_genre_never_becomes_a_genre(self):
        genres = [node['label'] for node in self.nodes_of(self.payload, 'genre')]
        self.assertNotIn('#road test', genres)
        self.assertNotIn('road test', genres)
        self.assertFalse([g for g in genres if g.startswith('#')])
        self.assertFalse([n for n in self.payload['nodes'] if n['label'].startswith('#')])

    def test_a_key_node_is_the_camelot_spelling_with_its_other_reading(self):
        node = self.node(self.payload, 'key:6a')
        self.assertEqual(node['label'], '6A')
        self.assertEqual(node['detail']['camelot'], '6A')
        self.assertEqual(node['detail']['notation'], 'G min')
        self.assertEqual(node['detail']['wheel_index'], 6)
        self.assertEqual(node['detail']['ring'], 'A')

    def test_the_other_reading_is_the_label_when_that_is_the_setting(self):
        with patch.object(app, 'KEY_DISPLAY', 'notation'):
            payload = self.graph()
        node = self.node(payload, 'key:6a')
        self.assertEqual(node['label'], 'G min')
        self.assertEqual(node['detail']['camelot'], '6A')      # identity stays Camelot

    def test_single_track_artists_are_reported_not_drawn(self):
        # 605 tracks over 523 artists taught this rule: one-off artists have no
        # node, and the layer says how many were held back.
        self.assertEqual([n['label'] for n in self.nodes_of(self.payload, 'artist')],
                         ['Same Artist'])
        option = self.layer_option(self.payload, 'artist')
        self.assertEqual((option['nodes'], option['values'], option['low_weight']), (1, 2, 1))

    def test_an_edge_means_two_values_share_tracks_and_carries_how_many(self):
        edge = next(e for e in self.payload['edges']
                    if {e['source'], e['target']} == {'artist:same-artist', 'genre:house'})
        self.assertEqual(edge['weight'], 2)               # songs 1 and 2
        self.assertEqual(edge['type'], 'artist-genre')

    def test_two_values_of_one_layer_are_never_joined_to_each_other(self):
        for edge in self.payload['edges']:
            source = self.node(self.payload, edge['source'])
            target = self.node(self.payload, edge['target'])
            self.assertNotEqual(source['type'], target['type'], edge)
            self.assertNotEqual(edge['type'].split('-')[0],
                                edge['type'].split('-')[1], edge)

    def test_every_edge_endpoint_is_a_node_in_the_payload(self):
        ids = {node['id'] for node in self.payload['nodes']}
        for edge in self.payload['edges']:
            self.assertIn(edge['source'], ids)
            self.assertIn(edge['target'], ids)

    def test_degrees_count_the_edges_a_node_takes_part_in(self):
        degree = {}
        for edge in self.payload['edges']:
            degree[edge['source']] = degree.get(edge['source'], 0) + 1
            degree[edge['target']] = degree.get(edge['target'], 0) + 1
        for node in self.payload['nodes']:
            self.assertEqual(node['degree'], degree.get(node['id'], 0), node['id'])
        self.assertGreater(self.node(self.payload, 'key:6a')['degree'], 0)

    def test_hubs_are_the_heaviest_nodes_with_their_degrees(self):
        hubs = self.payload['hubs']
        self.assertLessEqual(len(hubs), 6)
        weights = [hub['weight'] for hub in hubs]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertIn('genre:house', [hub['id'] for hub in hubs])
        for hub in hubs:
            self.assertEqual(hub, {k: self.node(self.payload, hub['id'])[k] for k in hub})


class GraphLayerContracts(RelationshipTestBase):
    """Layers are separate node types: each can be asked for on its own, and the
    empty ones (crates, tags and transitions on most libraries) say so."""

    def setUp(self):
        super().setUp()
        self.add_song(1, title='Alpha', artist='Same Artist', album='Album One',
                      genre='House', key='G min')
        self.add_song(2, title='Beta', artist='Same Artist', album='Album One',
                      genre='House', key='G min')
        self.add_song(3, title='Gamma', artist='Solo Artist', album='Album Two',
                      genre='Deep House', key='C maj')
        app.add_song_tags(1, ['tool'])

    def test_asking_for_two_layers_keeps_only_those_nodes_and_their_edges(self):
        payload = self.graph(layers='genre,key')
        self.assertEqual(payload['layers'], ['genre', 'key'])
        self.assertEqual({n['type'] for n in payload['nodes']}, {'genre', 'key'})
        for edge in payload['edges']:
            self.assertEqual({self.node(payload, edge['source'])['type'],
                              self.node(payload, edge['target'])['type']},
                             {'genre', 'key'})

    def test_every_layer_still_reports_its_counts_when_it_is_switched_off(self):
        payload = self.graph(layers='genre')
        self.assertEqual([o['key'] for o in payload['layer_options']],
                         list(relationships.LAYERS))
        self.assertEqual(self.layer_option(payload, 'tag')['nodes'], 1)
        self.assertEqual(self.layer_option(payload, 'artist')['nodes'], 1)
        self.assertEqual(self.layer_option(payload, 'key')['nodes'], 2)
        self.assertEqual(self.nodes_of(payload, 'tag'), [])

    def test_an_unknown_layer_name_is_dropped_rather_than_guessed(self):
        payload = self.graph(layers='nonsense')
        self.assertEqual(payload['layers'], [])
        self.assertEqual(payload['nodes'], [])
        self.assertEqual(payload['edges'], [])
        self.assertEqual(payload['stats']['nodes'], 0)

    def test_one_layer_comes_back_alone_with_its_own_honest_counts(self):
        """What the rail's "show only this layer" asks for: that layer's nodes,
        whatever edges survive between them, and stats about what it got."""
        self.add_crate(1, 'Warm-up', song_ids=(1, 2))
        self.add_crate(2, 'Peak', song_ids=(1,))
        payload = self.graph(layers='crate')
        self.assertEqual(payload['layers'], ['crate'])
        self.assertEqual({node['type'] for node in payload['nodes']}, {'crate'})
        self.assertEqual(len(payload['nodes']), self.layer_option(payload, 'crate')['nodes'])
        self.assertEqual(payload['stats']['nodes'], len(payload['nodes']))
        # Two crates share a track, but one layer never joins to itself, so an
        # isolated crate graph is dots with no lines -- not a broken payload.
        self.assertEqual(payload['edges'], [])
        self.assertEqual(payload['stats']['edges'], 0)

    def test_asking_layer_by_layer_adds_up_to_the_whole_graph(self):
        """Isolation filters the graph; it must never build a different one."""
        self.add_crate(1, 'Warm-up', song_ids=(1, 2))
        self.add_transition(1, target=2, notes='works')
        whole = self.graph()
        by_layer = {layer: self.graph(layers=layer) for layer in relationships.LAYERS}
        for layer, payload in by_layer.items():
            self.assertEqual({node['id'] for node in payload['nodes']},
                             {node['id'] for node in self.nodes_of(whole, layer)}, layer)
            self.assertEqual(payload['layers'], [layer])
            self.assertEqual(payload['stats']['nodes'], len(payload['nodes']))
            for edge in payload['edges']:
                self.assertEqual(self.node(payload, edge['source'])['type'], layer)
                self.assertEqual(self.node(payload, edge['target'])['type'], layer)
        self.assertEqual(sorted(node['id'] for node in whole['nodes']),
                         sorted(node['id'] for payload in by_layer.values()
                                for node in payload['nodes']),
                         'every node is in exactly one layer')

    def test_repeated_names_collapse_and_an_empty_selection_means_all_of_them(self):
        self.add_crate(1, 'Warm-up', song_ids=(1,))
        self.assertEqual(self.graph(layers=' CRATE , crate ,, ')['layers'], ['crate'])
        self.assertEqual(self.graph(layers='crate,key')['layers'], ['crate', 'key'])
        # `?layers=` with an empty value is still "no filter"; the panel only
        # sends a real selection when the user makes one.
        self.assertEqual(self.graph(layers='')['layers'], list(relationships.LAYERS))
        self.assertEqual(self.graph()['layers'], list(relationships.LAYERS))

    def test_the_arrangement_is_echoed_and_falls_back_to_clusters(self):
        self.assertEqual(self.graph(arrangement='wheel')['arrangement'], 'wheel')
        self.assertEqual(self.graph(arrangement='nonsense')['arrangement'], 'clusters')
        self.assertEqual(self.graph()['arrangement'], 'clusters')

    def test_an_empty_layer_is_reported_as_empty(self):
        payload = self.graph()
        for layer in ('crate', 'transition'):
            option = self.layer_option(payload, layer)
            self.assertTrue(option['empty'], layer)
            self.assertEqual(option['nodes'], 0)
        self.assertEqual(self.nodes_of(payload, 'crate'), [])
        for option in payload['layer_options']:
            self.assertEqual(option['empty'], option['values'] == 0)

    def test_transitions_become_nodes_for_the_tracks_they_link(self):
        self.add_transition(1, target=2, notes='long blend', tag='warm-up')
        self.add_transition(2, target=1, notes='bring it back')
        self.add_transition(1, text='Not owned yet', notes='tease')
        self.add_transition(3, target=2, notes='unverified', status='to_try')
        payload = self.graph()
        linked = {node['detail']['song_id'] for node in self.nodes_of(payload, 'transition')}
        self.assertEqual(linked, {1, 2})                      # 3 is only 'to_try'
        link = next(e for e in payload['edges'] if e['type'] == 'transition-transition')
        self.assertEqual(link['weight'], 2)                   # both directions
        self.assertIn('long blend', link['detail']['notes'])
        self.assertIn('bring it back', link['detail']['notes'])
        self.assertEqual(link['detail']['tags'], ['warm-up'])
        self.assertEqual(payload['stats']['transition_links'], 1)
        self.assertEqual(payload['stats']['text_only_transitions'], 1)

    def test_a_to_try_transition_stays_out_of_the_graph(self):
        self.add_transition(1, target=2, notes='unverified', status='to_try')
        payload = self.graph()
        self.assertEqual(self.nodes_of(payload, 'transition'), [])
        self.assertEqual(payload['stats']['transition_links'], 0)
        self.assertEqual([f['id'] for f in payload['findings'] if f['id'] == 'no-transitions'],
                         ['no-transitions'])

    def test_a_manual_crate_is_a_node_with_its_id_and_kind(self):
        self.add_crate(1, 'Warm-up', song_ids=(1, 2))
        node = self.node(self.graph(), 'crate:warm-up')
        self.assertEqual(node['weight'], 2)
        self.assertEqual(node['label'], 'Warm-up')
        self.assertEqual(node['detail']['crate_name'], 'Warm-up')
        self.assertEqual(node['detail']['crate_id'], 1)
        self.assertFalse(node['detail']['smart'])

    def test_a_smart_crate_is_evaluated_through_its_rules(self):
        rules = [{'field': 'genre', 'op': 'eq', 'value': 'House'}]
        self.add_crate(2, 'House only', smart=True, rules=rules)
        payload = self.graph()
        node = self.node(payload, 'crate:house-only')
        self.assertEqual(node['detail']['smart'], True)
        self.assertEqual(node['detail']['crate_id'], 2)
        self.assertEqual(node['weight'], 2)                   # songs 1 and 2
        self.assertEqual(node['song_ids'], [1, 2])
        with app.get_db() as db:                              # not a stored membership
            stored = db.execute('SELECT COUNT(*) FROM crate_songs WHERE crate_id=2').fetchone()[0]
        self.assertEqual(stored, 0)
        # The crate follows the data rather than a stored membership: a new
        # House track joins it without touching crate_songs.
        self.add_song(4, title='Delta', artist='Other', genre='House', key='6A')
        self.assertEqual(self.node(self.graph(), 'crate:house-only')['weight'], 3)


class GraphFindingContracts(RelationshipTestBase):
    """Findings are computed from the counts, and each one an action can run."""

    def test_junk_and_missing_genres_name_the_tracks_they_mean(self):
        self.add_song(1, artist='Same', genre='70', key='G min')
        self.add_song(2, artist='Same', genre=None, key='G min')
        payload = self.graph()
        junk = self.finding(payload, 'junk-genres')
        self.assertEqual(junk['severity'], 'warn')
        self.assertEqual(junk['metric'], 1)
        self.assertEqual(junk['song_ids'], [1])
        self.assertEqual(junk['node_ids'], ['genre:70'])
        self.assertEqual(junk['action'], {'kind': 'select-songs',
                                          'label': 'Select those tracks',
                                          'target': 'junk-genres'})
        self.assertIn('"70"', junk['detail'])
        genre = self.node(payload, 'genre:70')
        self.assertTrue(genre['detail']['junk'])
        genreless = self.finding(payload, 'genreless')
        self.assertEqual(genreless['severity'], 'warn')
        self.assertEqual(genreless['song_ids'], [2])
        self.assertEqual(genreless['action']['kind'], 'select-songs')

    def test_a_genre_used_once_is_a_finding_with_a_way_to_act_on_it(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key='G min')
        self.add_song(3, artist='Solo', genre='Deep House', key='G min')
        payload = self.graph()
        finding = self.finding(payload, 'single-track-genres')
        self.assertEqual(finding['metric'], 1)
        self.assertEqual(finding['song_ids'], [3])
        self.assertEqual(finding['node_ids'], ['genre:deep-house'])
        self.assertEqual(finding['action'],
                         {'kind': 'open', 'label': 'Review tags vs genres',
                          'target': 'tag-review'})
        self.assertIn('Deep House', finding['detail'])

    def test_a_key_that_mixes_nowhere_is_named_with_its_tracks(self):
        self.add_songs([(song_id, {'artist': 'Same', 'genre': 'House', 'musical_key': 'G min'})
                        for song_id in range(1, 9)]
                       + [(9, {'artist': 'Same', 'genre': 'House', 'musical_key': 'C maj'})])
        payload = self.graph()
        self.assertEqual(self.node(payload, 'key:6a')['weight'], 8)
        self.assertEqual(self.node(payload, 'key:6a')['detail']['harmonic_ids'], [])
        islands = self.finding(payload, 'key-islands')
        self.assertEqual(islands['metric'], 1)
        self.assertEqual(islands['node_ids'], ['key:6a'])
        self.assertEqual(islands['song_ids'], list(range(1, 9)))
        self.assertEqual(islands['action']['target'], 'key-islands')
        # One track in a neighbouring Camelot key and the shelf can mix again.
        self.add_song(10, artist='Other', genre='House', key='C min')           # 5A
        after = self.graph()
        self.assertIsNone(self.finding(after, 'key-islands'))
        self.assertEqual(self.node(after, 'key:6a')['detail']['harmonic_ids'], ['key:5a'])

    def test_a_key_under_eight_tracks_is_not_called_an_island(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key='G min')
        self.assertIsNone(self.finding(self.graph(), 'key-islands'))

    def test_tracks_without_a_key_offer_the_analysis_run(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key=None)
        finding = self.finding(self.graph(), 'no-key')
        self.assertEqual(finding['severity'], 'warn')
        self.assertEqual(finding['song_ids'], [2])
        self.assertEqual(finding['action'], {'kind': 'run-analysis',
                                             'label': 'Analyse BPM & key',
                                             'target': 'analyze-bpm'})

    def test_the_empty_layers_are_findings_with_something_to_do_about_them(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        payload = self.graph()
        self.assertEqual(self.finding(payload, 'no-tags')['action']['target'], 'tag-review')
        self.assertEqual(self.finding(payload, 'no-crates')['action']['target'], 'crates')
        self.assertEqual(self.finding(payload, 'no-transitions')['action']['target'],
                         'transitions')
        # Tagging one track is enough to retire the tag finding.
        app.add_song_tags(1, ['tool'])
        after = self.graph()
        self.assertIsNone(self.finding(after, 'no-tags'))
        self.assertEqual(self.node(after, 'tag:tool')['weight'], 1)
        # A crate changes what the rail can show, and the finding goes with it.
        self.add_crate(1, 'Warm-up', song_ids=(1,))
        self.assertIsNone(self.finding(self.graph(), 'no-crates'))

    def test_thin_crates_are_counted_because_a_one_track_crate_is_usually_a_slip(self):
        for song_id in (1, 2, 3, 4):
            self.add_song(song_id, artist='Same', genre='House', key='G min')
        self.add_crate(1, 'Tiny', song_ids=(1,))
        self.add_crate(2, 'Real set', song_ids=(1, 2, 3, 4))
        finding = self.finding(self.graph(), 'thin-crates')
        self.assertEqual(finding['metric'], 1)
        self.assertEqual(finding['song_ids'], [1])
        self.assertIn('Tiny (1)', finding['detail'])
        # It names the crate itself, so a busy rail can point at it, and it
        # offers both things a DJ would do next: take the tracks, or go and fix
        # the crate. `action` stays the primary button for older panels.
        self.assertEqual(finding['node_ids'], ['crate:tiny'])
        self.assertEqual([button['target'] for button in finding['actions']],
                         ['thin-crates', 'crates'])
        self.assertEqual(finding['action'], finding['actions'][0])

    def test_text_only_transitions_are_counted_as_leaving_the_library(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key='G min')
        self.add_transition(1, target=2, notes='inside')
        self.add_transition(1, text='Not owned', notes='outside')
        payload = self.graph()
        finding = self.finding(payload, 'text-only-transitions')
        self.assertEqual(finding['metric'], 1)
        self.assertEqual(finding['action']['target'], 'transitions')
        self.assertIsNone(self.finding(payload, 'no-transitions'))
        # The track whose mix leaves the library has a transition node even
        # though its target has none, so the finding can name and select it.
        self.assertEqual(finding['song_ids'], [1])
        self.assertEqual(finding['node_ids'], ['transition:song-1-same'])
        self.assertEqual([button['kind'] for button in finding['actions']],
                         ['open', 'select-songs'])

    def test_one_hit_artists_explain_why_the_artist_layer_is_thin(self):
        self.add_song(1, artist='Repeated', genre='House', key='G min')
        self.add_song(2, artist='Repeated', genre='House', key='G min')
        self.add_song(3, artist='Only once', genre='House', key='G min')
        finding = self.finding(self.graph(), 'one-hit-artists')
        self.assertEqual(finding['metric'], 1)
        self.assertEqual(finding['song_ids'], [3])

    def test_concentration_is_only_called_out_when_two_genres_really_dominate(self):
        self.add_songs([(song_id, {'artist': 'Same', 'genre': 'House', 'musical_key': 'G min'})
                        for song_id in (1, 2, 3)]
                       + [(4, {'artist': 'Other', 'genre': 'Techno', 'musical_key': '8B'})])
        finding = self.finding(self.graph(), 'genre-concentration')
        self.assertEqual(finding['metric'], 1.0)
        self.assertEqual(finding['node_ids'], ['genre:house', 'genre:techno'])
        self.assertEqual(finding['action']['kind'], 'focus')
        # Diluted below a fifth of the library (4 of 24 tracks), the remark is
        # no longer true and the panel stops making it.
        self.add_songs([(song_id, {'artist': f'A{song_id}', 'genre': f'Genre {song_id}',
                                   'musical_key': '6A'}) for song_id in range(5, 25)])
        self.assertIsNone(self.finding(self.graph(), 'genre-concentration'))

    def test_every_finding_is_well_formed_and_capped(self):
        self.add_song(1, artist='Same', genre='70', key=None)
        self.add_song(2, artist='Solo', genre=None, key='G min')
        self.add_crate(1, 'Tiny', song_ids=(1,))
        payload = self.graph()
        self.assertTrue(payload['findings'])
        self.assertLessEqual(len(payload['findings']), relationships.FINDING_LIMIT)
        for finding in payload['findings']:
            self.assertIn(finding['severity'], ('warn', 'info'))
            self.assertTrue(finding['title'].strip(), finding)
            self.assertTrue(finding['detail'].strip(), finding)
            self.assertIsInstance(finding['song_ids'], list)
            self.assertIsInstance(finding['node_ids'], list)
            # `actions` is the full list of buttons and `action` its first, so a
            # finding with no way to act reports neither rather than one of
            # them being a half-built button.
            self.assertIsInstance(finding['actions'], list)
            if finding['action'] is None:
                self.assertEqual(finding['actions'], [], finding['id'])
            else:
                self.assertEqual(finding['action'], finding['actions'][0], finding['id'])
            for button in finding['actions']:
                self.assertIn(button['kind'],
                              ('select-songs', 'open', 'focus', 'run-analysis'))
                self.assertTrue(button['label'].strip())
                self.assertTrue(button['target'])
        # Warnings come first: the panel reads top-down.
        severities = [f['severity'] for f in payload['findings']]
        self.assertEqual(severities, sorted(severities, key=lambda s: s != 'warn'))

    def test_findings_are_silent_about_a_library_that_is_fine(self):
        self.add_songs([(song_id, {'artist': 'Same', 'album': 'Album',
                                   'genre': 'House', 'musical_key': 'G min'})
                        for song_id in range(1, 5)])
        app.add_song_tags(1, ['tool'])
        self.add_crate(1, 'Set', song_ids=(1, 2, 3, 4))
        self.add_transition(1, target=2, notes='works')
        payload = self.graph()
        self.assertEqual([f['id'] for f in payload['findings']
                          if f['severity'] == 'warn'], [])


class GraphCapContracts(RelationshipTestBase):
    """A big library is drawn small on purpose -- and says what it left out."""

    def setUp(self):
        super().setUp()
        self.add_songs([(song_id, {'artist': f'Artist {song_id % 5}',
                                   'album': f'Album {song_id % 3}',
                                   'genre': f'Genre {song_id % 4}',
                                   'musical_key': 'G min'})
                        for song_id in range(1, 21)])
        self.full = self.graph()
        self.small = self.service(node_limit=1).payload()

    def test_the_node_cap_keeps_the_heaviest_value_of_every_layer(self):
        for layer in relationships.LAYERS:
            kept = [n for n in self.small['nodes'] if n['type'] == layer]
            self.assertLessEqual(len(kept), 1, layer)
            if kept:
                heaviest = max(self.nodes_of(self.full, layer), key=lambda n: n['weight'])
                self.assertEqual(kept[0]['id'], heaviest['id'], layer)

    def test_omitted_nodes_add_up_to_what_the_cap_held_back(self):
        self.assertEqual(self.small['stats']['omitted_nodes'],
                         self.full['stats']['nodes'] - len(self.small['nodes']))
        self.assertGreater(self.small['stats']['omitted_nodes'], 0)
        self.assertEqual(self.small['stats']['node_limit'], 1)

    def test_the_edge_cap_keeps_the_strongest_links_and_reports_the_rest(self):
        small = self.service(edge_limit=2).payload()
        self.assertEqual(len(small['edges']), 2)
        self.assertEqual(small['stats']['omitted_edges'], len(self.full['edges']) - 2)
        self.assertEqual([e['weight'] for e in small['edges']],
                         sorted([e['weight'] for e in small['edges']], reverse=True))
        self.assertEqual(small['stats']['node_limit'], relationships.NODE_LIMIT)

    def test_a_default_build_reports_the_limits_it_used(self):
        self.assertEqual(self.full['stats']['node_limit'], relationships.NODE_LIMIT)
        self.assertEqual(self.full['stats']['edge_limit'], relationships.EDGE_LIMIT)


class GraphApiContracts(RelationshipTestBase):
    """The endpoint itself: an empty library, determinism, and read-onlyness."""

    def test_an_empty_library_draws_nothing_and_says_what_is_missing(self):
        payload = self.graph()
        self.assertEqual(payload['stats']['songs'], 0)
        self.assertEqual(payload['nodes'], [])
        self.assertEqual(payload['edges'], [])
        self.assertEqual(payload['hubs'], [])
        self.assertEqual(payload['layers'], list(relationships.LAYERS))
        self.assertEqual({f['id'] for f in payload['findings']},
                         {'no-crates', 'no-tags', 'no-transitions'})
        for option in payload['layer_options']:
            self.assertTrue(option['empty'], option['key'])
            self.assertEqual(option['nodes'], 0)

    def test_a_library_without_the_tag_tables_is_empty_rather_than_an_error(self):
        # The tags tables arrive with the app's first start on a database, so the
        # rail must describe a pre-migration file instead of failing on it.
        self.add_song(1, artist='Same', genre='House', key='G min')
        with app.get_db() as db:
            db.execute('DROP TABLE song_tags')
            db.execute('DROP TABLE tags')
            db.commit()
        payload = self.graph()
        self.assertEqual(payload['stats']['songs'], 1)
        self.assertEqual(self.nodes_of(payload, 'tag'), [])
        self.assertEqual(self.layer_option(payload, 'tag')['empty'], True)
        self.assertIsNotNone(self.finding(payload, 'no-tags'))

    def test_the_same_library_twice_is_the_same_graph(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key='G min')
        self.add_song(3, artist='Solo', genre='Deep House', key='C maj')
        app.add_song_tags(2, ['tool'])
        self.assertEqual(json.dumps(self.graph(), sort_keys=True),
                         json.dumps(self.graph(), sort_keys=True))

    def test_the_graph_never_writes_anything(self):
        self.add_song(1, artist='Same', genre='House', key='G min')
        self.add_song(2, artist='Same', genre='House', key='G min')
        app.add_song_tags(1, ['tool'])
        self.add_crate(1, 'Warm-up', song_ids=(1, 2))
        self.add_transition(1, target=2, notes='works')
        self.assertEqual(app.DB_PATH, str(self.db_path))       # the scratch library
        before = self.dump_db()
        self.graph(layers='genre,crate,transition')
        self.service(node_limit=1).payload()
        self.assertEqual(self.dump_db(), before)
        with app.get_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM song_tags').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tags').fetchone()[0], 1)

    def test_layer_parsing_keeps_the_canonical_order_and_drops_nonsense(self):
        self.assertEqual(relationships.parse_layers(None), list(relationships.LAYERS))
        self.assertEqual(relationships.parse_layers(''), list(relationships.LAYERS))
        self.assertEqual(relationships.parse_layers('key, genre'), ['genre', 'key'])
        self.assertEqual(relationships.parse_layers(['key', 'GENRE']), ['genre', 'key'])
        self.assertEqual(relationships.parse_layers('nonsense'), [])
        self.assertEqual(relationships.parse_layers(123), list(relationships.LAYERS))
        self.assertEqual(relationships.parse_layers(('tag',)), ['tag'])

    def test_arrangement_parsing_only_accepts_the_two_it_knows(self):
        self.assertEqual(relationships.parse_arrangement('wheel'), 'wheel')
        self.assertEqual(relationships.parse_arrangement(' WHEEL '), 'wheel')
        self.assertEqual(relationships.parse_arrangement('clusters'), 'clusters')
        for value in (None, '', 'nonsense', 123, 'wheels'):
            self.assertEqual(relationships.parse_arrangement(value), 'clusters')

    def test_node_ids_are_stable_slugs_of_the_labels(self):
        self.assertEqual(relationships.slug('Progressive House'), 'progressive-house')
        self.assertEqual(relationships.slug('Peak Hour!'), 'peak-hour')
        self.assertEqual(relationships.slug(None), '')
        self.assertEqual(relationships.camelot_slot('6a'), (6, 'A'))
        self.assertEqual(relationships.camelot_slot('13A'), None)
        # 12B mixes into 11B and 1B (its neighbours on the ring) and 12A.
        self.assertEqual(relationships.harmonic_slots((12, 'B')), [(11, 'B'), (1, 'B'), (12, 'A')])
        self.assertEqual(relationships.harmonic_slots((1, 'A')), [(12, 'A'), (2, 'A'), (1, 'B')])


class GraphScaleContracts(RelationshipTestBase):
    """The real library is 605 tracks over 523 artists. This is the guarantee
    that the rail holds up when the collection grows."""

    def seed_library(self, tracks=1000, artists=300, genres=60, albums=60, tags=12):
        # All twenty-four Camelot slots (twelve numbers on each ring). The walk
        # has to be on song_id % 24 -- keying the ring off song_id % 2 would pin
        # every number to one ring and only ever produce twelve of them.
        self.add_songs([(song_id, {
            'artist': f'Artist {song_id % artists}',
            'album': f'Album {song_id % albums}',
            'genre': f'Genre {song_id % genres}',
            'musical_key': f'{(song_id % 24) % 12 + 1}{"AB"[(song_id % 24) // 12]}',
            'bpm': 120 + (song_id % 40),
        }) for song_id in range(1, tracks + 1)])
        with app.get_db() as db:
            db.executemany('INSERT INTO tags(id,name) VALUES(?,?)',
                           [(tag_id + 1, f'Tag {tag_id}') for tag_id in range(tags)])
            db.executemany('INSERT INTO song_tags(song_id,tag_id) VALUES(?,?)',
                           [(song_id, song_id % tags + 1) for song_id in range(1, tracks + 1)])
            db.commit()

    def test_a_thousand_tracks_build_in_well_under_a_second_with_the_caps_applied(self):
        self.seed_library()
        started = time.perf_counter()
        payload = self.graph()
        elapsed = time.perf_counter() - started
        self.assertEqual(payload['stats']['songs'], 1000)
        self.assertLess(elapsed, 1.0, f'the graph took {elapsed:.3f}s to build')
        self.assertLessEqual(payload['stats']['nodes'],
                             len(relationships.LAYERS) * relationships.NODE_LIMIT)
        self.assertLessEqual(payload['stats']['edges'], relationships.EDGE_LIMIT)
        self.assertGreater(payload['stats']['omitted_nodes'], 0)
        self.assertGreater(payload['stats']['omitted_edges'], 0)
        self.assertEqual(self.layer_option(payload, 'artist')['nodes'], 300)
        self.assertLessEqual(len(self.nodes_of(payload, 'artist')), relationships.NODE_LIMIT)
        # Ids carried per node are capped while the count stays exact.
        for node in payload['nodes']:
            self.assertLessEqual(len(node['song_ids']), relationships.SONG_ID_LIMIT)
            self.assertGreaterEqual(node['songs'], len(node['song_ids']))

    def test_the_wheel_arrangement_has_a_slot_for_every_key(self):
        self.seed_library(tracks=240, artists=40, genres=12, albums=12)
        payload = self.graph(arrangement='wheel', layers='key,genre')
        keys = self.nodes_of(payload, 'key')
        # Twelve numbers on two rings, and 240 tracks cover all twenty-four.
        self.assertEqual(len(keys), 24)
        self.assertEqual({node['detail']['ring'] for node in keys}, {'A', 'B'})
        self.assertEqual({node['detail']['wheel_index'] for node in keys}, set(range(1, 13)))
        for node in keys:
            index, ring = node['detail']['wheel_index'], node['detail']['ring']
            other = 'B' if ring == 'A' else 'A'
            neighbours = [((index - 2) % 12 + 1, ring), ((index % 12) + 1, ring),
                          (index, other)]
            self.assertEqual(node['detail']['harmonic_ids'],
                             [f'key:{number}{neighbour_ring}'.lower()
                              for number, neighbour_ring in neighbours])
        whole_wheel = {f'{index}{ring}' for index in range(1, 13) for ring in 'AB'}
        for node in self.nodes_of(payload, 'genre'):
            self.assertIn(node['detail']['primary_key'], whole_wheel)


if __name__ == '__main__':
    unittest.main()
