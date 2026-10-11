/* Geometry and parsing for the relationships rail -- no DOM, no app state.
 *
 * The backend (relationships.py) counts the graph: which genres, tags, artists,
 * albums, crates, keys and transitions exist, how heavy each one is, and which
 * pairs share tracks. Everything here turns that payload into coordinates and
 * decides what the panel is allowed to do -- rail width, which layers are on,
 * which node the pointer is over. Keeping it pure means the layout can be
 * asserted in tests/test_relationships_ui.js instead of only being eyeballed.
 */

// Declared with `var` so the plain-script globals are also reachable from the
// vm sandbox the frontend tests run in (a top-level `const` is not).
var REL_LAYERS = ['genre', 'tag', 'artist', 'album', 'crate', 'key', 'transition'];
var REL_LAYER_LABELS = {
  genre: 'Genres', tag: 'Tags', artist: 'Artists', album: 'Albums',
  crate: 'Crates', key: 'Keys', transition: 'Transitions',
};
var REL_WIDTH_MIN = 280;
var REL_WIDTH_MAX = 720;
var REL_WIDTH_DEFAULT = 420;
var REL_NODE_MIN = 3.5;
var REL_NODE_MAX = 22;
var REL_LABEL_RADIUS = 8;        // a node this big is worth a text label

// ── layers and rail state ────────────────────────────────────────────────────

// 'genre, tag' -> ['genre','tag']: unknown names are dropped rather than
// guessed, and the result keeps the canonical layer order so the legend never
// reshuffles between renders.
function relParseLayers(value, all) {
  const known = all || REL_LAYERS;
  if (value == null) return known.slice();
  const parts = Array.isArray(value) ? value : String(value).split(/[,\s]+/);
  const wanted = new Set(parts.map(p => String(p).trim().toLowerCase()).filter(Boolean));
  if (!wanted.size) return known.slice();
  return known.filter(layer => wanted.has(layer));
}

function relLayerCsv(layers) {
  return relParseLayers(layers, REL_LAYERS).join(',');
}

// Add or remove one layer, keeping canonical order. Removing the last layer is
// refused: an empty rail is not a view, it is a bug the user cannot undo.
function relToggleLayer(layers, layer, all) {
  const shown = relParseLayers(layers, all);
  if (!REL_LAYERS.includes(layer)) return shown;
  if (shown.includes(layer)) {
    return shown.length > 1 ? shown.filter(l => l !== layer) : shown;
  }
  return relParseLayers(shown.concat([layer]), all);
}

// No value at all (a missing stored width) keeps the default; anything else is
// a real width and gets clamped. `Number(null)` is 0, so null has to be
// rejected before the clamp -- otherwise 'no width saved' would mean 'as
// narrow as the rail can go'.
function relClampWidth(px) {
  if (px === null || px === undefined || px === '') return REL_WIDTH_DEFAULT;
  const width = Math.round(Number(px));
  if (!isFinite(width)) return REL_WIDTH_DEFAULT;
  return Math.min(REL_WIDTH_MAX, Math.max(REL_WIDTH_MIN, width));
}

// The rail has exactly three states; the buttons are the only way between them.
// 'toggle' means the header button: closed -> open, anything else -> closed.
function relNextRailState(state, action) {
  const current = ['hidden', 'open', 'max'].includes(state) ? state : 'open';
  if (action === 'maximise') return current === 'max' ? 'open' : 'max';
  if (action === 'hide') return 'hidden';
  if (action === 'open') return 'open';
  return current === 'hidden' ? 'open' : 'hidden';
}

function relParseArrangement(value) {
  return String(value || '').trim().toLowerCase() === 'wheel' ? 'wheel' : 'clusters';
}

// ── Camelot wheel ────────────────────────────────────────────────────────────

// '6A' -> {index:6, ring:'A'}; anything else -> null.
function relCamelotSlot(label) {
  const match = /^(\d{1,2})\s*([AB])$/i.exec(String(label || '').trim());
  if (!match) return null;
  const index = parseInt(match[1], 10);
  if (!(index >= 1 && index <= 12)) return null;
  return { index, ring: match[2].toUpperCase() };
}

// Camelot numbers run clockwise from 12 o'clock; ring A is minor, B is major.
// index 12 sits at the top, index 1 one step clockwise from it.
function relWheelAngle(index, ring) {
  const step = (Number(index) % 12) * (Math.PI * 2 / 12);
  return step - Math.PI / 2;
}

// The keys that mix cleanly into this one: same ring either side, and the
// relative major/minor (same number, other ring).
function relHarmonicPartners(label) {
  const slot = relCamelotSlot(label);
  if (!slot) return [];
  const other = slot.ring === 'A' ? 'B' : 'A';
  const prev = ((slot.index - 2 + 12) % 12) + 1;
  const next = (slot.index % 12) + 1;
  return [`${prev}${slot.ring}`, `${next}${slot.ring}`, `${slot.index}${other}`];
}

// ── weights and geometry ─────────────────────────────────────────────────────

