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
- 私有仓库凭据集中管理：支持 HTTPS 访问令牌与 SSH 私钥，多个任务可复用同一份
  凭据，可在界面上直接测试是否可用；密钥永不回传浏览器，不写入命令行、`.git/config` 或日志
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
- 任务管理：增删改查、启停、立即运行、回滚上一版本、下载产物；删除前二次确认
  并停止容器、清理部署文件夹与历史存档
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

## 凭据与代理

这两项是内网或私有仓库环境的常见前提，都做成「配置一次、多处复用」。

### Git 凭据

「凭据」页面集中管理仓库访问凭证，任务在表单里通过下拉框引用。

| 类型 | 适用地址 | 说明 |
|------|----------|------|
| HTTPS 访问令牌 | `https://host/owner/repo.git` | GitHub 建议用细粒度只读令牌；用户名通常填 `x-access-token` |
| SSH 私钥 | `git@host:owner/repo.git` | 需把公钥登记到仓库的 Deploy Keys；**可在页面上直接生成密钥对**，支持带口令的私钥 |

**获取指引内置在系统里**：凭据页面右上角「怎么获取？」列出了两种方式各自的操作步骤、
要打开的 GitHub 页面和所需权限，不必再翻文档。指引文案由后端
`GET /api/credentials/guide` 提供，GitHub 改版时只需改一处。

**自动生成密钥对（推荐用于 SSH）**：新建 SSH 凭据时点「自动生成密钥对」，服务端调用
`ssh-keygen` 生成密钥，私钥填入表单、公钥立即显示并提供一键复制与配置步骤——
省去手工生成、找文件、分清公私钥的过程。已保存的凭据可随时点「公钥」按钮取回公钥
（如服务器重装后重新添加 Deploy Key）。

**为什么集中管理**：几十个任务共用同一个令牌时，轮换只需改一处，而不必逐个任务修改。

**可用性测试**：每份凭据可点「测试」并填入一个真实仓库地址，服务会执行
`git ls-remote` 验证认证是否通过，结果与时间会记录在列表里。令牌过期是导致
部署突然失败的常见原因，提前发现比事后翻日志划算。

**安全设计**：

- 密钥只在保存时提交，之后**永不回传**浏览器（接口只返回「是否已设置」与长度）
- 私钥以 `0600` 写入临时目录，只在该次运行期间存在，不内联进 `GIT_SSH_COMMAND`
- 自动生成时私钥仅落临时目录并在读取后立即删除，不会留在磁盘上
- 公钥与指纹（`SHA256:...`）**不属于机密**，可回传界面用于核对与重新配置；
  指纹与 `ssh-keygen -lf` / GitHub 显示的值一致，可直接比对

> 任务表单里仍保留「任务内访问令牌」字段以兼容既有配置；**引用了全局凭据时以凭据为准**。

### 网络代理

「设置 → 网络代理」中配置，作用于 git 操作，可选同时作用于构建脚本。

| 配置项 | 说明 |
|--------|------|
| 代理地址 | 支持 `http://`、`https://`、`socks5://`，如 `http://proxy.corp.local:8080` |
| 不使用代理的地址 | `no_proxy` 列表，逗号分隔。**内网仓库必须列在这里**，否则会绕经代理而失败 |
| 代理用户名 / 密码 | 可空；密码不回传浏览器，日志中脱敏为 `***` |
| 构建脚本也使用该代理 | 勾选后 `prepare`/`deploy` 脚本里的 `npm install`、`pip install` 等也能联网 |

**实现要点**：代理以**环境变量**下发（`http_proxy`/`https_proxy`/`no_proxy`），
而不是 `git -c http.proxy=...`——后者会把代理密码暴露在进程命令行里，任何能执行
`ps` 的用户都能看到。

**连通性检查会自动适配**：配置代理后，服务探测的是代理本身而非目标主机；
`no_proxy` 命中的主机（如内网仓库）则直连探测。探测同时读取设置页与进程环境里
的代理，与 git 使用同一份配置。**只有确定的失败**（域名不存在、连接被拒绝）
才会在预检阶段中止；`EAI_AGAIN` 这类临时解析失败会放行给 git 重试，
避免一次 DNS 抖动被误报为不可达。

