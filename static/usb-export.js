// USB export controls and progress polling.
let usbPlanSnapshot = null;
let usbExportTimer = null;
let usbLastLayoutScope = null;
let usbLastLayoutMode = null;

window.usbPickerVolumePaths=new Set();
window.usbPickerVolumeRequest=0;

async function loadUsbPickerVolumes(){
  const requestId=++window.usbPickerVolumeRequest;
  window.usbPickerVolumePaths=new Set();
  updateUsbPickerSelection(null);
  try{
    const response=await fetch('/api/export/usb/volumes');
    const data=await response.json();
    if(requestId!==window.usbPickerVolumeRequest) return;
    if(!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    window.usbPickerVolumePaths=new Set((data.volumes||[]).map(volume=>volume.path));
    if(folderPick?.path) updateUsbPickerSelection(folderPick.path);
  }catch(error){
    if(requestId!==window.usbPickerVolumeRequest) return;
    console.error('Could not load writable USB drives and SD cards:',error);
  }
}

function updateUsbPickerSelection(path){
  const allowed=!!path&&window.usbPickerVolumePaths.has(path);
  const useFolder=document.getElementById('folderUseBtn');
  if(useFolder&&folderPickTarget==='usb') useFolder.disabled=!allowed;
  const pathEl=document.getElementById('folderUsePath');
  if(pathEl&&folderPickTarget==='usb'){
    pathEl.textContent=allowed?path:(path&&path.startsWith('/Volumes/')?path+' — not eligible for export':'');
    pathEl.title=path||'';
  }
}

function populateUsbCrates(){
  const sel=document.getElementById('usbCrateSelect');
  const multi=document.getElementById('usbCrateOptions');
  const crates=window._allCrates||[];
  if(sel) sel.innerHTML='<option value="">Choose a crate…</option>'+crates.map(c=>
    `<option value="${c.id}">${esc(c.name)}${c.is_smart?' · smart':''}</option>`).join('');
  if(multi){
    multi.innerHTML=crates.length?crates.map(c=>
      `<label style="display:flex;align-items:center;gap:9px;padding:6px 7px;border-radius:6px;color:var(--text2);font-size:11px;cursor:pointer">
        <input type="checkbox" class="usb-crate-choice" value="${c.id}" onchange="updateUsbCrateSelection()">
        <span>${esc(c.name)}${c.is_smart?' <span style="color:var(--muted)">· smart</span>':''}</span>
      </label>`).join(''):'<div class="tool-hint" style="padding:8px">No crates yet.</div>';
  }
  updateUsbCrateSelection();
}

function selectedUsbCrateIds(){
  return [...document.querySelectorAll('#usbCrateOptions .usb-crate-choice:checked')]
    .map(input=>parseInt(input.value,10)).filter(Number.isInteger);
}

function updateUsbCrateSelection(){
  const count=selectedUsbCrateIds().length;
  const label=document.getElementById('usbCrateSelectionCount');
  if(label) label.textContent=count?`${count} crate${count===1?'':'s'} selected.`:'Choose one or more crates.';
  usbPlanSnapshot=null;
  const start=document.getElementById('usbExportBtn');
  if(start) start.disabled=true;
}

function setUsbCrateSelection(checked){
  document.querySelectorAll('#usbCrateOptions .usb-crate-choice').forEach(input=>{input.checked=checked;});
  updateUsbCrateSelection();
}

function invalidateUsbPlan(){
  usbPlanSnapshot=null;
  const start=document.getElementById('usbExportBtn');
  if(start) start.disabled=true;
}

function updateUsbExportHints(){
  const mode=document.getElementById('usbModeSelect')?.value||'rekordbox';
  const scope=document.getElementById('usbScopeSelect')?.value||'library';
  const hint=document.getElementById('usbModeHint');
  if(hint){
    hint.textContent=mode==='rekordbox'
      ? 'Pioneer/AlphaTheta: copies audio and playlists plus a rekordbox.xml handoff. Import it and export the USB in Rekordbox to create the player-ready database.'
      : mode==='engine'
        ? 'Denon/Engine OS: copies audio and playlists plus an XML handoff. Import it in Engine DJ, then use its USB export/Sync Manager.'
        : 'No DJ software required: copies audio, folders, portable M3U8 crate lists and a CSV catalogue. Direct browsing support depends on your player model.';
  }
  const wholeLibraryChoice=document.getElementById('usbWholeLibraryChoice');
  if(wholeLibraryChoice) wholeLibraryChoice.style.display=mode==='files'&&scope!=='library'?'':'none';
  const wholeLibraryButton=document.getElementById('usbWholeLibraryBtn');
  if(wholeLibraryButton) wholeLibraryButton.setAttribute('aria-pressed',String(scope==='library'));
  const allCratesOption=document.querySelector('#usbScopeSelect option[value="crates"]');
  if(allCratesOption) allCratesOption.textContent=mode==='files'
    ? 'All crates (separate audio folders)'
    : 'All crates (import crate playlists)';
  const crateSel=document.getElementById('usbCrateSelect');
  if(crateSel) crateSel.style.display=scope==='crate'?'':'none';
  const crateMulti=document.getElementById('usbCrateMultiSelect');
  if(crateMulti) crateMulti.style.display=scope==='selected_crates'?'':'none';
  const scopeLabel=document.getElementById('usbScopeLabel');
  if(scopeLabel) scopeLabel.textContent=mode==='files'?'2. Choose what to copy':'2. Choose what to import';
  const layoutWrap=document.getElementById('usbLayoutWrap');
  if(layoutWrap) layoutWrap.style.display=mode==='files'?'':'none';
  const layoutSel=document.getElementById('usbLayoutSelect');
  const layoutHint=document.getElementById('usbLayoutHint');
  const layoutContextChanged=scope!==usbLastLayoutScope||mode!==usbLastLayoutMode;
  if(layoutSel&&mode==='files'&&scope!=='library'&&layoutContextChanged)
    layoutSel.value='crates';
  usbLastLayoutScope=scope;
  usbLastLayoutMode=mode;
  if(layoutHint){
    if(scope==='library'){
      layoutHint.textContent=layoutSel?.value==='crates'
        ? 'Crated tracks are grouped into their crate folders; tracks with no crate go together in Crates/Uncrated.'
        : 'Each track is copied once in its library-relative folder; playlists reference those copies.';
    }else if(layoutSel?.value==='crates'){
      layoutHint.textContent=scope==='crate'
        ? 'Copies this crate into its own folder under Music/Crates.'
        : 'Copies each selected crate into its own folder under Music/Crates; shared tracks are duplicated.';
    }else{
      layoutHint.textContent=scope==='crate'
        ? 'Copies each selected track once in its library-relative folder; the crate playlist points to those files.'
        : 'Copies each selected track once in its library-relative folder; each crate playlist points to those files.';
    }
  }
  const volumeInput=document.getElementById('usbVolumeInput');
  const picker=document.querySelector('#folderPickerModal .iacts');
  if(picker) picker.classList.toggle('usb-volume-picker',folderPickTarget==='usb');
  if(volumeInput) volumeInput.oninput=invalidateUsbPlan;
  if(crateSel) crateSel.onchange=invalidateUsbPlan;
  invalidateUsbPlan();
  const start=document.getElementById('usbExportBtn');
  if(start) start.disabled=true;
  const plan=document.getElementById('usbPlanHint');
  if(plan) plan.textContent=scope==='crates'||scope==='selected_crates'
    ? mode==='files'
      ? layoutSel?.value==='crates'
        ? 'Each selected crate gets its own audio folder. Shared tracks are copied into each; check the space estimate before exporting.'
        : 'Selected tracks are copied once into library folders; crate playlists are included.'
      : 'Selected crates are imported as playlists; this handoff does not create physical crate folders.'
    : scope==='crate'
      ? mode==='files'&&layoutSel?.value==='library'
        ? 'Copies this crate once into library folders and includes its playlist.'
        : 'Choose one manual or smart crate; audio-only exports can use a dedicated crate folder.'
      : mode==='files'&&layoutSel?.value==='crates'
        ? 'Exports every library track: crate members are grouped by crate and uncrated tracks go together in Crates/Uncrated.'
        : 'Exports the full library once, including crate playlists.';
}

function selectUsbWholeLibrary(){
  const scope=document.getElementById('usbScopeSelect');
  if(!scope) return;
  scope.value='library';
  updateUsbExportHints();
}

function usbRequestBody(){
  const scope=document.getElementById('usbScopeSelect').value;
  const body={mode:document.getElementById('usbModeSelect').value,scope,
              layout:document.getElementById('usbLayoutSelect')?.value||'library',
              volume:(document.getElementById('usbVolumeInput').value||'').trim()};
  if(scope==='crate') body.crate_id=parseInt(document.getElementById('usbCrateSelect').value,10);
  if(scope==='selected_crates') body.crate_ids=selectedUsbCrateIds();
  return body;
}

function formatUsbBytes(n){
  if(!Number.isFinite(n)) return 'unknown size';
  const units=['B','KB','MB','GB','TB']; let i=0; let value=n;
  while(value>=1024&&i<units.length-1){value/=1024;i++;}
  return `${value.toFixed(i===0?0:1)} ${units[i]}`;
}

async function planUsbExport(){
  const hint=document.getElementById('usbPlanHint');
  const body=usbRequestBody();
  if(body.scope==='crate'&&!Number.isInteger(body.crate_id)){
    hint.textContent='Choose a crate first.';return;
  }
  if(body.scope==='selected_crates'&&!body.crate_ids.length){
    hint.textContent='Choose one or more crates first.';return;
  }
  if(!body.volume){hint.textContent='Choose a mounted USB drive or SD card first.';return;}
  hint.textContent='Checking selected tracks, destination and free space…';
  try{
    const r=await fetch('/api/export/usb/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||`HTTP ${r.status}`);
    usbPlanSnapshot={...body,track_count:d.track_count,copy_count:d.copy_count,bytes_required:d.bytes_required};
    const extra=d.copy_count-d.track_count;
    const notes=[];
    if((body.scope==='crates'||body.scope==='selected_crates')&&body.mode!=='files')
      notes.push('crates imported as playlists, not physical folders');
    if(d.empty_crates?.length) notes.push(`empty crates: ${d.empty_crates.join(', ')}`);
    if(d.missing_count) notes.push(`${d.missing_count} missing source file(s)`);
    if(d.unsupported_count) notes.push(`${d.unsupported_count} unsupported audio file(s) skipped`);
    const extraNote=body.mode==='files'&&extra>0?` Tracks shared by crates need ${extra} extra copies.`:'';
    hint.textContent=`Ready: ${d.track_count} track(s), ${d.copy_count} audio copy/copies (${formatUsbBytes(d.bytes_required)} required; ${formatUsbBytes(d.bytes_free)} free).${extraNote}${notes.length?' '+notes.join('; ')+'.':''}`;
    document.getElementById('usbExportBtn').disabled=false;
  }catch(e){usbPlanSnapshot=null;document.getElementById('usbExportBtn').disabled=true;hint.textContent='Cannot prepare export: '+e.message;}
}

async function startUsbExport(){
  if(!usbPlanSnapshot){await planUsbExport();if(!usbPlanSnapshot)return;}
  if(!confirm(`Create a new export folder on ${usbPlanSnapshot.volume}?\n\nThe drive or card will not be formatted or erased. An existing export will not be overwritten.`))return;
  const btn=document.getElementById('usbExportBtn');
  const status=document.getElementById('usbExportStatus');
  btn.disabled=true;status.style.display='block';status.textContent='Starting USB export…';
  try{
    const r=await fetch('/api/export/usb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(usbPlanSnapshot)});
    const d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${r.status}`);
    clearInterval(usbExportTimer);
    usbExportTimer=setInterval(()=>pollUsbExport(d.job_id),800);
    pollUsbExport(d.job_id);
  }catch(e){status.textContent='Export could not start: '+e.message;btn.disabled=false;}
}

async function pollUsbExport(jobId){
  try{
    const r=await fetch(`/api/export/usb/status/${encodeURIComponent(jobId)}`);
    const d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${r.status}`);
    const status=document.getElementById('usbExportStatus');
    const progress=d.total?`${d.done||0}/${d.total}`:'Preparing files';
    status.textContent=`${d.status==='copying'?'Copying files':d.status}: ${progress} · ${d.copied||0} copied · ${d.failed||0} failed${d.empty_crates?.length?'\nEmpty crates: '+d.empty_crates.join(', '):''}${d.path?'\nDestination: '+d.path:''}`;
    if(['complete','partial','failed'].includes(d.status)){
      clearInterval(usbExportTimer);usbExportTimer=null;
      if(d.errors?.length) status.textContent+='\n'+d.errors.slice(0,5).join('\n');
      if(d.status==='complete') toast('✅ USB export complete');
      else toast(d.status==='partial'?'⚠ USB export incomplete':'❌ USB export failed');
      document.getElementById('usbExportBtn').disabled=false;
    }
  }catch(e){
    clearInterval(usbExportTimer);usbExportTimer=null;
    document.getElementById('usbExportStatus').textContent='Could not read export progress: '+e.message;
    document.getElementById('usbExportBtn').disabled=false;
  }
}
