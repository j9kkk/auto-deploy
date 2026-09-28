# AutoDeploy

[![CI](https://github.com/j9kkk/git-deploy/actions/workflows/ci.yml/badge.svg)](https://github.com/j9kkk/git-deploy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

从 GitHub 自动拉取公开代码库、打包并部署到 Linux 服务器的常驻服务。提供 Web 控制台，
支持多任务、定时调度、多种部署方式和完整的运行统计。

---

## 功能

**任务配置**
- 仓库地址（https / ssh / git / 本地路径）、分支、子目录、浅克隆深度
- 私有仓库支持访问令牌（通过 `GIT_ASKPASS` 传递，不写入命令行、`.git/config` 或日志）
- 拉取频率：固定间隔（`30s` / `15m` / `6h` / `2d`）或标准 5 段 cron 表达式
- 可随时暂停/启用调度，也可点击「运行」立即部署

**部署方式**（每种方式都会自动打包一份 `.tar.gz` 产物供下载）

| 方式 | 说明 |
|------|------|
| 仅打包 | 只构建并打包，不做发布 |
| 自定义脚本 | 完全由部署脚本控制发布流程 |
| 发布目录 + 软链 | 打包到 `releases/<时间戳>-<commit>`，原子切换 `current` 软链 |
| 发布 + systemd | 切换软链后重启服务，并校验 `is-active` 状态 |
| Docker | 构建镜像，可选执行容器更新命令 |
| Docker Compose | 执行 `docker compose up -d --build` |
| rsync | 把发布目录同步到远端或本地目标 |

**Web 控制台**
- 总览：成功率、进行中任务、14 天趋势图、最近与即将执行的任务
- 任务管理：增删改查、启停、立即运行、回滚上一版本、下载产物
- 运行记录：按状态/任务/关键字筛选，**实时日志**（增量轮询，自动滚动）
- 统计：每日运行曲线、任务排行、磁盘占用
- 设置：调度并发、超时、数据保留策略、通知 Webhook、账号密码、操作日志

**安全**
- 管理员账号密码登录，PBKDF2-HMAC-SHA256（24 万次迭代）加盐哈希
- 会话 Cookie `HttpOnly` + `SameSite=Lax`，令牌哈希后落库；修改密码后其他会话立即失效
- 登录失败次数限制与临时锁定
- 全站 CSP（无任何外部 CDN 依赖，内网/离线环境可用）
- 操作审计日志，可导出 CSV

---

## 快速开始

```bash
git clone https://github.com/j9kkk/git-deploy.git
cd git-deploy
./run.sh
```

首次运行会自动创建虚拟环境、安装依赖，并生成管理员密码打印到终端：

```
==================================================================
  首次启动：已创建管理员账号
  用户名: admin
  初始密码: xxxxxxxxxxxxxxxxxxxx
  （此密码仅显示这一次，请立即登录并修改）
==================================================================
  访问地址: http://127.0.0.1:8770/
```

浏览器会自动打开控制台。也可以用参数指定密码，避免打印到日志：

```bash
./run.sh --initial-password 'YourStr0ngPass!'
```

### 常用参数

```bash
./run.sh --port 9000              # 换端口
./run.sh --host 0.0.0.0           # 允许外部访问
./run.sh --data-dir /var/lib/autodeploy   # 指定数据目录
./run.sh --no-browser             # 不自动打开浏览器
./run.sh --log-level DEBUG        # 调试日志
```

配置项也可用环境变量覆盖（优先级高于 Web 设置）：`AUTODEPLOY_PORT`、
`AUTODEPLOY_HOST`、`AUTODEPLOY_DATA_DIR`、`AUTODEPLOY_MAX_GLOBAL_WORKERS` 等，
命名规则为 `AUTODEPLOY_` + 设置名大写。

---

## 安装为 systemd 服务

```bash
sudo ./scripts/install.sh
```

脚本会完成：安装 git、创建 `autodeploy` 系统用户、复制程序到 `/opt/autodeploy`、
创建数据目录 `/var/lib/autodeploy`、建立虚拟环境、写入并启动 systemd 单元，
同时为 systemd 部署方式配置最小化的 sudo 权限。

查看初始密码与日志：

```bash
journalctl -u autodeploy -n 80 --no-pager | grep -A3 首次启动
journalctl -u autodeploy -f
```

可用环境变量调整安装位置：`AUTODEPLOY_INSTALL_DIR`、`AUTODEPLOY_DATA_DIR`、
`AUTODEPLOY_USER`、`AUTODEPLOY_PORT`、`AUTODEPLOY_HOST`。

### Docker 部署方式

安装脚本创建的 `autodeploy` 账号默认没有 Docker 权限，任务使用 Docker /
Docker Compose 部署方式前需要加组：

```bash
sudo usermod -aG docker autodeploy
sudo systemctl restart autodeploy    # 组变更需重启服务进程才生效
```

注意：docker 组权限等价于 root，请勿将控制台直接暴露到公网。

### 卸载

```bash
sudo ./scripts/uninstall.sh --dry-run   # 预览将执行的操作，不做任何改动
sudo ./scripts/uninstall.sh             # 停止并删除服务，保留数据目录与账号
sudo ./scripts/uninstall.sh --purge     # 连数据目录与账号一起删除
```

默认卸载会保留 `/var/lib/autodeploy`（任务配置、运行历史、打包产物），
确认不再需要时再用 `--purge`。无论哪种方式，**任务发布到「目标目录」的
站点（如 `/var/www/blog`）不会被删除**，卸载开始时会把这些目录列出来。
安装时用过非默认路径的，用同样的环境变量指定，例如
`sudo AUTODEPLOY_DATA_DIR=/data/autodeploy ./scripts/uninstall.sh --purge`。

### 反向代理

如需公网访问，请在前面加 Nginx 并启用 HTTPS，参考 [deploy/nginx.conf.example](deploy/nginx.conf.example)。
配置完成后在控制台「设置 → 安全」中勾选：

- **信任反向代理的 X-Forwarded-For** — 审计日志记录真实 IP
- **仅通过 HTTPS 发送会话 Cookie** — 仅在已启用 HTTPS 时勾选，否则将无法登录

> **重要**：部署任务可以执行任意脚本，等价于服务器上的服务账号权限。
> 请勿在未加访问控制的情况下直接暴露到公网。

---

## 工作原理

每次运行的流水线：

```
环境检查 → 拉取代码 → 变更检测 → 准备脚本(构建) → 暂存发布目录 → 打包 → 部署 → 清理旧版本
```

几个关键设计：

- **变更检测**：拉取后比对提交哈希，无变化时可自动跳过（默认开启），避免无意义的重复部署。
- **原子发布**：先在同级目录创建临时软链，再用 `os.replace` 原子替换 `current`，
  读者永远看到完整的旧版本或新版本，不会出现悬空链接。任何步骤失败都不会影响正在服务的版本。
- **工作副本复用**：`data/workspaces/task-<id>` 保留在本地，只拉取增量；
  每次会 `reset --hard` + `clean -fdx`，保证构建起点干净。
- **脚本环境变量**：所有脚本都会收到 `AUTODEPLOY_RELEASE_DIR`、`AUTODEPLOY_COMMIT`、
  `AUTODEPLOY_SOURCE_DIR`、`AUTODEPLOY_CURRENT_LINK` 等变量，便于编写通用脚本。
- **进程组终止**：命令以独立会话启动，取消或超时会终止整个进程组，不留孤儿进程。
- **日志脱敏**：URL 中的凭证、GitHub Token、`Authorization` 头在写入日志前统一脱敏。

### 目录结构

```
git-deploy/
├── app/
│   ├── main.py         # FastAPI 应用与命令行入口
│   ├── config.py       # 配置加载与目录布局
│   ├── db.py           # SQLite 连接与建表
│   ├── store.py        # 仓储层（用户/会话/任务/运行/审计）
│   ├── security.py     # 密码哈希、会话令牌、登录限流
│   ├── schedule.py     # 间隔与 cron 表达式解析
│   ├── executor.py     # 子进程执行、日志流、取消、脱敏
│   ├── gitops.py       # clone/fetch/reset、变更检测、凭证传递
│   ├── deployer.py     # 暂存、打包、软链切换、各部署方式
│   ├── runner.py       # 单次运行的流水线编排
│   ├── scheduler.py    # 调度循环、并发控制、数据保留
│   ├── validation.py   # 请求校验规则
│   ├── service.py      # 组件装配与服务生命周期
│   └── api/            # 路由：auth / tasks / runs / stats / settings
├── web/                # 控制台前端（原生 JS，无构建步骤）
├── tests/selfcheck.py  # 自检脚本，246 项检查
├── scripts/install.sh  # systemd 安装脚本
├── deploy/             # 反向代理示例
├── .github/            # CI 工作流、Issue 与 PR 模板
├── run.sh              # 启动脚本
└── data/               # 运行时数据（数据库、工作副本、发布、日志）
```

### 数据目录

```
data/
├── autodeploy.db       # SQLite（用户、任务、运行记录、审计）
├── config.json         # Web 界面修改的设置
├── workspaces/task-N/  # 每个任务的 git 工作副本
├── releases/task-N/    # 每个任务的发布版本与 current 软链
├── artifacts/task-N/   # 打包产物 .tar.gz
├── logs/run-N.log      # 每次运行的完整日志
└── tmp/                # 临时凭证助手等
```

设置了「目标目录」的任务，其发布目录位于 `<目标目录>/releases`，
`<目标目录>/current` 可被 Nginx 或 systemd 直接引用。

---

## 部署脚本示例

**前端静态站点 + Nginx**

```bash
# 准备脚本
npm ci
npm run build

# 部署脚本
nginx -s reload
```

打包路径填 `dist`，目标目录填 `/var/www/blog`，
之后 Nginx 指向 `/var/www/blog/current` 即可。

**Python 服务 + systemd**

准备脚本：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

部署方式选「发布 + systemd」，服务名填 `myapp`，
`systemctl restart` 权限已由安装脚本授权。

**Docker Compose**

打包路径留空（需要完整的 `docker-compose.yml` 与 Dockerfile），
部署方式选「Docker Compose」，Compose 文件填 `docker-compose.yml`。

---

## 自检

```bash
.venv/bin/python tests/selfcheck.py
```

覆盖调度表达式解析、密码与会话安全、路径校验、发布与软链原子性、
本地 Git 克隆与变更检测、命令执行与取消、数据库仓储层、
完整 API 端到端流程（含真实部署、取消与跳过逻辑）、日志增量协议、调度器行为。
共 246 项检查，全部通过时退出码为 0。该脚本不依赖 pytest，可在裸服务器上直接运行。

---

## 参与贡献

欢迎提交 Issue 与 Pull Request，请先阅读 [贡献指南](CONTRIBUTING.md)。
变更记录见 [CHANGELOG.md](CHANGELOG.md)。

安全漏洞请勿在公开 Issue 中披露，改走
[私密报告渠道](https://github.com/j9kkk/git-deploy/security/advisories/new)。

---

## 故障排查

**服务无法启动**

```bash
journalctl -u autodeploy -n 100 --no-pager
./run.sh --log-level DEBUG          # 前台运行看完整输出
```

**部署失败**

在控制台运行记录中点击「日志」查看完整输出，常见原因：

| 现象 | 原因 |
|------|------|
| `git 操作失败：远程分支不存在` | 分支名填错，或仓库默认分支是 `master` |
| `命令不存在: docker` | 服务器未安装对应命令，可在「设置 → 系统信息」查看依赖 |
| `systemctl restart 失败` | 服务名不对，或缺少 sudo 权限（见安装脚本的 sudoers 配置） |
| `打包路径未匹配到任何文件` | 打包路径相对的是「仓库子目录」，注意构建产物是否已生成 |
| 任务一直被跳过 | 代码确实无变化，或「跳过无变化」需要关闭 |

**忘记密码**

```bash
# 删除用户表内容后重启，会重新生成管理员密码
sqlite3 data/autodeploy.db "DELETE FROM users;"
./run.sh --initial-password 'NewStr0ngPass!'
```

**磁盘占用过大**

在「统计 → 存储占用」点击「立即清理」，或调整「设置 → 数据保留」中的
保留天数与版本数。清理永远不会删除当前正在使用的发布版本。

---

## 技术说明

- Python 3.10+，FastAPI + Uvicorn，SQLite（WAL 模式）
- 前端为原生 JavaScript，无构建步骤、无外部请求，离线可用
- 除 Web 框架外全部使用标准库，便于在受限服务器上部署
- 许可证：MIT