## 安装为 systemd 服务

**方式一：一键安装（推荐）**——不需要先克隆仓库，在目标服务器上执行一条命令：

```bash
curl -fsSL https://raw.githubusercontent.com/j9kkk/git-deploy/main/scripts/bootstrap.sh | sudo bash
```

脚本自动完成：下载最新发布版本 → 检查 git / Python 3.10+ → 创建系统用户与
sudo 授权 → 复制程序到 `/opt/autodeploy` → 建立虚拟环境 → 写入并启动 systemd 单元。

- **升级**：重复执行同一条命令即升级到最新发布版。数据目录不受影响
  （任务配置、运行历史与已发布的站点都保留）；
- **已有安装的自定义配置自动保留**：安装时用过 `AUTODEPLOY_*` 环境变量或
  自定义服务名的，重跑时会从现有 systemd 单元读取并沿用，不会被冲回默认值；
- 需要代理时先 `export https_proxy=…` 再执行；
- 也可指定版本或仓库（通过 `sudo env` 透传，避免被 sudo 的环境重置丢弃）：
  `curl -fsSL … | sudo env AUTODEPLOY_REF=v0.1.0 bash`，
  或 `AUTODEPLOY_REPO_URL=<fork 地址>`；
- 已克隆仓库的也可以直接 `sudo bash scripts/bootstrap.sh`，非 root 执行时会
  自动通过 sudo 提权。

**方式二：从源码目录安装**（已克隆仓库或离线机器）：

```bash
git clone https://github.com/j9kkk/git-deploy.git
cd git-deploy
sudo ./scripts/install.sh
```

安装脚本会完成：安装 git、创建 `autodeploy` 系统用户、复制程序到 `/opt/autodeploy`、
创建数据目录 `/var/lib/autodeploy`、建立虚拟环境、写入并启动 systemd 单元，
同时为 systemd 部署方式配置最小化的 sudo 权限。重复执行同样是对已有安装的
原地升级。

查看初始密码与日志：

```bash
journalctl -u autodeploy -n 80 --no-pager | grep -A3 首次启动
journalctl -u autodeploy -f
```

可用环境变量调整安装位置：`AUTODEPLOY_INSTALL_DIR`、`AUTODEPLOY_DATA_DIR`、
`AUTODEPLOY_USER`、`AUTODEPLOY_PORT`、`AUTODEPLOY_HOST`；也可用
`AUTODEPLOY_SERVICE_NAME` 更换服务名、`AUTODEPLOY_PYTHON` 指定 Python 解释器
（默认 `python3`，需 3.10+）。

### Docker 部署方式

安装脚本会自动把 `autodeploy` 账号加入 docker 组（服务启动前完成，无需手工操作），
任务可直接使用 Docker / Docker Compose 部署方式。

如果安装时机器上还没有 Docker，之后再安装的话需要补一次加组：

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

## 自我更新

AutoDeploy 提供 **「设置 → 系统信息 → 版本更新」**。该卡片只占一行：
**当前版本 · 最新版本 · 升级日志 · 配置**。打开「系统信息」时会自动检查一次更新源。

- **当前已是最新版本**时只显示这一行提示，不显示最新版本号；
- **有更新**时显示最新版本号，并出现**升级图标**。点击后二次确认，确认框会列出
  **更新源与网络代理**，确认后才开始升级；
- 升级过程在卡片内**原地展示日志**（下载 → 备份 → 替换 → 依赖 → 等待重启确认），
  页面不重绘，正在阅读的日志位置会保留；
- **「升级日志」**列出历次更新/回滚：状态、目标版本、原版本、时间与完整日志；
  每条记录都可**回滚到该次操作之前的版本**（就地二次确认）；
- **「配置」**用于修改更新源并立即强制检查。更新源相当于代码执行信任源，请只使用可信仓库。

