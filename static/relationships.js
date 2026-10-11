/* Relationships rail -- the right-hand panel that draws the library's graph.
 *
 * The backend (relationships.py, served by GET /api/relationships) counts what
 * is in the library: every genre, tag, artist, album, crate, key and transition
 * as a node weighted by how many tracks carry it, plus an edge for each pair of
 * values that share a track, and findings computed from those same counts.
 * relationships-layout.js turns that payload into coordinates. This file is the
 * third part: the panel itself -- opening and hiding it, resizing it, switching
 * arrangement, adding and removing layers, drawing the canvas, and turning a
 * finding or a node into something the user can act on in the table.
 *
 * Deliberately defensive. The rail is a read-only view: if a fetch fails, if a
 * layer is empty, or if the page's app globals are not there (the frontend
 * tests run this file headless), it says so and stays usable rather than
 * throwing or pretending there is data.
 */

// Node colour: a CSS custom property per node type (defined in index.html) with
// a literal fallback, so the palette follows the theme but never renders blank.
var REL_PALETTE = {
  genre: ['--rel-genre', '#38e2ff'],
  tag: ['--rel-tag', '#bf5af2'],
  artist: ['--rel-artist', '#32d74b'],
  album: ['--rel-album', '#ffd60a'],
  crate: ['--rel-crate', '#ff9f0a'],
  key: ['--rel-key', '#ff453a'],
  transition: ['--rel-transition', '#8e8e93'],
};

// Where the rail's own state is remembered between visits. The graph itself is
// never cached -- it is one cheap query, and a stale graph is a lie.
var REL_STORE = { rail: 'relRail', width: 'relWidth', arrangement: 'relArrangement', layers: 'relLayers' };

var relState = {
  rail: 'hidden',              // 'hidden' | 'open' | 'max'
  arrangement: 'clusters',
  layers: null,                // filled on init from REL_LAYERS
  width: 420,
  focus: null,
  hover: null,
  menu: null,                  // 'add' | 'remove' | 'more' | null
  data: null,
  layout: null,
  visible: [],
  edges: [],
  status: '',
  error: '',
  loading: false,
  requestId: 0,
  resizeTimer: null,
};

// -- SMALL HELPERS -----------------------------------------------------------
// Resolving the layout module, the app's globals and storage through one place
// each keeps the rest of the file free of `typeof` noise.

var relToolsCache = null;

// The pure geometry module, or null when this file is loaded on its own (a
// headless test, or a page that forgot the script tag). Never cached as null,
// so a late-loaded layout module is still picked up.
function relTools() {
  if (relToolsCache) return relToolsCache;
  if (typeof relLayoutGraph !== 'function' || typeof relParseLayers !== 'function'
      || typeof relNodeRadius !== 'function' || typeof relHitTest !== 'function') return null;
  relToolsCache = {
    layers: (typeof REL_LAYERS !== 'undefined' ? REL_LAYERS : Object.keys(REL_PALETTE)),
    labels: (typeof REL_LAYER_LABELS !== 'undefined' ? REL_LAYER_LABELS : {}),
    labelRadius: (typeof REL_LABEL_RADIUS !== 'undefined' ? REL_LABEL_RADIUS : 8),
    defaultWidth: (typeof REL_WIDTH_DEFAULT !== 'undefined' ? REL_WIDTH_DEFAULT : 420),
    parse: relParseLayers, csv: relLayerCsv, toggle: relToggleLayer,
    clamp: relClampWidth, nextState: relNextRailState, arrangement: relParseArrangement,
    slot: relCamelotSlot, harmonic: relHarmonicPartners, radius: relNodeRadius,
    layout: relLayoutGraph, hitTest: relHitTest, visibleNodes: relVisibleNodes,
    visibleEdges: relVisibleEdges, summarise: relSummarise, title: relNodeTitle,
    trackCount: relTrackCount,
  };
  return relToolsCache;
}

function relLayerKeys() {
  const tools = relTools();
  return tools ? tools.layers.slice() : Object.keys(REL_PALETTE);
}

// Every layer is on until the user says otherwise; called before anything reads
// relState.layers so the panel also works when it is driven directly rather
// than through initRelationships().
function relEnsureLayers() {
  if (!relState.layers || !relState.layers.length) relState.layers = relLayerKeys();
  return relState.layers;
}

function relLayerLabel(layer) {
  const tools = relTools();
  return (tools && tools.labels[layer]) || layer;
}

function relEl(id) {
  if (typeof document === 'undefined' || !document.getElementById) return null;
  return document.getElementById(id);
}

function relBody() {
  if (typeof document === 'undefined') return null;
  return document.body || null;
}

function relEsc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function relStore(key, value) {
  try {
    if (typeof localStorage === 'undefined') return null;
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, String(value));
    return String(value);
  } catch (e) { return null; }
}

// Call one of the page's own functions. Absent in a headless run -- that is not
// an error, it just means there is nothing to drive -- but a function that
// exists and fails is reported rather than swallowed.
function relCall(name, ...args) {
  const scope = (typeof globalThis !== 'undefined') ? globalThis : null;
  const fn = scope ? scope[name] : null;
  if (typeof fn !== 'function') return false;
  try { fn(...args); return true; }
  catch (e) {
    console.error('relationships: ' + name + '() failed', e);
    relStatus('Could not run ' + name + '(): ' + (e && e.message ? e.message : e));
    return false;
  }
}

// index.html keeps the selection set in a top-level `let`, which lives in the
// global lexical environment rather than on window -- so it is reached by a
// direct reference, guarded by typeof for headless runs.
function relSelectionSet() {
  try {
    if (typeof selectedIds === 'undefined') return null;
    return selectedIds || null;
  } catch (e) { return null; }
}

