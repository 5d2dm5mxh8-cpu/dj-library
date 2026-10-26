const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadTreeFns() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  for (const hook of [
    'renderTransitionTree(', 'transTreeEdgeHtml(', 'transTreeNodeHtml(',
    '/transition-tree?depth=', 'transTreeDepth', 'transTreeSubUrl(',
    'jumpToSong(${t.id})', 'tr-spine', 'Mix path from this song',
    'Show deeper',
  ]) {
    assert.ok(html.includes(hook), `index.html should include ${hook}`);
  }
  const start = html.indexOf('// Single builder for the tree header');
  const end = html.indexOf('// -- ADD TRANSITION PICKER');
  assert.notEqual(start, -1, 'tree script block should exist');
  assert.ok(end > start, 'tree script block should be closed');
  const src = html.slice(start, end);
  const sandbox = {
    esc: (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
    displayKey: (raw) => raw || null,
    jumpToSong: () => {},
    localStorage: { getItem: () => null, setItem: () => {} },
    window: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${src};globalThis.__tree = { transTreeEdgeHtml, transTreeNodeHtml, transTreeRootHtml, transTreeHdr, transTreeChips, transTreeSubUrl, transTreeOmittedHtml };`, sandbox);
  return sandbox.__tree;
}

test('the tree shows name, bpm, key and notes, and ends cycles', () => {
  const tree = loadTreeFns();
  const edge = {
    notes: 'blend on the break', tag: 'peak', status: 'confirmed',
    target: {
      kind: 'song', id: 2, title: 'Two', artist: 'V', bpm: 125.5,
      musical_key: '8A', children: [
        { notes: 'back around', tag: '', status: 'confirmed',
          target: { kind: 'song', id: 1, title: 'One', artist: 'V', revisit: true, children: [] } },
      ],
    },
  };
  const html = tree.transTreeEdgeHtml(edge, 0);
  assert.ok(html.includes('Two'), 'song name should render');
  assert.ok(html.includes('125.5'), 'bpm should render');
  assert.ok(html.includes('8A'), 'key should render');
  assert.ok(html.includes('blend on the break'), 'notes should render along the wire');
  assert.ok(html.includes('peak'), 'tag should render');
  assert.ok(html.includes('jumpToSong(2)'), 'song stops should jump');
  assert.ok(html.includes('visited'), 'cycle revisits should be labelled');
});

test('frontier stops offer Show deeper instead of a dead end', () => {
  const tree = loadTreeFns();
  const frontier = tree.transTreeNodeHtml({
    status: 'confirmed', notes: '', tag: '',
    target: { kind: 'song', id: 4, title: 'Four', artist: 'V', bpm: 127, musical_key: '10A', children: [], has_more: true },
  }, 0);
  assert.ok(frontier.includes('Show deeper'), 'frontier stops should offer onward navigation');
  assert.ok(frontier.includes('expandTransTreeBranch(4,0,'), 'the button should expand that branch');
  const deadEnd = tree.transTreeNodeHtml({
    status: 'confirmed', notes: '', tag: '',
    target: { kind: 'song', id: 4, title: 'Four', artist: 'V', bpm: 127, musical_key: '10A', children: [], has_more: false },
  }, 0);
  assert.ok(!deadEnd.includes('Show deeper'), 'true dead ends should stay quiet');
});

test('branch expansion URLs carry the ancestor path', () => {
  const tree = loadTreeFns();
  assert.equal(tree.transTreeSubUrl(9, ['1', '2']), '/api/songs/9/transition-tree?depth=3&exclude=1%2C2');
  assert.equal(tree.transTreeSubUrl(9, []), '/api/songs/9/transition-tree?depth=3');
});

test('keyless chipless stops leave no empty shell', () => {
  const tree = loadTreeFns();
  assert.ok(!tree.transTreeRootHtml({ title: 'X', artist: 'Y', filename: 'x.mp3', bpm: null, musical_key: null }).includes('tr-chips'),
    'a stop with no bpm and no key should render no chips span');
  const hdr = tree.transTreeHdr(1, 3);
  for (const d of [1, 2, 3, 4, 5]) assert.ok(hdr.includes(`>${d}<`), `depth ${d} should be offered`);
});

test('only the latest tree render writes into the shared mount', async () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  const start = html.indexOf('// Single builder for the tree header');
  const end = html.indexOf('// -- ADD TRANSITION PICKER');
  const src = html.slice(start, end);
  const mount = { innerHTML: '', isConnected: true, querySelector: () => null, insertAdjacentHTML() {} };
  const pending = [];
  const sandbox = {
    esc: (s) => String(s == null ? '' : s),
    displayKey: (raw) => raw || null,
    jumpToSong: () => {},
    localStorage: { getItem: () => null, setItem: () => {} },
    document: { getElementById: (id) => (id === 'transTree' ? mount : null) },
    fetch: (url) => new Promise((resolve) => {
      pending.push(() => resolve({ ok: true, json: async () => ({
        title: `Song ${url.match(/songs\/(\d+)/)[1]}`, children: [], tree_truncated: false,
      }) }));
    }),
    window: {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const r1 = vm.runInContext('renderTransitionTree(1)', sandbox);
  const r2 = vm.runInContext('renderTransitionTree(2)', sandbox);
  pending[1](); await r2;          // latest request resolves first…
  const afterLatest = mount.innerHTML;
  assert.ok(afterLatest.includes('setTransTreeDepth(2,'), 'latest render should own the mount');
  pending[0](); await r1;          // …then the stale one resolves and must not write
  assert.equal(mount.innerHTML, afterLatest, 'stale renders must not clobber the mount');
});

test('capped branches admit how many mixes are hidden', () => {
  const tree = loadTreeFns();
  const edge = {
    notes: '', tag: '', status: 'confirmed',
    target: {
      kind: 'song', id: 4, title: 'Four', artist: 'V', bpm: 127, musical_key: '10A',
      children: [{
        notes: '', tag: '', status: 'confirmed',
        target: { kind: 'song', id: 10, title: 'S10', artist: 'V', children: [], has_more: false },
      }],
      truncated: true, omitted: 55,
    },
  };
  const html = tree.transTreeEdgeHtml(edge, 0);
  assert.ok(html.includes('plus 55 more mixes'), 'capped branches should name the hidden count');
  assert.equal(tree.transTreeOmittedHtml(0, 10), '', 'no hidden mixes means no note');
  assert.ok(tree.transTreeOmittedHtml(1, 10).includes('1 more mix —'), 'singular should read naturally');
});

test('to_try hops and text leaves stay legible', () => {
  const tree = loadTreeFns();
  const trying = tree.transTreeNodeHtml({
    status: 'to_try', notes: '', tag: '',
    target: { kind: 'song', id: 3, title: 'Three', artist: 'V', bpm: 126, musical_key: '9A', children: [] },
  }, 1);
  assert.ok(trying.includes('to try'), 'to_try hops should be flagged');
  const leaf = tree.transTreeNodeHtml({
    status: 'confirmed', notes: 'want this', tag: '',
    target: { kind: 'text', text: 'Unowned Dub' },
  }, 1);
  assert.ok(leaf.includes('Unowned Dub'), 'text targets should render');
  assert.ok(leaf.includes('not in library'), 'text targets should be badged');
});
