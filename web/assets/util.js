/* AutoDeploy — shared helpers: HTTP client, formatting, toasts, modals.
 * No frameworks and no external requests, so the console works on an
 * air-gapped server. Everything hangs off the global AD namespace. */

window.AD = window.AD || {};

(function (AD) {
  'use strict';

  // ------------------------------------------------------------------ state
  AD.state = {
    user: null,
    settings: null,
    defaults: null,
    tasks: [],
    dashboard: null,
    currentRunId: null,
    liveTimer: null,
    pollTimer: null,
    // 由当前视图注册的「就地刷新」回调：运行状态变化时只更新引用该运行的
    // 行与面板，不重建视图（重建会丢失滚动位置、展开状态与正在看的日志）。
    // 视图自行决定是否发请求；未注册时全局轮询不做任何事。
    viewRefresh: null,
  };

  // -------------------------------------------------------------- http client
  async function request(method, path, body) {
    const options = { method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      const error = new Error('无法连接到服务，请确认服务仍在运行');
      error.status = 0;
      throw error;
    }

    if (response.status === 401) {
      // Only report an expired session when one actually existed. A 401 while
      // sitting on the login screen is expected, not an error to announce.
      const hadSession = Boolean(AD.state.user);
      AD.state.user = null;
      AD.showLogin(hadSession ? '登录状态已过期，请重新登录' : '');
      const error = new Error('未登录');
      error.status = 401;
      throw error;
    }

    const contentType = response.headers.get('content-type') || '';
    let payload = null;
    if (contentType.includes('application/json')) {
      try {
        payload = await response.json();
      } catch (err) {
        payload = null;
      }
    } else {
      payload = await response.text();
    }

    if (!response.ok) {
      const error = new Error(extractMessage(payload, response.status));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function extractMessage(payload, status) {
    if (payload && typeof payload === 'object') {
      const detail = payload.detail;
      if (typeof detail === 'string') return detail;
      if (detail && typeof detail === 'object') {
        if (detail.errors && typeof detail.errors === 'object') {
          const parts = Object.entries(detail.errors).map(([k, v]) => k + ': ' + v);
          if (parts.length) return parts.join('；');
        }
        if (typeof detail.message === 'string') return detail.message;
      }
      if (typeof payload.message === 'string') return payload.message;
      if (Array.isArray(payload.errors) && payload.errors.length) {
        return payload.errors.map((e) => e.msg || JSON.stringify(e)).join('；');
      }
    }
    if (typeof payload === 'string' && payload.trim()) return payload.slice(0, 300);
    const byStatus = {
      400: '请求无效', 401: '未登录', 403: '没有权限', 404: '资源不存在',
      409: '状态冲突', 422: '参数校验失败', 429: '请求过于频繁', 500: '服务器内部错误',
    };
    return byStatus[status] || ('请求失败（HTTP ' + status + '）');
  }

  AD.api = {
    get: (path) => request('GET', path),
    post: (path, body) => request('POST', path, body === undefined ? {} : body),
    put: (path, body) => request('PUT', path, body),
    patch: (path, body) => request('PATCH', path, body),
    del: (path) => request('DELETE', path),
  };

  // ------------------------------------------------------------- formatting
  function pad(value) { return String(value).padStart(2, '0'); }

  /** Render an ISO timestamp in the server's system timezone. The offset
   * (minutes east of UTC) arrives via /api/health and /api/dashboard; until
   * it is known we fall back to the browser's own timezone. */
  AD.tzOffsetMinutes = null;

  function applyOffset(date) {
    if (AD.tzOffsetMinutes === null) return date;
    const minutes = date.getTime() + date.getTimezoneOffset() * 60000 + AD.tzOffsetMinutes * 60000;
    return new Date(minutes);
  }

  AD.formatTime = function (isoString, withSeconds) {
    if (!isoString) return '—';
    const date = applyOffset(new Date(String(isoString).endsWith('Z') ? isoString : isoString + 'Z'));
    if (Number.isNaN(date.getTime())) return '—';
    const base = date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
    return withSeconds ? base + ':' + pad(date.getSeconds()) : base;
  };

  AD.formatRelative = function (isoString) {
    if (!isoString) return '—';
    const date = new Date(String(isoString).endsWith('Z') ? isoString : isoString + 'Z');
    if (Number.isNaN(date.getTime())) return '—';
    const diff = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diff < 0) return AD.formatCountdown(-diff) + '后';
    if (diff < 60) return diff + ' 秒前';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 2592000) return Math.floor(diff / 86400) + ' 天前';
    return AD.formatTime(isoString);
  };

  AD.formatCountdown = function (seconds) {
    if (seconds <= 0) return '即将';
    if (seconds < 60) return seconds + ' 秒';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟';
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      return minutes ? hours + ' 小时 ' + minutes + ' 分' : hours + ' 小时';
    }
    return Math.floor(seconds / 86400) + ' 天';
  };

  AD.formatDuration = function (ms) {
    if (ms === null || ms === undefined || ms === 0) return '—';
    const seconds = ms / 1000;
    if (seconds < 1) return Math.round(ms) + 'ms';
    if (seconds < 60) return seconds.toFixed(1) + 's';
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    if (minutes < 60) return minutes + 'm ' + rest + 's';
    const hours = Math.floor(minutes / 60);
    return hours + 'h ' + (minutes % 60) + 'm';
  };

  AD.formatBytes = function (bytes) {
    if (!bytes && bytes !== 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = Number(bytes);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (index === 0 ? Math.round(value) : value.toFixed(1)) + ' ' + units[index];
  };

  AD.shortCommit = function (commit) {
    return commit ? String(commit).slice(0, 8) : '—';
  };

  AD.escapeHtml = function (value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  /** Attribute-safe escaping for values placed inside a quoted attribute. */
  AD.attr = function (value) { return AD.escapeHtml(value); };

  AD.statusLabel = function (status) {
    const map = {
      success: '成功', failed: '失败', running: '运行中',
      queued: '排队中', cancelled: '已取消', skipped: '已跳过',
    };
    return map[status] || status || '未知';
  };

  AD.triggerLabel = function (trigger) {
    return { manual: '手动', schedule: '定时', webhook: 'Webhook', rollback: '回滚', system: '系统' }[trigger] || trigger;
  };

  // ------------------------------------------------------------------ toasts
  AD.toast = function (message, kind, timeout) {
    const root = document.getElementById('toasts');
    if (!root) return;
    const node = document.createElement('div');
    node.className = 'toast ' + (kind || 'info');
    node.textContent = message;
    root.appendChild(node);
    const life = timeout || (kind === 'error' ? 7000 : 3800);
    setTimeout(() => {
      node.style.transition = 'opacity .25s';
      node.style.opacity = '0';
      setTimeout(() => node.remove(), 260);
    }, life);
  };

  AD.toastError = (message) => AD.toast(message, 'error');
  AD.toastSuccess = (message) => AD.toast(message, 'success');

  // ------------------------------------------------------------------ modals
  // 弹窗按栈管理：确认框、凭据表单可以叠在任务表单之上，关闭时只移除
  // 自己那一层，不打断底层的编辑流程。open() 默认仍是“替换式”打开
  // （先清空再挂载），传入 stack: true 才叠加。
  AD.Modal = {
    stack: [],
    open(options) {
      if (!options.stack) this.closeAll();
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML =
        '<div class="modal ' + (options.size || '') + '" role="dialog" aria-modal="true">'
        + '<div class="modal-head"><h2>' + AD.escapeHtml(options.title || '') + '</h2>'
        + '<div class="spacer"></div>'
        + (options.headExtra || '')
        + '<button class="ghost" data-close aria-label="关闭">✕</button></div>'
        + '<div class="modal-body">' + (options.body || '') + '</div>'
        + (options.footer === null ? ''
          : '<div class="modal-foot">'
            + (options.footerLeft || '<div class="left"></div>')
            + (options.footer || '<button data-close>关闭</button>')
            + '</div>')
        + '</div>';
      document.getElementById('modal-root').appendChild(backdrop);
      this.stack.push({
        backdrop,
        beforeClose: typeof options.beforeClose === 'function' ? options.beforeClose : null,
      });
      backdrop.addEventListener('click', (event) => {
        if (event.target === backdrop) this.requestClose();
        if (event.target.closest('[data-close]')) this.requestClose();
      });
      if (options.onMount) options.onMount(backdrop);
      return backdrop;
    },
    top() {
      return this.stack.length ? this.stack[this.stack.length - 1].backdrop : null;
    },
    // 用户发起的关闭（✕ / 遮罩 / Escape）：先过 beforeClose 关卡，
    // 表单用它做“放弃未保存修改？”确认；确认期间重复触发只算一次。
    async requestClose() {
      const entry = this.stack[this.stack.length - 1];
      if (!entry || entry.closing) return;
      if (entry.beforeClose) {
        entry.closing = true;
        let allowed = false;
        try { allowed = await entry.beforeClose(); } catch (err) { allowed = false; }
        entry.closing = false;
        if (!allowed) return;
      }
      if (this.stack[this.stack.length - 1] === entry) this.close();
    },
    close() {
      const entry = this.stack.pop();
      if (entry) entry.backdrop.remove();
      else {
        const root = document.getElementById('modal-root');
        if (root) root.innerHTML = '';
      }
    },
    // 精确移除指定弹窗：确认框完成时不依赖“自己还在栈顶”的假设。
    remove(backdrop) {
      const index = this.stack.findIndex((entry) => entry.backdrop === backdrop);
      if (index >= 0) this.stack.splice(index, 1);
      backdrop.remove();
    },
    closeAll() {
      this.stack = [];
      const root = document.getElementById('modal-root');
      if (root) root.innerHTML = '';
    },
  };

  AD.confirm = function (options) {
    return new Promise((resolve) => {
      let settled = false;
      let backdrop;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        if (backdrop) AD.Modal.remove(backdrop);
        resolve(value);
      };
      backdrop = AD.Modal.open({
        title: options.title || '请确认',
        size: 'narrow',
        // 确认框始终叠层：可能出现在任务表单等既有弹窗之上。
        stack: true,
        body: '<p>' + AD.escapeHtml(options.message || '') + '</p>'
          + (options.detail ? '<p class="faint">' + AD.escapeHtml(options.detail) + '</p>' : ''),
        footer: '<button data-cancel>取消</button>'
          + '<button class="' + (options.danger ? 'danger' : 'primary') + '" data-ok>'
          + AD.escapeHtml(options.confirmText || '确认') + '</button>',
        onMount(node) {
          node.querySelector('[data-ok]').addEventListener('click', () => finish(true));
          node.querySelector('[data-cancel]').addEventListener('click', () => finish(false));
        },
      });
      // 底层弹窗被整体替换等情况下 backdrop 会离开文档：视为取消。
      const observer = new MutationObserver(() => {
        if (!backdrop.isConnected) { finish(false); observer.disconnect(); }
      });
      observer.observe(document.getElementById('modal-root'), { childList: true });
    });
  };

  /** Prompt for a single text value; resolves to null when dismissed. */
  AD.prompt = function (options) {
    return new Promise((resolve) => {
      let settled = false;
      let backdrop;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        if (backdrop) AD.Modal.remove(backdrop);
        resolve(value);
      };
      backdrop = AD.Modal.open({
        title: options.title || '请输入',
        size: 'narrow',
        stack: true,
        body: '<div class="field"><label>' + AD.escapeHtml(options.label || '值') + '</label>'
          + '<input type="' + (options.type || 'text') + '" id="prompt-input" '
          + 'value="' + AD.attr(options.value || '') + '" '
          + 'placeholder="' + AD.attr(options.placeholder || '') + '"></div>'
          + (options.hint ? '<p class="faint">' + AD.escapeHtml(options.hint) + '</p>' : ''),
        footer: '<button data-cancel>取消</button><button class="primary" data-ok>确认</button>',
        onMount(node) {
          const input = node.querySelector('#prompt-input');
          input.focus();
          input.select();
          input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') { event.preventDefault(); finish(input.value); }
          });
          node.querySelector('[data-ok]').addEventListener('click', () => finish(input.value));
          node.querySelector('[data-cancel]').addEventListener('click', () => finish(null));
        },
      });
      const observer = new MutationObserver(() => {
        if (!backdrop.isConnected) { finish(null); observer.disconnect(); }
      });
      observer.observe(document.getElementById('modal-root'), { childList: true });
    });
  };

  // ------------------------------------------------------------------ misc
  AD.debounce = function (fn, wait) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  };

  AD.copyToClipboard = async function (text) {
    try {
      await navigator.clipboard.writeText(text);
      AD.toastSuccess('已复制到剪贴板');
    } catch (err) {
      // Clipboard access needs HTTPS or localhost; fall back to a textarea.
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try { document.execCommand('copy'); AD.toastSuccess('已复制到剪贴板'); }
      catch (e) { AD.toastError('复制失败，请手动选择文本'); }
      area.remove();
    }
  };

  AD.el = function (html) {
    const template = document.createElement('template');
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
  };

  AD.setBusy = function (button, busy, busyText) {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> ' + (busyText || '处理中…');
    } else {
      button.disabled = false;
      if (button.dataset.originalText) button.innerHTML = button.dataset.originalText;
    }
  };
})(window.AD);
