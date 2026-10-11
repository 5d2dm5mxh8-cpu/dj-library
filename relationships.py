"""The relationship graph behind the right-hand rail.

Every layer of the library is one node type -- genre, tag, artist, album,
crate, key and transition -- and an edge means two of those values ride the
same track. Weights are counted from the database, never invented: a genre
node's weight is how many tracks carry it, an edge's weight is how many tracks
the two values share, and every finding is a statement about those counts.

Nothing here knows about Flask or about drawing. The HTTP boundary lives in
relationships_routes.py, the layout math in static/relationships-layout.js, so
the graph can be built and asserted on plain rows.

Reads only: this module never writes to the library.
"""
import re
import sqlite3

# Node types, in the order the rail draws and lists them. Low cardinality
# first, so the columns a DJ reads left-to-right go genre -> tag -> ... ->
# transition.
LAYERS = ('genre', 'tag', 'artist', 'album', 'crate', 'key', 'transition')
LAYER_LABELS = {
    'genre': 'Genres', 'tag': 'Tags', 'artist': 'Artists', 'album': 'Albums',
    'crate': 'Crates', 'key': 'Keys', 'transition': 'Transitions',
}
ARRANGEMENTS = ('clusters', 'wheel')

# Layers where a single-track value is the norm rather than the exception (605
# tracks over 523 artists): giving every one-off its own node would bury the
# graph in leaves, so a value needs this many tracks before it earns a node.
# The one-offs are not hidden -- they are counted in a finding instead.
MIN_WEIGHT = {'artist': 2, 'album': 2}

# Caps. The point is a legible graph on a library of any size, not a dump of
# every pair: the nodes that did not fit are reported as omitted counts so the
# rail can say so out loud.
NODE_LIMIT = 120          # per layer, heaviest first
EDGE_LIMIT = 420
SONG_ID_LIMIT = 200       # ids carried per node / finding (counts are exact)
FINDING_LIMIT = 9

# A genre like "70" is a filing accident, not a genre. This is a shape rule,
# not a list of the user's values: all-digits, punctuation-only, or one letter.
_JUNK_GENRE = re.compile(r'^(\d+|[^\w]+|.)$')

_SEVERITY_ORDER = {'warn': 0, 'info': 1}


def parse_layers(value):
    """The layers a request asked for, in LAYERS order.

    Accepts a comma/space separated string or any iterable. Absent (None or
    empty) means every layer; unknown names are dropped rather than guessed.
    """
    if value is None:
        return list(LAYERS)
    if isinstance(value, str):
        parts = [p for p in re.split(r'[,\s]+', value.strip()) if p]
    else:
        try:
            parts = [str(p).strip() for p in value]
        except TypeError:
            return list(LAYERS)
    if not parts:
        return list(LAYERS)
    wanted = {p.lower() for p in parts if p}
    return [layer for layer in LAYERS if layer in wanted]


def parse_arrangement(value):
    """'wheel' or 'clusters' -- anything else falls back to clusters."""
    name = str(value or '').strip().lower()
    return name if name in ARRANGEMENTS else 'clusters'


def slug(text):
    """'Peak Hour!' -> 'peak-hour'. Stable ids for node identity."""
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', str(text or '').lower())).strip('-')


def camelot_slot(value):
    """'6A' -> (6, 'A'); anything else -> None."""
    match = re.match(r'^(\d{1,2})([AB])$', str(value or '').strip().upper())
    if not match:
        return None
    number = int(match.group(1))
    return (number, match.group(2)) if 1 <= number <= 12 else None


def harmonic_slots(slot):
    """The keys that mix cleanly into this one: the two neighbours on the same
    ring, and the same number on the other ring (relative major/minor)."""
    number, ring = slot
    other = 'B' if ring == 'A' else 'A'
    return [
        ((number - 2) % 12 + 1, ring),
        (number % 12 + 1, ring),
        (number, other),
    ]