> 旧服务器必须先由管理员完成一次升级并启动本次新代码，才能取得修正后的更新功能。
> 仅把新文件下载到磁盘并不表示服务已升级。旧前端不会因服务器仓库发布新代码自行获得这些功能。

### 重启前提与边界

更新和回滚开始前，必须确认本进程是实际 systemd unit 的主进程，并用只读
`systemctl show` 确认有效配置为 `Restart=always`、单元处于运行状态且没有阻止退出重启的配置。
`on-failure` 等策略不支持正常退出后拉起。开发环境、容器或无法验证的环境会在改程序文件前拒绝操作。
不调用 sudo、不修改 unit、不移除 `NoNewPrivileges`。更新/回滚排他，并阻止新部署开始；有活动部署时拒绝更新。

### 更新源与过程

默认源为 `https://github.com/j9kkk/git-deploy.git`，继承网络代理设置。可保存镜像地址后重新检查。
GitHub 仓库优先查 Releases API，失败后回退到 `git ls-remote --tags`；**两条路径都使用设置页配置的
代理**，因此必须经代理才能访问 GitHub 的服务器也能正常检查。

1. 确认检查到的目标标签；下载后校验标签、关键文件、版本和路径，拒绝软链及特殊文件。
2. 将 `app/ web/ requirements.txt run.sh` 备份到 `data/self-update-backups/`，保留完整代码备份。
3. 替换代码、安装依赖。替换或依赖安装失败时尝试恢复代码，并明确报告恢复结果。
4. 再次验证重启能力，正常退出，由 systemd 拉起。状态记录操作 ID、更新/回滚类型、预期版本和重启前进程标识。
5. 新进程确认版本及操作后才报告完成，卡片原地提示**「请刷新页面」**并给出刷新按钮，
   由用户决定何时刷新（不再自动整页 reload）。重启确认期限为 120 秒；
   前端总等待不超过 15 分钟，网络失败累计最多 40 次，离页取消请求和定时器。401/500 不算重启成功。
6. 失败保留日志与原因。中断或版本不符不会假报成功。检查失败不再被缓存，重试立即可见。

### 回滚与限制

**「升级日志」中每条记录都有「回滚到这个版本」**，就地二次确认后恢复该版本的代码备份，
并按同样规则确认重启。回滚目标只在**通过完成标记与结构校验的备份**中按版本号查找，
因此无法指向备份目录之外的代码。

**代码备份不包括数据库和 Python 依赖**，不能撤销数据库迁移或部分依赖安装，降级兼容性需管理员确认。
备份暂不自动清理，请按磁盘容量人工管理。进程或主机在文件替换中异常终止时会报告中断，可能需要人工恢复备份。

本次验证使用隔离临时目录、mock 重启/依赖安装和 Node vm 前端测试，并在真实浏览器中验证过卡片交互；
**尚未进行真实 Linux systemd 重启验证**。

### 手动方式（备用）

仍可用一个部署任务让服务部署它自己（把本仓库当作任务目标，要点见「自我更新」
章节）。无法使用应用内更新时，重跑一键安装命令即可升级：它重新下载最新发布版
并原地替换程序文件，数据目录不受影响。更新源不可用、或需要同步 systemd 单元 /
sudoers 变更的场景，则在源码目录重跑 `scripts/install.sh`。

### 任务配置

| 配置项 | 值 |
|--------|-----|
| 仓库地址 | `https://github.com/j9kkk/git-deploy.git`（或镜像前缀） |
| 分支 | `main`；生产环境建议填稳定 tag（如 `v0.1.0`），git 拉取 tag 名同样有效 |
| 部署方式 | 自定义脚本 |
| 打包路径 | `app`、`web`、`requirements.txt`、`run.sh`（每行一条） |
| 超时 | 建议 300 秒以上（含 `pip install`） |

> **打包路径必须填写**：自定义脚本方式下留空表示「纯脚本任务」，服务不会
> 暂存任何文件，发布目录为空，脚本里就没有可同步的 `$AUTODEPLOY_RELEASE_DIR`。

### 部署脚本模板

