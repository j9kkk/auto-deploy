# 更新日志

本文件记录项目的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 版本体系说明：自 0.1.0 起重新计版（此前 1.x 时代的发布已移除，完整历史见
> git 记录）。1.0 之前均为功能打磨期，按 0.x.y 迭代；
> **发布 1.0 需仓库所有者明确确认**。

## [0.2.0] - 2026-10-01

### 变更

- 部署方式收敛为 **Docker Compose**：新增 `Dockerfile`（内置 git / ssh / rsync /
  docker CLI，非 root 运行，自带健康检查）与 `docker-compose.yml`（命名数据卷持久化，
  固定项目名）；重写一键脚本 `scripts/bootstrap.sh`——自动安装 Docker、克隆/更新源码、
  构建启动、健康检查并从日志提取一次性初始密码，重复执行即升级
- 移除 systemd 安装脚本 `scripts/install.sh` 与 `scripts/uninstall.sh`；
  应用内自更新保留给非容器环境，容器部署统一用脚本或 compose 升级
- 仓库迁移至 `github.com/j9kkk/auto-deploy`：git remote、默认更新源、
  CI 徽章与各文档/模板链接同步切换

### 文档

- README 按最新功能重写：快速开始改为 Docker Compose 一键部署，
  新增 Webhook 触发说明，精简凭据/代理/自我更新与故障排查章节

### 新增

- 任务支持 Webhook 触发方式：每个任务创建时自动生成含随机令牌的触发地址
  （`/api/webhooks/{task_id}/{secret}`），向其发送 GET 或 POST 请求即触发一次
  部署，无需登录，可直接接入 GitHub / Gitee 的 Webhook 或外部自动化系统；
  与手动触发共享并发上限、自更新封锁与运行记录，触发方式在运行记录中显示
  为「Webhook」
- 编辑任务页可查看、复制触发地址；管理员可一键重置令牌，旧地址立即失效
- 存量任务在数据库迁移（schema v4）时自动补齐触发令牌；任务导出与整体备份
  均剥离触发令牌，不会外泄

## [0.1.0] - 2026-09-30

AutoDeploy：从 GitHub 自动拉取、构建并部署到 Linux 服务器的自托管常驻服务，
自带原生 JS 的 Web 控制台。本版本是重新计版后的基线，汇总当前全部功能。

### 部署任务

- 仓库地址支持 https / ssh / git / 本地路径，可配分支、子目录与浅克隆深度
  （0 表示完整历史）
- 私有仓库凭据集中管理：HTTPS 访问令牌与 SSH 私钥两类，多任务复用，界面可直接
  测试可用性；凭据经 GIT_ASKPASS 临时文件下发，绝不写入命令行、`.git/config`
  或日志，日志全程脱敏
- SSH 密钥支持：系统自动生成 ed25519 / RSA 密钥对，公钥一键复制并给出指纹，
  站内提供 GitHub 令牌与 Deploy Key 的图文获取指引
- 触发方式：手动运行、固定间隔（30s / 15m / 6h / 2d）或标准 5 段 cron，
  可随时暂停/启用调度
- git 网络韧性：HTTP/1.1 与低速超时兜底，认证类失败输出可操作的中文诊断

### 部署方式（均自动产出 .tar.gz 供下载）

- 仅打包 / 自定义脚本
- 发布目录 + 软链：打包到 `releases/<时间戳>-<commit>`，原子切换 `current`
- 发布 + systemd：软链切换后重启服务并校验 is-active
- Docker / Docker Compose（安装时自动把服务账号加入 docker 组）
- rsync：同步发布目录到远端或本地目标

### 一键安装与升级

- `curl -fsSL https://raw.githubusercontent.com/j9kkk/git-deploy/main/scripts/bootstrap.sh | sudo bash`
  一条命令完成首次部署：自动下载最新发布版、安装 git、校验 Python 3.10+、
  创建系统用户与最小化 sudo 授权、建立虚拟环境、写入并启动 systemd 单元
- 重复执行同一条命令即升级：数据目录不受影响，已有安装自定义的服务名/目录/
  端口从 systemd 单元读取并沿用，systemd 单元与 sudoers 随版本自动重写
- 下载按候选地址依次尝试并校验压缩包完整性；Python 过旧、网络不通均给出
  明确中文提示；系统升级 Python 导致虚拟环境失效可自动重建
- 卸载脚本支持预览（--dry-run）与彻底清除（--purge），默认保留数据目录

### Web 控制台

- 总览：成功率、进行中任务、14 天趋势图、最近与即将执行的任务
- 任务全生命周期管理：增删改查、启停、立即运行、回滚上一版本、下载产物
- 运行记录：按状态/任务/关键字筛选，实时增量日志、自动滚动、可取消运行
- 统计：每日运行曲线、任务排行、磁盘占用
- 应用内自更新：检查新版本、原地升级（进度日志就地展示）、按版本回滚、
  升级历史记录

### 安全

- 管理员口令 PBKDF2-HMAC-SHA256（24 万次迭代）加盐哈希
- 会话 Cookie `HttpOnly` + `SameSite=Lax`，登录失败限流与临时锁定，
  改密后其他会话立即失效；可选「仅 HTTPS 发送会话 Cookie」
- 全站 CSP，无任何外部 CDN 依赖，内网/离线环境可用
- 用户提供的路径严格校验，禁止 `..` 越界与绝对路径注入
- 子进程独立进程组启动，取消时终止整组；超时与取消统一管理
- 操作审计日志，可导出 CSV

### 其他

- 全局 HTTP(S) 代理仅下发给子进程（git 与部署脚本），连通性预检自动适配代理
- 通知 Webhook、数据保留策略、调度并发与超时均可在设置页调整
