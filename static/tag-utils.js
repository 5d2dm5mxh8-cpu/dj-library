/* Tag helpers (tags are their own thing).
 *
 * Genre and tags are separate: `songs.genre` holds genres only -- Mixxx reads
 * it as the Genre column and the rekordbox.xml Genre attribute comes from it --
 * while tags live in the tags / song_tags tables and travel in each file's
 * [DJ Library] comment block. Everything here keeps the two apart, so a '#tag'
 * typed into a genre field becomes a tag instead of a fake genre.
 *
 * Plain functions, no DOM and no app state, so tests/test_tag_ui.js can run
 * them in a plain V8 context.
 */

// 'a; b, #c' -> ['a','b','c']: splits on semicolons, commas, newlines and the
// '#' marker (the same boundaries the download API accepts), trims, and removes
// case-insensitive duplicates.
function parseTagInput(value){
  const out=[], seen=new Set();
  for(const raw of String(value||'').split(/[;,\n#]/)){
    const part=raw.trim();
    const key=part.toLowerCase();
    if(!part||seen.has(key)) continue;
    seen.add(key);
    out.push(part);
  }
  return out;
}

// A song's tags as one editable line: ['peak hour','tool'] -> 'peak hour; tool'.
function formatTagLine(tags){
  return (tags||[]).map(t=>String(t||'').trim()).filter(Boolean).join('; ');
}

// What a smart crate's rules need stamped onto a song -- a genre rule needs a
// genre, a tag rule needs a tag: {genres:[...], tags:[...]}.
function crateRuleValues(rulesJson){
  const genres=[], tags=[], seen=new Set();
  const push=(list,label,value)=>{
    const key=label+':'+value.toLowerCase();
    if(!value||seen.has(key)) return;
    seen.add(key);
    list.push(value);
  };
  try{
    (JSON.parse(rulesJson||'[]')||[]).forEach(r=>{
      if(!r||!r.value) return;
      const value=String(r.value).trim();
      if(!value) return;
      if(r.field==='genre') push(genres, 'genre', value);
      else if(r.field==='tag') push(tags, 'tag', value);
    });
  }catch(e){ return {genres:[], tags:[]}; }
  return {genres, tags};
}

// Adds genres to a song's ';'-separated genre string without duplicating
// anything it already carries. Genres only: a '#tag' is never merged in, and a
// '#tag' already sitting in the string is dropped rather than carried along.
function mergeGenreList(existing, genres){
  const parts=String(existing||'').split(';')
    .map(s=>s.trim())
    .filter(p=>p && !p.startsWith('#'));
  for(const g of genres||[]){
    const value=String(g||'').trim();
    if(!value||value.startsWith('#')) continue;
    if(!parts.some(p=>p.toLowerCase()===value.toLowerCase())) parts.push(value);
  }
  return parts.join('; ');
}
