/* AutoDeploy — application bootstrap: session handling, routing, polling. */

(function (AD) {
  'use strict';

  const VIEWS = ['dashboard', 'tasks', 'runs', 'stats', 'settings'];
  const HASH_TO_VIEW = {
    '': 'dashboard', '#': 'dashboard',
    '#/dashboard': 'dashboard', '#/tasks': 'tasks',
    '#/runs': 'runs', '#/stats': 'stats', '#/settings': 'settings',
  };

  // ---------------------------------------------------------------- session
  AD.showLogin = function (message) {
    document.getElementById('app-view').classList.add('hidden');
    document.getElementById('login-view').classList.remove('hidden');
    const errorBox = document.getElementById('login-error');
    if (message) {
      errorBox.textContent = message;
      errorBox.classList.remove('hidden');
    } else {
      errorBox.classList.add('hidden');
    }
    AD.stopLiveLog();
    AD.stopPolling();
    const input = document.getElementById('username');
    if (input) input.focus();
  };

  AD.showApp = function () {
    document.getElementById('login-view').classList.add('hidden');
    document.getElementById('app-view').classList.remove('hidden');
    const user = AD.state.user || {};
    document.getElementById('user-name').textContent = user.display_name || user.username || '';
    document.getElementById('user-avatar').textContent =
      (user.display_name || user.username || 'A').charAt(0).toUpperCase();
    AD.startPolling();
  };

  async function checkSession() {
    try {
      const data = await AD.api.get('/api/auth/me');
      AD.state.user = data.user;
      AD.showApp();
      return true;
    } catch (err) {
      if (err.status !== 401) {
        // A non-auth failure means the service is unreachable, not that the
        // session is invalid; surface that instead of showing the login form.
        AD.showLogin('无法连接服务：' + err.message);
      } else {
        AD.showLogin('');
      }
      return false;
    }
  }

  // ---------------------------------------------------------------- routing
  AD.state.currentView = null;

  AD.navigate = function (view) {
    if (VIEWS.indexOf(view) === -1) view = 'dashboard';
    if (window.location.hash !== '#/' + view) {
      window.location.hash = '#/' + view;
    } else {
      AD.render(view);
    }
  };

  AD.render = async function (view) {
    const container = document.getElementById('content');
    const previousView = AD.state.currentView;
    AD.state.currentView = view;
    document.querySelectorAll('#nav .nav-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.view === view);
    });
    AD.stopLiveLog();
    if (AD.stopAllInlineLogs) {
      AD.stopAllInlineLogs(); // 停掉行内日志轮询（任务页重绘后会按需重启）
      // 只在真正离开任务页时清除展开状态；任务页内部重绘需要保留它，
      // 以便把用户正在看的展开行在渲染后恢复回来。
      if (previousView !== 'tasks') AD.state.expandedTaskId = null;
    }
    container.innerHTML = '<div class="loading-block"><span class="spinner"></span> 加载中…</div>';
    try {
      await AD.views[view](container);
    } catch (err) {
      if (err.status === 401) return;
      container.innerHTML =
        '<div class="panel"><div class="alert error">加载失败：' + AD.escapeHtml(err.message)
        + '</div><button id="view-retry">重试</button></div>';
      const retry = container.querySelector('#view-retry');
      if (retry) retry.addEventListener('click', () => AD.render(view));
    }
  };

  function currentViewFromHash() {
    const view = HASH_TO_VIEW[window.location.hash];
    return view || 'dashboard';
  }

  window.addEventListener('hashchange', () => AD.render(currentViewFromHash()));

  // ---------------------------------------------------------------- polling
  AD.startPolling = function () {
    AD.stopPolling();
    AD.state.pollTimer = setInterval(refreshStatus, 5000);
    refreshStatus();
  };

  AD.stopPolling = function () {
    if (AD.state.pollTimer) { clearInterval(AD.state.pollTimer); AD.state.pollTimer = null; }
  };

  /** Keep the header badge and the active view roughly in sync. */
  async function refreshStatus() {
    const badge = document.getElementById('scheduler-status');
    try {
      const health = await AD.api.get('/api/health');
      if (badge) {
        badge.className = 'badge ' + (health.scheduler_running ? 'success' : 'failed');
        badge.textContent = health.scheduler_running ? '调度运行中' : '调度已停止';
      }
    } catch (err) {
      if (badge) { badge.className = 'badge failed'; badge.textContent = '服务不可达'; }
      return;
    }
  }

  // ------------------------------------------------------------------ boot
  function bindGlobalHandlers() {
    document.querySelectorAll('#nav .nav-item').forEach((item) => {
      item.addEventListener('click', () => AD.navigate(item.dataset.view));
    });

    document.getElementById('logout-btn').addEventListener('click', async () => {
      try { await AD.api.post('/api/auth/logout'); } catch (err) { /* ignore */ }
      AD.state.user = null;
      AD.showLogin('已安全退出');
    });

    document.getElementById('login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = document.getElementById('login-submit');
      const errorBox = document.getElementById('login-error');
      const infoBox = document.getElementById('login-info');
      errorBox.classList.add('hidden');
      infoBox.classList.add('hidden');

      AD.setBusy(button, true, '登录中…');
      try {
        const result = await AD.api.post('/api/auth/login', {
          username: document.getElementById('username').value.trim(),
          password: document.getElementById('password').value,
        });
        AD.state.user = result.user;
        document.getElementById('password').value = '';
        AD.showApp();
        if (result.user && result.user.display_name) {
          AD.toastSuccess('欢迎回来，' + (result.user.display_name || result.user.username));
        }
        AD.navigate(currentViewFromHash());
      } catch (err) {
        errorBox.textContent = err.message;
        errorBox.classList.remove('hidden');
        document.getElementById('password').select();
      }
      AD.setBusy(button, false);
    });

    // Escape closes any open modal.
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') AD.Modal.close();
    });
  }

  async function boot() {
    bindGlobalHandlers();
    const authenticated = await checkSession();
    if (!authenticated) {
      // Poll once in the background so the form enables itself if the service
      // comes back, without showing an error the user did not cause.
      setTimeout(async () => {
        if (!AD.state.user) {
          try {
            const data = await AD.api.get('/api/auth/me');
            AD.state.user = data.user;
            AD.showApp();
            AD.navigate(currentViewFromHash());
          } catch (err) { /* still not available */ }
        }
      }, 5000);
      return;
    }
    AD.navigate(currentViewFromHash());
  }

  // An expired session is reported by the HTTP layer through AD.showLogin, so
  // polling stops as soon as the 401 arrives.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window.AD);
