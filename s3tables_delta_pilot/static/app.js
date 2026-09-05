const SESSION_STORAGE_KEY = 's3tables-uploader-v2-session-id';
const sessionTerminalPhases = ['READY_FOR_REVIEW', 'READY_FOR_ACKNOWLEDGEMENT', 'GLUE_RUNNING', 'FAILED'];
const state = { bucket: null, namespace: null, table: null, tableManaged: false, mode: 'append', review: null, keyAnalysis: null, keyAnalysisAcknowledged: false, isAdmin: false, userId: null, canViewHistory: false, canRollbackUploads: false, emulatedUserId: null, identityProfiles: [], sessionId: null, sessionPollTimer: null, deduplicationMode: 'keyed', lastRequestId: null };
const $ = (id) => document.getElementById(id);
const terminalStates = ['SUCCEEDED', 'FAILED', 'ERROR', 'TIMEOUT', 'STOPPED'];

function selectedTable() { return state.mode === 'create' ? $('new-table').value.trim().replaceAll('-', '_') : state.table; }
function bucketQuery() { return new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.namespace }); }
function userTag() { return $('reporting-month').value.trim(); }
function escapeHtml(value) { const node = document.createElement('span'); node.textContent = String(value); return node.innerHTML; }
function formatTime(value) { return value ? new Date(value).toLocaleString() : 'Unavailable'; }
function clearSessionPoll() { if (state.sessionPollTimer) { clearTimeout(state.sessionPollTimer); state.sessionPollTimer = null; } }
function clearPreflight({ forgetSession = true } = {}) { clearSessionPoll(); state.review = null; state.keyAnalysis = null; state.keyAnalysisAcknowledged = false; if (forgetSession) { state.sessionId = null; sessionStorage.removeItem(SESSION_STORAGE_KEY); } $('review').hidden = true; $('upload-actions').hidden = true; $('upload').disabled = true; $('upload-status').textContent = ''; $('upload-status').className = 'operation-status'; $('review-status').textContent = ''; $('review-status').className = 'operation-status'; }
function identityRequestPayload() {
  return {
    headers: { 'X-Pilot-User-Id': state.emulatedUserId },
    body_user_fields: {},
    backend_resolves: ['is_admin', 'assigned bucket/namespace scopes', 'history and rollback capabilities'],
  };
}
async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.emulatedUserId) headers.set('X-Pilot-User-Id', state.emulatedUserId);
  const response = await fetch(url, { ...options, headers });
  state.lastRequestId = response.headers.get('X-Request-ID') || state.lastRequestId;
  return response;
}
function updateSkillControls() {
  const hasBucket = Boolean(state.bucket);
  const hasFiles = $('skill-bundle-files').files.length > 0;
  const confirmed = $('skill-confirm-replace').checked;
  $('skill-builder').hidden = !hasBucket;
  $('upload-skill-bundle').disabled = !hasBucket || !hasFiles || !confirmed;
}
function clearSkillBundle() {
  $('skill-bundle-files').value = '';
  $('skill-confirm-replace').checked = false;
  $('skill-bundle-status').textContent = '';
  $('skill-bundle-status').className = 'operation-status';
  updateSkillControls();
}
async function uploadSkillBundle() {
  if (!state.bucket) return;
  const files = [...$('skill-bundle-files').files];
  const paths = files.map(file => file.webkitRelativePath || file.name);
  const button = $('upload-skill-bundle'); const status = $('skill-bundle-status');
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Uploading skill bundle…';
  status.className = 'operation-status'; status.textContent = 'Validating the complete bundle and replacing the S3 skill files…';
  try {
    const form = new FormData();
    form.append('table_bucket_arn', state.bucket.table_bucket_arn);
    form.append('paths_json', JSON.stringify(paths));
    form.append('confirm_replace', 'true');
    files.forEach(file => form.append('files', file, file.name));
    const response = await apiFetch('/api/skills/upload-bundle', { method: 'POST', body: form });
    const result = await response.json();
    if (!response.ok) { status.className = 'operation-status failed'; status.textContent = result.detail || 'Skill bundle upload failed.'; return; }
    status.className = 'operation-status complete'; status.textContent = `Published ${result.uploaded_paths.length} files to ${result.destination_uri}. ${result.deleted_paths.length} stale files removed. ${result.restart_reminder}`;
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = `Skill bundle upload failed: ${error.message || 'network request failed'}`;
  } finally {
    button.classList.remove('is-busy'); button.textContent = 'Upload replacement skill bundle'; updateSkillControls();
  }
}
function renderOutgoingIdentity() { $('outgoing-identity').textContent = JSON.stringify(identityRequestPayload(), null, 2); }
function renderAdminProvisioning() {
  $('admin-provisioning').hidden = !state.isAdmin;
  const bucketName = $('new-bucket').value.trim();
  const namespace = $('new-namespace').value.trim();
  $('create-bucket').disabled = !state.isAdmin || !/^[a-z0-9-]{3,63}$/.test(bucketName);
  $('create-namespace').disabled = !state.isAdmin || !state.bucket || !/^[a-z][a-z0-9_]{0,254}$/.test(namespace);
}
function clearDestination() {
  state.bucket = null; state.namespace = null; state.table = null; state.tableManaged = false; state.isAdmin = false; state.userId = null;
  state.canViewHistory = false; state.canRollbackUploads = false;
  $('bucket').replaceChildren(); $('namespace').replaceChildren(); $('namespace').disabled = true; $('tables').replaceChildren(); $('history').hidden = true;
  $('admin-provisioning').hidden = true;
  clearPreflight(); clearSkillBundle(); valid();
}
async function loadEffectiveIdentity() {
  renderOutgoingIdentity();
  const response = await apiFetch('/api/identity'); const data = await response.json();
  if (!response.ok) {
    $('effective-identity').textContent = JSON.stringify({ authorization: 'DENIED', detail: data.detail || 'No configured scope for this user.' }, null, 2);
    return null;
  }
  $('effective-identity').textContent = JSON.stringify(data, null, 2);
  return data;
}
async function loadIdentityProfiles() {
  const response = await fetch('/api/dev/identity-profiles'); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load local identity profiles');
  state.identityProfiles = data.profiles || [];
  $('emulated-user').replaceChildren(...state.identityProfiles.map(profile => {
    const option = document.createElement('option'); option.value = profile.user_id;
    option.textContent = `${profile.user_id} — ${profile.is_admin ? 'administrator' : profile.expected_access ? profile.can_rollback_uploads ? 'scoped editor with recovery' : 'scoped non-admin' : 'unassigned (denied)'}`;
    return option;
  }));
  state.emulatedUserId = state.identityProfiles[0]?.user_id || null;
  $('emulated-user').value = state.emulatedUserId || '';
  renderOutgoingIdentity();
}