```bash
set -e
STAGE="$AUTODEPLOY_RELEASE_DIR"
TARGET="/opt/autodeploy"                # 安装目录，与 install.sh 一致
BAK="/var/lib/autodeploy/self-update-backups"

# 1. 备份当前版本（保留最近 3 份，升级失败可回滚）
mkdir -p "$BAK"
STAMP="$(date +%Y%m%d%H%M%S)"
cp -a "$TARGET/app" "$BAK/app-$STAMP"
cp -a "$TARGET/web" "$BAK/web-$STAMP"
ls -1dt "$BAK"/app-* 2>/dev/null | tail -n +4 | xargs -r rm -rf

# 2. 同步新版本（打包路径只含这四项；安装目录属主就是服务账号，无需 sudo）
rsync -a --delete "$STAGE/app/"  "$TARGET/app/"
rsync -a --delete "$STAGE/web/"  "$TARGET/web/"
cp -f "$STAGE/requirements.txt" "$STAGE/run.sh" "$TARGET/"

# 3. 更新依赖
"$TARGET/.venv/bin/pip" install -q -r "$TARGET/requirements.txt"

# 4. 延迟重启（最后一条命令，必须原样使用）
#    5 秒后由 systemd 在本服务 cgroup 之外执行重启，
#    让本次运行先正常落库为「成功」；直接 systemctl restart 会把
#    部署脚本连同整个服务 cgroup 一起杀掉，运行会被记为中断。
sudo /usr/bin/systemd-run --collect --on-active=5s \
     /usr/bin/systemctl restart autodeploy

echo "新版本已就位，服务即将重启"
```

### 局限与注意

- **只更新代码与依赖**：systemd 单元、sudoers 等系统配置若有变更，仍需手动
  重跑一次 `scripts/install.sh`（升级到 1.2.1 及以后时重跑一次即可，之后无感）。
- **回滚**：升级失败导致服务起不来时，从备份目录恢复并重启：

  ```bash
  sudo rm -rf /opt/autodeploy/app /opt/autodeploy/web
  sudo cp -a /var/lib/autodeploy/self-update-backups/app-<时间戳> /opt/autodeploy/app
  sudo cp -a /var/lib/autodeploy/self-update-backups/web-<时间戳> /opt/autodeploy/web
  sudo systemctl restart autodeploy
  ```

- **服务起不来时控制台也进不去**，所以备份与回滚命令要提前知晓，别等到出事再找。
- 不要把自部署任务指向未验证的分支；生产建议锁定稳定 tag，升级即「改 tag → 运行」。

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
│   ├── sshkey.py       # SSH 公钥推导、指纹计算、密钥对生成
│   ├── deployer.py     # 暂存、打包、软链切换、各部署方式
│   ├── runner.py       # 单次运行的流水线编排
│   ├── scheduler.py    # 调度循环、并发控制、数据保留
│   ├── validation.py   # 请求校验规则
│   ├── service.py      # 组件装配与服务生命周期
│   └── api/            # 路由：auth / tasks / runs / credentials / stats / settings
├── web/                # 控制台前端（原生 JS，无构建步骤）
├── tests/selfcheck.py  # 自检脚本（无 pytest 依赖，含端到端检查）
├── scripts/bootstrap.sh # 一键安装/升级（远程下载，无需克隆仓库）
├── scripts/install.sh   # systemd 安装脚本（重复执行即原地升级）
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
本地 Git 克隆与变更检测、命令执行与取消、数据库仓储层、SSH 公钥推导与指纹
（与 `ssh-keygen` 交叉校验）、凭据获取指引、
完整 API 端到端流程（含真实部署、取消与跳过逻辑）、日志增量协议、
自我更新（状态机、重启确认、备份校验、更新源代理与 `no_proxy`、失败不缓存、
按版本回滚与更新历史落盘）、前端更新卡片与任务页交互（Node vm 驱动真实前端代码）、调度器行为。
检查项数量随版本增长（可运行脚本查看），全部通过时退出码为 0。
该脚本不依赖 pytest，可在裸服务器上直接运行。

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

**凭据失效 / 部署认证失败**

