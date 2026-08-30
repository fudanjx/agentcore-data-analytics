const state = { bucket: null, table: null, mode: 'append', review: null, isAdmin: false };
const $ = (id) => document.getElementById(id);
const terminalStates = ['SUCCEEDED', 'FAILED', 'ERROR', 'TIMEOUT', 'STOPPED'];

function selectedTable() { return state.mode === 'create' ? $('new-table').value.trim().replaceAll('-', '_') : state.table; }
function bucketQuery() { return new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace }); }
function userTag() { return $('reporting-month').value.trim(); }
function escapeHtml(value) { const node = document.createElement('span'); node.textContent = String(value); return node.innerHTML; }
function formatTime(value) { return value ? new Date(value).toLocaleString() : 'Unavailable'; }
function clearPreflight() { state.review = null; $('review').hidden = true; $('upload').hidden = true; $('upload').disabled = true; }

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
  $('history').hidden = !state.isAdmin;
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
    select.onclick = () => { clearPreflight(); state.table = table.name; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; selectTable(); valid(); loadHistory(); };
    card.append(select);
    if (state.isAdmin) {
      const remove = document.createElement('button'); remove.className = 'danger'; remove.type = 'button'; remove.textContent = 'Delete table';
      remove.onclick = () => deleteTable(table.name); card.append(remove);
    }
    return card;
  }));
  if (!data.tables.some(table => table.name === state.table)) state.table = null;
  selectTable(); valid(); await loadHistory();
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
  const tagIsValid = userTag().length > 0;
  $('preflight').disabled = !(state.bucket && tableIsValid && hasFiles && tagIsValid);
  if (!state.bucket) $('destination-help').textContent = 'No S3 Tables bucket is assigned to this user.';
  else if (!table) $('destination-help').textContent = state.mode === 'create' ? 'Enter a new table name to continue.' : 'Select one existing table, or check “Create a new table from this upload”.';
  else if (!tableIsValid) $('destination-help').textContent = 'Table names must start with a lowercase letter and use only a-z, 0-9, and underscores (_).';
  else if (!tagIsValid) $('destination-help').textContent = 'Enter a user tag to identify this upload.';
  else if (!hasFiles) $('destination-help').textContent = `Destination: ${table}. Select one or more supported files to continue.`;
  else $('destination-help').textContent = state.mode === 'create' && table !== $('new-table').value.trim() ? `New table will be created as: ${table}` : `Destination: ${table}. Ready to review upload.`;
}
function formData() {
  const data = new FormData(); data.append('mode', state.mode); data.append('table', selectedTable());
  data.append('table_bucket_arn', state.bucket.table_bucket_arn); data.append('namespace', state.bucket.namespace);
  [...$('files').files].forEach(file => data.append('files', file)); return data;
}

function selectedTypeOverrides() {
  return Object.fromEntries(
    [...document.querySelectorAll('[data-type-override]')]
      .filter(control => !control.disabled)
      .map(control => [control.dataset.typeOverride, control.value]),
  );
}

function selectedDeduplicationColumns() {
  return [...document.querySelectorAll('[data-deduplication-column]:checked')]
    .map(control => control.dataset.deduplicationColumn);
}

function updateCreateUploadEligibility() {
  if (!state.review || state.review.mode !== 'create') return;
  const selected = selectedDeduplicationColumns();
  const notice = $('deduplication-selection-notice');
  $('upload').disabled = !state.review.accepted || selected.length === 0;
  if (notice) notice.textContent = selected.length
    ? `Selected key: ${selected.join(' + ')}. Existing-key rows will not be appended.`
    : 'Choose at least one de-duplication column before uploading.';
}

