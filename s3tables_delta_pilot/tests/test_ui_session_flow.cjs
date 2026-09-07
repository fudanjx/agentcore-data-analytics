// Run with: node --test s3tables_delta_pilot/tests/test_ui_session_flow.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
function load(names, globals) {
  const context = vm.createContext(globals);
  for (const name of names) {
    const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
    assert.ok(start >= 0, name);
    const tail = source.slice(start);
    const next = tail.slice(1).search(/\n(?:async )?function /);
    vm.runInContext(next < 0 ? tail : tail.slice(0, next + 1), context);
  }
  return context;
}
function element() { return { textContent: '', disabled: false, classList: { add() {}, toggle() {} }, setAttribute() {} }; }
function ui() {
  const elements = {};
  const controls = ['case_no', 'visit_date', 'clinic'].map(name => ({
    checked: name === 'case_no', disabled: false, dataset: { deduplicationColumn: name },
    row: { hidden: false }, closest() { return this.row; },
  }));
  return { controls, elements, $: id => elements[id] ||= element(), document: { querySelectorAll: selector => selector.endsWith(':checked') ? controls.filter(c => c.checked) : controls } };
}
test('one-column analysis remains busy through queued/running polls and blocks repeat clicks', async () => {
  const dom = ui(); let posts = 0; let wake; let polls = 0;
  const state = { sessionId: 'session', sessionPollGeneration: 0, sessionPhase: 'READY_FOR_REVIEW' };
  const context = load(['clearSessionPoll', 'pollUploadSession', 'keyAnalysisBusy', 'selectedDeduplicationColumns', 'updateDeduplicationSelectionControls', 'analyseSelectedKey'], {
    ...dom, state, sessionTerminalPhases: [],
    selectedTypeOverrides: () => ({}), invalidateKeyAnalysis() {}, updateCreateUploadEligibility() {},
    responseDetail: (_, fallback) => fallback, renderSessionProgress() {},
    applySessionState() {}, clearTimeout() {}, setTimeout: fn => { wake = fn; return 1; },
    apiFetch: async (_, options) => {
      if (options?.method === 'POST') {
        posts++; assert.deepEqual(JSON.parse(options.body).deduplication_columns, ['case_no']);
        return { ok: true, json: async () => ({}) };
      }
      return { ok: true, json: async () => ({ session_id: 'session', phase: ['QUEUED', 'KEY_ANALYSING', 'READY_FOR_ACKNOWLEDGEMENT'][polls++] }) };
    },
  });
  let done = false;
  const task = context.analyseSelectedKey().then(() => { done = true; });
  await new Promise(setImmediate);
  assert.equal(done, false); assert.equal(dom.$('analyse-key').disabled, true);
  // Changing selections must not re-enable the button during processing.
  dom.controls[1].checked = true; context.updateDeduplicationSelectionControls();
  assert.equal(dom.$('analyse-key').disabled, true);
  await context.analyseSelectedKey(); assert.equal(posts, 1);
  wake(); await new Promise(setImmediate);
  assert.equal(done, false); assert.equal(dom.$('analyse-key').disabled, true);
  wake(); await task;
  assert.equal(polls, 3); assert.equal(dom.$('analyse-key').disabled, false);
  assert.match(dom.$('key-analysis-status').textContent, /completed/);
});
test('search is case insensitive and preserves checked columns hidden by the filter', () => {
  const dom = ui(); const context = load(['filterDeduplicationColumns', 'selectedDeduplicationColumns'], dom);
  dom.$('deduplication-search').value = ' DATE '; context.filterDeduplicationColumns();
  assert.deepEqual(dom.controls.map(c => c.row.hidden), [true, false, true]);
  assert.equal(context.selectedDeduplicationColumns().join(), 'case_no');
  dom.$('deduplication-search').value = 'missing'; context.filterDeduplicationColumns();
  assert.match(dom.$('deduplication-filter-count').textContent, /No matching/);
  dom.$('deduplication-search').value = ''; context.filterDeduplicationColumns();
  assert.ok(dom.controls.every(c => !c.row.hidden));
});
test('cancelling polling resolves its caller without applying a stale response', async () => {
  let receive; let applied = 0;
  const context = load(['clearSessionPoll', 'pollUploadSession'], {
    state: { sessionId: 'session', sessionPollGeneration: 0 }, clearTimeout() {},
    apiFetch: () => new Promise(resolve => { receive = resolve; }),
    applySessionState() { applied++; },
  });
  const task = context.pollUploadSession('session'); context.clearSessionPoll();
  receive({ ok: true, json: async () => ({ phase: 'PROFILING' }) });
  assert.equal((await task).phase, 'CANCELLED'); assert.equal(applied, 0);
});
test('repeated status polls preserve controls and acknowledgement instead of rebuilding preflight', () => {
  const dom = ui(); let renders = 0; let impacts = 0;
  const state = {};
  const context = load(['applySessionState'], {
    ...dom, state, SESSION_STORAGE_KEY: 'session', sessionStorage: { setItem() {} },
    selectedDeduplicationColumns: () => ['case_no'], selectedTypeOverrides: () => ({}),
    renderPreflight() { renders++; }, renderKeyAnalysis() { impacts++; },
    updateCreateUploadEligibility() {}, updateDeduplicationSelectionControls() {}, valid() {},
  });
  const session = { session_id: 'session', phase: 'KEY_ANALYSING', preflight: { accepted: true } };
  context.applySessionState(session); context.applySessionState(session);
  assert.equal(renders, 1);
  session.phase = 'READY_FOR_ACKNOWLEDGEMENT';
  session.key_impact = { acknowledgement_token: 'token', deduplication_columns: ['case_no'], type_overrides: {} };
  context.applySessionState(session); state.keyAnalysisAcknowledged = true; context.applySessionState(session);
  assert.equal(renders, 1); assert.equal(impacts, 1); assert.equal(state.keyAnalysisAcknowledged, true);
});
