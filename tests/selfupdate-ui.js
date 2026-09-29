'use strict';
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/assets/views.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../web/assets/style.css'), 'utf8');
const escapeHtml = value => String(value).replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[ch]);

// 只模拟此卡片需要的 DOM：重绘会创建新节点；折叠日志没有尺寸。
// 尺寸为固定模型，不代表真实浏览器布局、原生 toggle/scroll 事件调度验证。
function mockBody(nodes) {
  class Element {
    constructor(tag = 'div', attributes = {}) {
      this.tag = tag;
      this.attributes = attributes;
      this.children = [];
      this.handlers = {};
      this.value = '';
      this.open = Object.hasOwn(attributes, 'open');
      this.disabled = Object.hasOwn(attributes, 'disabled');
      this.scrollTop = 0;
      this.scrollLeft = 0;
      if (attributes.id) nodes.set('#' + attributes.id, this);
    }
    get clientHeight() { return this.attributes.id === 'upd-log' && this.parent.open ? 120 : 0; }
    get scrollHeight() { return this.clientHeight ? Math.max(120, this.innerHTML.split('\n').length * 20) : 0; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    async fire(name) {
      if (name === 'click' && this.disabled) return;
      await this.handlers[name]?.({ target: this, currentTarget: this });
    }
    querySelector(selector) {
      for (const child of this.children) {
        if (typeof child === 'string') continue;
        if ('#' + child.attributes.id === selector) return child;
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    }
    forget() {
      for (const child of this.children) if (typeof child !== 'string') child.forget();
      if (this.attributes.id) nodes.delete('#' + this.attributes.id);
    }
    remove() {
      this.forget();
      this.parent.children = this.parent.children.filter(child => child !== this);
    }
    get innerHTML() {
      return this.children.map(child => typeof child === 'string' ? child : child.outerHTML).join('');
    }
    get outerHTML() {
      const attrs = Object.entries(this.attributes).map(([key, value]) => ' ' + key + '="' + value + '"').join('');
      return '<' + this.tag + attrs + '>' + this.innerHTML + (['input', 'br'].includes(this.tag) ? '' : '</' + this.tag + '>');
    }
    set innerHTML(html) {
      for (const child of this.children) if (typeof child !== 'string') child.forget();
      this.children = [];
      this.insertAdjacentHTML('beforeend', html);
    }
    set textContent(text) { this.innerHTML = escapeHtml(text); }
    insertAdjacentHTML(position, html) {
      assert.equal(position, 'beforeend');
      const stack = [this];
      for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
        if (token.startsWith('</')) { stack.pop(); continue; }
        const parent = stack[stack.length - 1];
        if (!token.startsWith('<')) { parent.children.push(token); continue; }
        const [, tag, raw] = token.match(/^<([\w-]+)([^>]*)>$/);
        const attributes = {};
        for (const match of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attributes[match[1]] = match[2] || '';
        const element = new Element(tag, attributes);
        element.parent = parent;
        parent.children.push(element);
        if (!['input', 'br'].includes(tag)) stack.push(element);
      }
    }
  }
  return new Element();
}

async function scenario(responses, stored = new Map()) {
  const timers = new Map();
  let id = 0, reloads = 0;
  const nodes = new Map();
  const body = mockBody(nodes);
  const panel = { isConnected: true, querySelector() { return body; } };
  const AD = { state: {}, escapeHtml, attr: escapeHtml, confirm: async () => true };
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
function recoveryAvailable(test, canRollback = true) {
  assert.equal(typeof test.nodes.get('#upd-check')?.handlers.click, 'function');
  assert.equal(typeof test.nodes.get('#upd-read-state')?.handlers.click, 'function');
  const rollback = test.nodes.get('#upd-rollback');
  assert.ok(rollback, '没有备份也必须显示回滚入口');
  assert.equal(rollback.disabled, !canRollback);
  if (canRollback) assert.equal(typeof rollback.handlers.click, 'function');
  else {
    assert.equal(rollback.handlers.click, undefined);
    assert.ok(test.nodes.get('#upd-rollback-reason').innerHTML);
    assert.equal(rollback.attributes['aria-describedby'], 'upd-rollback-reason');
  }
}
(async () => {
  const done = { stage: 'done', active: false, operation: 'update', operation_id: 'op1',
    confirmed_operation_id: 'op1', confirmed_operation: 'update', before_boot_id: 'old', boot_id: 'new',
    before_pid: 1, pid: 2, current_version: 'v2.0.0', version: '2.0.0', expected_version: '2.0.0' };
  const stored = new Map();
  let test = await scenario([done], stored);
  assert.equal(test.reloads(), 1, '无 schema 的完整旧记录仍按严格条件确认');
  assert.equal(test.timers.size, 0);
  test = await scenario([done, { settings: {} }], stored);
  assert.equal(test.reloads(), 0);
  assert.equal(test.nodes.get('#upd-operation').attributes.class, 'alert success');
  recoveryAvailable(test, false);
  test = await scenario([{ ...done, schema_version: 2, operation: 'rollback', confirmed_operation: 'rollback' }]);
  assert.equal(test.reloads(), 1);
  for (const mismatch of [
    { current_version: '1.0.0' }, { version: '1.0.0' }, { expected_version: '3.0.0' },
    { confirmed_operation_id: undefined }, { operation_id: undefined }, { confirmed_operation: 'rollback' },
    { boot_id: 'old' }, { pid: 1 }, { operation: 'unknown', confirmed_operation: 'unknown' },
  ]) {
    test = await scenario([{ ...done, ...mismatch }, { settings: {} }]);
    assert.equal(test.reloads(), 0);
    assert.match(test.body.innerHTML, /未确认/);
    assert.doesNotMatch(test.body.innerHTML, /已确认完成/);
    recoveryAvailable(test, false);
  }
  for (const http of [401, 500]) {
    test = await scenario([{ stage: 'restarting', active: true, log: ['保留日志'] }, { http }]);
    await test.tick();
    assert.equal(test.reloads(), 0);
    assert.match(test.body.innerHTML, /保留日志/);
    assert.equal(test.nodes.get('#upd-error').attributes.class, 'alert error');
    assert.equal(typeof test.nodes.get('#upd-resume').handlers.click, 'function');
    assert.equal(test.timers.size, 0);
  }

  // 旧接口的 done/restarting，以及新接口归一化的历史终态都不能锁死恢复入口。
  for (const oldStage of ['done', 'restarting']) {
    for (const migrated of [false, true]) {
      const history = { stage: migrated ? 'unverified' : oldStage, active: false, can_rollback: true,
        current_version: 'v1.3.6', target_version: '1.2.0', log: ['历史日志'],
        ...(migrated ? { schema_version: 2, legacy_stage: oldStage, notice: '历史记录缺少完整身份证据' } : {}) };
      test = await scenario([history, { settings: {} }, { state: { operation_id: 'recovery' } },
        { stage: 'restarting', active: true, operation_id: 'recovery' }]);
      assert.equal(test.reloads(), 0);
      assert.equal(test.timers.size, 0);
      assert.match(test.body.innerHTML, /当前运行版本：<strong>v1.3.6/);
      assert.match(test.body.innerHTML, /上次操作状态：历史操作未确认/);
      assert.match(test.body.innerHTML, /本次检查结果/);
      assert.match(test.body.innerHTML, /历史日志/);
      assert.doesNotMatch(test.body.innerHTML, /已确认完成/);
      assert.equal(test.nodes.get('#upd-operation').attributes.class, 'alert warning');
      if (migrated) assert.match(test.body.innerHTML, /历史记录缺少完整身份证据/);
      recoveryAvailable(test);
      await test.nodes.get('#upd-rollback').fire('click');
      await test.flush();
      assert.equal(test.requests[2].url, '/api/system/self-update/rollback');
      assert.equal(test.requests[2].options.method, 'POST');
      assert.equal(test.reloads(), 0);
      test.AD.stopUpdatePanel();
    }
  }
  for (const stage of ['failed', 'error', 'unverified']) {
    test = await scenario([{ stage, active: false, can_rollback: true,
      error: '更新失败原因', log: ['失败日志'] }, { settings: {} },
      {}, { current: 'v1.3.6', latest: 'v1.3.6', update_available: false }]);
    assert.equal(test.nodes.get('#upd-operation').attributes.class, 'alert error');
    assert.match(test.body.innerHTML, /更新失败原因/);
    assert.match(test.body.innerHTML, /失败日志/);
    recoveryAvailable(test);
    await test.nodes.get('#upd-check').fire('click');
    assert.equal(test.requests[3].url, '/api/system/update/check?force=true');
    assert.match(test.nodes.get('#upd-check-result').innerHTML, /本次检查未发现可用更新/);
    assert.equal(test.nodes.get('#upd-operation').attributes.class, 'alert error', '检查结果不能覆盖上次失败状态');
    assert.equal(test.reloads(), 0);
  }
  const backupNotice = '旧备份缺少 complete.json，无法证明完整性，不自动回滚';
  test = await scenario([{ stage: 'unverified', active: false, can_rollback: false,
    notice: '历史记录未确认', backup_notice: backupNotice }, { settings: {} },
    { stage: 'idle', active: false }, { settings: {} }]);
  recoveryAvailable(test, false);
  assert.match(test.nodes.get('#upd-rollback-reason').innerHTML, /complete\.json/);
  assert.equal(test.body.innerHTML.split(backupNotice).length - 1, 1, '备份原因只显示一次');
  assert.doesNotMatch(test.body.innerHTML, /缺少目标版本及操作的完整确认/, '已有历史说明时不重复警告');
  await test.nodes.get('#upd-rollback').fire('click');
  assert.equal(test.requests.length, 2);
  await test.nodes.get('#upd-read-state').fire('click');
  await test.flush();
  assert.equal(test.requests[2].url, '/api/system/self-update/status');
  assert.equal(test.reloads(), 0);

  test = await scenario([]);
  for (let i = 0; i < 45; i++) await test.tick();
  assert.equal(test.timers.size, 0);
  assert.equal(test.reloads(), 0);
  assert.equal(test.requests.length, 40);
  test = await scenario([{ stage: 'idle', active: false, can_rollback: true }, { settings: { update_repo: 'https://example.com/old' } },
    {}, { current: 'v1.0.0', latest: 'v2.0.0', update_available: true },
    { state: { operation_id: 'confirmed-target' } }, { stage: 'restarting', active: true, operation_id: 'confirmed-target' }]);
  const repo = test.nodes.get('#upd-repo');
  assert.equal(repo.attributes.type, 'url');
  assert.equal(repo.value, 'https://example.com/old');
  assert.equal(repo.parent.attributes.class, 'upd-source-row');
  repo.value = 'https://example.com/new';
  await test.nodes.get('#upd-check').fire('click');
  assert.equal(test.requests[2].options.method, 'PUT');
  assert.equal(JSON.parse(test.requests[2].options.body).update_repo, 'https://example.com/new');
  assert.equal(test.requests[3].url, '/api/system/update/check?force=true');
  assert.doesNotMatch(test.nodes.get('#upd-check-result').innerHTML, /vv/);
  await test.nodes.get('#upd-start').fire('click');
  await test.flush();
  assert.equal(JSON.parse(test.requests[4].options.body).target_version, 'v2.0.0');
  test.AD.stopUpdatePanel();

  for (const errorResponse of [{ error: '更新源不可达' }, { http: 500 }]) {
    test = await scenario([{ stage: 'failed', error: '上次失败', active: false }, { settings: {} }, {}, errorResponse]);
    await test.nodes.get('#upd-check').fire('click');
    const result = test.nodes.get('#upd-check-result');
    assert.match(result.innerHTML, /class="alert error"/);
    assert.match(result.innerHTML, /本次检查失败/);
    assert.equal(result.querySelector('#upd-start'), null);
    assert.equal(test.nodes.get('#upd-check').disabled, false);
    assert.match(test.nodes.get('#upd-operation').innerHTML, /上次操作状态：失败/);
    recoveryAvailable(test, false);
  }
  // 重新检查期间清除旧的更新按钮，失败后不得执行上次检查的目标。
  test = await scenario([{ stage: 'idle', active: false }, { settings: {} },
    {}, { current: 'v1', latest: 'v2', update_available: true }, {}, { error: '第二次检查失败' }]);
  await test.nodes.get('#upd-check').fire('click');
  assert.ok(test.nodes.get('#upd-start'));
  await test.nodes.get('#upd-check').fire('click');
  assert.equal(test.nodes.get('#upd-start'), undefined);
  recoveryAvailable(test, false);

  // CSS 静态契约只验证声明；不声称做过真实浏览器响应式布局验证。
  assert.match(css, /\.upd-source-row\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;[^}]*gap:\s*10px;[^}]*width:\s*100%;/);
  assert.match(css, /\.upd-source-row input\s*\{[^}]*flex:\s*1 1 280px;[^}]*width:\s*100%;[^}]*min-width:\s*0;/);
  assert.match(css, /@media \(max-width: 600px\)\s*\{\s*\.upd-source-row\s*\{[^}]*flex-direction:\s*column;/);
  assert.match(css, /\.upd-source-row input, \.upd-source-row button\s*\{[^}]*width:\s*100%;/);
  assert.match(css, /\.upd-logs \.log-view\s*\{[^}]*min-height:\s*0;[^}]*max-height:\s*240px;/);
  assert.match(css, /\.alert\.error\s*\{[^}]*background:\s*var\(--danger-soft\)/);
  assert.match(css, /\.upd-logs \.log-view\s*\{[^}]*scrollbar-width:\s*thin;[^}]*color-scheme:\s*dark;/);

  const logState = count => ({ stage: 'restarting', active: true, log: Array.from({ length: count }, (_, i) => '日志 ' + i) });
  test = await scenario([logState(50), logState(55), logState(60), logState(65), logState(70),
    { ...logState(75), stage: 'failed', active: false }, { settings: {} },
    { ...logState(80), stage: 'failed', active: false }, { settings: {} }]);
  let details = test.nodes.get('#upd-logs');
  let log = test.nodes.get('#upd-log');
  assert.equal(details.open, false, '日志默认紧凑收起');
  assert.equal(log.clientHeight, 0);
  details.open = true;
  await details.fire('toggle');
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight, '首次展开显示最新日志');
  log.scrollTop = 160;
  log.scrollLeft = 12;
  await log.fire('scroll');
  await test.tick();
  assert.notEqual(test.nodes.get('#upd-log'), log, 'mock 确实替换了节点');
  details = test.nodes.get('#upd-logs');
  log = test.nodes.get('#upd-log');
  assert.equal(details.open, true);
  assert.equal(log.scrollTop, 160, '阅读历史时重绘不得跳到底部');
  assert.equal(log.scrollLeft, 12);
  details.open = false;
  await details.fire('toggle');
  await test.tick();
  details = test.nodes.get('#upd-logs');
  log = test.nodes.get('#upd-log');
  assert.equal(details.open, false, '收起状态随重绘保留');
  details.open = true;
  await details.fire('toggle');
  assert.equal(log.scrollTop, 160, '收起期间重绘不能用零尺寸覆盖历史位置');
  log.scrollTop = log.scrollHeight - log.clientHeight - 20;
  await log.fire('scroll');
  await test.tick();
  const oldDetails = details;
  log = test.nodes.get('#upd-log');
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight, '接近底部时跟随新增日志');
  oldDetails.open = false;
  await oldDetails.fire('toggle');
  await test.tick();
  assert.equal(test.nodes.get('#upd-logs').open, true, '旧节点的延迟事件不能修改当前展开状态');
  log = test.nodes.get('#upd-log');
  log.scrollTop = 200;
  await log.fire('scroll');
  await test.tick();
  assert.equal(test.nodes.get('#upd-log').scrollTop, 200, '进入失败终态也保留阅读位置');
  assert.equal(test.nodes.get('#upd-logs').open, true);
  recoveryAvailable(test, false);
  await test.nodes.get('#upd-read-state').fire('click');
  await test.flush();
  assert.equal(test.nodes.get('#upd-log').scrollTop, 200, '手动重新读取仍保留历史阅读位置');
  assert.equal(test.nodes.get('#upd-logs').open, true);

  test = await scenario([{ stage: 'unverified', active: false, notice: '<img src=x>',
    backup_notice: '<script>失败</script>', log: ['<script>日志</script>'] }, { settings: {} }]);
  assert.doesNotMatch(test.body.innerHTML, /<script>|<img/);
  assert.match(test.body.innerHTML, /&lt;script&gt;日志/);
  test = await scenario([{ stage: 'restarting', active: true }]);
  test.AD.stopUpdatePanel();
  assert.equal(test.timers.size, 0);
  console.log('前端回归通过：严格确认与单次刷新、历史未确认和失败恢复入口、检查结果隔离、禁用回滚原因、输入及响应式 CSS 契约、日志展开与滚动位置 mock、401/500、40 次重连上限、离页清理（未进行真实浏览器验证）');
})().catch(error => { console.error(error); process.exitCode = 1; });