function relDegreeMap(edges) {
  const degrees = {};
  for (const edge of edges || []) {
    if (!edge) continue;
    degrees[edge.source] = (degrees[edge.source] || 0) + 1;
    degrees[edge.target] = (degrees[edge.target] || 0) + 1;
  }
  return degrees;
}

// Area-proportional sizing: a node with 4x the tracks looks twice as wide, not
// four times, so one huge hub cannot swallow the panel.
function relNodeRadius(weight, maxWeight, min, max) {
  const low = min == null ? REL_NODE_MIN : min;
  const high = max == null ? REL_NODE_MAX : max;
  const peak = Math.max(1, Number(maxWeight) || 1);
  const value = Math.max(0, Number(weight) || 0);
  const scaled = low + (high - low) * Math.sqrt(value / peak);
  return Math.max(low, Math.min(high, scaled));
}

function relMaxWeight(nodes) {
  return (nodes || []).reduce((peak, node) => Math.max(peak, Number(node && node.weight) || 0), 0);
}

function relGroupByLayer(nodes) {
  const groups = {};
  for (const layer of REL_LAYERS) groups[layer] = [];
  for (const node of nodes || []) {
    if (!node || !groups[node.type]) continue;
    groups[node.type].push(node);
  }
  for (const layer of REL_LAYERS) {
    groups[layer].sort((a, b) => (b.weight - a.weight) || String(a.label).localeCompare(String(b.label)));
  }
  return groups;
}

// Deterministic spiral inside a cluster box: heaviest node at the centre.
function relPlaceCluster(items, centre, radius) {
  const placed = {};
  items.forEach((node, i) => {
    if (i === 0) { placed[node.id] = { x: centre.x, y: centre.y }; return; }
    const angle = i * 2.399963229728653;               // golden angle: spreads evenly
    const ring = radius * Math.sqrt(i / Math.max(1, items.length));
    placed[node.id] = {
      x: centre.x + Math.cos(angle) * Math.min(ring, radius),
      y: centre.y + Math.sin(angle) * Math.min(ring, radius),
    };
  });
  return placed;
}

// Clusters: one region per layer, laid out on a grid of cells and sized by how
// many nodes the layer holds, nodes spiralling outward from the heaviest.
function relLayoutClusters(nodes, edges, width, height, opts) {
  const options = opts || {};
  const groups = relGroupByLayer(nodes);
  const active = REL_LAYERS.filter(layer => groups[layer].length);
  const maxWeight = options.maxWeight || relMaxWeight(nodes);
  const sizes = {};
  for (const node of nodes || []) sizes[node.id] = relNodeRadius(node.weight, maxWeight);

  const columns = Math.max(1, Math.ceil(Math.sqrt(active.length)));
  const rows = Math.max(1, Math.ceil(active.length / columns));
  const cellW = width / columns;
  const cellH = height / rows;
  const layout = {};
  const anchors = {};

  active.forEach((layer, i) => {
    const cell = { x: (i % columns) * cellW, y: Math.floor(i / columns) * cellH };
    const centre = { x: cell.x + cellW / 2, y: cell.y + cellH / 2 + 10 };
    const items = groups[layer];
    const radius = Math.max(24, Math.min(cellW, cellH) / 2 - 14);
    Object.assign(layout, relPlaceCluster(items, centre, radius));
    anchors[layer] = { x: cell.x + cellW / 2, y: cell.y + 12, count: items.length, radius };
  });

  for (const id of Object.keys(layout)) layout[id].r = sizes[id];
  return { positions: layout, anchors, arrangement: 'clusters' };
}