function relStatus(message) {
  relState.status = String(message || '');
  const el = relEl('relStatus');
  if (el) el.textContent = relState.status;
}

// The rail's own panes ship `hidden` in the markup (the detail card and the
// layer menu start closed), so anything that fills one has to reveal it. Both
// the property and the attribute are set: the flag is what tests and script
// read, the attribute is what the stylesheet hides on.
function relShow(el, visible) {
  if (!el) return false;
  const want = !!visible;
  el.hidden = !want;
  if (el.removeAttribute && el.setAttribute) {
    if (want) el.removeAttribute('hidden');
    else el.setAttribute('hidden', '');
  }
  return want;
}

// -- OPEN / CLOSE / RESIZE ---------------------------------------------------

// Below this much table, a docked rail has squeezed the library into a strip of
// column headers, which is not a view anyone asked for.
var REL_MIN_TABLE = 260;

function relSidebarWidth() {
  if (typeof getComputedStyle !== 'function' || !document || !document.body) return 288;
  try {
    const raw = getComputedStyle(document.body).getPropertyValue('--sidebar-w');
    const width = parseFloat(raw);
    return isFinite(width) && width > 0 ? width : 288;
  } catch (e) { return 288; }
}

// Would the docked rail leave the table unusable? Then the rail goes over the
// library instead of beside it -- the same overlay the maximise button uses, so
// a narrow window keeps the rail usable and the library whole.
function relCrampedRail() {
  if (relState.rail !== 'open') return false;
  const viewport = (typeof window !== 'undefined' && window.innerWidth) || 0;
  if (!viewport) return false;
  return (viewport - relSidebarWidth() - 7 - relState.width) < REL_MIN_TABLE;
}

// What the status line should say when nothing else is going on.
function relIdleStatus() {
  return relCrampedRail()
    ? 'Not enough room beside the table — showing the rail over the library.' : '';
}

// Called on every render, so it must not touch the status line unless the
// cramped state actually flipped -- otherwise a re-render would wipe whatever
// the panel just told the user ("154 tracks selected"). `quiet` is for the drag
// handler, which updates the width on every mousemove.
function relApplyRailState(quiet) {
  const body = relBody();
  const cramped = relCrampedRail();
  if (body && body.dataset) body.dataset.rail = cramped ? 'max' : relState.rail;
  if (body && body.style && body.style.setProperty) {
    body.style.setProperty('--rail-w-set', relState.width + 'px');
  }
  if (!quiet && cramped !== relState.cramped) relStatus(relIdleStatus());
  relState.cramped = cramped;
}

function openRelationships() {
  relState.rail = 'open';
  relStore(REL_STORE.rail, relState.rail);
  relApplyRailState();
  if (!relState.data) loadRelationships();
  else renderRelationships();
  return true;
}

// Closing fully means the rail's own state collapses; the header button is the
// way back, so the graph data is kept and no refetch is needed.
function hideRelationships() {
  relState.rail = 'hidden';
  relState.menu = null;
  relStore(REL_STORE.rail, relState.rail);
  relApplyRailState();
  relRenderLayerMenu();
  return true;
}

function closeRelationships() {
  return hideRelationships();
}

function showRelationships() {
  return openRelationships();
}

function toggleRelationshipsMaximise() {
  relState.rail = relState.rail === 'max' ? 'open' : 'max';
  relState.menu = null;
  relStore(REL_STORE.rail, relState.rail);
  relApplyRailState();
  relRenderLayerMenu();
  renderRelationships();
  return relState.rail;
}