async function loadBuckets(preferredBucket = null, { preserveSession = false, preferredNamespace = null } = {}) {
  const previousBucketArn = state.bucket?.table_bucket_arn || null;
  const identity = await loadEffectiveIdentity();
  if (!identity) { clearDestination(); return; }
  const response = await apiFetch('/api/buckets'); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load assigned buckets');
  state.isAdmin = data.is_admin;
  state.userId = data.user_id;
  state.canViewHistory = Boolean(data.can_view_upload_history);
  state.canRollbackUploads = Boolean(data.can_rollback_uploads);
  const preferredBucketArn = typeof preferredBucket === 'string' ? preferredBucket : preferredBucket?.table_bucket_arn;
  const buckets = [...data.buckets];
  if (preferredBucket && typeof preferredBucket === 'object' && !buckets.some(bucket => bucket.table_bucket_arn === preferredBucketArn)) {
    buckets.push(preferredBucket);
  }
  $('bucket').replaceChildren(...buckets.map(bucket => {
    const option = document.createElement('option'); option.value = JSON.stringify(bucket);
    option.textContent = bucket.label; return option;
  }));
  state.bucket = buckets.find(bucket => bucket.table_bucket_arn === preferredBucketArn) || buckets[0] || null;
  $('bucket').value = state.bucket ? JSON.stringify(state.bucket) : '';
  $('bucket').disabled = !buckets.length;
  $('history').hidden = !state.canViewHistory;
  if (previousBucketArn !== state.bucket?.table_bucket_arn) clearSkillBundle();
  else updateSkillControls();
  renderAdminProvisioning();
  await loadNamespaces(preferredNamespace, { preserveSession });
}

async function loadNamespaces(preferredNamespace = null, { preserveSession = false } = {}) {
  state.namespace = null; state.table = null; state.tableManaged = false;
  $('namespace').replaceChildren(); $('tables').replaceChildren(); clearPreflight({ forgetSession: !preserveSession });
  if (!state.bucket) { $('namespace').disabled = true; $('scope').textContent = state.isAdmin ? 'Create an S3 Tables bucket to begin.' : 'No assigned S3 Tables bucket.'; renderAdminProvisioning(); valid(); return; }
  const query = new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn });
  const response = await apiFetch(`/api/namespaces?${query}`); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load namespaces');
  const namespaces = [...data.namespaces];
  if (preferredNamespace && !namespaces.includes(preferredNamespace)) namespaces.push(preferredNamespace);
  $('namespace').replaceChildren(...namespaces.map(namespace => {
    const option = document.createElement('option'); option.value = namespace; option.textContent = namespace; return option;
  }));
  state.namespace = namespaces.includes(preferredNamespace) ? preferredNamespace : namespaces[0] || null;
  $('namespace').value = state.namespace || '';
  $('namespace').disabled = !state.namespace;
  $('scope').textContent = state.namespace ? `Target: ${state.bucket.label} / ${state.namespace}` : `Create a namespace in ${state.bucket.label} to begin.`;
  renderAdminProvisioning();
  await loadTables();
}

async function createTableBucket() {
  const name = $('new-bucket').value.trim(); const button = $('create-bucket'); const status = $('create-bucket-status');
  button.disabled = true; button.classList.add('is-busy'); status.className = 'operation-status'; status.textContent = `Creating ${name}…`;
  try {
    const response = await apiFetch('/api/buckets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    const result = await response.json();
    if (!response.ok) { status.className = 'operation-status failed'; status.textContent = result.detail || 'Bucket creation failed.'; return; }
    $('new-bucket').value = '';
    await loadBuckets(result);
    $('admin-provisioning').open = true;
    status.className = 'operation-status complete'; status.textContent = `Created ${result.label}. Create a namespace in it next.`;
    $('new-namespace').focus();
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = `Bucket creation failed: ${error.message}`;
  } finally {
    button.classList.remove('is-busy'); renderAdminProvisioning();
  }
}

async function createSelectedNamespace() {
  const namespace = $('new-namespace').value.trim(); const bucket = state.bucket; const button = $('create-namespace'); const status = $('create-namespace-status');
  button.disabled = true; button.classList.add('is-busy'); status.className = 'operation-status'; status.textContent = `Creating ${namespace}…`;
  try {
    const response = await apiFetch('/api/namespaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table_bucket_arn: bucket.table_bucket_arn, namespace }) });
    const result = await response.json();
    if (!response.ok) { status.className = 'operation-status failed'; status.textContent = result.detail || 'Namespace creation failed.'; return; }
    $('new-namespace').value = '';
    await loadNamespaces(result.namespace);
    $('admin-provisioning').open = true;
    status.className = 'operation-status complete'; status.textContent = `Created namespace ${result.namespace}.`;
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = `Namespace creation failed: ${error.message}`;
  } finally {
    button.classList.remove('is-busy'); renderAdminProvisioning();
  }
}

async function loadTables() {
  if (!state.bucket || !state.namespace) return;
  const response = await apiFetch(`/api/tables?${bucketQuery()}`); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load tables');
  $('scope').textContent = `Target: ${state.bucket.label} / ${data.namespace}`;
  $('tables').replaceChildren(...data.tables.map(table => {
    const card = document.createElement('article'); card.className = 'table'; card.dataset.table = table.name;
    const select = document.createElement('button'); select.className = 'table-select'; select.type = 'button';
    select.innerHTML = `<strong>${table.name}</strong><small>Created: ${table.created_at || 'Unavailable'}</small><small>Modified: ${table.modified_at || 'Unavailable'}</small><small>Rows: ${table.row_count?.toLocaleString() ?? 'Unavailable'}</small>${table.uploader_managed ? '' : '<small class="browse-only">Browse only: no uploader schema/recovery contract.</small>'}`;
    select.onclick = () => { clearPreflight(); state.table = table.name; state.tableManaged = Boolean(table.uploader_managed); state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; selectTable(); valid(); loadHistory(); };
    card.append(select);
    if (state.isAdmin && table.uploader_managed) {
      const remove = document.createElement('button'); remove.className = 'danger'; remove.type = 'button'; remove.textContent = 'Delete table';
      remove.onclick = () => deleteTable(table.name); card.append(remove);
    }
    return card;
  }));
  if (!data.tables.some(table => table.name === state.table)) { state.table = null; state.tableManaged = false; }
  if (data.tables.length === 0) {
    state.mode = 'create'; state.tableManaged = true;
    $('create').checked = true; $('new-table-wrap').hidden = false;
  }
  selectTable(); valid(); await loadHistory();
}

async function deleteTable(table) {
  if (!confirm(`Delete table “${table}”? This permanently removes the table and its data.`)) return;
  $('activity').textContent = `Deleting ${table}…`;
  const response = await apiFetch('/api/tables', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table, table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.namespace }) });
  const result = await response.json(); if (!response.ok) return alert(result.detail || 'Table deletion failed');
  if (state.table === table) { state.table = null; state.tableManaged = false; }
  $('activity').textContent = `Deleted ${table}.`; await loadTables();
}

