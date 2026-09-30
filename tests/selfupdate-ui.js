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

// 只模拟此卡片需要的 DOM：支持后代选择器、节点替换、data-* 与属性查询。
// 尺寸为固定模型，不代表真实浏览器布局、原生 scroll 事件调度或响应式验证。
function mockDom() {
  // 全局按 id 索引：节点被替换/移除后要能从索引里消失，因此 remove/forget 都维护它。
  const byId = new Map();
  class Element {
    constructor(tag = 'div', attributes = {}) {
      this.tag = tag;
      this.attributes = attributes;
      this.children = [];
      this.handlers = {};
      this.value = '';
      this.textContent = '';
      this.scrollTop = 0;
      this.scrollLeft = 0;
      this.disabled = Object.hasOwn(attributes, 'disabled');
      this._parent = null;
      if (attributes.id) byId.set(attributes.id, this);
    }
    get owner() { let node = this; while (node._parent) node = node._parent; return node; }
    get parent() { return this._parent; }
    get id() { return this.attributes.id; }
    get className() { return this.attributes.class || ''; }
    get classList() { return { contains: name => this.className.split(/\s+/).includes(name) }; }
    get dataset() {
      const out = {};
      for (const [key, value] of Object.entries(this.attributes)) {
        if (key.startsWith('data-')) out[key.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = value;
      }
      return out;
    }
    get isConnected() { return this.tag === 'document' || Boolean(this._parent); }
    get id_() { return undefined; }
    get clientHeight() { return this.attributes.id === 'upd-log' && this._visible() ? 120 : 0; }
    get scrollHeight() { return this.clientHeight ? Math.max(120, this.innerHTML.split('\n').length * 20) : 0; }
    _visible() { return true; }
    addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
    async fire(name) {
      if (name === 'click' && this.disabled) return;
      for (const fn of this.handlers[name] || []) await fn({ target: this, currentTarget: this });
    }
    matches(selector) {
      // 支持 "a, b"、"祖先 后代" 与 "#id"/".class"/"[attr]"/"[attr=\"v\"]"/tag。
      if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
      const parts = selector.trim().split(/\s+/);
      if (parts.length > 1) {
        return this.matches(parts[parts.length - 1])
          && Boolean(this._parent?.closest(parts.slice(0, -1).join(' ')));
      }
      let rest = selector.trim(), ok = true;
      rest = rest.replace(/\[([\w-]+)(?:="([^"]*)")?\]/g, (_, key, value) => {
        ok &&= Object.hasOwn(this.attributes, key)
          && (value === undefined || this.attributes[key] === value);
        return '';
      });
      rest = rest.replace(/([.#])([\w-]+)/g, (_, kind, value) => {
        ok &&= kind === '#' ? this.attributes.id === value : this.classList.contains(value);
        return '';
      });
      return ok && (!rest || rest === this.tag);
    }
    querySelectorAll(selector) {
      return this.children.flatMap(child =>
        typeof child === 'string' ? [] : [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this._parent?.closest(selector) || null; }
    append(node) { node._parent = this; this.children.push(node); }
    remove() {
      this.forget();
      this._parent.children = this._parent.children.filter(child => child !== this);
      this._parent = null;
    }
    forget() {
      for (const child of this.children) if (typeof child !== 'string') child.forget();
      if (this.attributes.id) byId.delete(this.attributes.id);
    }
    get innerHTML() {
      return this.children.map(child => typeof child === 'string' ? child : child.outerHTML).join('');
    }
    get outerHTML() {
      const attrs = Object.entries(this.attributes).map(([key, value]) => ' ' + key + '="' + value + '"').join('');
      const voidTag = ['input', 'br', 'hr'].includes(this.tag);
      return '<' + this.tag + attrs + '>' + this.innerHTML + (voidTag ? '' : '</' + this.tag + '>');
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
        // 属性里的 ">" 不会出现在这里：内联 SVG 的 d 属性只用字母、数字和标点。
        for (const match of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attributes[match[1]] = match[2] || '';
        const element = new Element(tag, attributes);
        element._parent = parent;
        parent.children.push(element);
        // 自闭合标签（如内联 SVG 的 <path .../>）不入栈，否则后续的 </svg> 会错配。
        const selfClosing = /\/\s*$/.test(raw);
        if (!['input', 'br', 'hr'].includes(tag) && !selfClosing) stack.push(element);
      }
      assert.equal(stack.length, 1, '模拟 DOM 必须完整解析实际模板');
    }
  }
  const document = new Element('document');
  return { document, Element, byId };
}

async function scenario(responses) {
  const timers = new Map();
  const intervals = new Map();
  const allDelays = [];
  // 用副本驱动，这样测试能在同一会话内继续追加响应（轮询会反复读取）。
  const newResponses = [...responses];
  let id = 0, reloads = 0;
  const { document, Element, byId } = mockDom();
  document.createElement = (tag, attributes) => new Element(tag, attributes);
  const modalRoot = document.createElement('div', { id: 'modal-root' });
  document.append(modalRoot);
  document.getElementById = (key) => byId.get(key) || null;
  const container = document.createElement('div');
  document.append(container);
  // 面板必须挂进 document，否则 isConnected 为假，更新卡片会认为视图已被替换。
  const panel = document.createElement('div');
  container.append(panel);
  const body = document.createElement('div', { id: 'upd-body' });
  panel.append(body);
  panel.querySelector = selector => body.querySelector(selector)
    || (selector === '#upd-body' ? body : null);

  const AD = { state: {}, escapeHtml, attr: escapeHtml, setBusy(button, busy) { button.disabled = Boolean(busy); } };
  AD.ICONS = { upgrade: '<svg id="upgrade-icon"></svg>' };
  const confirms = [];
  AD.confirm = async (options) => { confirms.push(options); return true; };
  let modal = null;
  const modalOptions = [];
  AD.Modal = {
    open(options) {
      modalOptions.push(options);
      modalRoot.innerHTML = '';
      modal = new Element('div', { class: 'modal' });
      // 真实 Modal 把 title 渲染在 modal-head、footer 渲染在 modal-foot；
      // 这里把 body 与 footer 都解析进同一节点，以便按选择器定位按钮。
      if (options.title) modal.attributes['data-title'] = options.title;
      modal.innerHTML = (options.body || '')
        + (options.footer === null ? '' : (options.footer || '<button data-close>关闭</button>'));
      modalRoot.append(modal);
      options.onMount?.(modalRoot);
      AD.Modal.element = modal;
      return modal;
    },
    close() { modalRoot.innerHTML = ''; modal = null; AD.Modal.element = null; },
  };
  const requests = [];
  const context = { AD, window: { AD, addEventListener() {} }, document, AbortController,
    // 记录所有申请过的超时预算：请求完成会 clearTimeout，事后无法再看，但
    // 「abort 预算是否覆盖后端最坏情况」正是要验证的契约。
    setTimeout(fn, delay) { allDelays.push(delay); timers.set(++id, { fn, delay }); return id; },
    clearTimeout(number) { timers.delete(number); },
    setInterval(fn, delay) { intervals.set(++id, { fn, delay }); return id; },
    clearInterval(number) { intervals.delete(number); },
    location: { reload() { reloads++; } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const next = newResponses.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error('网络不可达');
      return { ok: !next.http, status: next.http || 200, json: async () => next };
    } };
  const flush = async () => { for (let n = 0; n < 30; n++) await Promise.resolve(); };
  vm.runInNewContext(source, context);
  AD.renderUpdatePanel(panel);
  await flush();
  const at = selector => panel.querySelector(selector);
  return { AD, panel, body, document, root: modalRoot, requests, confirms, timers, intervals,
    flush, reloads: () => reloads, at, modal: () => AD.Modal.element, modalOptions, allDelays,
    /** 追加一个响应：让同一会话内的后续轮询拿到新数据。 */
    push(response) { newResponses.push(response); },
    async tick() {
      const due = [...timers.values()];
      timers.clear();
      for (const { fn } of due) await fn();
      await flush();
    },
    async click(selector) { await at(selector).fire('click'); await flush(); },
  };
}

const idle = { stage: 'idle', active: false, can_rollback: false, current_version: 'v1.4.0', log: [] };
const settings = { settings: { update_repo: 'https://github.com/j9kkk/auto-deploy.git' }, proxy_description: '未启用' };
const noUpdate = { current: 'v1.4.0', latest: 'v1.4.0', update_available: false, error: '' };
const hasUpdate = { current: 'v1.4.0', latest: 'v1.5.0', update_available: true, error: '' };

(async () => {
  // --- 打开即检查：读设置（确认弹窗的更新源说明用）、读状态（判断是否已有操作在进行）、
  //     再自动检查一次。
  let test = await scenario([settings, idle, noUpdate]);
  assert.deepEqual(test.requests.map(r => r.url),
    ['/api/settings', '/api/system/self-update/status', '/api/system/update/check'],
    '打开卡片应先读设置与状态，并自动检查一次（不带 force）');
  const rowHtml = test.at('#upd-row').innerHTML;
  assert.match(rowHtml, /当前版本 <strong class="mono">v1\.4\.0/);
  assert.match(rowHtml, /当前已是最新版本/, '已是最新时给出明确提示');
  // 「已是最新」时整行只能出现当前版本一个版本号；不能顺带显示最新版本号。
  const shownVersions = rowHtml.match(/v\d+(?:\.\d+)+/g) || [];
  assert.deepEqual(shownVersions, ['v1.4.0'], '已是最新时只显示当前版本号');
  assert.doesNotMatch(rowHtml, /最新版本 <strong/);
  assert.equal(test.at('#upd-upgrade'), null, '已是最新时没有升级图标');
  assert.ok(test.at('#upd-history') && test.at('#upd-config'), '升级日志与配置始终可见');

  // --- 有更新：显示最新版本号 + 升级图标，且不显示「已是最新」。
  test = await scenario([settings, idle, hasUpdate]);
  assert.match(test.at('#upd-row').innerHTML, /最新版本 <strong class="mono">v1\.5\.0/);
  assert.doesNotMatch(test.at('#upd-row').innerHTML, /当前已是最新版本/);
  // 后端一次检查最坏 = Releases API(15s) + git ls-remote(30s)，前端的 abort 预算
  // 必须大于这个上界，否则界面会在后端还没返回时就报「检查失败」。
  assert.ok(test.allDelays.some(d => d >= 60000),
    '检查请求的超时预算必须覆盖后端最坏情况（≥60s），实际：' + JSON.stringify(test.allDelays));
  const icon = test.at('#upd-upgrade');
  assert.ok(icon, '有新版本时必须有升级图标');
  assert.equal(icon.disabled, false);
  assert.equal(icon.attributes.title, '升级到 v1.5.0');

  // --- 升级：二次确认展示更新源与代理，确认后才发起，并进入轮询。
  await test.click('#upd-upgrade');
  assert.equal(test.confirms.length, 1, '点击升级图标必须弹二次确认');
  assert.match(test.confirms[0].title, /更新到 v1\.5\.0/);
  assert.match(test.confirms[0].detail, /更新源：https:\/\/github\.com\/j9kkk\/auto-deploy\.git/);
  assert.match(test.confirms[0].detail, /网络代理：未启用/);
  const start = test.requests.find(r => r.url === '/api/system/self-update');
  assert.ok(start, '确认后才发起升级');
  assert.equal(start.options.method, 'POST');
  assert.equal(JSON.parse(start.options.body).target_version, 'v1.5.0');

  // 确认后进入进度视图：进度日志原地重建，页面不重绘。
  // 日志要有足够行数，否则「离底部 40px」在模拟尺寸里会被判成「贴着底部」。
  const baseLog = Array.from({ length: 20 }, (_, i) => '[10:00:0' + (i % 10) + '] 步骤 ' + i);
  const progress = { stage: 'downloading', active: true, operation: 'update', operation_id: 'op-1',
    target_version: 'v1.5.0', current_version: 'v1.4.0', log: baseLog };
  test.requests.length = 0;
  test = await scenario([settings, progress]);
  assert.equal(test.requests[0].url, '/api/settings');
  assert.equal(test.requests[1].url, '/api/system/self-update/status');
  assert.match(test.at('#upd-progress').innerHTML, /当前操作状态：下载中/);
  assert.match(test.at('#upd-progress').innerHTML, /目标版本 v1\.5\.0/);
  assert.match(test.at("#upd-progress").innerHTML, /步骤 0/);
  assert.match(test.at('#upd-row').innerHTML, /当前版本 <strong class="mono">v1\.4\.0/);
  // 操作进行中不发起检查，也就不提供升级入口：避免并发触发第二次更新。
  assert.equal(test.at('#upd-upgrade'), null, '更新进行中不得提供升级入口');
  assert.equal(test.requests.filter(r => r.url.includes('/update/check')).length, 0,
    '更新进行中不应重复检查');
  // 正在翻看历史的用户不能被新增日志拉回底部：阅读位置必须跨重绘保留。
  const firstLog = test.at('#upd-log');
  assert.ok(firstLog, '进行中必须显示升级过程日志');
  firstLog.scrollTop = 40;
  firstLog.scrollLeft = 7;
  await firstLog.fire('scroll');
  test.push({ ...progress, log: [...progress.log, '[10:00:01] 下载 100%'] });
  await test.tick();  // 一次状态轮询：日志变长，节点重建
  const secondLog = test.at('#upd-log');
  assert.ok(secondLog && secondLog !== firstLog, '日志增长时节点应被重建');
  assert.equal(secondLog.scrollTop, 40, '阅读历史位置不得被新增日志覆盖');
  assert.equal(secondLog.scrollLeft, 7);
  test.AD.stopUpdatePanel();

  // --- 确认完成：原地提示刷新（不自动整页 reload），并重查一次版本。
  const confirmed = { stage: 'done', active: false, operation: 'update', operation_id: 'op-1',
    confirmed_operation_id: 'op-1', confirmed_operation: 'update', before_boot_id: 'old', boot_id: 'new',
    before_pid: 1, pid: 2, current_version: 'v1.5.0', version: '1.5.0', expected_version: '1.5.0',
    target_version: 'v1.5.0', log: ['[10:00:05] 启动后已确认操作及目标版本运行'] };
  // 重查时服务已运行新版本，所以「已是最新」；沿用重启前的旧结果会显示可升级到已装版本。
  const nowLatest = { current: 'v1.5.0', latest: 'v1.5.0', update_available: false, error: '' };
  test = await scenario([settings, confirmed, nowLatest]);
  assert.equal(test.reloads(), 0, '确认完成不得自动整页刷新');
  assert.match(test.at('#upd-progress').innerHTML, /版本已更新为 <strong class="mono">v1\.5\.0/);
  assert.match(test.at('#upd-progress').innerHTML, /请刷新页面/);
  assert.equal(test.requests.filter(r => r.url.includes('/update/check?force=true')).length, 1,
    '确认成功后必须强制重查一次版本');
  assert.match(test.at('#upd-row').innerHTML, /当前已是最新版本/);
  assert.equal(test.at('#upd-upgrade'), null, '刚装上的版本不得再显示为可升级目标');
  const reload = test.at('#upd-reload');
  assert.ok(reload, '确认完成后必须提供刷新入口');
  await reload.fire('click');
  assert.equal(test.reloads(), 1, '由用户点击刷新按钮才 reload');

  // 旧记录 / 版本不符：不得伪装成功，也不得提供刷新按钮。
  for (const mismatch of [
    { ...confirmed, current_version: 'v1.4.0' },
    { ...confirmed, boot_id: 'old' },
    { ...confirmed, pid: 1 },
    { ...confirmed, confirmed_operation_id: undefined },
    { stage: 'unverified', active: false, notice: '历史记录缺少完整身份证据', log: [] },
  ]) {
    test = await scenario([settings, mismatch, noUpdate]);
    assert.equal(test.reloads(), 0);
    assert.equal(test.at('#upd-reload'), null, '未确认成功不得给刷新入口');
    assert.doesNotMatch(test.at('#upd-progress').innerHTML, /已确认完成/);
  }
  test = await scenario([settings, { stage: 'unverified', active: false, notice: '历史记录缺少完整身份证据', log: [] }, noUpdate]);
  assert.match(test.at('#upd-progress').innerHTML, /历史操作未确认/);

  // --- 检查失败：可见原因、提供重试，且不显示升级入口。
  for (const failure of [{ error: '更新源不可达', current: 'v1.4.0', latest: '', update_available: false }, { http: 500 }]) {
    test = await scenario([settings, idle, failure]);
    assert.match(test.at('#upd-row').innerHTML, /检查失败/);
    assert.equal(test.at('#upd-upgrade'), null, '检查失败不得沿用上次的升级入口');
    assert.ok(test.at('#upd-recheck'), '检查失败必须能重试');
    test.timers.clear();
  }

  // --- 重试走 force，且成功后升级入口出现（覆盖旧的失败结果）。
  test = await scenario([settings, idle, { error: '更新源不可达', latest: '' }]);
  test.requests.length = 0;
  await test.click('#upd-recheck');
  assert.equal(test.requests[0].url, '/api/system/update/check?force=true');

  // --- 升级失败：明确失败状态，并保留日志。
  test = await scenario([settings, { stage: 'failed', active: false, operation: 'update', operation_id: 'op-9',
    error: '依赖安装失败', target_version: 'v1.5.0', current_version: 'v1.4.0', log: ['[10:00:01] 失败日志'] }, noUpdate]);
  assert.equal(test.at('#upd-operation').attributes.class, 'alert error');
  assert.match(test.at('#upd-progress').innerHTML, /依赖安装失败/);
  assert.match(test.at('#upd-progress').innerHTML, /失败日志/);
  test.timers.clear();

  // --- 升级日志弹窗：每条记录含该次日志，按备份版本给出「回滚到这个版本」。
  const history = { current_version: 'v1.5.0', backups: [{ version: '1.4.0', created_at: 1, size: 10 }],
    entries: [
      { operation_id: 'h1', operation: 'update', stage: 'done', target_version: '1.5.0',
        previous_version: '1.4.0', backup_version: '1.4.0', current_version: '1.5.0',
        started_at: 1700000000, finished_at: 1700000060, error: '', log: ['[09:00:00] 更新日志一'] },
      { operation_id: 'h2', operation: 'update', stage: 'failed', target_version: '1.6.0',
        previous_version: '1.5.0', backup_version: '1.5.0', started_at: 1700000100, finished_at: 1700000120,
        error: '下载失败', log: ['[09:10:00] 更新日志二'] },
      { operation_id: 'h3', operation: 'update', stage: 'done', target_version: '1.4.0',
        previous_version: '1.3.7', backup_version: '', started_at: 0, finished_at: 1700000200,
        error: '', log: ['[09:20:00] 更新日志三'] },
    ] };
  test = await scenario([settings, idle, noUpdate, history]);
  test.timers.clear();
  await test.click('#upd-history');
  assert.equal(test.modalOptions.length, 1);
  assert.equal(test.modalOptions[0].title, '升级日志');
  const modalHtml = test.root.innerHTML;
  assert.match(modalHtml, /更新日志一/);
  assert.match(modalHtml, /更新日志二/);
  assert.match(modalHtml, /更新日志三/);
  // 已确认完成 / 失败 / 状态徽标都要出现，失败记录显示原因。
  assert.match(modalHtml, /已确认完成/);
  assert.match(modalHtml, /下载失败/);
  // h1 的备份 1.4.0 有可用备份 → 可回滚；h2 的备份 1.5.0 等于当前版本 → 已是当前版本；
  // h3 没有备份版本 → 明确说明。
  assert.match(modalHtml, /回滚到 v1\.4\.0/);
  assert.match(modalHtml, /已是当前版本/);
  assert.match(modalHtml, /无备份版本/);
  assert.match(modalHtml, /可回滚的版本：v1\.4\.0/);

  // 回滚需就地二次确认，确认后 POST 到 rollback 并带目标版本。
  const restore = test.root.querySelectorAll('[data-upd-restore]')[0];
  assert.ok(restore, '有可用备份时必须提供回滚按钮');
  await restore.fire('click');
  assert.match(test.root.innerHTML, /确认回滚到 v1\.4\.0/);
  const cancel = test.root.querySelector('[data-upd-cancel]');
  await cancel.fire('click');
  assert.match(test.root.innerHTML, /回滚到 v1\.4\.0/, '取消后恢复回滚入口');
  await test.root.querySelector('[data-upd-restore]').fire('click');
  test.requests.length = 0;
  await test.root.querySelector('[data-upd-confirm]').fire('click');
  const rollback = test.requests.find(r => r.url === '/api/system/self-update/rollback');
  assert.ok(rollback, '确认回滚必须发起 rollback 请求');
  assert.equal(JSON.parse(rollback.options.body).target_version, '1.4.0');

  // --- 后端拒绝回滚（如本机无 systemd）：错误要可见，且不能谎称「请求可能已送达」。
  test = await scenario([settings, idle, noUpdate, history,
    { http: 409, detail: '不支持自动重启：必须在真实 systemd 单元主进程中运行' }]);
  test.timers.clear();
  await test.click('#upd-history');
  await test.root.querySelectorAll('[data-upd-restore]')[0].fire('click');
  await test.root.querySelector('[data-upd-confirm]').fire('click');
  const refusal = test.at('#upd-error');
  assert.ok(refusal, '被拒绝时必须显示错误提示');
  assert.match(refusal.innerHTML, /不支持自动重启/);
  assert.doesNotMatch(refusal.innerHTML, /可能已送达/, 'HTTP 拒绝是确定结果，不应暗示请求可能已生效');
  assert.ok(test.root.querySelector('.modal'), '被拒绝时不得关闭弹窗（否则错误提示被遮住）');
  assert.match(test.root.innerHTML, /回滚到 v1\.4\.0/, '被拒绝后回滚入口应恢复可再试');

  // 网络中断（无 HTTP 状态）才提示核对状态。
  test = await scenario([settings, idle, noUpdate, history]);
  test.timers.clear();
  await test.click('#upd-history');
  await test.root.querySelectorAll('[data-upd-restore]')[0].fire('click');
  test.requests.length = 0;
  await test.root.querySelector('[data-upd-confirm]').fire('click');
  assert.match(test.at('#upd-error').innerHTML, /可能已送达/, '连接中断需提醒核对状态');

  // --- 无可用备份时不出现任何回滚入口。
  test = await scenario([settings, idle, noUpdate, { current_version: 'v1.4.0', backups: [],
    entries: [{ operation_id: 'h1', operation: 'update', stage: 'done', target_version: '1.4.0',
      previous_version: '1.3.7', backup_version: '1.3.7', started_at: 1, finished_at: 2, error: '', log: [] }] }]);
  test.timers.clear();
  await test.click('#upd-history');
  assert.equal(test.root.querySelectorAll('[data-upd-restore]').length, 0);
  assert.match(test.root.innerHTML, /回滚到 v1\.3\.7/);
  assert.match(test.root.innerHTML, /disabled/, '无备份的版本按钮必须禁用');
  assert.match(test.root.innerHTML, /暂无通过校验的代码备份/);

  // --- 配置弹窗：保存更新源并强制检查，结果回写到单行展示区。
  test = await scenario([settings, idle, noUpdate, { settings: { update_repo: 'https://example.com/mirror' } }, hasUpdate]);
  test.timers.clear();
  await test.click('#upd-config');
  const input = test.root.querySelector('#upd-repo');
  assert.ok(input, '配置弹窗必须能改更新源');
  assert.equal(input.attributes.type, 'url');
  assert.match(test.root.innerHTML, /网络代理/);
  input.value = 'https://example.com/mirror';
  test.requests.length = 0;
  await test.root.querySelector('#upd-config-save').fire('click');
  assert.equal(test.requests[0].url, '/api/settings');
  assert.equal(JSON.parse(test.requests[0].options.body).update_repo, 'https://example.com/mirror');
  assert.equal(test.requests[1].url, '/api/system/update/check?force=true');
  assert.match(test.root.innerHTML, /最新版本 v1\.5\.0/);
  assert.match(test.at('#upd-row').innerHTML, /最新版本 <strong class="mono">v1\.5\.0/);

  // --- 重连预算：网络错误在 40 次后停止，且期间不做整页重绘。
  test = await scenario([settings, { stage: 'restarting', active: true, operation_id: 'op-2', log: ['保留日志'] }]);
  for (let i = 0; i < 45; i++) {
    if (!test.timers.size) break;
    await test.tick();
  }
  assert.equal(test.timers.size, 0, '超过重连预算必须停止轮询');
  assert.equal(test.reloads(), 0);
  assert.match(test.at('#upd-progress').innerHTML, /保留日志/, '重绘日志时会话日志不丢失');

  // --- HTML 注入：日志与说明文案必须转义。
  test = await scenario([settings, { stage: 'unverified', active: false, notice: '<img src=x>',
    log: ['<script>日志</script>'] }, settings, noUpdate]);
  assert.doesNotMatch(test.at('#upd-progress').innerHTML, /<script>|<img/);
  assert.match(test.at('#upd-progress').innerHTML, /&lt;script&gt;日志/);
  test = await scenario([settings, idle, noUpdate, { current_version: 'v1', backups: [],
    entries: [{ operation_id: 'x', operation: 'update', stage: 'failed', target_version: '<b>1</b>',
      previous_version: 'v1', backup_version: 'v1', started_at: 1, finished_at: 2,
      error: '<script>失败</script>', log: ['<img src=x>'] }] }]);
  test.timers.clear();
  await test.click('#upd-history');
  assert.doesNotMatch(test.root.innerHTML, /<script>|<img/);

  // --- 离开卡片必须停掉定时器（轮询与控制器都不得残留）。
  test = await scenario([settings, { stage: 'restarting', active: true, operation_id: 'op-3' }]);
  test.AD.stopUpdatePanel();
  assert.equal(test.timers.size, 0);

  // --- CSS 静态契约：单行布局与日志滚动；不声称做过真实浏览器响应式验证。
  assert.match(css, /\.upd-row\s*\{[^}]*display:\s*flex;[^}]*align-items:\s*center;[^}]*flex-wrap:\s*wrap;/);
  assert.match(css, /\.upd-item\s*\{[^}]*display:\s*inline-flex;[^}]*min-width:\s*0;/);
  assert.match(css, /\.upd-latest\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/);
  assert.match(css, /#upd-log\s*\{[^}]*min-height:\s*0;[^}]*max-height:\s*260px;[^}]*overflow:\s*auto;/);
  assert.match(css, /#upd-log\s*\{[^}]*scrollbar-width:\s*thin;[^}]*color-scheme:\s*dark;/);
  assert.match(css, /\.upd-history-log\s*\{[^}]*max-height:\s*200px;[^}]*overflow:\s*auto;/);
  assert.match(css, /@media \(max-width: 600px\)\s*\{\s*\.upd-row\s*\{[^}]*align-items:\s*flex-start;/);
  assert.match(css, /#upd-progress:empty\s*\{\s*display:\s*none;/);
  assert.match(css, /\.alert\.error\s*\{[^}]*background:\s*var\(--danger-soft\)/);

  console.log('前端回归通过：单行展示与自动检查、升级二次确认含更新源、原地进度日志与手动刷新、'
    + '未确认不伪装成功、升级日志逐条回滚、失败重试与重连预算、注入转义、单行布局 CSS 契约'
    + '（未进行真实浏览器验证）');
})().catch(error => { console.error(error); process.exitCode = 1; });
