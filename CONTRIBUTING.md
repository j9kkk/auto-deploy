# 贡献指南

感谢参与改进 AutoDeploy。本文档说明本地开发、测试与提交规范。

## 开发环境

需要 Python 3.10+ 与 git。

```bash
git clone https://github.com/j9kkk/git-deploy.git
cd git-deploy

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install httpx pyflakes   # 仅供测试与静态检查

./run.sh --initial-password 'DevPass123!'   # 首次启动会创建 admin 账号
```

服务默认监听 `127.0.0.1:8770`。开发时可加 `--log-level DEBUG` 查看完整输出。

## 项目约定

- **仅使用标准库**：除 Web 框架（FastAPI / Uvicorn）外不引入第三方依赖，
  以便在受限服务器上部署。新增依赖前请先说明必要性。
- **前端无构建步骤**：`web/` 下为原生 JavaScript，不引入框架、打包器或 CDN 资源，
  保证内网与离线环境可用。
- **中文注释与界面文案**：面向运维人员的提示信息使用中文，并说明「为什么」而非「做了什么」。
- **测试优先**：任何行为变更都应在 `tests/selfcheck.py` 中补充对应检查。
  该脚本刻意不依赖 pytest，以便在裸服务器上直接运行。

## 测试

```bash
.venv/bin/python tests/selfcheck.py    # 全部检查，退出码 0 表示通过
```

自检覆盖：调度表达式解析、密码与会话安全、请求校验、发布与软链原子性、
本地 Git 克隆与变更检测、命令执行/超时/取消、数据库仓储层、
API 端到端流程（含真实部署与取消）、调度器派发行为。

涉及 Git 的用例一律使用本地仓库（`git init` + 提交），不依赖公网，
因此测试可离线、稳定、快速地运行。

## 静态检查

```bash
.venv/bin/python -m pyflakes app/ tests/     # Python
node --check web/assets/*.js                 # JavaScript
bash -n run.sh scripts/install.sh            # Shell
```

## 提交规范

提交信息使用 [约定式提交](https://www.conventionalcommits.org/zh-hans/) 前缀，
描述使用中文：

```
feat: 支持 rsync 部署方式
fix: 修复日志增量接口重复下发的问题
docs: 补充反向代理配置说明
test: 增加取消运行的回归用例
refactor: 抽取进程组终止逻辑
chore: 更新依赖版本
```

请让每个提交聚焦单一改动，并在信息中说明**原因**而非仅罗列改动。

## 安全相关

提交前请确认：

- 未提交任何真实令牌、密码或私钥；
- `data/`（数据库、工作副本、发布、日志）已被 `.gitignore` 排除；
- 新增日志输出不会泄露凭证（`app/executor.py` 的 `redact()` 负责统一脱敏）。

发现安全问题时请勿直接开公开 Issue，请通过仓库主页的邮箱私下联系维护者。

## 目录职责

| 路径 | 职责 |
|------|------|
| `app/config.py` | 配置加载与目录布局 |
| `app/db.py` / `app/store.py` | SQLite 连接、建表与仓储层 |
| `app/security.py` | 密码哈希、会话令牌、登录限流 |
| `app/schedule.py` | 间隔与 cron 表达式解析 |
| `app/executor.py` | 子进程执行、日志流、取消、凭证脱敏 |
| `app/gitops.py` | 克隆/拉取/重置、变更检测、凭证传递 |
| `app/deployer.py` | 暂存、打包、软链切换、各部署方式 |
| `app/runner.py` | 单次运行的流水线编排 |
| `app/scheduler.py` | 调度循环、并发控制、数据保留 |
| `app/service.py` | 组件装配与服务生命周期 |
| `app/api/` | 路由：auth / tasks / runs / stats / settings |
| `web/` | 控制台前端 |

新增功能时请遵循既有分层：路由只做参数校验与编排，业务逻辑落在对应模块，
SQL 集中在 `app/store.py`。
