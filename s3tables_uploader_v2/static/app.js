const SESSION_STORAGE_KEY = 's3tables-uploader-v2-session-id';
const sessionTerminalPhases = ['READY_FOR_REVIEW', 'READY_FOR_ACKNOWLEDGEMENT', 'GLUE_RUNNING', 'SUCCEEDED', 'FAILED'];
const state = { bucket: null, namespace: null, table: null, tableManaged: false, tableDeduplicationColumns: [], mode: 'append', review: null, keyAnalysis: null, keyAnalysisAcknowledged: false, temporalPolicyAcknowledged: false, isAdmin: false, userId: null, canViewHistory: false, canRollbackUploads: false, emulatedUserId: null, identityProfiles: [], sessionId: null, sessionPollTimer: null, gluePollTimer: null, activeJobRunId: null, deduplicationMode: 'keyed', lastHttpRequestId: null, currentOperationId: null, sessionPhase: null, keyAnalysisPending: false, appliedKeyToken: null, sessionPollGeneration: 0, sessionPollResolve: null, workerLeaseId: null, workerLease: null };
const $ = (id) => document.getElementById(id);
const terminalStates = ['SUCCEEDED', 'FAILED', 'ERROR', 'TIMEOUT', 'STOPPED'];

// Safari versions used in some managed environments do not expose the newer
// replaceChildren() and Array.prototype.at() helpers. Keep the UI usable on
// those browsers without changing the API contract or requiring a polyfill.
function setChildren(element, ...children) {
  if (typeof element.replaceChildren === 'function') {
    element.replaceChildren(...children);
    return;
  }
  while (element.firstChild) element.removeChild(element.firstChild);
  children.forEach(child => element.appendChild(child));
}
function lastPathSegment(path) {
  const parts = String(path).split('/');
  return parts.length ? parts[parts.length - 1] : '';
}

