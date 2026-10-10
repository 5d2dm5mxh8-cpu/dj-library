const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '..', 'static', 'tag-utils.js'), 'utf8');

function loadHelpers() {
  const sandbox = {};
  vm.runInNewContext(script, sandbox, { filename: 'static/tag-utils.js' });
  return sandbox;
}

const helpers = loadHelpers();

test('tag input accepts the marker, commas and semicolons without duplicates', () => {
  assert.deepEqual(Array.from(helpers.parseTagInput('#peak hour #tool')), ['peak hour', 'tool']);
  assert.deepEqual(Array.from(helpers.parseTagInput('house, tool; peak hour')),
    ['house', 'tool', 'peak hour']);
  assert.deepEqual(Array.from(helpers.parseTagInput('Tool; tool; #TOOL')), ['Tool']);
  assert.deepEqual(Array.from(helpers.parseTagInput('   ')), []);
  assert.deepEqual(Array.from(helpers.parseTagInput(null)), []);
  assert.deepEqual(Array.from(helpers.parseTagInput('peak\nhour')), ['peak', 'hour']);
});

test('a tag line round-trips through the editor format', () => {
  assert.equal(helpers.formatTagLine(['peak hour', 'tool']), 'peak hour; tool');
  assert.equal(helpers.formatTagLine([]), '');
  assert.equal(helpers.formatTagLine(null), '');
  assert.deepEqual(Array.from(helpers.parseTagInput(helpers.formatTagLine(['a', 'b']))), ['a', 'b']);
});

test('smart crate rules split into the genres and tags they need', () => {
  const rules = JSON.stringify([
    { field: 'genre', op: 'eq', value: 'Deep House' },
    { field: 'tag', op: 'contains', value: 'tool' },
    { field: 'bpm', op: 'gte', value: 124 },
    { field: 'tag', op: 'eq', value: 'Tool' },
    { field: 'genre', op: 'eq', value: 'Deep House' },
  ]);
  const needed = helpers.crateRuleValues(rules);
  assert.deepEqual(Array.from(needed.genres), ['Deep House']);
  assert.deepEqual(Array.from(needed.tags), ['tool'], 'the same tag twice, once');
  assert.deepEqual(Array.from(helpers.crateRuleValues('not json').genres), []);
  assert.deepEqual(Array.from(helpers.crateRuleValues(null).tags), []);
});

test('genre merging never puts a tag in the genre field', () => {
  assert.equal(helpers.mergeGenreList('House', ['Tech House', '#tool']), 'House; Tech House');
  assert.equal(helpers.mergeGenreList('House; Tech House', ['house']), 'House; Tech House');
  assert.equal(helpers.mergeGenreList(null, ['Deep House']), 'Deep House');
  assert.equal(helpers.mergeGenreList('#tool', []), '');
});

test('the library UI is wired to the separated tag model', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  assert.match(html, /<script src="\/static\/tag-utils\.js"><\/script>/,
    'the tag helper module is loaded by the page');
  assert.match(html, /\{key:'tags',\s+label:'Tags',[^}]*list:'tagEditList'\}/,
    'the metadata editor has a Tags field on its own autocomplete list');
  assert.match(html, /getElementById\('info-tags'\)/, 'the editor reads and writes that field');
  assert.match(html, /fetch\('\/api\/tags'\)/, 'the autocomplete reads the tag endpoint');
  assert.match(html, /`\/api\/songs\/\$\{id\}\/tags`/, 'tagging a song goes through the tag route');
  assert.match(html, /id="tagSeparationCard"/, 'Tools offers the tag separation review screen');
  assert.match(html, /onclick="loadTagSeparation\(\)"/, 'the review screen has an explicit loader');
  assert.match(html, /'\/api\/tag-separation'/, 'the review screen talks to its own endpoint');
  assert.doesNotMatch(html, /mergeGenreTags|parseCrateTags/, 'the genre-carrying tag helpers are gone');
  assert.match(html, /<option value="tag">Tag<\/option>/, 'smart crate rules can filter on a tag');
  assert.match(html, /parseTagInput\(|formatTagLine\(|crateRuleValues\(|mergeGenreList\(/,
    'the page uses the shared helpers instead of its own copies');
  assert.doesNotMatch(html, /Tags are stored on the track's genre/i,
    'no copy still claims tags live in the genre field');
});