function selectTable() { document.querySelectorAll('.table').forEach(x => x.classList.toggle('selected', x.dataset.table === state.table && state.mode === 'append')); }
function valid() {
  const table = selectedTable(); const hasFiles = $('files').files.length > 0;
  const tableIsValid = typeof table === 'string' && /^[a-z][a-z0-9_]{0,254}$/.test(table);
  const tagIsValid = userTag().length > 0;
  const requirements = [];
  if (!state.bucket) requirements.push('choose or create a bucket');
  if (!state.namespace) requirements.push('choose or create a namespace');
  if (!table) requirements.push(state.mode === 'create' ? 'enter a new table name' : 'select a table');
  else if (!tableIsValid) requirements.push('enter a valid table name');
  if (state.mode === 'append' && table && !state.tableManaged) requirements.push('choose an uploader-managed table or create a new table');
  if (!tagIsValid) requirements.push('enter a user tag');
  if (!hasFiles) requirements.push('select one or more files');
  const ready = requirements.length === 0;
  const hasReviewedSession = Boolean(state.sessionId && state.review);
  $('preflight').disabled = !ready || hasReviewedSession;
  $('preflight').title = ready ? 'Review the selected upload.' : `Still required: ${requirements.join('; ')}.`;
  $('review-requirements').textContent = hasReviewedSession ? 'This submitted upload is already under review. Change a file, destination, user tag, or processing choice to start a new session.' : ready ? 'All required fields are complete. The upload is ready for review.' : `To enable Review upload: ${requirements.join('; ')}.`;
  if (!state.bucket) $('destination-help').textContent = 'No S3 Tables bucket is assigned to this user.';
  else if (!table) $('destination-help').textContent = state.mode === 'create' ? 'Enter a new table name to continue.' : 'Select one existing table, or check “Create a new table from this upload”.';
  else if (state.mode === 'append' && !state.tableManaged) $('destination-help').textContent = 'This existing table is browse-only because it has no uploader schema and recovery contract.';
  else if (!tableIsValid) $('destination-help').textContent = 'Table names must start with a lowercase letter and use only a-z, 0-9, and underscores (_).';
  else if (!tagIsValid) $('destination-help').textContent = 'Enter a user tag to identify this upload.';
  else if (!hasFiles) $('destination-help').textContent = `Destination: ${table}. Select one or more supported files to continue.`;
  else $('destination-help').textContent = state.mode === 'create' && table !== $('new-table').value.trim() ? `New table will be created as: ${table}` : `Destination: ${table}. Ready to review upload.`;
}
function formData() {
  const data = new FormData(); data.append('mode', state.mode); data.append('table', selectedTable());
  data.append('table_bucket_arn', state.bucket.table_bucket_arn); data.append('namespace', state.namespace);
  [...$('files').files].forEach(file => data.append('files', file)); return data;
}

function selectedDeduplicationMode() {
  return document.querySelector('input[name="deduplication-mode"]:checked')?.value || 'keyed';
}

function selectedManualEncryptionColumns() {
  return [...document.querySelectorAll('[data-manual-encryption-column]:checked')]
    .map(control => control.dataset.manualEncryptionColumn);
}

function responseDetail(result, fallback) {
  const detail = result?.detail ?? result?.error?.message ?? result?.message;
  return typeof detail === 'string' ? detail : detail?.message || fallback;
}

function renderSessionProgress(session) {
  const started = session.phase_started_at ? Math.max(0, Math.floor((Date.now() - new Date(session.phase_started_at).getTime()) / 1000)) : null;
  const elapsed = Number.isFinite(started) ? ` (${started}s in ${session.phase || 'current phase'})` : '';
  const request = state.lastRequestId ? ` Request ID: ${state.lastRequestId}.` : '';
  const message = `${session.progress_message || 'Processing upload session…'}${elapsed}${request}`;
  $('activity').textContent = message;
  if (['STARTING_GLUE', 'GLUE_RUNNING'].includes(session.phase)) {
    $('outcome').hidden = false;
    $('status').className = 'running';
    $('status').textContent = message;
    $('upload-status').className = 'operation-status';
    $('upload-status').textContent = message;
  } else if (['PROFILING', 'KEY_ANALYSING'].includes(session.phase)) {
    $('review-status').className = 'operation-status';
    $('review-status').textContent = message;
  }
}