// Drag the strip between the table and the rail. The width lands on the body as
// --rail-w-set (which the stylesheet maps to --rail-w while the rail is open),
// and a double-click puts the default back.
function startRelationshipResize(event) {
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  const body = relBody();
  const gutter = relEl('relGutter');
  if (!body || typeof window === 'undefined' || !window.addEventListener) return false;
  if (gutter && gutter.classList) gutter.classList.add('dragging');
  if (body.style) body.style.cursor = 'col-resize';

  const tools = relTools();
  const onMove = (moveEvent) => {
    const right = (typeof window !== 'undefined' && window.innerWidth)
      ? window.innerWidth - moveEvent.clientX : relState.width;
    relState.width = tools ? tools.clamp(right) : Math.round(right);
    relApplyRailState(true);
  };
  const onUp = () => {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    if (gutter && gutter.classList) gutter.classList.remove('dragging');
    if (body.style) { body.style.cursor = ''; body.style.userSelect = ''; }
    relStore(REL_STORE.width, relState.width);
    renderRelationships();
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  if (typeof window.addEventListener === 'function') window.addEventListener('blur', onUp, { once: true });
  return true;
}

function relResetWidth() {
  const tools = relTools();
  relState.width = tools ? tools.defaultWidth : 420;
  relStore(REL_STORE.width, relState.width);
  relApplyRailState();
  renderRelationships();
}

// -- LOADING -----------------------------------------------------------------

async function loadRelationships(force) {
  if (relState.loading && !force) return relState.data;
  if (relState.data && !force) { renderRelationships(); return relState.data; }
  const layers = relState.layers || relLayerKeys();
  const url = '/api/relationships?layers=' + encodeURIComponent(layers.join(','))
    + '&arrangement=' + encodeURIComponent(relState.arrangement);
  relState.loading = true;
  const requestId = ++relState.requestId;
  relStatus('Counting the library…');
  if (typeof fetch !== 'function') {
    relState.loading = false;
    relState.error = 'This page cannot fetch the graph.';
    renderRelationships();
    return null;
  }
  try {
    const response = await fetch(url);
    const data = await response.json();
    if (requestId !== relState.requestId) return relState.data;
    if (!response.ok) throw new Error((data && data.error) || ('HTTP ' + response.status));
    relState.data = data;
    relState.error = '';
    // The load is done: drop the 'Counting…' line rather than leaving it up as
    // if the panel were still working.
    relStatus(relIdleStatus());
    // The endpoint is the source of truth for which layers exist, including the
    // ones that are empty on this library.
    if (Array.isArray(data.layers) && data.layers.length) relState.layers = data.layers;
  } catch (e) {
    relState.error = (e && e.message) ? e.message : 'Could not load the graph';
    relState.data = null;
  } finally {
    relState.loading = false;
  }
  renderRelationships();
  return relState.data;
}

async function refreshRelationships() {
  relState.focus = null;
  return loadRelationships(true);
}

// -- RENDER ------------------------------------------------------------------

function relColorFor(type) {
  const entry = REL_PALETTE[type];
  if (!entry) return 'var(--muted)';
  if (typeof document === 'undefined' || !document.body || typeof getComputedStyle !== 'function') {
    return entry[1];
  }
  try {
    const value = getComputedStyle(document.body).getPropertyValue(entry[0]);
    return (value && value.trim()) || entry[1];
  } catch (e) { return entry[1]; }
}

function relNeighbourhood() {
  const focus = relState.focus;
  const around = new Set();
  if (!focus) return around;
  around.add(focus);
  for (const edge of relState.edges || []) {
    if (edge.source === focus) around.add(edge.target);
    else if (edge.target === focus) around.add(edge.source);
  }
  return around;
}

function relLayoutNow() {
  const tools = relTools();
  if (!tools) return null;
  const canvas = relEl('relCanvas');
  const size = relCanvasSize(canvas);
  const visible = tools.visibleNodes((relState.data && relState.data.nodes) || [], relState.layers);
  relState.visible = visible;
  relState.edges = tools.visibleEdges((relState.data && relState.data.edges) || [], visible);
  relState.layout = tools.layout(visible, relState.edges, relState.arrangement, size.width, size.height);
  return relState.layout;
}

function relCanvasSize(canvas) {
  const body = relEl('relBody');
  const width = Math.max(120, (body && body.clientWidth) || (canvas && canvas.clientWidth) || 520);
  const height = Math.max(120, (body && body.clientHeight) || (canvas && canvas.clientHeight) || 360);
  return { width, height };
}

function renderRelationships() {
  const tools = relTools();
  relApplyRailState();
  relEnsureLayers();
  if (!tools) {
    relStatus('The relationships layout module is missing.');
    return false;
  }
  if (relState.error) {
    relStatus('Could not load the graph: ' + relState.error);
  }
  relLayoutNow();
  drawRelationshipGraph();
  relRenderLegend();
  relRenderSummary();
  relRenderFooter();
  relRenderFindings();
  relRenderDetail();
  relRenderLayerMenu();
  relSyncControls();
  return true;
}

function relSyncControls() {
  const clusters = relEl('relArrClusters');
  const wheel = relEl('relArrWheel');
  const isWheel = relState.arrangement === 'wheel';
  if (clusters && clusters.classList) clusters.classList.toggle('active', !isWheel);
  if (wheel && wheel.classList) wheel.classList.toggle('active', isWheel);
  if (clusters && clusters.setAttribute) clusters.setAttribute('aria-pressed', String(!isWheel));
  if (wheel && wheel.setAttribute) wheel.setAttribute('aria-pressed', String(isWheel));
  const maxBtn = relEl('relMaxBtn');
  if (maxBtn && maxBtn.setAttribute) {
    maxBtn.setAttribute('aria-pressed', String(relState.rail === 'max'));
    maxBtn.title = relState.rail === 'max' ? 'Restore the rail' : 'Maximise the rail';
  }
  const openBtn = relEl('relOpenBtn');
  if (openBtn && openBtn.setAttribute) {
    openBtn.setAttribute('aria-pressed', String(relState.rail !== 'hidden'));
  }
}

// -- CANVAS ------------------------------------------------------------------

function drawRelationshipGraph() {
  const tools = relTools();
  const canvas = relEl('relCanvas');
  if (!tools || !canvas || typeof canvas.getContext !== 'function') return false;
  const ctx = canvas.getContext('2d');
  if (!ctx) return false;
  const size = relCanvasSize(canvas);
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) ? Math.min(2, window.devicePixelRatio) : 1;
  canvas.width = Math.round(size.width * dpr);
  canvas.height = Math.round(size.height * dpr);
  if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size.width, size.height);

  const layout = relState.layout || relLayoutNow();
  if (!layout) return false;
  const positions = layout.positions || {};
  const nodes = relState.visible || [];
  relState.types = {};
  for (const node of nodes) relState.types[node.id] = node.type;
  const focus = relState.focus;
  const around = relNeighbourhood();
  const dim = (id) => focus && !around.has(id);
  const weights = relState.edges.map(edge => edge.weight || 1);
  const peakEdge = weights.length ? Math.max(...weights) : 1;

  // Edges underneath, so a node always sits on top of its own lines.
  for (const edge of relState.edges) {
    const from = positions[edge.source], to = positions[edge.target];
    if (!from || !to) continue;
    const faded = dim(edge.source) && dim(edge.target);
    const strength = Math.max(0.06, Math.min(1, (edge.weight || 1) / peakEdge));
    ctx.strokeStyle = relRgba(relColorFor(relEdgeLayer(edge)), faded ? 0.05 : 0.12 + 0.42 * strength);
    ctx.lineWidth = 0.6 + 1.5 * strength;
    const bow = Math.min(28, Math.hypot(to.x - from.x, to.y - from.y) * 0.08);
    const midX = (from.x + to.x) / 2, midY = (from.y + to.y) / 2;
    const nx = -(to.y - from.y), ny = (to.x - from.x);
    const len = Math.hypot(nx, ny) || 1;
    ctx.beginPath();
    ctx.moveTo(from.x, from.y);
    if (typeof ctx.quadraticCurveTo === 'function') {
      ctx.quadraticCurveTo(midX + (nx / len) * bow, midY + (ny / len) * bow, to.x, to.y);
    } else {
      ctx.lineTo(to.x, to.y);
    }
    ctx.stroke();
  }

  // A focused key node highlights the keys it mixes into: the ring the DJ would
  // actually use, drawn from the data (detail.harmonic_ids), not decoration.
  if (focus) {
    const node = nodes.find(n => n.id === focus);
    const partners = (node && node.type === 'key' && node.detail && node.detail.harmonic_ids) || [];
    if (partners.length && typeof ctx.setLineDash === 'function') {
      ctx.save && ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = relRgba(relColorFor('key'), 0.7);
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      let started = false;
      for (const id of partners) {
        const spot = positions[id];
        if (!spot) continue;
        if (!started) { ctx.moveTo(spot.x, spot.y); started = true; }
        else ctx.lineTo(spot.x, spot.y);
      }
      if (started && positions[focus]) ctx.lineTo(positions[focus].x, positions[focus].y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore && ctx.restore();
    }
  }

  // Cluster captions (clusters arrangement only): which region is which layer.
  if (layout.anchors && relState.arrangement !== 'wheel') {
    ctx.font = "10px ui-monospace, 'SF Mono', Menlo, monospace";
    for (const layer of relLayerKeys()) {
      const anchor = layout.anchors[layer];
      if (!anchor || !anchor.count) continue;
      ctx.fillStyle = relRgba(relColorFor(layer), focus ? 0.35 : 0.6);
      ctx.fillText(relLayerLabel(layer).toUpperCase() + ' · ' + anchor.count, anchor.x - 34, anchor.y);
    }
  }

  // Nodes, heaviest last so the hubs are never buried.
  const ordered = nodes.slice().sort((a, b) => (a.weight || 0) - (b.weight || 0));
  for (const node of ordered) {
    const spot = positions[node.id];
    if (!spot) continue;
    const faded = dim(node.id);
    const alpha = faded ? 0.18 : 1;
    const colour = relColorFor(node.type);
    ctx.beginPath();
    if (typeof ctx.arc === 'function') ctx.arc(spot.x, spot.y, Math.max(2, spot.r || 3), 0, Math.PI * 2);
    ctx.fillStyle = relRgba(colour, alpha * 0.85);
    ctx.fill();
    ctx.strokeStyle = relRgba(colour, alpha);
    ctx.lineWidth = node.id === focus ? 2 : 1;
    ctx.stroke();
    if (node.id === focus && typeof ctx.arc === 'function') {
      ctx.beginPath();
      ctx.arc(spot.x, spot.y, Math.max(2, spot.r || 3) + 3.5, 0, Math.PI * 2);
      ctx.strokeStyle = relRgba(relColorFor('genre'), 0.9);
      ctx.stroke();
    }
  }

  // Labels last: only what is worth reading -- big nodes, the focused node and
  // the pointer's node.
  ctx.font = "10px ui-monospace, 'SF Mono', Menlo, monospace";
  const labelRadius = tools.labelRadius || 8;
  for (const node of ordered) {
    const spot = positions[node.id];
    if (!spot) continue;
    const wanted = (spot.r || 0) >= labelRadius || node.id === focus || node.id === relState.hover;
    if (!wanted) continue;
    const text = tools.title(node);
    const faded = dim(node.id);
    if (typeof ctx.strokeText === 'function') {
      ctx.lineWidth = 3;
      ctx.strokeStyle = relRgba('bg', faded ? 0.2 : 0.85);
      ctx.strokeText(text, spot.x + 6, spot.y + 3);
    }
    ctx.fillStyle = relRgba(faded ? 'muted' : 'text', faded ? 0.4 : 1);
    ctx.fillText(text, spot.x + 6, spot.y + 3);
  }
  return true;
}

