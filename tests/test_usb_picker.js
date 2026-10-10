const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const usbScript = fs.readFileSync(path.join(root, 'static/usb-export.js'), 'utf8');

function sourceBetween(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.notEqual(from, -1, `missing source marker: ${start}`);
  assert.notEqual(to, -1, `missing source marker: ${end}`);
  return source.slice(from, to);
}

function makeElement() {
  const classes = new Set();
  return {
    value: '', textContent: '', title: '', innerHTML: '', disabled: false,
    addEventListener() {},
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
  };
}

function makeHarness(fetchImpl) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
    querySelector() { return null; },
    addEventListener() {},
  };
  const sandbox = {
    document,
    fetch: fetchImpl,
    console: { error() {} },
    setTimeout,
    clearTimeout,
    encodeURIComponent,
    esc(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      })[char]);
    },
    updateUsbExportHints() {},
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(sourceBetween(
    usbScript,
    'window.usbPickerVolumePaths=new Set();',
    'function populateUsbCrates()',
  ), context, { filename: 'static/usb-export.js' });
  vm.runInContext(sourceBetween(
    html,
    'let folderPick = { path: null };',
    'function setImportStatus(',
  ) + `\nglobalThis.pickerTest = {
    openFolderPicker, closeFolderPicker, browseFolder, searchFolders, usePickedFolder,
    get folderPick() { return folderPick; },
    get target() { return folderPickTarget; },
  };`, context, { filename: 'static/index.html' });
  return { api: context.pickerTest, document };
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

const eligiblePath = '/Volumes/SD Card';
const recoveryPath = '/Volumes/Recovery';