function renderPreflight(result) {
  const holder = $('review-body'); holder.replaceChildren();
  const decision = document.createElement('p');
  decision.className = result.accepted ? 'preflight-pass' : 'preflight-reject';
  decision.textContent = result.accepted
    ? 'Accepted: the upload meets the enforced schema and sanitization rules.'
    : 'Rejected: the upload does not meet the enforced schema and sanitization rules.';
  holder.append(decision);
  if (result.mode === 'create' && result.type_selections?.length) {
    const choices = document.createElement('section'); choices.className = 'type-selections';
    const rows = result.type_selections.map(choice => {
      const options = choice.allowed_target_types.map(type =>
        `<option value="${escapeHtml(type)}" ${type === choice.suggested_target_type ? 'selected' : ''}>${escapeHtml(type)}</option>`,
      ).join('');
      const samples = choice.samples_masked
        ? '<small class="sample-values">Examples are masked because this is a healthcare-sanitized column.</small>'
        : choice.sample_values?.length
          ? `<small class="sample-values">Random non-empty examples: ${choice.sample_values.map(escapeHtml).join(', ')}</small>`
          : '<small class="sample-values">No non-empty examples are available.</small>';
      return `<label class="type-selection"><span><strong>${escapeHtml(choice.column)}</strong><small>Detected: ${escapeHtml(choice.source_type)}. This choice becomes the initial table contract.</small>${samples}</span><select data-type-override="${escapeHtml(choice.column)}" ${choice.locked ? 'disabled' : ''}>${options}</select></label>`;
    }).join('');
    choices.innerHTML = `<h3>Choose initial column types</h3><p>These columns need a type conversion. The suggested type is selected from the complete first file. Up to five random, non-empty examples are shown only for this review and are not stored. Healthcare-sanitized fields remain locked as <code>STRING</code>.</p>${rows}`;
    holder.append(choices);
  }
  if (result.mode === 'create' && result.deduplication_candidates?.length) {
    const typeColumns = new Set((result.type_selections || []).map(choice => choice.column));
    const candidates = [...result.deduplication_candidates].sort((left, right) => {
      const leftPriority = typeColumns.has(left.column) ? 0 : 1;
      const rightPriority = typeColumns.has(right.column) ? 0 : 1;
      return leftPriority - rightPriority || left.column.localeCompare(right.column);
    });
    const section = document.createElement('section'); section.className = 'deduplication-selection';
    const rows = candidates.map(choice => {
      const examples = choice.samples_masked
        ? '<small class="sample-values">Examples are masked because this is a healthcare-sanitized column.</small>'
        : choice.sample_values?.length
          ? `<small class="sample-values">Random non-empty examples: ${choice.sample_values.map(escapeHtml).join(', ')}</small>`
          : '<small class="sample-values">No non-empty examples are available.</small>';
      const unavailable = choice.deduplication_eligible === false;
      const reason = unavailable ? `<small class="key-ineligible">${escapeHtml(choice.deduplication_ineligible_reason)}</small>` : '';
      const quality = choice.samples_masked ? '' : `<small>Non-empty: ${Number(choice.non_null_count || 0).toLocaleString()}; distinct: ${Number(choice.distinct_non_null_count || 0).toLocaleString()}.</small>`;
      return `<label class="deduplication-candidate ${unavailable ? 'ineligible' : ''}"><input type="checkbox" data-deduplication-column="${escapeHtml(choice.column)}" ${unavailable ? 'disabled' : ''}><span><strong>${escapeHtml(choice.column)}</strong><small>Stored type: ${escapeHtml(choice.target_type)}; detected: ${escapeHtml(choice.source_type)}.</small>${quality}${examples}${reason}</span></label>`;
    }).join('');
    section.innerHTML = `<h3>Choose de-duplication columns</h3><p>Select one stable identifier, or multiple fields for a composite key. CSN, case, HRN, MRN, and other encrypted identifiers may be selected; their examples remain masked. This selection becomes the table’s immutable de-duplication contract. Later uploads with an existing key are skipped: exact matches are counted as duplicates, while changed non-key values are counted as conflicts. Per-column non-empty and distinct counts help assess a single-column key; composite-key uniqueness is assessed by the Glue job.</p><p class="deduplication-notice" id="deduplication-selection-notice">Choose at least one de-duplication column before uploading.</p><div class="deduplication-candidates">${rows}</div>`;
    holder.append(section);
    section.querySelectorAll('[data-deduplication-column]').forEach(control => control.addEventListener('change', updateCreateUploadEligibility));
  }
  for (const file of result.files || []) {
    const item = document.createElement('article'); item.className = `preflight-file ${file.accepted ? 'accepted' : 'rejected'}`;
    const sanitized = file.sanitized_columns?.length ? file.sanitized_columns.join(', ') : 'None';
    const reasons = file.rejection_reasons || [];
    const fileDecision = file.accepted
      ? 'Accepted'
      : reasons.length
        ? `Rejected — ${reasons.join(' ')}`
        : 'Rejected — see the validation details below.';
    item.innerHTML = `<h3>${escapeHtml(file.filename)}</h3>
      <dl class="preflight-summary">
        <div><dt>Initial table schema</dt><dd>${file.target_column_count} columns</dd></div>
        <div><dt>Uploaded file schema</dt><dd>${file.source_column_count} columns</dd></div>
        <div><dt>Matching columns</dt><dd>${file.matching_column_count} (${Number(file.matching_percentage).toFixed(1)}%)</dd></div>
        <div><dt>Columns to sanitize</dt><dd>${file.sanitized_column_count}</dd></div>
      </dl>
      <p><strong>Sanitized columns:</strong> ${escapeHtml(sanitized)}</p>
      <p><strong>Decision:</strong> ${escapeHtml(fileDecision)}</p>`;
    if (file.extra_columns?.length || file.missing_columns?.length || file.type_conversions?.length || file.warnings?.length) {
      const details = document.createElement('details');
      details.innerHTML = `<summary>Technical schema details</summary><pre>${escapeHtml(JSON.stringify({
        extra_columns_ignored: file.extra_columns,
        missing_target_columns_filled_null: file.missing_columns,
        type_conversions: file.type_conversions,
        unsafe_casts: file.unsafe_casts,
        warnings: file.warnings,
      }, null, 2))}</pre>`;
      item.append(details);
    }
    holder.append(item);
  }
  if (result.rejection_reasons?.length) {
    const reasons = document.createElement('section'); reasons.className = 'preflight-reasons';
    reasons.innerHTML = `<strong>Why this upload was rejected</strong><ul>${result.rejection_reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>`;
    holder.append(reasons);
  }
}