// Edges are coloured by the layers they join, read from the id -> type map the
// draw pass builds once instead of scanning the node list per edge.
function relEdgeLayer(edge) {
  const types = relState.types || {};
  return types[edge.source] || types[edge.target] || 'genre';
}

// Colour helper that copes with both '#rrggbb' and 'rgba(...)' values coming
// out of the theme, and with the 'bg'/'text'/'muted' shorthand used above.
function relRgba(colour, alpha) {
  const shorthand = { bg: 'var(--bg)', text: 'var(--text)', muted: 'var(--muted)' };
  let value = shorthand[colour] || colour || '#888';
  const css = (typeof document !== 'undefined' && document.body && typeof getComputedStyle === 'function');
  if (value.startsWith('var(')) {
    const name = value.slice(4, -1).trim();
    value = css ? (getComputedStyle(document.body).getPropertyValue(name).trim() || '#888') : '#888';
  }
  if (value.startsWith('#')) {
    const hex = value.length === 4
      ? value.slice(1).split('').map(c => c + c).join('')
      : value.slice(1, 7);
    const int = parseInt(hex, 16);
    if (isFinite(int)) {
      const r = (int >> 16) & 255, g = (int >> 8) & 255, b = int & 255;
      return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }
  }
  const match = /rgba?\(([^)]+)\)/.exec(value);
  if (match) {
    const parts = match[1].split(',').map(p => p.trim());
    return 'rgba(' + parts[0] + ',' + parts[1] + ',' + parts[2] + ',' + alpha + ')';
  }
  return value;
}