到「凭据」页面点「测试」，填入该任务的仓库地址即可确认。常见原因：令牌过期
（细粒度 PAT 有有效期）、令牌缺少该仓库的读取权限、SSH 公钥未登记到 Deploy Keys。

还不确定凭据怎么来的，点凭据页面右上角**「怎么获取？」**，里面有令牌与 Deploy Key
两种方式的分步说明、要打开的 GitHub 页面和所需权限。若是 SSH 方式且没有现成密钥，
直接用「自动生成密钥对」，再点列表里的「公钥」把公钥添加到仓库的 Deploy Keys。
**SSH 地址必须写成 `git@github.com:owner/repo.git`**，用 `https://` 开头不会走 SSH。

日志里出现「私有仓库需要认证，但任务未配置可用凭据」即表示该任务没绑定凭据，
在任务表单的「凭据」下拉框里选一份即可。

**配置代理后仍无法拉取**

- 确认「设置 → 网络代理」中地址与账号密码正确，保存后重新运行任务
- 内网仓库要加入 `no_proxy`，否则请求会绕经外部代理而失败
- 若构建阶段（`npm install`）需要联网，确认勾选了「构建脚本也使用该代理」

**更新检查报「无法连接 …（Temporary failure in name resolution）」**

这是域名解析失败（`EAI_AGAIN`），按语义是**临时**故障，重试通常即可恢复，
不代表服务器真的连不上 GitHub。服务已把这类情况放行给 git 重试，不再直接判为
不可达；若仍反复出现，按服务器侧排查：`/etc/resolv.conf` 中的 DNS 是否可用、
容器或防火墙是否限制 53 端口出站，或改用可达的镜像源。连通性探测使用与 git
相同的代理设置（含 systemd 单元里的 `http_proxy`）并遵循 `no_proxy`，
因此代理配置正确时不会因为这个原因误报。

**忘记密码**

```bash
# 删除用户表内容后重启，会重新生成管理员密码
sqlite3 data/autodeploy.db "DELETE FROM users;"
./run.sh --initial-password 'NewStr0ngPass!'
```

**磁盘占用过大**

在「统计 → 存储占用」点击「立即清理」，或调整「设置 → 数据保留」中的
保留天数与版本数。清理永远不会删除当前正在使用的发布版本。

**删除任务会做什么**

任务页每行（以及编辑表单里）的删除按钮需要**两次确认**：先确认删除意图，
再输入任务名。通过后按顺序执行：

1. **停止容器**：`docker_compose` 任务按固定项目名执行 `docker compose down`，
   失败时（例如 compose 文件已被删除）改按 `com.docker.compose.project`
   标签逐个 `docker rm -f`；`docker` 任务执行该任务配置的回滚脚本作为停止
   钩子——容器名只有用户脚本才知道，服务不会猜测容器名去强删，未配置回滚
   脚本时会在提示中说明需要手动确认。仅带本项目项目标签的容器会被处理，
   手工 `docker run` 起来的容器一律不碰。
2. **删除存档**：工作目录、`releases/` 发布版本、`artifacts/` 打包产物、
   临时凭据目录，以及全部运行记录与日志文件。任务正在运行时不允许删除，
   需先取消。
3. **保留项**：`systemd` 服务不会被停止（如需停止请手动 `systemctl stop`），
   `rsync` 远端文件保持原样；**`target_dir` 目标目录本身不会被删除**，只
   清理其中的 `releases/` 与 `current` 软链，因为该目录通常还有业务自己的
   文件。目标目录为软链、根路径或配置异常时整段跳过。

容器停止使用任务**当前**配置，与回滚一致（历史运行未保存配置快照）。
`DELETE /api/tasks/{id}?purge=false` 可只删任务记录而保留文件，容器仍会停止
（记录消失后它已无人管理）。整个过程写入审计日志。

---

## 技术说明

- Python 3.10+，FastAPI + Uvicorn，SQLite（WAL 模式）
- 前端为原生 JavaScript，无构建步骤、无外部请求，离线可用
- 除 Web 框架外全部使用标准库，便于在受限服务器上部署
- 许可证：MIT
