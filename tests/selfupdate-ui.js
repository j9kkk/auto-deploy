'use strict';
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/assets/views.js'), 'utf8');
async function scenario(responses, stored = new Map()) {
  const timers = new Map();
  let id = 0, reloads = 0;
  const nodes = new Map();
  const body = { innerHTML: '', insertAdjacentHTML(_, html) { this.innerHTML += html; },
    querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, { value: '', querySelector: key => body.querySelector(key), handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; }, remove() {} }); return nodes.get(selector); } };
  const panel = { isConnected: true, querySelector() { return body; } };
  const AD = { state: {}, escapeHtml: String, attr: String, confirm: async () => true };
  const requests = [];
  const context = { AD, window: { AD, addEventListener() {} }, AbortController,
    setTimeout(fn) { timers.set(++id, fn); return id; }, clearTimeout(n) { timers.delete(n); },
    sessionStorage: { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v) },
    location: { reload() { reloads++; } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error('网络不可达');
      return { ok: !next.http, status: next.http || 200, json: async () => next };
    } };
  vm.runInNewContext(source, context);
  AD.renderUpdatePanel(panel);
  const flush = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };
  await flush();
  return { AD, body, nodes, timers, requests, flush, reloads: () => reloads,
    async tick() { const current = [...timers.values()]; timers.clear(); current.forEach(fn => fn()); await flush(); } };
}
(async () => {
  const done = { stage: 'done', active: false, operation: 'update', operation_id: 'op1',
    confirmed_operation_id: 'op1', confirmed_operation: 'update', before_boot_id: 'old', boot_id: 'new',
    before_pid: 1, pid: 2, current_version: 'v2.0.0', version: '2.0.0', expected_version: '2.0.0' };
  const stored = new Map();
  let test = await scenario([done], stored);
  assert.equal(test.reloads(), 1);
  assert.equal(test.timers.size, 0);
  test = await scenario([done, { settings: {} }], stored);
  assert.equal(test.reloads(), 0);
  for (const http of [401, 500]) {
    test = await scenario([{ stage: 'restarting', active: true, log: ['保留日志'] }, { http }]);
    await test.tick();
    assert.equal(test.reloads(), 0);
    assert.match(test.body.innerHTML, /保留日志/);
    assert.equal(test.timers.size, 0);
  }
  test = await scenario([{ ...done, current_version: '1.0.0' }]);
  assert.equal(test.reloads(), 0);
  assert.match(test.body.innerHTML, /未确认/);
  test = await scenario([{ stage: 'failed', error: '更新失败原因', log: ['失败日志'] }, { settings: {} }]);
  assert.match(test.body.innerHTML, /更新失败原因/);
  assert.match(test.body.innerHTML, /失败日志/);
  test = await scenario([]);
  for (let i = 0; i < 45; i++) await test.tick();
  assert.equal(test.timers.size, 0);
  assert.equal(test.reloads(), 0);
  assert.equal(test.requests.length, 40);
  test = await scenario([{ stage: 'idle', active: false, can_rollback: true }, { settings: { update_repo: '旧源' } },
    {}, { current: 'v1.0.0', latest: 'v2.0.0', update_available: true },
    { state: { operation_id: 'confirmed-target' } }, { stage: 'restarting', active: true, operation_id: 'confirmed-target' }]);
  test.nodes.get('#upd-repo').value = '新源';
  await test.nodes.get('#upd-check').handlers.click({ target: {} });
  assert.equal(test.requests[2].options.method, 'PUT');
  assert.equal(JSON.parse(test.requests[2].options.body).update_repo, '新源');
  assert.equal(test.requests[3].url, '/api/system/update/check?force=true');
  assert.doesNotMatch(test.nodes.get('#upd-check-result').innerHTML, /vv/);
  await test.nodes.get('#upd-start').handlers.click();
  await test.flush();
  assert.equal(JSON.parse(test.requests[4].options.body).target_version, 'v2.0.0');
  test.AD.stopUpdatePanel();
  test = await scenario([{ stage: 'restarting', active: true }]);
  test.AD.stopUpdatePanel();
  assert.equal(test.timers.size, 0);
  console.log('前端回归通过：确认后仅刷新一次、401/500、版本不符、失败日志、40 次重连上限、离页清理');
})().catch(error => { console.error(error); process.exitCode = 1; });