// -- LEGEND, FOOTER, FINDINGS, DETAIL ---------------------------------------

function relRenderLegend() {
  const el = relEl('relLegend');
  if (!el) return false;
  const options = (relState.data && relState.data.layer_options) || [];
  const active = relEnsureLayers();
  if (!options.length) {
    el.innerHTML = '<div class="rel-empty">' + (relState.error ? 'Could not load the layers.' :
      (relState.loading ? 'Counting the library…' : 'No layers to show.')) + '</div>';
    return true;
  }
  const html = options.map(option => {
    const on = active.includes(option.key);
    const count = option.empty ? 'none yet' : String(option.nodes);
    const title = option.empty
      ? 'Nothing in this layer yet'
      : option.low_weight
        ? option.nodes + ' shown · ' + option.low_weight + ' single-track values kept out of the graph'
        : option.nodes + ' nodes';
    return '<button type="button" class="rel-chip' + (on ? ' on' : '') + (option.empty ? ' empty' : '') + '"'
      + ' style="--rel-chip:' + relEsc(relColorFor(option.key)) + '"'
      + ' data-layer="' + relEsc(option.key) + '" title="' + relEsc(option.label + ' — ' + title) + '"'
      + ' onclick="toggleRelationshipLayer(\'' + relEsc(option.key) + '\')">'
      + '<span class="rel-dot"></span>' + relEsc(option.label) + '<b>' + relEsc(count) + '</b></button>';
  }).join('');
  el.innerHTML = html;
  return true;
}

// The one-line summary at the top of the rail: what is drawn, in which
// arrangement, and which node is the biggest hub -- the answer to "what am I
// looking at?" without reading the canvas.
function relRenderSummary() {
  const el = relEl('relSummary');
  if (!el) return false;
  const stats = (relState.data && relState.data.stats) || null;
  if (!stats) {
    el.textContent = relState.loading ? 'Counting the library…' : 'Nothing counted yet.';
    return true;
  }
  const hubs = (relState.data && relState.data.hubs) || [];
  const hub = hubs.length ? hubs[0] : null;
  const tools = relTools();
  const layers = relEnsureLayers().length;
  el.textContent = (relState.arrangement === 'wheel' ? 'Camelot wheel' : 'Clusters')
    + ' · ' + layers + ' of ' + relLayerKeys().length + ' layers'
    + (hub && tools ? ' · heaviest: ' + tools.title(hub) + ' (' + tools.trackCount(hub.weight) + ')' : '')
    + (relState.focus ? ' · focused' : '');
  return true;
}

function relRenderFooter() {
  const el = relEl('relFooter');
  if (!el) return false;
  const stats = (relState.data && relState.data.stats) || null;
  if (!stats) { el.textContent = 'No graph yet.'; return true; }
  const parts = [stats.songs + ' tracks', stats.nodes + ' nodes'];
  parts.push(stats.edges ? stats.edges + ' links' : 'no links yet');
  if (stats.edges) {
    const strongest = (relState.edges || []).reduce((peak, edge) => Math.max(peak, edge.weight || 0), 0);
    if (strongest) parts.push('strongest shares ' + strongest + (strongest === 1 ? ' track' : ' tracks'));
  }
  if (stats.omitted_edges) parts.push(stats.omitted_edges + ' weaker links not drawn');
  if (stats.omitted_nodes) parts.push(stats.omitted_nodes + ' weaker nodes not drawn');
  if (!stats.edges && !stats.nodes) parts.push('nothing to relate yet');
  el.textContent = parts.join(' · ');
  return true;
}

function relRenderFindings() {
  const el = relEl('relFindings');
  if (!el) return false;
  const findings = (relState.data && relState.data.findings) || [];
  if (!findings.length) {
    el.innerHTML = '<div class="rel-empty">Nothing to flag: the counts here are all clean.</div>';
    return true;
  }
  el.innerHTML = findings.map(finding => {
    // A finding can offer more than one button (a thin crate both selects its
    // tracks and goes to the crates); `action` is the primary one, so a payload
    // from an older panel still renders a single button.
    const buttons = (Array.isArray(finding.actions) && finding.actions.length)
      ? finding.actions : (finding.action ? [finding.action] : []);
    const action = buttons.map(button =>
      '<button type="button" class="rel-act" onclick="runRelationshipAction('
      + relEsc(JSON.stringify(button)) + ')">' + relEsc(button.label || 'Open') + '</button>').join('');
    return '<div class="rel-finding ' + relEsc(finding.severity || 'info') + '">'
      + '<div class="rel-finding-top"><span class="rel-dot ' + relEsc(finding.severity || 'info') + '"></span>'
      + '<span class="rel-finding-title">' + relEsc(finding.title) + '</span></div>'
      + '<div class="rel-finding-detail">' + relEsc(finding.detail) + '</div>'
      + (action ? '<div class="rel-finding-acts">' + action + '</div>' : '')
      + '</div>';
  }).join('');
  return true;
}

