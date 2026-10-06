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

  // 各部署方式用到的方式专属字段：collectForm 提交时就地清空与当前方式
  // 无关的值——界面上来回切换保留输入，但不会把旧方式的值存成脏配置，
  // 也不会触发后端的方式互斥校验。
  const METHOD_FIELD_IDS = {
    artifact: [],
    script: ['deploy_script'],
    release: ['deploy_script'],
    systemd: ['service_name', 'deploy_script'],
    docker: ['docker_image', 'docker_command'],
    docker_compose: ['docker_compose_file'],
    rsync: ['rsync_target', 'rsync_options'],
  };
  const METHOD_FIELD_ALL = ['deploy_script', 'service_name', 'docker_image',
    'docker_compose_file', 'docker_command', 'rsync_target', 'rsync_options'];
  // 方式专属必填项：与后端 _check_method_requirements 对应。
  const METHOD_REQUIRED = {
    script: ['deploy_script'],
    systemd: ['service_name'],
    docker: ['docker_image'],
    rsync: ['rsync_target'],
  };
  // 方式说明：把“选了它到底会发生什么”写在选择框下面，而不是让用户猜。
  const METHOD_DESCRIPTIONS = {
    artifact: '拉取代码并按打包路径生成 .tar.gz 产物，不发布、不切换软链。',
    script: '在源码目录执行你提供的部署脚本，发布过程完全由脚本控制。',
    release: '打包到 releases/N 并把 current 软链切换到新版本，可在发布后执行脚本。',
    systemd: '切换软链后重启指定的 systemd 服务并校验状态（在本机执行，不是远程服务器）。',
    docker: '在发布目录构建镜像；只有填写启动命令才会创建或更新容器。',
    docker_compose: '在发布目录执行 docker compose up -d --build 更新服务。',
    rsync: '把发布目录同步到目标路径；只有配置了目标目录才会更新本地软链。',
  };

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
    // 升级到新版本：向上的箭头，常用于「更新」动作。
    upgrade: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19.5V7"/><path d="m6.5 12.5 5.5-5.5 5.5 5.5"/><path d="M4.5 4.5h15"/></svg>',
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9.5 7V5.5A1.5 1.5 0 0 1 11 4h2a1.5 1.5 0 0 1 1.5 1.5V7"/><path d="M6.5 7l.8 11.2A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.8L17.5 7"/><path d="M10.5 11v5M13.5 11v5"/></svg>',
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
        // 倒计时基于真实时刻，不受展示时区影响。
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

    // 就地更新：运行状态落定时只改受影响的行与已展开的面板。
    // 没有活跃运行时不发请求；正在观看的行内日志有自己的轮询，无需这里兜底。
    AD.state.viewRefresh = async () => {
      if (!AD.state.tasks.some((task) => task.active_run)) return;
      await refreshTaskRowsInPlace();
    };
  };

  // ======================================================================
  // 删除任务
  // ======================================================================
  /** 删除后果的描述，用于两次确认；内容与后端实际动作一一对应。 */
  function deleteConsequences(task) {
    const items = ['停止并移除该任务部署的容器', '删除工作目录与发布产物', '删除全部运行记录与日志'];
    const method = String(task.deploy_method || '');
    if (method === 'docker_compose') items[0] = '执行 docker compose down，停止并移除该项目容器与网络';
    else if (method === 'docker') items[0] = '执行回滚脚本停止容器（未配置回滚脚本时需手动确认）';
    else if (method === 'systemd') items[0] = '不会停止 systemd 服务，如需停止请手动执行 systemctl stop';
    else if (method === 'rsync') items[0] = '不会同步远端，远端文件保持原样';
    else items[0] = '该部署方式没有需要停止的容器';
    items.push('目标目录（target_dir）本身不会被删除');
    return items;
  }

  /** 二次确认后删除任务：第一步确认意图，第二步要求输入任务名。 */
  AD.deleteTask = async function (taskId, taskOverride) {
    const task = taskOverride
      || (AD.state.tasks || []).find((item) => item.id === taskId);
    if (!task) { AD.toastError('任务不存在或已被删除'); return false; }
    // 运行中的任务不能删除：容器可能正在被使用。这里挡住所有入口
    // （列表行、编辑表单），不只依赖按钮的 disabled 状态。
    if (task.active_run) {
      AD.toastError(`任务正在运行（#${task.active_run.id}），请先取消后再删除`);
      return false;
    }

    const first = await AD.confirm({
      title: '删除任务',
      message: `确定要删除「${task.name}」吗？`,
      detail: '将删除该任务配置、运行记录与发布产物，并停止它部署的容器。此操作不可撤销。'
        + '影响范围：' + deleteConsequences(task).join('；') + '。',
      confirmText: '继续',
      danger: true,
    });
    if (!first) return false;

    // 第二步：输入任务名。这是唯一能防止「手滑点到删除」的关卡，
    // 因为删除会连带清掉磁盘上的部署文件夹，无法从界面恢复。
    const typed = await AD.prompt({
      title: '最后确认',
      label: `请输入任务名「${task.name}」以确认删除`,
      placeholder: task.name,
      value: '',
      hint: '删除后无法恢复：工作目录、发布产物与运行日志都会被清理。',
    });
    if (typed === null) return false;
    if (String(typed).trim() !== String(task.name)) {
      AD.toastError('输入的任务名不匹配，已取消删除');
      return false;
    }
    try {
      const result = await AD.api.del(`/api/tasks/${taskId}`);
      const removed = (result.purged || []).length;
      const stopped = (result.containers || []).length;
      AD.toastSuccess(`任务已删除（清理 ${removed} 项${stopped ? `，停止容器 ${stopped} 个` : ''}）`);
      AD.render('tasks');
      return true;
    } catch (err) {
      AD.toastError(err.message);
      return false;
    }
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
      <td class="task-name-cell" data-label="任务">
        <button type="button" class="task-name-link" data-task-edit="${task.id}"
                title="点击编辑任务">${e(task.name)}</button>
        ${task.description ? `<div class="faint truncate" title="${a(task.description)}">${e(task.description)}</div>` : ''}
        <div class="faint truncate" style="max-width:280px" title="${a(task.repo_url)}">
          ${e(task.repo_url)}<span class="dim"> @${e(task.repo_branch)}</span>
        </div>
      </td>
      <td data-label="状态">${statusBadge}<div class="faint" style="margin-top:3px">${stats}</div></td>
      <td class="mono" data-label="调度" style="font-size:11.5px">${e(task.schedule_expression || '—')}</td>
      <td data-label="部署方式"><span class="badge neutral">${e(methodLabel(task.deploy_method))}</span></td>
      <td class="nowrap" data-label="最近运行">${lastRun}</td>
      <td class="nowrap" data-label="下次执行">${nextRun}</td>
      <td data-label="操作">
        <div class="table-actions row-actions">
          <button class="icon-btn scroll-btn${expanded ? ' active' : ''}" data-task-log="${task.id}"
                  title="${expanded ? '收起日志' : '展开最近日志'}"
                  aria-label="${expanded ? '收起日志' : '展开最近日志'}"
                  aria-expanded="${expanded ? 'true' : 'false'}">${ICONS.scroll}</button>
          ${runButton}
          ${toggleButton}
          <button class="icon-btn danger" data-task-delete="${task.id}"
                  title="${active ? '任务运行中，需先取消才能删除' : '删除任务'}"
                  aria-label="${active ? '任务运行中，需先取消才能删除' : '删除任务'}">${ICONS.trash}</button>
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
            // 只就地刷新这一行：整表重绘会丢掉滚动位置与其它行的展开状态。
            await AD.refreshTaskRowsInPlace();
            // 运行后直接展开该行，让用户看到实时日志。
            AD.toggleTaskExpand(taskId, { forceOpen: true, runId: result.run_id });
          });
        });
        return;
      }
      const cancel = event.target.closest('[data-task-cancel]');
      if (cancel) {
        handle(cancel, async () => {
          const result = await AD.api.post(`/api/tasks/${cancel.dataset.taskCancel}/cancel`);
          AD.toastSuccess(result.message || '已请求取消当前运行');
        });
        // 取消是异步的（进程在安全点退出），状态由轮询就地收敛，不重绘表格。
        AD.pollRunStates();
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
          await AD.refreshTaskRowsInPlace();
        });
        return;
      }
      const remove = event.target.closest('[data-task-delete]');
      if (remove) {
        if (remove.disabled) return;
        // 删除连带停止容器与清理部署目录，走两步确认，不套用通用的单次确认。
        handle(remove, () => AD.deleteTask(Number(remove.dataset.taskDelete)));
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
      AD.clearInlinePanelSync();
      AD.state.expandedTaskId = null;
      delete AD.state.inlineRunSelection[taskId];
      syncScrollButton(scrollBtn, false);
      return;
    }

    // 只保留一个展开行，展开新的收起旧的。
    document.querySelectorAll('.expand-row').forEach((node) => node.remove());
    document.querySelectorAll('.scroll-btn.active').forEach((node) => syncScrollButton(node, false));
    AD.stopAllInlineLogs();
    AD.clearInlinePanelSync();
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
    // 活跃运行可能随就地同步变化，因此每次从 runs 现算而不是缓存一次。
    const activeRunOf = () => runs.find((run) => run.status === 'queued' || run.status === 'running');
    const activeRun = activeRunOf();
    const lastRun = runs[0];

    placeholder.querySelector('td > .task-expand').innerHTML = `
      <div class="expand-toolbar">
        <span class="badge ${t.enabled ? 'on' : 'off'}" data-inline-state>${t.enabled ? '已启用' : '已禁用'}</span>
        <span class="badge neutral">${e(methodLabel(t.deploy_method))}</span>
        <span class="faint" data-inline-schedule>${dedupeIn([AD.scheduleText(t), nextRunInlineText(t)]).map((text) =>
          e(text)).join(' · ')}</span>
        <div class="spacer"></div>
        <button class="sm" data-inline-rollback="${t.id}">${ICONS.rollback}<span>回滚上一版本</span></button>
        <button class="sm" data-inline-rollback-selected disabled aria-describedby="inline-rollback-reason-${t.id}">${ICONS.rollback}<span>回滚到这个版本</span></button>
        <button class="sm" data-inline-artifacts="${t.id}">${ICONS.download}<span>产物</span></button>
        <span data-inline-cancel-slot>${activeRun ? `<button class="sm danger" data-inline-cancel="${activeRun.id}">取消 #${activeRun.id}</button>` : ''}</span>
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
      <div class="mini-runs" data-inline-runs>${renderInlineRuns(runs, taskId)}</div>
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
    // 取消走事件委托：就地同步会替换掉取消按钮本身，直接绑定会随节点失效。
    root.addEventListener('click', async (event) => {
      const cancelButton = event.target.closest('[data-inline-cancel]');
      if (!cancelButton) return;
      await busyGuard(cancelButton, async () => {
        const result = await AD.api.post(`/api/runs/${cancelButton.dataset.inlineCancel}/cancel`);
        AD.toastSuccess(result.message || '已请求取消');
      });
      // 取消是异步的，终态由日志轮询就地落定，不重绘面板。
      AD.pollRunStates();
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
        // 回滚已改为后台运行：立即返回 run_id，进度在运行详情里轮询。
        AD.toastSuccess(result.message || `已开始回滚（运行 #${result.run_id}）`);
        AD.openRunDetail(result.run_id);
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

    // --- 就地同步：运行状态落定时更新本面板，不重绘整个视图 ---------
    const cancelSlot = root.querySelector('[data-inline-cancel-slot]');
    const renderCancelSlot = () => {
      const active = activeRunOf();
      cancelSlot.innerHTML = active
        ? `<button class="sm danger" data-inline-cancel="${active.id}">取消 #${active.id}</button>` : '';
    };
    AD.setInlinePanelSync({
      taskId,
      /** 一条运行的最新状态：更新工具条取消按钮与「最近运行」对应行。 */
      applyRun(run) {
        if (!root.isConnected) return;
        const known = runs.find((item) => item.id === run.id);
        if (known) Object.assign(known, run);
        else runs.unshift(run);
        renderCancelSlot();
        updateRollbackControls();
        const row = root.querySelector(`[data-inline-run-row="${run.id}"]`);
        if (row) {
          const statusCell = row.querySelector('[data-inline-run-status]');
          if (statusCell) statusCell.innerHTML = runBadgeHtml(run);
          const durationCell = row.querySelector('[data-inline-run-duration]');
          if (durationCell) durationCell.textContent = AD.formatDuration(run.duration_ms);
        }
      },
      /** 任务级字段变化（启用状态、下次执行时间）。 */
      applyTask(task) {
        if (!root.isConnected) return;
        const stateBadge = root.querySelector('[data-inline-state]');
        if (stateBadge) {
          stateBadge.className = 'badge ' + (task.enabled ? 'on' : 'off');
          stateBadge.textContent = task.enabled ? '已启用' : '已禁用';
        }
        const schedule = root.querySelector('[data-inline-schedule]');
        if (schedule) {
          schedule.textContent = dedupeIn([
            AD.scheduleText(task), nextRunInlineText(task),
          ]).join(' · ');
        }
      },
    });

    const requested = options_.runId || (options_.forceOpen ? AD.state.inlineRunSelection[taskId] : null);
    const targetRunId = runs.some((run) => run.id === requested) ? requested
      : (activeRun ? activeRun.id : (lastRun ? lastRun.id : null));
    selectRun(targetRunId);
  };

  AD.clearInlinePanelSync = () => { inlinePanelSync = null; };

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
      </tr></thead><tbody>${runs.slice(0, 8).map((run) => `<tr data-inline-run-row="${run.id}">
        <td class="mono dim">${run.id}</td>
        <td data-inline-run-status>${runBadgeHtml(run)}</td>
        <td class="mono dim truncate" style="max-width:200px" title="${a(run.commit_message || '')}">${run.commit_after ? e(AD.shortCommit(run.commit_after)) + ' ' + e(run.commit_message || '') : '—'}</td>
        <td class="dim">${e(AD.triggerLabel(run.trigger))}</td>
        <td class="dim nowrap" data-inline-run-duration>${e(AD.formatDuration(run.duration_ms))}</td>
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
          // 就地收敛：更新任务行与本面板的取消按钮、状态徽标，不重绘视图。
          AD.syncRunInPlace(detail.run, taskId);
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
        else {
          AD.stopInlineLog(taskId);
          AD.syncRunInPlace(detail.run, taskId);
        }
      } catch (err) { fail(err); }
    })();
  }

  /** 原地刷新任务行：状态落定后更新图标与徽标，不重建整个表格。
   *  重建表格会连带销毁用户正在查看的行内展开，所以这里只改这一行。
   *  传入 task 时直接使用该对象，避免轮询里对每个任务重复请求。 */
  function refreshTaskRowInPlace(taskId, task) {
    const applyTask = (found) => {
      const row = document.querySelector(`tr[data-task-row="${taskId}"]`);
      if (!found || !row) return;
      const active = found.active_run;

      // 运行/停止图标
      const cell = row.querySelector('.table-actions');
      if (cell) {
        const runButton = active
          ? `<button class="icon-btn stop" data-task-cancel="${found.id}" title="停止当前运行" aria-label="停止当前运行">${ICONS.stop}</button>`
          : `<button class="icon-btn run" data-task-run="${found.id}" title="立即运行" aria-label="立即运行">${ICONS.play}</button>`;
        const control = cell.querySelector('[data-task-run], [data-task-cancel]');
        if (control) control.outerHTML = runButton;
      }

      // 状态徽标与计数：与新表格同一套渲染逻辑，避免两者显示不一致。
      const statusCell = row.querySelector('td:nth-child(2)');
      if (statusCell) {
        const statusBadge = active
          ? `<span class="badge ${e(active.status)}"><span class="dot ${e(active.status)}"></span>${e(AD.statusLabel(active.status))}</span>`
          : found.enabled
            ? '<span class="badge on">已启用</span>'
            : '<span class="badge off">已暂停</span>';
        const stats = found.run_count
          ? `<span class="faint">${found.success_count} 成功 / ${found.failure_count} 失败</span>`
          : '<span class="faint">尚未运行</span>';
        statusCell.innerHTML = `${statusBadge}<div class="faint" style="margin-top:3px">${stats}</div>`;
      }

      // 最近运行列
      const lastRunCell = row.querySelector('td:nth-child(5)');
      if (lastRunCell) {
        lastRunCell.innerHTML = found.last_status
          ? `<span class="badge ${e(found.last_status)}">${e(AD.statusLabel(found.last_status))}</span>
             <div class="faint">${e(AD.formatRelative(found.last_run_at))}</div>`
          : '<span class="faint">—</span>';
      }

      // 禁用行样式
      row.classList.toggle('row-disabled', !found.enabled);
    };

    if (task) { applyTask(task); return; }
    // 页面上没有这一行时无需发请求：运行记录页等视图也会走到这里。
    if (!document.querySelector(`tr[data-task-row="${taskId}"]`)) return;
    AD.api.get('/api/tasks')
      .then((data) => applyTask((data.tasks || []).find((item) => item.id === taskId)))
      .catch(() => { /* 下次轮询或手动刷新兜底 */ });
  }
  AD.refreshTaskRowInPlace = refreshTaskRowInPlace;

  /** 就地刷新全表：一次请求更新所有任务行，供运行/启停等自身触发的动作使用，
   *  避免整表重绘丢滚动位置与展开状态。 */
  async function refreshTaskRowsInPlace() {
    const data = await AD.api.get('/api/tasks');
    AD.state.tasks = data.tasks || [];
    for (const task of AD.state.tasks) {
      refreshTaskRowInPlace(task.id, task);
      AD.syncInlinePanelTask(task);
    }
  }
  AD.refreshTaskRowsInPlace = refreshTaskRowsInPlace;

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
      catch (err) {
        // 兜底必须与真实返回同构：此前缺 schedule_defaults，读取默认表达式
        // 时会让新建表单直接抛错。
        AD.state.defaults = {
          repo_branch: 'main', timeout_seconds: 1800, keep_releases: 5,
          schedule_defaults: { interval: '1h', cron: '0 * * * *' },
        };
      }
    }
    // 凭据下拉需要最新列表：在别处刚创建/删除后应立刻反映出来。拉取失败
    // 不能伪装成“没有凭据”：保留旧缓存并提示，否则保存会把已有绑定静默解除。
    try {
      AD.state.credentials = (await AD.api.get('/api/credentials')).credentials || [];
      AD.state.credentialsError = false;
    } catch (err) {
      AD.state.credentials = AD.state.credentials || [];
      AD.state.credentialsError = true;
    }
    const defaults = AD.state.defaults;
    const t = task || {};
    const isEdit = Boolean(taskId);
    const scheduleDefaults = defaults.schedule_defaults || {};
    const boundCredentialId = t.credential_id != null ? String(t.credential_id) : '';
    const boundInList = (AD.state.credentials || []).some((c) => String(c.id) === boundCredentialId);
    // 认证方式初始值：绑定了集中凭据→凭据；有任务内令牌→令牌；否则无需认证。
    const initialAuth = boundCredentialId ? 'credential' : (t.has_token ? 'token' : 'none');
    const initialMethod = t.deploy_method || 'script';
    // 高级参数默认展开的条件：已有任务且任一高级项偏离默认值。
    const hasAdvanced = isEdit && (
      (t.git_depth !== undefined && t.git_depth !== 1)
      || (t.timeout_seconds !== undefined && t.timeout_seconds !== (defaults.timeout_seconds || 1800))
      || Boolean(t.env_vars && Object.keys(t.env_vars).length)
    );

    const credentialOptions = (AD.state.credentials || []).map((c) =>
      `<option value="${c.id}"${boundCredentialId === String(c.id) ? ' selected' : ''}>${e(c.name)}（${e(c.kind_label || c.kind)}）</option>`).join('');
    // 已绑定的凭据不在列表里（被删除或列表加载失败）时注入占位选项保留
    // 绑定值：让用户看到并主动处理，而不是保存时静默解除。
    const boundPlaceholder = boundCredentialId && !boundInList
      ? `<option value="${a(boundCredentialId)}" selected>（ID ${a(boundCredentialId)}）当前绑定的凭据</option>` : '';
    const credHint = AD.state.credentialsError
      ? '凭据列表加载失败：已保留当前绑定，可稍后重试或直接新建。'
      : (credentialOptions ? '凭据集中保存、可跨任务复用；可在此新建或测试仓库访问。' : '还没有凭据，点击「新建凭据」即可在此创建。');

    const body = `
      <div id="form-error" class="alert error hidden"></div>

      <div class="section-title">基础信息</div>
      <div class="form-grid cols-2">
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

      <div class="section-title">部署方式</div>
      <div class="form-grid cols-2">
        <div class="field span-2">
          <select id="f-deploy_method">
            ${STAGE_LABELS.map(([value, label, hint]) =>
              `<option value="${a(value)}"${initialMethod === value ? ' selected' : ''}>${e(label)} — ${e(hint)}</option>`).join('')}
          </select>
          <div class="hint" id="method-desc"></div>
        </div>
      </div>

      <div class="section-title">代码与认证</div>
      <div class="form-grid cols-2">
        <div class="field span-2">
          <label>仓库地址<span class="req">*</span></label>
          <input type="text" id="f-repo_url" value="${a(t.repo_url || '')}"
                 placeholder="https://github.com/owner/repo.git">
          <div class="hint">支持 https / ssh / git 协议；公开仓库选择「无需认证」即可</div>
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
        <div class="field span-2">
          <label>仓库认证</label>
          <div class="auth-options">
            <label><input type="radio" name="f-auth_mode" value="none"${initialAuth === 'none' ? ' checked' : ''}>无需认证（公开仓库）</label>
            <label><input type="radio" name="f-auth_mode" value="credential"${initialAuth === 'credential' ? ' checked' : ''}>使用集中凭据</label>
            <label><input type="radio" name="f-auth_mode" value="token"${initialAuth === 'token' ? ' checked' : ''}>任务内令牌</label>
          </div>
        </div>
        <div class="field span-2${initialAuth === 'credential' ? '' : ' hidden'}" id="auth-credential-wrap">
          <label>凭据</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select id="f-credential_id" style="flex:1">
              <option value="">不使用（公开仓库）</option>
              ${boundPlaceholder}
              ${credentialOptions}
            </select>
            <button type="button" class="sm" id="cred-create">新建凭据</button>
            <button type="button" class="sm" id="cred-test">测试仓库访问</button>
          </div>
          <div class="hint">${credHint}</div>
        </div>
        <div class="field span-2${initialAuth === 'token' ? '' : ' hidden'}" id="auth-token-wrap">
          <label>任务内访问令牌（可选）</label>
          <input type="password" id="f-git_token" autocomplete="new-password"
                 placeholder="${isEdit ? (t.has_token ? '已配置，留空表示不修改' : '未配置') : '私有仓库才需要'}">
          <div class="hint">仅该任务使用；以 GIT_ASKPASS 传递，不会写入命令行或日志</div>
          <div class="hint">GitHub 令牌在 Settings → Developer settings → Personal access tokens 生成，
            勾选目标仓库并把 Contents 设为 Read-only 即可</div>
          ${isEdit && t.has_token ? '<div class="checkbox-row" style="margin-top:6px"><input type="checkbox" id="f-clear_token"><label for="f-clear_token">清除已保存的令牌</label></div>' : ''}
        </div>
      </div>

      <div class="section-title">构建与发布</div>
      <div class="form-grid cols-2">
        <div class="field span-2">
          <label>打包路径</label>
          <textarea id="f-artifact_paths" rows="3" placeholder="dist&#10;package.json">${e(t.artifact_paths || '')}</textarea>
          <div class="hint">每行一条，支持通配符（如 <code class="code-inline">dist/**</code>）。决定哪些文件进入发布包；留空则打包整个仓库</div>
        </div>
        <div class="field${initialMethod === 'artifact' ? ' hidden' : ''}" data-release-field>
          <label>目标目录</label>
          <input type="text" id="f-target_dir" value="${a(t.target_dir || '')}" placeholder="/var/www/myapp">
          <div class="hint">发布目录与 current 软链会放在这里；留空使用数据目录</div>
        </div>
        <div class="field${initialMethod === 'artifact' ? ' hidden' : ''}" data-release-field>
          <label>保留版本数</label>
          <input type="number" id="f-keep_releases" min="0" max="1000" value="${a(t.keep_releases !== undefined ? t.keep_releases : 5)}">
          <div class="hint">超出后自动清理最旧的发布</div>
        </div>
        <div class="field span-2">
          <label>准备脚本</label>
          <textarea id="f-prepare_script" rows="4" placeholder="# 构建命令，例如&#10;npm ci&#10;npm run build">${e(t.prepare_script || '')}</textarea>
          <div class="hint">在源码目录执行，用于安装依赖与构建（对应 CI 的 build 阶段）</div>
        </div>
        <div class="field span-2" data-method-field="script release systemd">
          <label>部署脚本<span class="req req-deploy_script hidden">*</span></label>
          <textarea id="f-deploy_script" rows="4" placeholder="# 发布后动作，例如&#10;systemctl restart myapp">${e(t.deploy_script || '')}</textarea>
          <div class="hint">在发布完成后执行；使用「自定义脚本」方式时必填</div>
        </div>
        <div class="field span-2">
          <label>回滚脚本</label>
          <textarea id="f-rollback_script" rows="3" placeholder="# 可选，回滚时执行">${e(t.rollback_script || '')}</textarea>
          <div class="hint" id="rollback-hint">回滚时在目标发布目录执行</div>
        </div>
        <div class="field" data-method-field="systemd">
          <label>systemd 服务名<span class="req req-service_name hidden">*</span></label>
          <input type="text" id="f-service_name" value="${a(t.service_name || '')}" placeholder="myapp.service">
        </div>
        <div class="field" data-method-field="docker">
          <label>镜像名称<span class="req req-docker_image hidden">*</span></label>
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
          <label>rsync 目标<span class="req req-rsync_target hidden">*</span></label>
          <input type="text" id="f-rsync_target" value="${a(t.rsync_target || '')}" placeholder="user@host:/var/www/myapp/">
        </div>
        <div class="field" data-method-field="rsync">
          <label>rsync 参数</label>
          <input type="text" id="f-rsync_options" value="${a(t.rsync_options || '-az --delete')}">
        </div>
      </div>

      <div class="section-title">触发与通知</div>
      <div class="form-grid cols-2">
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
                 value="${a(t.schedule_expression || scheduleDefaults.interval || '1h')}"
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

      ${isEdit && t.webhook_url ? `
      <div class="form-grid cols-2">
        <div class="field span-2">
          <label>触发地址</label>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="text" id="f-webhook_url" readonly value="${a(t.webhook_url)}" spellcheck="false" style="flex:1">
            <button type="button" class="ghost" id="webhook-copy">复制</button>
            <button type="button" class="ghost" id="webhook-reset">重新生成</button>
          </div>
          <div class="hint">向该地址发送 GET 或 POST 请求即触发一次部署（外部无需登录），适合 GitHub/Gitee 的 Webhook 或 <code class="code-inline">curl</code>。重新生成后旧地址立即失效。</div>
        </div>
      </div>` : `
      <div class="hint" style="margin:-6px 0 12px">任务创建后自动生成 Webhook 触发地址（向它发请求即触发部署），可在编辑页复制使用。</div>`}
      <div class="form-grid cols-2">
        <div class="field span-2">
          <label>结果通知 Webhook</label>
          <input type="text" id="f-notify_webhook" value="${a(t.notify_webhook || '')}" placeholder="https://...">
          <div class="hint">部署结束后把结果发送到该地址，留空使用全局设置；与上面的触发地址无关</div>
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

      <details class="adv-details"${hasAdvanced ? ' open' : ''}>
        <summary>高级参数（拉取深度、超时、环境变量）</summary>
        <div class="form-grid cols-2">
          <div class="field">
            <label>拉取深度</label>
            <input type="number" id="f-git_depth" min="0" max="10000" value="${a(t.git_depth !== undefined ? t.git_depth : 1)}">
            <div class="hint">1 = 浅克隆（最快）；0 = 完整历史</div>
          </div>
          <div class="field">
            <label>阶段超时（秒）</label>
            <input type="number" id="f-timeout_seconds" min="10" max="86400"
                   value="${a(t.timeout_seconds !== undefined ? t.timeout_seconds : (defaults.timeout_seconds || 1800))}">
            <div class="hint">单个执行命令/阶段的超时，不是整条流水线的总时限</div>
          </div>
          <div class="field span-2">
            <label>环境变量</label>
            <textarea id="f-env_vars" rows="3" placeholder="NODE_ENV=production&#10;API_BASE=https://api.example.com">${e(envToText(t.env_vars))}</textarea>
            <div class="hint">每行一条 <code class="code-inline">KEY=value</code>，会导出到准备与部署脚本（回滚脚本不注入这些变量）</div>
          </div>
        </div>
      </details>

      <div class="section-title">配置摘要</div>
      <div id="config-summary" class="config-summary"></div>
    `;

    const backdrop = AD.Modal.open({
      title: isEdit ? '编辑任务：' + t.name : '新建任务',
      size: 'wide',
      body,
      footerLeft: isEdit ? `<button class="danger" id="form-delete">删除任务</button>` : '<div class="left"></div>',
      footer: '<button data-close>取消</button>'
        + `<button class="primary" id="form-save">${isEdit ? '保存修改' : '创建任务'}</button>`,
      // 关闭关卡：有未保存修改时先确认，避免误触遮罩/Escape 丢配置。
      beforeClose: async () => {
        if (!formDirty) return true;
        return await AD.confirm({
          title: '放弃未保存的修改？',
          message: '表单中还有未保存的修改，关闭后会丢失。',
          confirmText: '放弃修改',
          cancelText: '继续编辑',
          danger: true,
        });
      },
    });

    let formDirty = false;
    const markDirty = () => { formDirty = true; };
    backdrop.addEventListener('input', markDirty, true);
    backdrop.addEventListener('change', markDirty, true);

    const methodSelect = backdrop.querySelector('#f-deploy_method');
    const summaryNode = backdrop.querySelector('#config-summary');
    const credSelect = backdrop.querySelector('#f-credential_id');
    const typeSelect = backdrop.querySelector('#f-schedule_type');
    const exprInput = backdrop.querySelector('#f-schedule_expression');
    const value = (id) => {
      const node = backdrop.querySelector(id);
      return node ? node.value.trim() : '';
    };

    // 认证方式读取：不用 :checked 选择器，逐个读 radio 属性。
    const scanAuthMode = () => {
      const radios = backdrop.querySelectorAll('input[name="f-auth_mode"]');
      for (const radio of radios) { if (radio.checked) return radio.value; }
      return 'none';
    };

    // 配置摘要：用自然语言实时概括“这个任务到底会做什么”。
    const renderSummary = () => {
      if (!summaryNode) return;
      const lines = [];
      const repoUrl = value('#f-repo_url');
      const branch = value('#f-repo_branch') || 'main';
      const subdir = value('#f-repo_subdir');
      lines.push('代码：' + (repoUrl || '（未填写仓库地址）') + ' @ ' + branch + (subdir ? '，子目录 ' + subdir : ''));
      const authMode = scanAuthMode();
      if (authMode === 'credential') {
        let credName = '';
        (credSelect ? credSelect.querySelectorAll('option') : []).forEach((option) => {
          if (String(option.value) === String(credSelect.value)) credName = option.textContent;
        });
        lines.push('认证：使用集中凭据' + (credName ? '「' + credName + '」' : ''));
      } else if (authMode === 'token') {
        lines.push('认证：任务内令牌' + (t.has_token ? '（已配置）' : ''));
      } else {
        lines.push('认证：无需认证' + (isEdit && t.has_token ? '（保存后清除已存令牌）' : ''));
      }
      const method = methodSelect.value;
      const fieldLabels = { deploy_script: '部署脚本', service_name: '服务', docker_image: '镜像',
        docker_command: '启动命令', docker_compose_file: 'Compose 文件',
        rsync_target: '目标', rsync_options: '参数' };
      const bits = (METHOD_FIELD_IDS[method] || []).map((id) => {
        const current = value('#f-' + id);
        return current ? fieldLabels[id] + ' ' + current : '';
      }).filter(Boolean);
      lines.push('部署：' + methodLabel(method) + (bits.length ? ' — ' + bits.join('；') : ''));
      const scheduleType = typeSelect.value;
      const expression = value('#f-schedule_expression');
      const enabledNode = backdrop.querySelector('#f-enabled');
      if (enabledNode && !enabledNode.checked) lines.push('触发：定时已停用，仅手动运行');
      else if (scheduleType === 'manual') lines.push('触发：仅手动触发');
      else if (scheduleType === 'interval') lines.push('触发：固定间隔 ' + (expression || '（未填写）'));
      else lines.push('触发：Cron ' + (expression || '（未填写）') + '（按 UTC 计算）');
      summaryNode.innerHTML = lines.map((line) => '<div>' + e(line) + '</div>').join('');
    };
    const refreshSummary = AD.debounce(renderSummary, 200);
    backdrop.addEventListener('input', refreshSummary, true);
    backdrop.addEventListener('change', refreshSummary, true);

    const applyMethodUI = () => {
      const method = methodSelect.value;
      backdrop.querySelectorAll('[data-method-field]').forEach((field) => {
        field.classList.toggle('hidden', !field.dataset.methodField.split(' ').includes(method));
      });
      // 打包路径对所有方式都生效（决定发布包内容）；发布位置对「仅打包」无意义。
      backdrop.querySelectorAll('[data-release-field]').forEach((field) => {
        field.classList.toggle('hidden', method === 'artifact');
      });
      const required = METHOD_REQUIRED[method] || [];
      METHOD_FIELD_ALL.forEach((id) => {
        const mark = backdrop.querySelector('.req-' + id);
        if (mark) mark.classList.toggle('hidden', !required.includes(id));
      });
      const desc = backdrop.querySelector('#method-desc');
      if (desc) desc.textContent = METHOD_DESCRIPTIONS[method] || '';
      const rollbackHint = backdrop.querySelector('#rollback-hint');
      if (rollbackHint) {
        rollbackHint.textContent = method === 'systemd'
          ? '回滚时在目标发布目录执行，随后重启服务 ' + (value('#f-service_name') || '(尚未填写服务名)')
          : '回滚时在目标发布目录执行；该方式回滚只切换本地软链，不会重建容器或同步远端';
      }
      renderSummary();
    };
    methodSelect.addEventListener('change', applyMethodUI);

    const applyAuthMode = () => {
      const mode = scanAuthMode();
      const credWrap = backdrop.querySelector('#auth-credential-wrap');
      const tokenWrap = backdrop.querySelector('#auth-token-wrap');
      if (credWrap) credWrap.classList.toggle('hidden', mode !== 'credential');
      if (tokenWrap) tokenWrap.classList.toggle('hidden', mode !== 'token');
      renderSummary();
    };
    backdrop.querySelectorAll('input[name="f-auth_mode"]').forEach((radio) => {
      radio.addEventListener('change', applyAuthMode);
    });

    // 凭据区：内联新建（叠层表单，保存后回填选择）与就地测试当前仓库。
    const createButton = backdrop.querySelector('#cred-create');
    if (createButton) {
      createButton.addEventListener('click', async () => {
        await AD.openCredentialForm(null, {
          stack: true,
          onSaved: async (saved) => {
            try {
              AD.state.credentials = (await AD.api.get('/api/credentials')).credentials || [];
              AD.state.credentialsError = false;
            } catch (err) {
              AD.state.credentialsError = true;
            }
            const select = backdrop.querySelector('#f-credential_id');
            if (!select) return;
            select.innerHTML = '<option value="">不使用（公开仓库）</option>'
              + (AD.state.credentials || []).map((c) =>
                `<option value="${c.id}"${saved && String(c.id) === String(saved.id) ? ' selected' : ''}>${e(c.name)}（${e(c.kind_label || c.kind)}）</option>`).join('');
            const credentialRadio = Array.from(backdrop.querySelectorAll('input[name="f-auth_mode"]'))
              .find((radio) => radio.value === 'credential');
            if (credentialRadio) { credentialRadio.checked = true; applyAuthMode(); }
          },
        });
      });
    }
    const testButton = backdrop.querySelector('#cred-test');
    if (testButton) {
      testButton.addEventListener('click', async () => {
        const selectedId = backdrop.querySelector('#f-credential_id')?.value || '';
        const repoUrl = value('#f-repo_url');
        if (!selectedId) { AD.toastError('请先选择要测试的凭据'); return; }
        if (!repoUrl) { AD.toastError('请先填写仓库地址'); return; }
        AD.setBusy(testButton, true, '测试中…');
        try {
          const result = await AD.api.post(`/api/credentials/${selectedId}/test`, { repo_url: repoUrl });
          if (result.ok) AD.toastSuccess('仓库访问测试通过：' + result.message);
          else AD.toastError('测试失败：' + result.message);
        } catch (err) {
          AD.toastError(err.message);
        }
        AD.setBusy(testButton, false);
      });
    }

    const webhookUrlInput = backdrop.querySelector('#f-webhook_url');
    if (webhookUrlInput) {
      backdrop.querySelector('#webhook-copy').addEventListener('click', () => {
        AD.copyToClipboard(webhookUrlInput.value);
      });
      const resetButton = backdrop.querySelector('#webhook-reset');
      resetButton.addEventListener('click', async () => {
        // 重新生成不经过「保存」就立即生效：必须先确认，不能当普通草稿修改。
        const confirmed = await AD.confirm({
          title: '重新生成触发地址',
          message: '旧触发地址会立即失效，使用旧地址的外部 Webhook 将无法再触发本任务。',
          confirmText: '重新生成',
          danger: true,
        });
        if (!confirmed) return;
        AD.setBusy(resetButton, true, '生成中…');
        try {
          const result = await AD.api.post(`/api/tasks/${taskId}/webhook/reset`);
          webhookUrlInput.value = result.webhook_url;
          AD.toastSuccess('已重新生成触发地址，旧地址已失效');
        } catch (err) {
          AD.toastError(err.message);
        } finally {
          AD.setBusy(resetButton, false);
        }
      });
    }

    const hint = backdrop.querySelector('#schedule-hint');
    const preview = backdrop.querySelector('#schedule-preview');

    const applyScheduleVisibility = () => {
      // manual 下表达式不会生效：禁用并置灰，避免"填了却不跑"的误解。
      const manual = typeSelect.value === 'manual';
      exprInput.disabled = manual;
      exprInput.classList.toggle('muted', manual);
      if (manual) {
        hint.textContent = '仅手动点击「运行」时执行，表达式不生效';
        preview.textContent = '';
        return;
      }
      hint.textContent = typeSelect.value === 'interval'
        ? '间隔格式：30s / 15m / 6h / 2d（最小 30 秒）'
        : 'Cron 格式：分 时 日 月 周，例如 0 */6 * * *（按 UTC 时间计算）';
    };

    // 两种表达式的草稿分开保存：interval 切 cron 不再沿用 1h 然后报错。
    let lastInterval = typeSelect.value === 'interval' ? exprInput.value : (scheduleDefaults.interval || '1h');
    let lastCron = typeSelect.value === 'cron' ? exprInput.value : (scheduleDefaults.cron || '0 * * * *');
    let currentType = typeSelect.value;

    const refreshPreview = AD.debounce(async () => {
      const type = typeSelect.value;
      if (type === 'manual') return;
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

    typeSelect.addEventListener('change', () => {
      const next = typeSelect.value;
      if (next !== currentType) {
        if (currentType === 'interval') lastInterval = exprInput.value;
        if (currentType === 'cron') lastCron = exprInput.value;
        // 表达式格式在两种类型间不互通：切到对应类型时恢复该类型的草稿。
        if (next === 'interval' && !/^\d+\s*[smhd]?$/i.test(exprInput.value.trim())) exprInput.value = lastInterval;
        if (next === 'cron' && exprInput.value.trim().split(/\s+/).length !== 5) exprInput.value = lastCron;
        currentType = next;
      }
      applyScheduleVisibility();
      refreshPreview();
      renderSummary();
    });
    exprInput.addEventListener('input', () => {
      if (typeSelect.value === 'interval') lastInterval = exprInput.value;
      if (typeSelect.value === 'cron') lastCron = exprInput.value;
      refreshPreview();
    });
    applyMethodUI();
    applyAuthMode();
    applyScheduleVisibility();
    refreshPreview();
    renderSummary();

    backdrop.querySelector('#form-save').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      const payload = collectForm(backdrop, { isEdit, hasToken: Boolean(t.has_token) });
      if ((!isEdit || payload.name !== t.name) && !/^[A-Za-z_]{1,80}$/.test(payload.name)) {
        showFormError(backdrop, '任务名只能包含 1–80 个英文字母或下划线，且必须全局唯一；中文说明请填写备注。');
        return;
      }
      if (!payload.repo_url) { showFormError(backdrop, '请填写仓库地址'); return; }

      // 方式专属必填项前置校验（与后端 _check_method_requirements 一致），
      // 让错误就地出现在对应字段而不是提交后才弹后端报错。
      const methodRequired = METHOD_REQUIRED[payload.deploy_method] || [];
      for (const id of methodRequired) {
        if (!String(payload[id] || '').trim()) {
          const labels = { deploy_script: '部署脚本', service_name: 'systemd 服务名',
            docker_image: '镜像名称', rsync_target: 'rsync 目标' };
          showFormError(backdrop, `部署方式为「${methodLabel(payload.deploy_method)}」时必须填写「${labels[id] || id}」`);
          const input = backdrop.querySelector('#f-' + id);
          if (input) {
            if (typeof input.focus === 'function') input.focus();
            input.classList.toggle('input-error', true);
            input.addEventListener('input', () => input.classList.remove('input-error'), { once: true });
          }
          return;
        }
      }

      AD.setBusy(button, true, isEdit ? '保存中…' : '创建中…');
      try {
        let result;
        if (isEdit) {
          result = await AD.api.put(`/api/tasks/${taskId}`, payload);
          AD.toastSuccess('任务已保存');
        } else {
          result = await AD.api.post('/api/tasks', payload);
          AD.toastSuccess(result.run_id ? ('任务已创建并开始运行 #' + result.run_id) : '任务已创建');
          // 「创建后立即运行」派发失败时后端返回 warning：不提示会让用户
          // 以为第一次部署已经开始了。
          if (result.warning) AD.toastError(result.warning);
        }
        // 保存期间用户可能已关闭本表单并打开了其它弹窗：此时只刷新数据，
        // 不再代关当前弹窗（旧实现会误关新打开的弹窗）。
        if (AD.Modal.top && AD.Modal.top() !== backdrop) {
          AD.render('tasks');
          return;
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
        // 删除确认（确认框 + 输入任务名）叠在表单之上：取消时表单与草稿
        // 原样保留，不再像旧实现那样先关表单再重开、丢掉未保存的修改。
        const deleted = await AD.deleteTask(taskId, t);
        if (deleted) AD.Modal.close();
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

    // 方式无关的字段在提交时就地清空：界面上切换方式保留输入（可切回），
    // 但不会把其它方式的旧值存成脏配置，也避免触发后端的方式互斥校验。
    const relevant = new Set(METHOD_FIELD_IDS[payload.deploy_method] || []);
    for (const id of METHOD_FIELD_ALL) {
      if (!relevant.has(id)) payload[id] = '';
    }

    // 认证方式三选一：选中的方式决定提交什么，其余认证字段不随表单漂移。
    let authMode = 'none';
    for (const radio of backdrop.querySelectorAll('input[name="f-auth_mode"]')) {
      if (radio.checked) { authMode = radio.value; break; }
    }
    if (authMode === 'credential') {
      const credentialNode = backdrop.querySelector('#f-credential_id');
      payload.credential_id = credentialNode && credentialNode.value ? Number(credentialNode.value) : null;
    } else {
      // 切到其它认证方式即表示不再使用集中凭据：显式解绑。
      payload.credential_id = null;
    }
    const tokenNode = backdrop.querySelector('#f-git_token');
    if (authMode === 'token' && tokenNode && tokenNode.value) payload.git_token = tokenNode.value;
    const clearNode = backdrop.querySelector('#f-clear_token');
    if (authMode === 'token' && clearNode && clearNode.checked) payload.clear_token = true;
    // 「无需认证」要名副其实：存量任务内令牌一并清除，否则仍会被用于拉取。
    if (authMode === 'none' && options.isEdit && options.hasToken) payload.clear_token = true;

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
        <tbody>${runs.map((run) => `<tr data-task-detail-run-row="${run.id}">
          <td class="mono">${run.id}</td>
          <td data-task-detail-run-status>${runBadgeHtml(run)}</td>
          <td class="mono dim truncate" style="max-width:220px" title="${a(run.commit_message)}">${e(AD.shortCommit(run.commit_after))} ${e(run.commit_message || '')}</td>
          <td class="dim">${e(AD.triggerLabel(run.trigger))}</td>
          <td class="dim nowrap" data-task-detail-run-duration>${e(AD.formatDuration(run.duration_ms))}</td>
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
      // 确认框是叠层弹窗：取消时详情弹窗仍在，无需重开。
      if (!confirmed) return;
      AD.setBusy(event.currentTarget, true, '回滚中…');
      try {
        // 回滚已改为后台运行：立即返回 run_id，进度在运行详情里轮询。
        const result = await AD.api.post(`/api/tasks/${taskId}/rollback`);
        AD.toastSuccess(result.message || '回滚已开始');
        AD.Modal.close();
        AD.openRunDetail(result.run_id);
      } catch (err) {
        AD.toastError(err.message);
        AD.setBusy(event.currentTarget, false);
      }
    });

    backdrop.querySelectorAll('[data-run-log]').forEach((button) => {
      button.addEventListener('click', () => AD.openRunDetail(Number(button.dataset.runLog)));
    });

    // 就地同步：弹窗打开期间运行结束，只更新对应行，不重建弹窗。
    AD.syncTaskDetailRun = (run) => {
      if (!backdrop.isConnected || Number(run.task_id) !== Number(taskId)) return;
      const row = backdrop.querySelector(`[data-task-detail-run-row="${run.id}"]`);
      if (!row) return;
      const statusCell = row.querySelector('[data-task-detail-run-status]');
      if (statusCell) statusCell.innerHTML = runBadgeHtml(run);
      const durationCell = row.querySelector('[data-task-detail-run-duration]');
      if (durationCell) durationCell.textContent = AD.formatDuration(run.duration_ms);
    };
    const observer = new MutationObserver(() => {
      if (!document.getElementById('modal-root').firstChild) {
        AD.syncTaskDetailRun = null;
        observer.disconnect();
      }
    });
    observer.observe(document.getElementById('modal-root'), { childList: true });
  };

  /**
   * 运行结束后的就地收敛：读取该运行的最新记录并同步到引用它的行与面板
   * （任务行、行内展开面板、任务详情弹窗、运行记录行），不重绘整个视图。
   */
  AD.refreshRunAfterFinish = async function (runId) {
    let run;
    try { run = (await AD.api.get(`/api/runs/${runId}`)).run; }
    catch (err) { return; }
    if (!run) return;
    AD.syncRunInPlace(run);
    // 任务详情弹窗若正打开着同一个任务，刷新它的「最近运行」表格。
    AD.syncTaskDetailRun?.(run);
  };

  // ======================================================================
  // 运行状态的就地同步
  // ======================================================================
  // 运行结束时只更新引用该运行的节点（任务行、行内面板、运行记录行），不再
  // 重建整个视图：整页重绘会丢掉滚动位置、展开状态和用户正在查看的日志，
  // 也会让「查看日志」这类只读操作看起来像页面刷新。

  /** 运行状态徽标：初始渲染与就地同步共用，避免两处显示不一致。 */
  function runBadgeHtml(run) {
    const active = run.is_active !== undefined && run.is_active !== null
      ? Boolean(run.is_active)
      : (run.status === 'queued' || run.status === 'running');
    return `<span class="badge ${e(run.status)}">`
      + (active ? `<span class="dot ${e(run.status)}"></span>` : '')
      + `${e(AD.statusLabel(run.status))}</span>`;
  }

  /** 运行行的操作按钮：取消按钮只存在于仍在排队或运行中的记录。 */
  function runActionsHtml(run) {
    return `<button class="sm" data-run-log="${run.id}">日志</button>`
      + (run.is_active ? `<button class="sm danger" data-run-cancel="${run.id}">取消</button>` : '');
  }

  // 当前展开的行内面板（同一时刻至多一个）的就地更新入口。
  let inlinePanelSync = null;
  AD.setInlinePanelSync = (sync) => { inlinePanelSync = sync; };
  AD.syncInlinePanelRun = (taskId, run) => {
    if (inlinePanelSync && inlinePanelSync.taskId === Number(taskId)) inlinePanelSync.applyRun(run);
  };
  AD.syncInlinePanelTask = (task) => {
    if (inlinePanelSync && inlinePanelSync.taskId === Number(task.id)) inlinePanelSync.applyTask(task);
  };

  /** 把一条运行的最新状态同步到页面上所有引用它的节点。
   *  taskId 由调用方已知时显式传入，避免依赖运行记录里是否带 task_id。 */
  AD.syncRunInPlace = function (run, taskId) {
    if (!run || run.id === null || run.id === undefined) return;
    const runId = Number(run.id);

    // 运行记录表格：只改这一行的徽标、耗时与操作按钮。
    const row = document.querySelector(`tr[data-run-row="${runId}"]`);
    if (row) {
      const statusCell = row.querySelector('[data-run-status]');
      if (statusCell) statusCell.innerHTML = runBadgeHtml(run);
      const durationCell = row.querySelector('[data-run-duration]');
      if (durationCell && run.duration_ms !== undefined) {
        durationCell.textContent = AD.formatDuration(run.duration_ms);
      }
      const actions = row.querySelector('[data-run-actions]');
      if (actions) actions.innerHTML = runActionsHtml(run);
    }

    // 任务行与行内面板：按 task_id 精确定位，同样不触碰表格其余部分。
    const owner = taskId === undefined || taskId === null ? run.task_id : taskId;
    if (owner !== null && owner !== undefined) {
      AD.refreshTaskRowInPlace(Number(owner));
      AD.syncInlinePanelRun(owner, run);
    }
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

      <div class="panel" id="runs-table">
        ${data.runs.length ? `<div class="table-wrap"><table>
          <thead><tr>
            <th>#</th><th>任务</th><th>状态</th><th>触发</th><th>提交</th>
            <th>变更</th><th>耗时</th><th>时间</th><th class="right">操作</th>
          </tr></thead>
          <tbody>${data.runs.map((run) => `<tr data-run-row="${run.id}">
            <td class="mono dim">${run.id}</td>
            <td class="truncate" style="max-width:170px" title="${a(run.task_name)}">${e(run.task_name)}</td>
            <td data-run-status>${runBadgeHtml(run)}</td>
            <td class="dim nowrap">${e(AD.triggerLabel(run.trigger))}</td>
            <td class="mono dim truncate" style="max-width:230px" title="${a(run.commit_message || '')}">
              ${run.commit_after ? e(AD.shortCommit(run.commit_after)) + ' ' + e(run.commit_message || '') : '<span class="faint">—</span>'}
            </td>
            <td class="dim nowrap">${run.changed_files ? run.changed_files + ' 个文件' : (run.changes_detected ? '—' : '无变化')}</td>
            <td class="dim nowrap" data-run-duration>${e(AD.formatDuration(run.duration_ms))}</td>
            <td class="dim nowrap" title="${a(AD.formatTime(run.queued_at, true))}">${e(AD.formatRelative(run.queued_at))}</td>
            <td>
              <div class="table-actions" data-run-actions>${runActionsHtml(run)}</div>
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

    // 事件委托挂在本视图新建的节点上：表格就地更新后按钮依然有效，且每次
    // 渲染重新绑定，不会在 #content 上累积监听器。
    const tablePanel = container.querySelector('#runs-table');
    tablePanel.addEventListener('click', async (event) => {
      const logButton = event.target.closest('[data-run-log]');
      if (logButton) { AD.openRunDetail(Number(logButton.dataset.runLog)); return; }
      const cancelButton = event.target.closest('[data-run-cancel]');
      if (!cancelButton) return;
      AD.setBusy(cancelButton, true);
      try {
        const result = await AD.api.post(`/api/runs/${cancelButton.dataset.runCancel}/cancel`);
        AD.toastSuccess(result.message || '已请求取消');
      } catch (err) {
        AD.toastError(err.message);
        AD.setBusy(cancelButton, false);
      }
      // 取消是异步的（进程在安全点退出），状态由轮询就地收敛，这里不重绘。
      AD.pollRunStates();
    });

    // 就地更新：只同步状态变化了的行，不重建表格。没有活跃运行时不发请求。
    AD.state.viewRefresh = async () => {
      if (!container.querySelector('[data-run-cancel]')) return;
      const params = new URLSearchParams({ limit: '60' });
      if (runFilter.status) params.set('status', runFilter.status);
      if (runFilter.task_id) params.set('task_id', runFilter.task_id);
      if (runFilter.search) params.set('search', runFilter.search);
      const fresh = await AD.api.get('/api/runs?' + params.toString());
      const byId = new Map((fresh.runs || []).map((run) => [run.id, run]));
      for (const run of byId.values()) AD.syncRunInPlace(run);
    };
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
    // 与行内日志一致的上限：超长日志只保留尾部 800 行，避免 DOM 与内存膨胀。
    const LOG_DOM_LIMIT = 800;

    const paint = (lines, reset) => {
      if (reset) { buffer = []; logEl.innerHTML = ''; }
      if (!lines.length && !buffer.length) {
        logEl.innerHTML = '<span class="log-empty">暂无日志输出</span>';
        return;
      }
      buffer = buffer.concat(lines);
      if (buffer.length > LOG_DOM_LIMIT) buffer = buffer.slice(-LOG_DOM_LIMIT);
      const atBottom = followEl.checked;
      const html = lines.map((line) => `<div class="log-line">${colorize(line)}</div>`).join('');
      logEl.insertAdjacentHTML('beforeend', html);
      while (logEl.childElementCount > LOG_DOM_LIMIT) logEl.removeChild(logEl.firstElementChild);
      metaEl.textContent = '共 ' + lineCount + ' 行（显示最近 ' + buffer.length + ' 行）';
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
          // 取消按钮只对活跃运行有意义；运行结束后就地移除，避免点击报错。
          cancelButton?.remove();
          // 底层列表就地收敛，不重绘整个视图（重绘会丢失滚动与展开状态，
          // 也会让「查看日志」看起来像页面刷新）。
          await AD.refreshRunAfterFinish(runId);
        }
      } catch (err) {
        AD.stopLiveLog();
      }
    };

    AD.state.currentRunId = runId;
    AD.state.liveTimer = setInterval(poll, 1200);
    // 首探也纳入清理：弹窗关闭后不再补发这一次探测。
    const kickoff = setTimeout(() => {
      if (AD.state.liveTimer) poll();
    }, 250);
    AD.stopLiveLog = function () {
      if (AD.state.liveTimer) { clearInterval(AD.state.liveTimer); AD.state.liveTimer = null; }
      clearTimeout(kickoff);
      AD.state.currentRunId = null;
    };

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
        <div id="upd-body"></div>
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
        <button id="cred-guide">怎么获取？</button>
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
    container.querySelector('#cred-guide')?.addEventListener('click', () => AD.openCredentialGuide());

    container.querySelectorAll('[data-cred-pubkey]').forEach((b) =>
      b.addEventListener('click', () => AD.showCredentialPublicKey(Number(b.dataset.credPubkey))));

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
          ${item.kind === 'ssh_key' ? `<button class="sm" data-cred-pubkey="${item.id}">公钥</button>` : ''}
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

  // options.stack: 叠层打开（如从任务表单内联新建凭据，不关闭底层表单）；
  // options.onSaved: 保存成功后的回调，传入保存后的凭据。缺省行为是刷新
  // 凭据页（从凭据列表打开时保持原交互）。
  AD.openCredentialForm = async function (credentialId, options) {
    const formOptions = options || {};
    let item = null;
    if (credentialId) {
      const data = await AD.api.get(`/api/credentials/${credentialId}`);
      item = data.credential;
    }
    const kinds = await AD.api.get('/api/credentials/kinds');
    // 令牌获取指引与凭据页共用同一后端来源；拉取失败只影响帮助折叠块。
    let tokenGuide = null;
    try {
      tokenGuide = (await AD.api.get('/api/credentials/guide')).kinds
        .find((k) => k.kind === 'https_token') || null;
    } catch (err) { /* 指引仅是辅助，失败不阻塞表单 */ }
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
        <div class="row hidden" id="c-generate-row" style="margin-bottom:8px">
          <button class="sm" id="c-generate">自动生成密钥对</button>
          <select id="c-key-type" class="sm" style="width:auto">
            <option value="ed25519">ED25519（推荐）</option>
            <option value="rsa">RSA 4096（兼容旧环境）</option>
          </select>
          <span class="faint" id="c-generate-hint">不必手工运行 ssh-keygen</span>
        </div>
        <textarea id="c-secret" rows="5" placeholder=""></textarea>
        <div class="hint" id="c-secret-hint"></div>
        <div id="c-token-help" class="hidden" style="margin-top:8px">
          <details>
            <summary class="hint" style="cursor:pointer">没有令牌？点开查看如何生成（GitHub）</summary>
            <ol class="cred-steps" style="margin-top:8px" id="c-token-steps"></ol>
            <div class="hint" id="c-token-notes" style="margin-top:8px"></div>
          </details>
        </div>
      </div>
      <div class="field hidden" id="c-pubkey-wrap">
        <div id="c-pubkey-body"></div>
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
      stack: Boolean(formOptions.stack),
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
      // HTTPS 令牌的内联生成指引；SSH 有自己的「自动生成密钥对」，不需要它。
      const tokenHelp = backdrop.querySelector('#c-token-help');
      tokenHelp.classList.toggle('hidden', kind !== 'https_token' || !tokenGuide);
      if (kind === 'https_token' && tokenGuide) {
        backdrop.querySelector('#c-token-steps').innerHTML =
          tokenGuide.steps.map((s) => `<li>${e(s)}</li>`).join('');
        backdrop.querySelector('#c-token-notes').innerHTML =
          tokenGuide.notes.map((n) => `<div>· ${e(n)}</div>`).join('');
      }
      backdrop.querySelector('#c-username-wrap').classList.toggle('hidden', false);
      backdrop.querySelector('#c-passphrase-wrap').classList.toggle('hidden', kind !== 'ssh_key');
      // 只有新建 SSH 凭据才需要「自动生成」：已有凭据的私钥不回传，
      // 没有可替换的对象。
      backdrop.querySelector('#c-generate-row').classList.toggle('hidden', kind !== 'ssh_key' || isEdit);
      if (kind !== 'ssh_key') backdrop.querySelector('#c-pubkey-wrap').classList.add('hidden');
      if (isEdit) {
        secretInput.placeholder = t.has_secret ? '已保存，留空表示不修改' : '';
        secretInput.required = false;
      }
    };
    kindSelect.addEventListener('change', applyKind);
    applyKind();

    // 自动生成密钥对：私钥回填到输入框（保存时才提交），公钥立即展示供复制。
    // 这样用户不必运行 ssh-keygen，也不会把公钥私钥搞混。
    backdrop.querySelector('#c-generate')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      AD.setBusy(button, true, '生成中…');
      try {
        const result = await AD.api.post('/api/credentials/generate-keypair', {
          key_type: backdrop.querySelector('#c-key-type').value,
          comment: backdrop.querySelector('#c-name').value.trim() || 'autodeploy',
        });
        backdrop.querySelector('#c-secret').value = result.private_key;
        const wrap = backdrop.querySelector('#c-pubkey-wrap');
        wrap.classList.remove('hidden');
        backdrop.querySelector('#c-pubkey-body').innerHTML =
          publicKeyPanel(result.public_key, result.fingerprint, {
            intro: '密钥已生成，私钥已填入下方输入框（保存后不再回传）。'
                 + '现在请把公钥添加到 GitHub 的 Deploy Keys：',
          });
        bindPublicKeyPanel(backdrop, result.public_key);
        backdrop.querySelector('#c-username').value =
          backdrop.querySelector('#c-username').value.trim() || 'git';
        backdrop.querySelector('#c-generate-hint').textContent = '已生成，可直接保存';
        AD.toastSuccess('密钥已生成，请复制公钥到 GitHub');
      } catch (err) {
        credError(backdrop, err.message);
      }
      AD.setBusy(button, false);
    });

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
        let result;
        if (isEdit) result = await AD.api.put(`/api/credentials/${credentialId}`, payload);
        else result = await AD.api.post('/api/credentials', payload);
        AD.toastSuccess(isEdit ? '凭据已保存' : '凭据已创建');
        AD.Modal.close();
        if (formOptions.onSaved) await formOptions.onSaved(result.credential);
        else AD.render('credentials');
      } catch (err) {
        credError(backdrop, err.message);
        AD.setBusy(button, false);
      }
    });
  };

  // ----------------------------------------------------------------------
  // 公钥展示：生成密钥或查看已有 SSH 凭据时复用同一块 UI。
  // 公钥不是机密，可以放心显示与复制；私钥永不进入这里。
  // ----------------------------------------------------------------------
  function publicKeyPanel(publicKey, fingerprint, options) {
    const opts = options || {};
    return `
      <div class="alert info">
        ${opts.intro || '把下面这行公钥添加到 GitHub 的 Deploy Keys，私钥会安全保存在服务端。'}
      </div>
      <div class="field">
        <label>公钥（复制这一整行）</label>
        <textarea id="pk-value" rows="3" readonly class="mono">${e(publicKey)}</textarea>
        <div class="row" style="margin-top:8px">
          <button class="primary sm" id="pk-copy">复制公钥</button>
          <span class="faint" id="pk-copied"></span>
        </div>
      </div>
      ${fingerprint ? `<div class="field">
        <label>指纹</label>
        <div class="mono">${e(fingerprint)}</div>
        <div class="hint">指纹只是用来核对：与 GitHub 上显示的一致即表示两处是同一把钥匙。</div>
      </div>` : ''}
      <div class="section-title">接下来这样配置</div>
      <ol class="cred-steps">
        <li>打开仓库的 <span class="code-inline">Settings → Deploy Keys → Add deploy key</span></li>
        <li>把公钥粘贴到 <span class="code-inline">Key</span> 输入框。</li>
        <li><strong>不要勾选</strong> <span class="code-inline">Allow write access</span>——部署只需要读取权限。</li>
        <li>保存后回到本页点「测试」验证是否能连上仓库。</li>
      </ol>
    `;
  }

  function bindPublicKeyPanel(backdrop, publicKey) {
    backdrop.querySelector('#pk-copy')?.addEventListener('click', async () => {
      await AD.copyToClipboard(publicKey);
      const note = backdrop.querySelector('#pk-copied');
      if (note) note.textContent = '已复制，去 GitHub 粘贴即可';
    });
  }

  /** 查看已有 SSH 凭据的公钥，便于重新添加 Deploy Key 或核对指纹。 */
  AD.showCredentialPublicKey = async function (credentialId) {
    let data;
    try {
      data = await AD.api.get(`/api/credentials/${credentialId}/public-key`);
    } catch (err) { AD.toastError(err.message); return; }
    const backdrop = AD.Modal.open({
      title: 'SSH 公钥',
      body: '<div id="pk-body">' + publicKeyPanel(data.public_key, data.fingerprint, {
        intro: '这是该凭据对应的公钥。如果服务器已重装或 GitHub 上的 Deploy Key 被删除，'
             + '可以重新复制这行公钥添加回去，无需更换凭据。',
      }) + '</div>',
      footer: '<button data-close>关闭</button>',
    });
    bindPublicKeyPanel(backdrop, data.public_key);
  };

  /** 凭据获取指引：把「去哪点、要什么权限」讲清楚。 */
  AD.openCredentialGuide = async function () {
    let guide;
    try { guide = await AD.api.get('/api/credentials/guide'); }
    catch (err) { AD.toastError(err.message); return; }

    const section = (item) => `
      <div class="cred-guide-block">
        <h3>${e(item.label)}<span class="badge neutral" style="margin-left:8px">${e(item.best_for)}</span></h3>
        <div class="field">
          <label>在 GitHub 上操作</label>
          <div><a class="code-inline" href="${a(item.create_url)}" target="_blank" rel="noopener noreferrer">${e(item.create_url)}</a></div>
          <div class="hint">在新标签页打开。${item.create_url.includes('REPO_OWNER')
            ? '把链接里的 REPO_OWNER/REPO_NAME 换成你自己的仓库路径。' : ''}</div>
        </div>
        <ol class="cred-steps">${item.steps.map((s) => `<li>${e(s)}</li>`).join('')}</ol>
        <div class="hint" style="margin-top:8px">
          ${item.notes.map((n) => `<div>· ${e(n)}</div>`).join('')}
        </div>
      </div>`;

    const backdrop = AD.Modal.open({
      title: '如何获取仓库凭据',
      size: 'wide',
      body: `<div class="alert info">${e(guide.intro)}</div>
             ${guide.kinds.map(section).join('<div class="divider"></div>')}`,
      footer: '<button data-close>知道了</button>',
    });
    return backdrop;
  };

  function credError(backdrop, message) {
    const box = backdrop.querySelector('#cred-error');
    box.textContent = message;
    box.classList.remove('hidden');
  }


  // ======================================================================
  // 一键更新
  //
  // 卡片只占一行：当前版本 / 最新版本 / 升级日志 / 配置。所有状态变化都只重绘
  // 行与进度区这两个容器，绝不整页重渲染——否则正在看的升级日志、滚动位置和
  // 页面上的其它交互都会丢失。
  // ======================================================================
  let updateSession = null;
  const versionText = (v) => 'v' + String(v || '').replace(/^[vV]+/, '');
  const bareVersion = (v) => String(v || '').replace(/^[vV]+/, '');
  // 本页面加载的 views.js 内容指纹：与 status.frontend_fingerprint 一致说明
  // 运行的已是新前端，升级成功后不再要求刷新页面。
  const pageViewsFingerprint = (() => {
    try {
      const scripts = document.querySelectorAll('script[src]');
      for (const script of scripts) {
        const match = /\/assets\/views\.js\?v=([0-9a-f]+)/.exec(script.getAttribute('src'));
        if (match) return match[1];
      }
      return '';
    } catch (err) { return ''; }
  })();
  const UPD_STAGE_LABELS = {
    queued: '排队中', checking: '检查中', downloading: '下载中', backing_up: '备份代码',
    applying: '替换代码', dependencies: '更新依赖', pulling_image: '拉取镜像',
    recreating: '重建容器', restarting: '等待重启确认',
    preparing: '升级准备', prepared: '等待切换', switching: '切换版本',
    executing: '升级执行器运行中', verifying: '验证新版本',
    rolled_back: '已恢复旧版本', attention: '需要人工处理', recovery_required: '需要人工恢复',
    done: '已确认完成', failed: '失败', error: '失败', idle: '尚未更新',
    unverified: '历史操作未确认',
  };
  // 步骤条：Docker 与裸机各自的阶段序列，drawUpdateProgress 用来画进度。
  // Docker 新模型为独立执行器容器（拉取 → 执行器运行 → 验证）；旧版本写入的
  // docker-recreate 状态仍按「拉取 → 重建 → 重启」展示。
  const UPD_STAGE_STEPS = {
    docker: ['pulling_image', 'executing', 'verifying'],
    docker_recreate: ['pulling_image', 'recreating', 'restarting'],
    bare: ['downloading', 'backing_up', 'applying', 'dependencies', 'restarting'],
  };

  AD.stopUpdatePanel = function () {
    if (!updateSession) return;
    updateSession.stopped = true;
    clearTimeout(updateSession.timer);
    updateSession.controllers.forEach((controller) => controller.abort());
    updateSession = null;
  };
  window.addEventListener('pagehide', AD.stopUpdatePanel);

  AD.renderUpdatePanel = function (panel) {
    const previous = updateSession?.panel === panel ? updateSession : null;
    AD.stopUpdatePanel();
    if (!panel) return;
    const ctx = {
      panel, stopped: false, timer: null, failures: 0, polls: 0,
      deadline: Date.now() + 30 * 60 * 1000,
      state: previous?.state || null,
      check: previous?.check || null,
      checking: false,
      settings: previous?.settings || null,
      // 部署形态与升级能力（由状态接口带出）；不可自升级时行内只显示引导。
      runMode: previous?.runMode || null,
      rollbackOpen: false,
      // 正在预览的滚动位置；重绘日志时要还原，不能把读者拽回底部。
      logView: previous?.logView || { top: 0, left: 0, follow: true },
      rowSig: null, progSig: null,
      controllers: new Set(),
    };
    updateSession = ctx;
    panel.querySelector('#upd-body').innerHTML =
      '<div class="upd-row" id="upd-row"></div>'
      + '<div id="upd-alerts"></div>'
      + '<div id="upd-progress"></div>';
    loadUpdateSettings(ctx);  // 更新源与代理说明用于确认弹窗和配置弹窗
    loadUpdateHistory(ctx);   // 预载升级历史：首次进页面即可渲染「回退 ▾」（失败静默）
    drawUpdateRow(ctx);
    drawUpdateProgress(ctx);
    pollUpdateStatus(ctx);    // 先恢复持久化状态，不用 health 推断成功。
    // 进页面自动静默检查一次：有缓存时后端直接返回缓存，不打扰用户。
    // 是否执行由首轮状态轮询决定（进行中的操作不并发检查），见 pollUpdateStatus。
  };
  const updateAlive = (ctx) => !ctx.stopped && ctx.panel.isConnected;

  async function updateRequest(ctx, path, method = 'GET', payload, timeout = 10000) {
    const controller = new AbortController();
    ctx.controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), timeout);
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
    } finally { clearTimeout(timer); ctx.controllers.delete(controller); }
  }

  function updateError(ctx, message) {
    if (!updateAlive(ctx)) return;
    const box = ctx.panel.querySelector('#upd-alerts');
    box.innerHTML = '<div id="upd-error" class="alert error">' + e(message)
      + '<div><button id="upd-resume">重新读取状态</button></div></div>';
    box.querySelector('#upd-resume')?.addEventListener('click', () => AD.renderUpdatePanel(ctx.panel));
  }

  async function loadUpdateSettings(ctx) {
    try {
      const data = await updateRequest(ctx, '/api/settings');
      if (updateAlive(ctx)) ctx.settings = data;
    } catch (err) { /* 说明文字退化为默认文案，不影响升级主流程 */ }
  }

  /** 预载升级历史供回退菜单使用；失败静默，点击展开菜单时仍会重新拉取。 */
  async function loadUpdateHistory(ctx) {
    try {
      const data = await updateRequest(ctx, '/api/system/self-update/history', 'GET', undefined, 20000);
      if (updateAlive(ctx)) { ctx.historyData = data; drawUpdateRow(ctx); }
    } catch (err) { /* 回退菜单退化为点击展开时再拉取，不影响升级主流程 */ }
  }

  /** 严格确认：重启后启动身份与版本对齐才认定成功；跨容器 PID 可能相同，不作依据。 */
  function updateConfirmed(state) {
    return state.stage === 'done' && state.confirmed_operation_id === state.operation_id && state.operation_id
      && state.confirmed_operation === state.operation && ['update', 'rollback'].includes(state.operation)
      && state.boot_id && state.boot_id !== state.before_boot_id
      // 仅在后端给出 expected_version 时校验版本一致；缺失时不阻断确认。
      && (!state.expected_version || (bareVersion(state.current_version) === state.expected_version
        && bareVersion(state.version) === state.expected_version));
  }

  // ----------------------------------------------------------- 单行展示区
  function canSelfUpgrade(ctx) {
    // 形态未知时按可升级渲染，点确认时后端会给出准确原因；
    // docker_no_socket 明确不可升级，提前分流。
    return ctx.runMode !== 'docker_no_socket';
  }

  function drawUpdateRow(ctx) {
    const row = ctx.panel.querySelector('#upd-row');
    if (!row) return;
    const state = ctx.state || {};
    const current = state.current_version || (ctx.check && ctx.check.current) || '';
    const active = Boolean(state.active);
    const isAdmin = Boolean(AD.state.user && AD.state.user.is_admin);
    let latest;
    if (ctx.checking) {
      latest = '<span class="upd-latest dim">正在检查最新版本…</span>';
    } else if (!ctx.check) {
      latest = '<span class="upd-latest dim">尚未检查</span>';
    } else if (ctx.check.error) {
      latest = '<span class="upd-latest danger" title="' + a(ctx.check.error) + '">检查失败：'
        + e(ctx.check.error) + '</span><button class="ghost sm" id="upd-recheck">重试</button>';
    } else if (ctx.check.update_available) {
      const target = versionText(ctx.check.latest);
      if (!isAdmin) {
        latest = '<span class="upd-latest">最新版本 <strong class="mono">' + e(target) + '</strong></span>'
          + '<span class="dim">升级需要管理员登录</span>';
      } else if (!canSelfUpgrade(ctx)) {
        latest = '<span class="upd-latest">最新版本 <strong class="mono">' + e(target) + '</strong></span>'
          + '<span class="upd-hint-block danger" id="upd-hint">' + e(state.upgrade_hint || '当前环境不支持面板内升级，请使用服务器上的一键脚本升级。') + '</span>';
      } else {
        latest = '<span class="upd-latest">最新版本 <strong class="mono">' + e(target) + '</strong></span>'
          + '<button class="icon-btn run" id="upd-upgrade" title="一键升级到 ' + a(target) + '"'
          + (active ? ' disabled' : '') + ' aria-label="升级到 ' + a(target) + '">' + ICONS.upgrade + '</button>';
      }
    } else {
      latest = '<span class="upd-latest dim">当前已是最新版本</span>';
    }
    // 轮询每 1.5s 触发一次；只有内容真的变了才重建节点，避免打断悬停与焦点。
    // 历史预载完成后也要重建一次：回退按钮依赖 ctx.historyData。
    const signature = [current, active, ctx.checking, latest, ctx.rollbackOpen, isAdmin,
      Boolean(ctx.historyData)].join('|');
    if (signature === ctx.rowSig) return;
    ctx.rowSig = signature;
    row.innerHTML = '<span class="upd-item">当前版本 <strong class="mono">'
      + e(current ? versionText(current) : '未知') + '</strong></span>'
      + '<span class="upd-item" id="upd-latest">' + latest + '</span>'
      + '<div class="spacer"></div>'
      + (isAdmin ? '<div class="upd-rollback-wrap" id="upd-rollback-wrap"></div>' : '')
      + '<button class="sm" id="upd-history">升级日志</button>'
      + (isAdmin ? '<button class="sm" id="upd-config">配置</button>' : '');
    row.querySelector('#upd-recheck')?.addEventListener('click', () => runUpdateCheck(ctx, true));
    row.querySelector('#upd-upgrade')?.addEventListener('click',
      () => startUpdateOperation(ctx, 'update', ctx.check.latest));
    row.querySelector('#upd-history')?.addEventListener('click', () => openUpdateHistory(ctx));
    row.querySelector('#upd-config')?.addEventListener('click', () => openUpdateConfig(ctx));
    if (isAdmin) drawRollbackMenu(ctx, row.querySelector('#upd-rollback-wrap'), active);
  }

  /** 主行「回退 ▾」：有可回退目标时才渲染；菜单内容来自升级历史。 */
  function drawRollbackMenu(ctx, wrap, active) {
    if (!wrap) return;
    const targets = rollbackTargets(ctx);
    if (!targets.length) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = '<button class="sm" id="upd-rollback" aria-haspopup="true"'
      + (active || ctx.rollbackOpen ? ' disabled' : '') + '>回退 ▾</button>'
      + (ctx.rollbackOpen ? '<div class="upd-rollback-menu" id="upd-rollback-menu">'
        + '<div class="hint">回退到：</div>'
        + targets.map((item) => '<button class="sm menu-item" data-upd-rollback="'
          + a(item.value) + '" data-upd-rollback-kind="' + a(item.kind) + '">'
          + e(item.label) + '</button>').join('')
        + '</div>' : '');
    wrap.querySelector('#upd-rollback')?.addEventListener('click', () => {
      if (ctx.rollbackOpen) { ctx.rollbackOpen = false; drawUpdateRow(ctx); return; }
      // 菜单内容来自升级历史，展开时才拉取（幂等且轻量）。
      updateRequest(ctx, '/api/system/self-update/history', 'GET', undefined, 20000)
        .then((data) => { if (updateAlive(ctx)) { ctx.historyData = data; ctx.rollbackOpen = true; drawUpdateRow(ctx); } })
        .catch(() => { if (updateAlive(ctx)) updateError(ctx, '读取可回退版本失败'); });
    });
    wrap.querySelectorAll('[data-upd-rollback]').forEach((button) => {
      button.addEventListener('click', () => {
        const kind = button.dataset.updRollbackKind;
        const value = button.dataset.updRollback;
        ctx.rollbackOpen = false;
        drawUpdateRow(ctx);
        startUpdateOperation(ctx, 'rollback', value, kind === 'image' ? { target_image: value } : {});
      });
    });
  }

  /** 可回退目标清单：Docker=升级历史里的旧镜像引用，裸机=通过校验的备份。 */
  function rollbackTargets(ctx) {
    const data = ctx.historyData;
    if (!data) return [];
    const mode = ctx.runMode || data.run_mode || '';
    const current = data.current_version || (ctx.state && ctx.state.current_version) || '';
    const targets = [];
    if (mode === 'docker') {
      const seen = new Set();
      (data.entries || []).forEach((entry) => {
        const ref = String(entry.from_image || '');
        if (!ref || seen.has(ref)) return;
        seen.add(ref);
        const tag = ref.split(':').pop() || ref;
        if (tag && bareVersion(current) && tag === bareVersion(current)) return;
        targets.push({ kind: 'image', value: ref, label: versionText(tag) });
      });
    } else {
      (data.backups || []).forEach((item) => {
        if (bareVersion(item.version) === bareVersion(current)) return;
        targets.push({ kind: 'version', value: item.version, label: versionText(item.version) });
      });
    }
    return targets.slice(0, 5);
  }

  // ------------------------------------------------------------- 进度区域
  function drawUpdateProgress(ctx) {
    const box = ctx.panel.querySelector('#upd-progress');
    if (!box) return;
    const state = ctx.state;
    const view = ctx.logView;
    // 重绘前记住阅读位置：正在翻升级日志时不能被拉到底部或顶部。
    const previousLog = box.querySelector('#upd-log');
    if (previousLog && previousLog.clientHeight > 0) {
      view.top = previousLog.scrollTop;
      view.left = previousLog.scrollLeft;
      view.follow = previousLog.scrollHeight - previousLog.clientHeight - previousLog.scrollTop <= 32;
    }
    if (!state || (state.stage === 'idle' && !state.operation && !state.error)) {
      if (box.innerHTML) box.innerHTML = '';
      ctx.progSig = '';
      return;
    }
    const confirmed = updateConfirmed(state);
    const unverified = state.stage === 'unverified' || (state.stage === 'done' && !confirmed)
      || (state.stage === 'restarting' && !state.active);
    const failed = Boolean(state.error) || ['failed', 'error'].includes(state.stage);
    // 委托执行器的协同阶段用专属 tone 与按钮区，不落入通用的失败/未确认分支。
    const stageTone = { attention: 'warning', recovery_required: 'error', rolled_back: 'warning' }[state.stage] || '';
    const tone = stageTone || (failed ? 'error' : unverified ? 'warning' : confirmed ? 'success'
      : state.active ? 'info' : 'neutral');
    const label = unverified ? '历史操作未确认' : UPD_STAGE_LABELS[state.stage] || state.stage || '状态未知';
    const logs = state.log || [];
    // 旧记录（restart === 'docker-recreate'）沿用旧步骤序列；新模型为执行器容器
    // 三步。preparing/prepared/switching 都折叠到「升级执行器运行中」这一步。
    const steps = (state.restart === 'docker-recreate' ? UPD_STAGE_STEPS.docker_recreate
      : ctx.runMode === 'docker' ? UPD_STAGE_STEPS.docker : UPD_STAGE_STEPS.bare);
    const stepStage = steps.includes(state.stage) ? state.stage
      : ['preparing', 'prepared', 'switching'].includes(state.stage) && steps.includes('executing')
        ? 'executing' : null;
    const restoreRef = state.from_image_ref || '';
    const signature = [state.stage, state.active, tone, label, confirmed, unverified,
      state.target_version, state.version, state.error, state.notice, logs.length, logs[logs.length - 1],
      restoreRef]
      .join('|');
    if (signature === ctx.progSig) return;
    ctx.progSig = signature;
    let html = '<div id="upd-operation" class="alert ' + tone + '" data-upd-stage="' + a(state.stage || '') + '">'
      + (state.active ? '当前操作状态：' : '上次操作状态：') + e(label)
      + (state.target_version ? ' · 目标版本 ' + e(versionText(state.target_version)) : '')
      + (state.error ? '<div class="upd-operation-error">' + e(state.error) + '</div>' : '') + '</div>';
    if (state.stage === 'attention') {
      // 执行器还在运行、结果未定：给出三个出口；恢复按钮依赖旧镜像引用。
      html += '<div>'
        + (restoreRef ? '<button class="sm" id="upd-attention-restore">恢复到升级前版本</button>' : '')
        + '<button class="sm" id="upd-attention-wait">继续等待</button>'
        + '<button class="sm" id="upd-attention-resolve">我已手动处理</button></div>';
    } else if (state.stage === 'recovery_required') {
      // 执行器已退出且自动恢复失败：只能恢复旧版本，或确认人工已处理。
      html += '<div>'
        + (restoreRef ? '<button class="sm" id="upd-recovery-restore">恢复到升级前版本</button>' : '')
        + '<button class="sm" id="upd-recovery-resolve">我已手动处理</button></div>';
    } else if (state.stage === 'rolled_back') {
      // 自动恢复已完成：说明旧容器仍在服务，无需用户操作。
      html += '<div class="alert warning">升级失败，已自动恢复到升级前版本；旧容器继续提供服务</div>';
    }
    if (state.active && stepStage) {
      const index = steps.indexOf(stepStage);
      html += '<div class="upd-steps" id="upd-steps">' + steps.map((stage, i) => {
        const tone2 = i < index ? 'done' : i === index ? 'current' : 'todo';
        return '<span class="upd-step ' + tone2 + '"><span class="upd-step-dot"></span>'
          + e(UPD_STAGE_LABELS[stage] || stage) + '</span>';
      }).join('<span class="upd-step-line"></span>') + '</div>';
    }
    if (state.notice) {
      html += '<div class="alert warning">' + e(state.notice) + '</div>';
    } else if (unverified) {
      html += '<div class="alert warning">缺少目标版本及操作的完整确认，不能判定成功或自动刷新；可重新读取状态或检查更新。</div>';
    }
    // 页面已是新前端时刷新提示没有意义，只在「确认成功且页面仍是旧界面」时给出；
    // 任一指纹缺失（旧后端 / 读取失败）都按旧行为提示刷新，宁可多刷不可漏刷。
    const pageIsStale = !state.frontend_fingerprint || !pageViewsFingerprint
      || pageViewsFingerprint !== state.frontend_fingerprint;
    if (confirmed && pageIsStale) {
      html += '<div class="alert success" id="upd-done">版本已更新为 <strong class="mono">'
        + e(versionText(state.version || state.target_version)) + '</strong>，请刷新页面以加载新界面。'
        + '<div><button class="primary sm" id="upd-reload">刷新页面</button></div></div>';
    }
    if (failed && !stageTone && state.operation_id && state.operation === 'update') {
      html += '<div><button class="sm" id="upd-failed-rollback">回退到升级前版本</button></div>';
    }
    if (state.active || logs.length) {
      html += '<div class="upd-log-wrap"><div class="upd-log-head">升级过程日志'
        + '<span class="dim"> · ' + logs.length + ' 条</span></div>'
        + '<div id="upd-log" class="log-view" tabindex="0" aria-label="更新操作日志">'
        + (logs.length ? logs.map(e).join('\n') : '暂无日志') + '</div></div>';
    }
    box.innerHTML = html;
    const log = box.querySelector('#upd-log');
    if (log) {
      log.scrollTop = view.follow ? Math.max(0, log.scrollHeight - log.clientHeight) : view.top;
      log.scrollLeft = view.left;
      log.addEventListener('scroll', () => {
        if (!updateAlive(ctx) || ctx.panel.querySelector('#upd-log') !== log) return;
        view.top = log.scrollTop;
        view.left = log.scrollLeft;
        view.follow = log.scrollHeight - log.clientHeight - log.scrollTop <= 32;
      });
    }
    box.querySelector('#upd-reload')?.addEventListener('click', () => {
      AD.stopUpdatePanel();
      location.reload();
    });
    box.querySelector('#upd-failed-rollback')?.addEventListener('click', () => {
      // 升级失败后的快捷回退：优先按镜像引用回退（Docker；新状态为 from_image_ref，
      // 旧状态为 from_image），否则回退最近备份。
      const ref = ctx.state && (ctx.state.from_image_ref || ctx.state.from_image);
      if (ref) startUpdateOperation(ctx, 'rollback', ref.split(':').pop() || ref, { target_image: ref });
      else startUpdateOperation(ctx, 'rollback', '');
    });
    const restoreUpgrade = () => {
      // 协同阶段的恢复入口：按升级前镜像引用回退（Docker 委托执行的固定路径）。
      const ref = ctx.state && ctx.state.from_image_ref;
      if (ref) startUpdateOperation(ctx, 'rollback', ref.split(':').pop() || ref, { target_image: ref });
      else startUpdateOperation(ctx, 'rollback', '');
    };
    box.querySelector('#upd-attention-restore')?.addEventListener('click', restoreUpgrade);
    box.querySelector('#upd-recovery-restore')?.addEventListener('click', restoreUpgrade);
    const attentionAction = (action) => async () => {
      // wait=执行器仍在运行，继续等待；resolve=用户已手动处理（转 failed 并解除禁入）。
      // 轮询循环在 attention 阶段不会停（active=true 持续 setTimeout），这里只就地重绘。
      try {
        const result = await updateRequest(ctx, '/api/system/self-update/attention', 'POST', { action }, 30000);
        if (!updateAlive(ctx)) return;
        ctx.state = { ...ctx.state, ...result.state };
        drawUpdateRow(ctx);
        drawUpdateProgress(ctx);
      } catch (err) {
        updateError(ctx, err.status ? err.message : err.message + '；请求可能已送达，可重新读取状态确认');
      }
    };
    box.querySelector('#upd-attention-wait')?.addEventListener('click', attentionAction('wait'));
    box.querySelector('#upd-attention-resolve')?.addEventListener('click', attentionAction('resolve'));
    box.querySelector('#upd-recovery-resolve')?.addEventListener('click', attentionAction('resolve'));
  }

  async function pollUpdateStatus(ctx) {
    if (!updateAlive(ctx)) return;
    if (++ctx.polls > 1200 || Date.now() > ctx.deadline) {
      updateError(ctx, '等待确认超时，未确认更新成功；请检查服务或重新读取状态'); return;
    }
    try {
      const state = await updateRequest(ctx, '/api/system/self-update/status');
      if (!updateAlive(ctx)) return;
      if (ctx.operationId && ctx.operationId !== state.operation_id) {
        updateError(ctx, '操作标识已变化，停止自动刷新，请重新读取状态'); return;
      }
      if (state.run_mode) ctx.runMode = state.run_mode;
      ctx.state = state;
      drawUpdateRow(ctx);
      drawUpdateProgress(ctx);
      if (updateConfirmed(state)) {
        // 已确认：不再轮询状态（操作已结束），但保持会话存活，让重查能重绘
        // 行与进度区；刷新入口由 drawUpdateProgress 在原位给出。不自动整页
        // reload，那会打断用户正在看的日志或页面上的其它操作。
        // 且必须重查一次：上一次检查是拿重启前的版本比出来的，直接沿用会显示
        // 「可升级到刚装上的那个版本」，点下去只会被后端拒绝。
        runUpdateCheck(ctx, true);
        return;
      }
      if (!state.active) {
        // 非活跃状态：静默检查一次（进页面即知有无新版本；有缓存时后端直接返回缓存）。
        runUpdateCheck(ctx, false);
        return;
      }
    } catch (err) {
      if (!updateAlive(ctx)) return;
      // 升级中的重启窗口（容器重建/服务重启）期间状态接口必然短暂失联，
      // 这不是失败：继续轮询直到恢复，只有 HTTP 层的明确拒绝才终止。
      if (err.status && err.status !== 502 && err.status !== 503) { updateError(ctx, err.message || '状态查询失败'); return; }
      if (++ctx.failures >= 60) { updateError(ctx, '重连预算耗尽，请重新读取状态'); return; }
      const progress = ctx.panel.querySelector('#upd-progress');
      if (!ctx.state && progress && !progress.innerHTML) progress.textContent = '暂时失联，正在自动重连…';
      if (ctx.state && ctx.state.active && progress && !progress.querySelector('#upd-reconnect')) {
        // 复用进度区原有的追加式渲染：直接 innerHTML 重建会打断日志阅读位置，
        // 这里在进度区尾部插入一条轻提示，恢复后由下一次成功轮询的重绘移除。
        progress.insertAdjacentHTML('beforeend',
          '<div class="alert info" id="upd-reconnect">服务正在重启，等待恢复…（若长时间未恢复请检查容器或服务状态）</div>');
      }
    }
    if (updateAlive(ctx)) ctx.timer = setTimeout(() => pollUpdateStatus(ctx), 1500);
  }

  async function runUpdateCheck(ctx, force) {
    if (ctx.checking || !updateAlive(ctx)) return;
    ctx.checking = true;
    drawUpdateRow(ctx);
    try {
      // 后端一次检查最坏是 Releases API（15s）之后再回退 git ls-remote（30s），
      // 所以这里必须给足预算；沿用 10s 会让界面在后端还没返回时就报「检查失败」。
      ctx.check = await updateRequest(
        ctx, '/api/system/update/check' + (force ? '?force=true' : ''), 'GET', undefined, 60000);
    } catch (err) {
      if (!updateAlive(ctx)) return;
      const current = (ctx.state && ctx.state.current_version) || '';
      ctx.check = { error: err.message || '检查失败', current, latest: '', update_available: false };
    } finally {
      ctx.checking = false;
      if (updateAlive(ctx)) drawUpdateRow(ctx);
    }
  }

  // -------------------------------------------------------- 升级 / 回滚
  /** 返回是否已被后端受理；调用方可据此决定要不要关闭当前弹窗。 */
  async function startUpdateOperation(ctx, operation, target, extraPayload) {
    if (ctx.submitting) return false;
    ctx.submitting = true;
    try {
      const settings = (ctx.settings && ctx.settings.settings) || {};
      const repo = settings.update_repo || '默认更新源（github.com/j9kkk/auto-deploy）';
      const proxy = (ctx.settings && ctx.settings.proxy_description) || '未启用';
      const isImage = extraPayload && extraPayload.target_image;
      let message;
      let detail;
      if (operation === 'update' && ctx.runMode === 'docker') {
        message = '将拉取新镜像 ' + e(versionText(target)) + ' 并重建容器（约 10 秒，期间面板短暂不可用）。';
        detail = '任务数据、配置与历史记录均保留（数据在独立卷中）；切换前会备份 .env 并记录旧镜像，'
          + '启动异常时自动恢复旧版本；恢复失败时界面会给出人工处理指引。'
          + '更新源：' + e(repo) + '；网络代理：' + e(proxy) + '。';
      } else if (isImage) {
        message = '将把镜像切回 ' + e(target) + ' 并重建容器（期间面板短暂不可用）。';
        detail = '数据卷不受影响，仅程序版本回退；回退后请确认任务运行正常。';
      } else if (operation === 'rollback') {
        message = target ? '将回滚到 ' + e(versionText(target)) + ' 并重启服务。'
          : '将回滚到最近一份可用备份并重启服务。';
        detail = '备份仅含代码，不回滚数据库或 Python 依赖，降级可能不兼容；'
          + '只有重启后确认目标版本运行才算成功。';
      } else {
        message = '将替换程序代码并重启服务；只有重启后确认目标版本运行才算成功。';
        detail = '更新源：' + e(repo) + '；网络代理：' + e(proxy)
          + '；备份仅含代码，不回滚数据库或 Python 依赖，降级可能不兼容。';
      }
      const confirmed = await AD.confirm({
        title: operation === 'update' ? '一键升级到 ' + versionText(target) : '版本回退',
        message,
        detail,
        confirmText: operation === 'update' ? '开始升级' : '确认回退',
        danger: operation === 'rollback',
      });
      if (!confirmed || !updateAlive(ctx)) return false;
      const path = '/api/system/self-update' + (operation === 'rollback' ? '/rollback' : '');
      const payload = { target_version: target, ...(extraPayload || {}) };
      const result = await updateRequest(ctx, path, 'POST', payload, 30000);
      if (!updateAlive(ctx)) return false;
      ctx.operationId = result.state.operation_id;
      // 执行器升级含拉取、切换与验证，预算从 30 分钟放宽到 45 分钟。
      ctx.deadline = Date.now() + 45 * 60 * 1000;
      ctx.polls = 0;
      ctx.failures = 0;
      ctx.state = { ...(ctx.state || {}), ...result.state, active: true };
      drawUpdateRow(ctx);
      drawUpdateProgress(ctx);
      pollUpdateStatus(ctx);
      return true;
    } catch (err) {
      // HTTP 状态码是确定的拒绝（后端已校验并返回原因），不必提示「可能已送达」；
      // 只有请求本身没拿到响应（网络中断/超时）才需要提醒用户核对状态。
      updateError(ctx, err.status
        ? err.message
        : err.message + '；请求可能已送达，可重新读取状态确认');
      return false;
    } finally { ctx.submitting = false; }
  }

  // ----------------------------------------------------------- 配置弹窗
  function openUpdateConfig(ctx) {
    const settings = (ctx.settings && ctx.settings.settings) || {};
    const proxy = (ctx.settings && ctx.settings.proxy_description) || '未启用';
    AD.Modal.open({
      title: '更新源配置',
      size: 'narrow',
      body: '<div class="field"><label for="upd-repo">更新源（只使用可信仓库）</label>'
        + '<input id="upd-repo" type="url" value="' + a(settings.update_repo || '') + '" '
        + 'placeholder="https://github.com/组织/仓库">'
        + '<div class="hint">GitHub 仓库优先查 Releases，失败后回退到 git ls-remote；'
        + '也可填内网镜像或本地路径。保存后会立即强制检查一次。</div></div>'
        + '<dl class="kv"><dt>网络代理</dt><dd>' + e(proxy) + '</dd></dl>'
        + '<div id="upd-config-result"></div>',
      footer: '<button data-close>关闭</button><button class="primary" id="upd-config-save">保存并检查</button>',
      onMount(backdrop) {
        backdrop.querySelector('#upd-config-save').addEventListener('click', async (event) => {
          const result = backdrop.querySelector('#upd-config-result');
          const repo = backdrop.querySelector('#upd-repo').value.trim();
          AD.setBusy(event.currentTarget, true, '保存中…');
          try {
            const saved = await updateRequest(ctx, '/api/settings', 'PUT', { update_repo: repo }, 20000);
            if (!updateAlive(ctx)) return;
            ctx.settings = { ...(ctx.settings || {}), ...saved };
            ctx.check = await updateRequest(
              ctx, '/api/system/update/check?force=true', 'GET', undefined, 60000);
            if (!updateAlive(ctx)) return;
            drawUpdateRow(ctx);
            result.innerHTML = ctx.check.error
              ? '<div class="alert error">本次检查失败：' + e(ctx.check.error) + '</div>'
              : '<div class="alert success">更新源已保存：最新版本 ' + e(versionText(ctx.check.latest))
                + (ctx.check.update_available ? '，可升级' : '（当前已是最新）') + '</div>';
          } catch (err) {
            if (updateAlive(ctx)) result.innerHTML = '<div class="alert error">保存失败：' + e(err.message) + '</div>';
          } finally { AD.setBusy(event.currentTarget, false); }
        });
      },
    });
  }

  // ------------------------------------------------------- 升级日志弹窗
  function stamp(seconds) {
    const value = Number(seconds);
    if (!value) return '时间未知';
    const two = (n) => String(n).padStart(2, '0');
    // epoch 秒按服务器时区渲染，与 AD.formatTime 保持一致。
    const date = AD.tzOffsetMinutes === null
      ? new Date(value * 1000)
      : new Date(value * 1000 + (AD.tzOffsetMinutes - new Date(value * 1000).getTimezoneOffset()) * 60000);
    return date.getFullYear() + '-' + two(date.getMonth() + 1) + '-' + two(date.getDate())
      + ' ' + two(date.getHours()) + ':' + two(date.getMinutes());
  }

  function restoreActionHtml(entry, current, backups) {
    // 「回滚到这个版本」= 回到本次操作替换掉的那个版本（它的备份版本）。
    const restore = String(entry.backup_version || '');
    if (!restore) return '<span class="faint">无备份版本</span>';
    if (bareVersion(restore) === bareVersion(current)) return '<span class="faint">已是当前版本</span>';
    const known = backups.some((item) => bareVersion(item.version) === bareVersion(restore));
    if (known) return '<button class="sm" data-upd-restore="' + a(restore) + '">回滚到 '
      + e(versionText(restore)) + '</button>';
    return '<button class="sm" disabled title="没有该版本通过校验的代码备份">回滚到 '
      + e(versionText(restore)) + '</button>';
  }

  function historyItemHtml(entry, current, backups) {
    const operation = entry.operation === 'rollback' ? '回滚' : '更新';
    const done = entry.stage === 'done';
    const tone = done ? 'success' : entry.error ? 'failed' : 'neutral';
    const label = done ? '已确认完成' : (UPD_STAGE_LABELS[entry.stage] || entry.stage || '未知');
    const logs = entry.log || [];
    return '<div class="upd-history-item">'
      + '<div class="upd-history-head">'
      + '<span class="badge ' + tone + '">' + e(label) + '</span>'
      + '<span>' + operation + '到 <strong class="mono">'
      + e(versionText(entry.target_version || entry.current_version)) + '</strong></span>'
      + '<span class="dim">原版本 ' + e(versionText(entry.previous_version) || '未知')
      + ' · ' + e(stamp(entry.finished_at || entry.started_at)) + '</span>'
      + '<div class="spacer"></div>'
      + '<span class="upd-history-actions" data-upd-actions>'
      + restoreActionHtml(entry, current, backups) + '</span>'
      + '</div>'
      + (entry.error ? '<div class="alert error">' + e(entry.error) + '</div>' : '')
      + '<div class="log-view upd-history-log">'
      + (logs.length ? logs.map(e).join('\n') : '暂无日志') + '</div>'
      + '</div>';
  }

  async function openUpdateHistory(ctx) {
    let data;
    try { data = await updateRequest(ctx, '/api/system/self-update/history', 'GET', undefined, 20000); }
    catch (err) { if (updateAlive(ctx)) updateError(ctx, '读取升级日志失败：' + err.message); return; }
    const entries = data.entries || [];
    const backups = data.backups || [];
    const current = data.current_version || '';
    AD.Modal.open({
      title: '升级日志',
      size: 'wide',
      body: (entries.length
          ? entries.map((entry) => historyItemHtml(entry, current, backups)).join('')
          : '<div class="empty">还没有升级记录</div>')
        + '<div class="upd-history-note">可回滚的版本：'
        + (backups.length
          ? e(backups.map((item) => versionText(item.version)).join('、')) + '（仅代码，数据库与依赖不回滚）'
          : '暂无通过校验的代码备份，无法自动回滚') + '</div>',
      onMount(backdrop) {
        const bindRestore = (slot, version) => {
          slot.innerHTML = '<button class="sm" data-upd-restore="' + a(version) + '">回滚到 '
            + e(versionText(version)) + '</button>';
          slot.querySelector('[data-upd-restore]').addEventListener('click', () => promptRestore(slot, version));
        };
        const promptRestore = (slot, version) => {
          slot.innerHTML = '<span class="dim">确认回滚到 ' + e(versionText(version)) + '？</span>'
            + '<button class="danger sm" data-upd-confirm>确认回滚</button>'
            + '<button class="ghost sm" data-upd-cancel>取消</button>';
          slot.querySelector('[data-upd-cancel]').addEventListener('click', () => bindRestore(slot, version));
          slot.querySelector('[data-upd-confirm]').addEventListener('click', async (event) => {
            AD.setBusy(event.currentTarget, true, '回滚中…');
            // 只有后端受理了才关闭弹窗：被拒绝时保留弹窗，错误提示才不会被遮住。
            const accepted = await startUpdateOperation(ctx, 'rollback', version);
            if (accepted && updateAlive(ctx)) AD.Modal.close();
            else { AD.setBusy(event.currentTarget, false); bindRestore(slot, version); }
          });
        };
        backdrop.querySelectorAll('[data-upd-restore]').forEach((button) => {
          const version = button.dataset.updRestore;
          button.addEventListener('click', () => promptRestore(button.closest('[data-upd-actions]'), version));
        });
      },
    });
  }

})(window.AD);
