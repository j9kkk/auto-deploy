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
    // 有限样式对象：状态收敛会写入 style.color，缺失会让真实分支提前抛错。
    this.style = {};
    this.scrolled = false;
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
    // 后代选择器：末段匹配自身，前段由某个祖先匹配。
    if (/\s/.test(selector.trim())) {
      const parts = selector.trim().split(/\s+/);
      return this.matches(parts[parts.length - 1])
        && Boolean(this.parent?.closest(parts.slice(0, -1).join(' ')));
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

async function scenario(runs = [run(30), run(20)], taskExtra = {}, options = {}) {
  const task = { id: 1, name: 'Demo_task', repo_url: 'https://example.test/repo.git', repo_branch: 'main',
    enabled: true, schedule_type: 'manual', deploy_method: 'release', ...taskExtra };
  const document = new Element('document'), container = new Element(); document.append(container);
  // 弹窗挂载点：运行详情弹窗的关闭检测依赖它是否为空。
  const modalRoot = new Element('div', { id: 'modal-root' }); document.append(modalRoot);
  document.createElement = tag => new Element(tag);
  document.getElementById = id => document.querySelector('#' + id);
  const timers = new Map(), queues = new Map(), requests = [], unexpected = [], errors = [], successes = [], confirms = [], prompts = [], renders = [];
  let timerId = 0, confirmResult = true, promptResult = null, modal;
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
    else if (method === 'GET' && url.startsWith('/api/runs?') && options.runsView) {
      result = { runs, total: runs.length, statuses: [] };
    } else if (method === 'POST' && url === '/api/settings/schedule/preview') result = { ok: true, description: '固定间隔', next_runs: [] };
    else if (method === 'GET' && /^\/api\/runs\/\d+$/.test(url) && runs.some(r => url.endsWith('/' + r.id))) {
      result = { run: runs.find(r => url.endsWith('/' + r.id)) };
    } else if (method === 'GET' && /^\/api\/runs\/\d+$/.test(url)) {
      // 回滚异步化后前端会打开后台回滚运行的详情；给一个进行中的最小快照。
      result = { run: { id: Number(url.split('/').pop()), task_id: 1, status: 'running', is_active: true,
        trigger: 'rollback', log_tail: [] } };
    } else { unexpected.push(method + ' ' + url); throw new Error('未配置的模拟请求：' + url); }
    if (result instanceof Error) throw result;
    return structuredClone(await result);
  };
  const AD = { state: { defaults: { schedule_defaults: { interval: '1h' } } }, escapeHtml, attr: escapeHtml,
    debounce: fn => fn, setBusy: (button, busy) => { button.disabled = busy; },
    toastError: message => errors.push(message), toastSuccess: message => successes.push(message),
    confirm: async options_ => { confirms.push(options_); return confirmResult; },
    render: async view => { renders.push(view); },
    prompt: async options_ => { prompts.push(options_); return promptResult; },
    api: Object.fromEntries(['get', 'post', 'put', 'del'].map(method => [method,
      (url, body) => request(method === 'del' ? 'DELETE' : method.toUpperCase(), url, body)])),
    Modal: { open: options_ => { modalRoot.innerHTML = ''; modal = new Element(); modalRoot.append(modal);
      modal.innerHTML = options_.body + (options_.footerLeft || '') + options_.footer; return modal;
    }, close: () => { modalRoot.innerHTML = ''; modal = null; }, top: () => modal } };
  for (const key of ['statusLabel', 'formatTime', 'formatRelative', 'formatDuration', 'triggerLabel', 'shortCommit']) AD[key] = v => String(v ?? '');
  // 运行详情弹窗使用 setInterval 轮询与 MutationObserver 感知关闭；这里提供
  // 同契约的有限实现，让测试能驱动真实轮询与关闭清理逻辑。
  const intervals = new Map();
  const observers = [];
  let intervalId = 0;
  class TestMutationObserver {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
    /** 模拟 modal-root 被清空：仅未断开的观察者收到通知。 */
    fire() { if (!this.disconnected) this.callback([]); }
  }
  vm.runInNewContext(source, { AD, window: { AD, addEventListener() {} }, document,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { intervals.set(++intervalId, { fn, delay }); return intervalId; },
    clearInterval: id => intervals.delete(id),
    MutationObserver: TestMutationObserver,
    URLSearchParams,
  }, { filename: 'web/assets/views.js' });
  await AD.views.tasks(container);
  const q = selector => { const node = document.querySelector(selector); assert.ok(node, '找不到节点：' + selector); return node; };
  return { AD, task, document, container, q, timers, requests, errors, successes, confirms, prompts, renders, enqueue,
    confirmWith: value => { confirmResult = value; },
    promptWith: value => { promptResult = value; },
    async click(selector, force = false) { await q(selector).fire('click', force); await flush(); },
    async open(options) { await AD.toggleTaskExpand(1, options); await flush(); },
    async tick() { const due = [...timers]; timers.clear(); due.forEach(([, t]) => { assert.equal(t.delay, 1200); t.fn(); }); await flush(); },
    /** 触发一次运行详情弹窗的 1200ms 轮询。 */
    async intervalTick() {
      const due = [...intervals];
      const pending = due.map(([, t]) => { assert.equal(t.delay, 1200); return t.fn(); });
      await Promise.all(pending);
      await flush();
    },
    /** 触发弹窗打开时 250ms 的首次探测。 */
    async kickoffTick() {
      const due = [...timers];
      timers.clear();
      const pending = due.map(([, t]) => { assert.equal(t.delay, 250); return t.fn(); });
      await Promise.all(pending);
      await flush();
    },
    intervals,
    intervalCount() { return intervals.size; },
    /** 模拟弹窗关闭：modal-root 变空，未断开的观察者收到通知。 */
    closeModal() { AD.Modal.close(); observers.forEach(o => o.fire()); },
    observeCount() { return observers.filter(o => !o.disconnected).length; },
    finish() { AD.stopAllInlineLogs(); intervals.clear(); assert.equal(timers.size, 0); assert.deepEqual(unexpected, []);
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

// 运行结束后状态必须就地收敛：取消按钮消失、徽标与「最近运行」同步更新。
// 回归用户报告的问题：运行结束但界面仍显示「运行中」，取消按钮残留并报
// 「该运行已结束，无法取消」。
async function runFinishChecks() {
  // 内联面板路径：正在观看的运行结束后，行与面板一起收敛。
  const active = run(30, { status: 'running', is_active: true, release_dir: '' });
  const test = await scenario([active, run(20)], { active_run: active }); await test.open();
  assert.equal(test.q('[data-inline-cancel]').dataset.inlineCancel, '30');
  assert.equal(test.q('[data-task-cancel]').dataset.taskCancel, '1');
  test.enqueue('GET', '/api/runs/30/tail?after=1', { total: 2, lines: ['完成'], active: false });
  test.enqueue('GET', '/api/runs/30', { run: run(30, { status: 'success', is_active: false, duration_ms: 900 }) });
  // 终态收敛会就地刷新任务行；用最新任务状态回答这次请求。
  test.enqueue('GET', '/api/tasks', { tasks: [{ ...test.task, active_run: null, last_status: 'success', run_count: 1, success_count: 1, failure_count: 0 }] });
  await test.tick();
  assert.equal(test.timers.size, 0);
  assert.equal(test.document.querySelector('[data-inline-cancel]'), null, '运行结束后取消按钮必须消失');
  assert.equal(test.document.querySelector('[data-task-cancel]'), null, '任务行必须回到「立即运行」');
  assert.ok(test.q('[data-task-run]'));
  assert.ok(test.q('[data-inline-run-status]').querySelector('.badge.success'), '面板状态应变为成功');
  assert.equal(test.renders.length, 0, '终态收敛不得重绘整个视图');
  test.finish();

  // 任务页轮询路径：视图注册的 viewRefresh 只更新行与面板，不重绘。
  const poll = await scenario([active, run(20)], { active_run: active });
  await poll.open();
  assert.equal(poll.timers.size, 1, '活跃运行时继续轮询');
  poll.enqueue('GET', '/api/runs/30/tail?after=1', { total: 1, lines: [], active: false });
  poll.enqueue('GET', '/api/runs/30', { run: run(30, { status: 'success', is_active: false }) });
  poll.enqueue('GET', '/api/tasks', { tasks: [{ ...poll.task, active_run: null, last_status: 'success' }] });
  await poll.tick();
  assert.equal(poll.renders.length, 0, '状态收敛必须就地更新');
  assert.equal(poll.document.querySelector('[data-inline-cancel]'), null);
  assert.equal(poll.document.querySelector('[data-task-cancel]'), null);
  poll.finish();
}

// 「查看日志」不得触发整体刷新：详情弹窗只就地更新，不重建底层视图。
async function runDetailIsolationChecks() {
  const test = await scenario([run(30), run(20)], {}, { runsView: true });
  await test.AD.views.runs(test.container);
  const row = test.q('[data-run-row="30"]');
  assert.equal(row.querySelector('[data-run-actions]').querySelectorAll('button').length, 1,
    '已结束的运行不应有取消按钮');
  const rendersBefore = test.renders.length;
  await test.click('[data-run-log="30"]');
  assert.equal(test.renders.length, rendersBefore, '打开日志详情不得重绘视图');
  assert.ok(test.q('#rd-status'), '弹窗已打开');
  assert.equal(test.document.querySelector('#modal-root [data-cancel-run]'), null,
    '已结束运行的弹窗不得显示取消按钮');
  // 已结束的运行：首次探测即收敛，停止轮询。
  test.enqueue('GET', '/api/runs/30/tail?after=1', { total: 1, lines: [], active: false });
  await test.kickoffTick();
  assert.equal(test.intervalCount(), 0, '已结束的运行不得继续轮询');
  assert.equal(test.renders.length, rendersBefore, '终态收敛不得重绘视图');
  test.closeModal();
  test.finish();

  // 活跃运行的弹窗在结束时移除取消按钮并就地同步列表行。
  const live = run(31, { status: 'running', is_active: true });
  const active = await scenario([live, run(20)], {}, { runsView: true });
  await active.AD.views.runs(active.container);
  const liveRow = active.q('[data-run-row="31"]');
  assert.ok(liveRow.querySelector('[data-run-cancel]'), '活跃运行的列表行应有取消按钮');
  const before = active.renders.length;
  await active.click('[data-run-log="31"]');
  assert.equal(active.renders.length, before, '打开活跃运行的日志不得重绘视图');
  assert.ok(active.document.querySelector('#modal-root [data-cancel-run]'), '活跃运行的弹窗应有取消按钮');
  // 运行结束：弹窗轮询读取终态后移除取消按钮并同步列表行。
  active.enqueue('GET', '/api/runs/31/tail?after=1', { total: 1, lines: [], active: false });
  active.enqueue('GET', '/api/runs/31', { run: run(31, { status: 'success', is_active: false, duration_ms: 42 }) });
  await active.intervalTick();
  assert.equal(active.renders.length, before, '终态收敛不得重绘视图');
  assert.equal(active.document.querySelector('#modal-root [data-cancel-run]'), null,
    '运行结束后弹窗取消按钮必须消失');
  assert.equal(active.document.querySelector('[data-run-row="31"] [data-run-cancel]'), null,
    '运行结束后列表行的取消按钮必须消失');
  assert.ok(active.document.querySelector('[data-run-row="31"] [data-run-status] .badge.success'),
    '列表行状态应就地更新为成功');
  // 运行结束就地停止轮询时，250ms 首次探测一并被清理，不再补发请求。
  assert.equal(active.timers.size, 0, '终态后不得残留首探定时器');
  active.closeModal();
  active.finish();
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
    test.enqueue('POST', '/api/tasks/1/rollback', { ok: true, run_id: 40, message: '回滚已开始' });
    confirmation.resolve(true); await clicking; await flush();
    assert.equal(posts(test).length, 1); assert.equal(posts(test)[0].url, '/api/tasks/1/rollback');
    assert.deepEqual(posts(test)[0].body === undefined ? undefined : JSON.parse(JSON.stringify(posts(test)[0].body)),
      selector === selected ? { run_id: 20 } : undefined);
    assert.deepEqual(test.successes, ['回滚已开始']);
    // 回滚异步化后前端会打开后台回滚运行的详情弹窗（含轮询定时器），先关掉再收尾。
    test.closeModal();
    test.confirmWith(true); test.enqueue('POST', '/api/tasks/1/rollback', new Error('发布目录已清理，不能回滚'));
    await test.click(selector); assert.deepEqual(test.errors, ['发布目录已清理，不能回滚']);
    assert.equal(test.q(selector).disabled, false); test.finish();
  }
  const test = await scenario(); await test.open(); const confirmation = deferred(); test.confirmWith(confirmation.promise);
  const clicking = test.q(selected).fire(); await flush(); await test.click('[data-task-log]');
  confirmation.resolve(true); await clicking; assert.equal(posts(test).length, 0, '确认期间离开已断开的面板不能提交'); test.finish();
}

async function deleteChecks() {
  const del = test => test.requests.filter(r => r.method === 'DELETE');

  // 行内删除走两步确认：先确认意图，再要求输入任务名。
  const test = await scenario(); 
  assert.ok(test.q('[data-task-delete]'), '任务行必须提供删除入口');
  assert.equal(test.q('[data-task-delete]').dataset.taskDelete, '1');

  // 第一步取消：不发请求。
  test.confirmWith(false); test.promptWith('Demo_task');
  await test.click('[data-task-delete]');
  assert.equal(test.confirms.length, 1); assert.equal(test.prompts.length, 0, '首次确认取消后不得进入第二步');
  assert.equal(del(test).length, 0);

  // 第二步输入不匹配：仍然不发请求，并给出中文提示。
  test.confirmWith(true); test.promptWith('demo_task_wrong');
  await test.click('[data-task-delete]');
  assert.equal(test.prompts.length, 1);
  assert.equal(del(test).length, 0, '任务名不匹配不得提交删除');
  assert.ok(test.errors.some(m => /任务名不匹配/.test(m)), '名称不匹配需明确提示：' + JSON.stringify(test.errors));

  // 第二步取消（null）同样不发请求。
  test.promptWith(null); await test.click('[data-task-delete]');
  assert.equal(del(test).length, 0);

  // 正常路径：名称匹配后提交 DELETE，并汇报清理与容器数量。
  test.enqueue('DELETE', '/api/tasks/1', { ok: true, purged: ['a', 'b'], containers: ['c1'], runs_deleted: 3 });
  test.promptWith('Demo_task');
  await test.click('[data-task-delete]');
  const deletes = del(test);
  assert.equal(deletes.length, 1); assert.equal(deletes[0].url, '/api/tasks/1');
  assert.ok(test.successes.some(m => /任务已删除/.test(m) && /2 项/.test(m) && /容器 1 个/.test(m)),
    '需汇报清理数量：' + JSON.stringify(test.successes));
  const confirmOptions = test.confirms.at(-1);
  assert.equal(confirmOptions.danger, true);
  assert.match(confirmOptions.message, /Demo_task/);
  assert.match(confirmOptions.detail, /不可撤销/);
  // 确认文案要说明该部署方式下的真实后果，避免误导。
  assert.match(confirmOptions.detail, /影响范围/);
  assert.equal(test.prompts.at(-1).label.includes('Demo_task'), true);

  // 后端报错（例如运行中）要回显中文错误，不静默失败。
  test.enqueue('DELETE', '/api/tasks/1', new Error('任务正在运行，请先取消后再删除'));
  await test.click('[data-task-delete]');
  assert.ok(test.errors.some(m => /正在运行/.test(m)), JSON.stringify(test.errors));
  test.finish();

  // 运行中的任务：不进入确认流程，直接给出中文提示。
  // active_run 来自任务列表（列表接口据此标记），不是 runs 数组。
  const busy = await scenario([run(30, { status: 'running', is_active: true })],
    { active_run: run(30, { status: 'running', is_active: true }) });
  const busyButton = busy.q('[data-task-delete]');
  assert.match(busyButton.getAttribute('title'), /运行中/);
  busy.confirmWith(true); busy.promptWith('Demo_task');
  await busy.click('[data-task-delete]');
  assert.equal(busy.confirms.length, 0, '运行中任务不得进入删除确认');
  assert.equal(busy.prompts.length, 0);
  assert.equal(del(busy).length, 0);
  assert.ok(busy.errors.some(m => /正在运行.*请先取消/.test(m)), JSON.stringify(busy.errors));
  busy.finish();

  // 表单内的删除按钮复用同一套两步确认。
  const fromForm = await scenario();
  await fromForm.AD.openTaskForm(1); await flush();
  assert.ok(fromForm.q('#form-delete'), '编辑表单需保留删除入口');
  fromForm.confirmWith(true); fromForm.promptWith('Demo_task');
  fromForm.enqueue('DELETE', '/api/tasks/1', { ok: true, purged: [], containers: [], runs_deleted: 0 });
  await fromForm.click('#form-delete');
  assert.equal(del(fromForm).length, 1, '表单删除同样要走两步确认并提交');
  assert.equal(fromForm.prompts.length, 1);
  fromForm.finish();

  // 表单内取消删除时回到编辑表单，不丢失后续编辑入口。
  const cancelled = await scenario();
  await cancelled.AD.openTaskForm(1); await flush();
  cancelled.confirmWith(false); cancelled.promptWith('Demo_task');
  await cancelled.click('#form-delete');
  assert.equal(del(cancelled).length, 0);
  assert.equal(cancelled.confirms.length, 1);
  cancelled.finish();

  // 部署方式不同，后果说明必须随之变化（避免对 systemd/rsync 谎报会停容器）。
  for (const [method, pattern] of [
    ['docker_compose', /compose down/], ['docker', /回滚脚本/],
    ['systemd', /systemctl stop/], ['rsync', /远端/], ['release', /没有需要停止的容器/],
  ]) {
    const each = await scenario([run(30)], { deploy_method: method });
    each.confirmWith(false); each.promptWith('Demo_task');
    await each.click('[data-task-delete]');
    assert.match(each.confirms[0].detail, pattern, method + ' 的后果说明不正确：' + each.confirms[0].detail);
    each.finish();
  }
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
      // 默认方式为自定义脚本，部署脚本必填（前端前置校验会拦截空值）。
      test.q('#f-deploy_script').value = 'echo deploy';
      const method = editing ? 'PUT' : 'POST', url = editing ? '/api/tasks/1' : '/api/tasks';
      test.enqueue(method, url, {}); await test.click('#form-save');
      const request = test.requests.at(-1); assert.equal(request.method, method); assert.equal(request.url, url);
      assert.equal(request.body.name, name); assert.equal(request.body.description, '中文部署备注');
    }
    test.finish();
  }
}

