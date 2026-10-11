/* Frontend tests for the relationships rail.
 *
 * Two things are checked here, and both matter. First the pure geometry in
 * static/relationships-layout.js -- rail states, layer selection, Camelot
 * maths and the two arrangements -- because a graph that draws the wrong thing
 * looks fine and is useless. Then the panel itself in
 * static/relationships.js, driven against a fake DOM with the payload the real
 * /api/relationships returns: opening, resizing, hiding, maximising,
 * re-arranging, switching layers, focusing a node, and acting on a finding.
 *
 * Both scripts are loaded into one vm context, exactly as the page loads them,
 * so the panel really calls the layout helpers. `fetch` and every DOM element
 * are stubbed: no test touches the network or the library.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const readSource = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

// The element ids the panel and the page agree on.
const REL_IDS = [
  'relOpenBtn', 'relRail', 'relGutter', 'relHideBtn', 'relMaxBtn', 'relAddBtn',
  'relRemoveBtn', 'relMoreBtn', 'relArrClusters', 'relArrWheel', 'relLayerMenu',
  'relSummary', 'relLegend', 'relBody', 'relCanvas', 'relTooltip', 'relDetail',
  'relFindings', 'relFooter', 'relStatus',
];
const LAYER_NAMES = ['genre', 'tag', 'artist', 'album', 'crate', 'key', 'transition'];

// ── fake DOM ─────────────────────────────────────────────────────────────────

function makeClassList() {
  const held = new Set();
  return {
    add: (...names) => names.forEach((n) => held.add(n)),
    remove: (...names) => names.forEach((n) => held.delete(n)),
    toggle: (name, force) => {
      const want = force == null ? !held.has(name) : !!force;
      if (want) held.add(name); else held.delete(name);
      return want;
    },
    contains: (name) => held.has(name),
    values: () => [...held],
  };
}

function makeStyle() {
  const style = { setProperty(name, value) { style[name] = String(value); },
    getPropertyValue(name) { return style[name] || ''; },
    removeProperty(name) { delete style[name]; } };
  return style;
}

// A 2d context that records what was drawn instead of painting: enough for the
// renderer to run its real code path without a real canvas.
function makeContext2d(log) {
  const target = { fillStyle: '', strokeStyle: '', lineWidth: 1, font: '', globalAlpha: 1,
    textAlign: '', textBaseline: '', lineCap: '' };
  return new Proxy(target, {
    get(store, prop) {
      if (prop in store) return store[prop];
      return (...args) => {
        log.canvas.push({ fn: String(prop), args });
        return prop === 'measureText' ? { width: 24 } : undefined;
      };
    },
    set(store, prop, value) { store[prop] = value; return true; },
  });
}

function makeElement(id, tag, log) {
  const handlers = log.handlers;
  const el = {
    id, tagName: String(tag || 'div').toUpperCase(), tag: tag || 'div',
    innerHTML: '', textContent: '', value: '', title: '', type: '', role: '',
    disabled: false, hidden: false, checked: false, readOnly: false,
    children: [], parentNode: null, dataset: {}, attributes: {},
    style: makeStyle(), classList: makeClassList(),
    clientWidth: 380, clientHeight: 600, offsetWidth: 380, offsetHeight: 600,
    scrollTop: 0, scrollHeight: 600,
    setAttribute(name, value) { el.attributes[name] = String(value); },
    getAttribute(name) { return name in el.attributes ? el.attributes[name] : null; },
    removeAttribute(name) { delete el.attributes[name]; },
    hasAttribute(name) { return name in el.attributes; },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    removeEventListener(type, fn) {
      handlers[type] = (handlers[type] || []).filter((held) => held !== fn);
    },
    dispatchEvent() { return true; },
    appendChild(child) { el.children.push(child); if (child) child.parentNode = el; return child; },
    removeChild(child) { el.children = el.children.filter((c) => c !== child); return child; },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    insertAdjacentHTML(_where, html) { el.innerHTML += html; },
    querySelector(selector) { return el.querySelectorAll(selector)[0] || null; },
    querySelectorAll() { return []; },
    closest() { return null; }, matches() { return false; }, contains() { return false; },
    focus() {}, blur() {}, click() {}, scrollIntoView() {}, setPointerCapture() {},
    getContext() { return el.__context; },
    getBoundingClientRect() {
      return { width: el.clientWidth, height: el.clientHeight, top: 0, left: 0,
        right: el.clientWidth, bottom: el.clientHeight, x: 0, y: 0 };
    },
  };
  if (tag === 'canvas') el.__context = makeContext2d(log);
  return el;
}

function makeDocument(log) {
  const elements = {};
  const body = makeElement('body', 'body', log);
  const handlers = {};
  const doc = {
    body, documentElement: makeElement('html', 'html', log),
    hidden: false, visibilityState: 'visible', __handlers: handlers,
    getElementById(id) {
      if (!elements[id]) elements[id] = makeElement(id, /canvas/i.test(id) ? 'canvas' : 'div', log);
      return elements[id];
    },
    querySelector(selector) {
      return typeof selector === 'string' && selector.startsWith('#')
        ? doc.getElementById(selector.slice(1)) : null;
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeElement('', tag, log); },
    createTextNode(text) { return { textContent: String(text) }; },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    removeEventListener(type, fn) {
      handlers[type] = (handlers[type] || []).filter((held) => held !== fn);
    },
    dispatchEvent() { return true; },
    elements,
  };
  elements.body = body;
  return doc;
}

// The graph the real endpoint returns, trimmed to something a test can reason
// about: two heavy genres, a key, a crate, one transition link, and findings
// that between them cover every action kind the panel must honour.
function relationshipPayload() {
  return {
    layers: LAYER_NAMES.slice(),
    arrangement: 'clusters',
    layer_options: [
      { key: 'genre', label: 'Genres', nodes: 2, values: 75, low_weight: 0, empty: false },
      { key: 'tag', label: 'Tags', nodes: 0, values: 0, low_weight: 0, empty: true },
      { key: 'artist', label: 'Artists', nodes: 1, values: 523, low_weight: 474, empty: false },
      { key: 'album', label: 'Albums', nodes: 1, values: 200, low_weight: 0, empty: false },
      { key: 'crate', label: 'Crates', nodes: 1, values: 1, low_weight: 0, empty: false },
      { key: 'key', label: 'Keys', nodes: 1, values: 12, low_weight: 0, empty: false },
      { key: 'transition', label: 'Transitions', nodes: 0, values: 0, low_weight: 0, empty: true },
    ],
    nodes: [
      { id: 'genre:progressive-house', type: 'genre', label: 'Progressive House', weight: 83,
        degree: 2, songs: 83, song_ids: [1, 2, 3], detail: { genre: 'Progressive House', primary_key: '9A' } },
      { id: 'genre:house', type: 'genre', label: 'House', weight: 82, degree: 2, songs: 82,
        song_ids: [4], detail: { genre: 'House', primary_key: '6A' } },
      { id: 'key:6a', type: 'key', label: '6A', weight: 48, degree: 3, songs: 48, song_ids: [4],
        detail: { camelot: '6A', notation: 'G min', wheel_index: 6, ring: 'A',
          harmonic_ids: ['key:5a', 'key:7a', 'key:6b'], primary_key: '6A' } },
      { id: 'crate:warm-up', type: 'crate', label: 'Warm-up', weight: 12, degree: 1, songs: 12,
        song_ids: [4], detail: { crate_name: 'Warm-up', crate_id: 4, smart: false, primary_key: '6A' } },
      { id: 'transition:ace', type: 'transition', label: 'Ace — Sharam Jey', weight: 1, degree: 1,
        songs: 1, song_ids: [9], detail: { song_id: 9, title: 'Ace', artist: 'Sharam Jey', links: 1 } },
    ],
    edges: [
      { id: 'genre:house|key:6a', source: 'genre:house', target: 'key:6a', type: 'genre-key', weight: 30 },
      { id: 'crate:warm-up|key:6a', source: 'crate:warm-up', target: 'key:6a', type: 'crate-key', weight: 8 },
      { id: 'genre:progressive-house|key:6a', source: 'genre:progressive-house', target: 'key:6a',
        type: 'genre-key', weight: 4 },
    ],
    hubs: [{ id: 'genre:progressive-house', type: 'genre', label: 'Progressive House', weight: 83,
      degree: 2, songs: 83, detail: {} }],
    // Every finding carries `actions` (its buttons) and `action`, the first of
    // them, so a panel that renders a single button keeps working.
    findings: [
      { id: 'single-track-genres', severity: 'warn', title: '31 genres carry a single track',
        detail: 'A genre used once is usually a flavour.', metric: 31, song_ids: [1, 2],
        node_ids: ['genre:house'],
        action: { kind: 'open', label: 'Review tags vs genres', target: 'tag-review' },
        actions: [{ kind: 'open', label: 'Review tags vs genres', target: 'tag-review' }] },
      { id: 'genreless', severity: 'warn', title: '154 tracks have no genre',
        detail: 'They show as blank in Mixxx.', metric: 154, song_ids: [7, 8, 9], node_ids: [],
        action: { kind: 'select-songs', label: 'Select those tracks', target: 'genreless' },
        actions: [{ kind: 'select-songs', label: 'Select those tracks', target: 'genreless' }] },
      { id: 'no-key', severity: 'warn', title: '40 tracks have no key', detail: 'No key, no wheel.',
        metric: 40, song_ids: [11], node_ids: [],
        action: { kind: 'run-analysis', label: 'Analyse BPM & key', target: 'analyze-bpm' },
        actions: [{ kind: 'run-analysis', label: 'Analyse BPM & key', target: 'analyze-bpm' }] },
      { id: 'no-crates', severity: 'info', title: 'No crates yet', detail: 'Crates group a set.',
        metric: 0, song_ids: [], node_ids: [],
        action: { kind: 'open', label: 'Go to crates', target: 'crates' },
        actions: [{ kind: 'open', label: 'Go to crates', target: 'crates' }] },
    ],
    stats: { songs: 605, nodes: 184, edges: 420, isolated: 55, transition_links: 1,
      text_only_transitions: 0, omitted_nodes: 0, omitted_edges: 179, node_limit: 120,
      edge_limit: 420 },
  };
}

// ── loading the two modules into one context ─────────────────────────────────

function makeSandbox(options) {
  const settings = options || {};
  const log = { canvas: [], fetch: [], tabs: [], selection: [], analysis: 0, handlers: {} };
  const store = new Map();
  const writes = [];
  const windowHandlers = {};
  const document = makeDocument(log);
  const sandbox = {
    document, console, JSON, Math, Date, Object, Array,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame(callback) { callback(0); return 0; },
    cancelAnimationFrame() {},
    devicePixelRatio: 2, innerWidth: settings.innerWidth || 1440, innerHeight: 900, scrollY: 0,
    getComputedStyle: () => ({ getPropertyValue: () => '#38e2ff' }),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    localStorage: {
      getItem(key) { return store.has(key) ? store.get(key) : null; },
      setItem(key, value) { store.set(key, String(value)); writes.push([key, String(value)]); },
      removeItem(key) { store.delete(key); },
      clear() { store.clear(); },
    },
    selectedIds: new Set([99]),
    showTab(name) { log.tabs.push(name); },
    analyzeBpm() { log.analysis += 1; },
    updateSelectionBar() { log.selection.push([...sandbox.selectedIds]); },
    updateRowSelection() {}, renderSongs() {}, applyFilters() {}, clearSelection() {},
    allSongs: [], visibleSongs: [], esc: (value) => String(value == null ? '' : value),
    addEventListener(type, fn) { (windowHandlers[type] = windowHandlers[type] || []).push(fn); },
    removeEventListener(type, fn) {
      windowHandlers[type] = (windowHandlers[type] || []).filter((held) => held !== fn);
    },
    // Fires the listeners the panel registered anywhere -- window, document,
    // the gutter element -- which is how a test drives a mouse drag for real.
    __fire(type, event) {
      const payload = event || {};
      const listeners = [...(windowHandlers[type] || []), ...(document.__handlers[type] || []),
        ...(log.handlers[type] || [])];
      const inlineName = `on${type}`;
      for (const target of [sandbox, document, document.body,
        ...Object.values(document.elements)]) {
        if (target && typeof target[inlineName] === 'function') listeners.push(target[inlineName]);
      }
      listeners.forEach((fn) => fn(payload));
    },
    __log: log, __writes: writes,
  };
  sandbox.window = sandbox;
  // A real Response answers text() as well as json(); the panel reads the body
  // as text so an error page reports its status instead of a parse failure.
  sandbox.fetch = async (url, init) => {
    log.fetch.push({ url: String(url), init });
    const body = JSON.stringify(relationshipPayload());
    return { ok: true, status: 200, text: async () => body, json: async () => relationshipPayload() };
  };
  if (settings.fetch) sandbox.fetch = settings.fetch;
  if (settings.localStorage) sandbox.localStorage = settings.localStorage;
  return sandbox;
}

function loadLayout(options) {
  const sandbox = makeSandbox(options);
  const context = vm.createContext(sandbox);
  vm.runInContext(readSource('static/relationships-layout.js'), context,
    { filename: 'static/relationships-layout.js' });
  return context;
}

// Both files, in page order: the panel calls the layout helpers as globals.
function loadRail(options) {
  const context = loadLayout(options);
  vm.runInContext(readSource('static/relationships.js'), context,
    { filename: 'static/relationships.js' });
  return context;
}

// The real endpoint answers a `layers=` request with only those layers' nodes
// and the edges that survive between them, so the stub has to do the same.
// Without that, "isolating a layer refetches and shrinks the graph" could pass
// while the panel quietly kept the whole library on screen. A stub replaces the
// harness's logging fetch, so it records the urls it was asked for itself.
function layeredFetch(payload, asked) {
  return async (url) => {
    if (asked) asked.push(String(url));
    const match = /[?&]layers=([^&]*)/.exec(String(url));
    const layers = match
      ? LAYER_NAMES.filter((layer) => decodeURIComponent(match[1]).split(',').includes(layer))
      : null;
    const nodes = layers ? payload.nodes.filter((node) => layers.includes(node.type)) : payload.nodes;
    const ids = new Set(nodes.map((node) => node.id));
    const edges = (layers ? payload.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target))
      : payload.edges);
    const body = JSON.stringify({ ...payload, nodes, edges, layers: layers || payload.layers });
    return { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) };
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function openRail(context) {
  if (typeof context.initRelationships === 'function') context.initRelationships();
  await context.openRelationships();
  for (let i = 0; i < 6; i += 1) await tick();
  return context;
}

const railState = (context) => context.document.body.dataset.rail;
const fetchUrls = (context) => context.__log.fetch.map((call) => call.url);
const storedValues = (context) => context.__writes.map(([, value]) => value);
// The panel writes escaped markup (correctly), so this reads it back the way a
// human sees it -- otherwise a label like "Analyse BPM & key" would only ever
// be assertable in its &amp; form.
const decodeEntities = (html) => String(html)
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const textOf = (context, id) => decodeEntities(String(context.document.getElementById(id).innerHTML
  + context.document.getElementById(id).textContent));
// Wherever the panel applies the rail width, as a number of pixels -- read off
// the body, the rail or the document element, whichever the implementation
// uses, but it has to be a real width.
function railWidth(context) {
  const found = [];
  const targets = [context.document.body, context.document.getElementById('relRail'),
    context.document.documentElement];
  for (const element of targets) {
    if (!element || !element.style) continue;
    let raw = element.style.getPropertyValue('--rail-w-set');
    if (!raw) {
      const key = Object.keys(element.style).find((name) => /rail/i.test(name) && /w/i.test(name));
      if (key) raw = element.style[key];
    }
    const pixels = parseInt(raw, 10);
    if (Number.isFinite(pixels)) found.push(pixels);
  }
  return found.length ? found[found.length - 1] : NaN;
}

// ── layout: layers, state, width ─────────────────────────────────────────────

test('layer selection keeps the canonical order and refuses to empty the rail', () => {
  const layout = loadLayout();
  assert.deepEqual(Array.from(layout.REL_LAYERS), LAYER_NAMES,
    'the seven node types are the canonical order');
  assert.deepEqual(Array.from(layout.relParseLayers(null)), LAYER_NAMES, 'no filter means every layer');
  assert.deepEqual(Array.from(layout.relParseLayers('')), LAYER_NAMES, 'an empty string means every layer');
  assert.deepEqual(Array.from(layout.relParseLayers('key, genre, nonsense')), ['genre', 'key'],
    'unknown layers are dropped and order is canonical');
  assert.deepEqual(Array.from(layout.relParseLayers(['crate'])), ['crate'], 'an array works too');
  assert.deepEqual(Array.from(layout.relToggleLayer(['genre', 'key'], 'tag')), ['genre', 'tag', 'key'],
    'adding puts the layer in canonical position');
  assert.deepEqual(Array.from(layout.relToggleLayer(['genre', 'tag'], 'genre')), ['tag'],
    'removing drops it');
  assert.deepEqual(Array.from(layout.relToggleLayer(['genre'], 'genre')), ['genre'],
    'the last layer cannot be removed');
  assert.deepEqual(Array.from(layout.relToggleLayer(['genre'], 'not-a-layer')), ['genre'],
    'an unknown layer changes nothing');
  assert.equal(layout.relLayerCsv(['key', 'genre']), 'genre,key', 'the csv is what the fetch sends');
});

test('rail width and the three rail states behave like controls', () => {
  const layout = loadLayout();
  assert.equal(layout.relClampWidth(900), 720, 'too wide is clamped');
  assert.equal(layout.relClampWidth(10), 280, 'too narrow is clamped');
  assert.equal(layout.relClampWidth('nonsense'), 420, 'a non-number falls back to the default');
  assert.equal(layout.relClampWidth(null), layout.REL_WIDTH_DEFAULT);
  assert.equal(layout.relClampWidth(400.6), 401, 'pixels are whole numbers');

  assert.equal(layout.relNextRailState('hidden', 'toggle'), 'open');
  assert.equal(layout.relNextRailState('open', 'toggle'), 'hidden', 'the header button closes it again');
  assert.equal(layout.relNextRailState('max', 'toggle'), 'hidden');
  assert.equal(layout.relNextRailState('hidden', 'hide'), 'hidden', 'hide from closed stays closed');
  assert.equal(layout.relNextRailState('open', 'maximise'), 'max');
  assert.equal(layout.relNextRailState('max', 'maximise'), 'open', 'maximise toggles back');
  assert.equal(layout.relParseArrangement('wheel'), 'wheel');
  assert.equal(layout.relParseArrangement('nonsense'), 'clusters');
});

// ── layout: Camelot wheel ────────────────────────────────────────────────────

test('Camelot slots, harmonic partners and wheel angles are the real thing', () => {
  const layout = loadLayout();
  assert.deepEqual({ ...layout.relCamelotSlot('6a') }, { index: 6, ring: 'A' });
  assert.deepEqual({ ...layout.relCamelotSlot(' 12B ') }, { index: 12, ring: 'B' });
  assert.equal(layout.relCamelotSlot('13A'), null, 'the wheel has twelve numbers');
  assert.equal(layout.relCamelotSlot('G min'), null, 'notation is not a slot');

  assert.deepEqual(Array.from(layout.relHarmonicPartners('6A')), ['5A', '7A', '6B'],
    'neighbours either side plus the relative major/minor');
  assert.deepEqual(Array.from(layout.relHarmonicPartners('1A')), ['12A', '2A', '1B'], 'it wraps');
  assert.deepEqual(Array.from(layout.relHarmonicPartners('12B')), ['11B', '1B', '12A']);
  assert.deepEqual(Array.from(layout.relHarmonicPartners('nope')), []);

  const top = layout.relWheelAngle(12, 'A');
  assert.ok(Math.abs(top + Math.PI / 2) < 1e-9, '12 o`clock sits at the top');
  assert.equal(layout.relWheelAngle(12, 'B'), top, 'both rings share an angle');
  const step = layout.relWheelAngle(1, 'A') - top;
  assert.ok(Math.abs(step - (Math.PI * 2) / 12) < 1e-9, 'numbers run clockwise in twelfths');
  assert.notEqual(layout.relWheelAngle(3, 'A'), layout.relWheelAngle(4, 'A'));
});

// ── layout: weights, visibility, geometry ────────────────────────────────────

test('node size follows weight on a bounded area scale', () => {
  const layout = loadLayout();
  assert.ok(layout.relNodeRadius(0, 100) >= 3.5, 'a weightless node still has a dot');
  assert.equal(layout.relNodeRadius(100, 100), 22, 'the heaviest node hits the ceiling');
  assert.ok(layout.relNodeRadius(80, 100) > layout.relNodeRadius(20, 100), 'heavier is bigger');
  assert.ok(layout.relNodeRadius(1, 1000000) >= 3.5, 'one track in a huge library stays visible');
  const tiny = layout.relNodeRadius(50, 100, 1, 10);
  assert.ok(tiny >= 1 && tiny <= 10, 'bounds can be overridden and still hold');
});

test('degrees, visibility and the summary count what is really selected', () => {
  const layout = loadLayout();
  const edges = [
    { source: 'a', target: 'b' }, { source: 'a', target: 'c' }, { source: 'b', target: 'c' },
  ];
  assert.deepEqual({ ...layout.relDegreeMap(edges) }, { a: 2, b: 2, c: 2 });
  assert.deepEqual({ ...layout.relDegreeMap([]) }, {}, 'no edges, no degrees');

  const nodes = [
    { id: 'genre:house', type: 'genre', label: 'House', weight: 82 },
    { id: 'key:6a', type: 'key', label: '6A', weight: 48 },
    { id: 'crate:warm', type: 'crate', label: 'Warm', weight: 3 },
  ];
  const visible = layout.relVisibleNodes(nodes, 'genre,key');
  assert.deepEqual(visible.map((node) => node.id), ['genre:house', 'key:6a'],
    'only the chosen layers are drawn');
  assert.deepEqual(layout.relVisibleEdges(
    [{ source: 'genre:house', target: 'key:6a' }, { source: 'crate:warm', target: 'key:6a' }],
    visible).map((edge) => edge.id || edge.source), ['genre:house'],
    'an edge with a hidden end is not drawn');

  const summary = layout.relSummarise(nodes, []);
  assert.equal(summary.nodes, 3);
  assert.equal(summary.edges, 0);
  assert.equal(summary.isolated, 3, 'nothing is linked, so everything is isolated');
  // The summary's objects come from the sandbox realm, so compare their fields
  // rather than their identities.
  assert.equal(summary.byLayer.genre.nodes, 1);
  assert.equal(summary.byLayer.genre.tracks, 82);
  assert.equal(summary.byLayer.genre.top.id, nodes[0].id);
  assert.equal(summary.byLayer.tag.nodes, 0, 'an empty layer is reported, not omitted');

  assert.equal(layout.relNodeTitle(nodes[1]), '6A', 'a key without notation shows its Camelot name');
  assert.equal(layout.relNodeTitle({ type: 'key', label: '6A', detail: { camelot: '6A', notation: 'G min' } }),
    '6A · G min', 'both readings of a key are shown');
  assert.equal(layout.relNodeTitle(nodes[0]), 'House');
  assert.equal(layout.relTrackCount(1), '1 track', 'one track is singular');
  assert.equal(layout.relTrackCount(4), '4 tracks');
});

test('clusters place every layer in its own region, deterministically', () => {
  const layout = loadLayout();
  const payload = relationshipPayload();
  const first = layout.relLayoutGraph(payload.nodes, payload.edges, 'clusters', 380, 600);
  const again = layout.relLayoutGraph(payload.nodes, payload.edges, 'clusters', 380, 600);
  assert.deepEqual(again.positions, first.positions, 'the same payload lays out the same way');

  for (const node of payload.nodes) {
    const spot = first.positions[node.id];
    assert.ok(spot, `${node.id} has a position`);
    assert.ok(Number.isFinite(spot.x) && Number.isFinite(spot.y), `${node.id} is at a real point`);
    assert.ok(spot.x >= -30 && spot.x <= 410, `${node.id} stays inside the panel width`);
    assert.ok(spot.y >= -30 && spot.y <= 630, `${node.id} stays inside the panel height`);
    assert.ok(spot.r > 0, `${node.id} has a radius`);
  }

  const heavy = (layer) => {
    const node = payload.nodes.filter((n) => n.type === layer)
      .sort((a, b) => b.weight - a.weight)[0];
    return first.positions[node.id];
  };
  assert.notDeepEqual({ x: heavy('genre').x, y: heavy('genre').y },
    { x: heavy('key').x, y: heavy('key').y }, 'different layers get different regions');
  assert.ok(first.anchors.genre && first.anchors.key, 'each drawn layer has a labelled anchor');
});

test('the wheel puts keys on their Camelot angle and orbits the rest', () => {
  const layout = loadLayout();
  const payload = relationshipPayload();
  const wheel = layout.relLayoutGraph(payload.nodes, payload.edges, 'wheel', 380, 600);
  const key = wheel.positions['key:6a'];
  const expected = {
    x: wheel.centre.x + Math.cos(layout.relWheelAngle(6, 'A')) * wheel.rings.ringA,
    y: wheel.centre.y + Math.sin(layout.relWheelAngle(6, 'A')) * wheel.rings.ringA,
  };
  assert.ok(Math.abs(key.x - expected.x) < 1e-6 && Math.abs(key.y - expected.y) < 1e-6,
    '6A sits on the A ring at its own angle');
  assert.ok(wheel.rings.orbit > wheel.rings.ringB && wheel.rings.ringB > wheel.rings.ringA,
    'the orbit ring is outside the two key rings');

  const orbit = wheel.positions['genre:house'];
  const distance = (spot) => Math.hypot(spot.x - wheel.centre.x, spot.y - wheel.centre.y);
  assert.ok(distance(orbit) > distance(key),
    'a genre orbits further out than the key it sits in');
  const orbitAngle = Math.atan2(orbit.y - wheel.centre.y, orbit.x - wheel.centre.x);
  const keyAngle = layout.relWheelAngle(6, 'A');
  assert.ok(Math.abs(orbitAngle - keyAngle) < 0.4,
    'it orbits near its own key, not somewhere random');
  assert.ok(['crate:warm-up', 'transition:ace'].every((id) => wheel.positions[id]),
    'nodes without a key are still placed somewhere readable');
});

test('hit testing finds the node under the pointer, small one first', () => {
  const layout = loadLayout();
  const overlay = { positions: { big: { x: 10, y: 10, r: 20 }, small: { x: 10, y: 10, r: 4 } } };
  assert.equal(layout.relHitTest(overlay, 10, 10), 'small',
    'the smaller node on top wins the click');
  assert.equal(layout.relHitTest(overlay, 10, 10, 0), 'small');
  assert.equal(layout.relHitTest(overlay, 100, 100), null, 'empty space hits nothing');
  const payload = relationshipPayload();
  const clusters = layout.relLayoutGraph(payload.nodes, payload.edges, 'clusters', 380, 600);
  const spot = clusters.positions['key:6a'];
  assert.equal(layout.relHitTest(clusters, spot.x, spot.y), 'key:6a',
    'the layout the panel draws is the layout the pointer hits');
});

// ── the panel ────────────────────────────────────────────────────────────────

test('opening the rail loads the graph and shows what came back', async () => {
  const rail = loadRail();
  assert.ok(railState(rail) === undefined || railState(rail) === 'hidden',
    'the rail starts closed');
  await openRail(rail);

  assert.equal(railState(rail), 'open', 'opening is a body state, so the CSS can lay it out');
  const url = fetchUrls(rail)[0];
  assert.match(url, /\/api\/relationships/, 'the panel reads its own endpoint');
  assert.match(url, /layers=[^&]*genre/, 'and asks for the layers it draws');
  assert.match(url, /arrangement=clusters/, 'clusters is the default arrangement');

  assert.match(textOf(rail, 'relLegend'), /Genres/, 'the legend names the drawn layers');
  assert.match(textOf(rail, 'relLegend'), /Keys/);
  assert.ok(textOf(rail, 'relSummary').length > 0, 'the summary says what the graph holds');
  const drawn = rail.__log.canvas.map((call) => call.fn);
  assert.ok(drawn.some((fn) => ['arc', 'ellipse', 'fillRect', 'lineTo', 'fillText'].includes(fn)),
    'the canvas was actually drawn on');
});

test('findings are listed with their action, and the cap is admitted', async () => {
  const rail = loadRail();
  await openRail(rail);
  const findings = textOf(rail, 'relFindings');
  for (const finding of relationshipPayload().findings) {
    assert.ok(findings.includes(finding.title), `"${finding.title}" is listed`);
    assert.ok(findings.includes(finding.action.label), `"${finding.action.label}" is offered`);
  }
  const footer = textOf(rail, 'relFooter') + textOf(rail, 'relSummary') + textOf(rail, 'relStatus');
  assert.match(footer, /179|599/, 'the links left out by the edge cap are admitted, not hidden');
});

test('a finding can offer more than one button, and older payloads still work', async () => {
  // A crate finding both selects the tracks in its crates and takes you to the
  // crates; `action` alone is still honoured for a payload that predates it.
  const payload = relationshipPayload();
  const thin = payload.findings.find((finding) => finding.id === 'no-crates');
  thin.actions = [
    { kind: 'select-songs', label: 'Select those tracks', target: 'thin-crates', song_ids: [1, 2] },
    { kind: 'open', label: 'Go to crates', target: 'crates' },
  ];
  thin.action = thin.actions[0];
  const rail = loadRail({ fetch: layeredFetch(payload) });
  await openRail(rail);

  const card = rail.document.getElementById('relFindings').innerHTML;
  const finding = card.slice(card.indexOf('No crates yet'));
  assert.match(finding.slice(0, 900), /Select those tracks/);
  assert.match(finding.slice(0, 900), /Go to crates/,
    'both of the finding\'s buttons are rendered');
  assert.ok(finding.indexOf('Select those tracks') < finding.indexOf('Go to crates'),
    'and in the order the payload gave them');

  rail.runRelationshipAction(thin.actions[0]);
  assert.ok(rail.selectedIds.has(1) && rail.selectedIds.has(2),
    'the first button selects what it named');
  rail.runRelationshipAction(thin.actions[1]);
  assert.equal(rail.__log.tabs[rail.__log.tabs.length - 1], 'crates',
    'the second opens the crate view');

  // `action` with no `actions`: what a panel served by an older payload sees.
  const legacy = relationshipPayload();
  for (const finding of legacy.findings) delete finding.actions;
  const old = loadRail({ fetch: layeredFetch(legacy) });
  await openRail(old);
  const legacyCard = textOf(old, 'relFindings');
  for (const finding of legacy.findings) {
    assert.ok(legacyCard.includes(finding.action.label),
      `"${finding.action.label}" still renders from action alone`);
  }
});

test('hiding, showing and maximising are the three rail states', async () => {
  const rail = loadRail();
  await openRail(rail);

  rail.hideRelationships();
  assert.equal(railState(rail), 'hidden');
  assert.ok(storedValues(rail).some((value) => value.includes('hidden')),
    'the choice survives a reload');

  rail.showRelationships();
  assert.equal(railState(rail), 'open', 'the header button brings it back');

  rail.toggleRelationshipsMaximise();
  assert.equal(railState(rail), 'max');
  assert.ok(storedValues(rail).some((value) => value.includes('max')));
  rail.toggleRelationshipsMaximise();
  assert.equal(railState(rail), 'open', 'maximise toggles back');
});

test('switching layers refetches and remembers which layers are on', async () => {
  const rail = loadRail();
  await openRail(rail);
  const before = fetchUrls(rail).length;

  rail.toggleRelationshipLayer('artist');
  for (let i = 0; i < 6; i += 1) await tick();

  const after = fetchUrls(rail).slice(before);
  assert.ok(after.length >= 1, 'changing the layers reloads the graph');
  assert.doesNotMatch(after[after.length - 1], /artist/, 'the layer that was switched off is not asked for');
  assert.match(after[after.length - 1], /genre/, 'the layers that stayed on are still asked for');
  assert.ok(storedValues(rail).some((value) => /genre/.test(value) && !/artist/.test(value)),
    'the layer set is persisted');
});

test('the … menu isolates the focused layer and really shrinks the graph', async () => {
  const payload = relationshipPayload();
  const asked = [];
  const rail = loadRail({ fetch: layeredFetch(payload, asked) });
  await openRail(rail);
  asked.length = 0;

  rail.openRelationshipLayerMenu('more');
  assert.match(textOf(rail, 'relLayerMenu'), /Show only this layer/,
    'the control is a row in the … menu, not just a function');

  rail.focusRelationshipNode('crate:warm-up');
  rail.relShowOnlyFocusedLayer();
  for (let i = 0; i < 6; i += 1) await tick();

  assert.equal(asked.length, 1, 'isolating a layer refetches the graph exactly once');
  assert.match(asked[0], /layers=crate(&|$)/, 'and asks the endpoint for that layer alone');
  assert.deepEqual(Array.from(rail.relState.layers), ['crate'], 'only that layer stays on');
  assert.deepEqual(Array.from(rail.relState.data.nodes).map((node) => node.id), ['crate:warm-up'],
    'what comes back is what a one-layer request returns');
  assert.ok(rail.relState.visible.length < payload.nodes.length, 'so the drawn graph really shrank');
  assert.equal(rail.relState.menu, null, 'the menu closes behind the choice');
  assert.equal(textOf(rail, 'relLayerMenu'), '', 'and its popover is emptied and hidden');
  assert.ok(rail.__writes.some(([key, value]) => key === 'relLayers' && value === 'crate'),
    'the isolated layer survives a reload');
});

test('with nothing focused the isolation control picks the heaviest node', async () => {
  const payload = relationshipPayload();
  const heaviest = payload.nodes.slice().sort((a, b) => (b.weight || 0) - (a.weight || 0))[0];
  const asked = [];
  const rail = loadRail({ fetch: layeredFetch(payload, asked) });
  await openRail(rail);
  rail.clearRelationshipFocus();
  assert.equal(rail.relState.focus, null, 'nothing is focused');
  asked.length = 0;

  rail.relShowOnlyFocusedLayer();
  for (let i = 0; i < 6; i += 1) await tick();

  assert.equal(asked.length, 1, 'the choice refetches once');
  assert.match(asked[0], new RegExp(`layers=${heaviest.type}(&|$)`),
    'the busiest node decides which layer is kept');
  assert.deepEqual(Array.from(rail.relState.layers), [heaviest.type]);
  assert.ok(rail.relState.data.nodes.every((node) => node.type === heaviest.type),
    'and only that layer is left to draw');
});

test('the +/-/... controls offer the layers and the arrangements', async () => {
  const rail = loadRail();
  await openRail(rail);

  rail.openRelationshipLayerMenu('add');
  assert.match(textOf(rail, 'relLayerMenu'), /already on/,
    'with every layer on there is nothing left to add, and the menu says so');

  rail.openRelationshipLayerMenu('add');        // same menu again closes it
  assert.equal(textOf(rail, 'relLayerMenu'), '', 'the popover toggles shut');

  rail.toggleRelationshipLayer('tag');          // switch one off
  rail.openRelationshipLayerMenu('add');
  assert.match(textOf(rail, 'relLayerMenu'), /Tags/,
    'the add menu offers the layer that is switched off');

  rail.openRelationshipLayerMenu('remove');
  const remove = textOf(rail, 'relLayerMenu');
  assert.match(remove, /Genres/, 'the remove menu offers a layer that is drawn');

  rail.openRelationshipLayerMenu('more');
  assert.match(textOf(rail, 'relLayerMenu'), /wheel|clusters|arrangement/i,
    'the more menu carries the arrangement choice');
});

test('arranging is remembered and the graph survives the switch', async () => {
  const rail = loadRail();
  await openRail(rail);
  rail.setRelationshipsArrangement('wheel');
  for (let i = 0; i < 6; i += 1) await tick();
  assert.ok(storedValues(rail).some((value) => value.includes('wheel')),
    'the arrangement is persisted');
  assert.ok(textOf(rail, 'relSummary').length > 0, 'the rail is still showing the graph');

  rail.setRelationshipsArrangement('clusters');
  for (let i = 0; i < 6; i += 1) await tick();
  assert.ok(storedValues(rail).some((value) => value.includes('clusters')));
});

test('resizing clamps and persists whatever the pointer did', async () => {
  const rail = loadRail();
  await openRail(rail);
  const gutter = rail.document.getElementById('relGutter');

  for (const clientX of [0, 5000]) {
    rail.startRelationshipResize({ clientX, clientY: 300, preventDefault() {}, target: gutter });
    rail.__fire('mousemove', { clientX, clientY: 300, preventDefault() {} });
    rail.__fire('mouseup', { clientX, clientY: 300 });
    const pixels = railWidth(rail);
    assert.ok(Number.isFinite(pixels), 'the rail carries its own width variable');
    assert.ok(pixels >= 280 && pixels <= 720, `a drag to x=${clientX} stays in range (${pixels})`);
  }
  const widths = storedValues(rail).map((value) => parseInt(value, 10)).filter(Number.isFinite);
  assert.ok(widths.some((value) => value >= 280 && value <= 720), 'the width is persisted');
});

test('focusing a node fills the detail card and can be cleared', async () => {
  const rail = loadRail();
  await openRail(rail);

  rail.focusRelationshipNode('genre:house');
  for (let i = 0; i < 4; i += 1) await tick();
  const detail = textOf(rail, 'relDetail');
  assert.match(detail, /House/, 'the node is named in the detail card');
  assert.match(detail, /82/, 'with the number of tracks behind it');
  assert.match(detail, /select/i, 'and a way to act on them');

  rail.clearRelationshipFocus();
  for (let i = 0; i < 4; i += 1) await tick();
  assert.doesNotMatch(textOf(rail, 'relDetail'), /82 tracks/,
    'clearing the focus puts the card back');
});

test('the actions a finding offers really run app commands', async () => {
  const rail = loadRail();
  await openRail(rail);

  rail.selectRelationshipSongs([1, 2, 3]);
  assert.ok([1, 2, 3].every((id) => rail.selectedIds.has(id)), 'the songs are selected in the table');
  assert.ok(rail.__log.selection.length > 0, 'the selection bar was refreshed');

  rail.runRelationshipAction({ kind: 'select-songs', song_ids: [7, 8] });
  assert.ok(rail.selectedIds.has(7) && rail.selectedIds.has(8));

  rail.runRelationshipAction({ kind: 'open', target: 'crates' });
  assert.equal(rail.__log.tabs[rail.__log.tabs.length - 1], 'crates',
    'the crate view is opened by name');

  rail.runRelationshipAction({ kind: 'run-analysis' });
  assert.ok(rail.__log.analysis >= 1,
    'the key analysis is triggered through its existing command');

  rail.runRelationshipAction({ kind: 'focus', target: 'genre:house' });
  for (let i = 0; i < 4; i += 1) await tick();
  assert.match(textOf(rail, 'relDetail'), /House/, 'a focus action focuses that node');
});

test('a node card selects the tracks behind that node', async () => {
  // The node's button used to carry only its id, and the lookup only knew about
  // findings -- so it selected nothing at all.
  const rail = await openRail(loadRail());
  const detail = rail.relationshipNodeDetail('genre:house');
  const select = detail.actions.find((action) => action.kind === 'select-songs');
  assert.ok(select, 'the node card offers a selection action');
  assert.deepEqual(Array.from(select.song_ids), [4], 'carrying the tracks behind that node');
  assert.match(select.label, /1 track/);

  rail.runRelationshipAction(select);
  assert.ok(rail.selectedIds.has(4), 'and the action really selects them in the table');
  assert.match(textOf(rail, 'relStatus'), /1 track selected/);

  // A node whose id list was capped promises only what it can select.
  const capped = rail.relState.data.nodes.find((node) => node.id === 'genre:house');
  capped.songs = 40;
  const cappedAction = rail.relationshipNodeDetail('genre:house')
    .actions.find((action) => action.kind === 'select-songs');
  assert.match(cappedAction.label, /Select 1 track of 40/);
});

test('a crate node card names its kind, its share and its links, and opens the crates', async () => {
  const payload = relationshipPayload();
  const asked = [];
  // The shared fixture's crate is manual; a smart one proves the card tells
  // the two kinds apart instead of calling everything a crate.
  payload.nodes.push({ id: 'crate:smart-house', type: 'crate', label: 'Smart House', weight: 310,
    degree: 2, songs: 310, song_ids: [1, 2],
    detail: { crate_name: 'Smart House', crate_id: 22, smart: true, primary_key: '9A' } });
  const rail = loadRail({ fetch: layeredFetch(payload, asked) });
  await openRail(rail);

  rail.focusRelationshipNode('crate:warm-up');
  for (let i = 0; i < 4; i += 1) await tick();
  const manual = textOf(rail, 'relDetail');
  assert.match(manual, /Warm-up/, 'the crate is named');
  assert.match(manual, /manual crate/, 'and says it is a manual one');
  assert.match(manual, /12 tracks/, 'with the tracks actually behind it');
  assert.match(manual, /2% of the library/, 'its share, computed from the library size');
  assert.match(manual, /1 of 1 drawn/, 'and how many of its links the graph drew');

  const crate = rail.relationshipNodeDetail('crate:warm-up');
  const select = crate.actions.find((action) => action.kind === 'select-songs');
  assert.ok(select, 'the crate card can select its members');
  assert.deepEqual(Array.from(select.song_ids), [4], 'carrying the crate members themselves');
  rail.runRelationshipAction(select);
  assert.ok(rail.selectedIds.has(4), 'and the members land in the table selection');
  rail.runRelationshipAction(crate.actions.find((action) => action.kind === 'open'));
  assert.equal(rail.__log.tabs[rail.__log.tabs.length - 1], 'crates',
    'Go to crates opens the crate view by name');

  rail.focusRelationshipNode('crate:smart-house');
  for (let i = 0; i < 4; i += 1) await tick();
  const smart = textOf(rail, 'relDetail');
  assert.match(smart, /smart crate \(rules\)/, 'a smart crate is named as one');
  assert.match(smart, /310 tracks/, 'with its own count');
});

test('a transition node card shows the track, its mix links and opens transitions', async () => {
  const payload = relationshipPayload();
  // Give the transition node the one drawn link its own layer would carry, so
  // "how many of its links are drawn" is a real number rather than 0 of 0.
  payload.edges.push({ id: 'genre:house|transition:ace', source: 'genre:house',
    target: 'transition:ace', type: 'genre-transition', weight: 2 });
  const rail = loadRail({ fetch: layeredFetch(payload) });
  await openRail(rail);

  rail.focusRelationshipNode('transition:ace');
  for (let i = 0; i < 4; i += 1) await tick();
  const detail = textOf(rail, 'relDetail');
  assert.match(detail, /Ace — Sharam Jey/, 'the track is named, not just its id');
  assert.match(detail, /Sharam Jey/, 'the artist is on the card');
  assert.match(detail, /Mix links/, 'and how many mix links that track has');
  assert.match(detail, /1 of 1 drawn/, 'plus how many of them the graph drew');
  assert.doesNotMatch(detail, /% of the library/,
    'one track does not claim a share of the library');

  const node = rail.relationshipNodeDetail('transition:ace');
  const select = node.actions.find((action) => action.kind === 'select-songs');
  assert.deepEqual(Array.from(select.song_ids), [9], 'the card carries that one track');
  rail.runRelationshipAction(select);
  assert.ok(rail.selectedIds.has(9), 'and selecting it reaches the table');
  rail.runRelationshipAction(node.actions.find((action) => action.kind === 'open'));
  assert.equal(rail.__log.tabs[rail.__log.tabs.length - 1], 'transitions',
    'Go to transitions opens the transitions view by name');
});

test('a redraw does not wipe what the panel just said', async () => {
  // Every resize, layer switch and selection triggers a render, and the rail
  // state is re-applied on each one: that must not clear the status line.
  const rail = await openRail(loadRail());
  rail.selectRelationshipSongs([1, 2]);
  assert.match(textOf(rail, 'relStatus'), /2 tracks selected/);

  rail.renderRelationships();
  assert.match(textOf(rail, 'relStatus'), /2 tracks selected/,
    'a redraw leaves the message alone');

  rail.setRelationshipsArrangement('wheel');
  assert.match(textOf(rail, 'relStatus'), /2 tracks selected/,
    'so does switching arrangement');
});

test('the panes the panel fills are actually revealed', async () => {
  // The detail card and the layer menu ship hidden in the markup; writing
  // content into a hidden pane looks identical to a broken panel.
  const rail = await openRail(loadRail());
  const menu = rail.document.getElementById('relLayerMenu');
  const detail = rail.document.getElementById('relDetail');
  assert.equal(detail.hidden, false, 'the detail pane is revealed so its hint is readable');
  assert.match(textOf(rail, 'relDetail'), /Click any node/);

  rail.openRelationshipLayerMenu('more');
  assert.equal(menu.hidden, false, 'the popover is revealed when it opens');
  assert.match(textOf(rail, 'relLayerMenu'), /Camelot wheel/);

  rail.openRelationshipLayerMenu('more');
  assert.equal(menu.hidden, true, 'and hidden again when it closes');

  rail.focusRelationshipNode('genre:house');
  assert.equal(detail.hidden, false);
  assert.match(textOf(rail, 'relDetail'), /House/);
  rail.clearRelationshipFocus();
  assert.equal(detail.hidden, false, 'the hint stays visible after a focus is cleared');
});

test('a window too narrow for a docked rail lays the rail over the library', async () => {
  // A docked rail on a small window would squeeze the track table to nothing,
  // so the panel borrows its own maximise overlay and says so.
  const narrow = await openRail(loadRail({ innerWidth: 700 }));
  assert.equal(railState(narrow), 'max', 'no room beside the table means the rail goes over it');
  assert.match(textOf(narrow, 'relStatus'), /Not enough room/,
    'and the status line explains why the rail is not docked');

  const wide = await openRail(loadRail({ innerWidth: 1440 }));
  assert.equal(railState(wide), 'open', 'a window with room docks the rail as usual');
  assert.doesNotMatch(textOf(wide, 'relStatus'), /Not enough room/);
  assert.doesNotMatch(textOf(wide, 'relStatus'), /Counting/,
    'a finished load does not leave a loading message standing');
});

test('a failed load leaves the rail usable and says so', async () => {
  const rail = loadRail({
    fetch: async () => { throw new Error('network down'); },
  });
  await openRail(rail);
  assert.equal(railState(rail), 'open', 'the rail is still there to retry in');
  assert.match(textOf(rail, 'relStatus'), /error|could not|failed|unavailable|retry/i,
    'the failure is reported in the rail instead of thrown at the page');
});

test('a server that does not know the endpoint says to restart it', async () => {
  // The page ships with the repo but the Flask process may be older than it: a
  // pulled update does not reach a running server, so /api/relationships answers
  // 404 with an HTML error page. That has to read as "restart", not as a JSON
  // parse failure in whatever words the browser happens to use.
  const notFound = '<!doctype html><html><title>404 Not Found</title><h1>Not Found</h1></html>';
  const rail = loadRail({
    fetch: async () => ({ ok: false, status: 404, text: async () => notFound }),
  });
  await openRail(rail);
  const said = textOf(rail, 'relStatus') + textOf(rail, 'relLegend');
  assert.match(said, /404/, 'the status the server sent is reported');
  assert.match(said, /restart/i, 'and so is the thing to do about it');
  assert.doesNotMatch(said, /expected pattern|Unexpected token|JSON/i,
    'no browser-specific parse error instead of the real reason');
  assert.equal(rail.relState.data, null, 'no half-a-graph is invented');
  assert.equal(railState(rail), 'open', 'the rail stays there to retry in');
});

test('an error page from the API reports its status, not a parse error', async () => {
  const failure = '<!doctype html><title>500 Internal Server Error</title>';
  const rail = loadRail({
    fetch: async () => ({ ok: false, status: 500, text: async () => failure }),
  });
  await openRail(rail);
  assert.match(textOf(rail, 'relStatus'), /500/, 'the real status is what is shown');
  assert.equal(rail.relState.data, null);
});

test('an empty library says it is empty rather than pretending', async () => {
  const empty = { layers: LAYER_NAMES.slice(), arrangement: 'clusters', layer_options: [],
    nodes: [], edges: [], hubs: [], findings: [],
    stats: { songs: 0, nodes: 0, edges: 0, isolated: 0, transition_links: 0,
      text_only_transitions: 0, omitted_nodes: 0, omitted_edges: 0, node_limit: 120,
      edge_limit: 420 } };
  const rail = loadRail({
    fetch: async () => ({ ok: true, status: 200, text: async () => JSON.stringify(empty),
      json: async () => empty }),
  });
  await openRail(rail);
  assert.match(textOf(rail, 'relFindings'), /nothing|no findings|empty|no tracks|import|scan/i,
    'an empty graph explains itself');
  const shown = textOf(rail, 'relFooter') + textOf(rail, 'relStatus');
  assert.match(shown, /0|nothing|no /i, 'and the counts are honest about there being nothing');
});

test('the panel and the page agree on the classes the rail writes', () => {
  // The rail's CSS and its panel were written as separate files, and they had
  // drifted: findings were emitted as -top/-title/-detail/-acts while the page
  // styled -h/-txt (so the cards fell back to the browser's 14px body type),
  // and the empty layer chips carried a bare `empty`, which is the app-wide
  // empty-pane class -- a centred growing column that inflated the strip.
  const html = readSource('static/index.html');
  for (const cls of ['rel-finding-top', 'rel-finding-title', 'rel-finding-detail',
    'rel-finding-acts']) {
    assert.ok(html.includes('.' + cls), '.' + cls + ' is styled, as the panel writes it');
  }
  for (const gone of ['.rel-finding-h{', '.rel-finding-txt{']) {
    assert.ok(!html.includes(gone), 'no rule is left for ' + gone);
  }
  assert.ok(html.includes('.rel-chip.rel-chip-none{'),
    'the empty-layer modifier has its own rule');
});

test('an empty layer chip does not borrow the app-wide empty class', async () => {
  const rail = await openRail(loadRail());
  const legend = rail.document.getElementById('relLegend').innerHTML;
  assert.ok(legend.includes('rel-chip-none'), "empty layers carry the rail's own class");
  assert.ok(!legend.includes('rel-chip on empty') && !legend.includes('rel-chip empty'),
    'never the bare `empty`, which the rest of the app styles as a growing column');
});

// ── the page really carries the rail ─────────────────────────────────────────

test('the rail is part of the page, not a separate screen', () => {
  const html = readSource('static/index.html');
  assert.match(html, /<script src="\/static\/relationships-layout\.js"><\/script>/,
    'the layout module is loaded');
  assert.match(html, /<script src="\/static\/relationships\.js"><\/script>/,
    'the panel module is loaded after it');
  assert.ok(html.indexOf('/static/relationships-layout.js') < html.indexOf('/static/relationships.js'),
    'layout first: the panel calls its helpers');

  for (const id of REL_IDS) {
    assert.match(html, new RegExp(`id="${id}"`), `#${id} exists in the markup`);
  }

  const openButton = html.match(/<[a-z]+[^>]*id="relOpenBtn"[^>]*>/);
  assert.ok(openButton, 'the header has a rail button');
  assert.match(openButton[0], /openRelationships\(\)/, 'and it opens the rail');

  const states = new Set([...html.matchAll(/data-rail="([a-z]+)"/g)].map((match) => match[1]));
  for (const state of ['open', 'hidden', 'max']) {
    assert.ok(states.has(state), `body[data-rail="${state}"] is styled`);
  }

  for (const layer of LAYER_NAMES) {
    assert.match(html, new RegExp(`--rel-${layer}\\s*:`),
      `the ${layer} node colour is defined as a theme variable`);
  }
  assert.match(html, /\.rail\b|id="relRail"/, 'the rail has its own markup');
  assert.match(html, /rail-gutter|id="relGutter"/, 'and a drag handle to resize it');
});
