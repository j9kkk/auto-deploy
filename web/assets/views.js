/* AutoDeploy — views: dashboard, tasks, task form, runs, live log, stats, settings. */

window.AD = window.AD || {};
AD.views = {};

(function (AD) {
  'use strict';

  const e = AD.escapeHtml;
  const a = AD.attr;

  const STAGE_LABELS = [
    ['artifact', '仅打包', '构建并打包产物，不发布到服务器'],
    ['script', '自定义脚本', '完全由部署脚本控制发布过程'],
    ['release', '发布目录 + 软链', '打包到 releases/N，并把 current 软链切换过去'],
    ['systemd', '发布 + systemd', '切换软链后重启 systemd 服务并校验状态'],
    ['docker', 'Docker 构建', '在发布目录构建镜像，可选执行容器启动命令'],
    ['docker_compose', 'Docker Compose', '执行 docker compose up -d --build'],
    ['rsync', 'rsync 同步', '把发布目录同步到远端或本地目标路径'],
  ];

  function methodLabel(method) {
    const found = STAGE_LABELS.find((item) => item[0] === method);
    return found ? found[1] : (method || '');
  }
  AD.methodLabel = methodLabel;

  // ---------------------------------------------------------------- icons
  // 全部为内联 SVG（stroke 继承 currentColor），无外部资源依赖。
  const ICONS = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a.7.7 0 0 0 1.07.6l10.2-6.5a.7.7 0 0 0 0-1.2L9.07 4.9A.7.7 0 0 0 8 5.5z"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="1.6"/></svg>',
    // 卷轴：展开/收起日志用
    scroll: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4.5h9.5a2 2 0 0 1 2 2v11a2 2 0 0 0 2 2H8.5a2 2 0 0 1-2-2z"/><path d="M8.5 20.5a2 2 0 0 1-2-2v-11a2 2 0 0 0-2-2"/><path d="M10.5 9h5M10.5 12.5h5"/></svg>',
    log: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 5.5h16M4 12h16M4 18.5h10"/></svg>',
    edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4.5L19 9.5a2.1 2.1 0 0 0-3-3L5.5 17 4 20z"/></svg>',
    disable: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8.2"/><path d="M6 6l12 12"/></svg>',
    enable: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4h-4"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11"/><path d="m7 11 5 5 5-5"/><path d="M5 20h14"/></svg>',
    rollback: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>',
  };
  AD.ICONS = ICONS;

  // ======================================================================
  // Dashboard
  // ======================================================================
  AD.views.dashboard = async function (container) {
    const data = await AD.api.get('/api/dashboard');
    AD.state.dashboard = data;
    const overview = data.overview || {};
    const tasks = data.tasks || {};

    container.innerHTML = `
      <div class="view-header">
        <h1>总览</h1>
        <div class="spacer"></div>
        <span class="faint" id="dash-clock"></span>
        <button class="ghost sm" id="dash-refresh" title="刷新">刷新</button>
      </div>

      <div class="grid cols-4" style="margin-bottom:16px">
        <div class="stat success">
          <span class="label">部署成功</span>
          <span class="value">${fmtNum(overview.success)}</span>
          <span class="sub">成功率 ${overview.success_rate || 0}%</span>
        </div>
        <div class="stat danger">
          <span class="label">部署失败</span>
          <span class="value">${fmtNum(overview.failed)}</span>
          <span class="sub">累计 ${fmtNum(overview.total)} 次运行</span>
        </div>
        <div class="stat accent">
          <span class="label">进行中</span>
          <span class="value">${fmtNum(overview.active)}</span>
          <span class="sub">并发上限 ${(data.scheduler || {}).max_global_workers || '—'}</span>
        </div>
        <div class="stat">
          <span class="label">启用任务</span>
          <span class="value">${fmtNum(tasks.enabled)}</span>
          <span class="sub">共 ${fmtNum(tasks.total)} 个任务${tasks.failing ? ` · ${tasks.failing} 个最近失败` : ''}</span>
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head">
            <h2>最近 14 天</h2>
            <div class="spacer"></div>
            <span class="faint">柱高代表运行次数</span>
          </div>
          ${renderChart(data.daily || [])}
        </div>

        <div class="panel">
          <div class="panel-head"><h2>进行中的运行</h2>
            <div class="spacer"></div>
            <span class="badge ${overview.active ? 'running' : 'neutral'}">${fmtNum(overview.active)}</span>
          </div>
          ${renderActiveRuns(data.active_runs || [])}
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>最近运行</h2>
            <div class="spacer"></div>
            <button class="ghost sm" data-goto="runs">查看全部</button>
          </div>
          ${renderRecentRuns(data.recent_runs || [])}
        </div>

        <div class="panel">
          <div class="panel-head"><h2>即将执行</h2>
            <div class="spacer"></div>
            <button class="ghost sm" data-goto="tasks">管理任务</button>
          </div>
          ${renderUpcoming(data.upcoming || [])}
        </div>
      </div>
    `;

    const clock = container.querySelector('#dash-clock');
    const scheduleClock = () => {
      if (clock) clock.textContent = '服务器时间 ' + AD.formatTime(data.now, true);
    };
    scheduleClock();
    setTimeout(scheduleClock, 600);

    container.querySelector('#dash-refresh').addEventListener('click', () => AD.render('dashboard'));
    container.querySelectorAll('[data-goto]').forEach((button) => {
      button.addEventListener('click', () => AD.navigate(button.dataset.goto));
    });
    bindRunLinks(container);
  };

  function renderChart(daily) {
    if (!daily.length) return '<div class="empty">暂无数据</div>';
    const max = Math.max(1, ...daily.map((day) => day.total || 0));
    const bars = daily.map((day) => {
      const total = day.total || 0;
      const height = total ? Math.max(4, Math.round((total / max) * 92)) : 3;
      const success = day.success || 0;
      const failed = day.failed || 0;
      const skipped = day.skipped || 0;
      const label = String(day.day || '').slice(5);
      const title = `${day.day}：共 ${total} 次（成功 ${success} / 失败 ${failed} / 跳过 ${skipped}）`;
      return `<div class="bar-wrap" title="${a(title)}">
        <div class="bar" style="height:${height}px">
          ${success ? `<div class="seg-success" style="height:${(success / total) * 100}%"></div>` : ''}
          ${failed ? `<div class="seg-failed" style="height:${(failed / total) * 100}%"></div>` : ''}
          ${skipped ? `<div class="seg-skipped" style="height:${(skipped / total) * 100}%"></div>` : ''}
          ${!success && !failed && !skipped ? '<div style="height:100%;background:var(--border-strong)"></div>' : ''}
        </div>
        <span class="bar-label">${e(label)}</span>
      </div>`;
    }).join('');
    return `<div class="chart">${bars}</div>
      <div class="row" style="gap:14px;margin-top:14px;font-size:11.5px">
        <span class="row" style="gap:5px"><span class="dot success"></span>成功</span>
        <span class="row" style="gap:5px"><span class="dot failed"></span>失败</span>
        <span class="row" style="gap:5px"><span class="dot skipped"></span>跳过</span>
      </div>`;
  }

  function renderActiveRuns(runs) {
    if (!runs.length) {
      return '<div class="empty"><div class="big">✓</div>当前没有运行中的部署</div>';
    }
    return '<div class="table-wrap"><table><thead><tr>'
      + '<th>任务</th><th>状态</th><th>开始时间</th><th></th></tr></thead><tbody>'
      + runs.map((run) => `<tr>
          <td>${e(run.task_name)}</td>
          <td><span class="badge ${e(run.status)}"><span class="dot ${e(run.status)}"></span>${e(AD.statusLabel(run.status))}</span></td>
          <td class="dim nowrap">${e(AD.formatRelative(run.started_at || run.queued_at))}</td>
          <td class="right"><button class="sm" data-run-log="${run.id}">查看日志</button></td>
        </tr>`).join('')
      + '</tbody></table></div>';
  }

  function renderRecentRuns(runs) {
    if (!runs.length) return '<div class="empty">还没有运行记录</div>';
    return '<div class="table-wrap"><table><thead><tr>'
      + '<th>任务</th><th>状态</th><th>提交</th><th>耗时</th><th>时间</th></tr></thead><tbody>'
      + runs.map((run) => `<tr>
          <td class="truncate" style="max-width:190px">${e(run.task_name)}</td>
          <td><span class="badge ${e(run.status)}">${e(AD.statusLabel(run.status))}</span></td>
          <td class="mono dim">${e(AD.shortCommit(run.commit_after))}</td>
          <td class="dim nowrap">${e(AD.formatDuration(run.duration_ms))}</td>
          <td class="dim nowrap">${e(AD.formatRelative(run.queued_at))}</td>
        </tr>`).join('')
      + '</tbody></table></div>';
  }

  function renderUpcoming(items) {
    if (!items.length) {
      return '<div class="empty">没有已排期的任务<div class="faint" style="margin-top:6px">'
        + '为任务配置「间隔」或「cron」调度后，这里会显示下次执行时间</div></div>';
    }
    return '<div class="table-wrap"><table><thead><tr>'
      + '<th>任务</th><th>调度</th><th>下次执行</th></tr></thead><tbody>'
      + items.map((item) => {
        const when = new Date(String(item.next_run_at).endsWith('Z') ? item.next_run_at : item.next_run_at + 'Z');
        const seconds = Math.max(0, Math.round((when.getTime() - Date.now()) / 1000));
        return `<tr>
          <td class="truncate" style="max-width:200px">${e(item.name)}</td>
          <td class="dim mono" style="font-size:11.5px">${e(item.schedule_expression)}</td>
          <td class="nowrap">${e(AD.formatTime(item.next_run_at))}
            <span class="faint">（${e(AD.formatCountdown(seconds))}后）</span></td>
        </tr>`;
      }).join('')
      + '</tbody></table></div>';
  }

  function fmtNum(value) { return value === null || value === undefined ? '0' : String(value); }

  function bindRunLinks(container) {
    container.querySelectorAll('[data-run-log]').forEach((button) => {
      button.addEventListener('click', () => AD.openRunDetail(Number(button.dataset.runLog)));
    });
    container.querySelectorAll('[data-task-edit]').forEach((button) => {
      button.addEventListener('click', () => AD.openTaskForm(Number(button.dataset.taskEdit)));
    });
  }
  AD.bindRunLinks = bindRunLinks;

  // ======================================================================
  // Tasks list
  // ======================================================================
  AD.views.tasks = async function (container) {
    const data = await AD.api.get('/api/tasks');
    AD.state.tasks = data.tasks || [];

    container.innerHTML = `
      <div class="view-header">
        <h1>任务</h1>
        <span class="badge neutral">${AD.state.tasks.length} 个</span>
        <div class="spacer"></div>
        <input type="text" id="task-search" placeholder="搜索任务名或仓库地址" style="width:220px">
        <button class="primary" id="task-new">+ 新建任务</button>
      </div>
      <div id="tasks-body"></div>
    `;

    const render = (filter) => {
      const body = container.querySelector('#tasks-body');
      const keyword = (filter || '').trim().toLowerCase();
      const tasks = keyword
        ? AD.state.tasks.filter((task) =>
            (task.name || '').toLowerCase().includes(keyword)
            || (task.repo_url || '').toLowerCase().includes(keyword))
        : AD.state.tasks;
      AD.stopAllInlineLogs();
      body.innerHTML = renderTaskTable(tasks, keyword);
      bindTaskActions(container);
      // 状态刷新（如运行结束后的重绘）会重建表格；把之前展开的行恢复，
      // 避免用户正在看的实时日志凭空消失。
      if (AD.state.expandedTaskId && tasks.some((task) => task.id === AD.state.expandedTaskId)) {
        AD.toggleTaskExpand(AD.state.expandedTaskId, { forceOpen: true });
      }
    };
    render('');

    const search = container.querySelector('#task-search');
    search.addEventListener('input', AD.debounce(() => render(search.value), 180));
    container.querySelector('#task-new').addEventListener('click', () => AD.openTaskForm(null));
  };

  function renderTaskTable(tasks, keyword) {
    if (!tasks.length) {
      return `<div class="panel"><div class="empty">
        <div class="big">＋</div>
        <h3>${keyword ? '没有匹配的任务' : '还没有任务'}</h3>
        <p class="faint">${keyword ? '换个关键字试试' : '创建第一个任务，配置仓库地址、拉取频率与部署脚本'}</p>
        ${keyword ? '' : '<button class="primary" id="task-new-empty">+ 新建任务</button>'}
      </div></div>`;
    }

    return `<div class="panel">
      <div class="table-wrap"><table class="task-table">
        <thead><tr>
          <th>任务</th><th>状态</th><th>调度</th><th>部署方式</th>
          <th>最近运行</th><th>下次执行</th><th class="right">操作</th>
        </tr></thead>
        <tbody>${tasks.map(taskRow).join('')}</tbody>
      </table></div>
    </div>`;
  }

  function taskRow(task) {
    const active = task.active_run;
    const lastStatus = task.last_status;
    const statusBadge = active
      ? `<span class="badge ${e(active.status)}"><span class="dot ${e(active.status)}"></span>${e(AD.statusLabel(active.status))}</span>`
      : task.enabled
        ? '<span class="badge on">已启用</span>'
        : '<span class="badge off">已暂停</span>';

    const stats = task.run_count
      ? `<span class="faint">${task.success_count} 成功 / ${task.failure_count} 失败</span>`
      : '<span class="faint">尚未运行</span>';

    const lastRun = lastStatus
      ? `<span class="badge ${e(lastStatus)}">${e(AD.statusLabel(lastStatus))}</span>
         <div class="faint">${e(AD.formatRelative(task.last_run_at))}</div>`
      : '<span class="faint">—</span>';

    const nextRun = task.next_run_at
      ? `${e(AD.formatTime(task.next_run_at))}<div class="faint">${e(AD.scheduleText(task))}</div>`
      : `<span class="faint">${e(task.enabled ? '仅手动触发' : '已暂停')}</span>`;

    // 运行/停止共用一个位置：空闲时是播放（运行），运行中变为终止（取消）。
    const runButton = active
      ? `<button class="icon-btn stop" data-task-cancel="${task.id}" title="停止当前运行" aria-label="停止当前运行">${ICONS.stop}</button>`
      : `<button class="icon-btn run" data-task-run="${task.id}" title="立即运行" aria-label="立即运行">${ICONS.play}</button>`;

    const expanded = AD.state.expandedTaskId === task.id;
    // 启用/禁用用颜色区分状态：启用=绿底（点击将禁用），禁用=灰/黄底（点击将启用）
    const toggleButton = task.enabled
      ? `<button class="icon-btn state-on" data-task-toggle="${task.id}"
                 title="禁用调度" aria-label="禁用调度">${ICONS.disable}</button>`
      : `<button class="icon-btn state-off" data-task-toggle="${task.id}"
                 title="启用调度" aria-label="启用调度">${ICONS.enable}</button>`;

    return `<tr data-task-row="${task.id}" class="${task.enabled ? '' : 'row-disabled'}">
      <td class="task-name-cell">
        <button type="button" class="task-name-link" data-task-edit="${task.id}"
                title="点击编辑任务">${e(task.name)}</button>
        ${task.description ? `<div class="faint truncate" title="${a(task.description)}">${e(task.description)}</div>` : ''}
        <div class="faint truncate" style="max-width:280px" title="${a(task.repo_url)}">
          ${e(task.repo_url)}<span class="dim"> @${e(task.repo_branch)}</span>
        </div>
      </td>
      <td>${statusBadge}<div class="faint" style="margin-top:3px">${stats}</div></td>
      <td class="mono" style="font-size:11.5px">${e(task.schedule_expression || '—')}</td>
      <td><span class="badge neutral">${e(methodLabel(task.deploy_method))}</span></td>
      <td class="nowrap">${lastRun}</td>
      <td class="nowrap">${nextRun}</td>
      <td>
        <div class="table-actions row-actions">
          <button class="icon-btn scroll-btn${expanded ? ' active' : ''}" data-task-log="${task.id}"
                  title="${expanded ? '收起日志' : '展开最近日志'}"
                  aria-label="${expanded ? '收起日志' : '展开最近日志'}"
                  aria-expanded="${expanded ? 'true' : 'false'}">${ICONS.scroll}</button>
          ${runButton}
          ${toggleButton}
        </div>
      </td>
    </tr>`;
  }

  AD.scheduleText = function (task) {
    if (task.schedule_description) return task.schedule_description;
    if (task.schedule_type === 'interval') return '间隔 ' + task.schedule_expression;
    if (task.schedule_type === 'cron') return 'cron ' + task.schedule_expression;
    return '仅手动';
  };

  // 任务页所有动作使用事件委托：监听器只挂一次，表格随状态重绘、行内
  // 展开随启停重建都不会累积或丢失事件绑定。
  function bindTaskActions(container) {
    if (container.dataset.taskActionsBound) return;
    container.dataset.taskActionsBound = '1';

    const handle = async (button, fn) => {
      AD.setBusy(button, true);
      try { await fn(); } catch (err) { AD.toastError(err.message); } finally { AD.setBusy(button, false); }
    };

    container.addEventListener('click', (event) => {
      const run = event.target.closest('[data-task-run]');
      if (run) {
        const taskId = Number(run.dataset.taskRun);
        const task = (AD.state.tasks || []).find((item) => item.id === taskId) || {};
        // 运行会真正改动线上服务，先二次确认，避免误点。
        AD.confirm({
          title: '确认运行部署',
          message: `将立即对「${task.name || '该任务'}」执行一次部署。`,
          detail: '部署会拉取最新代码并更新线上服务，请确认当前不是发布冻结期。',
          confirmText: '开始运行',
        }).then((ok) => {
          if (!ok) return;
          return handle(run, async () => {
            const result = await AD.api.post(`/api/tasks/${taskId}/run`);
            AD.toastSuccess('已开始运行 #' + result.run_id);
            // 运行后直接展开该行，让用户看到实时日志。
            await AD.render('tasks');
            AD.toggleTaskExpand(taskId, { forceOpen: true, runId: result.run_id });
          });
        });
        return;
      }
      const cancel = event.target.closest('[data-task-cancel]');
      if (cancel) {
        handle(cancel, async () => {
          await AD.api.post(`/api/tasks/${cancel.dataset.taskCancel}/cancel`);
          AD.toastSuccess('已请求取消当前运行');
          AD.render('tasks');
        });
        return;
      }
      const logBtn = event.target.closest('[data-task-log]');
      if (logBtn) {
        // 点击在展开与收起之间切换（不传 forceOpen，由 toggle 判断）。
        AD.toggleTaskExpand(Number(logBtn.dataset.taskLog));
        return;
      }
      const edit = event.target.closest('[data-task-edit]');
      if (edit) {
        AD.openTaskForm(Number(edit.dataset.taskEdit));
        return;
      }
      const toggle = event.target.closest('[data-task-toggle]');
      if (toggle) {
        handle(toggle, async () => {
          const result = await AD.api.post(`/api/tasks/${toggle.dataset.taskToggle}/toggle`);
          AD.toastSuccess(result.enabled ? '任务已启用' : '任务已禁用');
          AD.render('tasks');
        });
        return;
      }
      const emptyNew = event.target.closest('#task-new-empty');
      if (emptyNew) AD.openTaskForm(null);
    });
  }
  AD.bindTaskActions = bindTaskActions;

  // ======================================================================
  // 行内展开：详情 + 实时日志（替代原弹窗）
  // ======================================================================
  /** 同步卷轴按钮的激活态与提示文案。 */
  function syncScrollButton(button, expanded) {
    if (!button) return;
    button.classList.toggle('active', Boolean(expanded));
    button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    const label = expanded ? '收起日志' : '展开最近日志';
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  AD.toggleTaskExpand = async function (taskId, options) {
    const options_ = options || {};
    const row = document.querySelector(`tr[data-task-row="${taskId}"]`);
    if (!row) { AD.stopInlineLog(taskId); AD.state.expandedTaskId = null; return; }

    const existing = document.getElementById(`task-expand-${taskId}`);
    const scrollBtn = row.querySelector('.scroll-btn');
    if (existing && !options_.forceOpen) {
      existing.remove();
      AD.stopInlineLog(taskId);
      AD.state.expandedTaskId = null;
      delete AD.state.inlineRunSelection[taskId];
      syncScrollButton(scrollBtn, false);
      return;
    }

    // 只保留一个展开行，展开新的收起旧的。
    document.querySelectorAll('.expand-row').forEach((node) => node.remove());
    document.querySelectorAll('.scroll-btn.active').forEach((node) => syncScrollButton(node, false));
    AD.stopAllInlineLogs();
    AD.state.expandedTaskId = taskId;
    syncScrollButton(scrollBtn, true);

    const placeholder = document.createElement('tr');
    placeholder.className = 'expand-row';
    placeholder.id = `task-expand-${taskId}`;
    placeholder.innerHTML = `<td colspan="7"><div class="task-expand">
      <div class="loading-block" style="padding:22px"><span class="spinner"></span> 加载中…</div>
    </div></td>`;
    row.after(placeholder);

    let data;
    try { data = await AD.api.get(`/api/tasks/${taskId}`); }
    catch (err) {
      if (document.getElementById(`task-expand-${taskId}`) !== placeholder) return;
      placeholder.remove();
      AD.state.expandedTaskId = null;
      syncScrollButton(scrollBtn, false);
      AD.toastError(err.message);
      return;
    }
    if (document.getElementById(`task-expand-${taskId}`) !== placeholder) return;

    const t = data.task;
    const runs = data.runs || [];
    const checks = data.preflight || [];
    const activeRun = runs.find((run) => run.status === 'queued' || run.status === 'running');
    const lastRun = runs[0];

    placeholder.querySelector('td > .task-expand').innerHTML = `
      <div class="expand-toolbar">
        <span class="badge ${t.enabled ? 'on' : 'off'}">${t.enabled ? '已启用' : '已禁用'}</span>
        <span class="badge neutral">${e(methodLabel(t.deploy_method))}</span>
        ${dedupeIn([AD.scheduleText(t), nextRunInlineText(t)]).map((text) =>
          `<span class="faint">${e(text)}</span>`).join('')}
        <div class="spacer"></div>
        <button class="sm" data-inline-rollback="${t.id}">${ICONS.rollback}<span>回滚上一版本</span></button>
        <button class="sm" data-inline-rollback-selected disabled aria-describedby="inline-rollback-reason-${t.id}">${ICONS.rollback}<span>回滚到这个版本</span></button>
        <button class="sm" data-inline-artifacts="${t.id}">${ICONS.download}<span>产物</span></button>
        ${activeRun ? `<button class="sm danger" data-inline-cancel="${activeRun.id}">取消 #${activeRun.id}</button>` : ''}
      </div>
      <div class="hint inline-rollback-reason" id="inline-rollback-reason-${t.id}" data-inline-rollback-reason aria-live="polite"></div>
      ${t.workspace_error ? `<div class="alert warning">工作目录：${e(t.workspace_error)}</div>` : ''}

      <dl class="kv">
        <dt>仓库</dt><dd class="mono">${e(t.repo_url)} <span class="dim">@${e(t.repo_branch)}</span>${t.repo_subdir ? ' / ' + e(t.repo_subdir) : ''}</dd>
        <dt>发布目录</dt><dd class="mono truncate" title="${a(t.releases_root || '')}">${e(t.releases_root || '—')}</dd>
        <dt>环境检查</dt><dd>${checks.map((c) =>
          `<span class="badge ${c.ok ? 'on' : 'failed'}" title="${a(c.message)}">${c.ok ? '✓' : '✗'} ${e(c.name)}</span>`).join(' ') || '<span class="faint">—</span>'}</dd>
      </dl>

      <div class="expand-log-head">
        <strong style="font-size:13px">执行日志</strong>
        <span class="faint" data-inline-logmeta></span>
        <div class="spacer"></div>
        <a class="faint" data-inline-download hidden>下载日志</a>
      </div>
      <div class="log-view" data-inline-log><span class="log-empty">${lastRun ? '加载中…' : '该任务还没有运行记录'}</span></div>

      <div class="section-title">最近运行</div>
      <div class="mini-runs">${renderInlineRuns(runs, taskId)}</div>
    `;

    // --- 工具条动作 -----------------------------------------------
    const root = placeholder.querySelector('.task-expand');
    const busyGuard = async (button, fn) => {
      AD.setBusy(button, true);
      try { await fn(); } catch (err) { AD.toastError(err.message); } finally { AD.setBusy(button, false); }
    };
    const inline = async (selector, fn) => {
      const button = root.querySelector(selector);
      if (!button) return;
      button.addEventListener('click', () => busyGuard(button, fn));
    };
    inline('[data-inline-cancel]', async () => {
      await AD.api.post(`/api/runs/${root.querySelector('[data-inline-cancel]').dataset.inlineCancel}/cancel`);
      AD.toastSuccess('已请求取消');
      AD.toggleTaskExpand(taskId, { forceOpen: true });
    });
    let selectedRun = null;
    let rollbackBusy = false;
    const previousButton = root.querySelector('[data-inline-rollback]');
    const selectedButton = root.querySelector('[data-inline-rollback-selected]');
    const rollbackReason = root.querySelector('[data-inline-rollback-reason]');
    const updateRollbackControls = () => {
      const active = runs.some((run) => ['queued', 'running'].includes(run.status));
      previousButton.disabled = rollbackBusy || active;
      selectedButton.disabled = rollbackBusy || active || !selectedRun
        || selectedRun.status !== 'success' || !selectedRun.release_dir;
      rollbackReason.textContent = active ? '任务正在运行或排队，暂不能回滚。'
        : !selectedRun ? '选择一条成功的执行记录后可回滚到对应版本。'
        : selectedRun.status !== 'success' || !selectedRun.release_dir
          ? `当前查看运行 #${selectedRun.id}，只有部署成功且保留发布目录的记录才可回滚。`
          : `回滚目标：运行 #${selectedRun.id}${selectedRun.commit_after ? ' · ' + AD.shortCommit(selectedRun.commit_after) : ''}。执行前会校验发布目录是否仍可用。`;
    };
    const requestRollback = async (button, run) => {
      if (button.disabled || rollbackBusy) return;
      rollbackBusy = true;
      updateRollbackControls();
      try {
        const ok = await AD.confirm({
          title: run ? `回滚到运行 #${run.id}` : '回滚到上一版本',
          message: run ? `将把 current 切换到运行 #${run.id} 的发布目录。` : '将把 current 切换到上一发布版本。',
          detail: '使用任务当前配置执行回滚脚本，systemd 方式还会重启服务。Docker / Compose 容器和 rsync 远端不会自动重新部署，需由回滚脚本处理；数据库不会自动恢复。',
          confirmText: '确认回滚',
          danger: true,
        });
        if (!ok || !root.isConnected) return;
        AD.setBusy(button, true);
        const result = await AD.api.post(`/api/tasks/${taskId}/rollback`, run ? { run_id: run.id } : undefined);
        AD.toastSuccess(result.message);
      } catch (err) { AD.toastError(err.message); }
      finally {
        AD.setBusy(button, false);
        rollbackBusy = false;
        updateRollbackControls();
      }
    };
    previousButton.addEventListener('click', () => requestRollback(previousButton, null));
    selectedButton.addEventListener('click', () => requestRollback(selectedButton, selectedRun ? { ...selectedRun } : null));
    root.querySelector('[data-inline-artifacts]')?.addEventListener('click', async () => {
      try {
        const artifacts = await AD.api.get(`/api/tasks/${taskId}/artifacts`);
        if (!artifacts.artifacts.length) { AD.toast('暂无打包产物', 'info'); return; }
        const list = artifacts.artifacts.map((item) =>
          `<li><a href="${a(item.url)}" download>${e(item.name)}</a>
           <span class="faint">${e(AD.formatBytes(item.size))} · ${e(AD.formatRelative(item.modified_at))}</span></li>`).join('');
        AD.Modal.open({
          title: '打包产物',
          size: 'narrow',
          body: `<ul class="release-list">${list}</ul>`,
          footer: '<button data-close>关闭</button>',
        });
      } catch (err) { AD.toastError(err.message); }
    });

    // --- 日志：实时轮询，与弹窗版同一套增量协议 ---------------------
    const logEl = root.querySelector('[data-inline-log]');
    const metaEl = root.querySelector('[data-inline-logmeta]');
    const download = root.querySelector('[data-inline-download]');
    const selectRun = (runId, scroll = false) => {
      selectedRun = runs.find((run) => run.id === runId) || null;
      AD.state.inlineRunSelection[taskId] = runId;
      download.hidden = !runId;
      if (runId) download.href = `/api/runs/${runId}/log?download=true`;
      root.querySelectorAll('[data-inline-run-log]').forEach((button) => {
        const current = Number(button.dataset.inlineRunLog) === runId;
        button.classList.toggle('active', current);
        button.setAttribute('aria-pressed', String(current));
        button.textContent = current ? '正在查看' : '详情';
      });
      updateRollbackControls();
      startInlineLog(taskId, runId, logEl, metaEl, (detail) => {
        if (AD.state.inlineRunSelection[taskId] !== runId) return;
        selectedRun = { ...selectedRun, ...detail };
        const row = runs.find((run) => run.id === runId);
        if (row) Object.assign(row, detail);
        updateRollbackControls();
      });
      if (scroll) logEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };
    root.querySelectorAll('[data-inline-run-log]').forEach((button) => {
      button.addEventListener('click', () => selectRun(Number(button.dataset.inlineRunLog), true));
    });
    const requested = options_.runId || (options_.forceOpen ? AD.state.inlineRunSelection[taskId] : null);
    const targetRunId = runs.some((run) => run.id === requested) ? requested
      : (activeRun ? activeRun.id : (lastRun ? lastRun.id : null));
    selectRun(targetRunId);
  };

  /** 下次执行的纯文本描述（与调度描述一起做去重，避免重复展示）。 */
  function nextRunInlineText(task) {
    if (!task.enabled) return '已禁用调度';
    if (!task.next_run_at) return '仅手动触发';
    const when = new Date(String(task.next_run_at).endsWith('Z') ? task.next_run_at : task.next_run_at + 'Z');
    const seconds = Math.max(0, Math.round((when.getTime() - Date.now()) / 1000));
    return `下次：${AD.formatTime(task.next_run_at)}（${AD.formatCountdown(seconds)}后）`;
  }

  /** 去除空值与重复项，保持原始顺序。 */
  function dedupeIn(items) {
    const seen = new Set();
    const out = [];
    items.forEach((item) => {
      const text = (item || '').trim();
      if (!text || seen.has(text)) return;
      seen.add(text);
      out.push(text);
    });
    return out;
  }

  function renderInlineRuns(runs, taskId) {
    if (!runs.length) return '<p class="faint">还没有运行记录；点击右上角「运行」开始第一次部署。</p>';
    return `<table><thead><tr>
        <th>#</th><th>状态</th><th>提交</th><th>触发</th><th>耗时</th><th>时间</th><th></th>
      </tr></thead><tbody>${runs.slice(0, 8).map((run) => `<tr>
        <td class="mono dim">${run.id}</td>
        <td><span class="badge ${e(run.status)}">${run.status === 'running' || run.status === 'queued' ? `<span class="dot ${e(run.status)}"></span>` : ''}${e(AD.statusLabel(run.status))}</span></td>
        <td class="mono dim truncate" style="max-width:200px" title="${a(run.commit_message || '')}">${run.commit_after ? e(AD.shortCommit(run.commit_after)) + ' ' + e(run.commit_message || '') : '—'}</td>
        <td class="dim">${e(AD.triggerLabel(run.trigger))}</td>
        <td class="dim nowrap">${e(AD.formatDuration(run.duration_ms))}</td>
        <td class="dim nowrap">${e(AD.formatRelative(run.queued_at))}</td>
        <td class="right"><button class="sm" data-inline-run-log="${run.id}" aria-pressed="false">详情</button></td>
      </tr>`).join('')}</tbody></table>
      <div class="faint" style="margin-top:6px">共 ${runs.length} 条记录</div>`;
  }

  // 会话令牌同时保护正在等待的请求，防止切换日志后旧响应覆盖新内容。
  AD.state.inlineLogTimers = {};
  AD.state.inlineRunSelection = {};
  const inlineLogSessions = {};

  AD.stopInlineLog = function (taskId) {
    const timer = AD.state.inlineLogTimers[taskId];
    if (timer) clearTimeout(timer);
    delete AD.state.inlineLogTimers[taskId];
    delete inlineLogSessions[taskId];
  };

  AD.stopAllInlineLogs = function () {
    Object.keys(inlineLogSessions).forEach((key) => AD.stopInlineLog(Number(key)));
  };

  function startInlineLog(taskId, runId, logEl, metaEl, onRun) {
    AD.stopInlineLog(taskId);
    logEl.innerHTML = `<span class="log-empty">${runId ? '加载中…' : '该任务还没有运行记录'}</span>`;
    metaEl.textContent = runId ? `运行 #${runId} · 加载中…` : '';
    if (!runId) return;
    const session = {};
    inlineLogSessions[taskId] = session;
    const alive = () => inlineLogSessions[taskId] === session && logEl.isConnected;
    let lineCount = 0;

    const paint = (lines, reset) => {
      const follow = reset || logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight <= 32;
      if (reset || logEl.querySelector('.log-empty')) logEl.innerHTML = '';
      logEl.insertAdjacentHTML('beforeend',
        lines.map((line) => `<div class="log-line">${colorize(line)}</div>`).join(''));
      while (logEl.childElementCount > 800) logEl.removeChild(logEl.firstChild);
      if (!logEl.childElementCount) logEl.innerHTML = '<span class="log-empty">暂无日志输出</span>';
      metaEl.textContent = `运行 #${runId} · 已读取 ${lineCount} 行（最多显示最近 800 行）`;
      if (follow) logEl.scrollTop = logEl.scrollHeight;
    };
    const fail = (err) => {
      if (!alive()) return;
      metaEl.textContent = `运行 #${runId} · 日志读取失败：${err.message}，点击详情重试`;
      if (!lineCount) logEl.innerHTML = '<span class="log-empty">日志读取失败，请点击该运行的详情重试。</span>';
      AD.stopInlineLog(taskId);
    };
    const schedule = () => {
      if (alive()) AD.state.inlineLogTimers[taskId] = setTimeout(poll, 1200);
    };
    const poll = async () => {
      if (!alive()) return;
      try {
        const result = await AD.api.get(`/api/runs/${runId}/tail?after=${lineCount}`);
        if (!alive()) return;
        lineCount = result.total;
        paint(result.lines || [], Boolean(result.reset));
        if (result.active) schedule();
        else {
          // 终态重新读取记录，取得部署结束时才写入的发布目录。
          const detail = await AD.api.get(`/api/runs/${runId}`);
          if (!alive()) return;
          onRun(detail.run);
          AD.stopInlineLog(taskId);
          refreshTaskRowInPlace(taskId);
        }
      } catch (err) { fail(err); }
    };
    (async () => {
      try {
        const detail = await AD.api.get(`/api/runs/${runId}`);
        if (!alive()) return;
        lineCount = (detail.run.log_tail || []).length;
        paint(detail.run.log_tail || [], true);
        onRun(detail.run);
        if (detail.run.is_active) schedule();
        else AD.stopInlineLog(taskId);
      } catch (err) { fail(err); }
    })();
  }

  /** 原地刷新任务行：轮询发现状态落定后更新图标与徽标，不重建整个表格。
   *  重建表格会连带销毁用户正在查看的行内展开，所以这里只改这一行。 */
  function refreshTaskRowInPlace(taskId) {
    AD.api.get('/api/tasks').then((data) => {
      const task = (data.tasks || []).find((item) => item.id === taskId);
      const row = document.querySelector(`tr[data-task-row="${taskId}"]`);
      if (!task || !row) return;
      const active = task.active_run;

      // 运行/停止图标
      const cell = row.querySelector('.table-actions');
      if (cell) {
        const runButton = active
          ? `<button class="icon-btn stop" data-task-cancel="${task.id}" title="停止当前运行" aria-label="停止当前运行">${ICONS.stop}</button>`
          : `<button class="icon-btn run" data-task-run="${task.id}" title="立即运行" aria-label="立即运行">${ICONS.play}</button>`;
        const control = cell.querySelector('[data-task-run], [data-task-cancel]');
        if (control) control.outerHTML = runButton;
      }

      // 状态徽标与计数：与新表格同一套渲染逻辑，避免两者显示不一致。
      const statusCell = row.querySelector('td:nth-child(2)');
      if (statusCell) {
        const statusBadge = active
          ? `<span class="badge ${e(active.status)}"><span class="dot ${e(active.status)}"></span>${e(AD.statusLabel(active.status))}</span>`
          : task.enabled
            ? '<span class="badge on">已启用</span>'
            : '<span class="badge off">已暂停</span>';
        const stats = task.run_count
          ? `<span class="faint">${task.success_count} 成功 / ${task.failure_count} 失败</span>`
          : '<span class="faint">尚未运行</span>';
        statusCell.innerHTML = `${statusBadge}<div class="faint" style="margin-top:3px">${stats}</div>`;
      }

      // 最近运行列
      const lastRunCell = row.querySelector('td:nth-child(5)');
      if (lastRunCell) {
        lastRunCell.innerHTML = task.last_status
          ? `<span class="badge ${e(task.last_status)}">${e(AD.statusLabel(task.last_status))}</span>
             <div class="faint">${e(AD.formatRelative(task.last_run_at))}</div>`
          : '<span class="faint">—</span>';
      }

      // 禁用行样式
      row.classList.toggle('row-disabled', !task.enabled);
    }).catch(() => { /* 下次轮询或手动刷新兜底 */ });
  }

  // ======================================================================
  // Task form (create / edit)
  // ======================================================================
  AD.openTaskForm = async function (taskId) {
    let task = null;
    if (taskId) {
      const data = await AD.api.get(`/api/tasks/${taskId}`);
      task = data.task;
    }
    if (!AD.state.defaults) {
      try { AD.state.defaults = await AD.api.get('/api/settings/defaults'); }
      catch (err) { AD.state.defaults = { repo_branch: 'main', timeout_seconds: 1800, keep_releases: 5 }; }
    }
    // 凭据下拉需要最新列表：在别处刚创建/删除后应立刻反映出来。
    try { AD.state.credentials = (await AD.api.get('/api/credentials')).credentials || []; }
    catch (err) { AD.state.credentials = AD.state.credentials || []; }
    const defaults = AD.state.defaults;
    const t = task || {};
    const isEdit = Boolean(taskId);

    const body = `
      <div id="form-error" class="alert error hidden"></div>

      <div class="section-title">基础信息</div>
      <div class="form-grid">
        <div class="field">
          <label for="f-name">任务名（唯一标识）<span class="req">*</span></label>
          <input type="text" id="f-name" value="${a(t.name || '')}" placeholder="例如：blog_frontend" aria-describedby="task-name-hint" spellcheck="false">
          <div id="task-name-hint" class="hint">全局唯一，不区分大小写；仅允许 1–80 个英文字母或下划线，不允许中文、数字、空格和连字符。任务名用于工作目录名称，中文说明请填写备注。${isEdit && !/^[A-Za-z_]{1,80}$/.test(t.name || '') ? ' 当前是旧版名称，可原样保留；修改名称时必须符合新规则。' : ''}</div>
        </div>
        <div class="field">
          <label>备注</label>
          <input type="text" id="f-description" value="${a(t.description || '')}" placeholder="可选">
        </div>
      </div>

      <div class="section-title">代码仓库</div>
      <div class="form-grid">
        <div class="field span-2">
          <label>仓库地址<span class="req">*</span></label>
          <input type="text" id="f-repo_url" value="${a(t.repo_url || '')}"
                 placeholder="https://github.com/owner/repo.git">
          <div class="hint">支持 https / ssh / git 协议；公开仓库无需凭证</div>
        </div>
        <div class="field">
          <label>分支<span class="req">*</span></label>
          <input type="text" id="f-repo_branch" value="${a(t.repo_branch || defaults.repo_branch || 'main')}">
        </div>
        <div class="field">
          <label>仓库子目录</label>
          <input type="text" id="f-repo_subdir" value="${a(t.repo_subdir || '')}" placeholder="例如 frontend/">
          <div class="hint">留空表示仓库根目录</div>
        </div>
        <div class="field">
          <label>拉取深度</label>
          <input type="number" id="f-git_depth" min="0" max="10000" value="${a(t.git_depth !== undefined ? t.git_depth : 1)}">
          <div class="hint">1 = 浅克隆（最快）；0 = 完整历史</div>
        </div>
        <div class="field span-2">
          <label>凭据</label>
          <select id="f-credential_id">
            <option value="">不使用（公开仓库或任务内令牌）</option>
            ${(AD.state.credentials || []).map((c) =>
              `<option value="${c.id}"${String(t.credential_id) === String(c.id) ? ' selected' : ''}>${e(c.name)}（${e(c.kind_label || c.kind)}）</option>`).join('')}
          </select>
          <div class="hint">
            在「凭据」页面集中管理；选择后优先于下方任务内令牌
            ${(AD.state.credentials || []).length ? '' : '（当前还没有凭据，可先到「凭据」页面创建）'}
          </div>
        </div>
        <div class="field">
          <label>任务内访问令牌（可选）</label>
          <input type="password" id="f-git_token" autocomplete="new-password"
                 placeholder="${isEdit ? (t.has_token ? '已配置，留空表示不修改' : '未配置') : '私有仓库才需要'}">
          <div class="hint">仅该任务使用；以 GIT_ASKPASS 传递，不会写入命令行或日志</div>
          ${isEdit && t.has_token ? '<div class="checkbox-row" style="margin-top:6px"><input type="checkbox" id="f-clear_token"><label for="f-clear_token">清除已保存的令牌</label></div>' : ''}
        </div>
      </div>

      <div class="section-title">调度</div>
      <div class="form-grid">
        <div class="field">
          <label>调度类型</label>
          <select id="f-schedule_type">
            <option value="interval"${(t.schedule_type || 'interval') === 'interval' ? ' selected' : ''}>固定间隔</option>
            <option value="cron"${t.schedule_type === 'cron' ? ' selected' : ''}>Cron 表达式</option>
            <option value="manual"${t.schedule_type === 'manual' ? ' selected' : ''}>仅手动触发</option>
          </select>
        </div>
        <div class="field">
          <label>表达式</label>
          <input type="text" id="f-schedule_expression"
                 value="${a(t.schedule_expression || defaults.schedule_defaults.interval)}"
                 placeholder="1h 或 0 */6 * * *">
          <div class="hint" id="schedule-hint">间隔格式：30s / 15m / 6h / 2d</div>
        </div>
        <div class="field span-2">
          <div id="schedule-preview" class="faint" style="min-height:20px"></div>
        </div>
      </div>
      <div class="checkbox-row">
        <input type="checkbox" id="f-enabled"${(t.enabled === undefined ? true : t.enabled) ? ' checked' : ''}>
        <label for="f-enabled">启用定时调度</label>
      </div>

      <div class="section-title">部署方式</div>
      <div class="field">
        <select id="f-deploy_method">
          ${STAGE_LABELS.map(([value, label, hint]) =>
            `<option value="${a(value)}"${(t.deploy_method || 'script') === value ? ' selected' : ''}>${e(label)} — ${e(hint)}</option>`).join('')}
        </select>
      </div>

      <div class="form-grid">
        <div class="field span-2">
          <label>打包路径</label>
          <textarea id="f-artifact_paths" rows="3" placeholder="dist&#10;package.json">${e(t.artifact_paths || '')}</textarea>
          <div class="hint">每行一条，支持通配符（如 <code class="code-inline">dist/**</code>）。留空则打包整个仓库</div>
        </div>
        <div class="field">
          <label>目标目录</label>
          <input type="text" id="f-target_dir" value="${a(t.target_dir || '')}" placeholder="/var/www/myapp">
          <div class="hint">发布目录与 current 软链会放在这里</div>
        </div>
        <div class="field">
          <label>保留版本数</label>
          <input type="number" id="f-keep_releases" min="0" max="1000" value="${a(t.keep_releases !== undefined ? t.keep_releases : 5)}">
          <div class="hint">超出后自动清理最旧的发布</div>
        </div>
      </div>

      <div class="form-grid">
        <div class="field span-2">
          <label>准备脚本</label>
          <textarea id="f-prepare_script" rows="4" placeholder="# 构建命令，例如&#10;npm ci&#10;npm run build">${e(t.prepare_script || '')}</textarea>
          <div class="hint">在源码目录执行，用于安装依赖与构建（对应 CI 的 build 阶段）</div>
        </div>
        <div class="field span-2">
          <label>部署脚本</label>
          <textarea id="f-deploy_script" rows="4" placeholder="# 发布后动作，例如&#10;systemctl restart myapp">${e(t.deploy_script || '')}</textarea>
          <div class="hint">在发布完成后执行；使用「自定义脚本」方式时必填</div>
        </div>
        <div class="field span-2">
          <label>回滚脚本</label>
          <textarea id="f-rollback_script" rows="3" placeholder="# 可选，回滚时执行">${e(t.rollback_script || '')}</textarea>
        </div>
      </div>

      <div class="form-grid">
        <div class="field" data-method-field="systemd">
          <label>systemd 服务名</label>
          <input type="text" id="f-service_name" value="${a(t.service_name || '')}" placeholder="myapp.service">
        </div>
        <div class="field" data-method-field="docker docker_compose">
          <label>镜像名称</label>
          <input type="text" id="f-docker_image" value="${a(t.docker_image || '')}" placeholder="myapp:latest">
        </div>
        <div class="field span-2" data-method-field="docker_compose">
          <label>Compose 文件</label>
          <input type="text" id="f-docker_compose_file" value="${a(t.docker_compose_file || '')}" placeholder="docker-compose.yml">
        </div>
        <div class="field span-2" data-method-field="docker">
          <label>容器启动命令</label>
          <textarea id="f-docker_command" rows="3" placeholder="docker rm -f myapp || true&#10;docker run -d --name myapp -p 8080:80 myapp:latest">${e(t.docker_command || '')}</textarea>
        </div>
        <div class="field span-2" data-method-field="rsync">
          <label>rsync 目标</label>
          <input type="text" id="f-rsync_target" value="${a(t.rsync_target || '')}" placeholder="user@host:/var/www/myapp/">
        </div>
        <div class="field" data-method-field="rsync">
          <label>rsync 参数</label>
          <input type="text" id="f-rsync_options" value="${a(t.rsync_options || '-az --delete')}">
        </div>
      </div>

      <div class="section-title">运行参数</div>
      <div class="form-grid">
        <div class="field">
          <label>超时时间（秒）</label>
          <input type="number" id="f-timeout_seconds" min="10" max="86400"
                 value="${a(t.timeout_seconds !== undefined ? t.timeout_seconds : (defaults.timeout_seconds || 1800))}">
        </div>
        <div class="field">
          <label>通知 Webhook</label>
          <input type="text" id="f-notify_webhook" value="${a(t.notify_webhook || '')}" placeholder="https://...">
        </div>
        <div class="field span-2">
          <label>环境变量</label>
          <textarea id="f-env_vars" rows="3" placeholder="NODE_ENV=production&#10;API_BASE=https://api.example.com">${e(envToText(t.env_vars))}</textarea>
          <div class="hint">每行一条 <code class="code-inline">KEY=value</code>，会导出到所有脚本</div>
        </div>
      </div>
      <div class="checkbox-row">
        <input type="checkbox" id="f-skip_if_no_changes"${(t.skip_if_no_changes === undefined ? true : t.skip_if_no_changes) ? ' checked' : ''}>
        <label for="f-skip_if_no_changes">代码无变化时跳过部署</label>
      </div>
      ${isEdit ? '' : `<div class="checkbox-row">
        <input type="checkbox" id="f-run_on_create">
        <label for="f-run_on_create">创建后立即运行一次</label>
      </div>`}
    `;

    const backdrop = AD.Modal.open({
      title: isEdit ? '编辑任务：' + t.name : '新建任务',
      size: 'wide',
      body,
      footerLeft: isEdit ? `<button class="danger" id="form-delete">删除任务</button>` : '<div class="left"></div>',
      footer: '<button data-close>取消</button>'
        + `<button class="primary" id="form-save">${isEdit ? '保存修改' : '创建任务'}</button>`,
    });

    const methodSelect = backdrop.querySelector('#f-deploy_method');
    const applyMethodVisibility = () => {
      const method = methodSelect.value;
      backdrop.querySelectorAll('[data-method-field]').forEach((field) => {
        const applies = field.dataset.methodField.split(' ').includes(method);
        field.classList.toggle('hidden', !applies);
      });
    };
    methodSelect.addEventListener('change', applyMethodVisibility);
    applyMethodVisibility();

    const typeSelect = backdrop.querySelector('#f-schedule_type');
    const exprInput = backdrop.querySelector('#f-schedule_expression');
    const hint = backdrop.querySelector('#schedule-hint');
    const preview = backdrop.querySelector('#schedule-preview');

    const refreshPreview = AD.debounce(async () => {
      const type = typeSelect.value;
      if (type === 'manual') {
        hint.textContent = '仅手动点击「运行」时执行';
        preview.textContent = '';
        return;
      }
      hint.textContent = type === 'interval'
        ? '间隔格式：30s / 15m / 6h / 2d（最小 30 秒）'
        : 'Cron 格式：分 时 日 月 周，例如 0 */6 * * *';
      try {
        const result = await AD.api.post('/api/settings/schedule/preview', {
          schedule_type: type, schedule_expression: exprInput.value, count: 5,
        });
        if (!result.ok) {
          preview.innerHTML = '<span style="color:var(--danger)">' + e(result.error) + '</span>';
        } else {
          preview.innerHTML = e(result.description) + ' · 接下来：'
            + result.next_runs.map((time) => e(AD.formatTime(time))).join(' → ');
        }
      } catch (err) { preview.textContent = ''; }
    }, 320);

    typeSelect.addEventListener('change', refreshPreview);
    exprInput.addEventListener('input', refreshPreview);
    refreshPreview();

    backdrop.querySelector('#form-save').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const payload = collectForm(backdrop, { isEdit });
      if ((!isEdit || payload.name !== t.name) && !/^[A-Za-z_]{1,80}$/.test(payload.name)) {
        showFormError(backdrop, '任务名只能包含 1–80 个英文字母或下划线，且必须全局唯一；中文说明请填写备注。');
        return;
      }
      if (!payload.repo_url) { showFormError(backdrop, '请填写仓库地址'); return; }

      AD.setBusy(button, true, isEdit ? '保存中…' : '创建中…');
      try {
        let result;
        if (isEdit) {
          result = await AD.api.put(`/api/tasks/${taskId}`, payload);
          AD.toastSuccess('任务已保存');
        } else {
          result = await AD.api.post('/api/tasks', payload);
          AD.toastSuccess(result.run_id ? ('任务已创建并开始运行 #' + result.run_id) : '任务已创建');
        }
        AD.Modal.close();
        AD.render('tasks');
      } catch (err) {
        showFormError(backdrop, err.message);
        AD.setBusy(button, false);
      }
    });

    const deleteButton = backdrop.querySelector('#form-delete');
    if (deleteButton) {
      deleteButton.addEventListener('click', async () => {
        const confirmed = await AD.confirm({
          title: '删除任务',
          message: `确定要删除「${t.name}」吗？`,
          detail: '工作目录与发布产物会一并删除；该操作不可撤销。',
          confirmText: '删除',
          danger: true,
        });
        if (!confirmed) { AD.openTaskForm(taskId); return; }
        try {
          await AD.api.del(`/api/tasks/${taskId}`);
          AD.toastSuccess('任务已删除');
          AD.Modal.close();
          AD.render('tasks');
        } catch (err) { AD.toastError(err.message); }
      });
    }
  };

  function showFormError(backdrop, message) {
    const box = backdrop.querySelector('#form-error');
    box.textContent = message;
    box.classList.remove('hidden');
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function envToText(env) {
    if (!env) return '';
    if (typeof env === 'string') return env;
    return Object.entries(env).map(([key, value]) => `${key}=${value}`).join('\n');
  }

  function collectForm(backdrop, options) {
    const value = (id) => {
      const node = backdrop.querySelector(id);
      return node ? node.value.trim() : '';
    };
    const checked = (id) => {
      const node = backdrop.querySelector(id);
      return node ? node.checked : false;
    };
    const payload = {
      name: backdrop.querySelector('#f-name').value,
      description: value('#f-description'),
      repo_url: value('#f-repo_url'),
      repo_branch: value('#f-repo_branch'),
      repo_subdir: value('#f-repo_subdir'),
      git_depth: Number(value('#f-git_depth') || 1),
      schedule_type: value('#f-schedule_type'),
      schedule_expression: value('#f-schedule_expression'),
      enabled: checked('#f-enabled'),
      deploy_method: value('#f-deploy_method'),
      artifact_paths: value('#f-artifact_paths'),
      target_dir: value('#f-target_dir'),
      keep_releases: Number(value('#f-keep_releases') || 0),
      prepare_script: backdrop.querySelector('#f-prepare_script').value,
      deploy_script: backdrop.querySelector('#f-deploy_script').value,
      rollback_script: backdrop.querySelector('#f-rollback_script').value,
      service_name: value('#f-service_name'),
      docker_image: value('#f-docker_image'),
      docker_command: backdrop.querySelector('#f-docker_command').value,
      docker_compose_file: value('#f-docker_compose_file'),
      rsync_target: value('#f-rsync_target'),
      rsync_options: value('#f-rsync_options'),
      env_vars: backdrop.querySelector('#f-env_vars').value,
      timeout_seconds: Number(value('#f-timeout_seconds') || 1800),
      notify_webhook: value('#f-notify_webhook'),
      skip_if_no_changes: checked('#f-skip_if_no_changes'),
    };

    const credentialNode = backdrop.querySelector('#f-credential_id');
    if (credentialNode) {
      payload.credential_id = credentialNode.value ? Number(credentialNode.value) : null;
    }

    const tokenNode = backdrop.querySelector('#f-git_token');
    if (tokenNode && tokenNode.value) payload.git_token = tokenNode.value;
    const clearNode = backdrop.querySelector('#f-clear_token');
    if (clearNode && clearNode.checked) payload.clear_token = true;

    const runOnCreate = backdrop.querySelector('#f-run_on_create');
    if (runOnCreate && !options.isEdit) payload.run_on_create = runOnCreate.checked;

    return payload;
  }

  // ======================================================================
  // Task detail
  // ======================================================================
  AD.openTaskDetail = async function (taskId) {
    const data = await AD.api.get(`/api/tasks/${taskId}`);
    const t = data.task;
    const runs = data.runs || [];
    const releases = data.releases || [];
    const checks = data.preflight || [];

    const body = `
      <div class="grid cols-3" style="margin-bottom:16px">
        <div class="stat">
          <span class="label">运行次数</span><span class="value">${t.run_count}</span>
          <span class="sub">${t.success_count} 成功 / ${t.failure_count} 失败</span>
        </div>
        <div class="stat ${t.last_status === 'success' ? 'success' : t.last_status === 'failed' ? 'danger' : ''}">
          <span class="label">最近状态</span>
          <span class="value" style="font-size:19px">${e(AD.statusLabel(t.last_status) || '—')}</span>
          <span class="sub">${e(AD.formatRelative(t.last_run_at))}</span>
        </div>
        <div class="stat">
          <span class="label">平均耗时</span>
          <span class="value" style="font-size:19px">${e(AD.formatDuration(t.run_count ? Math.round(t.total_duration_ms / t.run_count) : 0))}</span>
          <span class="sub">下次：${e(t.next_run_at ? AD.formatTime(t.next_run_at) : '未排期')}</span>
        </div>
      </div>

      <div class="section-title">配置</div>
      <dl class="kv">
        <dt>仓库</dt><dd class="mono">${e(t.repo_url)} <span class="dim">@${e(t.repo_branch)}</span>${t.repo_subdir ? ' / ' + e(t.repo_subdir) : ''}</dd>
        <dt>调度</dt><dd>${e(AD.scheduleText(t))}${t.enabled ? '' : ' <span class="badge off">已暂停</span>'}</dd>
        <dt>部署方式</dt><dd>${e(methodLabel(t.deploy_method))}</dd>
        <dt>发布目录</dt><dd class="mono">${e(t.releases_root || '')}</dd>
        <dt>当前版本</dt><dd class="mono">${e(t.current_link || '')}</dd>
        <dt>打包路径</dt><dd class="mono">${t.artifact_paths ? e(t.artifact_paths).replace(/\n/g, ', ') : '<span class="faint">整个仓库</span>'}</dd>
        <dt>超时</dt><dd>${e(t.timeout_seconds)} 秒</dd>
        <dt>环境检查</dt><dd>${checks.map((c) =>
            `<span class="badge ${c.ok ? 'on' : 'failed'}" title="${a(c.message)}">${c.ok ? '✓' : '✗'} ${e(c.name)}</span>`).join(' ') || '<span class="faint">—</span>'}</dd>
      </dl>

      <div class="section-title">发布版本（${releases.length}）</div>
      ${releases.length ? `<ul class="release-list">${releases.map((r) => `
        <li>
          <span class="mono truncate" style="flex:1">${e(r.name)}</span>
          ${r.is_current ? '<span class="badge on">当前</span>' : ''}
          <span class="faint">${e(AD.formatBytes(r.size))}</span>
          <span class="faint">${e(AD.formatRelative(r.modified_at))}</span>
        </li>`).join('')}</ul>`
        : '<p class="faint">还没有发布版本</p>'}

      <div class="section-title">最近运行</div>
      ${runs.length ? `<div class="table-wrap"><table>
        <thead><tr><th>#</th><th>状态</th><th>提交</th><th>触发</th><th>耗时</th><th>时间</th><th></th></tr></thead>
        <tbody>${runs.map((run) => `<tr>
          <td class="mono">${run.id}</td>
          <td><span class="badge ${e(run.status)}">${e(AD.statusLabel(run.status))}</span></td>
          <td class="mono dim truncate" style="max-width:220px" title="${a(run.commit_message)}">${e(AD.shortCommit(run.commit_after))} ${e(run.commit_message || '')}</td>
          <td class="dim">${e(AD.triggerLabel(run.trigger))}</td>
          <td class="dim nowrap">${e(AD.formatDuration(run.duration_ms))}</td>
          <td class="dim nowrap">${e(AD.formatRelative(run.queued_at))}</td>
          <td class="right"><button class="sm" data-run-log="${run.id}">日志</button></td>
        </tr>`).join('')}</tbody></table></div>`
        : '<p class="faint">还没有运行记录</p>'}
    `;

    const backdrop = AD.Modal.open({
      title: t.name,
      size: 'wide',
      body,
      footerLeft: `<button data-rollback>回滚上一版本</button>
                   <button data-download>下载产物</button>
                   <button data-export>导出配置</button>`,
      footer: '<button data-close>关闭</button>'
        + `<button data-run>立即运行</button>`
        + `<button class="primary" data-edit>编辑</button>`,
    });

    backdrop.querySelector('[data-run]').addEventListener('click', async (event) => {
      AD.setBusy(event.currentTarget, true);
      try {
        const result = await AD.api.post(`/api/tasks/${taskId}/run`);
        AD.toastSuccess('已开始运行 #' + result.run_id);
        AD.Modal.close();
        AD.openRunDetail(result.run_id);
      } catch (err) { AD.toastError(err.message); AD.setBusy(event.currentTarget, false); }
    });

    backdrop.querySelector('[data-edit]').addEventListener('click', () => {
      AD.Modal.close();
      AD.openTaskForm(taskId);
    });

    backdrop.querySelector('[data-download]').addEventListener('click', async (event) => {
      try {
        const data = await AD.api.get(`/api/tasks/${taskId}/artifacts`);
        if (!data.artifacts.length) { AD.toastError('暂无打包产物'); return; }
        const list = data.artifacts.map((item) =>
          `<li><a href="${a(item.url)}" download>${e(item.name)}</a>
           <span class="faint">${e(AD.formatBytes(item.size))} · ${e(AD.formatRelative(item.modified_at))}</span></li>`).join('');
        AD.Modal.open({
          title: '打包产物',
          size: 'narrow',
          body: `<ul class="release-list">${list}</ul>`,
          footer: '<button data-close>关闭</button>',
        });
      } catch (err) { AD.toastError(err.message); }
    });

    backdrop.querySelector('[data-export]').addEventListener('click', () => {
      window.location.href = `/api/tasks/${taskId}/export`;
    });

    backdrop.querySelector('[data-rollback]').addEventListener('click', async (event) => {
      const confirmed = await AD.confirm({
        title: '回滚到上一版本',
        message: '确定要把 current 软链切换到上一个发布版本吗？',
        detail: 'systemd 方式的任务会自动重启服务。',
        confirmText: '回滚',
        danger: true,
      });
      if (!confirmed) { AD.openTaskDetail(taskId); return; }
      AD.setBusy(event.currentTarget, true, '回滚中…');
      try {
        const result = await AD.api.post(`/api/tasks/${taskId}/rollback`);
        AD.toastSuccess(result.message);
        AD.Modal.close();
      } catch (err) {
        AD.toastError(err.message);
      }
    });

    backdrop.querySelectorAll('[data-run-log]').forEach((button) => {
      button.addEventListener('click', () => AD.openRunDetail(Number(button.dataset.runLog)));
    });
  };

  // ======================================================================
  // Runs list
  // ======================================================================
  const runFilter = { status: '', task_id: '', search: '' };

  AD.views.runs = async function (container) {
    const params = new URLSearchParams({ limit: '60' });
    if (runFilter.status) params.set('status', runFilter.status);
    if (runFilter.task_id) params.set('task_id', runFilter.task_id);
    if (runFilter.search) params.set('search', runFilter.search);
    const data = await AD.api.get('/api/runs?' + params.toString());

    if (!AD.state.tasks.length) {
      try { AD.state.tasks = (await AD.api.get('/api/tasks')).tasks || []; } catch (err) { /* ignore */ }
    }

    container.innerHTML = `
      <div class="view-header">
        <h1>运行记录</h1>
        <span class="badge neutral">${data.total} 条</span>
        <div class="spacer"></div>
        <button class="ghost sm" id="runs-refresh">刷新</button>
      </div>

      <div class="panel tight" style="margin-bottom:14px">
        <div class="row wrap">
          <div class="pill-group" id="status-filter">
            ${[{ value: '', label: '全部' }].concat(data.statuses || []).map((item) =>
              `<button class="sm${runFilter.status === item.value ? ' active' : ''}" data-status="${a(item.value)}">${e(item.label)}</button>`).join('')}
          </div>
          <select id="task-filter" style="width:200px">
            <option value="">全部任务</option>
            ${AD.state.tasks.map((task) =>
              `<option value="${task.id}"${String(runFilter.task_id) === String(task.id) ? ' selected' : ''}>${e(task.name)}</option>`).join('')}
          </select>
          <input type="text" id="run-search" placeholder="搜索提交信息或错误" style="width:220px"
                 value="${a(runFilter.search)}">
          ${(runFilter.status || runFilter.task_id || runFilter.search)
            ? '<button class="ghost sm" id="runs-clear">清除筛选</button>' : ''}
        </div>
      </div>

      <div class="panel">
        ${data.runs.length ? `<div class="table-wrap"><table>
          <thead><tr>
            <th>#</th><th>任务</th><th>状态</th><th>触发</th><th>提交</th>
            <th>变更</th><th>耗时</th><th>时间</th><th class="right">操作</th>
          </tr></thead>
          <tbody>${data.runs.map((run) => `<tr>
            <td class="mono dim">${run.id}</td>
            <td class="truncate" style="max-width:170px" title="${a(run.task_name)}">${e(run.task_name)}</td>
            <td><span class="badge ${e(run.status)}">${run.is_active ? `<span class="dot ${e(run.status)}"></span>` : ''}${e(AD.statusLabel(run.status))}</span></td>
            <td class="dim nowrap">${e(AD.triggerLabel(run.trigger))}</td>
            <td class="mono dim truncate" style="max-width:230px" title="${a(run.commit_message || '')}">
              ${run.commit_after ? e(AD.shortCommit(run.commit_after)) + ' ' + e(run.commit_message || '') : '<span class="faint">—</span>'}
            </td>
            <td class="dim nowrap">${run.changed_files ? run.changed_files + ' 个文件' : (run.changes_detected ? '—' : '无变化')}</td>
            <td class="dim nowrap">${e(AD.formatDuration(run.duration_ms))}</td>
            <td class="dim nowrap" title="${a(AD.formatTime(run.queued_at, true))}">${e(AD.formatRelative(run.queued_at))}</td>
            <td>
              <div class="table-actions">
                <button class="sm" data-run-log="${run.id}">日志</button>
                ${run.is_active ? `<button class="sm danger" data-run-cancel="${run.id}">取消</button>` : ''}
              </div>
            </td>
          </tr>`).join('')}</tbody>
        </table></div>`
        : `<div class="empty"><div class="big">📋</div><h3>没有匹配的运行记录</h3>
           <p class="faint">调整筛选条件，或先在任务页面手动运行一次</p></div>`}
      </div>
    `;

    container.querySelector('#runs-refresh').addEventListener('click', () => AD.render('runs'));

    container.querySelectorAll('#status-filter button').forEach((button) => {
      button.addEventListener('click', () => { runFilter.status = button.dataset.status; AD.render('runs'); });
    });

    const taskFilter = container.querySelector('#task-filter');
    taskFilter.addEventListener('change', () => { runFilter.task_id = taskFilter.value; AD.render('runs'); });

    const search = container.querySelector('#run-search');
    search.addEventListener('input', AD.debounce(() => { runFilter.search = search.value; AD.render('runs'); }, 320));

    const clear = container.querySelector('#runs-clear');
    if (clear) {
      clear.addEventListener('click', () => {
        runFilter.status = ''; runFilter.task_id = ''; runFilter.search = '';
        AD.render('runs');
      });
    }

    container.querySelectorAll('[data-run-log]').forEach((button) => {
      button.addEventListener('click', () => AD.openRunDetail(Number(button.dataset.runLog)));
    });

    container.querySelectorAll('[data-run-cancel]').forEach((button) => {
      button.addEventListener('click', async () => {
        AD.setBusy(button, true);
        try {
          await AD.api.post(`/api/runs/${button.dataset.runCancel}/cancel`);
          AD.toastSuccess('已请求取消');
          AD.render('runs');
        } catch (err) { AD.toastError(err.message); AD.setBusy(button, false); }
      });
    });
  };

  // ======================================================================
  // Run detail with live log
  // ======================================================================
  AD.openRunDetail = async function (runId) {
    AD.stopLiveLog();
    let data;
    try { data = await AD.api.get(`/api/runs/${runId}`); }
    catch (err) { AD.toastError(err.message); return; }
    const run = data.run;

    const body = `
      <div class="grid cols-4" style="margin-bottom:14px">
        <div class="stat"><span class="label">状态</span>
          <span class="value" style="font-size:18px" id="rd-status">${e(AD.statusLabel(run.status))}</span>
          <span class="sub" id="rd-active">${run.is_active ? '进行中…' : '已结束'}</span></div>
        <div class="stat"><span class="label">耗时</span>
          <span class="value" style="font-size:18px" id="rd-duration">${e(AD.formatDuration(run.duration_ms))}</span>
          <span class="sub" id="rd-exit">退出码 ${run.exit_code === null || run.exit_code === undefined ? '—' : run.exit_code}</span></div>
        <div class="stat"><span class="label">提交</span>
          <span class="value mono" style="font-size:15px">${e(AD.shortCommit(run.commit_after))}</span>
          <span class="sub">${e(run.branch || '')}</span></div>
        <div class="stat"><span class="label">触发方式</span>
          <span class="value" style="font-size:18px">${e(AD.triggerLabel(run.trigger))}</span>
          <span class="sub">${e(AD.formatTime(run.queued_at))}</span></div>
      </div>

      ${run.error ? `<div class="alert error"><strong>失败原因：</strong>${e(run.error)}</div>` : ''}

      <div class="panel tight" style="margin-bottom:14px">
        <div class="row wrap" style="gap:16px;font-size:12.5px">
          <span><span class="dim">开始：</span>${e(AD.formatTime(run.started_at, true) || '—')}</span>
          <span><span class="dim">结束：</span>${e(AD.formatTime(run.finished_at, true) || '—')}</span>
          <span><span class="dim">作者：</span>${e(run.commit_author || '—')}</span>
          <span><span class="dim">变更文件：</span>${run.changed_files || 0}</span>
        </div>
        ${run.commit_message ? `<div class="faint" style="margin-top:8px">提交信息：${e(run.commit_message)}</div>` : ''}
        ${run.release_dir ? `<div class="faint mono" style="margin-top:6px">发布目录：${e(run.release_dir)}</div>` : ''}
      </div>

      <div class="row" style="margin-bottom:8px">
        <strong style="font-size:13px">执行日志</strong>
        <span class="faint" id="rd-log-meta"></span>
        <div class="spacer"></div>
        <label class="row" style="gap:5px;font-size:12px;color:var(--text-dim)">
          <input type="checkbox" id="rd-follow" checked> 自动滚动
        </label>
        <button class="ghost sm" id="rd-copy">复制</button>
        <button class="ghost sm" id="rd-download">下载</button>
      </div>
      <div class="log-view" id="rd-log"><span class="log-empty">加载中…</span></div>
    `;

    const backdrop = AD.Modal.open({
      title: run.task_name + ' · 运行 #' + run.id,
      size: 'wide',
      body,
      footerLeft: run.is_active ? '<button class="danger" data-cancel-run>取消本次运行</button>' : '<div class="left"></div>',
      footer: '<button data-close>关闭</button>'
        + `<button data-task-detail>任务详情</button>`,
    });

    const logEl = backdrop.querySelector('#rd-log');
    const followEl = backdrop.querySelector('#rd-follow');
    const metaEl = backdrop.querySelector('#rd-log-meta');
    let lineCount = 0;
    let buffer = [];

    const paint = (lines, reset) => {
      if (reset) { buffer = []; logEl.innerHTML = ''; }
      if (!lines.length && !buffer.length) {
        logEl.innerHTML = '<span class="log-empty">暂无日志输出</span>';
        return;
      }
      buffer = buffer.concat(lines);
      const atBottom = followEl.checked;
      const html = lines.map((line) => `<div class="log-line">${colorize(line)}</div>`).join('');
      logEl.insertAdjacentHTML('beforeend', html);
      metaEl.textContent = '共 ' + buffer.length + ' 行';
      if (atBottom) logEl.scrollTop = logEl.scrollHeight;
    };

    // Initial content comes from the snapshot embedded in the run record.
    lineCount = (run.log_tail || []).length;
    paint(run.log_tail || [], true);

    const poll = async () => {
      try {
        const result = await AD.api.get(`/api/runs/${runId}/tail?after=${lineCount}`);
        if (result.reset) {
          lineCount = 0;
          paint(result.lines || [], true);
          lineCount = (result.lines || []).length;
          return;
        }
        if (result.lines && result.lines.length) {
          paint(result.lines, false);
          lineCount = result.total;
        }
        const statusEl = backdrop.querySelector('#rd-status');
        if (statusEl) statusEl.textContent = result.status_label;
        const exitEl = backdrop.querySelector('#rd-exit');
        if (exitEl) exitEl.textContent = '退出码 ' + (result.exit_code === null || result.exit_code === undefined ? '—' : result.exit_code);
        const durationEl = backdrop.querySelector('#rd-duration');
        if (durationEl) durationEl.textContent = AD.formatDuration(result.duration_ms);
        const activeEl = backdrop.querySelector('#rd-active');
        if (activeEl) activeEl.textContent = result.active ? '进行中…' : '已结束';

        if (!result.active) {
          AD.stopLiveLog();
          const badge = backdrop.querySelector('#rd-status');
          if (badge) badge.style.color = result.status === 'success' ? 'var(--success)'
            : result.status === 'failed' ? 'var(--danger)' : '';
          // Refresh the underlying list so the table reflects the final state.
          if (AD.state.currentView === 'runs' || AD.state.currentView === 'tasks') AD.render(AD.state.currentView);
        }
      } catch (err) {
        AD.stopLiveLog();
      }
    };

    AD.state.currentRunId = runId;
    AD.state.liveTimer = setInterval(poll, 1200);
    setTimeout(poll, 250);

    backdrop.querySelector('#rd-copy').addEventListener('click', () => AD.copyToClipboard(buffer.join('\n')));
    backdrop.querySelector('#rd-download').addEventListener('click', () => {
      window.location.href = `/api/runs/${runId}/log?download=true`;
    });
    backdrop.querySelector('[data-task-detail]').addEventListener('click', () => {
      AD.stopLiveLog();
      AD.Modal.close();
      AD.openTaskDetail(run.task_id);
    });
    const cancelButton = backdrop.querySelector('[data-cancel-run]');
    if (cancelButton) {
      cancelButton.addEventListener('click', async () => {
        AD.setBusy(cancelButton, true, '取消中…');
        try {
          await AD.api.post(`/api/runs/${runId}/cancel`);
          AD.toastSuccess('已请求取消，进程会在安全点停止');
        } catch (err) { AD.toastError(err.message); }
        AD.setBusy(cancelButton, false);
      });
    }
    // Stop polling when the modal is dismissed.
    const observer = new MutationObserver(() => {
      if (!document.getElementById('modal-root').firstChild) { AD.stopLiveLog(); observer.disconnect(); }
    });
    observer.observe(document.getElementById('modal-root'), { childList: true });
  };

  AD.stopLiveLog = function () {
    if (AD.state.liveTimer) { clearInterval(AD.state.liveTimer); AD.state.liveTimer = null; }
    AD.state.currentRunId = null;
  };

  /** Colour-code log lines by their leading marker. */
  function colorize(line) {
    const escaped = e(line);
    if (/^\$ /.test(line)) return '<span class="l-cmd">' + escaped + '</span>';
    if (/^! /.test(line)) return '<span class="l-err">' + escaped + '</span>';
    if (/^✓ /.test(line)) return '<span class="l-ok">' + escaped + '</span>';
    if (/^--- /.test(line)) return '<span class="l-stage">' + escaped + '</span>';
    if (/^[=-]{10,}$/.test(line)) return '<span class="l-sep">' + escaped + '</span>';
    if (/^结果: /.test(line)) return '<span class="l-ok">' + escaped + '</span>';
    return escaped;
  }

  // ======================================================================
  // Statistics
  // ======================================================================
  AD.views.stats = async function (container) {
    const [stats, storage] = await Promise.all([
      AD.api.get('/api/stats?days=30'),
      AD.api.get('/api/storage'),
    ]);
    const o = stats.overview || {};

    container.innerHTML = `
      <div class="view-header">
        <h1>统计</h1>
        <div class="spacer"></div>
        <button class="ghost sm" id="stats-refresh">刷新</button>
      </div>

      <div class="grid cols-4" style="margin-bottom:16px">
        <div class="stat"><span class="label">总运行次数</span><span class="value">${o.total}</span>
          <span class="sub">跳过 ${o.skipped} 次</span></div>
        <div class="stat success"><span class="label">成功</span><span class="value">${o.success}</span>
          <span class="sub">成功率 ${o.success_rate}%</span></div>
        <div class="stat danger"><span class="label">失败</span><span class="value">${o.failed}</span>
          <span class="sub">取消 ${o.cancelled} 次</span></div>
        <div class="stat accent"><span class="label">平均耗时</span>
          <span class="value" style="font-size:21px">${e(AD.formatDuration(Math.round(o.avg_duration_ms || 0)))}</span>
          <span class="sub">累计 ${e(AD.formatDuration(o.total_duration_ms || 0))}</span></div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>每日运行（近 30 天）</h2></div>
        ${renderChart(stats.daily || [])}
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>任务排行</h2></div>
          ${renderTaskRanking(stats.by_task || [])}
        </div>
        <div class="panel">
          <div class="panel-head"><h2>存储占用</h2>
            <div class="spacer"></div>
            <button class="ghost sm" id="run-maintenance">立即清理</button>
          </div>
          <dl class="kv">
            <dt>数据目录</dt><dd class="mono">${e(storage.data_dir || '')}</dd>
            <dt>工作副本</dt><dd>${e(AD.formatBytes(storage.workspaces_bytes))}</dd>
            <dt>发布版本</dt><dd>${e(AD.formatBytes(storage.releases_bytes))}</dd>
            <dt>打包产物</dt><dd>${e(AD.formatBytes(storage.artifacts_bytes))}</dd>
            <dt>运行日志</dt><dd>${e(AD.formatBytes(storage.logs_bytes))}</dd>
            <dt>合计</dt><dd><strong>${e(AD.formatBytes(storage.total_bytes))}</strong></dd>
            <dt>磁盘剩余</dt><dd>${e(AD.formatBytes((storage.disk || {}).free))}
              / ${e(AD.formatBytes((storage.disk || {}).total))}</dd>
            <dt>保留策略</dt><dd>日志保留 ${storage.run_retention_days} 天，
              每任务保留 ${storage.release_retention_count} 个版本</dd>
          </dl>
        </div>
      </div>
    `;

    container.querySelector('#stats-refresh').addEventListener('click', () => AD.render('stats'));
    container.querySelector('#run-maintenance').addEventListener('click', async (event) => {
      AD.setBusy(event.currentTarget, true, '清理中…');
      try {
        const result = await AD.api.post('/api/maintenance/run');
        const r = result.report || {};
        AD.toastSuccess(`清理完成：${r.runs || 0} 条记录、${r.releases || 0} 个旧版本、${r.sessions || 0} 个会话`);
        AD.render('stats');
      } catch (err) { AD.toastError(err.message); AD.setBusy(event.currentTarget, false); }
    });
  };

  function renderTaskRanking(items) {
    if (!items.length) return '<div class="empty">暂无数据</div>';
    return '<div class="table-wrap"><table><thead><tr>'
      + '<th>任务</th><th>运行</th><th>成功</th><th>失败</th><th>最近状态</th></tr></thead><tbody>'
      + items.map((item) => `<tr>
          <td class="truncate" style="max-width:180px">${e(item.name)}</td>
          <td>${item.run_count}</td>
          <td class="dim">${item.success_count}</td>
          <td class="dim">${item.failure_count}</td>
          <td>${item.last_status ? `<span class="badge ${e(item.last_status)}">${e(AD.statusLabel(item.last_status))}</span>` : '<span class="faint">—</span>'}</td>
        </tr>`).join('')
      + '</tbody></table></div>';
  }

  // ======================================================================
  // Settings
  // ======================================================================
  AD.views.settings = async function (container) {
    const [data, system, audit, sessions] = await Promise.all([
      AD.api.get('/api/settings'),
      AD.api.get('/api/system'),
      AD.api.get('/api/audit?limit=40'),
      AD.api.get('/api/sessions'),
    ]);
    AD.state.settings = data.settings;
    const s = data.settings;

    container.innerHTML = `
      <div class="view-header"><h1>设置</h1></div>

      <div class="tabs" id="settings-tabs">
        <button class="tab active" data-tab="general">常规</button>
        <button class="tab" data-tab="account">账号安全</button>
        <button class="tab" data-tab="system">系统信息</button>
        <button class="tab" data-tab="audit">操作日志</button>
      </div>

      <div id="settings-panel"></div>
    `;

    const panel = container.querySelector('#settings-panel');
    const tabs = {
      general: () => renderGeneralPanel(panel, data),
      account: () => renderAccountPanel(panel, sessions, system),
      system: () => { renderSystemPanel(panel, system, data); AD.renderUpdatePanel(panel.querySelector('#update-panel')); },
      audit: () => renderAuditPanel(panel, audit),
    };
    tabs.general();

    container.querySelectorAll('#settings-tabs .tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        container.querySelectorAll('#settings-tabs .tab').forEach((other) => other.classList.remove('active'));
        tab.classList.add('active');
        tabs[tab.dataset.tab]();
      });
    });
  };

  function renderGeneralPanel(panel, data) {
    const s = data.settings;
    const num = (key, label, hint, min, max) => `
      <div class="field">
        <label>${e(label)}</label>
        <input type="number" id="s-${a(key)}" min="${min}" max="${max}" value="${a(s[key])}">
        <div class="hint">${e(hint)}${min !== undefined ? `（${min} ~ ${max}）` : ''}</div>
      </div>`;

    panel.innerHTML = `
      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>调度与并发</h2></div>
          ${num('poll_interval_seconds', '调度轮询间隔（秒）', '调度器检查到期任务的频率')}
          ${num('max_global_workers', '并发部署上限', '同时运行的部署数量，超出后排队')}
          <div class="checkbox-row">
            <input type="checkbox" id="s-queue_while_running"${s.queue_while_running ? ' checked' : ''}>
            <label for="s-queue_while_running">任务运行中仍排队下一次触发</label>
          </div>
          <div class="hint">不勾选时，任务运行期间到达的调度点会被跳过（推荐）</div>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>执行</h2></div>
          ${num('default_timeout_seconds', '默认超时（秒）', '单个部署的总超时')}
          ${num('git_timeout_seconds', 'Git 操作超时（秒）', '克隆与拉取的超时')}
          ${num('kill_grace_seconds', '停止宽限期（秒）', 'SIGTERM 后等待多久强杀')}
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>数据保留</h2></div>
          ${num('run_retention_days', '运行记录保留天数', '0 表示不按时间清理')}
          ${num('run_retention_count', '运行记录保留条数', '0 表示不按条数清理')}
          ${num('release_retention_count', '发布版本保留数', '每个任务保留的发布目录数量')}
          ${num('artifact_retention_count', '打包产物保留数', '每个任务保留的压缩包数量')}
          ${num('log_max_bytes', '单次运行日志上限（字节）', '超出后截断，避免占满磁盘')}
        </div>

        <div class="panel">
          <div class="panel-head"><h2>安全与通知</h2></div>
          ${num('session_ttl_hours', '登录有效期（小时）', '会话过期后需要重新登录')}
          ${num('login_max_attempts', '登录失败上限', '超过后临时锁定')}
          ${num('login_lockout_seconds', '锁定时长（秒）', '锁定期间拒绝登录')}
          <div class="field">
            <label>全局通知 Webhook</label>
            <input type="text" id="s-notify_webhook" value="${a(s.notify_webhook || '')}"
                   placeholder="https://hooks.example.com/deploy">
            <div class="hint">任务未单独配置时使用；每次运行结束 POST 一条 JSON</div>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="s-trust_proxy_headers"${s.trust_proxy_headers ? ' checked' : ''}>
            <label for="s-trust_proxy_headers">信任反向代理的 X-Forwarded-For</label>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="s-secure_cookies"${s.secure_cookies ? ' checked' : ''}>
            <label for="s-secure_cookies">仅通过 HTTPS 发送会话 Cookie</label>
          </div>
          <div class="hint">仅在使用 HTTPS 时勾选，否则将无法登录</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>网络代理</h2>
          <div class="spacer"></div>
          <span class="faint">当前：${e(data.proxy_description || '未启用')}</span>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" id="s-proxy_enabled"${s.proxy_enabled ? ' checked' : ''}>
          <label for="s-proxy_enabled">为 Git 操作启用代理</label>
        </div>
        <div class="hint" style="margin-bottom:12px">
          内网或跨境访问 GitHub 不稳定时启用。代理以环境变量下发给 git，
          不会写入进程命令行，日志中的密码亦会被脱敏。
        </div>
        <div class="grid cols-2">
          <div class="field">
            <label>代理地址</label>
            <input type="text" id="s-proxy_url" value="${a(s.proxy_url || '')}"
                   placeholder="http://proxy.corp.local:8080 或 socks5://127.0.0.1:1080">
            <div class="hint">支持 http / https / socks5</div>
          </div>
          <div class="field">
            <label>不使用代理的地址（no_proxy）</label>
            <input type="text" id="s-proxy_no_proxy" value="${a(s.proxy_no_proxy || '')}"
                   placeholder="localhost,127.0.0.1,.corp.local">
            <div class="hint">逗号分隔；内网仓库应在此列出，否则会绕经代理而失败</div>
          </div>
          <div class="field">
            <label>代理用户名</label>
            <input type="text" id="s-proxy_username" value="${a(s.proxy_username || '')}" placeholder="可选">
          </div>
          <div class="field">
            <label>代理密码</label>
            <input type="password" id="s-proxy_password" autocomplete="new-password"
                   placeholder="${s.proxy_password_set ? '已设置，留空表示不修改' : '可选'}">
          </div>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" id="s-proxy_for_scripts"${s.proxy_for_scripts ? ' checked' : ''}>
          <label for="s-proxy_for_scripts">构建脚本也使用该代理</label>
        </div>
        <div class="hint">勾选后 prepare/deploy 脚本中的 npm、pip 等也能联网；内网构建源通常不需要</div>
      </div>

      <div class="panel">
        <div class="row">
          <button class="primary" id="settings-save">保存设置</button>
          <button id="settings-reload">重新载入</button>
          <div class="spacer"></div>
          <span class="faint">配置文件：<span class="mono">${e(data.config_path)}</span></span>
        </div>
        <div id="settings-result" style="margin-top:12px"></div>
      </div>
    `;

    panel.querySelector('#settings-save').addEventListener('click', async (event) => {
      const read = (id) => {
        const node = panel.querySelector(id);
        return node ? Number(node.value) : undefined;
      };
      const payload = {
        poll_interval_seconds: read('#s-poll_interval_seconds'),
        max_global_workers: read('#s-max_global_workers'),
        default_timeout_seconds: read('#s-default_timeout_seconds'),
        git_timeout_seconds: read('#s-git_timeout_seconds'),
        kill_grace_seconds: read('#s-kill_grace_seconds'),
        run_retention_days: read('#s-run_retention_days'),
        run_retention_count: read('#s-run_retention_count'),
        release_retention_count: read('#s-release_retention_count'),
        artifact_retention_count: read('#s-artifact_retention_count'),
        log_max_bytes: read('#s-log_max_bytes'),
        session_ttl_hours: read('#s-session_ttl_hours'),
        login_max_attempts: read('#s-login_max_attempts'),
        login_lockout_seconds: read('#s-login_lockout_seconds'),
        notify_webhook: panel.querySelector('#s-notify_webhook').value.trim(),
        queue_while_running: panel.querySelector('#s-queue_while_running').checked,
        trust_proxy_headers: panel.querySelector('#s-trust_proxy_headers').checked,
        secure_cookies: panel.querySelector('#s-secure_cookies').checked,
        proxy_enabled: panel.querySelector('#s-proxy_enabled').checked,
        proxy_url: panel.querySelector('#s-proxy_url').value.trim(),
        proxy_username: panel.querySelector('#s-proxy_username').value.trim(),
        proxy_no_proxy: panel.querySelector('#s-proxy_no_proxy').value.trim(),
        proxy_for_scripts: panel.querySelector('#s-proxy_for_scripts').checked,
      };
      // 密码留空表示保持原值，因此只有填了才提交。
      const proxyPassword = panel.querySelector('#s-proxy_password').value;
      if (proxyPassword) payload.proxy_password = proxyPassword;
      Object.keys(payload).forEach((key) => { if (payload[key] === undefined) delete payload[key]; });

      AD.setBusy(event.currentTarget, true, '保存中…');
      try {
        const result = await AD.api.put('/api/settings', payload);
        const box = panel.querySelector('#settings-result');
        box.innerHTML = '<div class="alert success">设置已保存'
          + (result.restart_required ? '；部分设置需要重启服务后生效' : '') + '</div>';
        AD.toastSuccess('设置已保存');
        AD.setBusy(event.currentTarget, false);
      } catch (err) {
        panel.querySelector('#settings-result').innerHTML =
          '<div class="alert error">' + e(err.message) + '</div>';
        AD.setBusy(event.currentTarget, false);
      }
    });

    panel.querySelector('#settings-reload').addEventListener('click', () => AD.render('settings'));
  }

  function renderAccountPanel(panel, sessions, system) {
    const list = sessions.sessions || [];
    panel.innerHTML = `
      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>修改密码</h2></div>
          <div id="pwd-result"></div>
          <div class="field">
            <label>当前密码</label>
            <input type="password" id="pwd-current" autocomplete="current-password">
          </div>
          <div class="field">
            <label>新密码</label>
            <input type="password" id="pwd-new" autocomplete="new-password">
            <div class="hint">至少 8 位，需包含大小写字母、数字或符号中的两类</div>
          </div>
          <div class="field">
            <label>确认新密码</label>
            <input type="password" id="pwd-confirm" autocomplete="new-password">
          </div>
          <button class="primary" id="pwd-save">更新密码</button>
          <div class="hint" style="margin-top:8px">修改后其他设备的登录会立即失效</div>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>登录会话</h2>
            <div class="spacer"></div>
            <button class="ghost sm" id="revoke-others">退出其他会话</button>
          </div>
          <ul class="release-list">${list.map((item) => `
            <li>
              <span class="mono">${e(item.ip || '未知地址')}</span>
              <span class="truncate faint" style="flex:1" title="${a(item.user_agent)}">${e(shortAgent(item.user_agent))}</span>
              ${item.is_current ? '<span class="badge on">当前</span>' : ''}
              <span class="faint nowrap">${e(AD.formatRelative(item.created_at))}</span>
            </li>`).join('')}
          </ul>
        </div>
      </div>
    `;

    panel.querySelector('#pwd-save').addEventListener('click', async (event) => {
      const body = {
        current_password: panel.querySelector('#pwd-current').value,
        new_password: panel.querySelector('#pwd-new').value,
        confirm_password: panel.querySelector('#pwd-confirm').value,
      };
      AD.setBusy(event.currentTarget, true, '更新中…');
      try {
        const result = await AD.api.post('/api/auth/password', body);
        panel.querySelector('#pwd-result').innerHTML =
          '<div class="alert success">' + e(result.message) + '</div>';
        panel.querySelector('#pwd-current').value = '';
        panel.querySelector('#pwd-new').value = '';
        panel.querySelector('#pwd-confirm').value = '';
        AD.toastSuccess('密码已更新');
      } catch (err) {
        panel.querySelector('#pwd-result').innerHTML =
          '<div class="alert error">' + e(err.message) + '</div>';
      }
      AD.setBusy(event.currentTarget, false);
    });

    panel.querySelector('#revoke-others').addEventListener('click', async (event) => {
      AD.setBusy(event.currentTarget, true);
      try {
        const result = await AD.api.post('/api/sessions/revoke-others');
        AD.toastSuccess('已退出 ' + result.revoked + ' 个其他会话');
        AD.render('settings');
      } catch (err) { AD.toastError(err.message); AD.setBusy(event.currentTarget, false); }
    });
  }

  function shortAgent(agent) {
    const text = String(agent || '未知客户端');
    if (text.length <= 44) return text;
    return text.slice(0, 44) + '…';
  }

  function renderSystemPanel(panel, system, data) {
    const s = data.settings;
    panel.innerHTML = `
      <div class="panel" id="update-panel">
        <div class="panel-head"><h2>版本更新</h2>
          <div class="spacer"></div>
          <span class="faint">程序代码更新</span>
        </div>
        <div id="upd-body">
          <div class="row">
            <button class="primary" id="upd-check">检查更新</button>
            <span class="faint" id="upd-hint">查询远程仓库的最新发布版本</span>
          </div>
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>运行环境</h2></div>
          <dl class="kv">
            <dt>版本</dt><dd>${e(system.version)}</dd>
            <dt>Python</dt><dd>${e(system.python)}</dd>
            <dt>系统</dt><dd>${e(system.platform)}</dd>
            <dt>主机名</dt><dd>${e(system.hostname)}</dd>
            <dt>进程 ID</dt><dd class="mono">${system.pid}</dd>
            <dt>数据目录</dt><dd class="mono">${e(system.data_dir)}</dd>
            <dt>数据库</dt><dd class="mono">${e(system.db_path)}
              <span class="faint">（${e(AD.formatBytes(system.db_size))}）</span></dd>
          </dl>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>依赖命令</h2></div>
          <ul class="release-list">${Object.entries(system.binaries || {}).map(([name, path]) => `
            <li>
              <span class="mono" style="flex:1">${e(name)}</span>
              ${path ? `<span class="badge on">✓</span><span class="faint mono truncate" style="max-width:220px">${e(path)}</span>`
                     : '<span class="badge off">未安装</span>'}
            </li>`).join('')}
          </ul>
          <div class="hint" style="margin-top:10px">
            部署脚本、Docker、systemd、rsync 等能力取决于对应命令是否存在。
          </div>
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <div class="panel-head"><h2>磁盘与调度</h2></div>
          <dl class="kv">
            <dt>磁盘总量</dt><dd>${e(AD.formatBytes(system.disk.total))}</dd>
            <dt>已使用</dt><dd>${e(AD.formatBytes(system.disk.used))}</dd>
            <dt>剩余</dt><dd>${e(AD.formatBytes(system.disk.free))}</dd>
            <dt>调度器</dt><dd>${system.scheduler.running ? '<span class="badge on">运行中</span>' : '<span class="badge failed">已停止</span>'}</dd>
            <dt>并发占用</dt><dd>${system.scheduler.active_runs} / ${system.scheduler.max_global_workers}</dd>
          </dl>
        </div>

        <div class="panel">
          <div class="panel-head"><h2>服务器配置</h2></div>
          <dl class="kv">
            <dt>监听地址</dt><dd class="mono">${e(s.host)}:${e(s.port)}</dd>
            <dt>Shell</dt><dd class="mono">${e(s.shell)}</dd>
            <dt>时区</dt><dd>${e(s.timezone)}</dd>
            <dt>默认分支</dt><dd class="mono">${e(s.default_branch)}</dd>
          </dl>
          <div class="hint" style="margin-top:10px">
            监听地址与端口通过命令行参数 <span class="code-inline">--host/--port</span> 或环境变量
            <span class="code-inline">AUTODEPLOY_HOST/AUTODEPLOY_PORT</span> 配置。
          </div>
          <div class="divider"></div>
          <div class="row">
            <button id="export-all">导出全部数据</button>
            <button id="export-audit">导出操作日志</button>
          </div>
        </div>
      </div>
    `;

    panel.querySelector('#export-all').addEventListener('click', () => { window.location.href = '/api/export'; });
    panel.querySelector('#export-audit').addEventListener('click', () => { window.location.href = '/api/audit/export'; });
  }

  function renderAuditPanel(panel, audit) {
    const entries = audit.entries || [];
    const ACTION_LABELS = {
      login_success: '登录成功', login_failed: '登录失败', login_locked: '登录被锁定',
      logout: '退出登录', password_changed: '修改密码', password_change_failed: '修改密码失败',
      admin_bootstrapped: '初始化管理员', task_created: '创建任务', task_updated: '更新任务',
      task_deleted: '删除任务', task_toggled: '启停任务', task_files_cleaned: '清理任务文件',
      run_triggered: '触发运行', run_dispatched: '调度运行', run_cancelled: '取消运行',
      run_deleted: '删除记录', run_interrupted: '运行中断', rollback: '回滚',
      settings_updated: '更新设置', maintenance_run: '执行清理', sessions_revoked: '撤销会话',
    };
    panel.innerHTML = `
      <div class="panel">
        <div class="panel-head"><h2>操作日志</h2>
          <div class="spacer"></div>
          <span class="faint">最近 ${entries.length} 条</span>
        </div>
        ${entries.length ? `<div class="table-wrap"><table>
          <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th><th>来源 IP</th></tr></thead>
          <tbody>${entries.map((entry) => `<tr>
            <td class="dim nowrap">${e(AD.formatTime(entry.ts, true))}</td>
            <td>${e(entry.actor || '系统')}</td>
            <td>${e(ACTION_LABELS[entry.action] || entry.action)}</td>
            <td class="mono dim">${e(entry.target || '—')}</td>
            <td class="truncate" style="max-width:280px" title="${a(entry.detail)}">${e(entry.detail || '—')}</td>
            <td class="mono dim">${e(entry.ip || '—')}</td>
          </tr>`).join('')}</tbody>
        </table></div>` : '<div class="empty">暂无操作记录</div>'}
      </div>
    `;
  }

  // ======================================================================
  // 凭据管理
  // ======================================================================
  AD.views.credentials = async function (container) {
    const data = await AD.api.get('/api/credentials');
    const items = data.credentials || [];

    container.innerHTML = `
      <div class="view-header">
        <h1>凭据</h1>
        <span class="badge neutral">${items.length} 个</span>
        <div class="spacer"></div>
        <button class="primary" id="cred-new">+ 新建凭据</button>
      </div>

      <div class="alert info">
        凭据集中管理后，多个任务可复用同一份令牌，轮换时只需改一处。
        密钥内容仅在保存时提交，之后不会再回传到浏览器。
      </div>

      ${items.length ? `<div class="panel"><div class="table-wrap"><table>
        <thead><tr>
          <th>名称</th><th>类型</th><th>账号 / 指纹</th><th>可用性</th><th>被引用</th><th class="right">操作</th>
        </tr></thead>
        <tbody>${items.map(credentialRow).join('')}</tbody>
      </table></div></div>`
      : `<div class="panel"><div class="empty">
           <div class="big">🔑</div><h3>还没有凭据</h3>
           <p class="faint">私有仓库或 SSH 地址需要凭据；公开仓库可以跳过</p>
           <button class="primary" id="cred-new-empty">+ 新建凭据</button>
         </div></div>`}
    `;

    container.querySelector('#cred-new')?.addEventListener('click', () => AD.openCredentialForm(null));
    container.querySelector('#cred-new-empty')?.addEventListener('click', () => AD.openCredentialForm(null));

    container.querySelectorAll('[data-cred-edit]').forEach((b) =>
      b.addEventListener('click', () => AD.openCredentialForm(Number(b.dataset.credEdit))));
    container.querySelectorAll('[data-cred-test]').forEach((b) =>
      b.addEventListener('click', () => AD.testCredential(Number(b.dataset.credTest))));
    container.querySelectorAll('[data-cred-delete]').forEach((b) =>
      b.addEventListener('click', () => AD.deleteCredential(Number(b.dataset.credDelete))));
  };

  function credentialRow(item) {
    const tested = item.last_tested_at
      ? (item.last_test_ok
          ? `<span class="badge on">可用</span>`
          : `<span class="badge failed">不可用</span>`)
        + `<div class="faint" title="${a(item.last_test_error || '')}">${e(AD.formatRelative(item.last_tested_at))}</div>`
      : '<span class="faint">未测试</span>';

    const identity = item.kind === 'ssh_key'
      ? `<span class="mono" style="font-size:11px">${e(item.fingerprint || '（无指纹）')}</span>
         <div class="faint">用户 ${e(item.username || 'git')}</div>`
      : `<span class="mono">${e(item.username || '未指定')}</span>
         <div class="faint">令牌 ${item.secret_length || 0} 字符</div>`;

    return `<tr>
      <td>
        <div style="font-weight:500">${e(item.name)}</div>
        ${item.description ? `<div class="faint truncate" style="max-width:240px">${e(item.description)}</div>` : ''}
      </td>
      <td><span class="badge neutral">${e(item.kind_label || item.kind)}</span></td>
      <td>${identity}</td>
      <td>${tested}</td>
      <td>${item.used_by ? `<span class="badge neutral">${item.used_by} 个任务</span>` : '<span class="faint">未使用</span>'}</td>
      <td>
        <div class="table-actions">
          <button class="sm" data-cred-test="${item.id}">测试</button>
          <button class="sm" data-cred-edit="${item.id}">编辑</button>
          <button class="sm danger" data-cred-delete="${item.id}">删除</button>
        </div>
      </td>
    </tr>`;
  }

  AD.testCredential = async function (credentialId) {
    const repoUrl = await AD.prompt({
      title: '测试凭据',
      label: '用于测试的仓库地址',
      placeholder: 'https://github.com/owner/repo.git 或 git@github.com:owner/repo.git',
      hint: '同一个令牌对不同仓库的权限可能不同，请用真实地址测试。测试不会修改任何仓库内容。',
    });
    if (!repoUrl) return;
    const toast = AD.toast('正在测试，请稍候…', 'info', 30000);
    try {
      const result = await AD.api.post(`/api/credentials/${credentialId}/test`, { repo_url: repoUrl });
      if (result.ok) AD.toastSuccess(result.message);
      else AD.toastError(result.message);
      AD.render('credentials');
    } catch (err) {
      AD.toastError(err.message);
    }
  };

  AD.deleteCredential = async function (credentialId) {
    let detail = '该操作不可撤销。';
    let force = false;
    try {
      const info = await AD.api.get(`/api/credentials/${credentialId}`);
      const used = info.credential.used_by || 0;
      if (used > 0) {
        // 有任务在引用时，删除会让这些任务静默失去凭据，必须明确告知。
        const names = (info.credential.tasks || []).map((t) => t.name).join('、');
        detail = `仍有 ${used} 个任务在使用该凭据（${names}）。删除后这些任务将回退使用任务内配置的令牌，可能导致部署失败。`;
        force = true;
      }
    } catch (err) { /* 拿不到详情就直接问 */ }

    const confirmed = await AD.confirm({
      title: '删除凭据',
      message: '确定要删除该凭据吗？',
      detail,
      confirmText: force ? '仍然删除' : '删除',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await AD.api.del(`/api/credentials/${credentialId}${force ? '?force=true' : ''}`);
      AD.toastSuccess('凭据已删除');
      AD.render('credentials');
    } catch (err) { AD.toastError(err.message); }
  };

  AD.openCredentialForm = async function (credentialId) {
    let item = null;
    if (credentialId) {
      const data = await AD.api.get(`/api/credentials/${credentialId}`);
      item = data.credential;
    }
    const kinds = await AD.api.get('/api/credentials/kinds');
    const t = item || {};
    const isEdit = Boolean(credentialId);

    const body = `
      <div id="cred-error" class="alert error hidden"></div>
      <div class="field">
        <label>名称<span class="req">*</span></label>
        <input type="text" id="c-name" value="${a(t.name || '')}" placeholder="例如：GitHub 只读令牌">
        <div class="hint">用于在任务里辨认，建议写清用途与范围</div>
      </div>
      <div class="field">
        <label>类型<span class="req">*</span></label>
        <select id="c-kind"${isEdit ? ' disabled' : ''}>
          ${kinds.kinds.map((k) => `<option value="${a(k.value)}"${(t.kind || 'https_token') === k.value ? ' selected' : ''}>${e(k.label)}</option>`).join('')}
        </select>
        <div class="hint" id="c-kind-hint"></div>
        ${isEdit ? '<div class="hint">类型不可修改；如需更换请新建凭据</div>' : ''}
      </div>
      <div class="field" id="c-username-wrap">
        <label>用户名</label>
        <input type="text" id="c-username" value="${a(t.username || '')}">
        <div class="hint" id="c-username-hint"></div>
      </div>
      <div class="field">
        <label id="c-secret-label">密钥<span class="req">*</span></label>
        <textarea id="c-secret" rows="5" placeholder=""></textarea>
        <div class="hint" id="c-secret-hint"></div>
      </div>
      <div class="field hidden" id="c-passphrase-wrap">
        <label>私钥口令</label>
        <input type="password" id="c-passphrase" autocomplete="new-password">
        <div class="hint">私钥没有口令时留空</div>
      </div>
      <div class="field">
        <label>备注</label>
        <input type="text" id="c-description" value="${a(t.description || '')}" placeholder="可选">
      </div>
    `;

    const backdrop = AD.Modal.open({
      title: isEdit ? '编辑凭据：' + t.name : '新建凭据',
      body,
      footer: '<button data-close>取消</button>'
        + `<button class="primary" id="cred-save">${isEdit ? '保存' : '创建'}</button>`,
    });

    const kindSelect = backdrop.querySelector('#c-kind');
    const applyKind = () => {
      const kind = kindSelect.value;
      const meta = kinds.kinds.find((k) => k.value === kind) || {};
      backdrop.querySelector('#c-kind-hint').textContent = meta.hint || '';
      const secretLabel = backdrop.querySelector('#c-secret-label');
      const secretInput = backdrop.querySelector('#c-secret');
      secretLabel.innerHTML = e(meta.secret_label || '密钥') + '<span class="req">*</span>';
      secretInput.placeholder = meta.secret_placeholder || '';
      secretInput.rows = kind === 'ssh_key' ? 6 : 2;
      backdrop.querySelector('#c-username-hint').textContent = kind === 'ssh_key'
        ? 'SSH 地址通常使用 git，留空则默认 git' : 'GitHub 使用 x-access-token；留空则使用默认值';
      backdrop.querySelector('#c-username-wrap').classList.toggle('hidden', false);
      backdrop.querySelector('#c-passphrase-wrap').classList.toggle('hidden', kind !== 'ssh_key');
      if (isEdit) {
        secretInput.placeholder = t.has_secret ? '已保存，留空表示不修改' : '';
        secretInput.required = false;
      }
    };
    kindSelect.addEventListener('change', applyKind);
    applyKind();

    backdrop.querySelector('#cred-save').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const payload = {
        name: backdrop.querySelector('#c-name').value.trim(),
        username: backdrop.querySelector('#c-username').value.trim(),
        description: backdrop.querySelector('#c-description').value.trim(),
      };
      if (!isEdit) payload.kind = kindSelect.value;
      const secret = backdrop.querySelector('#c-secret').value;
      if (secret) payload.secret = secret;
      const passphrase = backdrop.querySelector('#c-passphrase')?.value || '';
      if (passphrase) payload.passphrase = passphrase;

      if (!payload.name) { credError(backdrop, '请填写凭据名称'); return; }
      if (!isEdit && !secret) { credError(backdrop, '请填写密钥内容'); return; }

      AD.setBusy(button, true, '保存中…');
      try {
        if (isEdit) await AD.api.put(`/api/credentials/${credentialId}`, payload);
        else await AD.api.post('/api/credentials', payload);
        AD.toastSuccess(isEdit ? '凭据已保存' : '凭据已创建');
        AD.Modal.close();
        AD.render('credentials');
      } catch (err) {
        credError(backdrop, err.message);
        AD.setBusy(button, false);
      }
    });
  };

  function credError(backdrop, message) {
    const box = backdrop.querySelector('#cred-error');
    box.textContent = message;
    box.classList.remove('hidden');
  }


  // ======================================================================
  // 一键更新
  // ======================================================================
  let updateSession = null;
  const versionText = (v) => 'v' + String(v || '').replace(/^[vV]+/, '');
  AD.stopUpdatePanel = function () {
    if (!updateSession) return;
    updateSession.stopped = true;
    clearTimeout(updateSession.timer);
    updateSession.controller?.abort();
    updateSession = null;
  };
  window.addEventListener('pagehide', AD.stopUpdatePanel);

  AD.renderUpdatePanel = function (panel) {
    const previous = updateSession?.panel === panel ? updateSession : null;
    AD.stopUpdatePanel();
    if (!panel) return;
    const ctx = { panel, stopped: false, timer: null, failures: 0, polls: 0,
      deadline: Date.now() + 15 * 60 * 1000, state: previous?.state || null,
      logView: previous?.logView || { open: false, top: 0, left: 0, follow: true },
      logRendered: previous?.logRendered || false };
    updateSession = ctx;
    pollUpdateStatus(ctx); // 先恢复持久化状态，不用 health 推断成功。
  };
  const updateAlive = (ctx) => !ctx.stopped && ctx.panel.isConnected;

  async function updateRequest(ctx, path, method = 'GET', payload) {
    const controller = new AbortController();
    ctx.controller = controller;
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
        signal: controller.signal, headers: { 'Content-Type': 'application/json' },
        body: payload === undefined ? undefined : JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) {
        const err = new Error(response.status === 401 ? '登录已过期，请重新登录后打开更新卡片恢复状态' :
          (typeof data.detail === 'string' ? data.detail : '服务请求失败（HTTP ' + response.status + '）'));
        err.status = response.status;
        throw err;
      }
      return data;
    } finally { clearTimeout(timer); }
  }

  function updateError(ctx, message) {
    if (!updateAlive(ctx)) return;
    const body = ctx.panel.querySelector('#upd-body');
    body.querySelector('#upd-error')?.remove();
    body.insertAdjacentHTML('beforeend', '<div id="upd-error" class="alert error">' + e(message)
      + '<div><button id="upd-resume">重新读取状态</button></div></div>');
    body.querySelector('#upd-resume')?.addEventListener('click', () => AD.renderUpdatePanel(ctx.panel));
  }

  function updateConfirmed(state) {
    return state.stage === 'done' && state.confirmed_operation_id === state.operation_id && state.operation_id
      && state.confirmed_operation === state.operation && ['update', 'rollback'].includes(state.operation)
      && state.boot_id && state.boot_id !== state.before_boot_id && state.pid !== state.before_pid
      && String(state.current_version).replace(/^[vV]+/, '') === state.expected_version
      && String(state.version).replace(/^[vV]+/, '') === state.expected_version;
  }

  function drawUpdateState(ctx, state) {
    const body = ctx.panel.querySelector('#upd-body');
    const view = ctx.logView;
    const rememberLog = (details, log) => {
      if (!details || !log) return;
      view.open = details.open;
      // 收起的 details 没有可用尺寸，不能用它覆盖历史阅读位置。
      if (details.open && log.clientHeight > 0) {
        view.top = log.scrollTop;
        view.left = log.scrollLeft;
        view.follow = log.scrollHeight - log.clientHeight - log.scrollTop <= 32;
      }
    };
    if (ctx.logRendered) rememberLog(body.querySelector('#upd-logs'), body.querySelector('#upd-log'));
    const confirmed = updateConfirmed(state);
    const unverified = state.stage === 'unverified' || (state.stage === 'done' && !confirmed)
      || (state.stage === 'restarting' && !state.active);
    const labels = { queued: '排队中', checking: '检查中', downloading: '下载中', backing_up: '备份代码', applying: '替换代码',
      dependencies: '更新依赖', restarting: '等待重启确认', done: '已确认完成', failed: '失败',
      error: '失败', idle: '尚未更新', unverified: '历史操作未确认' };
    const tone = state.error || ['failed', 'error'].includes(state.stage) ? 'error'
      : unverified ? 'warning' : confirmed ? 'success' : state.active ? 'info' : 'neutral';
    const label = unverified ? '历史操作未确认' : labels[state.stage] || state.stage || '状态未知';
    const logs = state.log || [];
    body.innerHTML = '<div class="upd-current">当前运行版本：<strong>'
      + e(state.current_version ? versionText(state.current_version) : '未知') + '</strong></div>'
      + '<div id="upd-operation" class="alert ' + tone + '">'
      + (state.active ? '当前操作状态：' : '上次操作状态：') + e(label)
      + (state.target_version ? ' · 目标版本 ' + e(versionText(state.target_version)) : '')
      + (state.legacy_stage && state.legacy_stage !== state.stage ? ' · 原记录阶段：' + e(state.legacy_stage === 'done' ? '完成（未经确认）' : labels[state.legacy_stage] || state.legacy_stage) : '')
      + (state.error ? '<div class="upd-operation-error">' + e(state.error) + '</div>' : '') + '</div>'
      + (state.notice ? '<div class="alert warning">' + e(state.notice) + '</div>'
        : unverified ? '<div class="alert warning">缺少目标版本及操作的完整确认，不能判定成功或自动刷新；可重新读取状态或检查更新。</div>' : '')
      + '<details id="upd-logs" class="upd-logs"' + (view.open ? ' open' : '') + '>'
      + '<summary>操作日志（' + logs.length + ' 条）<span class="dim"> · 展开 / 收起</span></summary>'
      + '<div id="upd-log" class="log-view" tabindex="0" aria-label="更新操作日志">'
      + (logs.length ? logs.map(e).join('\n') : '暂无日志') + '</div></details>';
    const details = body.querySelector('#upd-logs');
    const log = body.querySelector('#upd-log');
    const restoreLog = () => {
      if (!details.open) return;
      log.scrollTop = view.follow ? Math.max(0, log.scrollHeight - log.clientHeight) : view.top;
      log.scrollLeft = view.left;
    };
    restoreLog();
    details.addEventListener('toggle', () => {
      if (!updateAlive(ctx) || body.querySelector('#upd-logs') !== details) return;
      view.open = details.open;
      restoreLog();
    });
    log.addEventListener('scroll', () => {
      if (updateAlive(ctx) && body.querySelector('#upd-log') === log) rememberLog(details, log);
    });
    ctx.logRendered = true;
  }

  async function pollUpdateStatus(ctx) {
    if (!updateAlive(ctx)) return;
    if (++ctx.polls > 600 || Date.now() > ctx.deadline) {
      updateError(ctx, '等待确认超时，未确认更新成功；请检查服务或重新读取状态'); return;
    }
    try {
      const state = await updateRequest(ctx, '/api/system/self-update/status');
      if (!updateAlive(ctx)) return;
      if (ctx.operationId && ctx.operationId !== state.operation_id) {
        updateError(ctx, '操作标识已变化，停止自动刷新，请重新读取状态'); return;
      }
      ctx.state = state;
      drawUpdateState(ctx, state);
      if (updateConfirmed(state)) {
        const key = 'autodeploy-reloaded-' + state.operation_id;
        try {
          if (!sessionStorage.getItem(key)) {
            sessionStorage.setItem(key, '1');
            AD.stopUpdatePanel();
            location.reload(); return;
          }
        } catch (_) { updateError(ctx, '无法保存刷新标记，请手动刷新页面'); }
      }
      if (!state.active) { await updateControls(ctx, state); return; }
    } catch (err) {
      if (!updateAlive(ctx)) return;
      // HTTP 错误不是重启成功；网络错误总预算不因一次连通而重置。
      if (err.status || ++ctx.failures >= 40) { updateError(ctx, err.message || '重连预算耗尽'); return; }
      if (!ctx.state) ctx.panel.querySelector('#upd-body').textContent = '暂时失联，正在有限重连…';
    }
    if (updateAlive(ctx)) ctx.timer = setTimeout(() => pollUpdateStatus(ctx), 1500);
  }

  async function updateControls(ctx, state) {
    const body = ctx.panel.querySelector('#upd-body');
    const rollbackReason = state.backup_notice || (state.can_rollback
      ? '备份仅包含代码，数据库和 Python 依赖不会回滚。'
      : '没有通过结构及版本校验的代码备份，无法自动回滚。');
    body.insertAdjacentHTML('beforeend', '<div class="field upd-source"><label for="upd-repo">更新源（只使用可信仓库）</label>'
      + '<div class="upd-source-row"><input id="upd-repo" type="url" placeholder="https://github.com/组织/仓库">'
      + '<button id="upd-check">保存更新源并强制检查</button></div></div>'
      + '<section class="upd-check-section" aria-labelledby="upd-check-title"><h3 id="upd-check-title">本次检查结果</h3>'
      + '<div id="upd-check-result" aria-live="polite"><div class="dim">尚未检查；上次操作状态不代表本次检查结果。</div></div></section>'
      + '<div class="upd-recovery"><button id="upd-read-state">重新读取状态</button>'
      + '<button id="upd-rollback"' + (state.can_rollback ? '' : ' disabled aria-describedby="upd-rollback-reason"')
      + '>回滚上一版本</button></div>'
      + '<div id="upd-rollback-reason" class="upd-rollback-reason">' + e(rollbackReason) + '</div>');
    body.querySelector('#upd-read-state').addEventListener('click', () => AD.renderUpdatePanel(ctx.panel));
    if (state.can_rollback) body.querySelector('#upd-rollback').addEventListener('click', () => startUpdateOperation(ctx, 'rollback'));
    try {
      const settings = await updateRequest(ctx, '/api/settings');
      if (!updateAlive(ctx)) return;
      body.querySelector('#upd-repo').value = settings.settings.update_repo || '';
    } catch (err) { updateError(ctx, err.message); }
    if (!updateAlive(ctx)) return;
    body.querySelector('#upd-check').addEventListener('click', async (event) => {
      event.target.disabled = true;
      const result = body.querySelector('#upd-check-result');
      result.innerHTML = '<div class="dim">正在保存更新源并检查…</div>';
      try {
        await updateRequest(ctx, '/api/settings', 'PUT', { update_repo: body.querySelector('#upd-repo').value.trim() });
        const check = await updateRequest(ctx, '/api/system/update/check?force=true');
        if (!updateAlive(ctx)) return;
        if (check.error) throw new Error(check.error);
        result.innerHTML = '<div>检查时运行版本 ' + e(versionText(check.current)) + '，更新源最新版本 ' + e(versionText(check.latest)) + '</div>'
          + (check.update_available ? '<button id="upd-start">更新到 ' + e(versionText(check.latest)) + '</button>' : '<div>本次检查未发现可用更新</div>');
        result.querySelector('#upd-start')?.addEventListener('click', () => startUpdateOperation(ctx, 'update', check.latest));
      } catch (err) {
        if (updateAlive(ctx)) result.innerHTML = '<div class="alert error">本次检查失败：' + e(err.message) + '</div>';
      }
      finally { if (updateAlive(ctx)) event.target.disabled = false; }
    });
  }

  async function startUpdateOperation(ctx, operation, target) {
    if (ctx.submitting) return;
    ctx.submitting = true;
    try {
      const confirmed = await AD.confirm({ title: operation === 'update' ? '更新到 ' + versionText(target) : '回滚上一版本',
        message: '将替换程序代码并重启服务。仅在后端确认目标版本运行后自动刷新。',
        detail: '备份仅含代码，不回滚数据库或 Python 依赖；降级可能不兼容。',
        confirmText: '确认执行', danger: operation === 'rollback' });
      if (!confirmed || !updateAlive(ctx)) return;
      const result = await updateRequest(ctx, '/api/system/self-update' + (operation === 'rollback' ? '/rollback' : ''),
        'POST', operation === 'update' ? { target_version: target } : {});
      if (!updateAlive(ctx)) return;
      ctx.operationId = result.state.operation_id;
      ctx.deadline = Date.now() + 15 * 60 * 1000;
      ctx.polls = 0;
      ctx.failures = 0;
      pollUpdateStatus(ctx);
    } catch (err) { updateError(ctx, err.message + '；请求可能已送达，可重新读取状态确认'); }
    finally { ctx.submitting = false; }
  }

})(window.AD);