function fixtureFetch(calls) {
  return async url => {
    calls.push(String(url));
    if (String(url).startsWith('/api/export/usb/volumes')) {
      return response({ volumes: [{ name: 'SD Card', path: eligiblePath }] });
    }
    if (String(url).startsWith('/api/folders/browse?')) {
      const requested = decodeURIComponent(String(url).split('path=')[1] || '');
      const normalized = requested || '/Users/test';
      if (normalized === '/Volumes') {
        return response({
          path: normalized,
          roots: [{ label: '💾 Recovery', path: recoveryPath }, { label: '💾 SD Card', path: eligiblePath }],
          entries: [
            { name: 'Recovery', path: recoveryPath },
            { name: 'SD Card', path: eligiblePath },
          ],
        });
      }
      if (normalized === eligiblePath || normalized === recoveryPath) {
        return response({ path: normalized, roots: [], entries: [{ name: 'nested', path: `${normalized}/nested` }] });
      }
      return response({ path: normalized, roots: [{ label: 'Home', path: '/Users/test' }], entries: [] });
    }
    if (String(url).startsWith('/api/folders/search?')) {
      return response({ results: [recoveryPath] });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };
}

test('USB picker waits for eligibility and offers/selects only eligible mount roots', async () => {
  let resolveVolumes;
  const calls = [];
  const harness = makeHarness(url => {
    calls.push(String(url));
    if (String(url).startsWith('/api/export/usb/volumes')) {
      return new Promise(resolve => { resolveVolumes = resolve; });
    }
    return fixtureFetch([])(url);
  });
  const { api, document } = harness;
  const modal = document.getElementById('folderPickerModal');
  const entries = document.getElementById('folderEntries');
  const search = document.getElementById('folderSearchInput');
  const useButton = document.getElementById('folderUseBtn');
  const volumeInput = document.getElementById('usbVolumeInput');

  const opening = api.openFolderPicker('usb');
  assert.equal(search.disabled, true);
  assert.equal(useButton.disabled, true);
  assert.match(entries.innerHTML, /Finding writable USB drives and SD cards/);
  assert.equal(calls.some(url => url.startsWith('/api/folders/browse?')), false,
    'the broad folder browse must not start before the allow-list arrives');

  resolveVolumes(response({ volumes: [{ name: 'SD Card', path: eligiblePath }] }));
  await opening;
  assert.equal(api.folderPick.path, '/Volumes');
  assert.match(entries.innerHTML, /SD Card/);
  assert.doesNotMatch(entries.innerHTML, /Recovery/);
  assert.match(document.getElementById('folderRoots').innerHTML, /SD Card/);
  assert.doesNotMatch(document.getElementById('folderRoots').innerHTML, /Recovery/);
  assert.equal(useButton.disabled, true, 'the /Volumes parent itself is not selectable');

  await api.browseFolder(recoveryPath);
  assert.equal(api.folderPick.path, null);
  assert.equal(useButton.disabled, true);
  assert.match(entries.innerHTML, /Choose an eligible mounted export volume/);
  assert.equal(calls.some(url => url.includes(encodeURIComponent(recoveryPath))), false,
    'an ineligible path must be rejected before a browse request');
  api.folderPick.path = recoveryPath;
  api.usePickedFolder();
  assert.equal(volumeInput.value, '', 'even a stale/direct invalid path cannot be selected');
  assert.equal(api.folderPick.path, recoveryPath, 'use handler never writes a stale invalid path');

  await api.browseFolder(eligiblePath);
  assert.equal(api.folderPick.path, eligiblePath);
  assert.equal(useButton.disabled, false);
  assert.match(entries.innerHTML, /new export folder will be created/);
  api.usePickedFolder();
  assert.equal(volumeInput.value, eligiblePath);
  assert.equal(modal.classList.contains('open'), false);
});

test('USB picker search filters Recovery results using the eligible-root allow-list', async () => {
  const calls = [];
  const { api, document } = makeHarness(fixtureFetch(calls));
  await api.openFolderPicker('usb');
  document.getElementById('folderSearchInput').value = 'Recovery';
  await api.searchFolders('Recovery');
  assert.match(document.getElementById('folderEntries').innerHTML, /No writable USB drives or SD cards match/);
  assert.doesNotMatch(document.getElementById('folderEntries').innerHTML, /Recovery/);
  assert.equal(document.getElementById('folderUseBtn').disabled, true);
});

test('empty eligibility result and failed eligibility request fail closed', async () => {
  const calls = [];
  const { api, document } = makeHarness(async url => {
    calls.push(String(url));
    if (String(url).startsWith('/api/export/usb/volumes')) return response({ error: 'offline' }, 503);
    return fixtureFetch([])(url);
  });
  await api.openFolderPicker('usb');
  assert.equal(api.folderPick.path, '/Volumes');
  assert.equal(document.getElementById('folderUseBtn').disabled, true);
  assert.match(document.getElementById('folderEntries').innerHTML, /No writable USB drives or SD cards are mounted/);
  assert.doesNotMatch(document.getElementById('folderRoots').innerHTML, /Recovery|SD Card/);
  api.folderPick.path = '';
  api.usePickedFolder();
  assert.equal(document.getElementById('usbVolumeInput').value, '');
  assert.equal(calls.some(url => url.startsWith('/api/export/usb/volumes')), true);
});

test('late search response from USB cannot overwrite the generic picker', async () => {
  let resolveSearch;
  const harness = makeHarness(url => {
    if (String(url).startsWith('/api/folders/search')) {
      return new Promise(resolve => { resolveSearch = resolve; });
    }
    return fixtureFetch([])(url);
  });
  const { api, document } = harness;
  await api.openFolderPicker('usb');
  document.getElementById('folderSearchInput').value = 'Recovery';
  const searching = api.searchFolders('Recovery');
  await api.openFolderPicker('import');
  const genericRoots = document.getElementById('folderRoots').innerHTML;
  resolveSearch(response({ results: [recoveryPath] }));
  await searching;
  assert.equal(api.target, 'import');
  assert.equal(api.folderPick.path, '/Users/test');
  assert.equal(document.getElementById('folderRoots').innerHTML, genericRoots);
  assert.doesNotMatch(genericRoots, /Clear search/);
});

test('late eligibility response cannot replace a picker opened for another target', async () => {
  let resolveVolumes;
  const calls = [];
  const harness = makeHarness(url => {
    calls.push(String(url));
    if (String(url).startsWith('/api/export/usb/volumes')) {
      return new Promise(resolve => { resolveVolumes = resolve; });
    }
    return fixtureFetch([])(url);
  });
  const { api, document } = harness;
  const openingUsb = api.openFolderPicker('usb');
  const openingImport = api.openFolderPicker('import');
  await openingImport;
  assert.equal(api.target, 'import');
  assert.equal(api.folderPick.path, '/Users/test');
  assert.equal(document.getElementById('folderSearchInput').disabled, false);

  resolveVolumes(response({ volumes: [{ name: 'SD Card', path: eligiblePath }] }));
  await openingUsb;
  assert.equal(api.target, 'import');
  assert.equal(api.folderPick.path, '/Users/test');
  assert.equal(document.getElementById('folderPickerModal').classList.contains('open'), true);
  assert.match(document.getElementById('folderRoots').innerHTML, /Home/);
  assert.doesNotMatch(document.getElementById('folderRoots').innerHTML, /💾 SD Card/);
});