// The rows the detail card shows for one node: always what it is and how heavy
// it is, then whatever only that layer knows (key notation, crate identity,
// which track a transition node stands for).
function relationshipNodeDetail(nodeId) {
  const tools = relTools();
  if (!tools) return null;
  const nodes = (relState.data && relState.data.nodes) || [];
  const node = nodes.find(n => n.id === nodeId);
  if (!node) return null;
  const detail = node.detail || {};
  const rows = [];
  const degrees = (function () {
    const map = {};
    for (const edge of relState.edges || []) {
      map[edge.source] = (map[edge.source] || 0) + 1;
      map[edge.target] = (map[edge.target] || 0) + 1;
    }
    return map;
  })();
  const neighbours = (relState.edges || []).filter(e => e.source === nodeId || e.target === nodeId).length;

  if (node.type === 'key') {
    if (detail.notation) rows.push({ label: 'Notation', value: String(detail.notation) });
    const partners = (detail.harmonic_ids || []).map(id => {
      const other = nodes.find(n => n.id === id);
      return other ? tools.title(other) : id;
    });
    const empty = (detail.harmonic_ids || []).length === 0;
    rows.push({ label: 'Mixes into', value: empty ? 'no tracks in its neighbouring keys' : partners.join(', ') });
  } else if (node.type === 'crate') {
    rows.push({ label: 'Crate', value: detail.smart ? 'smart crate (rules)' : 'manual crate' });
  } else if (node.type === 'transition') {
    if (detail.artist) rows.push({ label: 'Artist', value: String(detail.artist) });
    rows.push({ label: 'Mix links', value: String(detail.links || 0) });
  } else if (node.type === 'genre') {
    if (detail.primary_key) rows.push({ label: 'Mostly in', value: String(detail.primary_key) });
    if (detail.junk) rows.push({ label: 'Looks like', value: 'a filing accident, not a genre' });
  } else if (detail.primary_key) {
    rows.push({ label: 'Mostly in', value: String(detail.primary_key) });
  }
  const songs = (relState.data && relState.data.stats) ? relState.data.stats.songs : 0;
  if (songs && node.type !== 'transition') {
    rows.push({ label: 'Share', value: Math.round((node.weight / songs) * 1000) / 10 + '% of the library' });
  }
  rows.push({ label: 'Links', value: neighbours + ' of ' + (degrees[nodeId] || 0) + ' drawn' });

  const actions = [];
  const selectable = node.song_ids || [];
  if (selectable.length) {
    // The payload caps a node's id list, so the label promises only what the
    // button will really select and says so when there are more tracks.
    const extra = Number(node.songs || selectable.length) > selectable.length
      ? ' of ' + Number(node.songs) : '';
    actions.push({ kind: 'select-songs', target: node.id, song_ids: selectable,
                   label: 'Select ' + relTrackCount(selectable.length) + extra });
  }
  if (node.type === 'crate') actions.push({ kind: 'open', label: 'Go to crates', target: 'crates' });
  if (node.type === 'transition') actions.push({ kind: 'open', label: 'Go to transitions', target: 'transitions' });
  if (node.type === 'genre') actions.push({ kind: 'open', label: 'Review tags vs genres', target: 'tag-review' });
  return {
    node,
    title: tools.title(node),
    subtitle: relLayerLabel(node.type) + ' · ' + relTrackCount(node.weight),
    rows,
    actions,
  };
}

function relRenderDetail() {
  const el = relEl('relDetail');
  if (!el) return false;
  const detail = relState.focus ? relationshipNodeDetail(relState.focus) : null;
  relShow(el, true);
  if (!detail) {
    const hubs = (relState.data && relState.data.hubs) || [];
    el.innerHTML = '<div class="rel-empty">' + (hubs.length
      ? 'Click any node to see what it is and what it overlaps with.'
      : 'Nothing drawn yet.') + '</div>';
    return true;
  }
  const rows = detail.rows.map(row =>
    '<div class="rel-row"><span class="rel-k">' + relEsc(row.label) + '</span>'
    + '<span class="rel-v">' + relEsc(row.value) + '</span></div>').join('');
  const actions = detail.actions.map(action =>
    '<button type="button" class="rel-act" onclick="runRelationshipAction('
    + relEsc(JSON.stringify(action)) + ')">' + relEsc(action.label) + '</button>').join('')
    + '<button type="button" class="rel-act ghost" onclick="clearRelationshipFocus()">Clear focus</button>';
  el.innerHTML = '<div class="rel-detail-hdr">'
    + '<span class="rel-dot" style="--rel-chip:' + relEsc(relColorFor(detail.node.type)) + '"></span>'
    + '<span class="rel-detail-title">' + relEsc(detail.title) + '</span></div>'
    + '<div class="rel-detail-sub">' + relEsc(detail.subtitle) + '</div>'
    + '<div class="rel-rows">' + rows + '</div>'
    + '<div class="rel-finding-acts">' + actions + '</div>';
  return true;
}

// -- LAYERS AND MENUS -------------------------------------------------------

function toggleRelationshipLayer(layer) {
  const tools = relTools();
  if (!tools) return false;
  const before = relEnsureLayers();
  const after = tools.toggle(before, layer, tools.layers);
  if (after.length === before.length && after.every((l, i) => l === before[i])) {
    relStatus('Keep at least one layer on.');
    return false;
  }
  relState.layers = after;
  relState.focus = null;
  relStore(REL_STORE.layers, after.join(','));
  relRenderLayerMenu();
  return loadRelationships(true) && true;
}

// + / − / … : the same menu in three moods. Add lists the layers that are off,
// remove lists the ones that are on, more holds arrangement and resets.
function openRelationshipLayerMenu(mode) {
  const wanted = String(mode || 'more');
  relState.menu = relState.menu === wanted ? null : wanted;
  relRenderLayerMenu();
  return relState.menu;
}