function applySessionState(session) {
  state.sessionId = session.session_id;
  sessionStorage.setItem(SESSION_STORAGE_KEY, session.session_id);
  // Key analysis completion redraws preflight. Preserve the choices covered
  // by the analysis so the redraw does not leave the acknowledgement visible
  // while silently clearing the selected composite key.
  const restoredDeduplicationColumns = session.key_impact?.deduplication_columns || selectedDeduplicationColumns();
  const restoredTypeOverrides = session.key_impact?.type_overrides || selectedTypeOverrides();
  if (session.preflight) {
    state.review = session.preflight;
    $('review').hidden = false;
    $('review').open = true;
    renderPreflight(session.preflight, { restoredDeduplicationColumns, restoredTypeOverrides });
    $('upload-actions').hidden = !session.preflight.accepted;
  }
  if (session.key_impact) {
    const impact = session.key_impact;
    state.keyAnalysis = {
      token: impact.acknowledgement_token,
      columns: impact.deduplication_columns || [],
      typeSignature: JSON.stringify(restoredTypeOverrides),
    };
    state.keyAnalysisAcknowledged = false;
    renderKeyAnalysis(impact);
  }
  if (session.ingestion?.job_run_id) {
    $('outcome').hidden = false;
    $('status').textContent = session.progress_message;
    $('status').className = 'running';
    $('status-body').textContent = JSON.stringify(session.ingestion, null, 2);
    poll(session.ingestion.job_run_id, session.ingestion.qc_uri, 'ingestion');
  }
  updateCreateUploadEligibility();
}

function sessionFailure(session) {
  const reason = responseDetail(session.error, session.progress_message || 'The upload session failed.');
  $('activity').textContent = 'Upload session failed.';
  $('review-status').className = 'operation-status failed';
  $('review-status').textContent = reason;
  $('upload-status').className = 'operation-status failed';
  $('upload-status').textContent = reason;
  $('outcome').hidden = false;
  $('status').className = 'failed';
  $('status').textContent = 'Upload was not started.';
  $('status-body').textContent = JSON.stringify(session.error || session, null, 2);
}

async function pollUploadSession(sessionId, { until = [] } = {}) {
  clearSessionPoll();
  try {
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(sessionId)}`);
    const session = await response.json();
    if (!response.ok) {
      state.sessionId = null; sessionStorage.removeItem(SESSION_STORAGE_KEY);
      throw new Error(responseDetail(session, 'The upload session is no longer available.'));
    }
    if (state.sessionId && state.sessionId !== session.session_id) return session;
    state.sessionId = session.session_id;
    renderSessionProgress(session);
    if (session.phase === 'FAILED') { sessionFailure(session); return session; }
    applySessionState(session);
    if (until.includes(session.phase) || session.phase === 'FAILED' || (!until.length && sessionTerminalPhases.includes(session.phase))) return session;
    state.sessionPollTimer = setTimeout(() => { pollUploadSession(sessionId, { until }).catch(error => {
      $('review-status').className = 'operation-status failed'; $('review-status').textContent = error.message;
    }); }, 1000);
    return session;
  } catch (error) {
    clearSessionPoll();
    throw error;
  }
}

async function resumeUploadSession() {
  const sessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!sessionId) return;
  state.sessionId = sessionId;
  $('activity').textContent = 'Reconnecting to the previous upload session…';
  try {
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(sessionId)}`);
    const session = await response.json();
    if (!response.ok) throw new Error(responseDetail(session, 'The upload session is no longer available.'));
    const bucket = { table_bucket_arn: session.table_bucket_arn, label: session.table_bucket_arn.split('/').at(-1) || session.table_bucket_arn };
    await loadBuckets(bucket, { preserveSession: true, preferredNamespace: session.namespace });
    state.mode = session.mode;
    state.table = session.mode === 'append' ? session.table : null;
    state.tableManaged = true;
    $('create').checked = session.mode === 'create';
    $('new-table-wrap').hidden = session.mode !== 'create';
    if (session.mode === 'create') $('new-table').value = session.table;
    selectTable(); valid();
    applySessionState(session);
    if (!sessionTerminalPhases.includes(session.phase)) await pollUploadSession(sessionId);
    if (session?.preflight) {
      $('review-status').className = 'operation-status complete';
      $('review-status').textContent = 'Reconnected to the previously submitted files. You may continue this upload without selecting the files again.';
    }
  } catch (error) {
    sessionStorage.removeItem(SESSION_STORAGE_KEY); state.sessionId = null;
    $('activity').textContent = `Previous upload session is unavailable: ${error.message}`;
  }
}

function selectedTypeOverrides() {
  return Object.fromEntries(
    [...document.querySelectorAll('[data-type-override]')]
      .filter(control => !control.disabled)
      .map(control => [control.dataset.typeOverride, control.value]),
  );
}

function updateTypeChoiceImpact(control) {
  const holder = document.getElementById(`type-impact-${control.dataset.typeOverride}`);
  if (!holder) return;
  let impacts = {};
  try { impacts = JSON.parse(control.dataset.lossyTargetTypes || '{}'); } catch { impacts = {}; }
  const impact = impacts[control.value];
  if (!impact) { holder.textContent = ''; holder.hidden = true; return; }
  holder.hidden = false;
  holder.textContent = `Choosing ${control.value} will convert ${Number(impact.invalid_value_count).toLocaleString()} non-compliant populated value(s) to NULL. Choose STRING to preserve every value.`;
}

function selectedDeduplicationColumns() {
  return [...document.querySelectorAll('[data-deduplication-column]:checked')]
    .map(control => control.dataset.deduplicationColumn);
}

