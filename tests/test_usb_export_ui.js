const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '..', 'static/usb-export.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'static/index.html'), 'utf8');
const usbUiScript = script.slice(script.indexOf('let usbPlanSnapshot'), script.indexOf('function usbRequestBody'));

function makeElement(value = '') {
  return { value, textContent: '', innerHTML: '', disabled: false, style: {},
    attributes: {}, setAttribute(name, content) { this.attributes[name] = content; } };
}

test('crate file exports can use per-crate folders or one-copy library folders', () => {
  const mode = makeElement('files');
  const scope = makeElement('library');
  const layout = makeElement('library');
  const hint = makeElement();
  const planHint = makeElement();
  const exportButton = makeElement();
  const wholeLibraryChoice = makeElement();
  const wholeLibraryButton = makeElement();
  const document = {
    getElementById(id) {
      return ({ usbModeSelect: mode, usbScopeSelect: scope, usbLayoutSelect: layout,
        usbLayoutWrap: makeElement(), usbLayoutHint: hint, usbPlanHint: planHint,
        usbExportBtn: exportButton, usbWholeLibraryChoice: wholeLibraryChoice,
        usbWholeLibraryBtn: wholeLibraryButton })[id] || makeElement();
    },
    querySelector() { return null; },
  };
  const sandbox = { document, window: {}, folderPickTarget: 'usb' };
  sandbox.window = sandbox;
  vm.runInNewContext(usbUiScript, sandbox, { filename: 'static/usb-export.js' });
  sandbox.updateUsbExportHints();
  assert.equal(layout.value, 'library', 'full-library export starts with one copy per track');
  assert.equal(wholeLibraryChoice.style.display, 'none');
  layout.value = 'crates';
  sandbox.updateUsbExportHints();
  assert.match(hint.textContent, /tracks with no crate.*Crates\/Uncrated/i);
  assert.match(planHint.textContent, /every library track.*Crates\/Uncrated/i);
  assert.match(html, /<option value="crates">Crate folders — one folder per crate<\/option>/);
  assert.match(html, /full library, crate-folder mode groups crate songs by crate and puts uncrated songs together in Crates\/Uncrated/);

  scope.value = 'selected_crates';
  sandbox.updateUsbExportHints();
  assert.equal(layout.value, 'crates', 'crate selections default to separate crate folders');
  assert.equal(wholeLibraryChoice.style.display, '');
  assert.equal(wholeLibraryButton.attributes['aria-pressed'], 'false');
  assert.match(hint.textContent, /each selected crate.*own folder/i);

  sandbox.selectUsbWholeLibrary();
  assert.equal(scope.value, 'library', 'whole-library action selects the full library');
  assert.equal(layout.value, 'crates', 'whole-library action retains the selected folder layout');
  assert.equal(wholeLibraryChoice.style.display, 'none');
  assert.equal(planHint.textContent.includes('Crates/Uncrated'), true);

  scope.value = 'selected_crates';
  sandbox.updateUsbExportHints();
  layout.value = 'library';
  sandbox.updateUsbExportHints();
  assert.match(hint.textContent, /each selected track once.*library-relative folder/i);
  assert.match(planHint.textContent, /copied once into library folders/i);
  assert.equal(layout.disabled, false);
});

test('selected crates are sent to the export planner and reflected in the UI', async () => {
  const volumeInputMarkup = html.match(/<input class="fi" id="usbVolumeInput"([^>]*)>/)?.[1] || '';
  assert.notEqual(volumeInputMarkup, '', 'USB destination input should be present');
  assert.doesNotMatch(volumeInputMarkup, /\breadonly\b/, 'USB destination should allow typing');
  assert.match(volumeInputMarkup, /oninput="invalidateUsbPlan\(\)"/,
    'manual destination edits should invalidate an old export plan');

  const elements = new Map([
    ['usbScopeSelect', makeElement('selected_crates')],
    ['usbModeSelect', makeElement('files')],
    ['usbLayoutSelect', makeElement('library')],
    ['usbVolumeInput', makeElement('/Volumes/SD Card')],
    ['usbPlanHint', makeElement()],
    ['usbExportBtn', makeElement()],
    ['usbCrateSelectionCount', makeElement()],
  ]);
  const choices = [
    { value: '12', checked: true },
    { value: '7', checked: false },
    { value: '25', checked: true },
  ];
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
    querySelectorAll(selector) {
      if (selector === '#usbCrateOptions .usb-crate-choice:checked') return choices.filter(item => item.checked);
      if (selector === '#usbCrateOptions .usb-crate-choice') return choices;
      return [];
    },
    querySelector() { return null; },
  };
  const calls = [];
  const sandbox = {
    document,
    window: {},
    _allCrates: [
      { id: 12, name: 'Breaks', is_smart: false },
      { id: 7, name: 'Warm-up', is_smart: true },
    ],
    esc: String,
    fetch: async (url, options) => {
      calls.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          track_count: 18, copy_count: 23, bytes_required: 1024,
          bytes_free: 4096, empty_crates: ['Breaks'],
        }),
      };
    },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(script, sandbox, { filename: 'static/usb-export.js' });

  sandbox.populateUsbCrates();
  assert.match(elements.get('usbCrateOptions').innerHTML, /Breaks/);
  assert.match(elements.get('usbCrateOptions').innerHTML, /Warm-up/);
  sandbox.updateUsbCrateSelection();
  assert.equal(elements.get('usbCrateSelectionCount').textContent, '2 crates selected.');
  assert.deepEqual(Array.from(sandbox.usbRequestBody().crate_ids), [12, 25]);

  await sandbox.planUsbExport();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/export/usb/plan');
  assert.deepEqual(JSON.parse(calls[0].options.body).crate_ids, [12, 25]);
  assert.equal(JSON.parse(calls[0].options.body).volume, '/Volumes/SD Card',
    'manually entered SD card paths should be submitted to server validation');
  assert.equal(elements.get('usbExportBtn').disabled, false);
  assert.match(elements.get('usbPlanHint').textContent, /18 track\(s\), 23 audio copy\/copies/);
  assert.match(elements.get('usbPlanHint').textContent, /empty crates: Breaks/);
});