// Wheel: the twelve Camelot keys on the wheel (ring A inside, B outside) and
// every other node orbiting the key its tracks mostly sit in -- so a genre's
// distance from the ring is real, not decorative.
function relLayoutWheel(nodes, edges, width, height, opts) {
  const options = opts || {};
  const maxWeight = options.maxWeight || relMaxWeight(nodes);
  const centre = { x: width / 2, y: height / 2 };
  const base = Math.max(40, Math.min(width, height) / 2 - 18);
  const ringA = base * 0.52;
  const ringB = base * 0.72;
  const orbit = base;
  const layout = {};
  const anchors = {};
  const sizes = {};
  for (const node of nodes || []) sizes[node.id] = relNodeRadius(node.weight, maxWeight);

  const keys = [];
  const others = [];
  for (const node of nodes || []) {
    if (!node) continue;
    const slot = node.type === 'key'
      ? (relCamelotSlot(node.detail && node.detail.camelot) || relCamelotSlot(node.label))
      : null;
    if (slot) keys.push({ node, slot });
    else others.push(node);
  }

  for (const entry of keys) {
    const angle = relWheelAngle(entry.slot.index, entry.slot.ring);
    const radius = entry.slot.ring === 'A' ? ringA : ringB;
    layout[entry.node.id] = {
      x: centre.x + Math.cos(angle) * radius,
      y: centre.y + Math.sin(angle) * radius,
      r: sizes[entry.node.id],
    };
    anchors[entry.node.id] = { angle, radius };
  }

  // Orbits, heaviest first, grouped so nodes sharing a key stay near each other.
  const orbitGroups = {};
  const centreNodes = [];
  for (const node of others) {
    const key = relCamelotSlot((node.detail && node.detail.primary_key) || '');
    if (!key) { centreNodes.push(node); continue; }
    const bucket = `${key.index}${key.ring}`;
    (orbitGroups[bucket] = orbitGroups[bucket] || []).push(node);
  }
  const orbitKeys = Object.keys(orbitGroups).sort((a, b) => {
    const left = relCamelotSlot(a), right = relCamelotSlot(b);
    return (left.index - right.index) || left.ring.localeCompare(right.ring);
  });
  orbitKeys.forEach((bucket, bucketIndex) => {
    const slot = relCamelotSlot(bucket);
    const items = orbitGroups[bucket]
      .sort((a, b) => (b.weight - a.weight) || String(a.label).localeCompare(String(b.label)));
    const spread = Math.min(0.34, Math.max(0.06, 1.1 / Math.max(1, items.length)));
    items.forEach((node, i) => {
      const angle = relWheelAngle(slot.index, slot.ring)
        + (i === 0 ? 0 : ((i + 1) / 2) * spread * (i % 2 ? 1 : -1))
        + bucketIndex * 0.0001;
      const radius = orbit - (i >= 4 ? 14 : 0);
      layout[node.id] = {
        x: centre.x + Math.cos(angle) * radius,
        y: centre.y + Math.sin(angle) * radius,
        r: sizes[node.id],
      };
    });
  });

  // Nothing to orbit: keys missing, or tracks without one. Parked in the hub.
  centreNodes.sort((a, b) => (b.weight - a.weight) || String(a.label).localeCompare(String(b.label)));
  Object.assign(layout, relPlaceCluster(centreNodes, { x: centre.x, y: centre.y + 6 }, 26));
  for (const node of centreNodes) {
    if (layout[node.id]) layout[node.id].r = sizes[node.id];
    anchors[node.id] = { angle: 0, radius: 0 };
  }

  return { positions: layout, anchors, arrangement: 'wheel', centre, rings: { ringA, ringB, orbit } };
}

function relLayoutGraph(nodes, edges, arrangement, width, height, opts) {
  const safeWidth = Math.max(80, Number(width) || 0);
  const safeHeight = Math.max(80, Number(height) || 0);
  return relParseArrangement(arrangement) === 'wheel'
    ? relLayoutWheel(nodes, edges, safeWidth, safeHeight, opts)
    : relLayoutClusters(nodes, edges, safeWidth, safeHeight, opts);
}

// Which node the pointer is over -- the only reason the renderer needs geometry
// back from the layout: hit-testing in reverse (smallest node first, so a big
// node behind a small one does not steal the click).
function relHitTest(layout, x, y, pad) {
  const positions = (layout && layout.positions) || layout || {};
  const slack = pad == null ? 3 : pad;
  let found = null;
  const ids = Object.keys(positions).sort(
    (a, b) => (positions[a].r || 0) - (positions[b].r || 0));
  for (const id of ids) {
    const spot = positions[id];
    if (!spot) continue;
    const dx = x - spot.x, dy = y - spot.y;
    const reach = Math.max(3, spot.r || 0) + slack;
    if (dx * dx + dy * dy <= reach * reach) { found = id; break; }
  }
  return found;
}

// Nodes on screen right now, given the layers switched on.
function relVisibleNodes(nodes, layers) {
  const shown = relParseLayers(layers, REL_LAYERS);
  return (nodes || []).filter(node => node && shown.includes(node.type));
}

// Edges worth drawing: both ends must be on screen.
function relVisibleEdges(edges, visible) {
  const ids = new Set((visible || []).map(node => node.id));
  return (edges || []).filter(edge => edge && ids.has(edge.source) && ids.has(edge.target));
}

// The strongest node of each layer, plus the counts -- what the rail's summary
// line and the legend read from.
function relSummarise(nodes, edges) {
  const degrees = relDegreeMap(edges);
  const byLayer = {};
  for (const layer of REL_LAYERS) byLayer[layer] = { nodes: 0, tracks: 0, top: null };
  for (const node of nodes || []) {
    const entry = byLayer[node.type];
    if (!entry) continue;
    entry.nodes += 1;
    entry.tracks += Number(node.weight) || 0;
    if (!entry.top || node.weight > entry.top.weight) entry.top = node;
  }
  const isolated = (nodes || []).filter(node => !degrees[node.id]).length;
  return { nodes: (nodes || []).length, edges: (edges || []).length, isolated, byLayer };
}

// Human label for a node, so the rail and the tests agree on wording.
function relNodeTitle(node) {
  if (!node) return '';
  const detail = node.detail || {};
  if (node.type === 'key' && detail.notation && detail.camelot) {
    return `${detail.camelot} · ${detail.notation}`;
  }
  return String(node.label || node.id || '');
}

// "3 tracks" / "1 track": counts read as prose in the detail card.
function relTrackCount(count) {
  const value = Number(count) || 0;
  return `${value} track${value === 1 ? '' : 's'}`;
}

