const $ = selector => document.querySelector(selector);
let deletionPlan=null, deletionCycle=null;
async function previewDeletion(id) {
  deletionPlan=null; deletionCycle=id;
  $('#delete-job-id').value=''; $('#delete-confirm').disabled=true; $('#delete-error').textContent='';
  try {
    const data=await api(`/api/cycles/${id}/deletion-preview`,{});
    deletionPlan=data;
    $('#delete-plan').textContent=JSON.stringify(data,null,2);
    $('#delete-dialog').showModal();
  } catch(error) { $('#error').textContent=error.message; }
}
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json = value => esc(JSON.stringify(value, null, 2));
let current, draftIdentity = {}, cycles = [], previewed = false, paths = {};
let refreshFlight = null, pollTimer = null, refreshAgain = false;
let refreshGeneration = 0;
let reviewData, dirty = false;
let editRevision = 0, acceptedPayload = null;
function invalidatePreview() { editRevision++; acceptedPayload=null; previewed=false; $('#submit').disabled=true; }

function duration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds)) || Number(seconds) <= 0) return 'Duration unavailable';
  const n = Math.round(Number(seconds)), h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), s = n % 60;
  return `${h ? String(h).padStart(2,'0') + ':' : ''}${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function size(bytes) { return Number.isFinite(Number(bytes)) && bytes != null ? (bytes / 1e6).toFixed(1) + ' MB' : 'Size unavailable'; }
function sourceDetails(source) {
  return `<details><summary>${esc(source.basename || source.path?.split('/').at(-1))} · path details</summary><p class="path">${esc(source.host_path || source.path)}</p><p>Helper: ${esc(source.path)} · ARM database row: ${esc(source.arm_track_id ?? 'unavailable')}</p></details>`;
}

async function api(path, body) {
  const response = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

function outputRow(source) {
  const status = source.state === 'awaiting_finalization' ? 'Extracted; waiting for backend finalization' : source.state;
  const reason = source.reason || source.release_error || source.error;
  const actual = source.backend_metadata?.duration_validation?.actual_seconds ?? source.backend_metadata?.output_probe?.format?.duration;
  const seconds = Number(actual) > 0 ? actual : source.backend_metadata?.scan_duration;
  return `<tr data-key="source-${esc(source.source_id)}"><td>Source ${esc(source.source_id)}</td>
    <td class="${esc(source.state)}">${esc(status)}${source.state !== 'released' ? ' · DETAINED' : ''}</td>
    <td><strong>${duration(seconds)}</strong> · ${Number(actual) > 0 ? 'actual extracted' : Number(seconds)>0 ? 'scan estimate' : 'unavailable'}<br>${sourceDetails(source)}</td>
    <td>${esc(source.proposed)}<br><span class="reason">${esc(reason)}</span></td></tr>`;
}

function render(cycle) {
  const reviewable = cycle.sources.some(source => source.state !== 'released');
  return `<article><h2>${new Date(cycle.created * 1000).toLocaleString()} <span class="badge">${esc(cycle.state)}</span></h2>
    <p>Cycle ${esc(cycle.id)} · ARM job ${esc(cycle.job_id ?? 'pending')} · Extraction: <strong>${esc(cycle.backend_status ?? 'pending')}</strong> · Recognition: ${cycle.recognition_done ? 'complete' : 'pending'}</p>
     <p class="reason" ${cycle.technical_error ? '' : 'hidden'}>Technical failure: ${esc(cycle.technical_error)}</p>
     <p class="reason" ${cycle.fallback_unavailable ? '' : 'hidden'}>${esc(cycle.fallback_unavailable)}</p>
    <details data-captures><summary>Retained disc snapshots</summary><div class="snapshots" data-keyed>${(cycle.snapshots || []).filter(name => name.startsWith('capture')).map(name => {
      const original = name.replace('capture-', 'original-');
      return `<figure data-key="${esc(name)}"><a target="_blank" href="/api/cycles/${cycle.id}/evidence/${name}"><img alt="Retained disc snapshot" width="240" loading="lazy" decoding="async" data-src="/api/cycles/${cycle.id}/evidence/${name}"></a><figcaption><a target="_blank" href="/api/cycles/${cycle.id}/evidence/${original}">Original webcam image</a></figcaption></figure>`;
    }).join('')}</div></details>
    <table><thead><tr><th>Source identifier</th><th>Status</th><th>Duration / file details</th><th>Proposed name / unresolved reason</th></tr></thead>
     <tbody data-keyed>${cycle.sources.map(outputRow).join('')}</tbody></table>
    <button data-review="${cycle.id}" ${reviewable && !cycle.deletion ? '' : 'disabled'}>Identify / correct detained outputs</button>
     <div>${cycle.job_id ? `<button data-selection="${cycle.id}">Analyze retained video scan</button>${cycle.deletion?.status==='deleted' ? '<strong>Run deleted — diagnostic evidence retained</strong>' : `<button class="danger" data-delete="${cycle.id}">Delete run…</button>`}` : ''}</div>
     <p ${cycle.deletion ? '' : 'hidden'}>Deletion: ${esc(cycle.deletion?.status)} — full audit in findings below.</p>
    <a href="/api/cycles/${cycle.id}/record/md">Human-readable record</a> ·
    <a href="/api/cycles/${cycle.id}/record/json">Structured record (private paths)</a> ·
    <a href="/api/cycles/${cycle.id}/export">Export sanitized partial masterlist</a>
    <details data-findings><summary>Findings, enrichment and source mapping</summary><div data-lazy>Open to load retained evidence.</div></details></article>`;
}

function renderEvidence(cycle) {
  const evidence = cycle.sources.map(source => ({source_id:source.source_id,
    assignment:source.assignment, manual_correction:source.manual_authorized || false,
    extraction_state:source.extraction_state, number_kind:source.backend_metadata?.source_number_kind,
    provenance:source.backend_metadata?.scan_source, physical_toc:source.backend_metadata?.physical_toc_track,
    stream_comparison:source.backend_metadata?.stream_comparison}));
  return `
      <h3 ${cycle.deletion ? '' : 'hidden'}>Deletion audit</h3><pre ${cycle.deletion ? '' : 'hidden'}>${json(cycle.deletion)}</pre>
      <h3>Local observations</h3><pre>${json({requests:cycle.local_requests, results:cycle.locals})}</pre>
      <h3>External fallback</h3><pre>${json(cycle.fallback || {status:cycle.fallback_pending ? 'pending' : 'not called', reasons:cycle.fallback_reasons})}</pre>
      <h3>Combined identity / masterlist / attribution</h3><pre>${json(cycle.decision || {})}</pre>
      <h3>Backend editorial metadata (looked up or edited, not printed-disc extraction)</h3><pre>${json(cycle.backend_editorial_metadata || {})}</pre>
      <h3>Source assignments and preservation evidence</h3><pre>${json(evidence)}</pre>
      <details><summary>Detailed backend source metadata</summary><pre>${json(cycle.sources.map(s => ({source_id:s.source_id, metadata:s.backend_metadata})))}</pre></details>
      <h3>Corrections and history</h3><pre>${json({corrections:cycle.corrections, history:cycle.history})}</pre>
    `;
}

// Patch existing nodes instead of replacing cards. Details and focus survive updates.
function patchNode(node, next) {
  if (node.nodeType !== next.nodeType || node.nodeName !== next.nodeName) {
    node.replaceWith(next.cloneNode(true)); return;
  }
  if (node.nodeType === Node.TEXT_NODE) {
    if (node.data !== next.data) node.data = next.data;
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE || node.hasAttribute('data-lazy')) return;
  for (const attr of [...node.attributes]) {
    if (attr.name === 'open' || (node.tagName === 'IMG' && attr.name === 'src')) continue;
    if (!next.hasAttribute(attr.name)) node.removeAttribute(attr.name);
  }
  for (const attr of next.attributes) {
    if (node.getAttribute(attr.name) !== attr.value) node.setAttribute(attr.name, attr.value);
  }
  if (node.hasAttribute('data-keyed')) {
    const existing = new Map([...node.children].map(child => [child.dataset.key, child]));
    [...next.children].forEach((child, index) => {
      let match = existing.get(child.dataset.key);
      if (match) { patchNode(match, child); existing.delete(child.dataset.key); }
      else match = child.cloneNode(true);
      if (node.children[index] !== match) node.insertBefore(match, node.children[index] || null);
    });
    existing.forEach(child => child.remove());
    return;
  }
  const old = [...node.childNodes], fresh = [...next.childNodes];
  for (let i = 0; i < Math.max(old.length, fresh.length); i++) {
    if (!fresh[i]) old[i].remove();
    else if (!old[i]) node.append(fresh[i].cloneNode(true));
    else patchNode(old[i], fresh[i]);
  }
}

async function loadFindings(card) {
  const panel = card.querySelector('[data-findings]');
  if (!panel.open || card.detailFlight || card.detailRevision === card.dataset.revision) return;
  const revision = card.dataset.revision;
  card.detailFlight = true;
  try {
    const detail = await api(`/api/cycles/${card.dataset.id}`);
    if (!card.isConnected || !panel.isConnected || !panel.open || card.dataset.revision !== revision || String(detail.updated ?? '') !== revision) return;
    const content = card.querySelector('[data-lazy]');
    const next = document.createElement('div'); next.innerHTML = renderEvidence(detail);
    // Keep nested disclosure nodes when their contents change.
    content.removeAttribute('data-lazy');
    patchNode(content, next);
    content.setAttribute('data-lazy', '');
    card.detailRevision = revision;
  } catch (error) {
    if (card.isConnected && panel.open && card.dataset.revision === revision) $('#error').textContent = error.message;
  } finally {
    card.detailFlight = false;
    if (card.isConnected && panel.open && card.dataset.revision !== revision) loadFindings(card);
  }
}

function bindCard(card) {
  card.querySelector('[data-review]').onclick = () => edit(card.dataset.id);
  const deletion = card.querySelector('[data-delete]');
  if (deletion) deletion.onclick = () => previewDeletion(card.dataset.id);
  const selection = card.querySelector('[data-selection]');
  if (selection) selection.onclick = async () => {
    try { const data = await api(`/api/cycles/${card.dataset.id}/selection-analysis`); $('#analysis-result').textContent = JSON.stringify(data, null, 2); $('#analysis-dialog').showModal(); }
    catch (error) { $('#error').textContent = error.message; }
  };
  card.querySelector('[data-findings]').ontoggle = () => loadFindings(card);
  const captures = card.querySelector('[data-captures]');
  captures.ontoggle = () => {
    if (captures.open) captures.querySelectorAll('img[data-src]').forEach(img => { if (!img.hasAttribute('src')) img.src = img.dataset.src; });
  };
  if (captures.open) captures.ontoggle();
}

function updateCards() {
  const container = $('#cycles'), present = new Map([...container.querySelectorAll(':scope > article')].map(card => [card.dataset.id, card]));
  if (!cycles.length) { container.textContent = 'No cycles yet. Start with a manual empty opening.'; return; }
  if (!present.size) container.replaceChildren();
  let previous = null;
  for (const cycle of cycles) {
    let card = present.get(cycle.id);
    const revision = String(cycle.updated ?? '');
    if (!card || card.dataset.revision !== revision) {
      const template = document.createElement('template'); template.innerHTML = render(cycle);
      if (card) patchNode(card, template.content.firstElementChild);
      else card = template.content.firstElementChild;
      card.dataset.id = cycle.id; card.dataset.revision = revision;
      bindCard(card);
    }
    const expected = previous ? previous.nextElementSibling : container.firstElementChild;
    if (expected !== card) container.insertBefore(card, expected);
    previous = card; present.delete(cycle.id);
    loadFindings(card);
  }
  present.forEach(card => card.remove());
}

function fallbackStatus(fallback) {
  if (!fallback.configured_key_file) return 'disabled — runtime key missing';
  if (!fallback.call_limit) return 'disabled — call ceiling is zero';
  if (!fallback.spending_limit_usd) return 'disabled — spending limit missing';
  return `${fallback.model} · $${fallback.committed_cost_estimate_usd} committed of $${fallback.spending_limit_usd}`;
}

function refresh() {
  const generation = ++refreshGeneration;
  if (refreshFlight) { refreshAgain = true; return refreshFlight; }
  clearTimeout(pollTimer);
  refreshFlight = refreshOnce(generation).finally(() => {
    refreshFlight = null;
    const delay = refreshAgain ? 0 : document.hidden ? 10000 : 2000;
    refreshAgain = false;
    pollTimer = setTimeout(refresh, delay);
  });
  return refreshFlight;
}

async function refreshOnce(generation) {
  try {
    const state = await api('/api/status');
    if (generation !== refreshGeneration) return;
    cycles = state.cycles;
    paths = state.paths || {};
    $('#stage').textContent = state.camera.stage === 'ready' ? 'READY FOR DISC' : state.camera.stage;
    $('#message').textContent = state.camera.message;
    const queue = (state.queue?.drives || []).map(d => `${d.mount}: ${d.state}${d.job_status ? ' (' + d.job_status + ')' : ''}`).join(' · ') || 'queue status unknown';
    $('#backend').textContent = `Drive: ${queue} · Tray: ${state.camera.tray} · Camera: ${state.camera.connected ? 'connected' : 'idle'} · Sharpness: ${state.camera.sharpness ?? '—'} · Fallback: ${fallbackStatus(state.fallback)}`;
    $('#error').textContent = state.error || '';
    if (state.camera.connected) $('#live').src = '/api/preview.jpg?t=' + Date.now();
    if (!$('#review').open) updateCards();
  } catch (error) { if (generation === refreshGeneration) $('#error').textContent = error.message; }
}

const fields = {tv:['series','season','disc','edition','series_year'], movie:['title','year'], music:['artist','album','disc'], audiobook:['author','book','disc','part']};
$('#delete-job-id').oninput=()=>{ $('#delete-confirm').disabled=!(deletionPlan?.safe && $('#delete-job-id').value===String(deletionPlan.job_id)); };
$('#delete-cancel').onclick=()=>{deletionPlan=null; $('#delete-dialog').close();};
$('#delete-confirm').onclick=async()=>{
  if (!deletionPlan?.safe || $('#delete-job-id').value!==String(deletionPlan.job_id)) return;
  const plan=deletionPlan; deletionPlan=null; $('#delete-confirm').disabled=true;
  try {
    const result=await api(`/api/cycles/${deletionCycle}/delete-run`,{token:plan.token,confirmed_job_id:Number($('#delete-job-id').value)});
    $('#delete-plan').textContent=JSON.stringify(result,null,2);
    $('#delete-error').textContent=result.status==='deleted' ? 'Run deleted. Remove the disc, close the tray, then manually reopen it empty for the next test.' : 'Cleanup incomplete. Inspect errors; obtain a new preview before another explicit attempt.';
    await refresh();
  } catch(error) { $('#delete-error').textContent=error.message; }
};
function drawFields(identity) {
  $('#fields').innerHTML = fields[$('#media_type').value].map(field => `<label>${field}<input data-field="${field}" value="${esc(identity[field] ?? '')}" ${['season','year','series_year','disc'].includes(field) ? 'type="number" min="0"' : 'maxlength="160"'}></label>`).join('');
}

async function edit(id) {
 try {
  current = cycles.find(cycle => cycle.id === id);
  reviewData = await api(`/api/cycles/${id}/review`);
  drawReview(reviewData);
  $('#review').showModal();
 } catch(error) { $('#error').textContent = error.message; }
}

function drawReview(data) {
  invalidatePreview();
  reviewData = data;
  draftIdentity = {...data.identity};
  $('#media_type').value = draftIdentity.media_type || 'tv';
  drawFields(draftIdentity);
  const row = source => {
    source = {...source, assignment:data.draft && Object.hasOwn(data.draft,'assignments') ? data.draft.assignments[source.source_id] : source.assignment};
    return `<fieldset data-source="${source.source_id}">
    <legend>Source ${source.source_id} — <strong>${duration(source.duration_seconds)}</strong></legend>
    <p>${esc(source.duration_basis)} · ${esc(source.display_role)} · ${esc(size(source.size_bytes))} · ${esc(source.state)}</p>${sourceDetails(source)}<div class="fields">
    <label>Role<select data-role><option value="">Unassigned</option>${['tv','movie','track','music','audiobook','extra'].map(kind => `<option value="${kind}" ${source.assignment?.kind === kind ? 'selected' : ''}>${kind === 'tv' ? 'episode' : kind}</option>`).join('')}</select></label>
    <label>Episode / track / movie part number<input data-number type="number" min="0" value="${esc(source.assignment?.number ?? '')}"></label>
    <label>Extra title<input data-title value="${esc(source.assignment?.title || '')}" maxlength="160"></label>
    <label>Version / perspective (optional)<input data-version value="${esc(source.assignment?.version || '')}" maxlength="100"></label>
    </div></fieldset>`;
  };
  const editable = data.sources.filter(s => s.state !== 'released'), shorts = editable.filter(s => s.short_clip), main = editable.filter(s => !s.short_clip);
  $('#assignments').innerHTML = `<h3>Assign sources — duration is a candidate filter, not episode proof</h3>${main.map(row).join('')}${shorts.length ? `<details id="short-clips"><summary>Show ${shorts.length} short clips — all remain assignable</summary>${shorts.map(row).join('')}</details>` : ''}`;
  const candidates = data.candidates.filter(c => !c.rejected);
  function candidate(c) {
    const f = c.facts || {}, names = c.episode_names || {};
    return `<article><h4>${esc(f.series || f.title || f.book || f.album || c.id)} · Season ${esc(f.season ?? 'unknown')} · Disc ${esc(f.disc ?? 'unknown')}</h4>
      <p>${esc(c.edition_name || f.edition || 'Edition unspecified')} ${c.confirmed ? '— CONFIRMED for this cycle' : ''}</p>
      <p>Episodes: ${esc((f.episodes || []).join(', '))} · Expected count: ${f.episodes?.length ?? 'unknown'}</p>
      <ul>${Object.entries(names).map(([n,t]) => `<li>Episode ${esc(n)} — ${esc(t)}</li>`).join('')}</ul>
      <ul>${c.evidence.map(e => `<li>${esc(e.field)}: ${esc(e.status)} — ${json(e.expected)}</li>`).join('')}</ul>
      <details><summary>Reference ID, identifiers, versions and provenance</summary><pre>${json({id:c.id,printed_identifiers:c.descriptive_disc?.printed_identifiers,versions:c.collection_conventions,technical_associations:c.source_observations,provenance:c.provenance,origin:c.reference_origin,uncertainties:c.legacy_unresolved})}</pre></details>
      <button type="button" data-confirm="${esc(c.id)}">Yes, use this masterlist</button><button type="button" data-reject="${esc(c.id)}">No, wrong disc</button></article>`;
  }
  $('#masterlists').innerHTML = `<h3>Suggested Masterlist Match</h3><p>Does a reference describe this disc? Confirmation prepares identity and mappings; it does not release files.</p>${candidates.map(candidate).join('') || '<p>No applicable suggestions. Manual correction remains available.</p>'}<details><summary>Rejected candidates (${data.candidates.filter(c=>c.rejected).length})</summary>${data.candidates.filter(c=>c.rejected).map(c=>`<p>${esc(c.id)} <button type="button" data-reconsider="${esc(c.id)}">Reconsider</button></p>`).join('')}</details>`;
  $('#mapping-basis').textContent = [data.draft?.mapping_basis,...(data.draft?.warnings || [])].filter(Boolean).join(' · ');
  ['confirm','reject','reconsider'].forEach(action=>document.querySelectorAll(`[data-${action}]`).forEach(button=>button.onclick=async()=>{
    if (dirty && !window.confirm('Apply this reference action? Supported identity fields and proposals will replace unsaved edits.')) return;
    invalidatePreview();
    try {
      const result=await api(`/api/cycles/${current.id}/masterlist`,{reference_id:button.dataset[action],action});
      drawReview(result);
      if (result.preview) await requestPreview();
    } catch(error) { $('#review-error').textContent=error.message; }
  }));
  $('#proposals').textContent = '';
  $('#review-error').textContent = '';
  previewed = false;
  $('#submit').disabled = true;
  dirty = false;
}

function showPreview(sources) {
  $('#proposals').textContent = sources.map(source => `Source ${source.source_id}: ${source.state === 'released' ? source.path + ' (already released; unchanged)' : source.proposed ? (source.destination || (paths.released || reviewData.released_root) + '/' + source.proposed) + (source.collision ? ' — COLLISION: remains detained' : source.publication_ready ? ' — completed output' : ' — not complete; will remain detained until finalized') : 'REMAINS DETAINED — ' + source.reason}`).join('\n');
  previewed = true;
  $('#submit').disabled = false;
}

function readFields() {
  document.querySelectorAll('[data-field]').forEach(input => {
    if (input.value !== '') draftIdentity[input.dataset.field] = input.type === 'number' ? Number(input.value) : input.value;
    else delete draftIdentity[input.dataset.field];
  });
}

function payload() {
  readFields();
  const identity = {...draftIdentity, media_type:$('#media_type').value}, assignments = {};
  fields[identity.media_type].forEach(field => { if (draftIdentity[field] !== undefined) identity[field] = draftIdentity[field]; });
  document.querySelectorAll('[data-source]').forEach(row => {
    const kind = row.querySelector('[data-role]').value;
    if (!kind) return;
    const assignment = {kind}, number = row.querySelector('[data-number]').value;
    const title = row.querySelector('[data-title]').value, version = row.querySelector('[data-version]').value;
    if (number !== '') assignment.number = Number(number);
    if (title) assignment.title = title;
    if (version) assignment.version = version;
    assignments[row.dataset.source] = assignment;
  });
  identity.episodes = Object.values(assignments).filter(a => a.kind === 'tv').map(a => a.number).filter(n => n !== undefined);
  identity.tracks = Object.values(assignments).filter(a => ['track','music','audiobook'].includes(a.kind)).map(a => a.number).filter(n => n !== undefined);
  return {identity, assignments};
}

$('#media_type').onchange = () => { readFields(); drawFields(draftIdentity); };
$('#form').oninput = () => { invalidatePreview(); dirty = true; };
$('#form').onchange = () => { invalidatePreview(); dirty = true; };
async function requestPreview() {
  const revision=editRevision, cycleId=current.id, sent=JSON.stringify(payload());
  try {
    const result = await api(`/api/cycles/${cycleId}/preview`, JSON.parse(sent));
    if (revision!==editRevision || current.id!==cycleId || JSON.stringify(payload())!==sent) return;
    acceptedPayload=JSON.parse(sent);
    showPreview(result.sources);
    $('#review-error').textContent = '';
  } catch (error) { $('#review-error').textContent = error.message; }
}
$('#preview').onclick = requestPreview;
$('#form').onsubmit = async event => {
  event.preventDefault();
  if (!previewed || !acceptedPayload || JSON.stringify(payload())!==JSON.stringify(acceptedPayload)) return;
  try { await api(`/api/cycles/${current.id}/correct`, acceptedPayload); $('#review').close(); await refresh(); }
  catch (error) { $('#review-error').textContent = error.message; }
};
$('#cancel').onclick = () => $('#review').close();
refresh();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
