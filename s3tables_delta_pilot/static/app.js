const state = { bucket: null, table: null, mode: 'append', review: null, isAdmin: false };
const $ = (id) => document.getElementById(id);

function selectedTable() { return state.mode === 'create' ? $('new-table').value.trim().replaceAll('-', '_') : state.table; }
function bucketQuery() { return new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace }); }

async function loadBuckets() {
  const response = await fetch('/api/buckets'); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load assigned buckets');
  state.isAdmin = data.is_admin;
  $('bucket').replaceChildren(...data.buckets.map(bucket => {
    const option = document.createElement('option'); option.value = JSON.stringify(bucket);
    option.textContent = `${bucket.label} / ${bucket.namespace}`; return option;
  }));
  state.bucket = data.buckets[0] || null;
  $('bucket').value = state.bucket ? JSON.stringify(state.bucket) : '';
  await loadTables();
}

async function loadTables() {
  if (!state.bucket) return;
  const response = await fetch(`/api/tables?${bucketQuery()}`); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load tables');
  $('scope').textContent = `Target: ${state.bucket.label} / ${data.namespace}`;
  $('tables').replaceChildren(...data.tables.map(table => {
    const card = document.createElement('article'); card.className = 'table'; card.dataset.table = table.name;
    const select = document.createElement('button'); select.className = 'table-select'; select.type = 'button';
    select.innerHTML = `<strong>${table.name}</strong><small>Created: ${table.created_at || 'Unavailable'}</small><small>Modified: ${table.modified_at || 'Unavailable'}</small><small>Rows: ${table.row_count?.toLocaleString() ?? 'Unavailable'}</small>`;
    select.onclick = () => { state.table = table.name; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; selectTable(); valid(); };
    card.append(select);
    if (state.isAdmin) {
      const remove = document.createElement('button'); remove.className = 'danger'; remove.type = 'button'; remove.textContent = 'Delete table';
      remove.onclick = () => deleteTable(table.name); card.append(remove);
    }
    return card;
  }));
  if (!data.tables.some(table => table.name === state.table)) state.table = null;
  selectTable(); valid();
}

async function deleteTable(table) {
  if (!confirm(`Delete table “${table}”? This permanently removes the table and its data.`)) return;
  $('activity').textContent = `Deleting ${table}…`;
  const response = await fetch('/api/tables', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table, table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace }) });
  const result = await response.json(); if (!response.ok) return alert(result.detail || 'Table deletion failed');
  if (state.table === table) state.table = null;
  $('activity').textContent = `Deleted ${table}.`; await loadTables();
}

function selectTable() { document.querySelectorAll('.table').forEach(x => x.classList.toggle('selected', x.dataset.table === state.table && state.mode === 'append')); }
function valid() {
  const table = selectedTable(); const hasFiles = $('files').files.length > 0;
  const tableIsValid = typeof table === 'string' && /^[a-z][a-z0-9_]{0,254}$/.test(table);
  $('preflight').disabled = !(state.bucket && tableIsValid && hasFiles);
  if (!state.bucket) $('destination-help').textContent = 'No S3 Tables bucket is assigned to this user.';
  else if (!table) $('destination-help').textContent = state.mode === 'create' ? 'Enter a new table name to continue.' : 'Select one existing table, or check “Create a new table from this upload”.';
  else if (!tableIsValid) $('destination-help').textContent = 'Table names must start with a lowercase letter and use only a-z, 0-9, and underscores (_).';
  else if (!hasFiles) $('destination-help').textContent = `Destination: ${table}. Select one or more supported files to continue.`;
  else $('destination-help').textContent = state.mode === 'create' && table !== $('new-table').value.trim() ? `New table will be created as: ${table}` : `Destination: ${table}. Ready to review upload.`;
}
function formData() {
  const data = new FormData(); data.append('mode', state.mode); data.append('table', selectedTable());
  data.append('table_bucket_arn', state.bucket.table_bucket_arn); data.append('namespace', state.bucket.namespace);
  [...$('files').files].forEach(file => data.append('files', file)); return data;
}

$('refresh').onclick = loadTables;
$('bucket').onchange = async () => { state.bucket = JSON.parse($('bucket').value); state.table = null; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; await loadTables(); };
$('create').onchange = () => { state.mode = $('create').checked ? 'create' : 'append'; if (state.mode === 'create') state.table = null; $('new-table-wrap').hidden = state.mode !== 'create'; selectTable(); valid(); };
$('new-table').oninput = valid; $('files').onchange = valid;
$('preflight').onclick = async () => {
  $('activity').textContent = 'Scanning selected file schemas…';
  const response = await fetch('/api/preflight', { method: 'POST', body: formData() });
  const result = await response.json(); if (!response.ok) { $('activity').textContent = 'Preflight failed.'; return alert(result.detail || 'Preflight failed'); }
  $('activity').textContent = 'Preflight complete. Review details before upload.';
  state.review = result; $('review').hidden = false; $('review').open = false; $('review-body').textContent = JSON.stringify(result, null, 2);
  $('approval').hidden = !result.requires_confirmation; $('upload').hidden = false; $('upload').disabled = result.requires_confirmation && !$('allow-casts').checked;
};
$('allow-casts').onchange = () => { $('upload').disabled = !$('allow-casts').checked; };
$('upload').onclick = async () => {
  const data = formData(); data.delete('mode'); data.delete('table'); data.delete('table_bucket_arn'); data.delete('namespace');
  data.append('request', JSON.stringify({ mode: state.mode, table: selectedTable(), table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace, request_id: crypto.randomUUID(), allow_unsafe_casts: $('allow-casts').checked }));
  $('outcome').hidden = false; $('activity').textContent = `Uploading ${$('files').files.length} file(s) to temporary S3 storage…`; $('status').textContent = 'File upload is in process…'; $('status').className = 'running';
  const response = await fetch('/api/ingestions', { method: 'POST', body: data }); const result = await response.json();
  if (!response.ok) { $('status').textContent = 'Upload was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2); return; }
  $('activity').textContent = 'Files uploaded. Starting ETL…'; $('status-body').textContent = JSON.stringify(result, null, 2); poll(result.job_run_id, result.qc_uri);
};
async function poll(id, qcUri) {
  const response = await fetch(`/api/ingestions/${id}`); const result = await response.json();
  $('activity').textContent = result.message; $('status').textContent = result.message; $('status').className = result.state === 'SUCCEEDED' ? 'succeeded' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'failed' : 'running';
  $('status-body').textContent = JSON.stringify(result, null, 2);
  if (!['SUCCEEDED','FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state)) setTimeout(() => poll(id, qcUri), 5000);
  else { const qc = await fetch(`/api/qc?uri=${encodeURIComponent(qcUri)}`).then(r => r.ok ? r.json() : null); if (qc) $('status-body').textContent = JSON.stringify({ job: result, qc }, null, 2); if (result.state === 'SUCCEEDED') loadTables(); }
}
loadBuckets().catch(error => { $('scope').textContent = `Unable to load assigned S3 Tables buckets: ${error}`; });