function updateDeduplicationSelectionControls() {
  const controls = [...document.querySelectorAll('[data-deduplication-column]')].filter(control => !control.disabled);
  const selectedCount = controls.filter(control => control.checked).length;
  const allSelected = controls.length > 0 && selectedCount === controls.length;
  const selectAll = $('select-all-deduplication');
  if (selectAll) {
    selectAll.disabled = controls.length === 0;
    selectAll.textContent = allSelected ? 'Clear all columns' : 'Select all columns';
    selectAll.setAttribute('aria-pressed', String(allSelected));
  }
  const count = $('deduplication-selection-count');
  if (count) count.textContent = `${selectedCount} of ${controls.length} eligible columns selected.`;
  const analyse = $('analyse-key');
  if (analyse) analyse.disabled = selectedCount === 0;
}

function deduplicationSelectionChanged() {
  invalidateKeyAnalysis();
  updateDeduplicationSelectionControls();
}

function toggleAllDeduplicationColumns() {
  const controls = [...document.querySelectorAll('[data-deduplication-column]')].filter(control => !control.disabled);
  const selectAll = !controls.every(control => control.checked);
  controls.forEach(control => { control.checked = selectAll; });
  deduplicationSelectionChanged();
}

function updateCreateUploadEligibility() {
  if (!state.review) return;
  const mode = selectedDeduplicationMode();
  if (mode === 'none') {
    $('upload').disabled = !state.review.accepted;
    return;
  }
  if (state.review.mode !== 'create') {
    const configured = state.review.deduplication_columns || [];
    if (configured.length) {
      $('upload').disabled = !state.review.accepted;
      return;
    }
    // A legacy/no-dedup table may adopt its first key on this append. It uses
    // the same analysis and acknowledgement safeguards as a first upload.
    const selected = selectedDeduplicationColumns();
    const notice = $('deduplication-selection-notice');
    const analysisIsCurrent = state.keyAnalysis && state.keyAnalysis.columns.join('|') === selected.join('|') && state.keyAnalysis.typeSignature === JSON.stringify(selectedTypeOverrides());
    $('upload').disabled = !state.review.accepted || selected.length === 0 || !analysisIsCurrent || !state.keyAnalysisAcknowledged;
    if (notice) notice.textContent = selected.length
      ? analysisIsCurrent
        ? state.keyAnalysisAcknowledged
          ? `Key analysis acknowledged for: ${selected.join(' + ')}. Upload is enabled.`
          : 'Acknowledge the key-impact analysis before uploading.'
        : `Selected key: ${selected.join(' + ')}. Run key-impact analysis before uploading.`
      : 'Choose at least one de-duplication column before uploading.';
    return;
  }
  const selected = selectedDeduplicationColumns();
  const notice = $('deduplication-selection-notice');
  const analysisIsCurrent = state.keyAnalysis && state.keyAnalysis.columns.join('|') === selected.join('|') && state.keyAnalysis.typeSignature === JSON.stringify(selectedTypeOverrides());
  $('upload').disabled = !state.review.accepted || selected.length === 0 || !analysisIsCurrent || !state.keyAnalysisAcknowledged;
  if (notice) notice.textContent = selected.length
    ? analysisIsCurrent
      ? state.keyAnalysisAcknowledged
        ? `Key analysis acknowledged for: ${selected.join(' + ')}. Upload is enabled.`
        : 'Acknowledge the key-impact analysis before uploading.'
      : `Selected key: ${selected.join(' + ')}. Run key-impact analysis before uploading.`
    : 'Choose at least one de-duplication column before uploading.';
}

function invalidateKeyAnalysis() {
  state.keyAnalysis = null; state.keyAnalysisAcknowledged = false;
  const holder = $('key-analysis-result'); if (holder) holder.replaceChildren();
  updateCreateUploadEligibility();
}

function renderKeyAnalysis(result) {
  const holder = $('key-analysis-result'); if (!holder) return;
  const m = result.metrics;
  const key = (result.deduplication_columns || []).map(escapeHtml).join(' + ');
  holder.innerHTML = `<section class="key-analysis-result"><h4>Composite-key impact</h4><p><strong>Current composite key:</strong> <code>${key}</code></p><dl class="preflight-summary">
    <div><dt>Incoming rows</dt><dd>${Number(m.incoming_rows).toLocaleString()}</dd></div>
    <div><dt>Unique keys</dt><dd>${Number(m.unique_composite_keys).toLocaleString()}</dd></div>
    <div><dt>Exact duplicate rows</dt><dd>${Number(m.exact_duplicate_rows).toLocaleString()}</dd></div>
    <div><dt>Conflicting key groups</dt><dd>${Number(m.conflicting_key_groups).toLocaleString()}</dd></div>
    <div><dt>Rows in conflicting groups</dt><dd>${Number(m.rows_in_conflicting_key_groups).toLocaleString()}</dd></div>
    <div><dt>Expected retained rows</dt><dd>${Number(m.expected_retained_rows).toLocaleString()}</dd></div>
    <div><dt>Expected skipped rows</dt><dd>${Number(m.expected_skipped_rows).toLocaleString()}</dd></div>
  </dl><p class="hint">This is a fast, local-only review of raw uploaded values: no encryption, masking, S3 staging, or Glue work occurs. Exact duplicates retain one row. Every row in a same-key, different-row conflict group is skipped. You may revise the selected columns and run this analysis again.</p>
  <label class="acknowledgement"><input id="acknowledge-key-analysis" type="checkbox"> I acknowledge this key-impact result and want to enforce this immutable de-duplication contract.</label></section>`;
  $('acknowledge-key-analysis').onchange = () => { state.keyAnalysisAcknowledged = $('acknowledge-key-analysis').checked; updateCreateUploadEligibility(); };
}

