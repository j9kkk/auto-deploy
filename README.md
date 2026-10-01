# AutoDeploy

[![CI](https://github.com/j9kkk/auto-deploy/actions/workflows/ci.yml/badge.svg)](https://github.com/j9kkk/auto-deploy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

从 GitHub 自动拉取代码、构建并部署到服务器的自托管常驻服务，自带原生 JS 的 Web 控制台。
支持多任务、定时调度与 Webhook 触发、多种部署方式和完整的运行统计。

## 功能

- **任务**：仓库地址（https / ssh / git / 本地路径）、分支、子目录、浅克隆深度；
  固定间隔（`30s`～`2d`）或标准 5 段 cron 调度，可暂停/启用、立即运行、回滚上一版本
- **Webhook 触发**：每个任务自带一个含随机令牌的触发地址，向其发送 GET/POST 即部署一次，
  可直接接入 GitHub / Gitee Webhook 或外部自动化；支持一键重置令牌，旧地址立即失效
- **凭据**：HTTPS 访问令牌与 SSH 私钥集中管理、多任务复用；可在线测试可用性、
  一键生成密钥对（含指纹与配置指引）；密钥永不回传浏览器、不写入命令行或日志
- **部署方式**（每种方式都会自动打包一份 `.tar.gz` 产物供下载）：

| 方式 | 说明 |
|------|------|
| 仅打包 | 只构建并打包，不做发布 |
| 自定义脚本 | 完全由部署脚本控制发布流程 |
| 发布目录 + 软链 | 打包到 `releases/<时间戳>-<commit>`，原子切换 `current` 软链 |
| 发布 + systemd | 切换软链后重启服务并校验状态 |
| Docker | 构建镜像，可选执行容器更新命令 |
| Docker Compose | 执行 `docker compose up -d --build` |
| rsync | 把发布目录同步到远端或本地目标 |

- **控制台**：总览大盘（成功率、14 天趋势）、任务管理、运行记录（实时日志、可取消）、
  统计（每日曲线、磁盘占用）、设置（调度并发、超时、数据保留、通知、网络代理、操作日志）
- **安全**：PBKDF2 口令哈希、登录限流与会话管理、全站 CSP（无外部资源，离线可用）、
  路径校验防越界、子进程独立进程组、日志统一脱敏、操作审计可导出 CSV

## 快速开始

一条命令完成部署。脚本自动检测系统环境并安装缺失的 Docker 组件，创建
`/opt/auto-deploy`，下载 `docker-compose.yml` 并从 GHCR 拉取官方镜像启动——
无需克隆仓库、无需本地构建：

```bash
curl -fsSL https://raw.githubusercontent.com/j9kkk/auto-deploy/main/scripts/bootstrap.sh | bash
```

或手动执行：

```bash
sudo mkdir -p /opt/auto-deploy && cd /opt/auto-deploy
sudo curl -fsSLo docker-compose.yml \
  https://raw.githubusercontent.com/j9kkk/auto-deploy/main/docker-compose.yml
docker compose pull && docker compose up -d
```

首次启动会自动创建管理员账号，初始密码只打印一次：

```bash
docker compose logs autodeploy | grep -A 3 "首次启动"
```

浏览器访问 `http://<服务器IP>:8770/` 即可使用。常用命令（在安装目录下）：

```bash
docker compose logs -f autodeploy   # 查看日志
docker compose restart autodeploy   # 重启
docker compose down                 # 停止（任务数据保留）
docker compose down -v              # 停止并删除数据卷
```

本地开发需要自行构建镜像：

```bash
git clone https://github.com/j9kkk/auto-deploy.git
cd auto-deploy
docker compose up --build -d
```

## 配置

- **端口**：执行脚本前 `export AUTODEPLOY_PORT=9000`，或写入安装目录下的 `.env`
  （`AUTODEPLOY_PORT=9000`）
- **版本锁定**：`AUTODEPLOY_VERSION=v0.2.0` 部署指定版本（compose 文件与镜像 tag
  同步锁定）；默认跟随 `latest` 镜像（由 CI 随 main 分支自动构建）
- **镜像源**：GHCR 访问不畅时可换镜像地址 `AUTODEPLOY_IMAGE=<registry>/auto-deploy`
- **离线安装**：`AUTODEPLOY_COMPOSE_FILE=/path/docker-compose.yml` 跳过在线下载
  （镜像仍需 `docker load` 预先导入）
- **其他设置**：环境变量命名规则为 `AUTODEPLOY_` + 设置名大写（如
  `AUTODEPLOY_MAX_GLOBAL_WORKERS`），优先级高于界面设置
- **部署 Docker 应用**：任务可选 Docker / Docker Compose 方式。一键脚本**默认已自动挂载**
  宿主机 Docker socket（检测 docker 组 GID 后写入 `docker-compose.override.yml`，
  升级不会覆盖主文件、也不动你手写的 override），无需手动编辑任何文件；
  如需关闭，用 `AUTODEPLOY_MOUNT_DOCKER_SOCKET=0` 重跑脚本。注意 docker 组权限
  等价于 root，请勿将控制台暴露到公网。未使用一键脚本的手动部署需自行创建：

  ```yaml
  # /opt/auto-deploy/docker-compose.override.yml
  services:
    autodeploy:
      volumes:
        - /var/run/docker.sock:/var/run/docker.sock
      group_add:
        - "999"   # 宿主机 docker 组 GID：stat -c %g /var/run/docker.sock
  ```

- **反向代理**：参考 [deploy/nginx.conf.example](deploy/nginx.conf.example)；
  启用 HTTPS 后在「设置 → 安全」勾选「信任反向代理头」与「仅 HTTPS 发送会话 Cookie」
  （后者在纯 HTTP 下开启会导致无法登录）

## 升级与回滚

重复执行一键脚本即升级（拉取最新镜像并滚动重启，数据卷不受影响），
或手动：`cd /opt/auto-deploy && docker compose pull && docker compose up -d`。
回滚：`AUTODEPLOY_VERSION=v旧版本号` 重跑脚本，或改 `.env` 中的
`AUTODEPLOY_IMAGE_TAG` 后 `docker compose up -d`。

任务数据（数据库、工作副本、发布产物、日志）保存在 Docker 数据卷
`auto-deploy_autodeploy-data` 中，备份示例：

```bash
docker run --rm -v auto-deploy_autodeploy-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/autodeploy-data.tgz -C /data .
```

## 工作原理

每次运行的流水线：

```
环境检查 → 拉取代码 → 变更检测 → 准备脚本(构建) → 暂存发布目录 → 打包 → 部署 → 清理旧版本
```

- **变更检测**：比对提交哈希，无变化自动跳过（可在任务里关闭）
- **原子发布**：软链 + `os.replace` 原子切换，任何步骤失败都不影响正在服务的版本
- **工作副本复用**：`workspaces/task-N` 只拉取增量，每次 `reset --hard` + `clean -fdx`
- **脚本环境变量**：部署脚本收到 `AUTODEPLOY_RELEASE_DIR`、`AUTODEPLOY_COMMIT` 等
  变量，便于编写通用脚本
- **进程组终止与脱敏**：取消/超时终止整个进程组；URL 凭证、Token、Authorization
  头写日志前统一脱敏

容器内 `/app/data` 下：`autodeploy.db`（SQLite，WAL）、`config.json`（界面设置）、
`workspaces/`（git 工作副本）、`releases/`（发布版本）、`artifacts/`（打包产物）、
`logs/`（运行日志）。

## 故障排查

**服务异常**：`docker compose logs -f autodeploy`；部署失败看运行记录里的「日志」。

| 现象 | 原因 |
|------|------|
| `git 操作失败：远程分支不存在` | 分支名填错，或仓库默认分支是 `master` |
| `Cannot connect to the Docker daemon` | 未挂载 docker socket，见「部署 Docker 应用」 |
| `打包路径未匹配到任何文件` | 打包路径相对的是「仓库子目录」，注意构建产物是否已生成 |
| 任务一直被跳过 | 代码确实无变化，或「跳过无变化」需要关闭 |

**凭据失效**：到「凭据」页面点「测试」并填入仓库地址即可确认；SSH 地址必须写成
`git@github.com:owner/repo.git`。不确定怎么获取令牌或 Deploy Key 时，点凭据页右上角
「怎么获取？」查看内置分步指引。

**忘记密码**：

```bash
docker compose exec autodeploy python -c "import sqlite3; sqlite3.connect('/app/data/autodeploy.db').execute('DELETE FROM users')"
docker compose restart autodeploy
docker compose logs autodeploy | grep -A 3 "首次启动"   # 新的初始密码
```

**磁盘占用过大**：「统计 → 存储占用」点「立即清理」，或调整「设置 → 数据保留」。
清理永远不会删除当前正在使用的发布版本。

## 自检与本地开发

```bash
.venv/bin/python tests/selfcheck.py   # 全部检查，退出码 0 表示通过
./run.sh                              # 本地开发启动（自动创建虚拟环境，默认 127.0.0.1:8770）
```

自检覆盖调度表达式、密码与会话安全、路径校验、发布与软链原子性、本地 Git 克隆与
变更检测、命令执行与取消、数据库仓储层、完整 API 端到端流程与调度器行为；
不依赖 pytest、不访问公网，可在裸服务器直接运行。

技术栈：Python 3.10+、FastAPI + Uvicorn、SQLite（WAL）、原生 JavaScript 前端；
除 Web 框架外全部使用标准库。许可证：MIT。

## 参与贡献

欢迎提交 Issue 与 Pull Request，请先阅读[贡献指南](CONTRIBUTING.md)，
变更记录见 [CHANGELOG.md](CHANGELOG.md)。安全漏洞请勿在公开 Issue 中披露，改走
[私密报告渠道](https://github.com/j9kkk/auto-deploy/security/advisories/new)。