function displayHistory(items) {
  const holder = $('history-body'); holder.replaceChildren();
  if (!items.length) { holder.textContent = 'No uploader-managed history is available for this table yet.'; return; }
  const activeRollbackUploadId = items
    .filter(item => item.status === 'SUCCESS' && item.previous_snapshot_id)
    .sort((left, right) => String(right.uploaded_at || '').localeCompare(String(left.uploaded_at || '')))[0]
    ?.upload_id;
  for (const item of items) {
    const row = document.createElement('article'); row.className = `history-row ${String(item.status || '').toLowerCase()}`;
    const files = (() => { try { return JSON.parse(item.filenames || '[]').join(', '); } catch { return item.filenames || 'Unavailable'; } })();
    const snapshot = item.previous_snapshot_id || 'No earlier snapshot (initial load)';
    const uploadActor = item.uploaded_by || 'Unknown user';
    const action = item.rollback_at
      ? `Rollback executed by ${item.rollback_by || 'Unknown user'} on ${formatTime(item.rollback_at)}`
      : `Upload executed by ${uploadActor} on ${formatTime(item.uploaded_at)}`;
    row.innerHTML = `<div><strong>User tag: ${item.reporting_month || 'Unspecified'}</strong> <span class="badge">${item.status || 'UNKNOWN'}</span><small>${files}</small><small>Rows: ${(item.rows_before ?? '—').toLocaleString?.() ?? item.rows_before ?? '—'} → ${(item.rows_after ?? '—').toLocaleString?.() ?? item.rows_after ?? '—'}; uploaded: ${(item.rows_uploaded ?? '—').toLocaleString?.() ?? item.rows_uploaded ?? '—'}</small><small>Previous snapshot: ${snapshot}</small><small>Original upload: ${uploadActor} on ${formatTime(item.uploaded_at)}</small><small>Latest action: ${action}</small>${item.error_message ? `<small class="error">${item.error_message}</small>` : ''}</div>`;
    if (item.status === 'SUCCESS' && item.previous_snapshot_id) {
      const rollback = document.createElement('button'); rollback.type = 'button'; rollback.className = 'danger'; rollback.textContent = 'Rollback upload';
      const canRollback = item.upload_id === activeRollbackUploadId;
      rollback.disabled = !canRollback;
      rollback.title = canRollback ? 'Restore the table to the snapshot before this latest upload.' : 'Only the latest successful upload can be rolled back.';
      if (canRollback) rollback.onclick = () => rollbackUpload(item);
      row.append(rollback);
    }
    holder.append(row);
  }
}

async function loadHistory() {
  if (!state.isAdmin || !state.bucket || !state.table || state.mode !== 'append') { $('history-body').textContent = state.isAdmin ? 'Select a table to view its upload history.' : ''; return; }
  const query = new URLSearchParams({ ...Object.fromEntries(bucketQuery()), table: state.table });
  const response = await fetch(`/api/upload-history?${query}`); const result = await response.json();
  if (!response.ok) { $('history-body').textContent = result.detail || 'Unable to load upload history.'; return; }
  displayHistory(result.history || []);
}

