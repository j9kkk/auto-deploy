'use strict';
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../web/assets/views.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../web/assets/style.css'), 'utf8');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[ch]);
const decode = text => text.replace(/&(amp|lt|gt|quot|#39|#10);/g,
  (_, key) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", '#10': '\n' })[key]);

// 有限 DOM：只实现本页需要的选择器、节点替换、表单值和事件冒泡。
// 不模拟浏览器布局、焦点、原生确认框或网络；CSS 仅检查声明契约。
class Element {
  constructor(tag = 'div', attrs = {}) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.handlers = {};
    this.parent = null; this.scrollTop = 0; this.clientHeight = 120;
    this.disabled = Object.hasOwn(attrs, 'disabled');
    this.hidden = Object.hasOwn(attrs, 'hidden');
    this.checked = Object.hasOwn(attrs, 'checked');
    this.dataset = new Proxy({}, { get: (_, key) => this.attrs['data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())],
      set: (_, key, value) => { this.attrs['data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())] = String(value); return true; } });
  }
  get id() { return this.attrs.id; }
  set id(value) { this.attrs.id = value; }
  get className() { return this.attrs.class || ''; }
  set className(value) { this.attrs.class = value; }
  get classList() {
    return { contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !names.has(name) : force;
        if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' '); return add;
      }, remove: name => this.classList.toggle(name, false) };
  }
  get isConnected() { return this.tag === 'document' || Boolean(this.parent?.isConnected); }
  get childElementCount() { return this.children.filter(n => n instanceof Element).length; }
  get firstChild() { return this.children[0]; }
  get scrollHeight() { return this.childElementCount * 20; }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tag === 'select') {
      const options = this.querySelectorAll('option');
      return (options.find(n => Object.hasOwn(n.attrs, 'selected')) || options[0])?.value || '';
    }
    return this.tag === 'textarea' ? this.textContent : (this.attrs.value || '');
  }
  set value(value) { this._value = String(value); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  matches(selector) {
    if (selector.includes(',')) return selector.split(',').some(s => this.matches(s.trim()));
    if (selector.includes(' > ')) {
      const [parent, child] = selector.split(' > ');
      return this.matches(child) && Boolean(this.parent?.matches(parent));
    }
    let rest = selector, ok = true;
    rest = rest.replace(/:nth-child\((\d+)\)/g, (_, n) => {
      ok &&= this.parent?.children.filter(c => c instanceof Element).indexOf(this) === Number(n) - 1; return '';
    });
    rest = rest.replace(/\[([\w-]+)(?:="([^"]*)")?\]/g, (_, key, value) => {
      ok &&= Object.hasOwn(this.attrs, key) && (value === undefined || this.attrs[key] === value); return '';
    });
    rest = rest.replace(/([.#])([\w-]+)/g, (_, kind, value) => {
      ok &&= kind === '#' ? this.id === value : this.classList.contains(value); return '';
    });
    assert.match(rest, /^(?:[\w-]+)?$/, '模拟 DOM 遇到未支持的选择器：' + selector);
    return ok && (!rest || rest === this.tag);
  }
  querySelectorAll(selector) {
    return this.children.filter(n => n instanceof Element).flatMap(n =>
      [...(n.matches(selector) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  async fire(name = 'click', force = false) {
    if (name === 'click' && this.disabled && !force) return;
    const calls = [];
    for (let node = this; node; node = node.parent) {
      for (const fn of node.handlers[name] || []) calls.push(fn({ target: this, currentTarget: node }));
    }
    await Promise.all(calls);
  }
  scrollIntoView() { this.scrolled = true; }
  append(node) { node.parent = this; this.children.push(node); }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parent = null; }
  remove() { this.parent?.removeChild(this); }
  after(node) { node.parent = this.parent; this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, node); }
  get textContent() { return this.children.map(n => typeof n === 'string' ? decode(n) : n.textContent).join(''); }
  set textContent(text) { this.innerHTML = escapeHtml(text); }
  get innerHTML() { return this.children.map(n => typeof n === 'string' ? n : n.outerHTML).join(''); }
  get outerHTML() { return `<${this.tag}${Object.entries(this.attrs).map(([k, v]) => ` ${k}="${escapeHtml(v)}"`).join('')}>${this.innerHTML}</${this.tag}>`; }
  set outerHTML(html) {
    const fragment = new Element(); fragment.innerHTML = html;
    const parent = this.parent, index = parent.children.indexOf(this);
    parent.children.splice(index, 1, ...fragment.children);
    fragment.children.forEach(n => { if (n instanceof Element) n.parent = parent; }); this.parent = null;
  }
  set innerHTML(html) {
    this.children.forEach(n => { if (n instanceof Element) n.parent = null; });
    this.children = []; this.insertAdjacentHTML('beforeend', html);
  }
  insertAdjacentHTML(position, html) {
    assert.equal(position, 'beforeend');
    const stack = [this];
    for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith('<!--')) continue;
      if (token.startsWith('</')) { assert.equal(stack.pop().tag, token.slice(2, -1)); continue; }
      const parent = stack[stack.length - 1];
      if (!token.startsWith('<')) { parent.children.push(token); continue; }
      const [, tag, raw] = token.match(/^<([\w-]+)([^>]*)>$/), attrs = {};
      for (const m of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = decode(m[2] || '');
      const node = new Element(tag, attrs); parent.append(node);
      if (!['input', 'br', 'hr'].includes(tag) && !token.endsWith('/>')) stack.push(node);
    }
    assert.equal(stack.length, 1, '模拟 DOM 必须完整解析实际模板');
  }
}
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const run = (id, extra = {}) => ({ id, status: 'success', release_dir: `/releases/${id}`,
  commit_after: `abcdef${id}`, log_tail: [`日志 ${id}`], is_active: false, ...extra });

async function scenario(runs = [run(30), run(20)], taskExtra = {}) {
  const task = { id: 1, name: 'Demo_task', repo_url: 'https://example.test/repo.git', repo_branch: 'main',
    enabled: true, schedule_type: 'manual', deploy_method: 'release', ...taskExtra };
  const document = new Element('document'), container = new Element(); document.append(container);
  document.createElement = tag => new Element(tag);
  document.getElementById = id => document.querySelector('#' + id);
  const timers = new Map(), queues = new Map(), requests = [], unexpected = [], errors = [], successes = [], confirms = [];
  let timerId = 0, confirmResult = true, modal;
  const enqueue = (method, url, result) => {
    const key = method + ' ' + url; if (!queues.has(key)) queues.set(key, []); queues.get(key).push(result);
  };
  const request = async (method, url, body) => {
    requests.push({ method, url, body });
    const queue = queues.get(method + ' ' + url);
    let result;
    if (queue?.length) result = queue.shift();
    else if (method === 'GET' && url === '/api/tasks') result = { tasks: [task] };
    else if (method === 'GET' && url === '/api/tasks/1') result = { task, runs };
    else if (method === 'GET' && url === '/api/credentials') result = { credentials: [] };
    else if (method === 'POST' && url === '/api/settings/schedule/preview') result = { ok: true, description: '固定间隔', next_runs: [] };
    else if (method === 'GET' && /^\/api\/runs\/\d+$/.test(url) && runs.some(r => url.endsWith('/' + r.id))) {
      result = { run: runs.find(r => url.endsWith('/' + r.id)) };
    } else { unexpected.push(method + ' ' + url); throw new Error('未配置的模拟请求：' + url); }
    if (result instanceof Error) throw result;
    return structuredClone(await result);
  };
  const AD = { state: { defaults: { schedule_defaults: { interval: '1h' } } }, escapeHtml, attr: escapeHtml,
    debounce: fn => fn, setBusy: (button, busy) => { button.disabled = busy; },
    toastError: message => errors.push(message), toastSuccess: message => successes.push(message),
    confirm: async options => { confirms.push(options); return confirmResult; }, render: async () => {},
    api: Object.fromEntries(['get', 'post', 'put'].map(method => [method, (url, body) => request(method.toUpperCase(), url, body)])),
    Modal: { open: options => { modal?.remove(); modal = new Element(); document.append(modal);
      modal.innerHTML = options.body + (options.footerLeft || '') + options.footer; return modal;
    }, close: () => modal.remove() } };
  for (const key of ['statusLabel', 'formatTime', 'formatRelative', 'formatDuration', 'triggerLabel', 'shortCommit']) AD[key] = v => String(v ?? '');
  vm.runInNewContext(source, { AD, window: { AD, addEventListener() {} }, document,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval: () => assert.fail('行内日志不得使用 setInterval'),
  }, { filename: 'web/assets/views.js' });
  await AD.views.tasks(container);
  const q = selector => { const node = document.querySelector(selector); assert.ok(node, '找不到节点：' + selector); return node; };
  return { AD, task, document, container, q, timers, requests, errors, successes, confirms, enqueue,
    confirmWith: value => { confirmResult = value; },
    async click(selector, force = false) { await q(selector).fire('click', force); await flush(); },
    async open(options) { await AD.toggleTaskExpand(1, options); await flush(); },
    async tick() { const due = [...timers]; timers.clear(); due.forEach(([, t]) => { assert.equal(t.delay, 1200); t.fn(); }); await flush(); },
    finish() { AD.stopAllInlineLogs(); assert.equal(timers.size, 0); assert.deepEqual(unexpected, []);
      for (const [key, queue] of queues) assert.equal(queue.length, 0, '未消费预期请求：' + key); },
  };
}
const detail = id => `[data-inline-run-log="${id}"]`;
const selected = '[data-inline-rollback-selected]', previous = '[data-inline-rollback]';
const log = '[data-inline-log]', meta = '[data-inline-logmeta]', download = '[data-inline-download]';
const posts = test => test.requests.filter(r => r.method === 'POST');
function showing(test, id, text) {
  assert.equal(test.AD.state.inlineRunSelection[1], id);
  assert.match(test.q(meta).textContent, new RegExp(`运行 #${id} · 已读取`));
  assert.ok(test.q(log).textContent.includes(text));
  assert.equal(test.q(download).href, `/api/runs/${id}/log?download=true`);
  assert.equal(test.q(download).hidden, false);
  for (const button of test.document.querySelectorAll('[data-inline-run-log]')) {
    const current = Number(button.dataset.inlineRunLog) === id;
    assert.equal(button.getAttribute('aria-pressed'), String(current));
    assert.equal(button.classList.contains('active'), current);
  }
}

async function selectionChecks() {
  const test = await scenario();
  const controls = test.q('.table-actions').querySelectorAll('button');
  assert.equal(controls[0].dataset.taskLog, '1'); assert.equal(controls[1].dataset.taskRun, '1');
  const name = test.q('.task-name-link'); assert.equal(name.tag, 'button'); assert.equal(name.getAttribute('type'), 'button');
  // 点击真实委托处理器，而不是直接调用内部 selectRun。
  await test.click('[data-task-log]'); showing(test, 30, '日志 30');
  assert.ok(test.requests.some(r => r.url === '/api/tasks/1'));
  assert.ok(test.requests.some(r => r.url === '/api/runs/30'));
  await test.click(detail(20)); showing(test, 20, '日志 20'); assert.equal(test.q(log).scrolled, true);
  assert.equal(test.q(selected).disabled, false);
  assert.match(test.q('[data-inline-rollback-reason]').textContent, /运行 #20/);
  await test.open({ forceOpen: true }); showing(test, 20, '日志 20');
  await test.open({ forceOpen: true, runId: 30 }); showing(test, 30, '日志 30');
  await test.open({ forceOpen: true, runId: 999 }); showing(test, 30, '日志 30');
  await test.click('[data-task-log]'); assert.equal(test.document.querySelector('.expand-row'), null);
  assert.equal(test.AD.state.inlineRunSelection[1], undefined);
  assert.equal(test.q('[data-task-log]').getAttribute('aria-expanded'), 'false');
  // 名称按钮实际打开表单；没有替换 openTaskForm 或 collectForm。
  await name.fire(); await flush(); assert.equal(test.q('#f-name').value, 'Demo_task');
  test.finish();

  const empty = await scenario([]); await empty.open();
  assert.match(empty.q(log).textContent, /没有运行记录/); assert.equal(empty.q(download).hidden, true);
  assert.equal(empty.q(selected).disabled, true); assert.equal(empty.timers.size, 0); empty.finish();
  const retry = await scenario([run(30, { log_tail: [] })]);
  retry.enqueue('GET', '/api/tasks/1', new Error('任务加载失败')); await retry.open();
  assert.deepEqual(retry.errors, ['任务加载失败']); assert.equal(retry.document.querySelector('.expand-row'), null);
  retry.enqueue('GET', '/api/runs/30', new Error('日志网络失败')); await retry.open();
  assert.match(retry.q(meta).textContent, /日志网络失败.*重试/); assert.equal(retry.timers.size, 0);
  await retry.click(detail(30)); showing(retry, 30, '暂无日志输出');
  assert.match(retry.q(meta).textContent, /已读取 0 行/); retry.finish();
}

async function raceChecks() {
  // 同一 run 再次选中仍必须拥有新会话，不能只按 run_id 防旧响应。
  for (const rejectOld of [false, true]) {
    const test = await scenario(), old = deferred();
    test.enqueue('GET', '/api/runs/30', old.promise); await test.open();
    await test.click(detail(20)); showing(test, 20, '日志 20');
    await test.click(detail(30)); showing(test, 30, '日志 30');
    const before = test.q('.task-expand').innerHTML;
    if (rejectOld) old.reject(new Error('过期失败')); else old.resolve({ run: run(30, { log_tail: ['过期日志'], is_active: true, status: 'failed' }) });
    await flush(); assert.equal(test.q('.task-expand').innerHTML, before);
    assert.equal(test.q(selected).disabled, false); assert.equal(test.timers.size, 0); test.finish();
  }
  // 首次详情及增量请求 pending 时，切换/收起/stopAll 都使旧结果失效。
  for (const phase of ['initial', 'tail', 'terminal']) for (const action of ['switch', 'collapse', 'stopAll']) {
    const test = await scenario([run(30, { status: 'running', is_active: true, release_dir: '' }), run(20)]);
    const pending = deferred();
    if (phase === 'initial') test.enqueue('GET', '/api/runs/30', pending.promise);
    await test.open();
    if (phase !== 'initial') {
      test.enqueue('GET', '/api/runs/30/tail?after=1', phase === 'tail' ? pending.promise : { total: 2, lines: ['完成'], active: false });
      if (phase === 'terminal') test.enqueue('GET', '/api/runs/30', pending.promise);
      await test.tick(); await test.tick(); assert.equal(test.timers.size, 0, 'pending 请求不得重复计时');
    }
    const oldRoot = test.q('.task-expand');
    if (action === 'switch') await test.click(detail(20));
    else if (action === 'collapse') await test.click('[data-task-log]');
    else test.AD.stopAllInlineLogs();
    const before = oldRoot.innerHTML, requestCount = test.requests.length;
    pending.resolve(phase === 'tail' ? { total: 9, lines: ['过期增量'], active: true }
      : { run: run(30, { log_tail: ['过期详情'], is_active: true }) });
    await flush();
    assert.equal(oldRoot.innerHTML, before, `${phase}/${action} 不得覆盖节点`);
    assert.equal(test.requests.length, requestCount, '过期终态不得触发行刷新');
    assert.equal(test.timers.size, 0); assert.equal(Object.keys(test.AD.state.inlineLogTimers).length, 0);
    if (action === 'switch') showing(test, 20, '日志 20'); test.finish();
  }
  for (const action of ['switch', 'collapse', 'stopAll']) {
    const test = await scenario([run(30, { is_active: true }), run(20)]); await test.open();
    assert.equal(test.timers.size, 1);
    if (action === 'switch') await test.click(detail(20));
    else if (action === 'collapse') await test.click('[data-task-log]');
    else test.AD.stopAllInlineLogs();
    assert.equal(test.timers.size, 0); const count = test.requests.length;
    await test.tick(); assert.equal(test.requests.length, count); test.finish();
  }
  // 任务详情本身延迟返回时，已收起/重新展开的 placeholder 不能复活。
  const test = await scenario(), old = deferred(); test.enqueue('GET', '/api/tasks/1', old.promise);
  const opening = test.AD.toggleTaskExpand(1); await flush(); await test.click('[data-task-log]');
  await test.open(); const before = test.q('.task-expand').innerHTML;
  old.resolve({ task: test.task, runs: [run(99)] }); await opening; await flush();
  assert.equal(test.q('.task-expand').innerHTML, before); test.finish();
}

async function pollingChecks() {
  const active = run(30, { status: 'running', is_active: true, release_dir: '' });
  const test = await scenario([active, run(20)], { active_run: active }); await test.open();
  assert.equal(test.timers.size, 1); assert.equal(test.q(selected).disabled, true);
  const pending = deferred(); test.enqueue('GET', '/api/runs/30/tail?after=1', pending.promise);
  await test.tick(); const count = test.requests.length; await test.tick();
  assert.equal(test.requests.length, count); assert.equal(test.timers.size, 0);
  pending.resolve({ total: 2, lines: ['新增日志'], active: true }); await flush();
  showing(test, 30, '日志 30新增日志'); assert.equal(test.timers.size, 1);
  test.enqueue('GET', '/api/runs/30/tail?after=2', { total: 1, lines: ['重置日志'], reset: true, active: true });
  await test.tick(); showing(test, 30, '重置日志'); assert.doesNotMatch(test.q(log).textContent, /新增日志/);
  assert.equal(test.timers.size, 1);
  const scrollButton = test.q('[data-task-log]'), scrollIcon = scrollButton.innerHTML;
  test.task.active_run = null; test.task.last_status = 'success';
  test.enqueue('GET', '/api/runs/30/tail?after=1', { total: 2, lines: ['最终日志'], active: false });
  test.enqueue('GET', '/api/runs/30', { run: run(30) }); await test.tick();
  assert.equal(test.timers.size, 0); assert.equal(test.q(selected).disabled, false);
  assert.equal(test.q(previous).disabled, false); assert.equal(test.q('[data-task-log]'), scrollButton);
  assert.equal(scrollButton.innerHTML, scrollIcon); assert.equal(scrollButton.getAttribute('aria-expanded'), 'true');
  const buttons = test.q('.table-actions').querySelectorAll('button');
  assert.equal(buttons[0], scrollButton); assert.equal(buttons[1].dataset.taskRun, '1');
  assert.equal(test.document.querySelector('[data-task-cancel]'), null); test.finish();

  const retry = await scenario([run(30, { is_active: true })]); await retry.open();
  retry.enqueue('GET', '/api/runs/30/tail?after=1', new Error('增量读取失败')); await retry.tick();
  assert.match(retry.q(log).textContent, /日志 30/); assert.match(retry.q(meta).textContent, /增量读取失败/);
  assert.equal(retry.timers.size, 0); await retry.click(detail(30)); assert.equal(retry.timers.size, 1);
  retry.AD.stopAllInlineLogs(); assert.equal(retry.timers.size, 0); retry.finish();
}

async function rollbackChecks() {
  for (const extra of [{ status: 'failed' }, { release_dir: '' }]) {
    const test = await scenario([run(30, extra), run(20)]); await test.open();
    assert.equal(test.q(selected).disabled, true); await test.click(selected, true);
    assert.equal(test.confirms.length, 0); assert.equal(posts(test).length, 0);
    await test.click(detail(20)); assert.equal(test.q(selected).disabled, false); test.finish();
  }
  for (const status of ['running', 'queued']) {
    const test = await scenario([run(30), run(20, { status, is_active: true })]); await test.open();
    showing(test, 20, '日志 20'); await test.click(detail(30));
    for (const selector of [selected, previous]) {
      assert.equal(test.q(selector).disabled, true); await test.click(selector, true);
    }
    assert.match(test.q('[data-inline-rollback-reason]').textContent, /正在运行或排队/);
    assert.equal(test.confirms.length, 0); assert.equal(posts(test).length, 0); test.finish();
  }
  for (const selector of [selected, previous]) {
    const test = await scenario(); await test.open(); await test.click(detail(20));
    test.confirmWith(false); await test.click(selector);
    assert.equal(posts(test).length, 0); assert.equal(test.q(selector).disabled, false);
    const confirmation = deferred(); test.confirmWith(confirmation.promise);
    const clicking = test.q(selector).fire(); await flush();
    assert.equal(test.q(selected).disabled, true); assert.equal(test.q(previous).disabled, true);
    await test.click(selector, true); assert.equal(test.confirms.length, 2, '确认期间重复点击不能再次确认');
    await test.click(detail(30)); assert.equal(posts(test).length, 0);
    const prompt = test.confirms[1];
    assert.match(prompt.title, selector === selected ? /运行 #20/ : /上一版本/);
    for (const text of ['当前配置', '回滚脚本', 'systemd', 'Docker / Compose', 'rsync', '数据库不会自动恢复']) assert.ok(prompt.detail.includes(text));
    assert.equal(prompt.danger, true);
    test.enqueue('POST', '/api/tasks/1/rollback', { message: '回滚已完成' });
    confirmation.resolve(true); await clicking; await flush();
    assert.equal(posts(test).length, 1); assert.equal(posts(test)[0].url, '/api/tasks/1/rollback');
    assert.deepEqual(posts(test)[0].body === undefined ? undefined : JSON.parse(JSON.stringify(posts(test)[0].body)),
      selector === selected ? { run_id: 20 } : undefined);
    assert.deepEqual(test.successes, ['回滚已完成']);
    test.confirmWith(true); test.enqueue('POST', '/api/tasks/1/rollback', new Error('发布目录已清理，不能回滚'));
    await test.click(selector); assert.deepEqual(test.errors, ['发布目录已清理，不能回滚']);
    assert.equal(test.q(selector).disabled, false); test.finish();
  }
  const test = await scenario(); await test.open(); const confirmation = deferred(); test.confirmWith(confirmation.promise);
  const clicking = test.q(selected).fire(); await flush(); await test.click('[data-task-log]');
  confirmation.resolve(true); await clicking; assert.equal(posts(test).length, 0, '确认期间离开已断开的面板不能提交'); test.finish();
}

async function formChecks() {
  // 直接构建真实 openTaskForm 并点击 form-save；不提取或复制校验算法。
  const invalid = ['', '中文', 'task1', 'task-name', ' task', 'task ', 'task\n', 'a\tb', 'A'.repeat(81)];
  for (const editing of [false, true]) {
    const test = await scenario([], { name: '旧名称-1 ' });
    await test.AD.openTaskForm(editing ? 1 : null); await flush();
    test.q('#f-repo_url').value = 'https://example.test/repo.git'; test.q('#f-description').value = '中文部署备注';
    if (editing) assert.match(test.q('#task-name-hint').textContent, /旧版名称.*原样保留/);
    for (const name of invalid) {
      test.q('#f-name').value = name; const count = test.requests.length;
      await test.click('#form-save'); assert.equal(test.requests.length, count, '非法名称不得提交：' + JSON.stringify(name));
      assert.equal(test.q('#f-name').value, name, '输入不得自动 trim');
      assert.match(test.q('#form-error').textContent, /英文字母或下划线.*中文说明请填写备注/);
      assert.equal(test.q('#form-error').classList.contains('hidden'), false);
    }
    const valid = ['_', 'Abc_task', 'A'.repeat(80), ...(editing ? ['旧名称-1 '] : [])];
    for (const name of valid) {
      // 保存成功会关闭节点；每次重新打开真实表单。
      await test.AD.openTaskForm(editing ? 1 : null); await flush();
      test.q('#f-name').value = name; test.q('#f-description').value = '中文部署备注';
      test.q('#f-repo_url').value = 'https://example.test/repo.git';
      const method = editing ? 'PUT' : 'POST', url = editing ? '/api/tasks/1' : '/api/tasks';
      test.enqueue(method, url, {}); await test.click('#form-save');
      const request = test.requests.at(-1); assert.equal(request.method, method); assert.equal(request.url, url);
      assert.equal(request.body.name, name); assert.equal(request.body.description, '中文部署备注');
    }
    test.finish();
  }
}

function styleChecks() {
  const rule = selector => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = css.match(new RegExp('(?:^|\\n)' + escaped + '\\s*\\{([^}]+)\\}'));
    assert.ok(match, '缺少样式规则：' + selector); return match[1];
  };
  for (const selector of ['.icon-btn svg', '.task-expand .expand-toolbar button svg']) {
    const text = rule(selector); assert.match(text, /width:\s*15px;/); assert.match(text, /height:\s*15px;/);
    assert.match(text, /display:\s*block;/);
  }
  assert.match(rule('.task-expand .expand-toolbar button svg'), /flex:\s*0 0 15px;/);
  assert.match(rule('.icon-btn'), /justify-content:\s*center;/);
  const toolbar = rule('.task-expand .expand-toolbar button');
  for (const declaration of ['display:\\s*inline-flex;', 'align-items:\\s*center;', 'justify-content:\\s*center;']) assert.match(toolbar, new RegExp(declaration));
  assert.match(rule('button, .btn'), /align-items:\s*center;/);
  const name = rule('.task-name-link'); assert.match(name, /font-size:\s*15px;/);
  assert.match(name, /font-weight:\s*700;/); assert.match(name, /color:\s*var\(--accent-hover\);/);
  assert.match(css, /--accent-hover:\s*#6ba1ff;/);
}

(async () => {
  await selectionChecks(); await raceChecks(); await pollingChecks(); await rollbackChecks(); await formChecks(); styleChecks();
  console.log('任务页 Node 回归通过：真实详情/表单 handler、日志选择与下载、异步会话隔离、增量轮询及清理、回滚确认与错误、名称原样校验及 CSS 契约（有限 DOM 模拟，未进行真实浏览器验收）');
})().catch(error => { console.error(error); process.exitCode = 1; });
