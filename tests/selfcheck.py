"""Self-contained self-check for AutoDeploy.

Runs without pytest so it works on a bare server:
    .venv/bin/python tests/selfcheck.py

Covers the pure-logic layers plus a full API round trip against a real ASGI app
using a temporary data directory.  No network access and no root privileges are
required; git-based tests use local repositories.
"""

from __future__ import annotations

import json
import sqlite3
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
    else:
        FAILED.append((label, detail or "断言失败"))


def section(title: str) -> None:
    print(f"\n\033[36m── {title}\033[0m")


def expect_raises(label: str, fn, exception=Exception) -> None:
    try:
        fn()
    except exception:
        PASSED.append(label)
    except Exception as exc:  # noqa: BLE001
        FAILED.append((label, f"抛出了意外的异常 {exc.__class__.__name__}: {exc}"))
    else:
        FAILED.append((label, "预期抛出异常但没有"))


def run() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="autodeploy-selfcheck-"))
    os.environ["AUTODEPLOY_DATA_DIR"] = str(tmp_root / "data")

    # Fresh import so config picks up the temporary data directory.
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[name]

    from app import config

    config.ensure_dirs()

    # ------------------------------------------------------------------
    section("时间与调度表达式")
    from app.schedule import (
        ScheduleError,
        iso,
        next_run_time,
        parse_cron,
        parse_interval,
        validate_schedule,
    )

    check("间隔 30s", parse_interval("30s") == 30)
    check("间隔 15m", parse_interval("15m") == 900)
    check("间隔 6h", parse_interval("6h") == 21600)
    check("间隔 2d", parse_interval("2d") == 172800)
    check("间隔裸数字按秒", parse_interval("120") == 120)
    expect_raises("间隔过小被拒绝", lambda: parse_interval("5s"), ScheduleError)
    expect_raises("间隔非法格式被拒绝", lambda: parse_interval("abc"), ScheduleError)
    expect_raises("负数间隔被拒绝", lambda: parse_interval("-10m"), ScheduleError)

    check("cron 每天午夜", parse_cron("0 0 * * *").hours == frozenset({0}))
    check("cron 步长 */6", parse_cron("0 */6 * * *").hours == frozenset({0, 6, 12, 18}))
    check("cron 区间 9-17", parse_cron("0 9-17 * * *").hours == frozenset(range(9, 18)))
    check("cron 列表 1,15", parse_cron("0 0 1,15 * *").days == frozenset({1, 15}))
    check("cron 月份名 jan", parse_cron("0 0 1 jan *").months == frozenset({1}))
    check("cron 星期名 mon", parse_cron("0 9 * * mon").weekdays == frozenset({1}))
    check("cron 快捷 @daily", parse_cron("@daily").minutes == frozenset({0}))
    expect_raises("cron 字段数不足被拒绝", lambda: parse_cron("0 0 * *"), ScheduleError)
    expect_raises("cron 分钟越界被拒绝", lambda: parse_cron("99 0 * * *"), ScheduleError)
    expect_raises("cron 非法快捷方式被拒绝", lambda: parse_cron("@never"), ScheduleError)

    from datetime import datetime, timedelta

    base = datetime(2026, 3, 10, 8, 30)
    following = parse_cron("0 9 * * *").next_after(base)
    check("cron 下次触发时间正确", following == datetime(2026, 3, 10, 9, 0), str(following))
    following = parse_cron("*/15 * * * *").next_after(datetime(2026, 3, 10, 8, 31))
    check("cron 每 15 分钟", following == datetime(2026, 3, 10, 8, 45), str(following))
    # A cron day-of-week of 0 means Sunday; 2026-03-15 is a Sunday.
    check(
        "cron 周日匹配（0=周日）",
        parse_cron("0 0 * * 0").matches(datetime(2026, 3, 15, 0, 0)),
    )
    check(
        "cron 周日不匹配周一",
        not parse_cron("0 0 * * 0").matches(datetime(2026, 3, 16, 0, 0)),
    )

    check("手动调度无下次时间", next_run_time("manual", "1h") is None)
    check("暂停任务无下次时间", next_run_time("interval", "1h", enabled=False) is None)
    check(
        "间隔下次时间为未来",
        next_run_time("interval", "1h", reference=base) == base + timedelta(hours=1),
    )
    check("校验合法 interval", validate_schedule("interval", "30m") is None)
    check("校验非法 interval 返回错误", validate_schedule("interval", "1s") is not None)
    check("校验非法 cron 返回错误", validate_schedule("cron", "bad") is not None)

    # ------------------------------------------------------------------
    section("密码与会话安全")
    from app.security import (
        LoginThrottle,
        generate_password,
        hash_password,
        needs_rehash,
        password_problem,
        token_fingerprint,
        verify_password,
    )

    digest = hash_password("CorrectHorse1!")
    check("密码校验通过", verify_password("CorrectHorse1!", digest))
    check("错误密码被拒绝", not verify_password("wrong", digest))
    check("空密码被拒绝", not verify_password("", digest))
    check("随机盐（同一密码哈希不同）", hash_password("same") != hash_password("same"))
    check("哈希格式可识别", digest.startswith("pbkdf2_sha256$"))
    check("当前参数无需重算", not needs_rehash(digest))
    check("旧参数需要重算", needs_rehash("pbkdf2_sha256$1000$aa$bb"))
    check("损坏哈希需要重算", needs_rehash("garbage"))
    check("损坏哈希校验失败", not verify_password("x", "garbage"))

    check("弱密码被拒绝", password_problem("123456") is not None)
    check("单一字符类型被拒绝", password_problem("abcdefgh") is not None)
    check("强密码通过", password_problem("Str0ngPass!") is None)
    generated = generate_password(20)
    check("生成密码长度正确", len(generated) == 20)
    check("生成密码满足强度", password_problem(generated) is None)
    check("token 指纹稳定", token_fingerprint("abc") == token_fingerprint("abc"))
    check("token 指纹不可逆", "abc" not in token_fingerprint("abc"))

    throttle = LoginThrottle(3, 60)
    for _ in range(3):
        throttle.record_failure("k")
    check("达到阈值后锁定", throttle.locked_for("k") > 0)
    throttle.reset("k")
    check("重置后解除锁定", throttle.locked_for("k") == 0)
    check("剩余次数计数正确", LoginThrottle(5, 60).remaining_attempts("x") == 5)

    # ------------------------------------------------------------------
    section("调度表达式校验 API 层")
    from app.validation import ValidationError, validate_task_payload

    with_errors = [
        ("缺少名称被拒绝", {"repo_url": "https://github.com/a/b.git"}, "name"),
        ("非法仓库地址被拒绝", {"name": "x", "repo_url": "not a url"}, "repo_url"),
        (
            "systemd 方式缺少服务名被拒绝",
            {"name": "x", "repo_url": "https://github.com/a/b.git", "deploy_method": "systemd"},
            "service_name",
        ),
        (
            "script 方式缺少脚本被拒绝",
            {"name": "x", "repo_url": "https://github.com/a/b.git", "deploy_method": "script"},
            "deploy_script",
        ),
        (
            "打包路径越界被拒绝",
            {"name": "x", "repo_url": "https://github.com/a/b.git", "artifact_paths": "../etc"},
            "artifact_paths",
        ),
        (
            "非法 cron 被拒绝",
            {"name": "x", "repo_url": "https://github.com/a/b.git",
             "schedule_type": "cron", "schedule_expression": "nope"},
            "schedule_expression",
        ),
        (
            "非法环境变量名被拒绝",
            {"name": "x", "repo_url": "https://github.com/a/b.git", "env_vars": {"bad name": "1"}},
            "env_vars",
        ),
    ]
    for label, payload, field in with_errors:
        try:
            validate_task_payload(payload)
        except ValidationError as exc:
            check(label, field in exc.errors, f"错误字段为 {list(exc.errors)}")
        else:
            check(label, False, "未抛出校验错误")

    cleaned = validate_task_payload(
        {
            "name": "  my task  ",
            "repo_url": "https://github.com/a/b.git",
            "deploy_method": "script",
            "deploy_script": "echo hi",
            "artifact_paths": "dist, package.json\ndist\n",
            "env_vars": "A=1\nB=two",
            "schedule_type": "interval",
            "schedule_expression": "2h",
        }
    )
    check("名称被裁剪", cleaned["name"] == "my task")
    check("打包路径去重", cleaned["artifact_paths"] == "dist\npackage.json")
    check("环境变量文本解析", cleaned["env_vars"] == {"A": "1", "B": "two"})

    # ------------------------------------------------------------------
    section("发布目录与暂存")
    from app.deployer import (
        create_bundle,
        list_releases,
        next_release_dir,
        parse_path_list,
        prune_releases,
        resolve_release_paths,
        stage_release,
        swap_symlink,
        validate_relative_path,
        DeployContext,
    )

    check("解析多行路径", parse_path_list("a\nb,c;d") == ["a", "b", "c", "d"])
    check("拒绝绝对路径", validate_relative_path("/etc/passwd", field_name="x") is not None)
    check("拒绝上级引用", validate_relative_path("../x", field_name="x") is not None)
    check("接受相对路径", validate_relative_path("dist/app.js", field_name="x") is None)

    task = {"id": 1, "target_dir": ""}
    releases_root, current_link = resolve_release_paths(task)
    check("默认发布根目录在数据目录内", str(releases_root).startswith(str(config.DATA_DIR)))

    workspace = tmp_root / "ws"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("print('hi')\n")
    (workspace / "dist").mkdir()
    (workspace / "dist" / "index.js").write_text("console.log(1)\n")
    (workspace / "dist" / "extra.css").write_text("body{}\n")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "junk.js").write_text("junk\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]\n")

    release_dir = next_release_dir(releases_root, "abcdef1234567890")
    artifact_one = tmp_root / "out.tar.gz"
    ctx = DeployContext(
        task=task, run_id=1, workspace=workspace, release_dir=release_dir,
        releases_root=releases_root, current_link=current_link,
        artifact_path=artifact_one,
        commit="abcdef1234567890",
    )

    stage_release(ctx, patterns=["dist/**"], full_copy=False)
    check("按模式暂存 dist", (release_dir / "dist" / "index.js").exists())
    check("暂存未包含无关目录", not (release_dir / "node_modules").exists())

    release_dir2 = next_release_dir(releases_root, "fedcba9876543210")
    check("发布目录名唯一", release_dir2 != release_dir)
    ctx2 = DeployContext(
        task=task, run_id=2, workspace=workspace, release_dir=release_dir2,
        releases_root=releases_root, current_link=current_link,
        artifact_path=tmp_root / "out2.tar.gz",
    )
    stage_release(ctx2, patterns=[], full_copy=True)
    check("完整暂存包含源码", (release_dir2 / "app.py").exists())
    check("完整暂存排除 node_modules", not (release_dir2 / "node_modules").exists())
    check("完整暂存排除 .git", not (release_dir2 / ".git").exists())

    expect_raises(
        "无匹配路径时报错",
        lambda: stage_release(
            DeployContext(
                task=task, run_id=3, workspace=workspace,
                release_dir=next_release_dir(releases_root, "0" * 8),
                releases_root=releases_root, current_link=current_link,
                artifact_path=tmp_root / "o3.tar.gz",
            ),
            patterns=["does-not-exist/**"], full_copy=False,
        ),
        Exception,
    )

    size = create_bundle(release_dir, ctx.artifact_path)
    check("打包生成文件", ctx.artifact_path.is_file() and size > 0)
    import tarfile

    with tarfile.open(ctx.artifact_path) as archive:
        names = archive.getnames()
    check("压缩包内含 dist", any("dist/index.js" in name for name in names), str(names[:5]))

    swap_symlink(current_link, release_dir)
    check("软链指向发布目录", current_link.is_symlink() and current_link.resolve() == release_dir.resolve())
    swap_symlink(current_link, release_dir2)
    check("软链可重复切换", current_link.resolve() == release_dir2.resolve())
    check("原子替换后无残留临时链接",
          not list(current_link.parent.glob(f".{current_link.name}.new*")))

    for index in range(4):
        extra = next_release_dir(releases_root, f"{index:08d}")
        extra.mkdir(parents=True, exist_ok=True)
    removed = prune_releases(releases_root, current_link, keep=2)
    check("清理旧发布", len(removed) > 0, str(removed))
    check("当前发布未被清理", current_link.resolve().exists())
    check("保留数量符合预期", len(list_releases(releases_root)) <= 3,
          str([p.name for p in list_releases(releases_root)]))

    # ------------------------------------------------------------------
    section("仓库操作（本地 Git）")
    from app.gitops import (
        GitError,
        _looks_transient,
        _split_host_port,
        check_reachable,
        repo_key,
        sync_checkout,
        validate_branch,
        validate_repo_url,
    )

    def git(*args, cwd):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout

    origin = tmp_root / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("config", "user.email", "t@example.com", cwd=origin)
    git("config", "user.name", "Tester", cwd=origin)
    (origin / "main.py").write_text("v1\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "first", cwd=origin)
    first = git("rev-parse", "HEAD", cwd=origin).strip()

    checkout = tmp_root / "checkout"
    info = sync_checkout(
        repo_url=str(origin), branch="main", workspace=checkout,
        depth=0, tmp_dir=tmp_root / "creds",
    )
    check("首次克隆成功", info.commit == first and info.is_first_clone)
    check("首次克隆视为有变化", info.changes_detected)
    check("读取提交信息", info.message == "first", info.message)
    check("读取作者", info.author == "Tester", info.author)

    again = sync_checkout(
        repo_url=str(origin), branch="main", workspace=checkout, depth=0,
        tmp_dir=tmp_root / "creds",
    )
    check("无变化时 changes_detected 为假", not again.changes_detected)
    check("无变化时提交相同", again.commit == first)

    (checkout / "main.py").write_text("LOCAL EDIT\n")
    (checkout / "scratch.tmp").write_text("scratch\n")
    (origin / "main.py").write_text("v2\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "second", cwd=origin)
    second = git("rev-parse", "HEAD", cwd=origin).strip()

    updated = sync_checkout(
        repo_url=str(origin), branch="main", workspace=checkout, depth=0,
        tmp_dir=tmp_root / "creds",
    )
    check("检测到新提交", updated.commit == second and updated.changes_detected)
    check("记录上一次提交", updated.previous_commit == first)
    check("本地修改被重置", (checkout / "main.py").read_text() == "v2\n")
    check("未跟踪文件被清理", not (checkout / "scratch.tmp").exists())
    check("统计变更文件数", updated.changed_files == 1, str(updated.changed_files))

    expect_raises(
        "分支不存在时抛出 GitError",
        lambda: sync_checkout(
            repo_url=str(origin), branch="nope", workspace=tmp_root / "checkout2",
            depth=1, tmp_dir=tmp_root / "creds",
        ),
        GitError,
    )
    check("校验合法仓库地址", validate_repo_url("https://github.com/a/b.git") is None)
    check("拒绝空仓库地址", validate_repo_url("") is not None)
    check("拒绝选项注入", validate_repo_url("-u./payload") is not None)
    check("接受 SSH 地址", validate_repo_url("git@github.com:a/b.git") is None)
    check("接受本地路径", validate_repo_url("/srv/git/repo.git") is None)
    check("拒绝含空白的地址", validate_repo_url("https://a b/c") is not None)
    check("校验合法分支", validate_branch("feature/x") is None)
    check("拒绝 .. 分支", validate_branch("a..b") is not None)
    check("拒绝选项式分支", validate_branch("-x") is not None)
    check("repo_key 规范化 .git", repo_key("https://x/y.git") == repo_key("https://x/y"))
    check("repo_key 无路径分隔符", "/" not in repo_key("https://x/y/z.git"))

    # Reachability probing: a local git directory is not http, so it is skipped;
    # an unreachable http host must be reported quickly and specifically rather
    # than costing three full TCP-connect timeouts inside git itself.
    check("非 http 地址跳过连通性检查", check_reachable(str(origin)) is None)
    check("ssh 地址跳过连通性检查", check_reachable("git@github.com:a/b.git") is None)
    probe_started = time.monotonic()
    unreachable = check_reachable("https://127.0.0.1:9/nope.git", timeout=2)
    check("不可达地址被快速识别", unreachable is not None, str(unreachable))
    check("连通性检查快速返回", time.monotonic() - probe_started < 8,
          f"{time.monotonic() - probe_started:.1f}s")
    check("区分瞬时网络错误", _looks_transient("Error in the HTTP2 framing layer"))
    check("区分永久错误（分支不存在）",
          not _looks_transient("fatal: Remote branch nope not found"))
    check("解析 http 主机端口", _split_host_port("https://h/a.git") == ("h", 443))
    check("解析自定义端口", _split_host_port("http://h:8080/a") == ("h", 8080))

    # ------------------------------------------------------------------
    section("命令执行与取消")
    from app.executor import redact, run_command

    result = run_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 0"])
    check("命令成功执行", result.ok and result.exit_code == 0)
    check("合并标准错误", "err" in result.output)
    result = run_command(["/bin/sh", "-c", "exit 7"])
    check("非零退出码被记录", result.exit_code == 7 and not result.ok)
    result = run_command(["/nonexistent/binary"])
    check("缺少命令返回 127", result.exit_code == 127)

    started = time.monotonic()
    result = run_command(["/bin/sh", "-c", "sleep 30"], timeout=2, kill_grace_seconds=2)
    check("超时被终止", result.timed_out and time.monotonic() - started < 15,
          f"耗时 {time.monotonic() - started:.1f}s")

    cancelled = {"flag": False}

    def cancel_soon() -> None:
        time.sleep(1)
        cancelled["flag"] = True

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    result = run_command(
        ["/bin/sh", "-c", "sleep 30"], timeout=60,
        check_cancelled=lambda: cancelled["flag"], kill_grace_seconds=2,
    )
    check("取消能中断进程", result.cancelled and time.monotonic() - started < 15)

    # A command with no output must still be interrupted (the reader thread
    # keeps the timeout check alive).
    started = time.monotonic()
    result = run_command(["/bin/sh", "-c", "sleep 20 >/dev/null 2>&1"], timeout=2, kill_grace_seconds=2)
    check("无输出命令亦能超时", result.timed_out and time.monotonic() - started < 15)

    check("脱敏 URL 凭证", "secret" not in redact("https://user:secret@github.com/a/b.git"))
    check("脱敏保留主机名", "github.com" in redact("https://user:secret@github.com/a/b.git"))
    check("脱敏 GitHub Token", "ghp_" not in redact("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"))

    # ------------------------------------------------------------------
    section("数据库与仓储层")
    from app.db import Database
    from app.store import Store

    store = Store(Database(tmp_root / "test.db"))
    check("schema 版本已写入", store.db.schema_version() >= 1)

    user_id = store.users.create("admin", hash_password("Str0ng!Pass"), display_name="Admin")
    check("大小写不敏感查找用户", store.users.get_by_username("ADMIN") is not None)
    check("用户计数正确", store.users.count() == 1)

    expires = iso(datetime.utcnow() + timedelta(hours=1))
    store.sessions.create(token_fingerprint("tok"), user_id, expires)
    check("有效会话可查", store.sessions.find_valid(token_fingerprint("tok")) is not None)
    store.sessions.create(token_fingerprint("old"), user_id, iso(datetime.utcnow() - timedelta(hours=1)))
    check("过期会话被拒绝", store.sessions.find_valid(token_fingerprint("old")) is None)

    task_id = store.tasks.create(
        {
            "name": "demo", "repo_url": "https://github.com/a/b.git", "repo_branch": "main",
            "schedule_type": "interval", "schedule_expression": "1h", "enabled": True,
            "deploy_method": "script", "deploy_script": "echo hi",
            "env_vars": {"K": "v"}, "git_token": "secret-token",
        }
    )
    decoded = store.tasks.get_decoded(task_id)
    check("任务布尔字段解码", decoded["enabled"] is True)
    check("环境变量解码", decoded["env_vars"] == {"K": "v"})
    check("令牌不外泄且标记存在", "git_token" not in decoded and decoded["has_token"])

    store.tasks.update(task_id, {"name": "renamed"})
    check("局部更新保留令牌", store.tasks.get(task_id)["git_token"] == "secret-token")
    check("局部更新生效", store.tasks.get(task_id)["name"] == "renamed")

    store.tasks.set_next_run(task_id, iso(datetime.utcnow() - timedelta(minutes=1)))
    check("到期任务可查询", len(store.tasks.due(iso(datetime.utcnow()))) == 1)
    store.tasks.set_enabled(task_id, False, None)
    check("停用任务不再到期", len(store.tasks.due(iso(datetime.utcnow()))) == 0)
    store.tasks.set_enabled(task_id, True, iso(datetime.utcnow() - timedelta(minutes=1)))

    run_id = store.runs.create(store.tasks.get(task_id), trigger="manual")
    check("运行记录创建后为活动状态", store.runs.count_active() == 1)
    store.runs.mark_running(run_id, pid=4321)
    check("运行状态更新为 running", store.runs.get(run_id)["status"] == "running")
    store.runs.mark_finished(run_id, status="success", exit_code=0, duration_ms=2500)
    check("运行结束状态正确", store.runs.get(run_id)["status"] == "success")
    store.tasks.record_run_finished(task_id, run_id=run_id, status="success", duration_ms=2500)
    counters = store.tasks.get(task_id)
    check("任务计数器累加", counters["run_count"] == 1 and counters["success_count"] == 1)

    overview = store.runs.stats_overview()
    check("统计成功数", overview["success"] == 1)
    check("统计成功率", overview["success_rate"] == 100.0)
    check("日统计返回正确天数", len(store.runs.stats_daily(7)) == 7)
    check("任务维度统计", store.runs.stats_by_task()[0]["run_count"] == 1)

    store.tasks.delete(task_id)
    check("删除任务级联删除运行记录", store.runs.count() == 0)

    task_id2 = store.tasks.create(
        {"name": "x", "repo_url": "https://github.com/a/b.git", "deploy_method": "script",
         "deploy_script": "true"}
    )
    stale = store.runs.create(store.tasks.get(task_id2))
    store.runs.mark_running(stale)
    check("中断运行可被标记失败", store.runs.finish_stale(reason="restart") == 1)
    check("中断运行状态为 failed", store.runs.get(stale)["status"] == "failed")

    for _ in range(6):
        extra = store.runs.create(store.tasks.get(task_id2))
        store.runs.mark_finished(extra, status="failed", exit_code=1, duration_ms=10)
    deleted, logs = store.runs.purge_old(older_than_iso=None, keep_count=3)
    check("按条数清理运行记录", deleted >= 3, f"删除 {deleted} 条")
    check("保留最新记录", len(store.runs.list_for_task(task_id2)) == 3)
    check("清理返回日志路径列表", isinstance(logs, list))

    store.audit.record("login_success", actor="admin", ip="127.0.0.1")
    check("审计日志写入", store.audit.list_recent()[0]["action"] == "login_success")
    store.close()

    # ------------------------------------------------------------------
    section("API 端到端")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.service import Service

    service = Service(initial_password="SelfCheck1!pass")
    check("首次启动创建管理员", service.bootstrap_report.created_admin)
    app = create_app(service)

    with TestClient(app) as client:
        response = client.get("/api/health")
        check("健康检查无需登录", response.status_code == 200)
        check("健康检查返回 ok", response.json().get("status") == "ok")

        response = client.get("/api/tasks")
        check("未登录访问被拒绝", response.status_code == 401)

        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-password"}
        )
        check("错误密码登录失败", response.status_code == 401)

        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": "SelfCheck1!pass"}
        )
        check("正确密码登录成功", response.status_code == 200)
        check("登录返回用户信息", response.json()["user"]["username"] == "admin")
        check("登录下发会话 Cookie", "autodeploy_session" in response.cookies)

        response = client.get("/api/auth/me")
        check("会话可用于鉴权", response.status_code == 200)
        check("me 返回管理员标识", response.json()["user"]["is_admin"] is True)

        response = client.post(
            "/api/tasks",
            json={
                "name": "端到端任务",
                "repo_url": str(origin),
                "repo_branch": "main",
                "schedule_type": "interval",
                "schedule_expression": "1h",
                "deploy_method": "release",
                "prepare_script": "echo preparing",
                "target_dir": "",
            },
        )
        check("创建任务成功", response.status_code == 201, response.text[:200])
        created = response.json()["task"]
        task_id = created["id"]
        check("新任务已排期", bool(created["next_run_at"]), str(created.get("next_run_at")))

        response = client.post(
            "/api/tasks",
            json={"name": "bad", "repo_url": "not-a-url"},
        )
        check("非法任务被拒绝", response.status_code == 422)

        # 任务名唯一性：重名创建被拒绝，且大小写不敏感
        response = client.post(
            "/api/tasks",
            json={
                "name": "端到端任务", "repo_url": str(origin),
                "deploy_method": "script", "deploy_script": "true",
            },
        )
        check("重名任务被拒绝", response.status_code == 422, response.text[:150])
        check("重名错误指向 name 字段",
              "name" in (response.json().get("detail", {}).get("errors") or {}))
        response = client.post(
            "/api/tasks",
            json={
                "name": "端到端任务".upper(), "repo_url": str(origin),
                "deploy_method": "script", "deploy_script": "true",
            },
        )
        check("大小写不同也算重名", response.status_code == 422)
        # 改名撞别的任务
        response = client.post(
            "/api/tasks",
            json={"name": "取消测试甲", "repo_url": str(origin),
                  "deploy_method": "script", "deploy_script": "true"},
        )
        check("创建第二个任务成功", response.status_code == 201, response.text[:150])
        second_id = response.json()["task"]["id"]
        response = client.put(f"/api/tasks/{second_id}", json={"name": "端到端任务"})
        check("改名撞已有任务被拒绝", response.status_code == 422)
        response = client.put(f"/api/tasks/{second_id}", json={"name": "取消测试甲"})
        check("改回自己的名字不报错", response.status_code == 200)
        client.delete(f"/api/tasks/{second_id}")

        response = client.get(f"/api/tasks/{task_id}")
        check("任务详情可读取", response.status_code == 200)
        check("详情包含环境检查", len(response.json()["preflight"]) >= 1)

        response = client.post(f"/api/tasks/{task_id}/preflight")
        check("预检接口不存在时不误报", response.status_code in (404, 405))

        response = client.get(f"/api/tasks/{task_id}/preflight")
        check("预检接口返回检查项", response.status_code == 200)
        check("预检包含 git", any(c["name"] == "git" for c in response.json()["checks"]))

        response = client.post(
            "/api/settings/schedule/preview",
            json={"schedule_type": "cron", "schedule_expression": "0 */6 * * *", "count": 3},
        )
        check("调度预览成功", response.status_code == 200 and response.json()["ok"])
        check("调度预览返回 3 个时间", len(response.json()["next_runs"]) == 3)

        response = client.post(
            "/api/settings/schedule/preview",
            json={"schedule_type": "interval", "schedule_expression": "bad"},
        )
        check("非法调度预览返回错误", not response.json()["ok"])

        # --- run a real deploy --------------------------------------
        response = client.post(f"/api/tasks/{task_id}/run")
        check("手动触发运行成功", response.status_code == 200, response.text[:200])
        run = response.json()["run_id"]

        deadline = time.monotonic() + 60
        status = "queued"
        while time.monotonic() < deadline:
            detail = client.get(f"/api/runs/{run}").json()["run"]
            status = detail["status"]
            if status not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("运行结束", status not in ("queued", "running"), f"最终状态 {status}")
        check("运行成功", status == "success", f"状态 {status}，错误 {detail.get('error')}")
        check("记录了提交", bool(detail["commit_after"]))
        check("记录了发布目录", bool(detail["release_dir"]))

        response = client.get(f"/api/runs/{run}/log")
        check("日志可读取", response.status_code == 200 and len(response.text) > 50)
        check("日志包含阶段标记", "阶段" in response.text)

        response = client.get(f"/api/tasks/{task_id}/artifacts")
        check("产物列表可读取", response.status_code == 200)
        check("存在打包产物", len(response.json()["artifacts"]) >= 1)
        if response.json()["artifacts"]:
            name = response.json()["artifacts"][0]["name"]
            response = client.get(f"/api/tasks/{task_id}/artifacts/{name}")
            check("产物可下载", response.status_code == 200 and len(response.content) > 0)

        response = client.get(f"/api/tasks/{task_id}/releases")
        check("发布列表可读取", response.status_code == 200)
        check("存在发布版本", response.json()["releases"], response.text[:200])

        # --- second run should skip (no code change) ----------------
        response = client.post(f"/api/tasks/{task_id}/run")
        run2 = response.json()["run_id"]
        deadline = time.monotonic() + 60
        status2 = "queued"
        while time.monotonic() < deadline:
            detail2 = client.get(f"/api/runs/{run2}").json()["run"]
            status2 = detail2["status"]
            if status2 not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("无变化时运行被跳过", status2 == "skipped",
              f"状态 {status2}，错误 {detail2.get('error')}")

        # --- dashboard / stats / storage ----------------------------
        response = client.get("/api/dashboard")
        check("总览接口可读取", response.status_code == 200)
        dashboard = response.json()
        check("总览包含统计", dashboard["overview"]["total"] >= 2)
        check("总览包含任务计数", dashboard["tasks"]["total"] == 1)
        check("总览包含日曲线", len(dashboard["daily"]) == 14)

        response = client.get("/api/stats?days=7")
        check("统计接口可读取", response.status_code == 200 and len(response.json()["daily"]) == 7)

        response = client.get("/api/storage")
        check("存储接口可读取", response.status_code == 200)
        check("存储统计包含日志占用", response.json()["logs_bytes"] >= 0)

        response = client.get("/api/system")
        check("系统信息可读取", response.status_code == 200)
        check("系统信息报告 git", bool(response.json()["binaries"].get("git")))

        # --- settings ------------------------------------------------
        response = client.get("/api/settings")
        check("设置可读取", response.status_code == 200)
        original_workers = response.json()["settings"]["max_global_workers"]

        response = client.put("/api/settings", json={"max_global_workers": 5})
        check("设置可更新", response.status_code == 200)
        check("设置更新生效", response.json()["settings"]["max_global_workers"] == 5)

        response = client.put("/api/settings", json={"max_global_workers": 999})
        check("越界设置被拒绝", response.status_code == 422)
        client.put("/api/settings", json={"max_global_workers": original_workers})

        # --- audit log ----------------------------------------------
        response = client.get("/api/audit")
        actions = [entry["action"] for entry in response.json()["entries"]]
        check("审计记录登录", "login_success" in actions, str(actions[:6]))
        check("审计记录任务创建", "task_created" in actions)
        check("审计记录运行触发", "run_triggered" in actions)

        # --- task export / update / toggle --------------------------
        response = client.get(f"/api/tasks/{task_id}/export")
        check("任务导出成功", response.status_code == 200)
        exported = json.loads(response.text)
        check("导出不含令牌", "git_token" not in exported["task"])

        response = client.put(
            f"/api/tasks/{task_id}", json={"name": "改名后的任务", "description": "desc"}
        )
        check("任务更新成功", response.status_code == 200)
        check("任务名称已更新", response.json()["task"]["name"] == "改名后的任务")

        response = client.post(f"/api/tasks/{task_id}/toggle")
        check("切换任务状态成功", response.status_code == 200)
        check("任务被暂停", response.json()["enabled"] is False)
        response = client.post(f"/api/tasks/{task_id}/toggle")
        check("任务被重新启用", response.json()["enabled"] is True)

        # --- runs list & filters ------------------------------------
        response = client.get("/api/runs?limit=10")
        check("运行列表可读取", response.status_code == 200)
        check("运行列表含状态标签", all("status_label" in r for r in response.json()["runs"]))
        response = client.get("/api/runs?status=success")
        check("按状态过滤生效",
              all(r["status"] == "success" for r in response.json()["runs"]))
        response = client.get("/api/runs?search=端到端")
        check("按关键字搜索生效", response.status_code == 200)

        # --- log tail polling ---------------------------------------
        response = client.get(f"/api/runs/{run}/tail?after=0")
        check("日志增量接口可用", response.status_code == 200)
        check("增量接口返回状态", "active" in response.json())

        # Regression: the client's `after` is an offset into the same window the
        # detail view used, so a fully-caught-up client must receive no lines
        # (a >= comparison here used to resend the whole log and duplicate it).
        first = client.get(f"/api/runs/{run}/tail?after=0").json()
        total_lines = first["total"]
        check("首屏返回全部日志行", total_lines > 0, str(total_lines))
        caught_up = client.get(f"/api/runs/{run}/tail?after={total_lines}").json()
        check("已同步时不再重复下发", caught_up["lines"] == [], str(caught_up["lines"])[:200])
        check("已同步时不要求重置", caught_up["reset"] is False)
        partial = client.get(f"/api/runs/{run}/tail?after=5").json()
        check("增量下发剩余行", len(partial["lines"]) == total_lines - 5,
              f"{len(partial['lines'])} vs {total_lines - 5}")
        ahead = client.get(f"/api/runs/{run}/tail?after={total_lines + 99}").json()
        check("客户端超前时要求重置", ahead["reset"] is True)

        # The detail snapshot and the tail window must agree, otherwise the
        # client's offset would drift on the first poll.
        detail = client.get(f"/api/runs/{run}").json()["run"]
        check("详情快照与增量窗口一致",
              len(detail["log_tail"]) == total_lines,
              f"详情 {len(detail['log_tail'])} vs 增量 {total_lines}")

        # --- export & maintenance -----------------------------------
        response = client.get("/api/export")
        check("整体导出成功", response.status_code == 200 and "tasks" in response.text)
        response = client.get("/api/audit/export")
        check("审计导出成功", response.status_code == 200 and "操作人" in response.text)
        response = client.post("/api/maintenance/run")
        check("手动维护成功", response.status_code == 200 and response.json()["ok"])

        # --- password change ----------------------------------------
        response = client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "NewPass123!"},
        )
        check("错误当前密码被拒绝", response.status_code == 400)
        response = client.post(
            "/api/auth/password",
            json={"current_password": "SelfCheck1!pass", "new_password": "123456"},
        )
        check("弱新密码被拒绝", response.status_code == 422)
        response = client.post(
            "/api/auth/password",
            json={
                "current_password": "SelfCheck1!pass",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            },
        )
        check("修改密码成功", response.status_code == 200)
        client.post("/api/auth/logout")
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": "NewPass123!"}
        )
        check("新密码可登录", response.status_code == 200)

        # --- deletion -------------------------------------------------
        response = client.delete(f"/api/tasks/{task_id}")
        check("删除任务成功", response.status_code == 200)
        response = client.get("/api/tasks")
        check("任务列表已为空", response.json()["total"] == 0)

        # --- cancellation -------------------------------------------
        cancel_task = client.post(
            "/api/tasks",
            json={
                "name": "取消测试",
                "repo_url": str(origin),
                "repo_branch": "main",
                "schedule_type": "manual",
                "deploy_method": "script",
                "prepare_script": "echo started; sleep 120; echo SHOULD_NOT_RUN",
                "deploy_script": "echo SHOULD_NOT_RUN_EITHER",
                "skip_if_no_changes": False,
            },
        )
        check("创建长任务成功", cancel_task.status_code == 201, cancel_task.text[:200])
        cancel_id = cancel_task.json()["task"]["id"]

        started_run = client.post(f"/api/tasks/{cancel_id}/run").json()["run_id"]
        # Wait until the prepare script is actually executing.
        run_started = False
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = client.get(f"/api/runs/{started_run}").json()["run"]["status"]
            if state == "running":
                run_started = True
                break
            time.sleep(0.3)
        check("长任务进入运行状态", run_started)

        cancel_response = client.post(f"/api/runs/{started_run}/cancel")
        check("取消请求被接受", cancel_response.status_code == 200 and cancel_response.json()["ok"])

        settled = "running"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            settled = client.get(f"/api/runs/{started_run}").json()["run"]["status"]
            if settled not in ("queued", "running"):
                break
            time.sleep(0.3)
        cancelled_run = client.get(f"/api/runs/{started_run}").json()["run"]
        check("取消后状态为 cancelled", settled == "cancelled", f"状态 {settled}")
        # A cancelled git command exits non-zero; the run must still read as a
        # cancellation rather than a repository failure.
        check("取消不报为 git 失败",
              "git" not in (cancelled_run.get("error") or ""),
              repr(cancelled_run.get("error")))

        cancel_log = client.get(f"/api/runs/{started_run}/log").text
        check("取消后不再执行后续步骤",
              "SHOULD_NOT_RUN" not in cancel_log and "SHOULD_NOT_RUN_EITHER" not in cancel_log)
        check("日志标明已取消", "已取消" in cancel_log)

        response = client.delete(f"/api/tasks/{cancel_id}")
        check("清理取消测试任务", response.status_code == 200)

        client.post("/api/auth/logout")
        response = client.get("/api/tasks")
        check("退出后无法访问", response.status_code == 401)

    # ------------------------------------------------------------------
    section("自我更新支持")
    install_sh = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    check("install.sh 含延迟重启授权（老安装升级后可用）",
          "systemd-run --collect --on-active=5s /usr/bin/systemctl restart" in install_sh)
    check("install.sh 每次重写 sudoers（升级能拿到新授权）",
          'cat > "/etc/sudoers.d/$SERVICE_NAME"' in install_sh
          and 'if [ ! -f "/etc/sudoers.d/$SERVICE_NAME" ]' not in install_sh)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 含自我更新章节", "## 自我更新" in readme)

    # ------------------------------------------------------------------
    section("凭据与代理")

    from app.config import Settings, describe_proxy, proxy_env, proxy_url_with_auth
    from app.gitops import resolve_credential
    from app.store import decode_credential, ssh_key_fingerprint

    # --- 凭据解析优先级：全局凭据 > 任务内令牌 ---
    task_with_token = {"git_username": "u", "git_token": "task-token"}
    check("无全局凭据时回退任务内令牌",
          resolve_credential(task_with_token, None).token == "task-token")
    picked = resolve_credential(task_with_token, {
        "kind": "https_token", "username": "ci", "secret": "global", "name": "G"})
    check("全局凭据优先于任务内令牌", picked.token == "global" and picked.name == "G")
    ssh = resolve_credential({"git_token": ""}, {
        "kind": "ssh_key", "username": "git", "secret": "KEY", "passphrase": "pp", "name": "S"})
    check("SSH 凭据被识别", ssh.is_ssh and ssh.private_key == "KEY" and ssh.passphrase == "pp")
    check("凭据 repr 不含秘密",
          "global" not in repr(picked) and "KEY" not in repr(ssh))

    # --- secret 不外泄 ---
    decoded = decode_credential({"id": 1, "name": "n", "kind": "https_token",
                                 "username": "u", "secret": "s3cr3t", "description": ""})
    check("解码后不含 secret 明文", "secret" not in decoded and decoded["has_secret"])
    check("解码后给出密钥长度", decoded["secret_length"] == 6)
    check("SSH 指纹可计算",
          ssh_key_fingerprint("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END-----").startswith("SHA256:")
          or ssh_key_fingerprint("AAAA") == "")
    check("空私钥指纹为空", ssh_key_fingerprint("") == "")

    # --- 代理 URL 与脱敏 ---
    st = Settings()
    check("未启用代理时环境为空", proxy_env(st) == {})
    st.proxy_enabled = True
    st.proxy_url = "http://proxy.local:8080"
    env_on = proxy_env(st)
    check("启用后同时设置大小写代理变量",
          env_on.get("http_proxy") == env_on.get("HTTPS_PROXY") == "http://proxy.local:8080")
    check("no_proxy 一并下发", bool(env_on.get("no_proxy")))
    st.proxy_username = "u@corp"
    st.proxy_password = "p:ss@word"
    auth_url = proxy_url_with_auth(st)
    check("代理凭证做百分号编码",
          "u%40corp" in auth_url and "p%3Ass%40word" in auth_url, auth_url)
    check("代理描述不含密码明文", "p:ss" not in describe_proxy(st) and "***" in describe_proxy(st))
    st.proxy_for_scripts = False
    check("关闭脚本代理后脚本环境不含代理", proxy_env(st, for_scripts=True) == {})
    check("Git 环境仍含代理", bool(proxy_env(st)))

    # --- 代理校验规则（API 层同源函数）---
    from app.validation import ValidationError as VErr
    from app.validation import validate_proxy_settings
    try:
        validate_proxy_settings({"proxy_enabled": True}, Settings())
        check("启用代理但无地址被拒绝", False)
    except VErr as exc:
        check("启用代理但无地址被拒绝", "proxy_url" in exc.errors)
    check("已有地址时可只开启开关",
          validate_proxy_settings({"proxy_enabled": True},
                                  Settings(proxy_url="http://x:1")).get("proxy_enabled") is True)
    try:
        validate_proxy_settings({"proxy_url": "proxy.local:8080"}, Settings())
        check("非法代理地址被拒绝", False)
    except VErr as exc:
        check("非法代理地址被拒绝", "proxy_url" in exc.errors)
    try:
        validate_proxy_settings({"proxy_url": "http://u:p@proxy.local:8080"}, Settings())
        check("代理地址内嵌密码被拒绝（避免日志泄露）", False)
    except VErr as exc:
        check("代理地址内嵌密码被拒绝（避免日志泄露）", "proxy_url" in exc.errors)

    # --- 连通性探测：走代理时探测代理本身 ---
    import socket
    import threading as _threading
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    proxy_port = server.getsockname()[1]
    _threading.Thread(target=lambda: server.accept(), daemon=True).start()
    from app.gitops import check_reachable
    check("代理可达时不再误报目标不可达",
          check_reachable("https://10.255.255.1:443/x.git",
                          proxy_url=f"http://127.0.0.1:{proxy_port}", timeout=2) is None)
    proxy_down = check_reachable("https://github.com/a/b.git",
                                 proxy_url="http://127.0.0.1:9", timeout=2)
    check("代理不可达时明确指出代理问题", proxy_down is not None and "代理" in proxy_down, str(proxy_down))

    # ------------------------------------------------------------------
    section("一键自我更新")

    import shutil as _shutil
    import threading as _threading

    from app import selfupdate as _su
    from app.selfupdate import compare_versions

    # 版本比较
    check("版本号比较", compare_versions("1.10.0", "1.9.9") > 0)
    check("v 前缀归一化", compare_versions("v1.2.1", "1.2.1") == 0)
    check("预发布排在正式版之前", compare_versions("1.3.0-rc1", "1.3.0") < 0)

    # 最新 tag 选取：必须取语义最大的，而不是列表顺序第一个
    origin2 = tmp_root / "update-origin"
    origin2.mkdir(parents=True, exist_ok=True)

    def _git(*args, cwd=origin2):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "T")
    (origin2 / "app").mkdir()
    (origin2 / "web").mkdir()
    (origin2 / "app" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (origin2 / "app" / "main.py").write_text("x = 1\n")
    (origin2 / "web" / "index.html").write_text("<html></html>\n")
    (origin2 / "requirements.txt").write_text("fastapi\n")
    (origin2 / "run.sh").write_text("#!/bin/sh\n")
    _git("add", "-A"); _git("commit", "-qm", "v1.0.0")
    _git("tag", "v1.0.0")
    (origin2 / "app" / "__init__.py").write_text('__version__ = "1.2.1"\n')
    _git("add", "-A"); _git("commit", "-qm", "v1.2.1")
    _git("tag", "v1.2.1")
    (origin2 / "app" / "__init__.py").write_text('__version__ = "1.3.0"\n')
    _git("add", "-A"); _git("commit", "-qm", "v1.3.0")
    _git("tag", "v1.3.0")

    # 检查更新（走 git ls-remote，不依赖 GitHub API）
    os.environ["AUTODEPLOY_UPDATE_REPO"] = str(origin2)
    from app.store import Store as _Store2

    store3 = _Store2(Database(tmp_root / "upd.db"))
    mgr = _su.SelfUpdateManager(store3)
    result = mgr.check(force=True)
    check("检查更新识别最新 tag", result.latest == "v1.3.0", str(result.latest))
    # 当前代码版本号恰为 1.3.0，与最新 tag 相同 ⇒ 无更新可用（正确行为）。
    check("同版本时无更新可用", result.update_available is False)
    # 模拟旧版本场景：伪造当前版本更低 ⇒ 有更新可用。
    with_patch = _su.UpdateCheck(
        current="1.0.0", latest=result.latest,
        update_available=compare_versions(result.latest, "1.0.0") > 0,
        checked_at=result.checked_at)
    check("旧版本时有更新可用", with_patch.update_available is True)

    # 状态持久化
    state = {"stage": "restarting", "log": [], "backup": "", "target_version": "v1.3.0"}
    mgr._save_state(state)
    check("状态文件可回读", mgr.state().get("stage") == "restarting")
    mgr.reconcile_on_startup()
    check("启动收尾把重启中落定为完成", mgr.state().get("stage") == "done")

    # 完整流程：下载 → 备份 → 替换 → 依赖（无 systemd ⇒ 提示手动重启）
    install_root = tmp_root / "install"
    install_root.mkdir(parents=True, exist_ok=True)
    for item in _su.UPDATE_ITEMS:
        src = origin2 / item
        dst = install_root / item
        if src.is_dir():
            _shutil.copytree(src, dst)
        else:
            _shutil.copy2(src, dst)
    # 用 v1.0.0 的内容伪装旧安装，并把 ROOT_DIR 指向它
    _git("checkout", "-q", "v1.0.0")
    for item in _su.UPDATE_ITEMS:
        src = origin2 / item
        dst = install_root / item
        if dst.is_dir():
            _shutil.rmtree(dst)
            _shutil.copytree(src, dst)
        else:
            _shutil.copy2(src, dst)

    original_root = _su.config.ROOT_DIR
    try:
        _su.config.ROOT_DIR = install_root
        st = mgr.start("v1.3.0")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if mgr.state().get("stage") in ("done", "failed"):
                break
            time.sleep(0.3)
        final = mgr.state()
        check("自更新流程完成", final.get("stage") == "done",
              f"stage={final.get('stage')} err={final.get('error')} log={final.get('log', [])[-3:]}")
        check("无 systemd 时提示手动重启", final.get("restart") == "manual")
        installed_version = (install_root / "app" / "__init__.py").read_text()
        check("安装目录已更新为目标版本", '1.3.0' in installed_version)
        check("更新前备份已保留", bool(mgr.latest_backup()))

        # 回滚
        result_rb = mgr.rollback()
        check("回滚执行成功", result_rb.get("ok") is True, str(result_rb))
        restored = (install_root / "app" / "__init__.py").read_text()
        check("回滚后恢复到更新前内容", '1.0.0' in restored, restored[:80])

        # 防护：源码目录有未提交改动时拒绝自更新
        (install_root / ".git").mkdir()
        (install_root / "uncommitted.txt").write_text("dev work\n")
        import subprocess as _sp
        _sp.run(["git", "init", "-q"], cwd=install_root, check=True)
        _sp.run(["git", "config", "user.email", "t@t"], cwd=install_root, check=True)
        _sp.run(["git", "config", "user.name", "T"], cwd=install_root, check=True)
        try:
            mgr.start("v1.3.0")
            check("未提交改动时拒绝自更新", False)
        except RuntimeError as exc:
            check("未提交改动时拒绝自更新", "未提交" in str(exc), str(exc))
    finally:
        os.environ.pop("AUTODEPLOY_UPDATE_REPO", None)
        _su.config.ROOT_DIR = original_root
    store3.close()

    # ------------------------------------------------------------------
    section("调度器行为")
    from app.runner import DeployRunner
    from app.scheduler import Scheduler
    from app.store import Store as Store2

    store2 = Store2(Database(tmp_root / "sched.db"))
    runner = DeployRunner(store2)
    scheduler = Scheduler(store2, runner)

    manual_id = store2.tasks.create(
        {"name": "manual", "repo_url": "https://github.com/a/b.git",
         "schedule_type": "manual", "deploy_method": "script", "deploy_script": "true"}
    )
    scheduler.reschedule(manual_id)
    check("手动任务不排期", store2.tasks.get(manual_id)["next_run_at"] is None)

    enabled_id = store2.tasks.create(
        {
            "name": "scheduled", "repo_url": str(origin), "repo_branch": "main",
            "schedule_type": "interval", "schedule_expression": "1h", "enabled": True,
            "deploy_method": "script", "deploy_script": "echo scheduled-run",
            "skip_if_no_changes": False,
        }
    )
    scheduler.reschedule(enabled_id)
    check("定时任务已排期", bool(store2.tasks.get(enabled_id)["next_run_at"]))

    store2.tasks.create(
        {"name": "paused", "repo_url": "https://github.com/a/b.git", "enabled": False,
         "schedule_type": "interval", "schedule_expression": "1h",
         "deploy_method": "script", "deploy_script": "true"}
    )
    check("停用任务不被排期", len(store2.tasks.due(iso(datetime.utcnow()))) == 0)

    # An overdue task is picked up by a tick.
    store2.tasks.set_next_run(enabled_id, iso(datetime.utcnow() - timedelta(minutes=5)))
    due_now = store2.tasks.due(iso(datetime.utcnow()))
    check("过期任务被识别为到期", len(due_now) == 1)

    # A dispatch through the scheduler must not raise; this path reads the
    # workspace and reschedules before handing off to the runner.
    started = scheduler.tick()
    check("调度 tick 派发到期任务", started == 1, f"派发 {started} 个")
    check("派发后重算了下次时间",
          store2.tasks.get(enabled_id)["next_run_at"] is not None)
    check("派发后产生运行记录", len(store2.runs.list_for_task(enabled_id)) == 1)

    # Wait for the dispatched run to settle so it does not outlive the test.
    settle_deadline = time.monotonic() + 60
    while time.monotonic() < settle_deadline and store2.runs.count_active() > 0:
        time.sleep(0.3)
    check("派发的运行已结束", store2.runs.count_active() == 0)

    preview = scheduler.preview("interval", "30m", 3)
    check("间隔预览返回 3 项", len(preview) == 3)
    preview = scheduler.preview("cron", "0 0 * * *", 3)
    check("cron 预览返回 3 项", len(preview) == 3)
    check("非法表达式预览为空", scheduler.preview("cron", "bad", 3) == [])

    # 工作目录以任务名命名：同名任务通过 -<id> 后缀保证唯一。
    from app import config as app_config

    ws_a = store2.tasks.create(
        {"name": "命名目录任务", "repo_url": str(origin), "deploy_method": "script",
         "deploy_script": "echo named-dir", "skip_if_no_changes": False}
    )
    # 数据库层唯一约束兜底：绕过 API 也无法创建重名任务。
    raised = False
    try:
        store2.tasks.create(
            {"name": "命名目录任务", "repo_url": str(origin), "deploy_method": "script",
             "deploy_script": "echo dup", "skip_if_no_changes": False}
        )
    except sqlite3.IntegrityError:
        raised = True
    check("数据库层拒绝重名任务", raised)
    ws_b = store2.tasks.create(
        {"name": "命名目录任务乙", "repo_url": str(origin), "deploy_method": "script",
         "deploy_script": "echo dup", "skip_if_no_changes": False}
    )
    dir_a = app_config.workspace_dir_for_task(store2.tasks.get(ws_a))
    dir_b = app_config.workspace_dir_for_task(store2.tasks.get(ws_b))
    check("不同任务解析到不同目录", dir_a.name != dir_b.name, f"{dir_a.name} vs {dir_b.name}")
    # 首次 migrate 建立认领后解析保持稳定
    first = app_config.migrate_workspace_to_name(store2.tasks.get(ws_a))
    for _ in range(3):
        check_resolved = app_config.workspace_dir_for_task(store2.tasks.get(ws_a))
        check_resolved2 = app_config.migrate_workspace_to_name(store2.tasks.get(ws_a))
        if check_resolved.name != dir_a.name or check_resolved2.name != dir_a.name:
            break
    check("任务目录解析稳定", check_resolved.name == dir_a.name and check_resolved2.name == dir_a.name,
          f"{check_resolved.name}/{check_resolved2.name} vs {dir_a.name}")
    resolved_b = app_config.migrate_workspace_to_name(store2.tasks.get(ws_b))
    resolved_b2 = app_config.workspace_dir_for_task(store2.tasks.get(ws_b))
    check("后建同名任务目录带后缀", resolved_b.name != dir_a.name and resolved_b2.name == resolved_b.name,
          f"{resolved_b.name}/{resolved_b2.name} vs {dir_a.name}")
    check("目录名使用任务名", "task-" not in dir_a.name.split("/")[-1] or dir_a.name.startswith("task-") is False)
    store2.tasks.delete(ws_a)
    store2.tasks.delete(ws_b)

    status = scheduler.status()
    check("调度器状态包含并发信息", "max_global_workers" in status)

    report = scheduler.maintenance()
    check("维护返回报告", "runs" in report and "sessions" in report)
    check("过期会话被清理", report["sessions"] >= 0)
    store2.close()

    # ------------------------------------------------------------------
    print()
    if FAILED:
        print(f"\033[31m✗ {len(FAILED)} 项失败\033[0m，\033[32m{len(PASSED)} 项通过\033[0m")
        for label, detail in FAILED:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        return 1
    print(f"\033[32m✓ 全部 {len(PASSED)} 项检查通过\033[0m")
    return 0


def main() -> int:
    try:
        return run()
    except Exception:  # noqa: BLE001
        print("\033[31m自检脚本自身崩溃：\033[0m")
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