async function analyseSelectedKey() {
  const selected = selectedDeduplicationColumns();
  if (!selected.length || !state.sessionId) return;
  const button = $('analyse-key');
  const status = $('key-analysis-status');
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Analysing selected key…';
  status.className = 'operation-status';
  status.textContent = 'Analysing all incoming rows locally. No sanitization, S3 staging, or Glue work is running.';
  $('activity').textContent = 'Analysing raw local upload rows for the selected composite key — no sanitization, S3, or Glue work…';
  try {
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(state.sessionId)}/key-impact`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_overrides: selectedTypeOverrides(), deduplication_columns: selected }),
    }); const result = await response.json();
    if (!response.ok) {
      const reason = responseDetail(result, 'Key-impact analysis failed.');
      $('activity').textContent = 'Key-impact analysis failed.';
      status.className = 'operation-status failed'; status.textContent = `Key-impact analysis failed: ${reason}`;
      return;
    }
    status.textContent = 'Composite-key impact analysis is in progress…';
    const session = await pollUploadSession(state.sessionId, { until: ['READY_FOR_ACKNOWLEDGEMENT'] });
    if (session.phase === 'READY_FOR_ACKNOWLEDGEMENT') {
      $('activity').textContent = 'Key-impact analysis is ready. Review it, revise the key if needed, or acknowledge it to enable upload.';
      status.className = 'operation-status complete'; status.textContent = 'Key-impact analysis completed. Review the results below.';
    }
  } catch (error) {
    $('activity').textContent = 'Key-impact analysis failed.';
    status.className = 'operation-status failed'; status.textContent = `Key-impact analysis failed: ${error.message || 'network request failed'}`;
  } finally {
    button.classList.remove('is-busy'); button.textContent = 'Analyse selected key impact';
    button.disabled = selectedDeduplicationColumns().length === 0;
  }
}

function renderPreflight(result, { restoredDeduplicationColumns = [], restoredTypeOverrides = {} } = {}) {
  const holder = $('review-body'); holder.replaceChildren();
  const decision = document.createElement('p');
  decision.className = result.accepted ? 'preflight-pass' : 'preflight-reject';
  decision.textContent = result.accepted
    ? 'Accepted: the upload meets the enforced schema and sanitization rules.'
    : 'Rejected: the upload does not meet the enforced schema and sanitization rules.';
  holder.append(decision);
  const sanitization = result.sanitization_review;
  if (sanitization) {
    const section = document.createElement('section'); section.className = 'sanitization-review';
    const automatic = sanitization.automatic_encrypted_columns || [];
    const candidates = sanitization.manual_encryption_candidates || [];
    const candidateRows = candidates.length ? `<div class="manual-sanitization-columns">${candidates.map(choice => {
      const samples = choice.samples_masked
        ? 'Examples are masked because this is an automatically protected healthcare field.'
        : choice.sample_values?.length ? `Examples: ${choice.sample_values.map(escapeHtml).join(', ')}` : 'No non-empty examples are available.';
      return `<label class="manual-sanitization-choice"><input type="checkbox" data-manual-encryption-column="${escapeHtml(choice.column)}"><span><strong>${escapeHtml(choice.column)}</strong><small>${samples}</small></span></label>`;
    }).join('')}</div>` : '<p class="hint">No additional columns are available for manual encryption.</p>';
    section.innerHTML = `<h3>Sanitization review</h3><p>Automatic healthcare detection is already enforced. You may additionally encrypt a column before staging it in AWS. Singapore NRIC-shaped values are automatically detected and encrypted even when the column name is not recognised.</p><p><strong>Automatically protected columns:</strong> ${automatic.length ? automatic.map(escapeHtml).join(', ') : 'None detected by column name.'}</p>${candidateRows}`;
    holder.append(section);
    section.querySelectorAll('[data-manual-encryption-column]').forEach(control => control.addEventListener('change', invalidateKeyAnalysis));
  }
  if (result.mode === 'create' && result.type_selections?.length) {
    const choices = document.createElement('section'); choices.className = 'type-selections';
    const rows = result.type_selections.map(choice => {
      const selectedType = restoredTypeOverrides[choice.column] || choice.suggested_target_type;
      const options = choice.allowed_target_types.map(type =>
        `<option value="${escapeHtml(type)}" ${type === selectedType ? 'selected' : ''}>${escapeHtml(type)}</option>`,
      ).join('');
      const samples = choice.samples_masked
        ? '<small class="sample-values">Examples are masked because this is a healthcare-sanitized column.</small>'
        : choice.sample_values?.length
          ? `<small class="sample-values">Random non-empty examples: ${choice.sample_values.map(escapeHtml).join(', ')}</small>`
          : '<small class="sample-values">No non-empty examples are available.</small>';
      const impacts = escapeHtml(JSON.stringify(choice.lossy_target_types || {}));
      return `<label class="type-selection"><span><strong>${escapeHtml(choice.column)}</strong><small>Detected: ${escapeHtml(choice.source_type)}. This choice becomes the initial table contract.</small>${samples}<small id="type-impact-${escapeHtml(choice.column)}" class="type-impact" hidden></small></span><select data-type-override="${escapeHtml(choice.column)}" data-lossy-target-types="${impacts}" ${choice.locked ? 'disabled' : ''}>${options}</select></label>`;
    }).join('');
    choices.innerHTML = `<h3>Choose ambiguous initial column types</h3><p>Automatic DATE, TIMESTAMP, BIGINT, and DOUBLE rules have already been applied where the full first file is unambiguous. Choose a type only for these remaining ambiguous columns. Up to five random, non-empty examples are shown only for this review and are not stored. Healthcare-sanitized fields remain locked as <code>STRING</code>.</p>${rows}`;
    holder.append(choices);
    choices.querySelectorAll('[data-type-override]').forEach(control => {
      updateTypeChoiceImpact(control);
      control.addEventListener('change', () => { updateTypeChoiceImpact(control); invalidateKeyAnalysis(); });
    });
  }
  const needsFirstKey = result.mode === 'create' || !(result.deduplication_columns || []).length;
  if (selectedDeduplicationMode() === 'keyed' && needsFirstKey && result.deduplication_candidates?.length) {
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
      const quality = choice.samples_masked ? '' : `<small>Non-empty: ${Number(choice.non_null_count || 0).toLocaleString()}; distinct cardinality is calculated only when key-impact analysis runs.</small>`;
      const checked = restoredDeduplicationColumns.includes(choice.column) ? 'checked' : '';
      return `<label class="deduplication-candidate ${unavailable ? 'ineligible' : ''}"><input type="checkbox" data-deduplication-column="${escapeHtml(choice.column)}" ${checked} ${unavailable ? 'disabled' : ''}><span><strong>${escapeHtml(choice.column)}</strong><small>Stored type: ${escapeHtml(choice.target_type)}; detected: ${escapeHtml(choice.source_type)}.</small>${quality}${examples}${reason}</span></label>`;
    }).join('');
    const activationNote = result.mode === 'append'
      ? 'This older table has no composite key yet. Your first acknowledged key will be saved prospectively for this and later keyed appends; existing table rows are not rewritten.'
      : 'This selection becomes the table’s immutable de-duplication contract.';
    section.innerHTML = `<h3>Choose de-duplication columns</h3><p>Select one stable identifier, or multiple fields for a composite key. CSN, case, HRN, MRN, and other encrypted identifiers may be selected; their examples remain masked. ${activationNote} Before upload, analyse the full incoming dataset to see the duplicate/conflict impact. Per-column non-empty and distinct counts help assess a single-column key.</p><p class="deduplication-notice" id="deduplication-selection-notice">Choose at least one de-duplication column before uploading.</p><div class="deduplication-actions"><button type="button" id="select-all-deduplication" class="secondary" aria-pressed="false">Select all columns</button><span id="deduplication-selection-count" class="hint"></span></div><div class="deduplication-candidates">${rows}</div><button type="button" id="analyse-key" class="key-analysis-action" disabled>Analyse selected key impact</button><p id="key-analysis-status" class="operation-status" aria-live="polite"></p><div id="key-analysis-result"></div>`;
    holder.append(section);
    section.querySelectorAll('[data-deduplication-column]').forEach(control => control.addEventListener('change', deduplicationSelectionChanged));
    $('select-all-deduplication').onclick = toggleAllDeduplicationColumns;
    $('analyse-key').onclick = analyseSelectedKey;
    updateDeduplicationSelectionControls();
  }
  if (selectedDeduplicationMode() === 'keyed' && result.mode === 'append' && result.deduplication_columns?.length) {
    const section = document.createElement('section'); section.className = 'deduplication-selection';
    section.innerHTML = `<h3>Existing de-duplication contract</h3><p><strong>Current composite key:</strong> <code>${result.deduplication_columns.map(escapeHtml).join(' + ')}</code></p><p class="hint">This table’s composite key is immutable. New rows that share a key with existing rows are handled by the configured de-duplication policy.</p>`;
    holder.append(section);
  }
  if (selectedDeduplicationMode() === 'none') {
    const section = document.createElement('section'); section.className = 'deduplication-selection';
    section.innerHTML = '<h3>Clean-data append selected</h3><p>No de-duplication analysis or target-table comparison will run. Every validated incoming row is appended.</p>';
    holder.append(section);
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

function displayHistory(items, latestRollbackUploadId) {
  const holder = $('history-body'); holder.replaceChildren();
  if (!items.length) { holder.textContent = 'No uploader-managed history is available for this table yet.'; return; }
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
      const canRollback = state.canRollbackUploads
        && (state.isAdmin || item.uploaded_by === state.userId)
        && item.upload_id === latestRollbackUploadId;
      rollback.disabled = !canRollback;
      rollback.title = canRollback ? 'Restore the table to the snapshot before this latest upload.' : 'Only your latest successful upload, when it is also the table\'s latest update, can be rolled back.';
      if (canRollback) rollback.onclick = () => rollbackUpload(item);
      row.append(rollback);
    }
    holder.append(row);
  }
}

async function loadHistory() {
  if (!state.canViewHistory || !state.bucket || !state.namespace || !state.table || !state.tableManaged || state.mode !== 'append') { $('history-body').textContent = state.canViewHistory ? state.table && !state.tableManaged ? 'This table is browse-only and has no uploader-managed history.' : 'Select an uploader-managed table to view its upload history.' : ''; return; }
  const query = new URLSearchParams({ ...Object.fromEntries(bucketQuery()), table: state.table });
  const response = await apiFetch(`/api/upload-history?${query}`); const result = await response.json();
  if (!response.ok) { $('history-body').textContent = result.detail || 'Unable to load upload history.'; return; }
  displayHistory(result.history || [], result.latest_rollback_upload_id);
}

async function rollbackUpload(item) {
  const warning = `Rolling back will restore “${state.table}” to its state immediately before upload ${item.upload_id}. This removes that upload’s data. Continue?`;
  if (!confirm(warning)) return;
  $('outcome').hidden = false; $('activity').textContent = `Starting rollback for ${item.upload_id}…`; $('status').textContent = 'Rollback is starting…'; $('status').className = 'running';
  const response = await apiFetch('/api/rollbacks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table: state.table, table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.namespace, upload_id: item.upload_id, confirm: true }) });
  const result = await response.json();
  if (!response.ok) { $('status').textContent = 'Rollback was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2); return; }
  $('status-body').textContent = JSON.stringify(result, null, 2); poll(result.job_run_id, result.qc_uri, 'rollback');
}

$('refresh').onclick = loadNamespaces;
$('refresh-history').onclick = loadHistory;
$('new-bucket').oninput = renderAdminProvisioning;
$('new-namespace').oninput = renderAdminProvisioning;
$('create-bucket').onclick = createTableBucket;
$('create-namespace').onclick = createSelectedNamespace;
$('upload-skill-bundle').onclick = uploadSkillBundle;
$('skill-bundle-files').onchange = updateSkillControls;
$('skill-confirm-replace').onchange = updateSkillControls;
$('emulated-user').onchange = async () => {
  clearSkillBundle();
  state.emulatedUserId = $('emulated-user').value || null;
  $('activity').textContent = `Testing backend authorization as ${state.emulatedUserId || 'no user'}…`;
  await loadBuckets();
};
$('bucket').onchange = async () => { clearPreflight(); state.bucket = JSON.parse($('bucket').value); clearSkillBundle(); state.namespace = null; state.table = null; state.tableManaged = false; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; await loadNamespaces(); };
$('namespace').onchange = async () => { clearPreflight(); state.namespace = $('namespace').value || null; state.table = null; state.tableManaged = false; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; await loadTables(); };
$('create').onchange = () => { clearPreflight(); state.mode = $('create').checked ? 'create' : 'append'; if (state.mode === 'create') { state.table = null; state.tableManaged = true; } $('new-table-wrap').hidden = state.mode !== 'create'; selectTable(); valid(); loadHistory(); };
$('new-table').oninput = () => { clearPreflight(); valid(); };
$('files').onchange = () => { clearPreflight(); valid(); };
$('reporting-month').oninput = () => { clearPreflight(); valid(); };
$('deduplication-mode').onchange = () => { state.deduplicationMode = selectedDeduplicationMode(); clearPreflight(); valid(); };
$('preflight').onclick = async () => {
  const button = $('preflight'); const status = $('review-status');
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Reviewing upload…';
  status.className = 'operation-status'; status.textContent = 'Analysing file structure, column names, types, and sanitization requirements…';
  $('activity').textContent = 'Scanning selected file schemas…';
  try {
    const response = await apiFetch('/api/v2/upload-sessions', { method: 'POST', body: formData() });
    const result = await response.json();
    if (!response.ok) {
      const reason = responseDetail(result, 'Data structure analysis could not start.');
      $('activity').textContent = 'Preflight failed.';
      status.className = 'operation-status failed'; status.textContent = `Upload review failed: ${reason}`;
      return;
    }
    state.sessionId = result.session_id;
    sessionStorage.setItem(SESSION_STORAGE_KEY, result.session_id);
    status.textContent = 'Files were received once into a private local session. Analysing structure and the proposed schema…';
    const session = await pollUploadSession(result.session_id, { until: ['READY_FOR_REVIEW'] });
    if (session.phase === 'READY_FOR_REVIEW') {
      const preview = session.preflight;
      $('activity').textContent = preview.accepted ? 'Data structure analysis is complete. Review the result below.' : 'Data structure analysis found validation issues.';
      status.className = preview.accepted ? 'operation-status complete' : 'operation-status failed';
      status.textContent = preview.accepted ? 'Upload review completed. Review the schema and processing choices below.' : 'Upload review completed with validation issues. See the rejection reasons below.';
    }
  } catch (error) {
    $('activity').textContent = 'Preflight failed.';
    status.className = 'operation-status failed'; status.textContent = `Upload review failed: ${error.message || 'network request failed'}`;
  } finally {
    button.classList.remove('is-busy'); button.textContent = 'Review upload'; valid();
  }
};
$('upload').onclick = async () => {
  const button = $('upload'); const status = $('upload-status'); let started = false;
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Starting upload…';
  status.className = 'operation-status'; status.textContent = 'Preparing the sanitized upload, recovery point, and ETL job…';
  $('outcome').hidden = false; $('activity').textContent = 'Preparing the session files for sanitization and AWS Glue…'; $('status').textContent = 'Upload preparation is in process…'; $('status').className = 'running';
  try {
    if (!state.sessionId) throw new Error('Review the selected upload before starting ETL.');
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(state.sessionId)}/ingestions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: crypto.randomUUID(), reporting_month: userTag(), type_overrides: selectedTypeOverrides(), deduplication_mode: selectedDeduplicationMode(), deduplication_columns: selectedDeduplicationMode() === 'keyed' ? selectedDeduplicationColumns() : [], key_analysis_token: state.keyAnalysis?.token || null, manual_encryption_columns: selectedManualEncryptionColumns() }),
    }); const result = await response.json();
    if (!response.ok) {
      const reason = responseDetail(result, 'Upload could not be started.');
      $('status').textContent = 'Upload was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2);
      status.className = 'operation-status failed'; status.textContent = `Upload could not start: ${reason}`;
      return;
    }
    started = true;
    button.textContent = 'Preparing ETL…';
    status.textContent = 'The session is preparing sanitized Parquet, a recovery point, and AWS Glue.';
    $('status-body').textContent = JSON.stringify(result, null, 2);
    const session = await pollUploadSession(state.sessionId, { until: ['GLUE_RUNNING'] });
    if (session.ingestion?.job_run_id) {
      button.textContent = 'ETL in progress…';
      status.textContent = 'AWS Glue is queued or running the Iceberg mutation; progress is shown below.';
    }
  } catch (error) {
    $('status').textContent = 'Upload was not started.'; $('status').className = 'failed';
    status.className = 'operation-status failed'; status.textContent = `Upload could not start: ${error.message || 'network request failed'}`;
  } finally {
    button.classList.remove('is-busy');
    if (!started) { button.textContent = 'Upload and run ETL'; updateCreateUploadEligibility(); }
  }
};
async function poll(id, qcUri, operation) {
  const response = await apiFetch(`/api/ingestions/${id}?operation=${operation}`); const result = await response.json();
  $('activity').textContent = result.message; $('status').textContent = result.message; $('status').className = result.state === 'SUCCEEDED' ? 'succeeded' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'failed' : 'running';
  if (operation === 'ingestion') {
    const uploadStatus = $('upload-status');
    uploadStatus.className = result.state === 'SUCCEEDED' ? 'operation-status complete' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'operation-status failed' : 'operation-status';
    uploadStatus.textContent = result.message;
  }
  $('status-body').textContent = JSON.stringify(result, null, 2);
  if (!terminalStates.includes(result.state)) setTimeout(() => poll(id, qcUri, operation), 5000);
  else { if (operation === 'ingestion') { $('upload').textContent = 'Upload and run ETL'; } const qc = await apiFetch(`/api/qc?uri=${encodeURIComponent(qcUri)}`).then(r => r.ok ? r.json() : null); if (qc) $('status-body').textContent = JSON.stringify({ job: result, qc }, null, 2); if (result.state === 'SUCCEEDED') { await loadTables(); await loadHistory(); } }
}

loadIdentityProfiles().then(loadBuckets).then(resumeUploadSession).catch(error => { $('scope').textContent = `Unable to load assigned S3 Tables buckets: ${error}`; });