// Some supported Safari versions expose getRandomValues() but not randomUUID().
// Keep the operation ID a standards-compliant, cryptographically random UUID so
// it remains safe to use as the ingestion request's idempotency key.
function createOperationRequestId() {
  const browserCrypto = typeof globalThis === 'undefined' ? null : globalThis.crypto;
  if (browserCrypto && typeof browserCrypto.randomUUID === 'function') return browserCrypto.randomUUID();
  if (!browserCrypto || typeof browserCrypto.getRandomValues !== 'function') {
    throw new Error('This browser cannot create a secure upload request ID. Please use a supported browser.');
  }
  const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function selectedTable() { return state.mode === 'create' ? $('new-table').value.trim().replace(/-/g, '_') : state.table; }
function bucketQuery() { return new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn, namespace: state.namespace }); }
function userTag() { return $('reporting-month').value.trim(); }
function escapeHtml(value) { const node = document.createElement('span'); node.textContent = String(value); return node.innerHTML; }
function formatTime(value) {
  return value ? new Date(value).toLocaleString('en-SG', {
    timeZone: 'Asia/Singapore', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true,
  }) : 'Unavailable';
}
function clearSessionPoll() {
  state.sessionPollGeneration += 1;
  if (state.sessionPollTimer) clearTimeout(state.sessionPollTimer);
  state.sessionPollTimer = null;
  if (state.sessionPollResolve) state.sessionPollResolve();
  state.sessionPollResolve = null;
}
function clearGluePoll() { if (state.gluePollTimer) { clearTimeout(state.gluePollTimer); state.gluePollTimer = null; } state.activeJobRunId = null; }
function clearPreflight({ forgetSession = true } = {}) { clearSessionPoll(); clearGluePoll(); state.sessionPhase = null; state.keyAnalysisPending = false; state.appliedKeyToken = null; state.review = null; state.keyAnalysis = null; state.keyAnalysisAcknowledged = false; state.temporalPolicyAcknowledged = false; state.currentOperationId = null; if (forgetSession) { state.sessionId = null; sessionStorage.removeItem(SESSION_STORAGE_KEY); } $('review').hidden = true; $('upload-actions').hidden = true; $('upload').disabled = true; $('upload-status').textContent = ''; $('upload-status').className = 'operation-status'; $('review-status').textContent = ''; $('review-status').className = 'operation-status'; $('retry-large').hidden = true; }
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
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
  state.lastHttpRequestId = response.headers.get('X-Request-ID') || state.lastHttpRequestId;
  return response;
}
function requestDiagnostic() { return state.lastHttpRequestId ? ` Support request ID: ${state.lastHttpRequestId}.` : ''; }
function updateSkillControls() {
  const hasBucket = Boolean(state.bucket);
  const hasFiles = $('skill-bundle-files').files.length > 0;
  $('skill-builder').hidden = !hasBucket;
  $('upload-skill-bundle').disabled = !hasBucket || !hasFiles;
}
function clearSkillBundle() {
  $('skill-bundle-files').value = '';
  $('skill-bundle-status').textContent = '';
  $('skill-bundle-status').className = 'operation-status';
  $('skill-location').textContent = 'Select an S3 Tables bucket to view its skill files.';
  setChildren($('skill-file-explorer'));
  updateSkillControls();
}
function formatFileSize(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}
function selectedSkillUploadPaths(files) {
  const paths = files.map(file => file.webkitRelativePath || file.name);
  const parts = paths.map(path => path.split('/').filter(Boolean));
  const first = parts[0]?.[0];
  // A directory picker prefixes every selected file with the local folder
  // name. That folder is not part of the managed S3 skill path.
  if (first && parts.every(path => path.length > 1 && path[0] === first)) {
    return parts.map(path => path.slice(1).join('/'));
  }
  return paths;
}
function buildSkillTree(files) {
  const root = { folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split('/'); let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [] });
      node = node.folders.get(part);
    }
    node.files.push({ ...file, name: lastPathSegment(file.path) });
  }
  return root;
}
function renderSkillTree(node, level = 0) {
  const holder = document.createElement('div'); holder.className = 'skill-tree-level';
  for (const [name, child] of [...node.folders.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const folder = document.createElement('details'); folder.className = 'skill-folder'; folder.open = level < 2;
    const summary = document.createElement('summary'); summary.textContent = `${name}/`; folder.append(summary);
    folder.append(renderSkillTree(child, level + 1)); holder.append(folder);
  }
  for (const file of [...node.files].sort((a, b) => a.name.localeCompare(b.name))) {
    const row = document.createElement('div'); row.className = 'skill-file-row';
    const details = document.createElement('div'); details.className = 'skill-file-details';
    const name = document.createElement('strong'); name.textContent = file.name; name.title = file.path;
    const meta = document.createElement('small'); meta.textContent = `${formatFileSize(file.size)} · ${formatTime(file.last_modified)}`;
    details.append(name, meta);
    const actions = document.createElement('div'); actions.className = 'skill-file-actions';
    const download = document.createElement('button'); download.type = 'button'; download.className = 'secondary'; download.textContent = 'Download'; download.onclick = () => downloadSkillFile(file.path); actions.append(download);
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'danger'; remove.textContent = 'Delete'; remove.onclick = () => deleteSkillFile(file.path); actions.append(remove);
    row.append(details, actions); holder.append(row);
  }
  return holder;
}
async function loadSkillFiles() {
  updateSkillControls();
  if (!state.bucket) return;
  const explorer = $('skill-file-explorer');
  $('skill-location').textContent = 'Loading files from the selected S3 skill prefix…';
  setChildren(explorer);
  try {
    const query = new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn });
    const response = await apiFetch(`/api/skills/files?${query}`); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Unable to list skill files.');
    $('skill-location').textContent = result.destination_uri;
    if (!result.files?.length) {
      explorer.textContent = 'No skill files are stored for this S3 Tables bucket yet.';
      explorer.className = 'skill-file-explorer empty';
      return;
    }
    explorer.className = 'skill-file-explorer'; explorer.append(renderSkillTree(buildSkillTree(result.files)));
  } catch (error) {
    $('skill-location').textContent = 'Unable to load the selected skill prefix.';
    explorer.className = 'skill-file-explorer failed'; explorer.textContent = error.message || 'Skill file listing failed.';
  }
}
async function downloadSkillFile(path) {
  if (!state.bucket) return;
  const status = $('skill-bundle-status'); status.className = 'operation-status'; status.textContent = `Downloading ${path}…`;
  try {
    const query = new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn, path });
    const response = await apiFetch(`/api/skills/files/download?${query}`);
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Skill file download failed.'); }
    const blob = await response.blob(); const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = lastPathSegment(path); link.click();
    URL.revokeObjectURL(url); status.className = 'operation-status complete'; status.textContent = `Downloaded ${path}.`;
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = error.message || 'Skill file download failed.';
  }
}
async function deleteSkillFile(path) {
  if (!state.bucket) return;
  const notice = path === 'SKILL.md' ? '\n\nDeleting SKILL.md will disable this bucket skill until a valid replacement is uploaded.' : '';
  if (!confirm(`Delete skill file “${path}”?${notice}`)) return;
  const status = $('skill-bundle-status'); status.className = 'operation-status'; status.textContent = `Deleting ${path}…`;
  try {
    const response = await apiFetch('/api/skills/files', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ table_bucket_arn: state.bucket.table_bucket_arn, path, confirm: true }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Skill file deletion failed.');
    status.className = 'operation-status complete'; status.textContent = `Deleted ${result.deleted_path}.`;
    await loadSkillFiles();
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = error.message || 'Skill file deletion failed.';
  }
}
async function uploadSkillBundle() {
  if (!state.bucket) return;
  const files = [...$('skill-bundle-files').files];
  const paths = selectedSkillUploadPaths(files);
  const button = $('upload-skill-bundle'); const status = $('skill-bundle-status');
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Uploading skill files…';
  status.className = 'operation-status'; status.textContent = 'Validating and uploading the selected skill files…';
  try {
    const form = new FormData();
    form.append('table_bucket_arn', state.bucket.table_bucket_arn);
    form.append('paths_json', JSON.stringify(paths));
    files.forEach(file => form.append('files', file, file.name));
    const response = await apiFetch('/api/skills/files', { method: 'POST', body: form });
    const result = await response.json();
    if (!response.ok) { status.className = 'operation-status failed'; status.textContent = result.detail || 'Skill bundle upload failed.'; return; }
    status.className = 'operation-status complete'; status.textContent = `Uploaded ${result.uploaded_paths.length} file(s): ${result.created_paths.length} new, ${result.overwritten_paths.length} overwritten. ${result.restart_reminder}`;
    await loadSkillFiles();
  } catch (error) {
    status.className = 'operation-status failed'; status.textContent = `Skill bundle upload failed: ${error.message || 'network request failed'}`;
  } finally {
    button.classList.remove('is-busy'); button.textContent = 'Upload skill files'; updateSkillControls();
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
  setChildren($('bucket')); setChildren($('namespace')); $('namespace').disabled = true; setChildren($('tables')); $('history').hidden = true;
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
  const response = await fetch('/api/dev/identity-profiles', { credentials: 'same-origin' }); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load local identity profiles');
  state.identityProfiles = data.profiles || [];
  setChildren($('emulated-user'), ...state.identityProfiles.map(profile => {
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
  setChildren($('bucket'), ...buckets.map(bucket => {
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
  await loadSkillFiles();
  await loadNamespaces(preferredNamespace, { preserveSession });
}

async function loadNamespaces(preferredNamespace = null, { preserveSession = false } = {}) {
  state.namespace = null; state.table = null; state.tableManaged = false;
  setChildren($('namespace')); setChildren($('tables')); clearPreflight({ forgetSession: !preserveSession });
  if (!state.bucket) { $('namespace').disabled = true; $('scope').textContent = state.isAdmin ? 'Create an S3 Tables bucket to begin.' : 'No assigned S3 Tables bucket.'; renderAdminProvisioning(); valid(); return; }
  const query = new URLSearchParams({ table_bucket_arn: state.bucket.table_bucket_arn });
  const response = await apiFetch(`/api/namespaces?${query}`); const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load namespaces');
  const namespaces = [...data.namespaces];
  if (preferredNamespace && !namespaces.includes(preferredNamespace)) namespaces.push(preferredNamespace);
  setChildren($('namespace'), ...namespaces.map(namespace => {
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
  setChildren($('tables'), ...data.tables.map(table => {
    const card = document.createElement('article'); card.className = 'table'; card.dataset.table = table.name;
    const select = document.createElement('button'); select.className = 'table-select'; select.type = 'button';
    select.innerHTML = `<strong>${table.name}</strong><small>Created: ${formatTime(table.created_at)}</small><small>Modified: ${formatTime(table.modified_at)}</small><small>Rows: ${table.row_count?.toLocaleString() ?? 'Unavailable'}</small>${table.uploader_managed ? '' : '<small class="browse-only">Browse only: no uploader schema/recovery contract.</small>'}`;
    select.onclick = () => { clearPreflight(); state.table = table.name; state.tableManaged = Boolean(table.uploader_managed); state.tableDeduplicationColumns = table.deduplication_columns || []; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; updateDeduplicationModeVisibility(); selectTable(); valid(); loadHistory(); };
    card.append(select);
    if (state.isAdmin && table.uploader_managed) {
      const remove = document.createElement('button'); remove.className = 'danger'; remove.type = 'button'; remove.textContent = 'Delete table';
      remove.onclick = () => deleteTable(table.name); card.append(remove);
    }
    return card;
  }));
  if (!data.tables.some(table => table.name === state.table)) { state.table = null; state.tableManaged = false; state.tableDeduplicationColumns = []; }
  if (data.tables.length === 0) {
    state.mode = 'create'; state.tableManaged = true;
    $('create').checked = true; $('new-table-wrap').hidden = false;
  }
  updateDeduplicationModeVisibility(); selectTable(); valid(); await loadHistory();
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
  $('preflight').disabled = !ready || hasReviewedSession || ['RECEIVED', 'PROFILING', 'QUEUED'].includes(state.sessionPhase);
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
  if (state.workerLeaseId) data.append('worker_lease_id', state.workerLeaseId);
  [...$('files').files].forEach(file => data.append('files', file)); return data;
}

async function cancelUnattachedWorkerLease() {
  if (!state.workerLeaseId || state.sessionId) return;
  const leaseId = state.workerLeaseId;
  state.workerLeaseId = null; state.workerLease = null;
  try { await apiFetch(`/api/v3/worker-leases/${encodeURIComponent(leaseId)}`, { method: 'DELETE' }); } catch (_) { /* expiry also cleans up */ }
}

async function warmSelectedFiles() {
  const files = [...$('files').files];
  if (!files.length) { await cancelUnattachedWorkerLease(); return; }
  const filePayload = { files: files.map(file => ({ name: file.name, size_bytes: file.size })) };
  if (state.workerLeaseId && !state.sessionId) {
    try {
      const response = await apiFetch(`/api/v3/worker-leases/${encodeURIComponent(state.workerLeaseId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(filePayload),
      });
      const lease = await response.json();
      if (response.ok) {
        state.workerLeaseId = lease.lease_id; state.workerLease = lease;
        $('activity').textContent = lease.reused
          ? `File selection updated; reusing the ${lease.worker_size === 'LARGE' ? 'large' : 'base'} worker.`
          : `File selection needs a ${lease.worker_size === 'LARGE' ? 'large' : 'base'} worker; replacing the idle worker.`;
        return;
      }
    } catch (_) { /* Fall back to a new lease below. */ }
    await cancelUnattachedWorkerLease();
  }
  try {
    const response = await apiFetch('/api/v3/worker-leases', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(filePayload),
    });
    const lease = await response.json();
    if (!response.ok) return; // Feature flag disabled or temporary failure: Review keeps its v2 fallback.
    state.workerLeaseId = lease.lease_id; state.workerLease = lease;
    $('activity').textContent = `Starting a ${lease.worker_size === 'LARGE' ? 'large' : 'base'} worker while the upload is reviewed…`;
  } catch (_) { /* Review creates a worker lease if the warm-up request was unavailable. */ }
}

async function retryLargeWorker() {
  if (!state.workerLeaseId) return;
  const response = await apiFetch(`/api/v3/worker-leases/${encodeURIComponent(state.workerLeaseId)}/retry-large`, { method: 'POST' });
  const result = await response.json();
  if (!response.ok) {
    $('upload-status').className = 'operation-status failed';
    $('upload-status').textContent = responseDetail(result, 'Large-worker retry could not start.');
    return;
  }
  state.workerLease = result;
  $('retry-large').hidden = true;
  $('review-status').className = 'operation-status';
  $('review-status').textContent = 'Large worker retry is starting…';
  if (state.sessionId) await pollUploadSession(state.sessionId);
}

function selectedDeduplicationMode() {
  return document.querySelector('input[name="deduplication-mode"]:checked')?.value || 'keyed';
}

function immutableDeduplicationColumns() {
  return state.mode === 'append' && hasLockedDeduplicationKey()
    ? (state.review?.deduplication_columns || [])
    : [];
}

function hasLockedDeduplicationKey() {
  return state.mode === 'append' && (state.tableDeduplicationColumns || []).length > 0;
}

function updateDeduplicationModeVisibility() {
  const locked = hasLockedDeduplicationKey();
  $('deduplication-mode').hidden = locked;
  if (locked) {
    document.querySelector('input[name="deduplication-mode"][value="keyed"]').checked = true;
    state.deduplicationMode = 'keyed';
  }
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
  const operationId = session.ingestion?.request_id || state.currentOperationId;
  const identifier = operationId
    ? ` Operation ID: ${operationId}.`
    : session.session_id ? ` Session ID: ${session.session_id}.` : '';
  const message = `${session.progress_message || 'Processing upload session…'}${elapsed}${identifier}`;
  $('activity').textContent = message;
  const ingestionQueueWait = session.phase === 'QUEUED' && Boolean(session.ingestion?.job_id);
  if (['STARTING_GLUE', 'GLUE_RUNNING'].includes(session.phase) || ingestionQueueWait) {
    $('outcome').hidden = false;
    $('status').className = 'running';
    $('status').textContent = message;
    $('upload-status').className = 'operation-status';
    $('upload-status').textContent = message;
  } else if (keyAnalysisBusy() && $('key-analysis-status')) {
    $('key-analysis-status').className = 'operation-status';
    $('key-analysis-status').textContent = message;
  } else if (['RECEIVED', 'PROFILING', 'QUEUED'].includes(session.phase)) {
    $('review-status').className = 'operation-status';
    $('review-status').textContent = message;
  }
}

function applySessionState(session) {
  state.sessionId = session.session_id;
  sessionStorage.setItem(SESSION_STORAGE_KEY, session.session_id);
  if (session.worker_lease) {
    state.workerLease = session.worker_lease;
    state.workerLeaseId = session.worker_lease.lease_id;
  }
  if (session.phase === 'FAILED' && session.error) sessionFailure(session);
  // Render preflight once. Polling must preserve selections, search text and
  // focus; a refreshed page restores choices from the acknowledged analysis.
  const restoredDeduplicationColumns = session.key_impact?.deduplication_columns || selectedDeduplicationColumns();
  const restoredTypeOverrides = session.key_impact?.type_overrides || selectedTypeOverrides();
  if (session.preflight && !state.review) {
    state.review = session.preflight;
    $('review').hidden = false;
    $('review').open = true;
    renderPreflight(session.preflight, { restoredDeduplicationColumns, restoredTypeOverrides });
    $('upload-actions').hidden = !session.preflight.accepted;
  }
  if (session.key_impact && session.phase === 'READY_FOR_ACKNOWLEDGEMENT' && state.appliedKeyToken !== session.key_impact.acknowledgement_token) {
    state.appliedKeyToken = session.key_impact.acknowledgement_token;
    const impact = session.key_impact;
    state.keyAnalysis = {
      token: impact.acknowledgement_token,
      columns: impact.deduplication_columns || [],
      typeSignature: JSON.stringify(restoredTypeOverrides),
    };
    state.keyAnalysisAcknowledged = false;
    renderKeyAnalysis(impact);
    if ($('key-analysis-status')) {
      $('key-analysis-status').className = 'operation-status complete';
      $('key-analysis-status').textContent = 'Key-impact analysis completed. Review the results below.';
    }
  }
  if (session.ingestion?.job_run_id) {
    $('outcome').hidden = false;
    $('status-body').textContent = JSON.stringify(session.ingestion, null, 2);
    const knownState = session.ingestion.state || (session.phase === 'SUCCEEDED' ? 'SUCCEEDED' : null);
    if (terminalStates.includes(knownState)) {
      state.activeJobRunId = null;
      const succeeded = knownState === 'SUCCEEDED';
      const message = succeeded ? 'ETL completed successfully.' : `ETL ended with state ${knownState}.`;
      $('activity').textContent = session.progress_message || message;
      $('status').textContent = session.progress_message || message;
      $('status').className = succeeded ? 'succeeded' : 'failed';
      $('upload-status').textContent = message;
      $('upload-status').className = succeeded ? 'operation-status complete' : 'operation-status failed';
      $('upload').textContent = 'Upload and run ETL';
    } else {
      $('status').textContent = session.progress_message;
      $('status').className = 'running';
      state.activeJobRunId = session.ingestion.job_run_id;
      poll(session.ingestion.job_run_id, session.ingestion.qc_uri, 'ingestion');
    }
  }
  updateCreateUploadEligibility();
  updateDeduplicationSelectionControls();
  valid();
}

function sessionFailure(session) {
  const reason = responseDetail(session.error, session.progress_message || 'The upload session failed.');
  $('activity').textContent = 'Upload session failed.';
  if ($('key-analysis-status')) {
    $('key-analysis-status').className = 'operation-status failed';
    $('key-analysis-status').textContent = reason;
  }
  $('review-status').className = 'operation-status failed';
  $('review-status').textContent = reason;
  $('upload-status').className = 'operation-status failed';
  $('upload-status').textContent = reason;
  $('outcome').hidden = false;
  $('status').className = 'failed';
  $('status').textContent = 'Upload was not started.';
  $('status-body').textContent = JSON.stringify(session.error || session, null, 2);
  $('retry-large').hidden = !(session.worker_lease?.can_retry_large && state.workerLeaseId);
}

async function pollUploadSession(sessionId, { until = [] } = {}) {
  clearSessionPoll();
  const generation = state.sessionPollGeneration;
  while (generation === state.sessionPollGeneration) {
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(sessionId)}`);
    const session = await response.json();
    if (generation !== state.sessionPollGeneration || state.sessionId !== sessionId) break;
    if (!response.ok) throw new Error(responseDetail(session, 'Unable to refresh upload progress. Refresh the page to reconnect.'));
    state.sessionPhase = session.phase;
    applySessionState(session);
    if (session.phase === 'FAILED') return session;
    renderSessionProgress(session);
    if (until.includes(session.phase) || ['GLUE_RUNNING', 'SUCCEEDED'].includes(session.phase) || (!until.length && sessionTerminalPhases.includes(session.phase))) return session;
    await new Promise(resolve => {
      state.sessionPollResolve = resolve;
      state.sessionPollTimer = setTimeout(() => {
        state.sessionPollTimer = null;
        state.sessionPollResolve = null;
        resolve();
      }, 1000);
    });
  }
  return { phase: 'CANCELLED' };
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
    const bucket = { table_bucket_arn: session.table_bucket_arn, label: lastPathSegment(session.table_bucket_arn) || session.table_bucket_arn };
    await loadBuckets(bucket, { preserveSession: true, preferredNamespace: session.namespace });
    state.mode = session.mode;
    state.table = session.mode === 'append' ? session.table : null;
    state.tableManaged = true;
    $('create').checked = session.mode === 'create';
    $('new-table-wrap').hidden = session.mode !== 'create';
    if (session.mode === 'create') $('new-table').value = session.table;
    selectTable(); valid();
    state.sessionPhase = session.phase;
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
  holder.textContent = impact.behaviour === 'invalid_values_become_null'
    ? `Choosing ${control.value} will convert ${Number(impact.invalid_value_count).toLocaleString()} non-compliant populated value(s) to NULL. Choose STRING to preserve every value.`
    : `Choosing ${control.value} is incompatible with ${Number(impact.invalid_value_count).toLocaleString()} populated value(s). Choose a compatible type.`;
}

function selectedDeduplicationColumns() {
  return [...document.querySelectorAll('[data-deduplication-column]:checked')]
    .map(control => control.dataset.deduplicationColumn);
}

function keyAnalysisBusy() {
  return state.keyAnalysisPending || ['KEY_ANALYSING', 'QUEUED'].includes(state.sessionPhase);
}

function filterDeduplicationColumns() {
  const query = ($('deduplication-search')?.value || '').trim().toLowerCase();
  const controls = [...document.querySelectorAll('[data-deduplication-column]')];
  let visible = 0;
  controls.forEach(control => {
    const matches = control.dataset.deduplicationColumn.toLowerCase().includes(query);
    control.closest('.deduplication-candidate').hidden = !matches;
    if (matches) visible += 1;
  });
  const summary = $('deduplication-filter-count');
  if (summary) summary.textContent = visible ? `${visible} of ${controls.length} columns shown. Filtering keeps all selections.` : 'No matching columns. Try another name.';
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
  if (analyse) {
    const busy = keyAnalysisBusy();
    analyse.disabled = selectedCount === 0 || busy;
    analyse.classList.toggle('is-busy', busy);
    analyse.setAttribute('aria-busy', String(busy));
    analyse.textContent = busy ? 'Analysing selected key…' : 'Analyse selected key impact';
  }
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
  if (keyAnalysisBusy() || (state.sessionPhase && !['READY_FOR_REVIEW', 'READY_FOR_ACKNOWLEDGEMENT'].includes(state.sessionPhase))) { $('upload').disabled = true; return; }
  if (state.review.temporal_policy_adoption?.required && !state.temporalPolicyAcknowledged) { $('upload').disabled = true; return; }
  if (hasLockedDeduplicationKey()) {
    $('upload').disabled = !state.review.accepted;
    return;
  }
  const mode = selectedDeduplicationMode();
  if (mode === 'none') {
    $('upload').disabled = !state.review.accepted;
    return;
  }
  if (state.review.mode !== 'create') {
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
  const holder = $('key-analysis-result'); if (holder) setChildren(holder);
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
  if (keyAnalysisBusy()) return;
  const selected = selectedDeduplicationColumns();
  if (!selected.length || !state.sessionId) {
    $('key-analysis-status').textContent = !selected.length ? 'Choose at least one column to analyse.' : 'Review the upload before analysing a key.';
    return;
  }
  state.keyAnalysisPending = true;
  invalidateKeyAnalysis();
  const sessionId = state.sessionId;
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
      status.className = 'operation-status failed'; status.textContent = `Key-impact analysis failed: ${reason}${requestDiagnostic()}`;
      return;
    }
    if (state.sessionId !== sessionId) return;
    state.sessionPhase = result.phase || 'KEY_ANALYSING';
    status.textContent = 'Composite-key impact analysis is in progress…';
    const session = await pollUploadSession(sessionId, { until: ['READY_FOR_ACKNOWLEDGEMENT'] });
    if (session.phase === 'READY_FOR_ACKNOWLEDGEMENT') {
      $('activity').textContent = 'Key-impact analysis is ready. Review it, revise the key if needed, or acknowledge it to enable upload.';
      status.className = 'operation-status complete'; status.textContent = 'Key-impact analysis completed. Review the results below.';
    }
  } catch (error) {
    $('activity').textContent = 'Key-impact analysis failed.';
    status.className = 'operation-status failed'; status.textContent = `Unable to confirm key-analysis status: ${error.message || 'network request failed'}. Refresh the page to reconnect.${requestDiagnostic()}`;
  } finally {
    if (state.sessionId === sessionId) {
      state.keyAnalysisPending = false;
      updateDeduplicationSelectionControls();
      updateCreateUploadEligibility();
    }
  }
}

function renderPreflight(result, { restoredDeduplicationColumns = [], restoredTypeOverrides = {} } = {}) {
  const holder = $('review-body'); setChildren(holder);
  updateDeduplicationModeVisibility();
  const decision = document.createElement('p');
  const needsTemporalPolicyAcknowledgement = Boolean(result.temporal_policy_adoption?.required);
  decision.className = needsTemporalPolicyAcknowledgement ? 'preflight-action' : result.accepted ? 'preflight-pass' : 'preflight-reject';
  decision.textContent = needsTemporalPolicyAcknowledgement
    ? 'Action required: confirm the stored temporal conversion policy before uploading.'
    : result.accepted
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
    choices.innerHTML = `<h3>Choose ambiguous initial column types</h3><p>Automatic DATE, TIMESTAMP, BIGINT, and DOUBLE rules have already been applied where the full first file is unambiguous. Choose a type only for these remaining ambiguous columns. Select <code>STRING</code> when preserving every source value is more important than typed conversion; it is the safest choice for minimum data loss. Up to five random, non-empty examples are shown only for this review and are not stored. Healthcare-sanitized fields remain locked as <code>STRING</code>.</p>${rows}`;
    holder.append(choices);
    choices.querySelectorAll('[data-type-override]').forEach(control => {
      updateTypeChoiceImpact(control);
      control.addEventListener('change', () => { updateTypeChoiceImpact(control); invalidateKeyAnalysis(); });
    });
  }
  if (needsTemporalPolicyAcknowledgement) {
    const policy = result.temporal_policy_adoption;
    const section = document.createElement('section'); section.className = 'temporal-policy-adoption';
    const columns = policy.columns.map(item => `<li><code>${escapeHtml(item.column)}</code> (${escapeHtml(item.target_type)}): ${Number(item.invalid_value_count).toLocaleString()} value(s) will become NULL.</li>`).join('');
    section.innerHTML = `<h3>Confirm temporal conversion policy</h3><p>This older table was created before its approved temporal conversion policy was stored. Confirming applies the same DATE/TIMESTAMP handling to this upload and saves it for later uploads. Valid values are retained; only non-parsable populated values become NULL.</p><ul>${columns}</ul><label class="acknowledgement"><input id="acknowledge-temporal-policy" type="checkbox"> I understand these values will become NULL and want to save this immutable policy for later uploads.</label>`;
    holder.append(section);
    $('acknowledge-temporal-policy').onchange = () => { state.temporalPolicyAcknowledged = $('acknowledge-temporal-policy').checked; updateCreateUploadEligibility(); };
  }
  const lockedKey = result.deduplication_locked_columns || state.tableDeduplicationColumns || [];
  const hasLockedKey = result.mode === 'append' && lockedKey.length > 0;
  const needsFirstKey = result.mode === 'create' || !hasLockedKey;
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
      ? 'This older table has no composite key yet. Your first acknowledged key will be saved for later uploads; existing table rows are not rewritten.'
      : 'This selection becomes the table’s immutable de-duplication contract.';
    section.innerHTML = `<h3>Choose de-duplication columns</h3><p>Select one stable identifier, or multiple fields for a composite key. CSN, case, HRN, MRN, and other encrypted identifiers may be selected; their examples remain masked. ${activationNote} Before upload, analyse the full incoming dataset to see the duplicate/conflict impact. Per-column non-empty and distinct counts help assess a single-column key.</p><p class="deduplication-notice" id="deduplication-selection-notice">Choose at least one de-duplication column before uploading.</p><div class="deduplication-actions"><button type="button" id="select-all-deduplication" class="secondary" aria-pressed="false">Select all columns</button><span id="deduplication-selection-count" class="hint"></span></div><label class="deduplication-search-label" for="deduplication-search">Find a column<input id="deduplication-search" type="search" placeholder="Filter column names…" aria-controls="deduplication-candidate-list"></label><p id="deduplication-filter-count" class="hint" aria-live="polite"></p><div id="deduplication-candidate-list" class="deduplication-candidates">${rows}</div><button type="button" id="analyse-key" class="key-analysis-action" disabled>Analyse selected key impact</button><p id="key-analysis-status" class="operation-status" aria-live="polite"></p><div id="key-analysis-result"></div>`;
    holder.append(section);
    section.querySelectorAll('[data-deduplication-column]').forEach(control => control.addEventListener('change', deduplicationSelectionChanged));
    $('select-all-deduplication').onclick = toggleAllDeduplicationColumns;
    $('analyse-key').onclick = analyseSelectedKey;
    $('deduplication-search').oninput = filterDeduplicationColumns;
    filterDeduplicationColumns();
    updateDeduplicationSelectionControls();
  }
  if (hasLockedKey) {
    const section = document.createElement('section'); section.className = 'deduplication-selection';
    const active = result.deduplication_columns || [];
    section.innerHTML = active.length
      ? `<h3>Automatic de-duplication key</h3><p><strong>Saved table key:</strong> <code>${lockedKey.map(escapeHtml).join(' + ')}</code></p><p><strong>This upload uses:</strong> <code>${active.map(escapeHtml).join(' + ')}</code></p><p class="hint">Only saved key columns present in this upload are used. This upload is de-duplicated locally using that derived key.</p>`
      : `<h3>Automatic de-duplication key</h3><p><strong>Saved table key:</strong> <code>${lockedKey.map(escapeHtml).join(' + ')}</code></p><p class="hint">None of the saved key columns are present in this upload. It will be appended without de-duplication.</p>`;
    holder.append(section);
  }
  if (!hasLockedKey && selectedDeduplicationMode() === 'none') {
    const section = document.createElement('section'); section.className = 'deduplication-selection';
    section.innerHTML = '<h3>Clean-data append selected</h3><p>No de-duplication analysis or target-table comparison will run. Every validated incoming row is appended.</p>';
    holder.append(section);
  }
  for (const file of result.files || []) {
    const item = document.createElement('article'); item.className = `preflight-file ${file.accepted ? 'accepted' : 'rejected'}`;
    const sanitized = file.sanitized_columns?.length ? file.sanitized_columns.join(', ') : 'None';
    const reasons = file.rejection_reasons || [];
    const fileDecision = file.temporal_policy_adoption_required
      ? 'Requires temporal-policy confirmation'
      : file.accepted
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
    if (file.temporal_coercions?.length) {
      const conversions = file.temporal_coercions.map(item =>
        `${item.column} (${Number(item.unsafe_value_count).toLocaleString()} value(s) will become NULL)`,
      ).join(', ');
      const temporalNotice = document.createElement('p');
      temporalNotice.className = 'hint';
      temporalNotice.innerHTML = `<strong>Approved temporal conversions:</strong> ${escapeHtml(conversions)}.`;
      item.append(temporalNotice);
    }
    if (file.extra_columns?.length || file.missing_columns?.length || file.type_conversions?.length || file.temporal_coercions?.length || file.warnings?.length) {
      const details = document.createElement('details');
      details.innerHTML = `<summary>Technical schema details</summary><pre>${escapeHtml(JSON.stringify({
        extra_columns_ignored: file.extra_columns,
        missing_target_columns_filled_null: file.missing_columns,
        type_conversions: file.type_conversions,
        unsafe_casts: file.unsafe_casts,
        temporal_coercions_to_null: file.temporal_coercions,
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
  const holder = $('history-body'); setChildren(holder);
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
  $('status-body').textContent = JSON.stringify(result, null, 2); pollMutation(result.mutation_id);
}

$('refresh').onclick = loadNamespaces;
$('refresh-history').onclick = loadHistory;
$('new-bucket').oninput = renderAdminProvisioning;
$('new-namespace').oninput = renderAdminProvisioning;
$('create-bucket').onclick = createTableBucket;
$('create-namespace').onclick = createSelectedNamespace;
$('upload-skill-bundle').onclick = uploadSkillBundle;
$('refresh-skill-files').onclick = loadSkillFiles;
$('retry-large').onclick = retryLargeWorker;
$('skill-bundle-files').onchange = updateSkillControls;
$('emulated-user').onchange = async () => {
  // A selected-file lease is owner scoped. Do not submit it after changing
  // the local identity emulation profile.
  state.workerLeaseId = null; state.workerLease = null;
  clearPreflight();
  clearSkillBundle();
  state.emulatedUserId = $('emulated-user').value || null;
  $('activity').textContent = `Testing backend authorization as ${state.emulatedUserId || 'no user'}…`;
  await loadBuckets();
};
$('bucket').onchange = async () => { clearPreflight(); state.bucket = JSON.parse($('bucket').value); clearSkillBundle(); state.namespace = null; state.table = null; state.tableManaged = false; state.tableDeduplicationColumns = []; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; updateDeduplicationModeVisibility(); await loadSkillFiles(); await loadNamespaces(); };
$('namespace').onchange = async () => { clearPreflight(); state.namespace = $('namespace').value || null; state.table = null; state.tableManaged = false; state.tableDeduplicationColumns = []; state.mode = 'append'; $('create').checked = false; $('new-table-wrap').hidden = true; updateDeduplicationModeVisibility(); await loadTables(); };
$('create').onchange = () => { clearPreflight(); state.mode = $('create').checked ? 'create' : 'append'; if (state.mode === 'create') { state.table = null; state.tableManaged = true; state.tableDeduplicationColumns = []; } $('new-table-wrap').hidden = state.mode !== 'create'; updateDeduplicationModeVisibility(); selectTable(); valid(); loadHistory(); };
$('new-table').oninput = () => { clearPreflight(); valid(); };
$('files').onchange = async () => { clearPreflight(); valid(); await warmSelectedFiles(); };
$('reporting-month').oninput = () => { clearPreflight(); valid(); };
$('deduplication-mode').onchange = () => { state.deduplicationMode = selectedDeduplicationMode(); clearPreflight(); valid(); };
$('preflight').onclick = async () => {
  const button = $('preflight'); const status = $('review-status');
  button.disabled = true; button.classList.add('is-busy'); button.textContent = 'Reviewing upload…';
  status.className = 'operation-status'; status.textContent = 'Sending files to the server. File analysis starts after receipt…';
  $('activity').textContent = 'Scanning selected file schemas…';
  try {
    const response = await apiFetch('/api/v2/upload-sessions', { method: 'POST', body: formData() });
    const result = await response.json();
    if (!response.ok) {
      const reason = responseDetail(result, 'Data structure analysis could not start.');
      $('activity').textContent = 'Preflight failed.';
      status.className = 'operation-status failed'; status.textContent = `Upload review failed: ${reason}${requestDiagnostic()}`;
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
    status.className = 'operation-status failed'; status.textContent = `Upload review failed: ${error.message || 'network request failed'}${requestDiagnostic()}`;
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
    state.currentOperationId = createOperationRequestId();
    const response = await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(state.sessionId)}/ingestions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: state.currentOperationId, reporting_month: userTag(), type_overrides: selectedTypeOverrides(), deduplication_mode: hasLockedDeduplicationKey() ? (immutableDeduplicationColumns().length ? 'keyed' : 'none') : selectedDeduplicationMode(), deduplication_columns: hasLockedDeduplicationKey() ? immutableDeduplicationColumns() : (selectedDeduplicationMode() === 'keyed' ? selectedDeduplicationColumns() : []), key_analysis_token: state.keyAnalysis?.token || null, temporal_policy_acknowledgement_token: state.temporalPolicyAcknowledged ? state.review?.temporal_policy_adoption?.acknowledgement_token || null : null, manual_encryption_columns: selectedManualEncryptionColumns() }),
    }); const result = await response.json();
    if (!response.ok) {
      const reason = responseDetail(result, 'Upload could not be started.');
      $('status').textContent = 'Upload was not started.'; $('status').className = 'failed'; $('status-body').textContent = JSON.stringify(result, null, 2);
      status.className = 'operation-status failed'; status.textContent = `Upload could not start: ${reason}${requestDiagnostic()}`;
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
    status.className = 'operation-status failed'; status.textContent = `Upload could not start: ${error.message || 'network request failed'}${requestDiagnostic()}`;
  } finally {
    button.classList.remove('is-busy');
    if (!started) { state.currentOperationId = null; button.textContent = 'Upload and run ETL'; updateCreateUploadEligibility(); }
  }
};
async function poll(id, qcUri, operation, retryCount = 0) {
  if (state.activeJobRunId && state.activeJobRunId !== id) return;
  state.activeJobRunId = id;
  try {
    const response = await apiFetch(`/api/ingestions/${id}?operation=${operation}`);
    const result = await response.json();
    if (!response.ok) throw new Error(responseDetail(result, 'AWS Glue status is temporarily unavailable.'));
    const terminal = terminalStates.includes(result.state);
    $('activity').textContent = result.message;
    $('status').textContent = result.message;
    $('status').className = result.state === 'SUCCEEDED' ? 'succeeded' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'failed' : 'running';
    if (operation === 'ingestion') {
      const uploadStatus = $('upload-status');
      uploadStatus.className = result.state === 'SUCCEEDED' ? 'operation-status complete' : ['FAILED','ERROR','TIMEOUT','STOPPED'].includes(result.state) ? 'operation-status failed' : 'operation-status';
      uploadStatus.textContent = result.message;
      if (terminal) $('upload').textContent = 'Upload and run ETL';
    }
    $('status-body').textContent = JSON.stringify(result, null, 2);
    if (!terminal) {
      state.gluePollTimer = setTimeout(() => poll(id, qcUri, operation), 5000);
      return;
    }
    state.gluePollTimer = null;
    state.activeJobRunId = null;
    // Persist the terminal state in the session store so a later refresh can
    // recover the completed result instead of showing GLUE_RUNNING forever.
    if (operation === 'ingestion' && state.sessionId) {
      try { await apiFetch(`/api/v2/upload-sessions/${encodeURIComponent(state.sessionId)}`); } catch (_) { /* status already comes from Glue */ }
    }
    try {
      const qc = await apiFetch(`/api/qc?uri=${encodeURIComponent(qcUri)}`).then(r => r.ok ? r.json() : null);
      if (qc) $('status-body').textContent = JSON.stringify({ job: result, qc }, null, 2);
    } catch (_) { /* QC is supplementary; never replace a terminal Glue state */ }
    if (result.state === 'SUCCEEDED') {
      try { await loadTables(); await loadHistory(); } catch (_) { /* table refresh can be retried independently */ }
    }
  } catch (error) {
    if (state.activeJobRunId !== id) return;
    const delay = Math.min(30000, 5000 * Math.max(1, retryCount + 1));
    const message = `Unable to refresh AWS Glue status; retrying in ${Math.ceil(delay / 1000)}s.`;
    $('activity').textContent = `${message} ${error.message || ''}`;
    $('status').textContent = message;
    $('status').className = 'running';
    if (operation === 'ingestion') {
      $('upload-status').textContent = message;
      $('upload-status').className = 'operation-status';
    }
    state.gluePollTimer = setTimeout(() => poll(id, qcUri, operation, retryCount + 1), delay);
  }
}

async function pollMutation(mutationId, retryCount = 0) {
  const activeId = `mutation:${mutationId}`;
  if (state.activeJobRunId && state.activeJobRunId !== activeId) return;
  state.activeJobRunId = activeId;
  try {
    const response = await apiFetch(`/api/mutations/${encodeURIComponent(mutationId)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(responseDetail(result, 'Mutation status is temporarily unavailable.'));
    const status = result.status || {};
    const terminal = ['SUCCEEDED', 'FAILED'].includes(status.phase);
    $('activity').textContent = status.message || 'Rollback is queued.';
    $('status').textContent = status.message || 'Rollback is queued.';
    $('status').className = status.phase === 'SUCCEEDED' ? 'succeeded' : status.phase === 'FAILED' ? 'failed' : 'running';
    $('status-body').textContent = JSON.stringify(result, null, 2);
    if (!terminal) {
      state.gluePollTimer = setTimeout(() => pollMutation(mutationId), 5000);
      return;
    }
    state.gluePollTimer = null;
    state.activeJobRunId = null;
    if (status.phase === 'SUCCEEDED') {
      try { await loadTables(); await loadHistory(); } catch (_) { /* refresh can be retried independently */ }
    }
  } catch (error) {
    if (state.activeJobRunId !== activeId) return;
    const delay = Math.min(30000, 5000 * Math.max(1, retryCount + 1));
    $('activity').textContent = `Unable to refresh rollback status; retrying in ${Math.ceil(delay / 1000)}s.`;
    $('status').textContent = $('activity').textContent;
    $('status').className = 'running';
    state.gluePollTimer = setTimeout(() => pollMutation(mutationId, retryCount + 1), delay);
  }
}

loadIdentityProfiles().then(loadBuckets).then(resumeUploadSession).catch(error => {
  const detail = error?.message || String(error);
  $('scope').textContent = `Unable to load assigned S3 Tables buckets: ${detail}`;
  $('effective-identity').textContent = JSON.stringify({ error: 'Identity profile could not be loaded', detail }, null, 2);
  $('outgoing-identity').textContent = JSON.stringify(identityRequestPayload(), null, 2);
});