function relRenderLayerMenu() {
  const el = relEl('relLayerMenu');
  if (!el) return false;
  if (!relState.menu) {
    el.innerHTML = '';
    el.classList && el.classList.remove('open');
    relShow(el, false);
    return true;
  }
  const options = (relState.data && relState.data.layer_options) || [];
  const active = relEnsureLayers();
  const heading = { add: 'Add a layer', remove: 'Remove a layer', more: 'More' }[relState.menu];
  let rows = [];
  if (relState.menu === 'add') {
    rows = options.filter(o => !active.includes(o.key)).map(o =>
      relMenuRow(o, 'off', 'toggleRelationshipLayer'));
    if (!rows.length) rows.push('<div class="rel-empty">Every layer is already on.</div>');
  } else if (relState.menu === 'remove') {
    rows = options.filter(o => active.includes(o.key)).map(o =>
      relMenuRow(o, 'on', 'toggleRelationshipLayer'));
  } else {
    rows = [
      '<button type="button" class="rel-menu-row" onclick="setRelationshipsArrangement(\'clusters\')">'
      + 'Clusters' + (relState.arrangement === 'clusters' ? ' ✓' : '') + '</button>',
      '<button type="button" class="rel-menu-row" onclick="setRelationshipsArrangement(\'wheel\')">'
      + 'Camelot wheel' + (relState.arrangement === 'wheel' ? ' ✓' : '') + '</button>',
      '<button type="button" class="rel-menu-row" onclick="relShowOnlyFocusedLayer()">'
      + 'Show only this layer</button>',
      '<button type="button" class="rel-menu-row" onclick="relResetWidth()">Reset rail width</button>',
      '<button type="button" class="rel-menu-row" onclick="refreshRelationships()">Count the library again</button>',
      '<button type="button" class="rel-menu-row" onclick="hideRelationships()">Hide the rail</button>',
    ];
  }
  el.innerHTML = '<div class="rel-menu-head">' + relEsc(heading) + '</div>' + rows.join('');
  el.classList && el.classList.add('open');
  relShow(el, true);
  return true;
}

function relMenuRow(option, state, handler) {
  const count = option.empty ? 'none yet' : String(option.nodes);
  return '<button type="button" class="rel-menu-row ' + (state === 'on' ? 'on' : 'off') + '"'
    + ' onclick="' + handler + '(\'' + relEsc(option.key) + '\')">'
    + '<span class="rel-dot" style="--rel-chip:' + relEsc(relColorFor(option.key)) + '"></span>'
    + relEsc(option.label) + '<b>' + relEsc(count) + '</b>'
    + '<span class="rel-menu-state">' + (state === 'on' ? 'shown' : 'hidden') + '</span></button>';
}

// "Show only this layer": isolates whatever the focus (or the strongest node)
// belongs to, which is the quickest way out of a busy graph.
function relShowOnlyFocusedLayer() {
  const nodes = (relState.data && relState.data.nodes) || [];
  const focus = relState.focus ? nodes.find(n => n.id === relState.focus) : null;
  const layer = focus ? focus.type
    : (nodes.slice().sort((a, b) => (b.weight || 0) - (a.weight || 0))[0] || {}).type;
  if (!layer) return false;
  relState.layers = [layer];
  relStore(REL_STORE.layers, layer);
  relState.menu = null;
  return loadRelationships(true) && true;
}

function setRelationshipsArrangement(name) {
  const tools = relTools();
  relState.arrangement = tools ? tools.arrangement(name) : (name === 'wheel' ? 'wheel' : 'clusters');
  relState.menu = null;
  relStore(REL_STORE.arrangement, relState.arrangement);
  renderRelationships();
  return relState.arrangement;
}

// -- FOCUS AND ACTIONS ------------------------------------------------------

function focusRelationshipNode(nodeId) {
  const nodes = (relState.data && relState.data.nodes) || [];
  if (nodeId && !nodes.some(n => n.id === nodeId)) return false;
  relState.focus = nodeId || null;
  renderRelationships();
  const detail = relEl('relDetail');
  if (detail && detail.scrollIntoView) {
    try { detail.scrollIntoView({ block: 'nearest' }); } catch (e) { /* older browsers */ }
  }
  return relState.focus;
}

function clearRelationshipFocus() {
  relState.focus = null;
  renderRelationships();
  return true;
}

// The panel's payoff: every count can become a selection in the table, so a
// finding like "154 tracks have no genre" ends with those tracks selected and
// the ordinary bulk tools (edit, add to crate, delete) available on them.
function selectRelationshipSongs(ids) {
  const list = (ids || []).map(id => parseInt(id, 10)).filter(Number.isInteger);
  if (!list.length) {
    relStatus('Nothing to select for that one.');
    return 0;
  }
  const set = relSelectionSet();
  if (set && typeof set.add === 'function') {
    if (typeof set.clear === 'function') set.clear();
    list.forEach(id => set.add(id));
  }
  let marked = 0;
  list.forEach(id => {
    if (set) { relCall('updateRowSelection', id); marked++; return; }
    const row = relEl('row-' + id);
    if (row && row.classList) { row.classList.add('selected'); marked++; }
  });
  relCall('updateSelectionBar');
  relCall('showTab', 'library');
  relStatus(relTrackCount(list.length) + ' selected — use the selection toolbar to edit, crate or delete them.');
  return marked;
}

// The tracks an action should select, looked up by the id it carries: a
// finding's own list, or the tracks behind a node. Actions that already carry
// their ids (the node detail card does) never need this.
function relActionSongs(target) {
  const findings = (relState.data && relState.data.findings) || [];
  const hit = findings.find(finding => finding.id === target);
  if (hit) return hit.song_ids || [];
  const nodes = (relState.data && relState.data.nodes) || [];
  const node = nodes.find(candidate => candidate.id === target);
  return node ? (node.song_ids || []) : [];
}