async function rollbackUpload(item) {
  const warning = `Rolling back will restore “${state.table}” to its state immediately before upload ${item.upload_id}. This removes that upload’s data. Continue?`;
  if (!confirm(warning)) return;
  $('outcome').hidden = false; $('activity').textContent = `Starting rollback for ${item.upload_id}…`; $('status').textContent = 'Rollback is starting…'; $('status').className = 'running';
  const response = await fetch('/api/rollbacks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table: state.table, table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace, upload_id: item.upload_id, confirm: true }) });
  const result = await response.json();
  if (!response.ok) { $('status').textContent = 'Rollback was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2); return; }
  $('status-body').textContent = JSON.stringify(result, null, 2); poll(result.job_run_id, result.qc_uri, 'rollback');
}

$('refresh').onclick = loadTables;
$('refresh-history').onclick = loadHistory;
$('bucket').onchange = async () => { clearPreflight(); state.bucket = JSON.parse($('bucket').value); state.table = null; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; await loadTables(); };
$('create').onchange = () => { clearPreflight(); state.mode = $('create').checked ? 'create' : 'append'; if (state.mode === 'create') state.table = null; $('new-table-wrap').hidden = state.mode !== 'create'; selectTable(); valid(); loadHistory(); };
$('new-table').oninput = () => { clearPreflight(); valid(); };
$('files').onchange = () => { clearPreflight(); valid(); };
$('reporting-month').oninput = () => { clearPreflight(); valid(); };
$('preflight').onclick = async () => {
  $('activity').textContent = 'Scanning selected file schemas…';
  const response = await fetch('/api/preflight', { method: 'POST', body: formData() });
  const result = await response.json(); if (!response.ok) { $('activity').textContent = 'Preflight failed.'; return alert(result.detail || 'Preflight failed'); }
  $('activity').textContent = result.accepted ? 'Preflight accepted. Upload may start.' : 'Preflight rejected. Correct the reported schema mismatch before uploading.';
  state.review = result; $('review').hidden = false; $('review').open = true; renderPreflight(result);
  $('upload').hidden = false; $('upload').disabled = !result.accepted;
  updateCreateUploadEligibility();
};
$('upload').onclick = async () => {
  const data = formData(); data.delete('mode'); data.delete('table'); data.delete('table_bucket_arn'); data.delete('namespace');
  data.append('request', JSON.stringify({ mode: state.mode, table: selectedTable(), table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.bucket.namespace, request_id: crypto.randomUUID(), reporting_month: userTag(), type_overrides: selectedTypeOverrides(), deduplication_columns: selectedDeduplicationColumns() }));
  $('outcome').hidden = false; $('activity').textContent = `Uploading ${$('files').files.length} file(s) to temporary S3 storage…`; $('status').textContent = 'File upload is in process…'; $('status').className = 'running';
  const response = await fetch('/api/ingestions', { method: 'POST', body: data }); const result = await response.json();
  if (!response.ok) { $('status').textContent = 'Upload was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2); return; }
  $('activity').textContent = 'Files uploaded. Recording recovery point and starting ETL…'; $('status-body').textContent = JSON.stringify(result, null, 2); poll(result.job_run_id, result.qc_uri, 'ingestion');
};
async function poll(id, qcUri, operation) {
  const response = await fetch(`/api/ingestions/${id}?operation=${operation}`); const result = await response.json();
  $('activity').textContent = result.message; $('status').textContent = result.message; $('status').className = result.state === 'SUCCEEDED' ? 'succeeded' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'failed' : 'running';
  $('status-body').textContent = JSON.stringify(result, null, 2);
  if (!terminalStates.includes(result.state)) setTimeout(() => poll(id, qcUri, operation), 5000);
  else { const qc = await fetch(`/api/qc?uri=${encodeURIComponent(qcUri)}`).then(r => r.ok ? r.json() : null); if (qc) $('status-body').textContent = JSON.stringify({ job: result, qc }, null, 2); if (result.state === 'SUCCEEDED') { await loadTables(); await loadHistory(); } }
}

loadBuckets().catch(error => { $('scope').textContent = `Unable to load assigned S3 Tables buckets: ${error}`; });