async function taskFormUxChecks() {
  // 切换部署方式保留输入：来回切换不清空已填内容（清空只发生在提交时，
  // 且只清空与当前方式无关的字段）。
  const switched = await scenario();
  await switched.AD.openTaskForm(null); await flush();
  switched.q('#f-deploy_script').value = 'echo keep';
  switched.q('#f-deploy_method').value = 'docker_compose';
  await switched.q('#f-deploy_method').fire('change');
  assert.equal(switched.q('#f-deploy_script').closest('[data-method-field]').classList.contains('hidden'), true,
    '非当前方式的字段应隐藏');
  assert.equal(switched.q('#f-deploy_script').value, 'echo keep', '切换方式不得清空输入');
  switched.q('#f-deploy_method').value = 'script';
  await switched.q('#f-deploy_method').fire('change');
  assert.equal(switched.q('#f-deploy_script').closest('[data-method-field]').classList.contains('hidden'), false);
  assert.equal(switched.q('#f-deploy_script').value, 'echo keep', '切回后输入应恢复');
  switched.finish();

  // 存量互斥字段不再阻止普通编辑：与当前方式无关的值提交时就地清空，
  // 不会触发后端互斥校验，也不会作为脏配置继续保存。
  const legacy = await scenario([run(30)], { deploy_method: 'release', service_name: 'old.service', docker_image: 'legacy:1' });
  await legacy.AD.openTaskForm(1); await flush();
  legacy.enqueue('PUT', '/api/tasks/1', {});
  await legacy.click('#form-save');
  const legacyBody = legacy.requests.at(-1).body;
  assert.equal(legacyBody.service_name, '', 'release 任务不得携带 systemd 字段');
  assert.equal(legacyBody.docker_image, '', 'release 任务不得携带 docker 字段');
  assert.equal(legacyBody.credential_id, null, '未选择凭据时显式提交空绑定');
  legacy.finish();

  // 勾选「清除已保存的令牌」只提交 clear_token 标志：后端已支持无
  // git_token 键的清除，协议不能再依赖“同时提交新令牌”。
  const tokened = await scenario([run(30)], { has_token: true });
  await tokened.AD.openTaskForm(1); await flush();
  assert.ok(tokened.q('#f-clear_token'), '已配置令牌时应显示清除选项');
  assert.equal(tokened.q('#f-clear_token').closest('#auth-token-wrap').classList.contains('hidden'), false,
    '有任务内令牌时认证方式应为「任务内令牌」');
  tokened.q('#f-clear_token').checked = true;
  tokened.enqueue('PUT', '/api/tasks/1', {});
  await tokened.click('#form-save');
  const tokenBody = tokened.requests.at(-1).body;
  assert.equal(tokenBody.clear_token, true);
  assert.equal('git_token' in tokenBody, false, '空令牌不得提交');
  tokened.finish();

  // 凭据列表加载失败时保留绑定：注入占位选项，而不是静默解除。
  const bound = await scenario([run(30)], { credential_id: 7 });
  bound.enqueue('GET', '/api/credentials', new Error('数据库不可用'));
  await bound.AD.openTaskForm(1); await flush();
  assert.equal(bound.q('#f-credential_id').value, '7', '绑定的凭据应通过占位选项保留');
  bound.enqueue('PUT', '/api/tasks/1', {});
  await bound.click('#form-save');
  assert.equal(bound.requests.at(-1).body.credential_id, 7, '列表失败不得静默解绑');
  bound.finish();

  // 仅打包方式：打包路径始终可见（它是该方式的核心字段），发布位置隐藏。
  const artifact = await scenario([run(30)], { deploy_method: 'artifact' });
  await artifact.AD.openTaskForm(1); await flush();
  assert.equal(artifact.q('#f-artifact_paths').closest('.field').classList.contains('hidden'), false,
    '打包路径对仅打包方式必须可见');
  for (const field of artifact.document.querySelectorAll('[data-release-field]')) {
    assert.equal(field.classList.contains('hidden'), true, '仅打包不应显示发布位置字段');
  }
  artifact.finish();

  // 默认配置拉取失败：兜底值与真实返回同构，新建表单不再抛错。
  const fallback = await scenario();
  fallback.AD.state.defaults = null;
  fallback.enqueue('GET', '/api/settings/defaults', new Error('服务不可用'));
  await fallback.AD.openTaskForm(null); await flush();
  assert.equal(fallback.q('#f-schedule_expression').value, '1h');
  assert.equal(fallback.q('#f-git_depth').value, '1');
  fallback.finish();

  // 配置摘要：以自然语言概括当前配置，帮助保存前确认。
  const summarised = await scenario();
  await summarised.AD.openTaskForm(null); await flush();
  const summaryText = summarised.q('#config-summary').textContent;
  assert.match(summaryText, /认证：无需认证/);
  assert.match(summaryText, /部署：自定义脚本/);
  assert.match(summaryText, /触发：固定间隔 1h/);
  summarised.finish();

  // 「创建后立即运行」派发失败时后端返回 warning，必须提示而不是只报成功。
  const warned = await scenario();
  await warned.AD.openTaskForm(null); await flush();
  warned.q('#f-name').value = 'New_task';
  warned.q('#f-repo_url').value = 'https://example.test/repo.git';
  warned.q('#f-deploy_script').value = 'echo hi';
  warned.enqueue('POST', '/api/tasks', { warning: '任务已创建，但未开始部署：调度器已暂停' });
  await warned.click('#form-save');
  assert.ok(warned.errors.some(m => /未开始部署/.test(m)), '创建警告必须提示');
  warned.finish();
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
  await selectionChecks(); await raceChecks(); await pollingChecks(); await runFinishChecks();
  await runDetailIsolationChecks(); await rollbackChecks(); await deleteChecks();
  await formChecks(); await taskFormUxChecks(); styleChecks();
  console.log('任务页 Node 回归通过：真实详情/表单 handler、日志选择与下载、异步会话隔离、增量轮询及清理、运行结束就地收敛（取消按钮消失且不重绘）、查看日志不触发整体刷新、回滚确认与错误、名称原样校验、删除两步确认与后果说明、表单 UX（方式切换保留输入、无关字段提交清空、clear_token 协议、凭据绑定保留、仅打包显隐、默认值兜底、配置摘要、创建警告）及 CSS 契约（有限 DOM 模拟，未进行真实浏览器验收）');
})().catch(error => { console.error(error); process.exitCode = 1; });