class Relationships:
    """Builds the relationship payload from the library's rows.

    `get_db` and `to_camelot` are injected so tests can run against a scratch
    database; `crate_song_ids` resolves a crate (smart crates evaluate their
    rules) and `key_display` says whether the user reads keys as '6A' or as
    'G min'.
    """

    def __init__(self, get_db, to_camelot, crate_song_ids, key_display=lambda: 'camelot',
                 node_limit=NODE_LIMIT, edge_limit=EDGE_LIMIT):
        self.get_db = get_db
        self.to_camelot = to_camelot
        self.crate_song_ids = crate_song_ids
        self.key_display = key_display
        self.node_limit = node_limit
        self.edge_limit = edge_limit

    # -- data ---------------------------------------------------------------

    @staticmethod
    def _read(db, sql):
        """A read that tolerates a database which has not been through init_db
        yet -- the tags tables only exist after the app's first start on that
        file, and a rail that 500s because of it helps nobody. A missing table
        means that layer is empty, which is reported honestly."""
        try:
            return [dict(row) for row in db.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            return []

    def _rows(self):
        """One pass over the library. The whole catalogue is a few thousand
        rows, so reading it once and folding it in Python beats a dozen
        GROUP BY queries that each have to split multi-value columns again."""
        with self.get_db() as db:
            songs = self._read(db, 'SELECT id, title, artist, album, genre, '
                                   'musical_key, bpm FROM songs ORDER BY id')
            transitions = self._read(db, 'SELECT from_song_id, to_song_id, to_text, notes, tag, '
                                         "status FROM transitions WHERE status='confirmed' "
                                         'ORDER BY from_song_id, id')
            crates = self._read(db, 'SELECT id, name, is_smart FROM crates '
                                    'ORDER BY sort_order, name, id')
            tagged = self._read(db, 'SELECT st.song_id, t.name FROM song_tags st '
                                    'JOIN tags t ON t.id = st.tag_id '
                                    'ORDER BY lower(t.name)')
        return songs, transitions, crates, tagged

    def _per_song_values(self, songs, crates, tagged):
        """song id -> [(layer, label), ...]: everything each track carries."""
        tags_by_song = {}
        for row in tagged:
            tags_by_song.setdefault(row['song_id'], []).append(row['name'])
        crates_by_song = {}
        for crate in crates:
            for song_id in self.crate_song_ids(crate['id']) or []:
                crates_by_song.setdefault(song_id, []).append(crate['name'])

        values = {}
        for song in songs:
            items = []
            for token in str(song.get('genre') or '').split(';'):
                token = token.strip()
                # Genre is a genre: the '#' marker is the tag field's business
                # and a leftover one is not a genre value.
                if token and not token.startswith('#'):
                    items.append(('genre', token))
            for tag in tags_by_song.get(song['id'], []):
                items.append(('tag', tag))
            for layer, field in (('artist', 'artist'), ('album', 'album')):
                label = str(song.get(field) or '').strip()
                if label:
                    items.append((layer, label))
            for name in crates_by_song.get(song['id'], []):
                items.append(('crate', name))
            slot = camelot_slot(self.to_camelot(song.get('musical_key')))
            if slot:
                items.append(('key', f'{slot[0]}{slot[1]}'))
            values[song['id']] = items
        return values

    def _transition_pairs(self, transitions):
        """(from_id, to_id) for links between two library tracks, plus how many
        links each track takes part in. Text-only targets have no node to link
        to -- they are counted, not invented."""
        pairs, degree, text_only = [], {}, 0
        for row in transitions:
            source = row.get('from_song_id')
            target = row.get('to_song_id')
            degree[source] = degree.get(source, 0) + 1
            if not target:
                text_only += 1
                continue
            degree[target] = degree.get(target, 0) + 1
            pairs.append((source, target, row))
        return pairs, degree, text_only

    # -- payload ------------------------------------------------------------

    def payload(self, layers=None, arrangement='clusters'):
        """The whole graph: nodes, edges, hubs, findings and honest counts.

        `layers` may be a name list (what the route passes after parsing) or a
        raw string, in which case it is parsed here. A list is taken at face
        value -- so `[]` really does mean 'no layers', while an absent value
        still means every layer.
        """
        if isinstance(layers, (list, tuple)):
            wanted = {str(layer).strip().lower() for layer in layers}
            selected = [layer for layer in LAYERS if layer in wanted]
        else:
            selected = parse_layers(layers)
        arrangement = parse_arrangement(arrangement)
        songs, transitions, crates, tagged = self._rows()
        values = self._per_song_values(songs, crates, tagged)
        pairs, trans_degree, text_only = self._transition_pairs(transitions)

        buckets = {layer: {} for layer in LAYERS}      # layer -> label -> songs
        for song_id, items in values.items():
            for layer, label in items:
                buckets[layer].setdefault(label, {'songs': []})['songs'].append(song_id)

        nodes, node_ids, omitted, layer_options = self._nodes(
            buckets, selected, songs, trans_degree,
            {crate['name']: crate for crate in crates})
        edges, edge_omitted = self._edges(values, node_ids, nodes)
        if 'transition' in selected:
            edges, trans_edges = self._transition_edges(pairs, node_ids, nodes, songs, edges)
        else:
            trans_edges = 0

        self._attach_harmonic(nodes, node_ids)
        self._attach_primary_key(nodes, values, songs)
        isolated = sum(1 for node in nodes if not node['degree'])
        hubs = sorted(nodes, key=lambda n: (-n['weight'], -n['degree'], n['id']))[:6]

        findings = self._findings(
            songs, buckets, crates, transitions, trans_degree, text_only,
            selected, nodes, isolated)

        return {
            'layers': selected,
            'arrangement': arrangement,
            'layer_options': layer_options,
            'nodes': nodes,
            'edges': edges,
            'hubs': [self._node_summary(n) for n in hubs],
            'findings': findings,
            'stats': {
                'songs': len(songs),
                'nodes': len(nodes),
                'edges': len(edges),
                'isolated': isolated,
                'transition_links': trans_edges,
                'text_only_transitions': text_only,
                'omitted_nodes': sum(omitted.values()),
                'omitted_edges': edge_omitted,
                'node_limit': self.node_limit,
                'edge_limit': self.edge_limit,
            },
        }

    def _nodes(self, buckets, selected, songs, trans_degree, crate_meta=None):
        """Value nodes for every selected layer, heaviest first, capped per
        layer. The cap is reported, not hidden."""
        songs_by_id = {s['id']: s for s in songs}
        nodes, node_ids, omitted, layer_options = [], {}, {}, []
        for layer in LAYERS:
            entries = buckets[layer]
            if layer == 'transition':
                entries = {
                    self._song_label(songs_by_id[song_id]): {'songs': [song_id]}
                    for song_id in trans_degree if song_id in songs_by_id
                }
            ranked = sorted(entries.items(),
                            key=lambda kv: (-len(kv[1]['songs']), kv[0].lower(), kv[0]))
            min_weight = MIN_WEIGHT.get(layer, 1)
            eligible = [(label, data) for label, data in ranked
                        if len(data['songs']) >= min_weight]
            layer_options.append({
                'key': layer, 'label': LAYER_LABELS[layer],
                'nodes': len(eligible), 'values': len(entries),
                'low_weight': len(ranked) - len(eligible),
                'empty': not entries,
            })
            if layer not in selected:
                continue
            kept = eligible[:self.node_limit]
            omitted[layer] = len(eligible) - len(kept)
            for label, data in kept:
                song_ids = sorted(data['songs'])
                node = self._make_node(layer, label, song_ids, songs_by_id, trans_degree,
                                       crate_meta or {})
                node_ids[node['id']] = node
                nodes.append(node)
        return nodes, node_ids, omitted, layer_options

    def _make_node(self, layer, label, song_ids, songs_by_id, trans_degree, crate_meta):
        base = f'{layer}:{slug(label)}' or f'{layer}:x'
        node = {
            'id': base, 'type': layer, 'label': label,
            'weight': len(song_ids), 'degree': 0,
            'song_ids': song_ids[:SONG_ID_LIMIT],
            'songs': len(song_ids),
            'truncated_songs': max(0, len(song_ids) - SONG_ID_LIMIT),
            'detail': {},
        }
        if layer == 'key':
            slot = camelot_slot(label)
            notation = self._key_notation(songs_by_id.values(), label)
            # The Camelot spelling is the node's identity (the wheel needs it),
            # but a user who reads keys as 'G min' sees that reading as the label.
            if str(self.key_display() or 'camelot').lower() != 'camelot' and notation:
                node['label'] = notation
            node['detail'] = {
                'camelot': label,
                'wheel_index': slot[0] if slot else None,
                'ring': slot[1] if slot else None,
                'notation': notation,
                'harmonic_ids': [],
            }
        elif layer == 'crate':
            crate = crate_meta.get(label) or {}
            node['detail'] = {
                'crate_name': label,
                'crate_id': crate.get('id'),
                'smart': bool(crate.get('is_smart')),
            }
        elif layer == 'transition':
            song = songs_by_id.get(song_ids[0]) if song_ids else None
            node['detail'] = {
                'song_id': song_ids[0] if song_ids else None,
                'title': (song or {}).get('title'),
                'artist': (song or {}).get('artist'),
                'links': trans_degree.get(song_ids[0], 0) if song_ids else 0,
            }
        elif layer == 'genre':
            node['detail'] = {'genre': label, 'junk': bool(_JUNK_GENRE.match(label))}
        return node

    @staticmethod
    def _song_label(song):
        title = (song.get('title') or song.get('filename') or 'Untitled').strip()
        artist = (song.get('artist') or '').strip()
        return f'{title} — {artist}' if artist else title

    def _key_notation(self, songs, camelot):
        """The stored spelling behind a Camelot label ('G min'), so the rail can
        show both readings of the same key."""
        for song in songs:
            slot = camelot_slot(self.to_camelot(song.get('musical_key')))
            if slot and f'{slot[0]}{slot[1]}' == camelot:
                return song.get('musical_key')
        return None

    def _edges(self, values, node_ids, nodes):
        """One edge per pair of values that share tracks. Both endpoints must be
        drawn nodes, and the strongest pairs win the cap."""
        shared = {}
        for song_id, items in values.items():
            drawn = [(layer, label) for layer, label in items
                     if f'{layer}:{slug(label)}' in node_ids]
            for i, (layer_a, label_a) in enumerate(drawn):
                for layer_b, label_b in drawn[i + 1:]:
                    if layer_a == layer_b:
                        continue        # same layer: two genres on one track is co-listing, not a relationship
                    id_a, id_b = f'{layer_a}:{slug(label_a)}', f'{layer_b}:{slug(label_b)}'
                    key = (id_a, id_b) if id_a <= id_b else (id_b, id_a)
                    shared[key] = shared.get(key, 0) + 1

        ranked = sorted(
            ((key, count) for key, count in shared.items()),
            key=lambda kv: (-kv[1], -min(node_ids[kv[0][0]]['weight'],
                                        node_ids[kv[0][1]]['weight']), kv[0]))
        kept, omitted = ranked[:self.edge_limit], max(0, len(ranked) - self.edge_limit)
        edges = []
        for (id_a, id_b), weight in kept:
            layer_a, layer_b = node_ids[id_a]['type'], node_ids[id_b]['type']
            edges.append({
                'id': f'{id_a}|{id_b}',
                'source': id_a, 'target': id_b,
                'type': '-'.join(sorted((layer_a, layer_b))),
                'weight': weight,
            })
            node_ids[id_a]['degree'] += 1
            node_ids[id_b]['degree'] += 1
        return edges, omitted

    def _transition_edges(self, pairs, node_ids, nodes, songs, edges):
        """Mix links between two library tracks, drawn between their transition
        nodes. Notes/tags travel on the edge so the rail can show why the link
        is there."""
        songs_by_id = {s['id']: s for s in songs}
        grouped = {}
        for source, target, row in pairs:
            if source not in songs_by_id or target not in songs_by_id:
                continue
            id_a = f'transition:{slug(self._song_label(songs_by_id[source]))}'
            id_b = f'transition:{slug(self._song_label(songs_by_id[target]))}'
            if id_a not in node_ids or id_b not in node_ids or id_a == id_b:
                continue
            key = (id_a, id_b) if id_a <= id_b else (id_b, id_a)
            entry = grouped.setdefault(key, {'weight': 0, 'notes': [], 'tags': set()})
            entry['weight'] += 1
            if row.get('notes'):
                entry['notes'].append(row['notes'])
            if row.get('tag'):
                entry['tags'].add(row['tag'])
        for (id_a, id_b), entry in sorted(grouped.items()):
            edges.append({
                'id': f'{id_a}|{id_b}',
                'source': id_a, 'target': id_b,
                'type': 'transition-transition',
                'weight': entry['weight'],
                'detail': {'notes': entry['notes'][:4], 'tags': sorted(entry['tags'])},
            })
            node_ids[id_a]['degree'] += 1
            node_ids[id_b]['degree'] += 1
        return edges, len(grouped)

    def _attach_harmonic(self, nodes, node_ids):
        """Points every key node at the keys it mixes into, so the wheel can
        draw the neighbours and the rail can say which shelf is empty."""
        for node in nodes:
            if node['type'] != 'key':
                continue
            slot = camelot_slot(node['label'])
            if not slot:
                continue
            node['detail']['harmonic_ids'] = [
                f'key:{slug(f"{number}{ring}")}'
                for number, ring in harmonic_slots(slot)
                if f'key:{slug(f"{number}{ring}")}' in node_ids
            ]

    def _attach_primary_key(self, nodes, values, songs):
        """The key a node's tracks mostly sit in -- what lets the wheel arrange
        genres/artists around the key ring instead of at random."""
        keys_by_song = {}
        for song in songs:
            slot = camelot_slot(self.to_camelot(song.get('musical_key')))
            if slot:
                keys_by_song[song['id']] = f'{slot[0]}{slot[1]}'
        for node in nodes:
            counts = {}
            if node['type'] == 'key':
                node['detail']['primary_key'] = node['label']
                continue
            for song_id in (node['song_ids'] if node['type'] != 'transition' else []):
                key = keys_by_song.get(song_id)
                if key:
                    counts[key] = counts.get(key, 0) + 1
            node['detail']['primary_key'] = (
                None if not counts else
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0])

    @staticmethod
    def _node_summary(node):
        return {'id': node['id'], 'type': node['type'], 'label': node['label'],
                'weight': node['weight'], 'degree': node['degree'],
                'songs': node['songs'], 'detail': node['detail']}

    # -- findings -----------------------------------------------------------

    def _findings(self, songs, buckets, crates, transitions, trans_degree,
                  text_only, selected, nodes, isolated):
        """What the counts actually say. Every finding is computed from the
        rows above; several carry a `song_ids` list so the panel can act on
        them instead of just describing them."""
        findings = []
        total = len(songs)

        def add(identifier, severity, title, detail, metric,
                song_ids=None, node_ids=None, action=None, actions=None):
            # A finding may need two buttons (a crate finding both selects its
            # tracks and takes you to the crates); `action` stays the primary
            # one so a panel that renders a single button keeps working.
            buttons = list(actions) if actions else ([action] if action else [])
            findings.append({
                'id': identifier, 'severity': severity, 'title': title,
                'detail': detail, 'metric': metric,
                'song_ids': sorted(set(song_ids or []))[:SONG_ID_LIMIT],
                'node_ids': list(node_ids or [])[:SONG_ID_LIMIT],
                'action': buttons[0] if buttons else None,
                'actions': buttons,
            })

        # Genres: the shape of the collection.
        genres = buckets['genre']
        if genres:
            top = sorted(genres.items(), key=lambda kv: (-len(kv[1]['songs']), kv[0]))[:2]
            top_share = sum(len(v['songs']) for _, v in top) / total if total else 0
            if len(top) >= 2 and top_share >= 0.2:
                add('genre-concentration', 'info',
                    f'{top[0][0]} and {top[1][0]} hold {round(top_share * 100)}% of the library',
                    f'{len(top[0][1]["songs"])} + {len(top[1][1]["songs"])} of {total} tracks. '
                    'Two shelves carry the whole collection — everything else is a long tail.',
                    top_share, node_ids=[f'genre:{slug(top[0][0])}', f'genre:{slug(top[1][0])}'],
                    action={'kind': 'focus', 'label': 'Show both in the rail',
                            'target': f'genre:{slug(top[0][0])}'})

        singles = [label for label, data in genres.items() if len(data['songs']) == 1]
        if singles:
            add('single-track-genres', 'warn',
                f'{len(singles)} genres carry a single track',
                'A genre used once is usually a flavour, not a genre: "'
                + '", "'.join(sorted(singles)[:4]) + '". Moving them to tags keeps the '
                'Genre column readable in Mixxx without losing the word.',
                len(singles), song_ids=[s for label in singles for s in genres[label]['songs']],
                node_ids=[f'genre:{slug(label)}' for label in singles],
                action={'kind': 'open', 'label': 'Review tags vs genres', 'target': 'tag-review'})

        junk = [label for label in genres if _JUNK_GENRE.match(label)]
        if junk:
            add('junk-genres', 'warn',
                f'{len(junk)} genre values are filing accidents',
                'These read as numbers or punctuation, not genres: '
                + ', '.join(f'"{label}"' for label in sorted(junk)[:6]) + '.',
                len(junk), song_ids=[s for label in junk for s in genres[label]['songs']],
                node_ids=[f'genre:{slug(label)}' for label in junk],
                action={'kind': 'select-songs', 'label': 'Select those tracks',
                        'target': 'junk-genres'})

        no_genre = [s['id'] for s in songs if not str(s.get('genre') or '').strip()]
        if no_genre:
            add('genreless', 'warn', f'{len(no_genre)} tracks have no genre',
                'They show as blank in Mixxx\'s Genre column and cannot be filtered or '
                'matched by a smart crate rule.',
                len(no_genre), song_ids=no_genre,
                action={'kind': 'select-songs', 'label': 'Select those tracks',
                        'target': 'genreless'})

        # Tags: the newest layer, so it is honestly empty on most libraries.
        tag_count = len(buckets['tag'])
        if tag_count == 0:
            add('no-tags', 'info', 'No tags yet',
                'Tags are separate from genres now and travel in each file\'s comment '
                'block. Anything with a # in front is a tag when it arrives.',
                0, action={'kind': 'open', 'label': 'Review tags vs genres',
                           'target': 'tag-review'})

        # Crates.
        if not crates:
            add('no-crates', 'info', 'No crates yet',
                'Crates are how you group tracks for a set; the rail draws them as '
                'their own node type once they exist.',
                0, action={'kind': 'open', 'label': 'Go to crates', 'target': 'crates'})
        else:
            thin = []
            for crate in crates:
                ids = self.crate_song_ids(crate['id']) or []
                if len(ids) < 3:
                    thin.append((crate['name'], len(ids), ids))
            if thin:
                add('thin-crates', 'info',
                    f'{len(thin)} crates hold fewer than 3 tracks',
                    ', '.join(f'{name} ({count})' for name, count, _ in thin[:6])
                    + ' — worth filling or folding together.',
                    len(thin), song_ids=[s for _, _, ids in thin for s in ids],
                    # The crates themselves, so a busy rail can point at them.
                    node_ids=[f'crate:{slug(name)}' for name, _, _ in thin],
                    action={'kind': 'select-songs', 'label': 'Select those tracks',
                            'target': 'thin-crates'},
                    actions=[
                        {'kind': 'select-songs', 'label': 'Select those tracks',
                         'target': 'thin-crates'},
                        {'kind': 'open', 'label': 'Go to crates', 'target': 'crates'},
                    ])

        # Transitions.
        if not transitions:
            add('no-transitions', 'info', 'No transitions yet',
                'A transition is a mix you have tested: it becomes an edge between two '
                'tracks here, and rides along in the file comment and the XML export.',
                0, action={'kind': 'open', 'label': 'Go to transitions', 'target': 'transitions'})
        elif text_only:
            # The tracks whose mix leaves the library: they have a transition
            # node even though the target has none, so the finding can name
            # them and the panel can select them.
            outside = [row.get('from_song_id') for row in transitions
                       if not row.get('to_song_id')]
            songs_by_id = {s['id']: s for s in songs}
            out_nodes = [f'transition:{slug(self._song_label(songs_by_id[song_id]))}'
                         for song_id in outside if song_id in songs_by_id]
            add('text-only-transitions', 'info',
                f'{text_only} transitions point outside the library',
                'They record a mix into a track you do not own, so there is no second '
                'node to draw — the note still exports.',
                text_only, song_ids=outside, node_ids=out_nodes,
                action={'kind': 'open', 'label': 'Go to transitions',
                        'target': 'transitions'},
                actions=[
                    {'kind': 'open', 'label': 'Go to transitions',
                     'target': 'transitions'},
                    {'kind': 'select-songs', 'label': 'Select those tracks',
                     'target': 'text-only-transitions'},
                ])

        # Keys: coverage and the shelves that mix nowhere.
        keyed = buckets['key']
        unkeyed = [s['id'] for s in songs if not s.get('musical_key')]
        if unkeyed:
            add('no-key', 'warn', f'{len(unkeyed)} tracks have no key',
                'Without a key the Camelot wheel and harmonic findings stay half-blind.',
                len(unkeyed), song_ids=unkeyed,
                action={'kind': 'run-analysis', 'label': 'Analyse BPM & key',
                        'target': 'analyze-bpm'})
        islands = []
        for label, data in keyed.items():
            if len(data['songs']) < 8:
                continue
            slot = camelot_slot(label)
            if not slot:
                continue
            neighbours = [f'{number}{ring}' for number, ring in harmonic_slots(slot)]
            if not any(neighbour in keyed for neighbour in neighbours):
                islands.append((label, len(data['songs']), data['songs']))
        if islands:
            islands.sort(key=lambda item: -item[1])
            add('key-islands', 'warn',
                f'{len(islands)} keys hold tracks that mix nowhere',
                ', '.join(f'{label} ({count})' for label, count, _ in islands[:5])
                + ' — no tracks in their neighbouring Camelot keys, so a set built '
                  'there cannot move without a jump.',
                len(islands), song_ids=[s for _, _, ids in islands for s in ids],
                node_ids=[f'key:{slug(label)}' for label, _, _ in islands],
                action={'kind': 'select-songs', 'label': 'Select those tracks',
                        'target': 'key-islands'})

        # Artists: why the artist layer looks thin.
        one_hit = [label for label, data in buckets['artist'].items()
                   if len(data['songs']) == 1]
        if one_hit:
            add('one-hit-artists', 'info',
                f'{len(one_hit)} artists appear on exactly one track',
                f'They are the long tail of {len(songs)} tracks, so the artist layer '
                'draws only artists with two or more — the rest stay in the table.',
                len(one_hit), song_ids=[s for label in one_hit
                                        for s in buckets['artist'][label]['songs']])

        if isolated and nodes:
            add('isolated-nodes', 'info',
                f'{isolated} nodes share no track with anything else',
                'They sit alone in the graph: a value nothing else in the library '
                'overlaps with.',
                isolated, node_ids=[n['id'] for n in nodes if not n['degree']])

        findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f['severity'], 9), -f['metric'], f['id']))
        return findings[:FINDING_LIMIT]