function runRelationshipAction(action) {
  if (!action || typeof action !== 'object') return false;
  const kind = String(action.kind || '');
  const target = action.target == null ? '' : String(action.target);
  if (kind === 'select-songs') {
    const ids = (action.song_ids && action.song_ids.length) ? action.song_ids : relActionSongs(target);
    return selectRelationshipSongs(ids) > 0;
  }
  if (kind === 'focus') return !!focusRelationshipNode(target);
  if (kind === 'open') {
    if (target === 'tag-review') {
      relCall('showTab', 'tools');
      relCall('loadTagSeparation');
      relStatus('Opened the tags-vs-genres review in Tools.');
      return true;
    }
    if (target === 'new-crate') { relCall('openCrateModal'); return true; }
    const done = relCall('showTab', target || 'library');
    if (done) relStatus('Opened ' + (target || 'library') + '.');
    return done;
  }
  if (kind === 'run-analysis') {
    relCall('showTab', 'settings');
    const started = relCall('analyzeBpm');
    relStatus(started ? 'Started BPM & key analysis.' : 'Analysis lives in Settings → Analyse BPM & Key.');
    return started;
  }
  relStatus('That finding has no action here yet.');
  return false;
}

// -- CANVAS POINTER ---------------------------------------------------------

function relPointer(event) {
  const canvas = relEl('relCanvas');
  if (!canvas || !event) return null;
  const box = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : null;
  if (!box) return null;
  return { x: event.clientX - box.left, y: event.clientY - box.top };
}

function relNodeAt(event) {
  const tools = relTools();
  const point = relPointer(event);
  if (!tools || !point || !relState.layout) return null;
  return tools.hitTest(relState.layout, point.x, point.y);
}

function onRelationshipCanvasMove(event) {
  const id = relNodeAt(event);
  if (id === relState.hover) return id;
  relState.hover = id;
  const tip = relEl('relTooltip');
  if (tip) {
    if (!id) {
      tip.style.display = 'none';
    } else {
      const node = (relState.visible || []).find(n => n.id === id);
      const tools = relTools();
      tip.textContent = node ? tools.title(node) + ' · ' + tools.trackCount(node.weight) : id;
      tip.style.display = 'block';
      const point = relPointer(event);
      if (point) { tip.style.left = Math.round(point.x + 10) + 'px'; tip.style.top = Math.round(point.y + 10) + 'px'; }
    }
  }
  drawRelationshipGraph();
  return id;
}

function onRelationshipCanvasLeave() {
  relState.hover = null;
  const tip = relEl('relTooltip');
  if (tip) tip.style.display = 'none';
  drawRelationshipGraph();
  return true;
}

function onRelationshipCanvasClick(event) {
  const id = relNodeAt(event);
  if (!id) return clearRelationshipFocus();
  return focusRelationshipNode(id);
}

// -- INIT -------------------------------------------------------------------

// Markup may already wire its own handlers (onclick="…"), so listeners are only
// attached for elements that did not: no control ever fires twice.
function relBind(el, event, handler, options) {
  if (!el || typeof el.addEventListener !== 'function') return false;
  const attribute = 'on' + event;
  if (el.getAttribute && el.getAttribute(attribute)) return false;
  el.addEventListener(event, handler, options);
  return true;
}

function relRestoreState() {
  const tools = relTools();
  if (!relState.layers) relState.layers = tools ? tools.layers.slice() : relLayerKeys();
  const storedRail = relStore(REL_STORE.rail);
  if (storedRail === 'open' || storedRail === 'hidden' || storedRail === 'max') relState.rail = storedRail;
  const storedWidth = parseInt(relStore(REL_STORE.width) || '', 10);
  relState.width = (tools ? tools.clamp(storedWidth) : storedWidth) || 420;
  relState.arrangement = tools ? tools.arrangement(relStore(REL_STORE.arrangement)) : 'clusters';
  const storedLayers = relStore(REL_STORE.layers);
  if (storedLayers) relState.layers = tools ? tools.parse(storedLayers, tools.layers) : relLayerKeys();
  relApplyRailState();
}

function initRelationships() {
  relRestoreState();
  relBind(relEl('relGutter'), 'mousedown', startRelationshipResize);
  relBind(relEl('relCanvas'), 'mousemove', onRelationshipCanvasMove);
  relBind(relEl('relCanvas'), 'mouseleave', onRelationshipCanvasLeave);
  relBind(relEl('relCanvas'), 'click', onRelationshipCanvasClick);
  const gutter = relEl('relGutter');
  if (gutter) relBind(gutter, 'dblclick', relResetWidth);
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', (event) => {
      if (!relState.menu) return;
      const menu = relEl('relLayerMenu');
      const target = event && event.target;
      if (menu && menu.contains && target && menu.contains(target)) return;
      const buttons = ['relAddBtn', 'relRemoveBtn', 'relMoreBtn'];
      if (target && target.id && buttons.includes(target.id)) return;
      relState.menu = null;
      relRenderLayerMenu();
    });
  }
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('resize', () => {
      if (relState.resizeTimer) clearTimeout(relState.resizeTimer);
      relState.resizeTimer = setTimeout(() => { if (relState.rail !== 'hidden') renderRelationships(); }, 120);
    });
  }
  const body = relEl('relBody');
  if (body && typeof ResizeObserver === 'function') {
    try {
      new ResizeObserver(() => {
        if (relState.resizeTimer) clearTimeout(relState.resizeTimer);
        relState.resizeTimer = setTimeout(() => { if (relState.rail !== 'hidden') renderRelationships(); }, 120);
      }).observe(body);
    } catch (e) { /* no observer: the window resize above still covers it */ }
  }
  relSyncControls();
  if (relState.rail !== 'hidden') loadRelationships();
  else relRenderLegend();
  return true;
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('DOMContentLoaded', initRelationships, { once: true });
}
