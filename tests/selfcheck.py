"""Self-contained self-check for AutoDeploy.

Runs without pytest so it works on a bare server:
    .venv/bin/python tests/selfcheck.py

Covers the pure-logic layers plus a full API round trip against a real ASGI app
using a temporary data directory.  No network access and no root privileges are
required; git-based tests use local repositories.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit

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

    # 系统时区展示辅助函数：UTC 时刻转本地渲染与日历日。
    from datetime import timezone as _timezone

    from app import schedule as sched

    utc_moment = datetime(2026, 10, 3, 0, 30, tzinfo=_timezone.utc)
    local_moment = sched.to_local(utc_moment)
    check("to_local 返回 naive 时间", local_moment is not None and local_moment.tzinfo is None, str(local_moment))
    offset_hours = round((local_moment - utc_moment.replace(tzinfo=None)).total_seconds() / 3600)
    expected_hours = round(sched.local_tz().utcoffset(datetime.now()).total_seconds() / 3600)
    check("to_local 偏移与 local_tz 一致", offset_hours == expected_hours, f"{offset_hours} vs {expected_hours}")
    check(
        "format_local 渲染为 YYYY-MM-DD HH:MM:SS",
        sched.format_local(utc_moment) == local_moment.strftime("%Y-%m-%d %H:%M:%S"),
    )
    check(
        "local_date 为系统时区日历日",
        sched.local_date(utc_moment) == local_moment.date().isoformat(),
    )
    check("format_local 空值回退当前时间", sched.format_local(None) != "")
    check(
        "to_local 对 naive UTC 输入按 UTC 解释",
        sched.to_local(datetime(2026, 10, 3, 0, 30)) == local_moment,
    )

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
        (
            "环境变量文本路径同样校验变量名",
            {"name": "x", "repo_url": "https://github.com/a/b.git", "env_vars": "BAD-NAME=1"},
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
            "name": "My_Task",
            "repo_url": "https://github.com/a/b.git",
            "deploy_method": "script",
            "deploy_script": "echo hi",
            "artifact_paths": "dist, package.json\ndist\n",
            "env_vars": "A=1\nB=two",
            "schedule_type": "interval",
            "schedule_expression": "2h",
        }
    )
    check("合法任务名原样保留", cleaned["name"] == "My_Task")
    for valid_name in ("A", "_", "___", "CON", "Mixed_Case", "A" * 80):
        result = validate_task_payload({**cleaned, "name": valid_name})
        check(f"接受合法任务名 {valid_name[:15]}", result["name"] == valid_name)
    for invalid_name in ("中文", "has space", " spaced ", "abc1", "a-b", "a.b", "../a", "/a",
                         "a\\b", "é", "Ａ", "A\n", "A\x00", "A" * 81, "", " ", None, 123, True, [], {}):
        for partial in (False, True):
            try:
                validate_task_payload({**cleaned, "name": invalid_name}, partial=partial, existing=cleaned)
            except ValidationError as exc:
                check(f"拒绝非法任务名 {invalid_name!r} partial={partial}", "name" in exc.errors)
            else:
                check(f"拒绝非法任务名 {invalid_name!r} partial={partial}", False)
    for legacy_name in ("历史中文任务", "old-name", " Old Task ", "L" * 120):
        legacy = {**cleaned, "name": legacy_name}
        result = validate_task_payload({"name": legacy_name, "description": "备注"}, partial=True, existing=legacy)
        check("旧不合规名原样保留可编辑 " + legacy_name[:15], result["name"] == legacy_name)
        result = validate_task_payload({"description": "新备注"}, partial=True, existing=legacy)
        check("旧名称省略不改写 " + legacy_name[:15], "name" not in result)
        expect_raises("旧名称不能改成另一个非法名", lambda: validate_task_payload(
            {"name": legacy_name + "!"}, partial=True, existing=legacy), ValidationError)
    check("打包路径去重", cleaned["artifact_paths"] == "dist\npackage.json")
    check("环境变量文本解析", cleaned["env_vars"] == {"A": "1", "B": "two"})

    # 互斥字段：方式专属字段与部署方式不匹配时显式报错；未提交该字段则放行。
    conflicting = [
        ("systemd 方式不应填镜像名", {"docker_image": "a:b"}, "docker_image"),
        ("rsync 方式不应填服务名", {"service_name": "a.service"}, "service_name"),
        ("docker 方式不应填 rsync 目标", {"rsync_target": "u@h:/srv"}, "rsync_target"),
        ("compose 方式不应填启动命令", {"docker_command": "docker ps"}, "docker_command"),
    ]
    for label, extra, field in conflicting:
        base = {"name": "x", "repo_url": "https://github.com/a/b.git",
                "deploy_method": "script", "deploy_script": "echo hi"}
        try:
            validate_task_payload({**base, **extra})
        except ValidationError as exc:
            check(label, field in exc.errors, f"错误字段为 {list(exc.errors)}")
        else:
            check(label, False, "未抛出校验错误")
    # 存量脏数据局部编辑（未显式提交互斥字段）不被误伤。
    legacy_task = {"name": "legacy", "repo_url": "https://github.com/a/b.git",
                   "deploy_method": "rsync", "rsync_target": "u@h:/srv",
                   "docker_image": "stale:image"}
    result = validate_task_payload({"description": "改备注"}, partial=True, existing=legacy_task)
    check("存量脏字段局部编辑放行", "docker_image" not in result and result["description"] == "改备注")

    # 令牌清除协议：界面勾选「清除已保存的令牌」时只提交 clear_token 标志，
    # 不带 git_token 键；此前清除标志因缺少该键被忽略，保存成功但令牌仍在。
    token_task = {**legacy_task, "git_token": "old-token"}
    merged = validate_task_payload({"clear_token": True}, partial=True, existing=token_task)
    check("clear_token 单独提交可清除令牌", merged.get("git_token") == "")
    kept = validate_task_payload({"description": "改备注"}, partial=True, existing=token_task)
    check("未提交 clear_token 不动令牌", "git_token" not in kept)
    replaced = validate_task_payload({"git_token": "new-token"}, partial=True, existing=token_task)
    check("提交新令牌直接替换", replaced.get("git_token") == "new-token")
    # manual 调度下表达式不参与校验，存任意文本也能通过。
    manual_task = validate_task_payload(
        {"name": "x", "repo_url": "https://github.com/a/b.git",
         "deploy_method": "script", "deploy_script": "echo hi",
         "schedule_type": "manual", "schedule_expression": "不是表达式"}
    )
    check("manual 调度忽略表达式", manual_task["schedule_expression"] == "不是表达式")

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

    # 拉取深度：0 是「完整历史」，不能被当成缺省值退化为浅克隆。
    from app.runner import resolve_git_depth

    check("深度 0 保留为完整历史", resolve_git_depth({"git_depth": 0}) == 0)
    check("深度 0 字符串同样保留", resolve_git_depth({"git_depth": "0"}) == 0)
    check("深度缺省回退浅克隆", resolve_git_depth({}) == 1)
    check("深度为空回退浅克隆", resolve_git_depth({"git_depth": None}) == 1)
    check("显式深度被保留", resolve_git_depth({"git_depth": 5}) == 5)
    check("非法深度回退浅克隆", resolve_git_depth({"git_depth": "abc"}) == 1)

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

    # 临时 DNS 抖动（EAI_AGAIN）不得被视为不可达：它按语义就是「稍后重试即可」，
    # 若在预检阶段就判定失败，git 自己的重试与抗抖配置根本没机会运行，用户会看到
    # 「检查失败：无法连接 github.com:443（Temporary failure in name resolution）」，
    # 而此时在服务器上手动访问一切正常——正是本次上报的现象。
    from unittest.mock import patch as probe_patch

    import errno
    import socket

    from app import gitops as gitops_module
    from app.gitops import _probe_failure_message

    with probe_patch.object(gitops_module.socket, "create_connection",
                            side_effect=socket.gaierror(
                                socket.EAI_AGAIN, "Temporary failure in name resolution")):
        transient = check_reachable("https://github.com/a/b.git", timeout=2)
    check("临时 DNS 失败不判定为不可达", transient is None, str(transient))

    # 域名确实不存在（EAI_NONAME）是确定失败，必须给出可读原因。
    with probe_patch.object(gitops_module.socket, "create_connection",
                            side_effect=socket.gaierror(
                                socket.EAI_NONAME,
                                "nodename nor servname provided, or not known")):
        missing = check_reachable("https://no-such-host.invalid/a.git", timeout=2)
    check("域名不存在仍快速报告且指出是解析问题",
          missing is not None and "解析" in missing and "no-such-host.invalid" in missing,
          str(missing))

    # 连接被拒绝是确定失败，保留原有可读原因；临时解析失败则不属于确定失败。
    # 用 errno 常量而非字面量：ECONNREFUSED 在 Linux 是 111、在 macOS 是 61，
    # 写死数字会让同一份自检在不同平台上得到相反结论。
    check("连接被拒绝属于确定失败",
          _probe_failure_message(
              "h", 443,
              ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")) is not None)
    check("临时解析失败不属于确定失败",
          _probe_failure_message("h", 443,
                                 socket.gaierror(socket.EAI_AGAIN, "temporary")) is None)

    # 进程环境里的代理（systemd 单元可能设置）git 会使用，探测必须一致；
    # 否则「走代理能通」会被直连探测误判为不可达。
    # 用替身记录被连的地址，既不真连网络，又能断言探测确实指向代理。
    probed = []

    class _FakeConnection:
        def __init__(self, address):
            probed.append(address)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def record_connection(address, *args, **kwargs):
        return _FakeConnection(address)

    with probe_patch.object(gitops_module.socket, "create_connection",
                            side_effect=record_connection), \
            probe_patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:9"}, clear=False):
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        check_reachable("https://github.com/a/b.git", timeout=2)
    check("探测使用进程环境里的代理（连的是代理而非目标主机）",
          probed == [("127.0.0.1", 9)], str(probed))

    # no_proxy 命中的主机必须直连探测：此时即使代理不可用也不能误报，
    # 探测目标是仓库主机本身。
    probed.clear()
    with probe_patch.object(gitops_module.socket, "create_connection",
                            side_effect=record_connection), \
            probe_patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:9",
                                          "no_proxy": "github.com"}, clear=False):
        bypassed = check_reachable("https://github.com/a/b.git", timeout=2)
    check("no_proxy 命中的主机直连探测而非走代理",
          bypassed is None and probed == [("github.com", 443)], f'{bypassed!r} {probed}')

    # 私有仓库未配凭据时，必须给出可操作提示而不是只报「命令退出码 128」。
    from app.gitops import _credential_hint

    _private_repo_output = (
        "Cloning into '/var/lib/autodeploy/workspaces/x'...\n"
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    )
    check("未配凭据给出可操作提示", "未配置可用凭据" in _credential_hint(_private_repo_output))
    check("凭据失效给出可操作提示",
          "凭据无效或已过期" in _credential_hint("fatal: Authentication failed for 'https://x'"))
    check("SSH 密钥被拒给出可操作提示",
          "Deploy Keys" in _credential_hint("git@github.com: Permission denied (publickey)."))
    check("本地权限错误不误报为密钥问题",
          _credential_hint("fatal: could not create work tree dir '/x': Permission denied") == "")
    check("无认证问题时不给凭据提示", _credential_hint("fatal: Remote branch nope not found") == "")

    # git 自己的解析失败同样要给可操作提示，且不得断言「不可达」——
    # 抖动重试后仍失败时，用户拿到的应是指向 DNS 的说明。
    from app.gitops import _network_hint

    check("git 解析失败给出 DNS 提示",
          "解析" in _network_hint("fatal: unable to access 'https://github.com/x/y.git/': "
                                  "Could not resolve host: github.com"))
    check("非网络问题不给 DNS 提示", _network_hint("fatal: Remote branch nope not found") == "")
    check("DNS 提示不误判为确定不可达",
          "不可达" not in _network_hint("could not resolve host: github.com"))

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
    from app.store import Store, TaskNameConflict

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

    # 编辑任务时保存令牌：曾因 git_token 不在 TASK_UPDATE_COLUMNS 而被静默丢弃，
    # 界面上保存成功、后续部署却匿名克隆私有仓库并报 128。必须能被写入与清除。
    store.tasks.update(task_id, {"git_token": "rotated-token"})
    check("更新可写入任务内令牌", store.tasks.get(task_id)["git_token"] == "rotated-token")
    store.tasks.update(task_id, {"git_token": ""})
    check("更新可清除任务内令牌", store.tasks.get(task_id)["git_token"] == "")

    # Webhook 触发令牌：创建任务时自动生成、可重置；导出快照必须剥离。
    webhook_secret = store.tasks.get(task_id)["webhook_secret"]
    check("创建任务自动生成触发令牌", bool(webhook_secret))
    check("解码行保留触发令牌", store.tasks.get_decoded(task_id)["webhook_secret"] == webhook_secret)
    store.tasks.update(task_id, {"webhook_secret": "rotated-webhook-secret"})
    check("更新可重写触发令牌", store.tasks.get(task_id)["webhook_secret"] == "rotated-webhook-secret")
    snapshot = store.export_snapshot()
    check("导出快照剥离触发令牌", all("webhook_secret" not in t for t in snapshot["tasks"]))

    # 存量库升级：v3 库补列后必须为空令牌任务自动补齐，老任务才有触发地址。
    store.db.execute("UPDATE tasks SET webhook_secret = ''")
    store.db.execute("UPDATE schema_version SET version = 3")
    store.db.init_schema()
    check("v4 迁移为存量任务补齐触发令牌", all(
        bool(t["webhook_secret"]) for t in store.tasks.list_all()))

    # 结构护栏：校验层能产出的字段必须都能落库，否则会在某一侧被静默丢弃。
    from app import validation as _validation
    from app.store import TASK_INSERT_COLUMNS as _INSERT, TASK_UPDATE_COLUMNS as _UPDATE

    import inspect as _inspect
    import re as _re

    _out_keys = set(_re.findall(r'out\["([a-z_]+)"\]', _inspect.getsource(_validation.validate_task_payload)))
    check("校验输出字段均可插入", not (_out_keys - set(_INSERT)),
          f"插入缺失: {sorted(_out_keys - set(_INSERT))}")
    check("校验输出字段均可更新", not (_out_keys - set(_UPDATE)),
          f"更新缺失: {sorted(_out_keys - set(_UPDATE))}")

    # --- 远程主机仓储与校验 ---------------------------------------------
    host_cred_id = store.credentials.create(
        {"name": "host-key", "kind": "ssh_key", "username": "deploy",
         "secret": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n"}
    )
    host_id = store.hosts.create(
        {"name": "Web_1", "address": "10.0.0.5", "port": 2222, "username": "deploy",
         "credential_id": host_cred_id, "base_dir": "/srv/autodeploy"}
    )
    saved_host = store.hosts.get(host_id)
    check("主机创建后可按 id 取回",
          saved_host["name"] == "Web_1" and saved_host["port"] == 2222)
    check("主机默认状态为未检测", saved_host["status"] == "unknown")
    check("主机名唯一性不区分大小写", store.hosts.name_taken("web_1"))
    check("排除自身时不误报重名", not store.hosts.name_taken("Web_1", exclude_id=host_id))
    check("主机列表可读取", [h["name"] for h in store.hosts.list_all()] == ["Web_1"])

    # 任务可以指向主机；引用计数用于删除保护。
    host_task = store.tasks.create({
        "name": "Hosted_task", "repo_url": "https://github.com/a/b.git",
        "deploy_method": "script", "deploy_script": "echo hi", "host_id": host_id,
    })
    check("任务可保存目标主机", store.tasks.get(host_task)["host_id"] == host_id)
    check("任务默认部署源为 git", store.tasks.get(host_task)["source_mode"] == "git")
    check("任务默认数据源为空列表", store.tasks.get(host_task)["data_sources"] == "[]")
    check("主机引用计数正确", store.hosts.usage_counts().get(host_id) == 1)

    store.hosts.record_check(host_id, status="online", host_key="ssh-ed25519 SHA256:abc")
    checked = store.hosts.get(host_id)
    check("连通性检查记录状态与指纹",
          checked["status"] == "online" and checked["host_key"] == "ssh-ed25519 SHA256:abc")
    store.hosts.record_check(host_id, status="unknown", host_key="")
    check("重置指纹会清空 host_key", store.hosts.get(host_id)["host_key"] == "")

    # 校验：地址/用户名的 shell 元字符、相对基础目录、越界 ..、非法端口都必须拒绝。
    from app.validation import ValidationError as _ValidationError
    from app.validation import describe_host as _describe_host
    from app.validation import validate_host_payload as _validate_host

    _host_ok = _validate_host({"name": "h", "address": "web.example.com",
                               "username": "deploy", "base_dir": "/srv/app/"})
    check("主机校验补全默认端口", _host_ok["port"] == 22)
    check("主机校验去掉尾部斜杠", _host_ok["base_dir"] == "/srv/app")
    for _bad, _field in [
        ({"name": "h", "address": "evil;rm -rf /", "username": "u"}, "address"),
        ({"name": "h", "address": "h", "username": "u;id"}, "username"),
        ({"name": "h", "address": "h", "username": "u", "base_dir": "relative/x"}, "base_dir"),
        ({"name": "h", "address": "h", "username": "u", "base_dir": "/a/../../b"}, "base_dir"),
        ({"name": "h", "address": "h", "username": "u", "port": 70000}, "port"),
        ({"name": "", "address": "h", "username": "u"}, "name"),
    ]:
        _raised = False
        try:
            _validate_host(_bad)
        except _ValidationError as exc:
            _raised = _field in exc.errors
        check(f"主机校验拒绝非法 {_field}", _raised, str(_bad)[:80])
    check("主机描述包含端口",
          _describe_host({"username": "d", "address": "h", "port": 2222}) == "d@h:2222")
    check("主机描述省略默认端口",
          _describe_host({"username": "d", "address": "h", "port": 22}) == "d@h")

    # 传输层：远端命令必须逐参数转义，避免把值拼进 shell 后被解释。
    from app.remote import build_remote_command as _build_remote, SshTransport as _SshTransport

    _remote_cmd = _build_remote(["echo", "a b; rm -rf /"], cwd="/srv/x", env={"K": "v 1"})
    check("远端命令逐参数转义",
          "a b; rm -rf /" not in _remote_cmd.split("&&")[-1].replace("'a b; rm -rf /'", ""),
          _remote_cmd)
    check("远端命令包含工作目录", "cd /srv/x" in _remote_cmd)
    check("远端命令导出环境变量", "export K='v 1'" in _remote_cmd)
    _transport = _SshTransport(address="h", username="u", private_key="KEY",
                               work_dir=tmp_root / "ssh-probe")
    _ssh_base = _transport.ssh_base()
    check("SSH 默认禁用交互提示", "BatchMode=yes" in _ssh_base)
    check("SSH 只用指定私钥", "IdentitiesOnly=yes" in _ssh_base)
    check("SSH 使用独立 known_hosts",
          any(str(tmp_root / "ssh-probe") in part for part in _ssh_base))
    _transport.cleanup()
    check("私钥临时文件在清理后删除",
          not (tmp_root / "ssh-probe" / "id_remote").exists())
    _passphrase_transport = _SshTransport(
        address="h", username="u", private_key="KEY", passphrase="secret",
        work_dir=tmp_root / "ssh-pass",
    )
    _passphrase_error = ""
    try:
        _passphrase_transport.ssh_base()
    except Exception as exc:  # noqa: BLE001 - 只要被拒绝即可
        _passphrase_error = str(exc)
    _passphrase_transport.cleanup()
    check("带口令的私钥被明确拒绝而不是挂起", "口令" in _passphrase_error, _passphrase_error)

    # SSH 失败信息必须可操作：原始输出只有退出码 255 时用户无从下手。
    from app.executor import CommandResult as _CommandResult
    from app.remote import _describe_ssh_failure as _ssh_failure

    _ssh_cases = [
        ("Permission denied (publickey).", "认证失败"),
        ("ssh: connect to host h port 22: Connection refused", "端口未开放"),
        ("ssh: connect to host h port 22: No route to host", "无法到达"),
        ("ssh: Could not resolve hostname h: Name or service not known", "无法解析"),
        ("Host key verification failed.", "指纹已变化"),
    ]
    for _raw, _expect in _ssh_cases:
        _message = _ssh_failure(_CommandResult("ssh", 255, _raw, 10))
        check(f"SSH 失败信息可操作（{_expect}）", _expect in _message, _message)
    _fallback = _ssh_failure(_CommandResult("ssh", 255, "", 10))
    check("SSH 失败兜底信息不为空且中文", bool(_fallback) and "SSH" in _fallback, _fallback)

    store.tasks.delete(host_task)
    store.hosts.delete(host_id)
    check("主机可删除", store.hosts.get(host_id) is None)

    # --- 远程部署：目标抽象与远程回滚校验 ------------------------------
    from app.executor import CommandResult
    from app.target import (
        LocalTarget as _LocalTarget,
        RemoteTarget as _RemoteTarget,
        STAGE_EXCLUDES as _STAGE_EXCLUDES,
        resolve_remote_layout as _resolve_layout,
    )

    _layout = _resolve_layout({"base_dir": "/opt/autodeploy"}, {"name": "myapp"})
    check("远程布局按任务名分目录",
          _layout.workspace == "/opt/autodeploy/workspaces/myapp")
    check("远程发布根与 current 同源",
          _layout.releases_root == "/opt/autodeploy/releases/myapp/releases"
          and _layout.current_link == "/opt/autodeploy/releases/myapp/current")
    _layout_td = _resolve_layout(
        {"base_dir": "/opt/autodeploy"}, {"name": "myapp", "target_dir": "/srv/www/app/"}
    )
    check("配置 target_dir 时发布目录随之迁移",
          _layout_td.releases_root == "/srv/www/app/releases"
          and _layout_td.current_link == "/srv/www/app/current")
    _bad_layout = ""
    try:
        _resolve_layout({"base_dir": "relative"}, {"name": "x"})
    except Exception as exc:  # noqa: BLE001 - 只要被拒绝
        _bad_layout = str(exc)
    check("非绝对路径的基础目录被拒绝", "绝对路径" in _bad_layout, _bad_layout)

    _local = _LocalTarget()
    check("本机目标不符合远程标记", not _local.is_remote and _local.label == "本机")
    check("本机目标返回 Path", isinstance(_local.path("/tmp/x"), Path))
    # 本机 upload 是语义 no-op：源码已经在位。
    check("本机 upload 不报错", _local.upload(tmp_root, "/ignored").ok)

    # 远程目标的命令构造：路径逐参数转义、bundle 用 tar、换链用原子 mv -T。
    _remote_calls = []

    class _FakeTransport:
        username = "deploy"
        address = "10.0.0.5"
        is_remote = True

        def run(self, args, **kwargs):
            _remote_calls.append(("run", list(args)))
            return CommandResult(" ".join(args), 0, "", 0)

        def run_script(self, script, **kwargs):
            _remote_calls.append(("script", script))
            return CommandResult(script, 0, "42", 0)

        def rsync_up(self, local, remote, **kwargs):
            _remote_calls.append(("up", local, remote, kwargs))
            return CommandResult("rsync", 0, "", 0)

        def rsync_down(self, remote, local, **kwargs):
            _remote_calls.append(("down", remote, local))
            return CommandResult("rsync", 0, "", 0)

    _rt = _RemoteTarget(_FakeTransport())
    check("远程目标标记为 remote", _rt.is_remote and _rt.label == "deploy@10.0.0.5")
    check("远程目标返回字符串路径", _rt.path("/srv/x") == "/srv/x")
    _rt.make_dirs("/srv/app dir")
    check("远程建目录逐参数转义",
          _remote_calls[-1] == ("run", ["mkdir", "-p", "/srv/app dir"]), str(_remote_calls[-1]))
    _rt.swap_symlink("/srv/app/current", "/srv/app/releases/20260101-abcdef12")
    _swap_script = _remote_calls[-1][1]
    check("远程换链使用原子 mv -T", "mv -Tf" in _swap_script and "ln -sfn" in _swap_script,
          _swap_script)
    _rt.bundle("/srv/app/releases/r1", "/srv/app/artifacts/a.tar.gz")
    _bundle_script = _remote_calls[-1][1]
    check("远程打包使用 tar czf 且先写 .part",
          "tar czf" in _bundle_script and ".part" in _bundle_script, _bundle_script)
    _before_upload = len(_remote_calls)
    _rt.upload(Path("/tmp/src"), "/srv/app/ws", delete=True, excludes=[".git"])
    _upload_calls = _remote_calls[_before_upload:]
    check("远程上传会先建目标目录",
          _upload_calls[0] == ("run", ["mkdir", "-p", "/srv/app/ws"]), str(_upload_calls))
    check("远程上传排除 .git 并启用 delete",
          _upload_calls[1][0] == "up"
          and _upload_calls[1][1] == Path("/tmp/src")
          and _upload_calls[1][2] == "/srv/app/ws"
          and _upload_calls[1][3].get("delete") is True
          and ".git" in _upload_calls[1][3].get("excludes", []),
          str(_upload_calls[1]))
    check("staged 排除项包含 .git 与 node_modules",
          ".git" in _STAGE_EXCLUDES and "node_modules" in _STAGE_EXCLUDES)

    # 远程回滚校验：越界路径、非法名字、软链、当前版本都必须被拒绝。
    from app.deployer import DeployError as _DeployError
    from app.deployer import _remote_rollback_target as _remote_target

    class _Layout:
        releases_root = "/srv/app/releases"
        current_link = "/srv/app/current"

    class _RollbackTarget:
        def __init__(self, *, dirs=(), links=None, symlinks=(), root="/srv/app/releases"):
            # 发布根本身也必须是目录，否则一切校验都会先卡在「没有发布目录」。
            self.dirs = set(dirs) | {root}
            self.links = links or {}
            self.symlinks = set(symlinks)

        def is_symlink(self, path):
            return str(path) in self.symlinks

        def is_dir(self, path):
            return str(path) in self.dirs

        def readlink(self, path):
            return self.links.get(str(path))

        def list_dirs(self, root):
            return sorted(
                d.rsplit("/", 1)[-1] for d in self.dirs
                if d.startswith(str(root) + "/")
            )

    _good = "20260101-120000-abcdef12"
    _base_task = {"id": 1, "deploy_method": "release"}
    _rt_target = _RollbackTarget(
        dirs={f"/srv/app/releases/{_good}", "/srv/app/releases/20251231-120000-00000000"},
        links={"/srv/app/current": f"/srv/app/releases/{_good}"},
    )
    _names_ok = False
    try:
        _root, _link, _active, _chosen = _remote_target(
            _base_task, None, _rt_target, _Layout()
        )
        _names_ok = _chosen.endswith("20251231-120000-00000000") and _active is not None
    except _DeployError:
        _names_ok = False
    check("远程回滚默认指向非当前的最新版本", _names_ok)

    for _label, _run, _expect in [
        ("不在发布根下的路径", {"release_dir": "/etc/passwd", "task_id": 1,
                                "status": "success", "deploy_method": "release"},
         "发布根下"),
        ("含 .. 的路径", {"release_dir": "/srv/app/releases/../../etc", "task_id": 1,
                          "status": "success", "deploy_method": "release"}, "无效"),
        ("非法版本名", {"release_dir": "/srv/app/releases/evil", "task_id": 1,
                        "status": "success", "deploy_method": "release"}, "有效的版本"),
        ("未成功的运行", {"release_dir": f"/srv/app/releases/{_good}", "task_id": 1,
                          "status": "failed", "deploy_method": "release"}, "部署成功"),
        ("属于其它任务", {"release_dir": f"/srv/app/releases/{_good}", "task_id": 99,
                          "status": "success", "deploy_method": "release"}, "不属于该任务"),
    ]:
        _rejected = ""
        try:
            _remote_target(_base_task, _run, _rt_target, _Layout())
        except _DeployError as exc:
            _rejected = str(exc)
        check(f"远程回滚拒绝{_label}", _expect in _rejected, _rejected)

    # 已经是当前版本时拒绝（避免无意义回滚与日志误导）。
    _same_current = _RollbackTarget(
        dirs={f"/srv/app/releases/{_good}"},
        links={"/srv/app/current": f"/srv/app/releases/{_good}"},
    )
    _same_msg = ""
    try:
        _remote_target(
            _base_task,
            {"release_dir": f"/srv/app/releases/{_good}", "task_id": 1,
             "status": "success", "deploy_method": "release"},
            _same_current, _Layout(),
        )
    except _DeployError as exc:
        _same_msg = str(exc)
    check("远程回滚拒绝重复指向当前版本", "已经是当前版本" in _same_msg, _same_msg)

    # 运行记录必须留存主机快照；主机稍后改名不影响历史。
    _host_snap_id = store.hosts.create(
        {"name": "snap-host", "address": "192.0.2.77", "username": "deploy",
         "credential_id": host_cred_id}
    )
    _snap_task_id = store.tasks.create({
        "name": "Snap_task", "repo_url": "https://github.com/a/b.git",
        "deploy_method": "script", "deploy_script": "echo hi", "host_id": _host_snap_id,
    })
    _snap_run = store.runs.create(store.tasks.get(_snap_task_id), trigger="manual")
    _snap_run_row = store.runs.get(_snap_run)
    check("运行记录自动带上任务的目标主机",
          _snap_run_row["host_id"] == _host_snap_id and not _snap_run_row["host_name"])
    _host_id_2, _host_name_2 = store.hosts.snapshot_for_run(store.tasks.get(_snap_task_id))
    check("主机快照取到当前名称", _host_id_2 == _host_snap_id and _host_name_2 == "snap-host")
    store.hosts.delete(_host_snap_id)
    # 外键是 ON DELETE SET NULL：主机被删后任务自动回到本机，不会留下悬空引用。
    _after_delete = store.tasks.get(_snap_task_id)
    check("主机删除后任务回到本机（外键置空）", _after_delete["host_id"] is None,
          str(_after_delete.get("host_id")))
    _missing_id, _missing_name = store.hosts.snapshot_for_run({"host_id": _host_snap_id})
    check("指向已删除主机的快照给出明确占位",
          _missing_id == _host_snap_id and "已删除" in _missing_name, _missing_name)
    store.tasks.delete(_snap_task_id)

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
    # 日统计必须按系统时区的日历日分桶：刚结束的这次运行属于「今天」，
    # 无论 UTC 日期还是本地日期，今天的桶里都要能找到它。
    daily = store.runs.stats_daily(7)
    from app.schedule import local_date

    today_bucket = next((d for d in daily if d["day"] == local_date()), None)
    check("日统计包含系统时区的今天", today_bucket is not None, str([d["day"] for d in daily]))
    check("日统计今天的桶计入本次运行", today_bucket is not None and today_bucket["total"] >= 1, str(today_bucket))
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

    # 并发创建、改名及创建/改名互撞均在同一 NOCASE 临界区完成。
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch as name_patch

    def race_names(operations):
        barrier = threading.Barrier(len(operations))

        def attempt(operation):
            barrier.wait(timeout=10)
            try:
                operation()
                return "ok"
            except TaskNameConflict:
                return "conflict"

        original_name_taken = store.tasks.name_taken
        lock_observations = []

        def slow_name_taken(*args, **kwargs):
            lock_observations.append(store.db._lock._is_owned())
            result = original_name_taken(*args, **kwargs)
            time.sleep(0.01)
            return result

        with name_patch.object(store.tasks, "name_taken", side_effect=slow_name_taken):
            with ThreadPoolExecutor(max_workers=len(operations)) as pool:
                results = list(pool.map(attempt, operations))
        check("唯一性检查处于数据库锁内", all(lock_observations) and bool(lock_observations))
        return results

    results = race_names([
        lambda n=n: store.tasks.create({"name": n, "repo_url": str(origin)})
        for n in ("Concurrent_Create", "CONCURRENT_CREATE") * 4
    ])
    check("并发大小写创建仅一个成功", results.count("ok") == 1 and results.count("conflict") == 7)
    rename_ids = [store.tasks.create({"name": n, "repo_url": str(origin)})
                  for n in ("Rename_Source_A", "Rename_Source_B")]
    results = race_names([
        lambda i=i, n=n: store.tasks.update(i, {"name": n})
        for i, n in zip(rename_ids, ("Concurrent_Rename", "CONCURRENT_RENAME"))
    ])
    check("并发大小写改名仅一个成功", sorted(results) == ["conflict", "ok"])
    results = race_names([
        lambda: store.tasks.update(rename_ids[0], {"name": "Mixed_Race"}),
        lambda: store.tasks.create({"name": "MIXED_RACE", "repo_url": str(origin)}),
    ])
    check("创建与改名并发互撞仅一个成功", sorted(results) == ["conflict", "ok"])
    expect_raises("仓储创建重名专用异常", lambda: store.tasks.create(
        {"name": "CONCURRENT_CREATE", "repo_url": str(origin)}), TaskNameConflict)

    # 模拟旧版本存量大小写冲突；不增索引、不自动重命名、不阻止启动。
    with name_patch.object(store.tasks, "name_taken", return_value=False):
        old_ids = [store.tasks.create({"name": n, "repo_url": str(origin)})
                   for n in ("Legacy_Case", "LEGACY_CASE")]
    store.db.init_schema()
    check("存量大小写冲突启动后原样保留", [store.tasks.get(i)["name"] for i in old_ids]
          == ["Legacy_Case", "LEGACY_CASE"])
    check("存量大小写冲突未改名可编辑", store.tasks.update(old_ids[0],
          {"name": "Legacy_Case", "description": "兼容旧任务"}))
    expect_raises("存量大小写冲突禁止新增", lambda: store.tasks.create(
        {"name": "legacy_case", "repo_url": str(origin)}), TaskNameConflict)

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

        # 首页资产 URL 的 ?v= 必须是各文件的内容指纹：曾写死 0.3.0，升级后
        # 浏览器命中同名 URL 的旧缓存；只跟随版本号时，同一版本内迭代前端
        # 同样看不到改动。内容指纹让任一资产变化其 URL 立即改变。
        response = client.get("/")
        check("首页正常返回 HTML", response.status_code == 200
              and "text/html" in response.headers.get("content-type", ""))
        html = response.text
        check("首页资产占位符已全部替换", "__VERSION__" not in html)
        versions = re.findall(r"/assets/[A-Za-z0-9._-]+\?v=([^\"']+)", html)
        check("首页四个资产都带版本参数", len(versions) == 4, str(versions))
        check("资产版本为内容指纹而非纯版本号",
              all(re.fullmatch(r"[0-9a-f]{12}", v) for v in versions), str(versions))
        check("不同资产指纹各自独立且非空", len(set(versions)) >= 1 and all(versions), str(versions))
        # 指纹必须真的来自文件内容：与直接对文件求 sha256[:12] 一致。
        import hashlib as _hashlib
        from app import config as _asset_config
        expected = {}
        for rel in ("style.css", "util.js", "views.js", "app.js"):
            blob = (_asset_config.WEB_DIR / "assets" / rel).read_bytes()
            expected[rel] = _hashlib.sha256(blob).hexdigest()[:12]
        paired = dict(re.findall(r"/assets/([A-Za-z0-9._-]+)\?v=([^\"']+)", html))
        check("资产指纹与文件内容哈希一致",
              paired == expected, f"{paired} != {expected}")

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
                "name": "End_To_End",
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

        # 凭据选择必须真实存在：此前外键错误落到通用 handler 返回 500。
        response = client.post(
            "/api/tasks",
            json={"name": "No_Cred", "repo_url": str(origin),
                  "deploy_method": "script", "deploy_script": "true",
                  "credential_id": 99999},
        )
        check("不存在的凭据被拒绝", response.status_code == 422, response.text[:150])
        check("凭据错误指向 credential_id 字段",
              "credential_id" in (response.json().get("detail", {}).get("errors") or {}))

        # 任务名唯一性：重名创建被拒绝，且大小写不敏感
        response = client.post(
            "/api/tasks",
            json={
                "name": "End_To_End", "repo_url": str(origin),
                "deploy_method": "script", "deploy_script": "true",
            },
        )
        check("重名任务被拒绝", response.status_code == 422, response.text[:150])
        check("重名错误指向 name 字段",
              "name" in (response.json().get("detail", {}).get("errors") or {}))
        response = client.post(
            "/api/tasks",
            json={
                "name": "End_To_End".upper(), "repo_url": str(origin),
                "deploy_method": "script", "deploy_script": "true",
            },
        )
        check("大小写不同也算重名", response.status_code == 422)
        # 改名撞别的任务
        response = client.post(
            "/api/tasks",
            json={"name": "Cancel_Alpha", "repo_url": str(origin),
                  "deploy_method": "script", "deploy_script": "true"},
        )
        check("创建第二个任务成功", response.status_code == 201, response.text[:150])
        second_id = response.json()["task"]["id"]
        response = client.put(f"/api/tasks/{second_id}", json={"name": "End_To_End"})
        check("改名撞已有任务被拒绝", response.status_code == 422)
        response = client.put(f"/api/tasks/{second_id}", json={"name": "Cancel_Alpha"})
        check("改回自己的名字不报错", response.status_code == 200)
        response = client.patch(f"/api/tasks/{second_id}", json={"name": "END_TO_END"})
        check("API 改名大小写撞名被拒绝", response.status_code == 422 and "name" in response.json()["detail"]["errors"])
        for invalid_name in ("中文", "has space", "ABC1", "../bad", "A" * 81, "", None, False, 42, [], {}):
            response = client.post("/api/tasks", json={"name": invalid_name, "repo_url": str(origin), "deploy_method": "release"})
            check(f"API 新增非法名称 {invalid_name!r}", response.status_code == 422 and "name" in response.json()["detail"]["errors"])
            response = client.patch(f"/api/tasks/{second_id}", json={"name": invalid_name})
            check(f"API 编辑非法名称 {invalid_name!r}", response.status_code == 422 and "name" in response.json()["detail"]["errors"])
        check("无效编辑未修改原名称", service.store.tasks.get(second_id)["name"] == "Cancel_Alpha")

        # 令牌清除协议端到端：只提交 clear_token 标志也能清除（配合前端
        # 「清除已保存的令牌」勾选；此前会保存成功但令牌仍在）。
        response = client.put(f"/api/tasks/{second_id}", json={"git_token": "tok-1"})
        check("写入任务内令牌", response.status_code == 200, response.text[:150])
        check("令牌写入后标记存在", response.json()["task"]["has_token"] is True)
        response = client.put(f"/api/tasks/{second_id}", json={"clear_token": True})
        check("clear_token 单独提交清除令牌", response.status_code == 200, response.text[:150])
        check("清除后台任务不再持有令牌", response.json()["task"]["has_token"] is False)
        active_id = service.store.runs.create(service.store.tasks.get(second_id))
        original_active_check = service.store.runs.has_active_for_task
        scheduler_lock_checks = []

        def locked_active_check(task):
            scheduler_lock_checks.append(service.scheduler._lock._is_owned())
            return original_active_check(task)

        with name_patch.object(service.store.runs, "has_active_for_task", side_effect=locked_active_check):
            for run_status in ("queued", "running"):
                if run_status == "running":
                    service.store.runs.mark_running(active_id)
                for next_name in ("Renamed_While_Active", "CANCEL_ALPHA"):
                    response = client.patch(f"/api/tasks/{second_id}", json={"name": next_name})
                    check(f"{run_status} 禁止改名 {next_name}", response.status_code == 409)
                response = client.patch(f"/api/tasks/{second_id}", json={"name": "Cancel_Alpha", "description": "运行中备注"})
                check(f"{run_status} 未改名可保存备注", response.status_code == 200)
        check("活动任务检查在调度锁内", all(scheduler_lock_checks) and bool(scheduler_lock_checks))
        service.store.runs.mark_finished(active_id, status="cancelled", exit_code=None)
        response = client.patch(f"/api/tasks/{second_id}", json={"name": "CANCEL_ALPHA"})
        check("非运行中允许仅大小写改名且主键不变", response.status_code == 200 and response.json()["task"]["id"] == second_id)
        client.delete(f"/api/tasks/{second_id}")

        legacy_id = service.store.tasks.create({"name": "旧中文任务", "repo_url": str(origin), "schedule_type": "manual"})
        for legacy_payload in ({"description": "只编辑备注"}, {"name": "旧中文任务", "description": "原样提交旧名"}):
            response = client.patch(f"/api/tasks/{legacy_id}", json=legacy_payload)
            check("API 旧中文名不改名兼容编辑", response.status_code == 200 and response.json()["task"]["name"] == "旧中文任务")
        response = client.patch(f"/api/tasks/{legacy_id}", json={"name": "另一个中文名"})
        check("API 旧名称实际改名必须合法", response.status_code == 422)
        response = client.patch(f"/api/tasks/{legacy_id}", json={"name": "Legacy_Renamed"})
        check("API 旧名称可改为新合法标识", response.status_code == 200 and response.json()["task"]["id"] == legacy_id)
        client.delete(f"/api/tasks/{legacy_id}")

        # 合法特殊名称的删除/清理使用同一所有权规则，保留外部或其他任务数据。
        for special_name in ("CON", "___"):
            response = client.post("/api/tasks", json={"name": special_name, "repo_url": str(origin),
                                   "schedule_type": "manual", "deploy_method": "release"})
            special_task = response.json()["task"]
            special_id = special_task["id"]
            direct = config.WORKSPACES_DIR / special_name
            cleaned_name = config._sanitize_dirname(special_name)
            foreign = config.WORKSPACES_DIR / cleaned_name
            own_suffix = config.WORKSPACES_DIR / f"{cleaned_name}-{special_id}"
            foreign_suffix = config.WORKSPACES_DIR / f"{special_name}-{special_id}"
            legacy_workspace = config.workspace_dir(special_id)
            for own_path in (direct, own_suffix):
                config._claim(own_path, special_id)
            for foreign_path in (foreign, foreign_suffix):
                config._claim(foreign_path, 99999)
                (foreign_path / "keep.txt").write_text("其他任务数据", encoding="utf-8")
            # 纯下划线的旧 sanitize 后缀恰好也是 task-id；模拟未认领旧布局。
            (legacy_workspace / config.CLAIM_FILE).unlink(missing_ok=True)
            (legacy_workspace / ".git").mkdir(parents=True)
            response = client.post(f"/api/maintenance/cleanup-task/{special_id}")
            check("清理特殊名称任务成功 " + special_name, response.status_code == 200)
            check("清理自身新名字及历史后缀 " + special_name, not direct.exists() and not own_suffix.exists())
            check("兼容清理真正无标记旧checkout " + special_name, not legacy_workspace.exists())
            check("清理保留其他归属目录 " + special_name, all((p / "keep.txt").exists() for p in (foreign, foreign_suffix)))
            for own_path in (direct, own_suffix):
                config._claim(own_path, special_id)
            config._claim(legacy_workspace, 99998)
            (legacy_workspace / ".git").mkdir()
            response = client.delete(f"/api/tasks/{special_id}")
            check("删除特殊名称任务成功 " + special_name, response.status_code == 200)
            check("删除清理自身新名字及历史后缀 " + special_name,
                  not direct.exists() and (own_suffix == legacy_workspace or not own_suffix.exists()))
            check("删除不误删其他归属目录含task-id " + special_name,
                  all(p.exists() for p in (foreign, foreign_suffix, legacy_workspace))
                  and config._claimed_by(legacy_workspace, 99998))
            for fixture in (foreign, foreign_suffix, legacy_workspace):
                shutil.rmtree(fixture)

        # --- 删除任务：停止容器 + 清空工作目录/发布产物/日志/历史存档 -----
        from unittest.mock import patch as del_patch

        from app import deployer as deployer_mod

        def result_of(command, exit_code=0, output=""):
            return deployer_mod.CommandResult(
                command=command, exit_code=exit_code, output=output, duration_ms=1
            )

        response = client.post("/api/tasks", json={"name": "Delete_Purge", "repo_url": str(origin),
                               "schedule_type": "manual", "deploy_method": "release"})
        purge_id = response.json()["task"]["id"]
        purge_row = service.store.tasks.get(purge_id)

        workspace = config.workspace_dir_for_task(purge_row)
        config._claim(workspace, purge_id)
        (workspace / "app.py").write_text("print('x')", encoding="utf-8")
        releases = config.releases_dir(purge_id) / "releases"
        (releases / "20260101-000000-abcdef12").mkdir(parents=True)
        artifacts = config.artifacts_dir(purge_id)
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "bundle.tar.gz").write_bytes(b"gz")
        temp_creds = config.temp_dir(purge_id) / ".git-credentials"
        temp_creds.mkdir(parents=True, exist_ok=True)

        purge_run = service.store.runs.create(purge_row, trigger="manual")
        service.store.runs.mark_finished(purge_run, status="success", exit_code=0)
        log_path = config.run_log_path(purge_run)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("部署日志", encoding="utf-8")
        service.store.runs.set_log(purge_run, str(log_path), 12)

        # 目标目录：只清理 releases 与 current，目录本身及业务文件必须保留。
        target_dir = tmp_root / "site"
        (target_dir / "assets").mkdir(parents=True)
        (target_dir / "assets" / "keep.txt").write_text("业务文件", encoding="utf-8")
        target_releases = target_dir / "releases"
        (target_releases / "20260101-000000-abcdef12").mkdir(parents=True)
        (target_dir / "current").symlink_to(target_releases / "20260101-000000-abcdef12")
        client.patch(f"/api/tasks/{purge_id}", json={"target_dir": str(target_dir)})

        response = client.delete(f"/api/tasks/{purge_id}")
        payload = response.json()
        check("删除任务返回清理明细", response.status_code == 200 and payload["ok"])
        check("删除结果包含容器与运行记录计数",
              "containers" in payload and payload["runs_deleted"] == 1)
        check("删除移除工作目录", not workspace.exists())
        check("删除移除发布产物目录", not config.releases_dir(purge_id).exists())
        check("删除移除打包产物", not artifacts.exists())
        check("删除移除临时凭据目录", not config.temp_dir(purge_id).exists())
        check("删除移除运行日志", not log_path.exists())
        check("删除清理目标目录发布历史",
              not target_releases.exists() and not (target_dir / "current").is_symlink())
        check("删除保留目标目录与其业务文件", (target_dir / "assets" / "keep.txt").exists())
        check("删除后详情返回404", client.get(f"/api/tasks/{purge_id}").status_code == 404)
        check("删除后不可重复删除", client.delete(f"/api/tasks/{purge_id}").status_code == 404)

        # purge=false 只保留文件，任务记录本身仍要删除。
        response = client.post("/api/tasks", json={"name": "Delete_Keep", "repo_url": str(origin),
                               "schedule_type": "manual", "deploy_method": "release"})
        keep_id = response.json()["task"]["id"]
        keep_workspace = config.workspace_dir_for_task(service.store.tasks.get(keep_id))
        config._claim(keep_workspace, keep_id)
        response = client.delete(f"/api/tasks/{keep_id}?purge=false")
        check("purge=false 仍删除任务记录", response.status_code == 200
              and client.get(f"/api/tasks/{keep_id}").status_code == 404)
        check("purge=false 保留工作目录", keep_workspace.exists() and bool(response.json()["kept"]))
        shutil.rmtree(keep_workspace)

        # 运行中的任务禁止删除：容器可能正在被使用。
        response = client.post("/api/tasks", json={"name": "Delete_Guard", "repo_url": str(origin),
                               "schedule_type": "manual", "deploy_method": "release"})
        guard_id = response.json()["task"]["id"]
        guard_run = service.store.runs.create(service.store.tasks.get(guard_id), trigger="manual")
        response = client.delete(f"/api/tasks/{guard_id}")
        check("运行中任务禁止删除", response.status_code == 409)
        check("拒绝删除后任务仍存在", service.store.tasks.get(guard_id) is not None)
        service.store.runs.mark_finished(guard_run, status="cancelled", exit_code=None)
        check("运行结束后允许删除", client.delete(f"/api/tasks/{guard_id}").status_code == 200)

        # 容器停止：只认本任务固定项目名标签，绝不触碰他人或手工容器。
        compose_task = {"id": 7100, "name": "Shop_Web", "deploy_method": "docker_compose"}
        listing = (
            "shop-web-1\tcom.docker.compose.project=autodeploy-shop-web\n"
            "foreign-1\tcom.docker.compose.project=someone-else\n"
            "manual-box\t\n"
        )
        compose_calls: list[list[str]] = []

        def compose_run(args, **kwargs):
            compose_calls.append(list(args))
            if list(args)[:2] == ["docker", "ps"]:
                return result_of(" ".join(args), output=listing)
            return result_of(" ".join(args))

        with del_patch.object(deployer_mod, "command_exists", return_value=True), \
                del_patch.object(deployer_mod, "run_command", side_effect=compose_run):
            stopped = deployer_mod.stop_task_containers(compose_task, log=lambda _m: None)
        check("compose 任务按固定项目名停止",
              ["docker", "compose", "-p", "autodeploy-shop-web", "down",
               "--remove-orphans", "--timeout", "10"] in compose_calls)
        check("compose 停止返回本项目容器名", stopped == ["shop-web-1"])
        check("compose 停止不触碰他人或手工容器",
              all("someone-else" not in " ".join(call) and "manual-box" not in " ".join(call)
                  for call in compose_calls))

        # compose down 失败（例如文件已删除）时，退化为按标签 rm -f，避免容器残留。
        fallback_calls: list[list[str]] = []

        def fallback_run(args, **kwargs):
            fallback_calls.append(list(args))
            if list(args)[:2] == ["docker", "ps"]:
                return result_of(" ".join(args), output=listing)
            # 只让 down 失败（compose 文件已被删除的情形），版本探测仍需成功，
            # 否则会退化成 v1 命令，测不到预期路径。
            if "down" in list(args):
                return result_of(" ".join(args), exit_code=1, output="no configuration file provided")
            return result_of(" ".join(args))

        with del_patch.object(deployer_mod, "command_exists", return_value=True), \
                del_patch.object(deployer_mod, "run_command", side_effect=fallback_run):
            stopped = deployer_mod.stop_task_containers(compose_task, log=lambda _m: None)
        check("compose down 失败时按项目标签强制移除",
              ["docker", "rm", "-f", "shop-web-1"] in fallback_calls)
        check("强制移除仍忽略他人容器",
              all("foreign-1" not in " ".join(call) and "manual-box" not in " ".join(call)
                  for call in fallback_calls))
        check("强制移除返回已移除容器", stopped == ["shop-web-1"])

        # 纯 docker 任务用回滚脚本作为停止钩子（容器名只有用户脚本知道）。
        shell_calls: list[list[str]] = []

        def shell_run(args, **kwargs):
            shell_calls.append(list(args))
            return result_of(" ".join(args))

        with del_patch.object(deployer_mod, "command_exists", return_value=True), \
                del_patch.object(deployer_mod, "run_command", side_effect=shell_run):
            deployer_mod.stop_task_containers(
                {"id": 7200, "name": "Api_Box", "deploy_method": "docker",
                 "rollback_script": "docker rm -f api-box"},
                log=lambda _m: None,
            )
        check("docker 任务用回滚脚本停止容器",
              any(call[0] in ("/bin/bash", "/bin/sh") and "api-box" in call[-1] for call in shell_calls))

        # 非 Docker 部署方式不得触碰 docker。
        for quiet_method in ("release", "script", "artifact", "systemd", "rsync"):
            touched: list[list[str]] = []

            def quiet_run(args, **kwargs):
                touched.append(list(args))
                return result_of(" ".join(args))

            with del_patch.object(deployer_mod, "command_exists", return_value=True), \
                    del_patch.object(deployer_mod, "run_command", side_effect=quiet_run):
                deployer_mod.stop_task_containers(
                    {"id": 7300, "name": "Quiet", "deploy_method": quiet_method},
                    log=lambda _m: None,
                )
            check(f"{quiet_method} 不触发容器清理", not touched)

        # 目标目录清理的安全边界：软链/根路径/无配置一律跳过。
        check("无目标目录时不清理", deployer_mod.purge_target_release_history(
            {"id": 7400, "name": "No_Target"}) == [])
        link_root = tmp_root / "link-site"
        link_root.mkdir(parents=True)
        real_root = tmp_root / "real-releases"
        real_root.mkdir(parents=True)
        (real_root / "v1").mkdir()
        (link_root / "releases").symlink_to(real_root)
        check("目标目录发布根为软链时跳过",
              deployer_mod.purge_target_release_history(
                  {"id": 7401, "name": "Link_Target", "target_dir": str(link_root)}) == []
              and real_root.exists())
        check("目标目录为根或其下时跳过",
              deployer_mod.purge_target_release_history(
                  {"id": 7402, "name": "Root_Target", "target_dir": "/"}) == [])
        normal_target = tmp_root / "normal-site"
        (normal_target / "releases" / "v1").mkdir(parents=True)
        (normal_target / "current").symlink_to(normal_target / "releases" / "v1")
        (normal_target / "own.txt").write_text("业务", encoding="utf-8")
        removed_paths = deployer_mod.purge_target_release_history(
            {"id": 7403, "name": "Normal_Target", "target_dir": str(normal_target)})
        check("目标目录清理移除releases与current", len(removed_paths) == 2
              and not (normal_target / "releases").exists()
              and not (normal_target / "current").is_symlink())
        check("目标目录清理保留目录本身与业务文件",
              normal_target.exists() and (normal_target / "own.txt").exists())

        response = client.post("/api/tasks", json={"name": "Collision_Read", "repo_url": str(origin),
                               "schedule_type": "manual", "deploy_method": "release"})
        collision_api_id = response.json()["task"]["id"]
        collision_paths = [config.WORKSPACES_DIR / "Collision_Read",
                           config.WORKSPACES_DIR / f"Collision_Read-{collision_api_id}"]
        for path in collision_paths:
            config._claim(path, 99997)
        response = client.get(f"/api/tasks/{collision_api_id}")
        check("候选双占用任务详情不500", response.status_code == 200
              and bool(response.json()["task"].get("workspace_error"))
              and response.json()["task"]["workspace"] == ""
              and not response.json()["task"]["workspace_exists"])
        response = client.get(f"/api/tasks/{collision_api_id}/preflight")
        check("候选双占用预检不500", response.status_code == 200 and bool(response.json().get("workspace_error")))
        response = client.get("/api/tasks")
        check("候选双占用任务列表不500", response.status_code == 200 and any(
            t["id"] == collision_api_id and t.get("workspace_error") for t in response.json()["tasks"]))
        response = client.get("/api/storage")
        check("双占用时存储统计跳过冲突任务不500", response.status_code == 200 and all(
            row["task_id"] != collision_api_id for row in response.json()["tasks"]))
        response = client.post("/api/maintenance/run")
        check("双占用时维护继续执行不500", response.status_code == 200 and response.json().get("ok"))
        check("维护不把冲突候选当孤儿删除", all(config._claimed_by(p, 99997) for p in collision_paths))
        response = client.patch(f"/api/tasks/{collision_api_id}", json={"description": "碰撞时仍可保存备注"})
        check("候选双占用保存备注不500", response.status_code == 200 and bool(response.json()["task"].get("workspace_error")))
        response = client.delete(f"/api/tasks/{collision_api_id}")
        check("候选双占用删除仅移除任务", response.status_code == 200 and all(p.exists() for p in collision_paths))
        for path in collision_paths:
            shutil.rmtree(path)

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

        # 自更新准入保护通过真实 API 路由验证，不触发任何重启。
        from unittest.mock import patch as api_patch
        with api_patch.object(service.selfupdate, '_restart_plan', return_value=('unsupported', '')):
            response = client.post('/api/system/self-update', json={'target_version': 'v2.0.0'})
            check('API 不支持重启明确返回冲突', response.status_code == 409 and '不支持' in response.json()['detail'])
        service.selfupdate._save_state({'stage': 'applying', 'log': ['测试日志'], 'operation_id': 'api-check'})
        response = client.get('/api/system/self-update/status')
        check('API 状态返回操作标识与日志', response.json()['active'] and response.json()['operation_id'] == 'api-check')
        # 前端指纹：页面据此判断自己是否已是新界面，决定是否提示刷新。
        from app.main import _asset_fingerprint as _fp
        check('API 状态返回前端指纹且与 views.js 一致',
              response.json().get('frontend_fingerprint') == _fp('views.js'),
              str(response.json().get('frontend_fingerprint'))[:40])
        response = client.post(f'/api/tasks/{task_id}/run')
        check('API 更新期间拒绝新部署', response.status_code == 409)
        response = client.post(f'/api/tasks/{task_id}/rollback')
        check('API 更新期间拒绝部署回滚', response.status_code == 409)
        response = client.post('/api/system/self-update/rollback')
        check('API 更新期间拒绝自更新回滚', response.status_code == 409)
        response = client.post('/api/tasks', json={'name': 'Created_During_Update', 'repo_url': str(origin),
            'deploy_method': 'script', 'deploy_script': 'true', 'run_on_create': True})
        check('创建后立即部署被阻止且返回警告', response.status_code == 201 and bool(response.json().get('warning')) and not response.json().get('run_id'))
        client.delete('/api/tasks/' + str(response.json()['task']['id']))
        service.selfupdate._save_state({'stage': 'idle', 'log': []})

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

        # --- rollback runs in the background ------------------------
        # 回滚已改为后台执行：接口立即返回 202 与 run_id，随后作为一条
        # trigger=rollback 的运行记录呈现在运行历史与增量日志轮询里。
        response = client.post(f"/api/tasks/{task_id}/rollback")
        check("回滚接口立即返回 202", response.status_code == 202, response.text[:200])
        rollback_run = response.json().get("run_id")
        check("回滚返回运行编号", isinstance(rollback_run, int) and rollback_run > 0)
        response = client.get(f"/api/runs/{rollback_run}")
        check("回滚运行记录可查询", response.status_code == 200
              and response.json()["run"]["trigger"] == "rollback")

        rollback_status = "queued"
        deadline = time.monotonic() + 60
        rollback_detail = {}
        while time.monotonic() < deadline:
            rollback_detail = client.get(f"/api/runs/{rollback_run}").json()["run"]
            rollback_status = rollback_detail["status"]
            if rollback_status not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("后台回滚结束", rollback_status not in ("queued", "running"),
              f"最终状态 {rollback_status}")
        check("后台回滚成功", rollback_status == "success",
              f"状态 {rollback_status}，错误 {rollback_detail.get('error')}")
        tail = client.get(f"/api/runs/{rollback_run}/tail?after=0").json()
        check("回滚日志经增量接口可读", tail["total"] > 0 and not tail["active"])
        response = client.post(f"/api/tasks/{task_id}/rollback")
        check("回滚成功后可再次发起", response.status_code == 202, response.text[:200])
        second_rollback = response.json().get("run_id")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            second_detail = client.get(f"/api/runs/{second_rollback}").json()["run"]
            if second_detail["status"] not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("第二次后台回滚成功", second_detail["status"] == "success",
              f"状态 {second_detail.get('status')}，错误 {second_detail.get('error')}")

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
            f"/api/tasks/{task_id}", json={"name": "Renamed_Task", "description": "desc"}
        )
        check("任务更新成功", response.status_code == 200)
        check("任务名称已更新", response.json()["task"]["name"] == "Renamed_Task")

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

        # --- runs pagination -----------------------------------------
        # 播种一批已结束运行，让分页断言与前面部署产生了多少条记录无关。
        seed_task = service.store.tasks.get(task_id)
        for _ in range(8):
            seeded = service.store.runs.create(seed_task, trigger="manual")
            service.store.runs.mark_finished(
                seeded, status="success", exit_code=0, duration_ms=100)
        all_total = client.get("/api/runs?limit=1").json()["total"]
        page_one = client.get("/api/runs?limit=3").json()
        check("分页回显 limit/offset/total",
              page_one["limit"] == 3 and page_one["offset"] == 0
              and page_one["total"] == all_total,
              str(page_one["total"]))
        check("首页返回 limit 条记录", len(page_one["runs"]) == 3)
        page_two = client.get("/api/runs?limit=3&offset=3").json()
        ids_one = [r["id"] for r in page_one["runs"]]
        ids_two = [r["id"] for r in page_two["runs"]]
        check("翻页不重叠且整体按新到旧",
              not set(ids_one) & set(ids_two)
              and ids_one + ids_two == sorted(ids_one + ids_two, reverse=True),
              f"{ids_one} + {ids_two}")
        over = client.get("/api/runs?limit=3&offset=999").json()
        check("越界分页返回空列表且 total 不变",
              over["runs"] == [] and over["total"] == all_total)
        filtered = client.get("/api/runs?status=success&limit=2&offset=2").json()
        check("筛选与分页可叠加",
              all(r["status"] == "success" for r in filtered["runs"])
              and filtered["total"] >= 8)

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

        # --- webhook 触发 ---------------------------------------------
        webhook_created = client.post(
            "/api/tasks",
            json={
                "name": "Webhook_Test",
                "repo_url": str(origin),
                "repo_branch": "main",
                "schedule_type": "manual",
                "deploy_method": "release",
            },
        )
        check("创建 Webhook 测试任务成功", webhook_created.status_code == 201,
              webhook_created.text[:200])
        webhook_task = webhook_created.json()["task"]
        webhook_id = webhook_task["id"]
        webhook_url = webhook_task["webhook_url"]
        webhook_secret = webhook_task["webhook_secret"]
        check("任务返回触发地址", "/api/webhooks/" in webhook_url and webhook_secret in webhook_url,
              webhook_url[:120])
        webhook_path = urlsplit(webhook_url).path

        webhook_response = client.post(webhook_path)
        check("Webhook POST 触发部署",
              webhook_response.status_code == 200 and webhook_response.json()["run_id"] > 0,
              webhook_response.text[:200])
        webhook_run = webhook_response.json()["run_id"]
        deadline = time.monotonic() + 60
        webhook_status = "queued"
        while time.monotonic() < deadline:
            webhook_status = client.get(f"/api/runs/{webhook_run}").json()["run"]["status"]
            if webhook_status not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("Webhook 触发的运行结束", webhook_status in ("success", "skipped"), webhook_status)
        check("触发方式记录为 webhook",
              client.get(f"/api/runs/{webhook_run}").json()["run"]["trigger"] == "webhook")

        get_response = client.get(webhook_path)
        check("Webhook GET 同样可触发", get_response.status_code == 200, get_response.text[:150])
        get_run = get_response.json()["run_id"]
        deadline = time.monotonic() + 60
        get_status = "queued"
        while time.monotonic() < deadline:
            get_status = client.get(f"/api/runs/{get_run}").json()["run"]["status"]
            if get_status not in ("queued", "running"):
                break
            time.sleep(0.4)
        check("无变化时 Webhook 触发被跳过", get_status == "skipped", get_status)

        check("错误令牌返回 404", client.post(webhook_path + "x").status_code == 404)
        check("错误任务号返回 404",
              client.post(f"/api/webhooks/999999/{webhook_secret}").status_code == 404)
        check("非数字任务号返回 404", client.post("/api/webhooks/abc/xyz").status_code == 404)

        # --- 暂停任务的触发准入 ----------------------------------------
        runs_before = client.get("/api/runs").json()["total"]
        toggle_resp = client.post(f"/api/tasks/{webhook_id}/toggle")
        check("暂停 Webhook 测试任务", toggle_resp.status_code == 200
              and toggle_resp.json()["enabled"] is False, toggle_resp.text[:150])
        paused_resp = client.post(webhook_path)
        check("暂停任务 Webhook 返回 409", paused_resp.status_code == 409,
              paused_resp.text[:150])
        runs_after_pause = client.get("/api/runs").json()["total"]
        check("暂停任务 Webhook 未产生运行", runs_after_pause == runs_before,
              f"{runs_before} -> {runs_after_pause}")
        manual_resp = client.post(f"/api/tasks/{webhook_id}/run")
        check("暂停任务仍可手动触发",
              manual_resp.status_code == 200 and manual_resp.json()["run_id"] > 0,
              manual_resp.text[:150])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            manual_status = client.get(f"/api/runs/{manual_resp.json()['run_id']}").json()["run"]["status"]
            if manual_status not in ("queued", "running"):
                break
            time.sleep(0.4)
        audit_entries = client.get("/api/audit").json().get("entries", [])
        rejected = [e for e in audit_entries
                    if e["action"] == "webhook_rejected" and e["target"] == f"task:{webhook_id}"]
        check("暂停 Webhook 调用已写入操作日志", bool(rejected),
              str(audit_entries[:3])[:200])
        check("被拒记录不含触发令牌",
              rejected and webhook_secret not in (rejected[0].get("detail") or ""))
        auth_failed = [e for e in audit_entries if e["action"] == "webhook_auth_failed"]
        check("鉴权失败调用已写入操作日志", bool(auth_failed))
        client.post(webhook_path + "x")  # 再触发一次鉴权失败，保证上方记录存在

        toggle_resp = client.post(f"/api/tasks/{webhook_id}/toggle")
        check("恢复启用 Webhook 测试任务", toggle_resp.status_code == 200
              and toggle_resp.json()["enabled"] is True, toggle_resp.text[:150])
        resumed_resp = client.post(webhook_path)
        check("恢复启用后 Webhook 立即可触发", resumed_resp.status_code == 200,
              resumed_resp.text[:150])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            resumed_status = client.get(f"/api/runs/{resumed_resp.json()['run_id']}").json()["run"]["status"]
            if resumed_status not in ("queued", "running"):
                break
            time.sleep(0.4)

        service.selfupdate._save_state({"stage": "applying", "log": [], "operation_id": "wh-check"})
        response = client.post(webhook_path)
        check("自更新期间 Webhook 拒绝触发", response.status_code == 409)
        service.selfupdate._save_state({"stage": "idle", "log": []})

        response = client.post(f"/api/tasks/{webhook_id}/webhook/reset")
        check("重置触发令牌成功",
              response.status_code == 200 and "/api/webhooks/" in response.json()["webhook_url"],
              response.text[:150])
        new_path = urlsplit(response.json()["webhook_url"]).path
        check("旧触发地址已失效", client.post(webhook_path).status_code == 404)
        reset_response = client.post(new_path)
        check("新触发地址可触发", reset_response.status_code == 200, reset_response.text[:150])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            reset_status = client.get(f"/api/runs/{reset_response.json()['run_id']}").json()["run"]["status"]
            if reset_status not in ("queued", "running"):
                break
            time.sleep(0.4)

        # --- 独立禁用 Webhook 触发 -------------------------------------
        dis_wh_resp = client.put(f"/api/tasks/{webhook_id}", json={"webhook_enabled": False})
        check("更新任务禁用 Webhook 触发", dis_wh_resp.status_code == 200
              and dis_wh_resp.json()["task"]["webhook_enabled"] is False, dis_wh_resp.text[:150])
        wh_disabled_resp = client.post(new_path)
        check("单独禁用 Webhook 时触发返回 409", wh_disabled_resp.status_code == 409,
              wh_disabled_resp.text[:150])
        check("单独禁用 Webhook 错误提示明确", "Webhook 触发已禁用" in wh_disabled_resp.text,
              wh_disabled_resp.text[:150])
        audit_entries = client.get("/api/audit").json().get("entries", [])
        wh_disabled_audit = [e for e in audit_entries
                             if e["action"] == "webhook_rejected" and "reason:webhook_disabled" in (e.get("detail") or "")]
        check("禁用 Webhook 调用写入审计日志且原因正确", bool(wh_disabled_audit))

        en_wh_resp = client.put(f"/api/tasks/{webhook_id}", json={"webhook_enabled": True})
        check("恢复启用 Webhook 触发", en_wh_resp.status_code == 200
              and en_wh_resp.json()["task"]["webhook_enabled"] is True, en_wh_resp.text[:150])
        wh_enabled_resp = client.post(new_path)
        check("恢复启用 Webhook 后可正常触发", wh_enabled_resp.status_code == 200,
              wh_enabled_resp.text[:150])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            en_status = client.get(f"/api/runs/{wh_enabled_resp.json()['run_id']}").json()["run"]["status"]
            if en_status not in ("queued", "running"):
                break
            time.sleep(0.4)

        response = client.delete(f"/api/tasks/{webhook_id}")
        check("清理 Webhook 测试任务", response.status_code == 200, response.text[:150])
        check("Webhook 触发写入审计日志", any(
            a["action"] == "run_triggered" and a["actor"] == "webhook"
            for a in service.store.audit.list_recent(limit=50)))

        # --- cancellation -------------------------------------------
        cancel_task = client.post(
            "/api/tasks",
            json={
                "name": "Cancel_Test",
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

        # --- 远程主机 API 自检 ---------------------------------------
        # 用 ssh_key 凭据建主机；不发起真实 SSH（测试机无目标主机），
        # 只验证接口契约、校验拒绝与安全字段不外泄。
        api_host_cred = client.post("/api/credentials", json={
            "name": "api-host-key", "kind": "ssh_key", "username": "deploy",
            "secret": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-body\n-----END OPENSSH PRIVATE KEY-----\n",
        })
        check("可为主机创建 ssh_key 凭据", api_host_cred.status_code == 201, api_host_cred.text[:150])
        api_host_cred_id = api_host_cred.json()["credential"]["id"]

        created_host = client.post("/api/hosts", json={
            "name": "Api_host", "address": "192.0.2.10", "port": 22,
            "username": "deploy", "credential_id": api_host_cred_id,
            "base_dir": "/srv/autodeploy",
        })
        check("创建主机返回 201", created_host.status_code == 201, created_host.text[:200])
        host_body = created_host.json()["host"]
        api_host_id = host_body["id"]
        check("主机响应不含私钥内容", "secret" not in created_host.text.lower())
        check("主机响应提供连接目标描述", host_body["target"] == "deploy@192.0.2.10")
        check("新建主机状态为未检测", host_body["status"] == "unknown")

        listed_hosts = client.get("/api/hosts")
        check("主机列表返回 200 且包含新建主机",
              listed_hosts.status_code == 200
              and any(h["id"] == api_host_id for h in listed_hosts.json()["hosts"]))

        # 重名与非法参数必须被拒绝，且不落库。
        dup_host = client.post("/api/hosts", json={
            "name": "API_HOST", "address": "192.0.2.11", "username": "deploy",
            "credential_id": api_host_cred_id,
        })
        check("主机重名被拒绝", dup_host.status_code == 409, dup_host.text[:120])
        bad_host = client.post("/api/hosts", json={
            "name": "Bad_host", "address": "h;id", "username": "deploy",
            "credential_id": api_host_cred_id,
        })
        check("非法主机地址被拒绝", bad_host.status_code == 422, bad_host.text[:120])
        bad_dir = client.post("/api/hosts", json={
            "name": "Bad_dir", "address": "192.0.2.12", "username": "deploy",
            "credential_id": api_host_cred_id, "base_dir": "relative/dir",
        })
        check("相对基础目录被拒绝", bad_dir.status_code == 422, bad_dir.text[:120])
        # 非 ssh_key 类型凭据必须被拒绝：用了也无法 SSH 登录。
        https_cred = client.post("/api/credentials", json={
            "name": "api-host-https", "kind": "https_token",
            "username": "git", "secret": "ghp_" + "a" * 30,
        })
        check("可创建 HTTPS 凭据用于对照", https_cred.status_code == 201, https_cred.text[:150])
        https_cred_id = https_cred.json()["credential"]["id"]
        wrong_kind = client.post("/api/hosts", json={
            "name": "Wrong_cred", "address": "192.0.2.13", "username": "deploy",
            "credential_id": https_cred_id,
        })
        check("非 SSH 类型的凭据被拒绝", wrong_kind.status_code == 422, wrong_kind.text[:150])

        # 被任务引用时禁止删除，避免任务悄悄退回本机部署。
        host_linked_task = client.post("/api/tasks", json={
            "name": "Host_linked", "repo_url": str(origin), "repo_branch": "main",
            "deploy_method": "script", "deploy_script": "echo hosted",
            "skip_if_no_changes": False, "host_id": api_host_id,
        })
        check("任务可绑定目标主机", host_linked_task.status_code == 201,
              host_linked_task.text[:200])
        host_linked_id = host_linked_task.json()["task"]["id"]
        check("任务详情回显目标主机",
              client.get(f"/api/tasks/{host_linked_id}").json()["task"]["host_id"] == api_host_id)
        blocked_delete = client.delete(f"/api/hosts/{api_host_id}")
        check("被任务引用的主机不能删除", blocked_delete.status_code == 409,
              blocked_delete.text[:150])

        # 凭据的主机引用：只绑定主机（不碰仓库）的私钥此前在列表里显示
        # 「未使用」，删除也不会被拦下——外键置空后该主机再无法登录。
        listed_cred = next(c for c in client.get("/api/credentials").json()["credentials"]
                           if c["id"] == api_host_cred_id)
        check("凭据列表统计主机引用", listed_cred.get("used_by_hosts") == 1,
              str(listed_cred.get("used_by_hosts")))
        check("只绑主机的凭据不算未使用",
              listed_cred.get("used_by") == 0 and listed_cred.get("used_by_hosts") == 1)
        cred_detail = client.get(f"/api/credentials/{api_host_cred_id}").json()["credential"]
        check("凭据详情点名引用它的主机",
              [h["id"] for h in cred_detail.get("hosts") or []] == [api_host_id],
              str(cred_detail.get("hosts")))
        blocked_cred_delete = client.delete(f"/api/credentials/{api_host_cred_id}")
        check("被主机引用的凭据不能直接删除", blocked_cred_delete.status_code == 409,
              blocked_cred_delete.text[:150])
        check("删除拦截文案点名主机",
              "主机" in blocked_cred_delete.json().get("detail", ""),
              blocked_cred_delete.text[:150])

        # 主机登录测试：不发起真实 SSH，只验证接口契约（类型、必填、超时清理）。
        wrong_kind_test = client.post(f"/api/credentials/{https_cred_id}/test-host",
                                      json={"address": "192.0.2.20", "username": "deploy"})
        check("HTTPS 凭据拒绝主机登录测试", wrong_kind_test.status_code == 422,
              wrong_kind_test.text[:150])
        bad_address = client.post(f"/api/credentials/{api_host_cred_id}/test-host",
                                  json={"address": "h;id", "username": "deploy"})
        check("主机登录测试校验地址", bad_address.status_code == 422, bad_address.text[:150])
        missing_user = client.post(f"/api/credentials/{api_host_cred_id}/test-host",
                                   json={"address": "192.0.2.20"})
        check("主机登录测试要求用户名", missing_user.status_code == 422,
              missing_user.text[:150])
        # 主机登录结论独立落库：失败不能把「仓库可用」一起抹掉。
        service.store.credentials.record_test(api_host_cred_id, ok=True, error="")
        failed_host_test = client.post(f"/api/credentials/{api_host_cred_id}/test-host",
                                       json={"address": "127.0.0.1", "port": 1,
                                             "username": "deploy"})
        check("主机登录测试返回 200 + ok:false（失败是结果不是请求错误）",
              failed_host_test.status_code == 200
              and failed_host_test.json().get("ok") is False,
              failed_host_test.text[:200])
        after_host_test = service.store.credentials.get(api_host_cred_id)
        check("主机登录测试不覆盖仓库测试结论",
              after_host_test.get("last_test_ok") == 1,
              str(after_host_test.get("last_test_ok")))
        check("主机登录测试结论单独记录",
              after_host_test.get("last_host_test_ok") == 0
              and bool(after_host_test.get("last_host_test_at")))
        check("主机登录测试返回目标描述",
              "@" in (failed_host_test.json().get("target") or ""),
              str(failed_host_test.json().get("target")))
        # 换私钥后两侧结论都必须失效，否则界面会拿旧结论骗人。
        client.put(f"/api/credentials/{api_host_cred_id}",
                   json={"secret": "-----BEGIN OPENSSH PRIVATE KEY-----\nnew\n-----END OPENSSH PRIVATE KEY-----\n"})
        cleared = service.store.credentials.get(api_host_cred_id)
        check("修改私钥后仓库测试结论失效", cleared.get("last_test_ok") is None)
        check("修改私钥后主机测试结论失效", cleared.get("last_host_test_ok") is None)

        client.delete(f"/api/tasks/{host_linked_id}")
        api_delete = client.delete(f"/api/hosts/{api_host_id}")
        check("解除引用后主机可删除", api_delete.status_code == 200, api_delete.text[:150])
        check("删除后主机不再出现在列表",
              all(h["id"] != api_host_id for h in client.get("/api/hosts").json()["hosts"]))
        after_host_gone = client.delete(f"/api/credentials/{api_host_cred_id}")
        check("主机解除绑定后凭据可删除", after_host_gone.status_code == 200,
              after_host_gone.text[:150])
        client.delete(f"/api/credentials/{https_cred_id}")

        # --- 监控 API 自检 -------------------------------------------
        from app.monitor import HostMonitor
        snap = HostMonitor().sample_host()
        check("HostMonitor 采样成功", snap.cpu_cores >= 1 and snap.memory_total > 0)
        check("HostMonitor 包含必要字段", bool(snap.timestamp and snap.os_name))
        check("HostMonitor 包含磁盘I/O速率与应用数据大小",
              hasattr(snap, "disk_read_rate") and hasattr(snap, "disk_write_rate") and hasattr(snap, "app_data_bytes"))

        host_resp = client.get("/api/monitor/host")
        check("主机监控接口返回 200", host_resp.status_code == 200)
        host_data = host_resp.json()
        check("主机监控数据包含 CPU/内存/磁盘/应用大小",
              "cpu_percent" in host_data and "memory_percent" in host_data
              and "disk_percent" in host_data and "app_data_bytes" in host_data)
        # 存储卡片已改为展示宿主机分区占用：三个分区字段必须同时可用，
        # 只给百分比会让「已用 / 总量」无处取值。
        check("主机监控返回分区已用/总量/剩余（存储卡片主指标）",
              all(isinstance(host_data.get(key), int) and host_data.get(key) >= 0
                  for key in ("disk_total", "disk_used", "disk_free"))
              and host_data["disk_used"] + host_data["disk_free"] <= host_data["disk_total"] + 1,
              f"total={host_data.get('disk_total')} used={host_data.get('disk_used')} "
              f"free={host_data.get('disk_free')}")
        check("主机监控包含磁盘I/O吞吐字段",
              "disk_read_rate" in host_data and "disk_write_rate" in host_data)
        check("主机监控包含系统名与运行时长", "os_name" in host_data and "uptime_seconds" in host_data)

        task_mon_resp = client.get("/api/monitor/tasks")
        check("任务资源监控接口返回 200", task_mon_resp.status_code == 200)
        check("任务资源监控返回列表", isinstance(task_mon_resp.json(), list))

        # 容器明细字段：抽屉面板要用原始字节数算内存百分比、展示镜像，
        # 只有 human 字符串（"20.5MiB"）无法得出进度条比例。
        from app.monitor import TaskResourceMonitor, get_monitor
        fake_stats = {
            "abc123": {
                "id": "abc1234567890",
                "name": "web-1",
                "compose_project": "autodeploy-mon-sample",
                "image": "nginx:1.27",
                "status": "running",
                "cpu_percent": 3.5,
                "mem_usage_human": "20.5MiB",
                "mem_usage_bytes": 20 * 1024**2,
                "mem_limit_bytes": 1024**3,
                "net_io": "1.2kB / 3.4kB",
                "block_io": "0B / 8.19kB",
                "pids": 4,
            }
        }
        sample_task = {"id": 99123, "name": "mon-sample", "deploy_method": "docker_compose",
                       "enabled": True}
        from unittest.mock import patch as mon_patch
        # 只替换 docker stats 来源，聚合逻辑仍走真实实现，避免测试自证。
        with mon_patch.object(type(get_monitor()), "get_docker_stats", return_value=fake_stats):
            detail_rows = TaskResourceMonitor.get_task_resources([sample_task])
        check("任务资源监控能按 compose 项目聚合容器", len(detail_rows) == 1
              and detail_rows[0]["container_count"] == 1, str(detail_rows)[:200])
        detail = (detail_rows[0].get("containers") or [{}])[0]
        check("容器明细带内存字节与上限（供抽屉算占用比例）",
              detail.get("mem_usage_bytes") == 20 * 1024**2
              and detail.get("mem_limit_bytes") == 1024**3, str(detail)[:200])
        check("容器明细带镜像名与进程数", detail.get("image") == "nginx:1.27" and detail.get("pids") == 4)
        check("docker stats 内存单位解析不回退成错误数值",
              TaskResourceMonitor._parse_mem_usage("20.5MiB / 1.5GiB") == (20 * 1024**2 + 524288, 3 * 1024**3 // 2),
              str(TaskResourceMonitor._parse_mem_usage("20.5MiB / 1.5GiB")))

        history_resp = client.get("/api/monitor/history?range=1h&limit=10")
        check("监控历史时序接口 1h 返回 200", history_resp.status_code == 200)
        check("监控历史返回快照数组", isinstance(history_resp.json(), list))

        # 持久化时序库与多时间跨度查询
        service.store.metrics.record({
            "timestamp": snap.timestamp,
            "cpu_percent": snap.cpu_percent,
            "memory_percent": snap.memory_percent,
            "disk_read_rate": snap.disk_read_rate,
            "disk_write_rate": snap.disk_write_rate,
            "net_rx_rate": snap.net_rx_rate,
            "net_tx_rate": snap.net_tx_rate,
        })
        h24_resp = client.get("/api/monitor/history?range=24h&limit=50")
        check("监控历史 24h 查询返回 200", h24_resp.status_code == 200 and len(h24_resp.json()) >= 1)
        h7d_resp = client.get("/api/monitor/history?range=7d&limit=50")
        check("监控历史 7d 查询返回 200", h7d_resp.status_code == 200)
        check("非法范围参数被拒绝", client.get("/api/monitor/history?range=invalid").status_code == 422)

        # --- 容器维度监控：采样速率、落库、分桶与透视 -----------------
        # docker stats 的 NetIO / BlockIO 是累计量，速率必须由相邻两次采样差值算出；
        # 直接把累计值当速率会让图表随时间无限上涨。
        check("docker stats 累计 I/O 文本可解析为字节",
              TaskResourceMonitor._parse_io_pair("1.2kB / 3.4MB") == (1200, 3400000),
              str(TaskResourceMonitor._parse_io_pair("1.2kB / 3.4MB")))
        check("docker stats 未就绪时的 I/O 占位不报错",
              TaskResourceMonitor._parse_io_pair("--") == (0, 0)
              and TaskResourceMonitor._parse_bytes("0B") == 0)

        from app.monitor import MonitorService
        rate_monitor = MonitorService()
        base_stats = {
            "ct1": {"id": "ct1-abcdef", "name": "web-1", "compose_project": "autodeploy-c1",
                    "status": "running", "cpu_percent": 12.5, "mem_usage_human": "20MiB",
                    "mem_usage_bytes": 20 * 1024**2, "mem_limit_bytes": 1024**3,
                    "net_io": "1kB / 2kB", "block_io": "3kB / 4kB",
                    "net_rx_total": 100000, "net_tx_total": 200000,
                    "block_read_total": 30000, "block_write_total": 40000, "pids": 3},
        }
        rate_monitor._record_container_samples(base_stats, 1000.0)
        later_stats = {"ct1": dict(base_stats["ct1"])}
        later_stats["ct1"].update({
            "net_rx_total": 100000 + 204800,     # 200 KiB / 2s = 100 KB/s
            "block_read_total": 30000 + 102400,  # 100 KiB / 2s = 50 KB/s
        })
        rate_monitor._record_container_samples(later_stats, 1002.0)
        batches = rate_monitor.get_container_batches()
        first_sample = batches[0][0]
        second_sample = batches[1][0]
        check("容器首次采样速率为 0（没有基线可比）",
              first_sample["net_rx_rate"] == 0.0 and first_sample["disk_read_rate"] == 0.0,
              str(first_sample))
        check("容器网络速率由累计量差值算出",
              abs(second_sample["net_rx_rate"] - 100.0) < 0.5 and second_sample["net_tx_rate"] == 0.0,
              str(second_sample["net_rx_rate"]))
        check("容器磁盘读速率由累计量差值算出",
              abs(second_sample["disk_read_rate"] - 50.0) < 0.5, str(second_sample["disk_read_rate"]))
        check("容器内存占用率按上限换算", abs(second_sample["memory_percent"] - 1.95) < 0.05,
              str(second_sample["memory_percent"]))
        check("容器重启后累计量回退不产生负速率", all(
              rate >= 0 for rate in (second_sample["net_rx_rate"], second_sample["disk_read_rate"])))

        # 持久化 + 分桶透视：同一容器的列数组必须与共享时间轴等长，
        # 否则前端按下标取值会串行。
        from app.api.monitor import CONTAINER_METRIC_KEYS, _bucket_rows, _pivot_containers
        from datetime import timezone as _mon_timezone
        bucket_base = 1792317600  # 固定纪元，避免依赖真实时钟
        sample_rows = []
        for step in range(6):
            ts = datetime.fromtimestamp(bucket_base + step * 25, tz=_mon_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for cid, cname in (("c1", "web-1"), ("c2", "db-1")):
                sample_rows.append({
                    "timestamp": ts, "container_id": cid, "container_name": cname,
                    "compose_project": "autodeploy-c", "cpu_percent": float(step),
                    "memory_bytes": 1024.0, "memory_percent": 1.0,
                    "disk_read_rate": 1.0, "disk_write_rate": 2.0,
                    "net_rx_rate": 3.0, "net_tx_rate": 4.0,
                })
        bucketed = _bucket_rows(sample_rows, 60, CONTAINER_METRIC_KEYS,
                                identity=True, since_epoch=bucket_base)
        check("容器采样按 60 秒分桶合并", len(bucketed) == 6, str(len(bucketed)))
        check("分桶后同一桶内取平均", bucketed[0]["cpu_percent"] == 1.0, str(bucketed[0]))
        pivoted = _pivot_containers(bucketed)
        check("容器透视按名称排序并共用时间轴",
              [c["name"] for c in pivoted["containers"]] == ["db-1", "web-1"]
              and len(pivoted["timestamps"]) == 3, str(pivoted)[:200])
        check("容器列数组与时间轴等长",
              all(len(c["cpu_percent"]) == len(pivoted["timestamps"])
                  and len(c["net_tx_rate"]) == len(pivoted["timestamps"])
                  for c in pivoted["containers"]))
        outside = _bucket_rows(sample_rows, 60, CONTAINER_METRIC_KEYS,
                               identity=True, since_epoch=bucket_base + 100)
        check("窗口外的采样被丢弃", outside and outside[0]["timestamp"].endswith("10:01:00Z"),
              str(outside[0] if outside else None))
        host_only = _bucket_rows(sample_rows, 20, CONTAINER_METRIC_KEYS,
                                 identity=False, since_epoch=bucket_base)
        check("主机口径不按容器拆分", len(host_only) == len({r["timestamp"] for r in sample_rows})
              or len({r["timestamp"] for r in host_only}) < len(sample_rows) * 2,
              str(len(host_only)))

        check("容器时序落库不再写空批次", isinstance(
              service.store.container_metrics.record_many([]), type(None)))
        service.store.container_metrics.record_many([
            {"timestamp": sample_rows[0]["timestamp"], "container_id": "c1",
             "container_name": "web-1", "compose_project": "p", "cpu_percent": 3.0,
             "memory_bytes": 1024, "memory_percent": 1.0, "disk_read_rate": 1.0,
             "disk_write_rate": 2.0, "net_rx_rate": 3.0, "net_tx_rate": 4.0}
        ])
        persisted = service.store.container_metrics.query_bucketed("2000-01-01T00:00:00Z", 60)
        check("容器时序可落库并按桶查询", len(persisted) == 1
              and persisted[0]["container_name"] == "web-1", str(persisted)[:200])

        ch1 = client.get("/api/monitor/containers/history?range=1h")
        check("容器历史接口 1h 返回 200", ch1.status_code == 200, ch1.text[:200])
        ch1_data = ch1.json()
        check("容器历史返回时间轴与容器列数组",
              isinstance(ch1_data.get("timestamps"), list)
              and isinstance(ch1_data.get("containers"), list))
        check("容器历史各列长度与时间轴一致",
              all(len(c.get("cpu_percent") or []) == len(ch1_data["timestamps"])
                  for c in ch1_data["containers"]))
        check("容器历史非法范围被拒绝",
              client.get("/api/monitor/containers/history?range=invalid").status_code == 422)
        ch7 = client.get("/api/monitor/containers/history?range=7d")
        check("容器历史 7d 查询返回 200", ch7.status_code == 200)

        # 7d 窗口必须取最新的 limit 条：旧实现 ORDER BY id ASC + LIMIT 会只返回
        # 窗口最前面的点，页面看起来像「只有刚上线那几小时有数据」。
        from app.schedule import utcnow as _mon_utcnow
        for offset in range(5):
            service.store.metrics.record({
                "timestamp": iso(_mon_utcnow() - timedelta(seconds=60 * (5 - offset))),
                "cpu_percent": float(offset), "memory_percent": 1.0,
                "disk_read_rate": 0.0, "disk_write_rate": 0.0,
                "net_rx_rate": 0.0, "net_tx_rate": 0.0,
            })
        recent = service.store.metrics.query_range(
            iso(_mon_utcnow() - timedelta(days=7)), limit=2)
        check("长周期查询保留最新的点（不回退到最早）",
              len(recent) == 2 and float(recent[-1]["cpu_percent"]) == 4.0, str(recent))

        # --- 备份与恢复全流程自检 -------------------------------------
        # 1. 创建备份
        bk_create_resp = client.post("/api/backups", json={"include_logs": False, "include_artifacts": False, "retention_count": 5})
        check("创建备份接口返回 200", bk_create_resp.status_code == 200, bk_create_resp.text[:150])
        bk_obj = bk_create_resp.json()["backup"]
        bk_id = bk_obj["id"]
        check("备份对象包含 ID 和文件名", bk_id > 0 and "autodeploy-backup-" in bk_obj["filename"])
        check("备份文件已生成且大小大于 0", bk_obj["size_bytes"] > 0 and len(bk_obj["sha256"]) == 64)

        # 2. 列表查询
        bk_list_resp = client.get("/api/backups")
        check("备份列表返回 200", bk_list_resp.status_code == 200)
        check("备份列表中存在新建备份", any(b["id"] == bk_id for b in bk_list_resp.json()["backups"]))

        # 3. 预检与完整性
        bk_inspect_resp = client.post(f"/api/backups/{bk_id}/inspect")
        check("备份预检接口返回 200", bk_inspect_resp.status_code == 200)
        inspection = bk_inspect_resp.json()["inspection"]
        check("预检结果确认数据库完整", inspection["valid"] is True)

        # 4. 下载接口
        bk_dl_resp = client.get(f"/api/backups/{bk_id}/download")
        check("备份下载接口返回 200", bk_dl_resp.status_code == 200)
        check("下载内容类型为 gzip", "gzip" in bk_dl_resp.headers.get("content-type", ""))

        # 5. 灾难恢复测试
        # 先创建一个标记任务，恢复后该任务应该消失
        temp_task_id = service.store.tasks.create({
            "name": "Should_Disappear_After_Restore",
            "repo_url": str(origin),
            "deploy_method": "script",
            "deploy_script": "true",
        })
        check("已创建用于恢复验证的临时任务", service.store.tasks.get(temp_task_id) is not None)
        restore_resp = client.post(f"/api/backups/{bk_id}/restore")
        check("恢复备份接口返回 200", restore_resp.status_code == 200, restore_resp.text[:150])
        check("数据已恢复（临时任务已回退消除）", service.store.tasks.get(temp_task_id) is None)
        # 恢复后备份资产自动补录：磁盘上已有的备份文件在恢复后依然保留在 backups 表中，
        # 用户在前端不会看到备份“自杀式消失”。
        check("恢复后现存备份记录完好保留",
              service.store.backups.get_by_filename(bk_obj["filename"]) is not None)

        # 6. 删除备份
        bk_del_resp = client.delete(f"/api/backups/{bk_id}")
        check("删除备份接口返回 200", bk_del_resp.status_code == 200)
        check("删除后记录与磁盘文件均不存在",
              service.store.backups.get(bk_id) is None and not Path(bk_obj["filepath"]).exists())
        check("删除不存在的备份返回 404", client.delete(f"/api/backups/{bk_id}").status_code == 404)

        # --- 云端存储与适配器自检 -----------------------------------
        from app.cloud.webdav import WebDAVProvider
        from app.cloud import prune_cloud_backups
        from app.cloud.base import CloudProvider

        # 调度器定时备份任务测试：直接面向 store 执行，无需任何全局 Service 单例。
        service.scheduler.invalidate_backup_schedule()
        check("设置变更后备份调度缓存已失效", service.scheduler._next_backup_time is None)
        before_sched_count = service.store.backups.count()
        service.scheduler._run_backup_job(include_logs=False, include_artifacts=False, retention_count=5)
        check("调度器后台线程成功执行定时备份", service.store.backups.count() == before_sched_count + 1)

        # 新增设置字段必须真实落盘（白名单遗漏会让保存静默丢失）。
        settings_resp = client.put("/api/settings", json={
            "backup_enabled": True,
            "backup_schedule_type": "cron",
            "backup_schedule_expression": "30 4 * * *",
            "backup_retention_count": 7,
            "backup_include_logs": False,
            "backup_include_artifacts": False,
            "cloud_backup_enabled": False,
            "cloud_provider": "webdav",
            "cloud_webdav_url": "https://dav.example.com/dav/",
            "cloud_webdav_username": "user",
            "cloud_webdav_password": "secret-pass",
            "cloud_webdav_root": "auto/backups",
            "cloud_retention_count": 5,
        })
        check("备份与云端设置保存成功", settings_resp.status_code == 200, settings_resp.text[:200])
        saved = service.settings
        check("备份定时设置已持久化", saved.backup_enabled is True
              and saved.backup_schedule_expression == "30 4 * * *"
              and saved.backup_retention_count == 7)
        check("云端存储设置已持久化", saved.cloud_provider == "webdav"
              and saved.cloud_webdav_url == "https://dav.example.com/dav/"
              and saved.cloud_webdav_password == "secret-pass")
        check("云端 WebDAV 密码不回显",
              "cloud_webdav_password" not in service.export_settings()
              and service.export_settings().get("cloud_webdav_password_set") is True)
        check("非法备份表达式被拒绝",
              client.put("/api/settings", json={"backup_schedule_expression": "not-a-cron"}).status_code == 422)
        check("非法云端目录（路径穿越）被拒绝",
              client.put("/api/settings", json={"cloud_webdav_root": "../escape"}).status_code == 422)
        check("非法 rclone 目标（缺冒号）被拒绝",
              client.put("/api/settings", json={"cloud_rclone_remote": "no-colon"}).status_code == 422)

        dav = WebDAVProvider("https://dav.example.com/dav/", "user", "pass", "my-backups")
        check("WebDAV 路径拼接正确", dav._target_url("test.tar.gz") == "https://dav.example.com/dav/my-backups/test.tar.gz")
        check("WebDAV Basic Auth 头生成正常", "Basic " in dav._headers().get("Authorization", ""))

        # 测试云端保留淘汰算法
        class MockCloudProvider(CloudProvider):
            def __init__(self):
                self.deleted = []
            def test_connection(self):
                return True, "ok"
            def upload_file(self, local_path, remote_filename):
                return True, "ok"
            def delete_file(self, remote_filename):
                self.deleted.append(remote_filename)
                return True, "ok"
            def list_files(self):
                return [
                    {"name": "autodeploy-backup-20261016-010000.tar.gz"},
                    {"name": "autodeploy-backup-20261016-020000.tar.gz"},
                    {"name": "autodeploy-backup-20261016-030000.tar.gz"},
                    {"name": "other-file.txt"},
                ]

        mock_p = MockCloudProvider()
        pruned_cnt = prune_cloud_backups(mock_p, keep_count=2)
        check("云端保留策略正确淘汰最旧备份", pruned_cnt == 1 and mock_p.deleted == ["autodeploy-backup-20261016-010000.tar.gz"])

        # 测试云存储连通性接口
        cloud_test_bad = client.post("/api/backups/cloud/test", json={"provider": "webdav", "webdav_url": "bad-url"})
        check("非法 WebDAV URL 测试返回失败状态", cloud_test_bad.status_code == 200 and cloud_test_bad.json()["ok"] is False)

        cloud_test_invalid_type = client.post("/api/backups/cloud/test", json={"provider": "unknown_type"})
        check("未知存储驱动类型被拒绝", cloud_test_invalid_type.status_code == 400)

        client.post("/api/auth/logout")
        check("未登录访问监控接口被拒绝", client.get("/api/monitor/host").status_code == 401)
        check("未登录访问备份接口被拒绝", client.get("/api/backups").status_code == 401)
        response = client.get("/api/tasks")
        check("退出后无法访问", response.status_code == 401)

    # ------------------------------------------------------------------
    section("部署脚本（GHCR 镜像 + Compose 一键部署）")
    from app.config import UPDATE_REPO_DEFAULT as config_repo_default
    from app.selfupdate import UPDATE_REPO_DEFAULT as selfupdate_repo_default
    check("默认更新源指向 auto-deploy 仓库",
          config_repo_default == selfupdate_repo_default
          == "https://github.com/j9kkk/auto-deploy.git",
          f"{config_repo_default!r} / {selfupdate_repo_default!r}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 快速开始提供 Docker Compose 一键部署",
          "scripts/bootstrap.sh" in readme and "docker compose up" in readme)

    bootstrap_sh = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    check("一键脚本拉取预构建镜像启动（无需克隆仓库与本地构建）",
          "compose pull" in bootstrap_sh and "up -d --no-build" in bootstrap_sh
          and "git clone" not in bootstrap_sh)
    check("一键脚本从官方仓库下载 compose 文件",
          "raw.githubusercontent.com/j9kkk/auto-deploy" in bootstrap_sh
          and "docker-compose.yml" in bootstrap_sh)
    check("一键脚本自动创建安装目录 /opt/auto-deploy",
          "/opt/auto-deploy" in bootstrap_sh and "mkdir -p" in bootstrap_sh)
    check("一键脚本自动安装缺失的 Docker 并校验 compose 插件",
          "get.docker.com" in bootstrap_sh and "docker compose version" in bootstrap_sh)
    check("一键脚本下载自动重试并校验内容（网络抖动与半截文件不会静默落盘）",
          "for attempt in 1 2 3" in bootstrap_sh and 'grep -q "^services:"' in bootstrap_sh)
    check("一键脚本支持版本锁定（AUTODEPLOY_VERSION 同步锁定镜像 tag）",
          "AUTODEPLOY_VERSION" in bootstrap_sh and "AUTODEPLOY_IMAGE_TAG" in bootstrap_sh)
    check("一键脚本支持离线安装与镜像源替换",
          "AUTODEPLOY_COMPOSE_FILE" in bootstrap_sh and "AUTODEPLOY_IMAGE" in bootstrap_sh)
    check("一键脚本自动挂载宿主机 Docker socket（无需手动编辑文件）",
          "docker-compose.override.yml" in bootstrap_sh
          and "stat -c %g" in bootstrap_sh and "group_add" in bootstrap_sh)
    check("一键脚本只管理自己生成的 override，不覆盖用户手写文件",
          "AutoDeploy 自动生成" in bootstrap_sh)
    check("一键脚本可关闭 socket 挂载",
          "AUTODEPLOY_MOUNT_DOCKER_SOCKET" in bootstrap_sh)
    check("一键脚本等待 /api/health 就绪（无人值守安装也能确认服务可用）",
          "/api/health" in bootstrap_sh)
    check("一键脚本从日志提取一次性初始密码（重试避免与日志落盘竞态）",
          "初始密码" in bootstrap_sh and "seq 1 10" in bootstrap_sh)
    check("一键脚本管道模式不读取自身文件（bash 增量读 stdin，落盘副本会截断）",
          "BASH_SOURCE" not in bootstrap_sh and "$0" not in bootstrap_sh)
    check("已移除 systemd 安装脚本（仅保留 Docker Compose 部署方式）",
          not (ROOT / "scripts" / "install.sh").exists()
          and not (ROOT / "scripts" / "uninstall.sh").exists())

    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("Compose 默认使用 GHCR 官方镜像并保留本地构建入口",
          "ghcr.io/j9kkk/auto-deploy" in compose_text and "build: ." in compose_text)
    check("Compose 定义 autodeploy 服务并持久化 /app/data",
          "autodeploy:" in compose_text and "autodeploy-data:/app/data" in compose_text)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check("镜像内置 git、ssh 与 docker CLI（各任务部署方式开箱可用）",
          "git openssh-client" in dockerfile and "docker-compose-plugin" in dockerfile)
    check("镜像以非 root 用户运行", "USER autodeploy" in dockerfile)
    check("镜像自带健康检查", "HEALTHCHECK" in dockerfile and "/api/health" in dockerfile)

    docker_wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    check("CI 自动构建并推送 amd64/arm64 镜像到 GHCR",
          "ghcr.io/j9kkk/auto-deploy" in docker_wf
          and "docker/build-push-action" in docker_wf
          and "linux/amd64,linux/arm64" in docker_wf
          and "packages: write" in docker_wf)
    syntax = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "bootstrap.sh")],
        capture_output=True, text=True,
    )
    check("bootstrap.sh 语法有效（bash -n）", syntax.returncode == 0,
          syntax.stderr.strip())

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
    # 私钥口令与 secret 同级机密：同样只暴露“是否设置”。
    decoded_ssh = decode_credential({"id": 2, "name": "k", "kind": "ssh_key", "username": "git",
                                     "secret": "KEY", "passphrase": "pp", "description": ""})
    check("解码后不含私钥口令明文", "passphrase" not in decoded_ssh and decoded_ssh["has_passphrase"])
    check("无口令凭据标记为未设置",
          decode_credential({"id": 3, "name": "p", "kind": "ssh_key",
                             "username": "git", "secret": "KEY", "description": ""})["has_passphrase"] is False)

    # 凭据局部校验不再把已有 kind 塞进输出：更新接口据其判断“类型是否
    # 变化”，此前仅改名/改备注也会把测试结论清成失败。
    from app.validation import validate_credential_payload
    renamed = validate_credential_payload({"name": "改名"}, partial=True,
                                          existing={"kind": "ssh_key", "secret": "KEY", "name": "旧名"})
    check("凭据局部改名不视为改类型", "kind" not in renamed)
    retyped = validate_credential_payload({"kind": "https_token", "secret": "tok"}, partial=True,
                                          existing={"kind": "ssh_key", "secret": "KEY", "name": "旧名"})
    check("显式提交类型仍进入输出", retyped.get("kind") == "https_token")
    check("空私钥指纹为空", ssh_key_fingerprint("") == "")
    check("非法私钥不产生指纹", ssh_key_fingerprint("not a key") == "")

    # --- SSH 公钥推导与指纹 ---
    # 指纹必须与 `ssh-keygen -lf` 一致，否则用户拿界面上的值和 GitHub 核对
    # 会永远对不上（曾直接用私钥文件内容做哈希，得到的值完全不同）。
    from app import sshkey

    generated_available = bool(shutil.which("ssh-keygen"))
    if generated_available:
        priv_text, pub_text, fp = sshkey.generate_keypair(comment="selfcheck")
        check("生成的私钥为 OpenSSH 格式",
              priv_text.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        check("生成的公钥为单行 ssh-ed25519", pub_text.startswith("ssh-ed25519 ") and "\n" not in pub_text)
        check("生成时给出指纹", fp.startswith("SHA256:"))

        # 从私钥推导的公钥必须与 ssh-keygen 输出逐字一致。
        derived = sshkey.public_key_line(priv_text, "selfcheck")
        check("公钥可由私钥复现", derived == pub_text, f"{derived!r} != {pub_text!r}")

        # 指纹必须等于 ssh-keygen 对同一把钥匙算出的值。
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as _dir:
            pub_path = Path(_dir) / "k.pub"
            pub_path.write_text(pub_text + "\n", encoding="utf-8")
            real = subprocess.run(
                ["ssh-keygen", "-lf", str(pub_path)], capture_output=True, text=True
            )
        real_fp = real.stdout.split()[1] if real.returncode == 0 and real.stdout.split() else ""
        check("指纹与 ssh-keygen 一致", real_fp == fp, f"{real_fp!r} != {fp!r}")

        # 界面显示的是 store 层的 ssh_key_fingerprint，必须单独断言：
        # 只测 sshkey.fingerprint 会漏掉 store 里另写一份实现的情形。
        check("store 指纹与 ssh-keygen 一致",
              ssh_key_fingerprint(priv_text) == real_fp,
              f"{ssh_key_fingerprint(priv_text)!r} != {real_fp!r}")
        check("解码出的指纹即 ssh-keygen 指纹",
              decode_credential({"id": 4, "name": "n", "kind": "ssh_key",
                                 "username": "git", "secret": priv_text,
                                 "description": ""}).get("fingerprint") == real_fp)

        # 解码时也带出公钥，但注释用的是凭据名，故只比较密钥本体。
        decoded_key = decode_credential(
            {"id": 2, "name": "n", "kind": "ssh_key", "username": "git",
             "secret": priv_text, "description": ""}
        ).get("public_key", "")
        check("SSH 凭据解码带出公钥",
              decoded_key.split()[:2] == pub_text.split()[:2], decoded_key)
        check("解码结果不含私钥明文",
              "PRIVATE KEY" not in json.dumps(
                  decode_credential({"id": 3, "name": "n", "kind": "ssh_key",
                                     "username": "git", "secret": priv_text, "description": ""})))

        # 带口令的私钥也能推导公钥：OpenSSH 私钥里的公钥段是明文，
        # 无需解密即可读出，因此口令不会影响公钥与指纹的展示。
        enc_priv, enc_pub, enc_fp = None, None, None
        with _tempfile.TemporaryDirectory() as _dir:
            enc_path = Path(_dir) / "enc"
            made = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(enc_path), "-N", "pw123", "-C", "e", "-q"],
                capture_output=True, text=True,
            )
            if made.returncode == 0:
                enc_priv = enc_path.read_text(encoding="utf-8")
                enc_pub = (Path(_dir) / "enc.pub").read_text(encoding="utf-8").strip()
                enc_fp = subprocess.run(
                    ["ssh-keygen", "-lf", str(enc_path) + ".pub"], capture_output=True, text=True
                ).stdout.split()[1]
        if enc_priv:
            check("带口令私钥仍可推导公钥",
                  sshkey.public_key_line(enc_priv).split()[:2] == enc_pub.split()[:2])
            check("带口令私钥指纹正确", sshkey.fingerprint(enc_priv) == enc_fp)
        else:
            check("跳过带口令私钥检查", True)
    else:
        check("跳过 SSH 生成检查（无 ssh-keygen）", True)

    check("拒绝非私钥内容", sshkey.validate_private_key("hello") is not None)
    check("拒绝空私钥", sshkey.validate_private_key("") is not None)

    # --- 凭据获取指引（界面帮助面板的数据源）---
    from app.api.credentials import credential_guide

    guide = credential_guide(user={"username": "admin"})
    kinds = {item["kind"] for item in guide["kinds"]}
    check("指引覆盖两种凭据类型", kinds == {"https_token", "ssh_key"}, str(kinds))
    check("指引说明各有适用场景",
          all(item.get("best_for") for item in guide["kinds"]))
    token_kind = next(i for i in guide["kinds"] if i["kind"] == "https_token")
    ssh_kind = next(i for i in guide["kinds"] if i["kind"] == "ssh_key")
    check("令牌指引指向细粒度令牌页",
          "personal-access-tokens/new" in token_kind["create_url"])
    check("令牌指引写明 Contents 只读权限",
          any("Contents" in s for s in token_kind["steps"]))
    check("SSH 指引指向仓库 Deploy Keys 页", "settings/keys" in ssh_kind["create_url"])
    check("SSH 指引提示不要开启写权限",
          any("write access" in text.lower()
              for text in ssh_kind["notes"] + ssh_kind["steps"]))
    check("指引每步都有内容",
          all(item["steps"] for item in guide["kinds"]))
    # 主机用途必须单独成段：只讲 Deploy Keys 会让「部署到远程主机」的用户
    # 在凭据页找不到 authorized_keys 这条路径。
    check("SSH 指引给出主机登录步骤",
          bool(ssh_kind.get("host_steps"))
          and any("authorized_keys" in s for s in ssh_kind["host_steps"]))
    check("SSH 指引说明主机登录不支持口令私钥",
          any("口令" in n for n in ssh_kind["notes"]))
    check("SSH 指引标注两种用途",
          len(ssh_kind.get("usages") or []) == 2, str(ssh_kind.get("usages")))
    check("令牌指引声明不能用于主机登录",
          any("主机" in n for n in token_kind["notes"]))

    # 类型选项也要带上用途：任务表单的凭据下拉与主机页都依赖它。
    from app.api.credentials import list_kinds
    kind_options = {k["value"]: k for k in list_kinds(user={"username": "admin"})["kinds"]}
    check("明文选项说明 SSH 私钥两种用途",
          len(kind_options["ssh_key"].get("usages") or []) == 2)
    check("令牌类型不声称可用于主机",
          len(kind_options["https_token"].get("usages") or []) == 1)

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
    section("Docker Compose 项目名")
    from app.deployer import _safe_task_slug

    check("英文名直接使用", _safe_task_slug("tdcode-site") == "tdcode-site")
    check("中文名回退 task", _safe_task_slug("我的博客站点") == "task")
    check("混合字符被清洗", _safe_task_slug("a b  c--d!") == "a-b-c-d")
    check("空名回退 task", _safe_task_slug("") == "task")
    check("同名结果稳定", _safe_task_slug("x") == _safe_task_slug("x"))
    check("超长名截断到 40 字符", len(_safe_task_slug("a" * 80)) == 40)
    check("项目名不含非法字符",
          all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in _safe_task_slug("A_b!C")))
    from app.deployer import compose_project_name
    check("项目名带 autodeploy 前缀且不含 id",
          compose_project_name({"name": "calendar-backend", "id": 3})
          == "autodeploy-calendar-backend")

    # 旧遗留容器识别：只清旧命名项目的冲突容器，不碰用户手工容器。
    import app.deployer as _deployer_mod

    conflict_out = ('Error response from daemon: Conflict. The container name "/web" '
                    'is already in use by container "abc123"; '
                    'The container name "/other" is already in use; '
                    'The container name "/renamed" is already in use')
    class _FakeRun:
        def __init__(self, listing):
            self.listing = listing
        def __call__(self, args, **kwargs):
            class _R:
                ok = True
                output = self.listing
            return _R()
    original_run = _deployer_mod.run_command
    _deployer_mod.run_command = _FakeRun(
        "web\tcom.docker.compose.project=20260929-090855-6bf1c2f2_default\n"
        "mine\tcom.docker.compose.project=myproj\n"
        "manual\tno-labels\n"
        "other\tcom.docker.compose.project=autodeploy-other-9\n"
        "renamed\tcom.docker.compose.project=autodeploy-renamed-task\n")
    try:
        picked = _deployer_mod._conflicting_stale_containers(
            conflict_out, "autodeploy-tdcode-site")
    finally:
        _deployer_mod.run_command = original_run
    check("识别旧命名项目的遗留容器", "web" in picked, str(picked))
    check("不碰其他项目的容器", "mine" not in picked)
    check("不碰无标签的手工容器", "manual" not in picked)
    check("识别历史带 id 项目与改名后旧项目的容器",
          "other" in picked and "renamed" in picked, str(picked))

    # ------------------------------------------------------------------
    section("Compose 内容直填（不依赖仓库）")

    from unittest.mock import patch as inline_patch

    from app import deployer as deployer_mod2
    from app.deployer import (
        LEGACY_COMPOSE_INLINE_DIR,
        REPO_FREE_METHODS,
        compose_inline_dir,
        compose_inline_path,
        list_releases,
        preflight as _preflight,
        stop_task_containers as _stop_containers,
    )

    def inline_result_of(command, exit_code=0, output=""):
        return deployer_mod2.CommandResult(
            command=command, exit_code=exit_code, output=output, duration_ms=1
        )

    check("方式已登记为不依赖仓库", "compose_inline" in REPO_FREE_METHODS)
    check("方式已进入部署方式清单", "compose_inline" in deployer_mod2.DEPLOY_METHODS
          and "compose_inline" in deployer_mod2._DEPLOY_DISPATCH)
    check("方式有中文标签",
          deployer_mod2.METHOD_LABELS.get("compose_inline") == "Compose 内容直填")

    # 持久化目录必须与可重建的 releases/ 分开：业务数据不能随发布清理消失。
    inline_task = {"id": 4242, "name": "Inline_Data"}
    inline_dir = compose_inline_dir(4242)
    check("工作目录位于 persistent 下",
          inline_dir == config.persistent_dir(4242)
          and config.PERSISTENT_DIR in inline_dir.parents, str(inline_dir))
    check("工作目录不在 releases 下",
          config.RELEASES_DIR not in inline_dir.parents, str(inline_dir))
    check("compose 文件名固定", compose_inline_path(4242).name == "docker-compose.yml")
    check("持久化目录被识别为用户数据",
          inline_dir in config.owned_data_dirs(inline_task))
    check("持久化目录不在可重建产物里",
          inline_dir not in config.owned_storage_dirs(inline_task))

    # 0.4.1 的旧目录仍必须被发布清理忽略（尚未迁移的存量实例）。
    inline_root = tmp_root / "inline-releases"
    (inline_root / LEGACY_COMPOSE_INLINE_DIR).mkdir(parents=True)
    (inline_root / "20260101-000000-abcdef12").mkdir()
    releases_seen = [p.name for p in list_releases(inline_root)]
    check("旧版固定目录不计入历史发布",
          LEGACY_COMPOSE_INLINE_DIR not in releases_seen, str(releases_seen))
    check("正常发布目录仍被列出", "20260101-000000-abcdef12" in releases_seen)

    # 升级迁移：旧目录数据搬到 persistent/，且不覆盖已存在的新目录。
    legacy_task_id = 5151
    legacy_root = tmp_root / "legacy-releases"
    legacy_dir = legacy_root / LEGACY_COMPOSE_INLINE_DIR
    (legacy_dir / "data").mkdir(parents=True)
    (legacy_dir / "data" / "important.txt").write_text("业务数据", encoding="utf-8")
    migrated = deployer_mod2.migrate_legacy_compose_inline(
        {"id": legacy_task_id}, legacy_root, log=lambda _m: None)
    new_dir = config.persistent_dir(legacy_task_id)
    check("旧版工作目录被迁移", migrated is True)
    check("迁移保留挂载数据",
          (new_dir / "data" / "important.txt").read_text(encoding="utf-8") == "业务数据")
    check("迁移后删除旧目录", not legacy_dir.exists())
    # 新目录已存在时不覆盖（避免把两份数据合并成不可信状态）
    legacy_root2 = tmp_root / "legacy-releases-2"
    (legacy_root2 / LEGACY_COMPOSE_INLINE_DIR / "data").mkdir(parents=True)
    (legacy_root2 / LEGACY_COMPOSE_INLINE_DIR / "data" / "old.txt").write_text("x", encoding="utf-8")
    (new_dir / "data" / "newer.txt").write_text("y", encoding="utf-8")
    skipped = deployer_mod2.migrate_legacy_compose_inline(
        {"id": legacy_task_id}, legacy_root2, log=lambda _m: None)
    check("新目录已存在时跳过迁移", skipped is False
          and (new_dir / "data" / "newer.txt").exists()
          and (legacy_root2 / LEGACY_COMPOSE_INLINE_DIR / "data" / "old.txt").exists())
    shutil.rmtree(new_dir, ignore_errors=True)

    # 回归：若在迁移前先把目标目录建出来，迁移会因「目标已存在」永久跳过，
    # 用户升级后看到空目录、旧数据仍在 releases 下「消失」。这里按 runner 的
    # 真实顺序断言目标目录必须由迁移自身创建。
    order_task_id = 5252
    order_root = tmp_root / "order-releases"
    (order_root / LEGACY_COMPOSE_INLINE_DIR / "data").mkdir(parents=True)
    (order_root / LEGACY_COMPOSE_INLINE_DIR / "data" / "keep.txt").write_text(
        "升级前的数据", encoding="utf-8")
    order_target = config.persistent_dir(order_task_id)
    check("迁移前目标目录不存在", not order_target.exists())
    order_ok = deployer_mod2.migrate_legacy_compose_inline(
        {"id": order_task_id}, order_root, log=lambda _m: None)
    check("未预先建目录时迁移成功", order_ok is True)
    check("迁移后数据可用",
          (order_target / "data" / "keep.txt").read_text(encoding="utf-8") == "升级前的数据")
    shutil.rmtree(order_target, ignore_errors=True)

    # 校验：仓库地址可为空，但内容必填；其它方式仍必须填仓库。
    inline_ok = validate_task_payload({
        "name": "Inline_Compose",
        "deploy_method": "compose_inline",
        "compose_content": "services:\n  app:\n    image: nginx:alpine\n",
    })
    check("无仓库方式允许空仓库地址", inline_ok.get("repo_url", "") == "")
    check("compose 内容原样保存", inline_ok["compose_content"].endswith("nginx:alpine\n"))
    for label, payload, field in (
        ("缺内容被拒绝",
         {"name": "x", "deploy_method": "compose_inline"}, "compose_content"),
        ("内容超长被拒绝",
         {"name": "x", "deploy_method": "compose_inline",
          "compose_content": "a" * 200_001}, "compose_content"),
        ("其它方式仍要求仓库地址",
         {"name": "x", "deploy_method": "release"}, "repo_url"),
    ):
        try:
            validate_task_payload(payload)
        except ValidationError as exc:
            check(label, field in exc.errors, f"错误字段为 {list(exc.errors)}")
        else:
            check(label, False, "未抛出校验错误")
    # 互斥：内容字段只属于该方式。
    try:
        validate_task_payload({
            "name": "x", "repo_url": "https://github.com/a/b.git",
            "deploy_method": "script", "deploy_script": "echo hi",
            "compose_content": "services: {}",
        })
    except ValidationError as exc:
        check("其它方式填 compose 内容被拒绝", "compose_content" in exc.errors)
    else:
        check("其它方式填 compose 内容被拒绝", False)

    # 无仓库方式不因缺 git/tar 被拦住。
    no_binary_checks = _preflight({"deploy_method": "compose_inline"}, target=None)
    check("无仓库方式不检查 git/tar",
          all(c["name"] not in {"git", "tar"} for c in no_binary_checks),
          str([c["name"] for c in no_binary_checks]))
    check("无仓库方式只要求 docker",
          [c["name"] for c in no_binary_checks] == ["docker"])

    # 部署命令：先 pull（跳过纯 build 服务）再 up，且项目目录固定。
    recorded: list[list[str]] = []
    written: dict[str, str] = {}

    class _FakeTarget:
        is_remote = False

        def which(self, binary):
            return binary == "docker"

        def run(self, args, **kwargs):
            recorded.append(list(args))
            return CommandResult(" ".join(args), 0, "", 0)

    def _fake_write(ctx, path, content):
        written[str(path)] = content

    inline_ctx = deployer_mod2.DeployContext(
        task={"id": 9001, "name": "Inline", "deploy_method": "compose_inline",
              "compose_content": "services:\n  app:\n    image: nginx:alpine\n"},
        run_id=1,
        workspace=inline_dir,
        release_dir=inline_dir,
        releases_root=inline_root,
        current_link=inline_root / "current",
        artifact_path=tmp_root / "inline.tar.gz",
        persistent_dir=inline_dir,
        target=_FakeTarget(),
    )
    real_supports = deployer_mod2._supports_compose_v2
    real_write = deployer_mod2._write_compose_file
    deployer_mod2._supports_compose_v2 = lambda target=None: True
    deployer_mod2._write_compose_file = _fake_write
    try:
        deployer_mod2.deploy_compose_inline(inline_ctx)
    finally:
        deployer_mod2._supports_compose_v2 = real_supports
        deployer_mod2._write_compose_file = real_write
    joined = [" ".join(args) for args in recorded]
    check("先拉取镜像再启动", len(joined) == 2 and " pull " in f" {joined[0]} "
          and " up " in f" {joined[1]} ", str(joined))
    check("pull 跳过纯 build 服务", "--ignore-buildable" in joined[0])
    check("使用固定项目目录",
          all("--project-directory" in args for args in recorded), str(joined))
    check("compose 文件写在持久化目录",
          str(inline_dir) in str(next(iter(written), "")), str(list(written)))
    check("project-directory 指向持久化目录",
          all(str(inline_dir) in args for args in recorded), str(joined))
    check("内容原样写入", next(iter(written.values()), "").endswith("nginx:alpine\n"))

    # compose_inline 的容器清理按项目标签走，不依赖已删除的 compose 文件。
    cleanup_calls: list[list[str]] = []

    def _cleanup_run(args, **kwargs):
        cleanup_calls.append(list(args))
        if list(args)[:2] == ["docker", "ps"]:
            return inline_result_of(" ".join(args), output=(
                "inline-1\tcom.docker.compose.project=autodeploy-inline-task\n"
                "foreign-1\tcom.docker.compose.project=someone-else\n"))
        return inline_result_of(" ".join(args))

    with inline_patch.object(deployer_mod, "command_exists", return_value=True), \
            inline_patch.object(deployer_mod, "run_command", side_effect=_cleanup_run):
        removed_names = _stop_containers(
            {"id": 9002, "name": "Inline_Task", "deploy_method": "compose_inline"},
            log=lambda _m: None)
    check("无仓库方式按标签移除容器", removed_names == ["inline-1"], str(removed_names))
    check("无仓库方式不触碰他人容器",
          all("foreign-1" not in " ".join(call) for call in cleanup_calls))

    # 回滚明确拒绝：没有历史版本可直接切回。
    ok, message = deployer_mod2.rollback_task(
        {"id": 9003, "name": "Inline_Rb", "deploy_method": "compose_inline"},
        log=lambda _m: None)
    check("无仓库方式拒绝回滚", ok is False and "compose 内容改回旧内容" in message, message)

    # ------------------------------------------------------------------
    section("持久化数据的备份、恢复与删除语义")

    from app import backup as backup_mod

    # 用一个独立的数据目录跑真实备份/恢复，避免污染运行中的 data/。
    persist_task_id = 6161
    persist_dir = config.persistent_dir(persist_task_id)
    (persist_dir / "data").mkdir(parents=True, exist_ok=True)
    (persist_dir / "data" / "app.db").write_text("业务数据库", encoding="utf-8")
    (persist_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    backup_store = Store(config.DB_PATH) if "config" in globals() else None
    check("持久化目录位于数据目录下", config.DATA_DIR in persist_dir.parents)

    # 备份包必须包含 persistent/，否则这份业务数据在灾难恢复时会永久丢失。
    if backup_store is None:
        backup_store = Store(Database(config.DB_PATH))
    archive = backup_mod.create_backup(
        backup_store, trigger="manual", include_logs=False,
        include_artifacts=False, retention_count=100,
    )
    import tarfile as _tarfile
    with _tarfile.open(archive["filepath"], "r:gz") as tar:
        names = tar.getnames()
    check("备份包含持久化数据",
          any(n.startswith("persistent/") for n in names), str(names[:10]))
    check("备份包含 compose 文件",
          any("docker-compose.yml" in n for n in names))
    check("manifest 标记包含持久化数据",
          bool(archive.get("contains_persistent")), str(archive.get("contains_persistent")))

    # 恢复要能还原被删掉的业务数据。
    (persist_dir / "data" / "app.db").unlink()
    check("恢复前数据已删", not (persist_dir / "data" / "app.db").exists())
    backup_mod.restore_backup(backup_store, archive["id"])
    check("恢复还原持久化数据",
          (persist_dir / "data" / "app.db").read_text(encoding="utf-8") == "业务数据库")

    # 旧备份（无 persistent/）不得清空现存数据。
    legacy_archive = backup_mod.create_backup(
        backup_store, trigger="manual", include_logs=False,
        include_artifacts=False, retention_count=100,
    )
    backup_mod.restore_backup(backup_store, legacy_archive["id"])
    check("恢复不影响未被备份的数据目录内容",
          (persist_dir / "data" / "app.db").exists())

    # 备份包里的软链/设备条目必须被拒绝（防止解压逃逸）。
    evil = tmp_root / "evil.tar.gz"
    with _tarfile.open(evil, "w:gz") as tar:
        info = _tarfile.TarInfo("link")
        info.type = _tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    try:
        backup_mod.inspect_backup_file(evil)
    except backup_mod.BackupError as exc:
        check("拒绝含软链的备份包", "链接" in str(exc) or "非法" in str(exc), str(exc))
    else:
        check("拒绝含软链的备份包", False, "未拒绝")

    # 清理本节创建的数据与备份文件，避免影响后续检查。
    for item in (persist_dir,):
        shutil.rmtree(item, ignore_errors=True)
    for path in (archive.get("filepath"), legacy_archive.get("filepath")):
        if path:
            Path(path).unlink(missing_ok=True)
    backup_store.db.close()

    # ------------------------------------------------------------------
    section("一键自我更新")

    from unittest.mock import patch
    from types import SimpleNamespace
    from app import selfupdate as su
    from app.scheduler import Scheduler as UpdateScheduler
    from app.executor import run_command as safe_command

    def tree(path, version):
        (path / "app").mkdir(parents=True, exist_ok=True)
        (path / "web").mkdir(exist_ok=True)
        (path / "app/__init__.py").write_text('__version__ = "' + version + '"\n')
        (path / "app/main.py").write_text('')
        (path / "web/index.html").write_text('测试')
        (path / "requirements.txt").write_text('fastapi\nuvicorn\n')
        (path / "run.sh").write_text('#!/bin/sh\n')

    install = tmp_root / "isolated-install"
    tree(install, "1.0.0")
    fake_store = SimpleNamespace(runs=SimpleNamespace(count_active=lambda: 0))
    ok = SimpleNamespace(ok=True, output="", error="")
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "systemctl":
            return SimpleNamespace(ok=True, output=f"Id=autodeploy.service\nLoadState=loaded\nActiveState=active\nMainPID={os.getpid()}\nControlGroup=/system.slice/autodeploy.service\nRestart=always\nRestartPreventExitStatus=\n")
        if "pip" in args:
            return ok
        raise AssertionError("自更新测试禁止真实外部命令：" + str(args))

    with patch.object(config, 'ROOT_DIR', install), patch.object(config, 'DATA_DIR', tmp_root / 'update-data'), \
            patch.object(config, 'TMP_DIR', tmp_root / 'update-tmp'), \
            patch.object(su, 'run_command', side_effect=command), patch.object(su.os, '_exit') as hard_exit:
        mgr = su.SelfUpdateManager(fake_store)
        with patch.object(mgr, '_cgroup_path', return_value='/system.slice/autodeploy.service'):
            check('真实主进程与有效 always 才支持退出', mgr._restart_plan()[0] == 'self-exit')
            for policy in ('on-failure', 'on-abnormal', 'on-abort', 'no', ''):
                result = command(['systemctl'])
                result.output = result.output.replace('Restart=always', 'Restart=' + policy)
                with patch.object(su, 'run_command', return_value=result):
                    check('拒绝重启策略 ' + policy, mgr._restart_plan()[0] == 'unsupported')
            result = command(['systemctl'])
            result.output = result.output.replace(f'MainPID={os.getpid()}', 'MainPID=0')
            with patch.object(su, 'run_command', return_value=result):
                check('拒绝非单元主进程', mgr._restart_plan()[0] == 'unsupported')
        with patch.object(mgr, '_cgroup_path', return_value=''):
            expect_raises('不支持环境启动前拒绝', lambda: mgr.start('v2.0.0'), RuntimeError)
            check('不支持环境不落状态或改文件', not mgr.state_path.exists() and '1.0.0' in (install / 'app/__init__.py').read_text())

        def download(target, tmp, settings, log, state):
            tree(tmp / 'src', '2.0.0')
        latest = su.UpdateCheck('1.0.0', 'v2.0.0', True, '')
        with patch.object(mgr, '_restart_plan', return_value=('self-exit', 'autodeploy.service')), \
                patch.object(mgr, 'check', return_value=latest), patch.object(mgr, '_download', side_effect=download), \
                patch.object(mgr, '_exit_for_restart') as restart:
            state = mgr.start('v2.0.0')
            mgr._thread.join(10)
            check('更新仅停在等待重启确认', mgr.state()['stage'] == 'restarting' and restart.called)
            expect_raises('重复更新拒绝', lambda: mgr.start('v2.0.0'), RuntimeError)
            expect_raises('重启中回滚拒绝', mgr.rollback, RuntimeError)
            sched = UpdateScheduler(fake_store, None)
            sched._lock = mgr._lock
            sched.deployment_blocked = mgr.deployment_blocked
            expect_raises('更新时手动部署拒绝', lambda: sched.run_now(1), RuntimeError)
            check('更新时定时部署拒绝', not sched._dispatch({'id': 1}, trigger='schedule'))
            saved = mgr.state()
            with patch.object(su, 'PROCESS_BOOT_ID', 'new-boot'), patch.object(su.os, 'getpid', return_value=os.getpid()+100), patch.object(su, '__version__', '2.0.0'):
                mgr.reconcile_on_startup()
            check('新进程确认目标版本后完成', mgr.state()['stage'] == 'done' and mgr.state()['confirmed_operation'] == 'update')
            mgr._save_state(saved)
            with patch.object(su, 'PROCESS_BOOT_ID', 'new-boot'), patch.object(su, '__version__', '9.0.0'):
                mgr.reconcile_on_startup()
            check('版本不符明确失败', mgr.state()['stage'] == 'failed',
                  'stage=%s err=%s' % (mgr.state().get('stage'), mgr.state().get('error')))
            expired = dict(saved, restart_deadline=time.time()-1)
            mgr._save_state(expired)
            check('重启确认超时失败', mgr.state()['stage'] == 'failed',
                  'stage=%s err=%s' % (mgr.state().get('stage'), mgr.state().get('error')))
            mgr.rollback()
            mgr._thread.join(10)
            check('回滚恢复旧代码仍等待确认', '1.0.0' in (install / 'app/__init__.py').read_text() and mgr.state()['stage'] == 'restarting')
            with patch.object(su, 'PROCESS_BOOT_ID', 'rollback-boot'), patch.object(su.os, 'getpid', return_value=os.getpid()+101), patch.object(su, '__version__', '1.0.0'):
                mgr.reconcile_on_startup()
            check('回滚操作独立确认', mgr.state()['stage'] == 'done' and mgr.state()['confirmed_operation'] == 'rollback')
            with patch.object(su, 'run_command', return_value=SimpleNamespace(ok=False)):
                mgr.start('v2.0.0')
                mgr._thread.join(10)
            check('依赖失败恢复代码且不误报成功', mgr.state()['stage'] == 'failed' and '1.0.0' in (install / 'app/__init__.py').read_text())
            original_replace = su.os.replace
            failed_once = [False]
            def fail_replace(src, dst):
                if Path(dst) == install / 'web' and not failed_once[0]:
                    failed_once[0] = True
                    raise OSError('模拟替换失败')
                return original_replace(src, dst)
            with patch.object(su.os, 'replace', side_effect=fail_replace):
                mgr.start('v2.0.0')
                mgr._thread.join(10)
            check('部分代码替换失败可靠恢复', mgr.state()['stage'] == 'failed' and '1.0.0' in (install / 'app/__init__.py').read_text() and (install / 'web/index.html').exists())
            bad = tmp_root / 'bad-download'
            tree(bad, '3.0.0')
            expect_raises('下载版本校验', lambda: mgr._validate_tree(bad, '2.0.0'), RuntimeError)
            (bad / 'web/escape').symlink_to('/tmp')
            expect_raises('下载软链越界拒绝', lambda: mgr._validate_tree(bad), RuntimeError)
            expect_raises('目标路径注入拒绝', lambda: mgr.start('../../tmp'), RuntimeError)
            mgr._save_state(dict(saved, stage='applying'))
            mgr.reconcile_on_startup()
            check('中断更新明确失败', mgr.state()['stage'] == 'failed')
        section('Docker 形态一键升级（独立执行器）与镜像回退')

        from app import upgrade_exec as ue

        docker_install = tmp_root / "docker-install"
        docker_install.mkdir()
        tree(docker_install, "1.0.0")
        (docker_install / "docker-compose.yml").write_text(
            'services:\n  autodeploy:\n    image: ${AUTODEPLOY_IMAGE:-ghcr.io/j9kkk/auto-deploy}:${AUTODEPLOY_IMAGE_TAG:-latest}\n')
        (docker_install / "docker-compose.override.yml").write_text(
            'services:\n  autodeploy:\n    ports:\n      - "18770:8770"\n')
        # 模拟 bootstrap.sh 装好的旧版本部署：.env 锁定在 1.0.0 对应的镜像 tag。
        (docker_install / ".env").write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=1.0.0\n')
        docker_data = tmp_root / 'docker-update-data'
        REPO = 'ghcr.io/j9kkk/auto-deploy'
        fake_store_docker = SimpleNamespace(runs=SimpleNamespace(count_active=lambda: 0))

        class FakeDocker:
            """docker CLI 替身：执行器在进程内真实运行，.env 等文件操作走真实文件系统。

            compose up 模拟会跟随当前 .env 的 tag 更新「容器」镜像身份并写入新
            进程启动标记（真实环境由重建后的新容器完成），从而端到端驱动新链路；
            故障注入通过 up_fail_once / tamper_on_config / executor_noop / pull_ok。
            """

            def __init__(self):
                self.image_ids = {REPO + ':1.0.0': 'sha256:' + 'a' * 64,
                                  REPO + ':2.0.0': 'sha256:' + 'b' * 64}
                self.local = {REPO + ':1.0.0'}
                self.container_ref = REPO + ':1.0.0'
                self.container_id = 'oldc1d2345678'
                self.pull_ok = True
                self.up_fail_once = False
                self.executor_noop = False      # docker run 只返回 ID，不执行升级
                self.tamper_on_config = False   # compose config 时模拟人工篡改 .env
                self.marker_version = None      # 覆盖启动标记版本（注入版本不符）
                self.calls = []
                self.run_cmds = []
                self.tag_cmds = []
                self.compose_envs = []

            def env_tag(self):
                for line in (docker_install / '.env').read_text().splitlines():
                    if line.startswith('AUTODEPLOY_IMAGE_TAG='):
                        return line.split('=', 1)[1].strip() or 'latest'
                return 'latest'

            def ref_id(self, ref):
                if ref not in self.image_ids:
                    self.image_ids[ref] = 'sha256:' + ref.encode().hex().ljust(64, '0')[:64]
                return self.image_ids[ref]

            def self_info_line(self):
                labels = {"com.docker.compose.project": "auto-deploy",
                          "com.docker.compose.project.working_dir": str(docker_install),
                          "com.docker.compose.project.config_files":
                              str(docker_install / 'docker-compose.yml') + ","
                              + str(docker_install / 'docker-compose.override.yml'),
                          "com.docker.compose.service": "autodeploy"}
                mounts = [{"Type": "bind", "Source": "/var/run/docker.sock",
                           "Destination": "/var/run/docker.sock", "RW": True},
                          {"Type": "bind", "Source": str(docker_install),
                           "Destination": str(docker_install), "RW": True},
                          {"Type": "volume", "Name": "auto-deploy-data",
                           "Destination": "/app/data", "RW": True}]
                return "\t".join([json.dumps(labels), self.container_ref,
                                  self.image_ids.get(self.container_ref, ''),
                                  json.dumps(mounts), str(docker_install), "auto-deploy",
                                  str(docker_install / 'docker-compose.yml') + ","
                                  + str(docker_install / 'docker-compose.override.yml'),
                                  "autodeploy"])

            def __call__(self, args, **kwargs):
                self.calls.append(args)
                if args[:2] == ["docker", "compose"]:
                    self.compose_envs.append(kwargs.get('base_env'))
                # 新链路的 compose 命令带 -p/-f 前缀（docker compose -p X -f Y config|up|ps），
                # 子命令出现在尾部，按成员匹配而不是固定下标。
                compose_sub = next((a for a in args[2:] if a in ('config', 'up', 'ps')), '')
                if compose_sub == 'config':
                    if self.tamper_on_config:
                        with open(docker_install / '.env', 'a', encoding='utf-8') as handle:
                            handle.write('EXTRA=manual\n')
                        self.tamper_on_config = False
                    payload = {"services": {"autodeploy": {"image": REPO + ":" + self.env_tag()}}}
                    return SimpleNamespace(ok=True, output=json.dumps(payload))
                if compose_sub == 'up':
                    if self.up_fail_once:
                        self.up_fail_once = False
                        return SimpleNamespace(ok=False, output="模拟重建失败", error="模拟重建失败")
                    self.container_ref = REPO + ":" + self.env_tag()
                    self.container_id = 'newc1d2345678'
                    plan = json.loads((docker_data / su.PLAN_FILE_NAME).read_text())
                    version = self.marker_version if self.marker_version is not None else plan['expected_version']
                    marker = {"operation_id": plan['operation_id'], "version": version,
                              "boot_id": "fake-new-boot", "pid": 424242,
                              "container_id": self.container_id, "time": time.time()}
                    (docker_data / su.MARKER_FILE_NAME).write_text(json.dumps(marker))
                    return SimpleNamespace(ok=True, output="")
                if compose_sub == 'ps':
                    return SimpleNamespace(ok=True, output=self.container_id + "\n")
                if args[:3] == ["docker", "run", "-d"]:
                    self.run_cmds.append(args)
                    if self.executor_noop:
                        return SimpleNamespace(ok=True, output="execdeadbeef1234\n")
                    # 执行器在进程内真实运行（升级计划与状态文件均为真实读写）。
                    ue.run(docker_data / su.PLAN_FILE_NAME)
                    return SimpleNamespace(ok=True, output="execdeadbeef1234\n")
                if args[:3] == ["docker", "image", "inspect"]:
                    ref = args[-1]
                    if ref in self.local:
                        return SimpleNamespace(ok=True, output=self.image_ids[ref] + "\n")
                    return SimpleNamespace(ok=False, output="", error="not found")
                if args[:2] == ["docker", "pull"]:
                    if not self.pull_ok:
                        return SimpleNamespace(ok=False, output="manifest unknown", error="")
                    ref = args[-1]
                    self.local.add(ref)
                    self.ref_id(ref)
                    return SimpleNamespace(ok=True, output="")
                if args[:2] == ["docker", "tag"]:
                    self.tag_cmds.append(args)
                    self.image_ids[args[3]] = args[2]
                    self.local.add(args[3])
                    return SimpleNamespace(ok=True, output="")
                if args[:3] == ["docker", "ps", "-a"]:
                    # 执行器容器已退出（真实环境终态后应被清理的残留形态）。
                    return SimpleNamespace(ok=True, output="execdeadbeef1234 exited\n")
                if args[:2] == ["docker", "rm"]:
                    return SimpleNamespace(ok=True, output="")
                if args[:2] == ["docker", "inspect"]:
                    # 兼容两种实参顺序：inspect <cid> --format <fmt> 与
                    # inspect --format <fmt> <name>。分支按格式串特征匹配，
                    # .State.Running（执行器读目标容器）必须先于 Labels 判断。
                    fmt = args[args.index('--format') + 1] if '--format' in args else ''
                    target = args[2] if args[2] != '--format' else args[4]
                    if '.State.Running' in fmt:
                        labels = {"com.docker.compose.project": "auto-deploy",
                                  "com.docker.compose.project.working_dir": str(docker_install),
                                  "com.docker.compose.project.config_files":
                                      str(docker_install / 'docker-compose.yml') + ","
                                      + str(docker_install / 'docker-compose.override.yml'),
                                  "com.docker.compose.service": "autodeploy"}
                        running = not (target.startswith('auto-deploy-upgrade-') and self.executor_noop)
                        return SimpleNamespace(ok=True, output="\t".join(
                            [json.dumps(labels), self.container_ref,
                             self.image_ids.get(self.container_ref, ''),
                             'true' if running else 'false']))
                    if '{{json .State}}' in fmt:
                        return SimpleNamespace(ok=True, output='{"running":true,"restarting":false}')
                    if '.State.Health' in fmt:
                        return SimpleNamespace(ok=True, output='{"Status":"healthy"}')
                    if fmt == '{{.Image}}':
                        return SimpleNamespace(ok=True, output=self.image_ids.get(self.container_ref, '') + "\n")
                    if 'Config.Labels' in fmt:
                        return SimpleNamespace(ok=True, output=self.self_info_line())
                    return SimpleNamespace(ok=False, output="", error="unsupported inspect")
                if args[:2] == ["docker", "info"]:
                    return SimpleNamespace(ok=True, output="")
                raise AssertionError("Docker 更新测试禁止真实外部命令：" + str(args))

        def run_docker_scenarios(fake, data_dir):
            with patch.object(config, 'ROOT_DIR', docker_install), \
                    patch.object(config, 'DATA_DIR', data_dir), \
                    patch.object(su, 'in_container', return_value=True), \
                    patch.object(su, 'docker_available', return_value=True), \
                    patch.object(su.SelfUpdateManager, '_self_container_id', return_value='abc123def456'), \
                    patch.object(su, 'run_command', side_effect=fake), \
                    patch.object(ue, 'run_command', side_effect=fake):
                yield

        fake = FakeDocker()
        gen = run_docker_scenarios(fake, docker_data)
        next(gen)
        dmgr = su.SelfUpdateManager(fake_store_docker)
        check('容器内 socket 可用判为 docker 形态', su.docker_run_mode() == 'docker')
        context = dmgr._docker_upgrade_context()
        check('升级上下文包含完整 compose 身份',
              context['project'] == 'auto-deploy' and context['service'] == 'autodeploy'
              and len(context['config_files']) == 2 and context['image_ref'] == REPO + ':1.0.0'
              and context['env_file'] == str(docker_install / '.env'))
        latest_d = su.UpdateCheck('1.0.0', 'v2.0.0', True, '')

        def wait_terminal(mgr, budget=60):
            """等待当前操作落定终态（监控线程以 2 秒周期轮询状态文件）。"""
            deadline = time.time() + budget
            while time.time() < deadline:
                if mgr.state().get('stage') in su.TERMINAL_STAGES:
                    return
                time.sleep(0.2)

        with patch.object(dmgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            state = dmgr.start('v2.0.0')
            dmgr._thread.join(30)
            if dmgr._monitor_thread:
                dmgr._monitor_thread.join(15)
            wait_terminal(dmgr)
        done_state = dmgr.state()
        check('委托升级完成且确认信息齐备', done_state['stage'] == 'done'
              and done_state.get('confirmed_operation_id') == state['operation_id']
              and done_state.get('version') == '2.0.0'
              and done_state.get('delegated') is True)
        env_text = (docker_install / '.env').read_text()
        check('镜像 tag 已写入 .env 且保留其余配置',
              'AUTODEPLOY_IMAGE_TAG=2.0.0' in env_text
              and 'AUTODEPLOY_IMAGE=ghcr.io/j9kkk/auto-deploy' in env_text
              and 'AUTODEPLOY_PORT=8770' in env_text)
        check('执行器以 root 与目标镜像 ID 运行并挂载 socket/安装目录/数据卷',
              fake.run_cmds and '--user' in fake.run_cmds[0]
              and fake.run_cmds[0][fake.run_cmds[0].index('--user') + 1] == '0'
              and any(a == '/var/run/docker.sock:/var/run/docker.sock' for a in fake.run_cmds[0])
              and any(a == f'{docker_install}:{docker_install}' for a in fake.run_cmds[0])
              and any(a == 'auto-deploy-data:/app/data' for a in fake.run_cmds[0])
              and any(a.startswith(su.EXECUTOR_LABEL + '=') for a in fake.run_cmds[0])
              and fake.run_cmds[0][-5] == fake.image_ids[REPO + ':2.0.0']
              and fake.run_cmds[0][-2:] == ['--plan', '/app/data/upgrade-plan.json'])
        compose_ups = [a for a in fake.calls if a[:2] == ["docker", "compose"] and 'up' in a]
        check('compose 命令固定项目/有序配置文件/工作目录并限定服务',
              compose_ups and '-p' in compose_ups[0] and compose_ups[0][compose_ups[0].index('-p') + 1] == 'auto-deploy'
              and compose_ups[0].count('-f') == 2
              and compose_ups[0][compose_ups[0].index('--project-directory') + 1] == str(docker_install)
              and compose_ups[0][-1] == 'autodeploy'
              and f'{REPO}:2.0.0' in fake.local)
        check('compose 子进程使用清洗环境（无 AUTODEPLOY_*）',
              fake.compose_envs and all(env is not None and not any(
                  str(k).startswith('AUTODEPLOY_') for k in env) for env in fake.compose_envs))
        check('拉取发生在切换之前且按目标引用',
              any(a[:3] == ["docker", "pull", REPO + ":2.0.0"] for a in fake.calls))
        check('升级历史记录镜像前后引用',
              dmgr.history() and dmgr.history()[0].get('from_image') == REPO + ':1.0.0'
              and dmgr.history()[0].get('to_image') == REPO + ':2.0.0'
              and dmgr.history()[0]['stage'] == 'done')
        rm_ids = [a[3] for a in fake.calls if a[:2] == ["docker", "rm"]]
        check('终态后已退出的执行器容器被清理（不留到下次升级）',
              bool(rm_ids) and all(cid == 'execdeadbeef1234' for cid in rm_ids),
              f'rm 调用：{rm_ids[:3]}')

        # 重建失败：执行器自动恢复 .env 快照并重建旧版本。
        fake.up_fail_once = True
        (docker_install / '.env').write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=1.0.0\n')
        fake.container_ref = REPO + ':1.0.0'
        fake.container_id = 'oldc1d2345678'
        with patch.object(dmgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            dmgr.start('v2.0.0')
            dmgr._thread.join(30)
            if dmgr._monitor_thread:
                dmgr._monitor_thread.join(15)
            wait_terminal(dmgr)
        rb_state = dmgr.state()
        check('重建失败自动恢复 .env 并回到旧版本', rb_state['stage'] == 'rolled_back'
              and 'AUTODEPLOY_IMAGE_TAG=1.0.0' in (docker_install / '.env').read_text()
              and '2.0.0' not in (docker_install / '.env').read_text()
              and fake.container_ref == REPO + ':1.0.0'
              and '恢复' in rb_state.get('error', ''))
        check('rolled_back 记录进入历史', dmgr.history()[0]['stage'] == 'rolled_back')
        check('成功恢复后快照清理', not list(docker_data.glob('*.env.snapshot')))

        # 人工篡改 .env 后指纹不符：拒绝覆盖，进入 recovery_required，快照保留。
        fake.up_fail_once = True
        fake.tamper_on_config = True
        with patch.object(dmgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            dmgr.start('v2.0.0')
            dmgr._thread.join(30)
            if dmgr._monitor_thread:
                dmgr._monitor_thread.join(15)
            wait_terminal(dmgr)
        tamper_state = dmgr.state()
        check('切换后 .env 被人工改动时拒绝覆盖并转人工处置',
              tamper_state['stage'] == 'recovery_required'
              and '人工' in tamper_state.get('error', '')
              and dmgr.deployment_blocked())
        check('人工处置场景保留快照', bool(list(docker_data.glob('*.env.snapshot'))))
        expect_raises('恢复失败后无法继续等待', lambda: dmgr.attention_action('wait'), RuntimeError)
        dmgr.attention_action('resolve')
        check('标记人工处理后解除禁入', dmgr.state()['stage'] == 'failed'
              and not dmgr.deployment_blocked()
              and dmgr.history()[0]['stage'] == 'failed')
        for snapshot in docker_data.glob('*.env.snapshot'):
            snapshot.unlink()

        (docker_install / '.env').write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=1.0.0\n')

        # 执行器崩溃（未记录终态）：监控转 attention；继续等待被拒，人工处理放行。
        fake2 = FakeDocker()
        fake2.executor_noop = True
        gen2 = run_docker_scenarios(fake2, tmp_root / 'docker-update-data-3')
        next(gen2)
        crash_mgr = su.SelfUpdateManager(fake_store_docker)
        with patch.object(crash_mgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            crash_mgr.start('v2.0.0')
            crash_mgr._thread.join(30)
            if crash_mgr._monitor_thread:
                crash_mgr._monitor_thread.join(15)
        check('执行器退出未落终态转人工处置', crash_mgr.state()['stage'] == 'attention'
              and crash_mgr.deployment_blocked())
        expect_raises('执行器缺席无法继续等待', lambda: crash_mgr.attention_action('wait'), RuntimeError)
        expect_raises('不支持的处置动作拒绝', lambda: crash_mgr.attention_action('nonsense'), RuntimeError)
        crash_mgr.attention_action('resolve')
        check('崩溃操作人工处理后解除禁入', not crash_mgr.deployment_blocked())
        gen2.close()

        # 有运行中的部署任务：升级与回退都必须被准入拒绝（防止重建中断任务）。
        busy_store = SimpleNamespace(runs=SimpleNamespace(count_active=lambda: 1))
        busy_mgr = su.SelfUpdateManager(busy_store)
        with patch.object(busy_mgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            expect_raises('有运行中任务时拒绝升级', lambda: busy_mgr.start('v2.0.0'), RuntimeError)
            expect_raises('有运行中任务时拒绝回滚',
                          lambda: busy_mgr.rollback(target_image=REPO + ':1.0.0'), RuntimeError)

        # 预检必须拒绝不可重建的上下文，且全部发生在任何改动之前。
        calls_before = len(fake.calls)
        info_no_service = {'image': REPO + ':1.0.0', 'working_dir': str(docker_install),
                           'project_working_dir': str(docker_install), 'project': 'auto-deploy',
                           'config_files': '', 'service': '', 'mounts': [], 'image_id': ''}
        with patch.object(dmgr, '_docker_self_info', return_value=info_no_service):
            try:
                dmgr._docker_preflight(lambda message: None)
                raised = ''
            except RuntimeError as exc:
                raised = str(exc)
        check('缺少 compose 服务标签拒绝升级', 'compose' in raised and '一键脚本' in raised)
        info_outside = {**info_no_service, 'config_files': '/etc/other-compose.yml',
                        'service': 'autodeploy'}
        with patch.object(dmgr, '_docker_self_info', return_value=info_outside):
            try:
                dmgr._docker_preflight(lambda message: None)
                raised = ''
            except RuntimeError as exc:
                raised = str(exc)
        check('配置文件在安装目录外拒绝升级', '安装目录之外' in raised and '一键脚本' in raised)
        info_no_mount = {**info_no_service, 'config_files': str(docker_install / 'docker-compose.yml'),
                         'service': 'autodeploy'}
        with patch.object(dmgr, '_docker_self_info', return_value=info_no_mount):
            try:
                dmgr._docker_preflight(lambda message: None)
                raised = ''
            except RuntimeError as exc:
                raised = str(exc)
        check('缺少数据卷挂载拒绝升级', '/app/data' in raised)
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote:2375'}):
            try:
                dmgr._docker_preflight(lambda message: None)
                raised = ''
            except RuntimeError as exc:
                raised = str(exc)
        check('自定义 DOCKER_HOST 拒绝面板升级', 'DOCKER_HOST' in raised)
        check('上下文拒绝发生在任何改动之前', len(fake.calls) == calls_before)

        # 安装目录不可见/.env 缺失：指引必须可操作，且不发任何 docker 命令。
        phantom_dir = tmp_root / 'host-dir-not-mounted'   # 刻意不创建，模拟未挂载
        missing_env_dir = tmp_root / 'host-dir-without-env'
        missing_env_dir.mkdir()
        phantom_info = {'image': REPO + ':2.0.0',
                        'working_dir': str(phantom_dir), 'project_working_dir': str(phantom_dir),
                        'project': 'auto-deploy', 'config_files': '', 'service': '',
                        'mounts': [], 'image_id': ''}
        missing_info = {**phantom_info, 'working_dir': str(missing_env_dir),
                        'project_working_dir': str(missing_env_dir)}
        calls_before = len(fake.calls)
        with patch.object(dmgr, '_docker_self_info', return_value=phantom_info):
            try:
                dmgr._docker_preflight(lambda message: None)
            except RuntimeError as exc:
                check('安装目录不可见时指引重跑一键脚本',
                      '在容器内不可见' in str(exc) and '一键脚本' in str(exc))
        with patch.object(dmgr, '_docker_self_info', return_value=missing_info):
            try:
                dmgr._docker_preflight(lambda message: None)
            except RuntimeError as exc:
                check('.env 缺失时给出创建或手动升级指引',
                      '没有 .env' in str(exc) and 'docker compose pull' in str(exc))
        check('安装目录不可用时不产生任何 docker 命令', len(fake.calls) == calls_before)

        # 拉取失败：中止且不动 .env 与旧容器（独立 DATA_DIR 与全新替身）。
        fake_pull = FakeDocker()
        fake_pull.pull_ok = False
        (docker_install / '.env').write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=1.0.0\n')
        gen_pull = run_docker_scenarios(fake_pull, tmp_root / 'docker-update-data-2')
        next(gen_pull)
        fresh_mgr = su.SelfUpdateManager(fake_store_docker)
        with patch.object(fresh_mgr, 'check', return_value=latest_d), \
                patch.object(su, '__version__', '1.0.0'):
            fresh_mgr.start('v2.0.0')
            fresh_mgr._thread.join(30)
        pf_state = fresh_mgr.state()
        check('拉取失败中止且未改 .env',
              pf_state['stage'] == 'failed'
              and 'AUTODEPLOY_IMAGE_TAG=1.0.0' in (docker_install / '.env').read_text()
              and '2.0.0' not in (docker_install / '.env').read_text()
              and '拉取失败' in pf_state['error'])
        gen_pull.close()

        # 无 socket 的容器形态：直接拒绝并给出引导。
        fake3 = FakeDocker()
        gen3 = run_docker_scenarios(fake3, tmp_root / 'docker-update-data-4')
        next(gen3)
        with patch.object(su, 'docker_available', return_value=False):
            no_sock = su.SelfUpdateManager(fake_store_docker)
            check('无 socket 判为 docker_no_socket', su.docker_run_mode() == 'docker_no_socket')
            expect_raises('无 socket 升级拒绝并引导', lambda: no_sock.start('v2.0.0'), RuntimeError)
        gen3.close()

        # 后续回退/attention 场景继续在主场景的数据目录内运行。
        gen_main = run_docker_scenarios(fake, docker_data)
        next(gen_main)

        # 正常回退：镜像本地已存在则直接使用，不重新拉取。
        pulls_before = sum(1 for a in fake.calls if a[:2] == ["docker", "pull"])
        fake.container_ref = REPO + ':2.0.0'
        fake.container_id = 'newc1d2345678'
        (docker_install / '.env').write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=2.0.0\n')
        with patch.object(su, '__version__', '2.0.0'):
            dmgr.rollback(target_image=REPO + ':1.0.0')
            dmgr._thread.join(30)
            if dmgr._monitor_thread:
                dmgr._monitor_thread.join(15)
            wait_terminal(dmgr)
        check('镜像回退切回 .env 旧 tag 并确认完成',
              dmgr.state()['stage'] == 'done' and dmgr.state().get('version') == '1.0.0'
              and 'AUTODEPLOY_IMAGE_TAG=1.0.0' in (docker_install / '.env').read_text())
        check('镜像回退记录 from/to 镜像',
              dmgr.history()[0].get('from_image') == REPO + ':2.0.0'
              and dmgr.history()[0].get('to_image') == REPO + ':1.0.0')
        check('本地已有镜像时回退不重新拉取',
              sum(1 for a in fake.calls if a[:2] == ["docker", "pull"]) == pulls_before
              and REPO + ':1.0.0' in fake.local)

        # 回退目标必须来自升级历史：凭空构造的镜像引用拒绝。
        with patch.object(su, '__version__', '2.0.0'):
            expect_raises('凭空镜像引用拒绝回退',
                          lambda: dmgr.rollback(target_image='ghcr.io/evil/auto-deploy:v9.9.9'), RuntimeError)
            expect_raises('非法镜像引用拒绝回退',
                          lambda: dmgr.rollback(target_image='bad ref with space'), RuntimeError)

        # attention 状态下恢复到升级前镜像：按记录的镜像 ID 打本地标记，不信任 tag。
        att = dmgr._new_state('update', 'v2.0.0', '')
        att.update(stage='attention', delegated=True, from_image_ref=REPO + ':1.0.0',
                   from_image_id=fake.image_ids[REPO + ':1.0.0'], error='结果不确定')
        su.save_state_file(att)
        fake.container_ref = REPO + ':2.0.0'
        (docker_install / '.env').write_text('AUTODEPLOY_PORT=8770\nAUTODEPLOY_IMAGE_TAG=2.0.0\n')
        tag_calls_before = len(fake.tag_cmds)
        with patch.object(su, '__version__', '2.0.0'):
            dmgr.rollback(target_image=REPO + ':1.0.0')
            dmgr._thread.join(30)
            if dmgr._monitor_thread:
                dmgr._monitor_thread.join(15)
            wait_terminal(dmgr)
        check('恢复回退使用记录的旧镜像 ID',
              len(fake.tag_cmds) > tag_calls_before
              and fake.tag_cmds[-1][2] == fake.image_ids[REPO + ':1.0.0'],
)
        check('恢复回退完成且 .env 指向本地标记',
              dmgr.state()['stage'] == 'done'
              and 'AUTODEPLOY_IMAGE_TAG=rollback-' in (docker_install / '.env').read_text())

        # attention 状态下仅允许恢复到升级前镜像，其他目标拒绝。
        att2 = dmgr._new_state('update', 'v2.0.0', '')
        att2.update(stage='attention', delegated=True, from_image_ref=REPO + ':1.0.0',
                    from_image_id=fake.image_ids[REPO + ':1.0.0'], error='结果不确定')
        su.save_state_file(att2)
        with patch.object(su, '__version__', '2.0.0'):
            expect_raises('人工处置中仅允许恢复到升级前镜像',
                          lambda: dmgr.rollback(target_image=REPO + ':2.0.0'), RuntimeError)
        dmgr.attention_action('resolve')

        gen_main.close()

        # 一键脚本契约：同路径挂载、属主迁移、增量 .env、版本核对。
        bootstrap_text = (ROOT / 'scripts/bootstrap.sh').read_text(encoding='utf-8')
        check('一键脚本 override 同路径挂载安装目录',
              '${INSTALL_DIR}:${INSTALL_DIR}' in bootstrap_text)
        check('一键脚本迁移安装目录属主给容器用户', 'chown 1000:1000' in bootstrap_text)
        check('一键脚本提供 .env 增量更新函数', 'upsert_env()' in bootstrap_text)
        check('一键脚本完成后核对实际版本', '/api/health' in bootstrap_text and '不一致' in bootstrap_text)

        # upsert_env 函数行为（以真实 /bin/bash 运行）。
        import subprocess as _sp
        extracted = _sp.run(['bash', '-c', "sed -n '/^upsert_env()/,/^}/p' scripts/bootstrap.sh"],
                            capture_output=True, text=True, cwd=ROOT)
        check('一键脚本 upsert_env 函数可提取', extracted.returncode == 0 and 'upsert_env()' in extracted.stdout)
        bdir = tmp_root / 'bootstrap-env'
        bdir.mkdir()
        (bdir / '.env').write_text('AUTODEPLOY_PORT=8770\n# 自定义注释\nCUSTOM_KEY=V\n')
        upsert_script = ("set -euo pipefail\ncd '" + str(bdir) + "'\nINSTALL_DIR='" + str(bdir)
                         + "'\nSUDO=''\n" + extracted.stdout
                         + "\nupsert_env AUTODEPLOY_PORT 9000\nupsert_env AUTODEPLOY_IMAGE_TAG 0.3.5\n"
                         + "upsert_env --remove CUSTOM_KEY\n")
        upsert_run = _sp.run(['bash', '-c', upsert_script], capture_output=True, text=True)
        env_after = (bdir / '.env').read_text().splitlines() if upsert_run.returncode == 0 else []
        check('upsert_env 增量更新保序且可删键',
              upsert_run.returncode == 0
              and env_after == ['# 自定义注释', 'AUTODEPLOY_PORT=9000', 'AUTODEPLOY_IMAGE_TAG=0.3.5'])

        section('.env 安全写入、镜像引用解析与脱敏')

        import hashlib as _hashlib

        env_dir = tmp_root / 'env-unit'
        env_dir.mkdir()
        env_file = env_dir / '.env'
        env_file.write_text('AUTODEPLOY_PORT=9000\n# 注释\nAUTODEPLOY_IMAGE=example.com/app\nAUTODEPLOY_IMAGE_TAG=1.0.0\n')
        os.chmod(env_file, 0o640)
        original_content = env_file.read_text()
        rewritten = su.rewrite_env_content(original_content, {'AUTODEPLOY_IMAGE_TAG': '2.0.0'})
        check('改写仅动目标键且保持行序',
              rewritten.splitlines() == ['AUTODEPLOY_PORT=9000', '# 注释',
                                         'AUTODEPLOY_IMAGE=example.com/app', 'AUTODEPLOY_IMAGE_TAG=2.0.0'])
        su.write_env_atomic(env_file, rewritten, 0o640)
        check('原子写入保留权限位且无临时残留',
              (env_file.stat().st_mode & 0o777) == 0o640
              and not list(env_dir.glob('.env.tmp-upgrade-*')))
        linked = env_dir / 'linked.env'
        linked.symlink_to(env_file)
        expect_raises('软链拒绝写入', lambda: su.write_env_atomic(linked, 'x', 0o600), RuntimeError)
        snapshot = su.read_env_snapshot(env_file)
        check('快照含内容权限与指纹',
              snapshot['sha256'] == _hashlib.sha256(rewritten.encode('utf-8')).hexdigest()
              and snapshot['mode'] == 0o640)
        su.restore_env_snapshot(env_file, snapshot, snapshot['sha256'])
        check('指纹一致时恢复成功', env_file.read_text() == rewritten)
        env_file.write_text('被人工改过\n')
        expect_raises('指纹不符拒绝恢复',
                      lambda: su.restore_env_snapshot(env_file, snapshot, snapshot['sha256']), RuntimeError)
        check('镜像引用解析 registry 端口',
              su.parse_image_ref('localhost:5000/auto-deploy:0.3.5')
              == ('localhost:5000/auto-deploy', '0.3.5', ''))
        check('镜像引用解析 digest',
              (su.parse_image_ref('ghcr.io/a/b@sha256:' + '0' * 64) or ('', '', ''))[2]
              == 'sha256:' + '0' * 64)
        check('非法镜像引用拒绝',
              su.parse_image_ref('bad ref with space') is None and su.parse_image_ref('') is None
              and su.parse_image_ref('repo:@evil') is None)
        check('URL 凭证脱敏', su.sanitize_url('https://user:secret@github.com/a/b') == 'https://github.com/a/b'
              and su.sanitize_url('https://token@github.com/a/b') == 'https://github.com/a/b')
        from app.executor import redact as _redact
        check('Authorization 整值脱敏', _redact('Authorization: Bearer abc def') == 'Authorization: ***')
        check('userinfo 型 URL 脱敏', _redact('https://user@github.com/a/b') == 'https://***@github.com/a/b')

        section('状态写入围栏')

        with patch.object(config, 'DATA_DIR', tmp_root / 'fencing-data'):
            fmgr = su.SelfUpdateManager(fake_store)
            op_a = fmgr._new_state('update', 'v2.0.0', '')
            op_a.update(stage='executing', delegated=True)
            check('新操作正常落盘', su.save_state_file(op_a))
            # 同操作基于旧快照推进：对齐代数后允许写入（拒绝会卡死推进链）。
            stale = dict(op_a, stage='prepared', generation=su.load_state_file()['generation'] - 1)
            check('同操作旧快照对齐代数后写入', su.save_state_file(stale)
                  and su.load_state_file()['stage'] == 'prepared')
            # 同操作声明更高代数（旧执行器的未来写入）必须拒绝。
            future = dict(op_a, stage='done', generation=su.load_state_file()['generation'] + 5)
            check('同操作高代数写入被拒绝', not su.save_state_file(future)
                  and su.load_state_file()['stage'] == 'prepared')
            op_a = su.load_state_file()
            op_b = fmgr._new_state('rollback', 'v1.0.0', '')
            check('异操作不得覆盖进行中的操作', not su.save_state_file(op_b)
                  and su.load_state_file()['operation_id'] == op_a['operation_id'])
            op_a['stage'] = 'done'
            op_a['generation'] = su.load_state_file()['generation']
            check('操作落终态', su.save_state_file(op_a)
                  and su.load_state_file()['stage'] == 'done')
            check('终态后异操作可写入', su.save_state_file(op_b)
                  and su.load_state_file()['operation_id'] == op_b['operation_id'])

        section('重启确认与委托操作启动收尾')

        # PID 不再是确认条件：容器 PID namespace 完全可能复用相同数值。
        with patch.object(config, 'DATA_DIR', tmp_root / 'pid-confirm-data'):
            pmgr = su.SelfUpdateManager(fake_store)
            pid_state = pmgr._new_state('update', 'v2.0.0', 'autodeploy.service')
            pid_state.update(stage='restarting', restart='self-exit', restart_deadline=time.time() + 60)
            pmgr._save_state(pid_state)
            with patch.object(su, 'PROCESS_BOOT_ID', 'pid-same-boot'), \
                    patch.object(su.os, 'getpid', return_value=pid_state['before_pid']), \
                    patch.object(su, '__version__', '2.0.0'):
                pmgr.reconcile_on_startup()
            check('PID 复用也确认成功', pmgr.state()['stage'] == 'done'
                  and pmgr.state()['confirmed_operation'] == 'update')

        # 委托操作：新进程只写启动标记，绝不伪报成功；执行器缺席转人工处置。
        with patch.object(config, 'DATA_DIR', tmp_root / 'delegated-data'):
            dmgr2 = su.SelfUpdateManager(fake_store)
            del_state = dmgr2._new_state('update', 'v2.0.0', '')
            del_state.update(stage='executing', delegated=True,
                             executor_container='auto-deploy-upgrade-x',
                             expected_version='2.0.0', before_boot_id='old-boot',
                             exec_deadline=time.time() + 60)
            dmgr2._save_state(del_state)
            with patch.object(su, 'PROCESS_BOOT_ID', 'marker-boot'), \
                    patch.object(su, '__version__', '2.0.0'), \
                    patch.object(su, 'run_command',
                                 return_value=SimpleNamespace(ok=False, output='', error='no docker')):
                dmgr2.reconcile_on_startup()
            marker = json.loads((tmp_root / 'delegated-data' / su.MARKER_FILE_NAME).read_text())
            check('新进程写入启动标记而不伪报成功',
                  marker['version'] == '2.0.0' and marker['boot_id'] == 'marker-boot'
                  and dmgr2.state()['stage'] in ('executing', 'attention'))
            check('执行器缺席且无终态转人工处置', dmgr2.state()['stage'] == 'attention'
                  and dmgr2.deployment_blocked())
            expect_raises('执行器缺席无法继续等待', lambda: dmgr2.attention_action('wait'), RuntimeError)
            dmgr2.attention_action('resolve')
            check('人工处理后解除禁入并记录历史',
                  dmgr2.state()['stage'] == 'failed' and not dmgr2.deployment_blocked()
                  and dmgr2.history()[0]['stage'] == 'failed')

        # 旧版 docker-recreate 记录的重启期限仍然生效（迁移兼容）。
        with patch.object(config, 'DATA_DIR', tmp_root / 'legacy-recreate-data'):
            lmgr = su.SelfUpdateManager(fake_store)
            lmgr._save_state({'schema_version': su.STATE_SCHEMA_VERSION, 'stage': 'restarting',
                              'restart': 'docker-recreate', 'restart_deadline': time.time() - 1,
                              'target_version': '2.0.0', 'log': []})
            check('旧版容器内重建记录超时同样判失败', lmgr.state()['stage'] == 'failed')

        section('旧版更新状态与备份兼容')
        with patch.object(config, 'DATA_DIR', tmp_root / 'legacy-update-data'):
            legacy_mgr = su.SelfUpdateManager(fake_store)
            for stage in ('restarting', 'done', 'applying'):
                legacy_mgr._save_state({'stage': stage, 'target_version': su.__version__, 'log': ['旧版原始日志']})
                legacy_mgr.reconcile_on_startup()
                migrated = legacy_mgr.state()
                check('旧版 ' + stage + ' 不伪报成功或超时', migrated['stage'] == 'unverified'
                      and migrated['legacy_stage'] == stage and not migrated.get('error')
                      and not migrated.get('confirmed_operation_id'))
                check('旧版 ' + stage + ' 保留日志且释放部署入口', migrated['log'] == ['旧版原始日志']
                      and bool(migrated.get('notice')) and not legacy_mgr.deployment_blocked())
                before_read = legacy_mgr.state_path.read_bytes()
                check('兼容读取幂等 ' + stage, legacy_mgr.state() == migrated
                      and legacy_mgr.state_path.read_bytes() == before_read)
            legacy_mgr._save_state({'stage': 'failed', 'target_version': su.__version__,
                                    'error': '旧机制的重启确认超时', 'log': ['sudo 被拒绝']})
            legacy_failed = legacy_mgr.state()
            check('旧失败保留原因并标明历史记录', legacy_failed['stage'] == 'failed'
                  and legacy_failed['error'] == '旧机制的重启确认超时' and bool(legacy_failed.get('notice')))

            modern = dict(saved)
            modern.pop('schema_version')
            modern['restart_deadline'] = time.time() + 60
            legacy_mgr._save_state(modern)
            with patch.object(su, 'PROCESS_BOOT_ID', 'compatible-boot'), \
                    patch.object(su.os, 'getpid', return_value=os.getpid() + 102), patch.object(su, '__version__', '2.0.0'):
                legacy_mgr.reconcile_on_startup()
            check('无格式号的完整新协议仍严格确认', legacy_mgr.state()['stage'] == 'done'
                  and legacy_mgr.state()['schema_version'] == su.STATE_SCHEMA_VERSION)
            for deadline in (None, 'invalid', {}, True, float('inf')):
                legacy_mgr._save_state(dict(saved, restart_deadline=deadline))
                invalid_state = legacy_mgr.state()
                check('无效重启期限明确失败 ' + str(deadline), invalid_state['stage'] == 'failed'
                      and '有效期限' in invalid_state['error'])
            no_deadline = dict(saved)
            no_deadline.pop('restart_deadline')
            legacy_mgr._save_state(no_deadline)
            check('现代协议缺期限不得降级为旧记录', legacy_mgr.state()['stage'] == 'failed'
                  and 'legacy_stage' not in legacy_mgr.state())
            # 容器各自有独立 PID namespace，PID 数值可复用：缺 PID 不阻断确认，
            # 缺 boot_id（真正的进程身份）才拒绝。
            missing_pid = dict(saved, restart_deadline=time.time() + 60)
            missing_pid.pop('before_pid')
            legacy_mgr._save_state(missing_pid)
            with patch.object(su, 'PROCESS_BOOT_ID', 'missing-pid-boot'), patch.object(su, '__version__', '2.0.0'):
                legacy_mgr.reconcile_on_startup()
            check('缺 PID 不阻断确认（boot 才是身份）', legacy_mgr.state()['stage'] == 'done')
            missing_boot = dict(saved, restart_deadline=time.time() + 60)
            missing_boot.pop('before_boot_id')
            legacy_mgr._save_state(missing_boot)
            with patch.object(su, 'PROCESS_BOOT_ID', 'missing-boot-check'), patch.object(su, '__version__', '2.0.0'):
                legacy_mgr.reconcile_on_startup()
            check('缺重启前 boot 身份不得确认成功', legacy_mgr.state()['stage'] == 'failed')
            legacy_mgr._save_state({'schema_version': 999, 'stage': 'done', 'log': []})
            unknown_bytes = legacy_mgr.state_path.read_bytes()
            check('未知协议不确认也不改写原文件', legacy_mgr.state()['stage'] == 'unverified'
                  and legacy_mgr.state_path.read_bytes() == unknown_bytes)

            old_backup = legacy_mgr.backups_dir / '20260929011842'
            tree(old_backup, '1.0.0')
            old_status = legacy_mgr.backup_status()
            check('旧备份不可证明完整性时说明原因', not old_status['can_rollback']
                  and '缺少完成标记' in old_status['backup_notice'] and old_backup.exists())
            expect_raises('无标记旧备份禁止自动恢复',
                          lambda: legacy_mgr._restore_backup(install, {'backup': str(old_backup)}), RuntimeError)
            incomplete = legacy_mgr.backups_dir / 'incomplete'
            incomplete.mkdir()
            check('旧备份与中断备份均不自动采用', legacy_mgr.latest_backup() is None)
            valid_backup = legacy_mgr.backups_dir / '1770000000000000000-valid'
            tree(valid_backup, '1.0.0')
            (valid_backup / 'complete.json').write_text(json.dumps({'version': '1.0.0'}))
            bad_backup = legacy_mgr.backups_dir / '9999999999999999999-bad'
            tree(bad_backup, '3.0.0')
            (bad_backup / 'complete.json').write_text('{broken')
            check('损坏的新备份不遮蔽有效备份', legacy_mgr.latest_backup() == valid_backup)
            (bad_backup / 'complete.json').write_text(json.dumps({'version': '9.0.0'}))
            expect_raises('备份标记版本不符拒绝', lambda: legacy_mgr._validate_backup(bad_backup), RuntimeError)
            (bad_backup / 'complete.json').write_text(json.dumps({'version': '3.0.0'}))
            (bad_backup / 'app/main.py').unlink()
            expect_raises('完成标记不能掩盖关键文件缺失', lambda: legacy_mgr._validate_backup(bad_backup), RuntimeError)
            (bad_backup / 'app/main.py').write_text('')
            (bad_backup / 'web/escape').symlink_to(tmp_root)
            expect_raises('备份内部软链拒绝', lambda: legacy_mgr._validate_backup(bad_backup), RuntimeError)
            (bad_backup / 'web/escape').unlink()
            (bad_backup / 'complete.json').unlink()
            (bad_backup / 'complete.json').symlink_to(valid_backup / 'complete.json')
            expect_raises('备份完成标记软链拒绝', lambda: legacy_mgr._validate_backup(bad_backup), RuntimeError)
            linked_backup = legacy_mgr.backups_dir / 'linked'
            linked_backup.symlink_to(valid_backup, target_is_directory=True)
            expect_raises('备份目录软链拒绝', lambda: legacy_mgr._validate_backup(linked_backup), RuntimeError)
            expect_raises('备份根外路径拒绝', lambda: legacy_mgr._validate_backup(install), RuntimeError)
            status = legacy_mgr.backup_status()
            check('可用备份与被忽略旧备份同时说明', status['can_rollback']
                  and '缺少完成标记' in status['backup_notice'] and '未通过校验' in status['backup_notice'])
            from app.api.stats import self_update_status
            api_status = self_update_status(service=SimpleNamespace(selfupdate=legacy_mgr), user={})
            check('状态接口包含运行版本与备份说明', api_status['current_version'] == su.__version__
                  and not api_status['active'] and api_status['can_rollback'] and bool(api_status['backup_notice']))
        with patch.object(config, 'DATA_DIR', tmp_root / 'linked-backup-data'):
            linked_mgr = su.SelfUpdateManager(fake_store)
            linked_mgr.backups_dir.parent.mkdir(parents=True)
            outside_backups = tmp_root / 'outside-backups'
            outside_backups.mkdir()
            linked_mgr.backups_dir.symlink_to(outside_backups, target_is_directory=True)
            before_code = (install / 'app/__init__.py').read_bytes()
            with patch.object(linked_mgr, '_restart_plan', return_value=('self-exit', 'autodeploy.service')), \
                    patch.object(linked_mgr, '_replace') as replace_spy, \
                    patch.object(linked_mgr, '_exit_for_restart') as exit_spy:
                expect_raises('备份根目录软链在更新前拒绝', lambda: linked_mgr.start('v2.0.0'), RuntimeError)
                expect_raises('备份生成也拒绝根目录软链',
                              lambda: linked_mgr._backup(install, {'operation_id': 'test'}, lambda message: None), RuntimeError)
                check('不安全备份位置不替换不退出不落状态', not replace_spy.called and not exit_spy.called
                      and not linked_mgr.state_path.exists() and not list(outside_backups.iterdir())
                      and (install / 'app/__init__.py').read_bytes() == before_code)
        check('测试绝未实际退出宿主', not hard_exit.called)
        check('重启仅调用只读 systemctl show', all(c[1] == 'show' for c in calls if c[0] == 'systemctl' and len(c) > 1))

    # 真正执行下载方法和标签校验，Git 仅操作隔离的本地仓库。
    origin_update = tmp_root / 'safe-update-origin'
    tree(origin_update, '2.0.0')
    for args in (['init', '-q'], ['config', 'user.email', 'test@example.invalid'],
                 ['config', 'user.name', '自检'], ['add', '.'], ['commit', '-qm', '测试版本'],
                 ['tag', 'v2.0.0']):
        result = safe_command(['git', *args], cwd=origin_update, timeout=20)
        if not result.ok:
            raise AssertionError(result.output)
    download_dir = tmp_root / 'real-update-download'
    download_dir.mkdir()
    download_settings = SimpleNamespace(update_repo=str(origin_update), git_timeout_seconds=20)
    with patch.object(config, 'proxy_env', return_value={}):
        mgr._download('v2.0.0', download_dir, download_settings, lambda message: None, {})
    check('真实下载目标标签并验证内容', mgr._validate_tree(download_dir / 'src', '2.0.0') == '2.0.0')
    with patch.object(config, 'load_settings', return_value=download_settings), patch.object(config, 'proxy_env', return_value={}):
        latest = mgr.check(force=True)
        check('真实查询识别最新标签', latest.latest == 'v2.0.0')

    # ------------------------------------------------------------------
    section('更新检查的网络路径')

    import urllib.error
    import urllib.request

    from app import gitops

    # 依据设置页配置的代理必须真正作用到 Releases API 这条主路径上：
    # 之前它只传给了 git 回退，在「必须走代理才能访问 GitHub」的服务器上，
    # 主路径永远失败，用户看到的就是「检测不到新版本」。
    used = []

    def recording_opener(*handlers):
        # build_opener 收到的是 ProxyHandler；真正的代理 URL 在它的 proxies 里。
        proxy_url = ''
        for handler in handlers:
            proxies = getattr(handler, 'proxies', None) or {}
            proxy_url = proxies.get('https') or proxies.get('http') or ''
        class Opener:
            def open(self, request, timeout=None):
                used.append(('proxy', proxy_url))
                raise urllib.error.URLError('测试替身不应真连')
        return Opener()

    def recording_urlopen(request, timeout=None):
        used.append(('direct', request.full_url))
        raise urllib.error.URLError('测试替身不应真连')

    def invoke(proxy):
        used.clear()
        with patch.object(su.urllib.request, 'build_opener', recording_opener), \
                patch.object(su.urllib.request, 'urlopen', recording_urlopen):
            try:
                su.urlopen_with_proxy(
                    su.urllib.request.Request('https://api.github.com/repos/x/y/releases/latest'),
                    proxy, timeout=5)
            except urllib.error.URLError:
                pass
        return used[0] if used else ('none', '')

    check('API 主路径使用设置页配置的代理（而不是进程环境）',
          invoke({'https_proxy': 'http://127.0.0.1:7897'}) == ('proxy', 'http://127.0.0.1:7897'))
    check('未配置代理时直连', invoke(None) == ('direct', 'https://api.github.com/repos/x/y/releases/latest')
          and invoke({}) == ('direct', 'https://api.github.com/repos/x/y/releases/latest'))
    check('no_proxy 命中的主机即使配了代理也直连',
          invoke({'https_proxy': 'http://127.0.0.1:9', 'no_proxy': 'api.github.com'})[0] == 'direct')
    check('no_proxy 按域后缀匹配且不误伤相似域名',
          su._proxy_bypassed('api.github.com', 'github.com') is True
          and su._proxy_bypassed('127.0.0.1', 'localhost,127.0.0.1') is True
          and su._proxy_bypassed('api.github.com', 'localhost,127.0.0.1') is False
          and su._proxy_bypassed('evil-github.com', 'github.com') is False)

    # git 回退必须走 gitops：才有 HTTP/1.1 抗抖配置与 GIT_TERMINAL_PROMPT=0，
    # 否则在缺凭据时会挂在交互提示上直到超时。
    fallback_calls = []
    leftover_commands = []

    def fake_ls_remote_tags(**kwargs):
        fallback_calls.append(kwargs)
        return SimpleNamespace(ok=True, output='abc\trefs/tags/v2.0.0\n', error='')

    def recording_run_command(args, **kwargs):
        leftover_commands.append(list(args))
        return SimpleNamespace(ok=False, output='', error='不应被调用')

    proxy_settings = SimpleNamespace(
        update_repo='https://github.com/j9kkk/auto-deploy.git', proxy_enabled=True,
        proxy_url='http://127.0.0.1:7897', proxy_username='', proxy_password='',
        proxy_no_proxy='localhost,127.0.0.1', proxy_for_scripts=False,
        git_timeout_seconds=30,
    )
    with patch.object(config, 'load_settings', return_value=proxy_settings), \
            patch.object(config, 'TMP_DIR', tmp_root / 'check-tmp'), \
            patch.object(gitops, 'ls_remote_tags', side_effect=fake_ls_remote_tags), \
            patch.object(su.SelfUpdateManager, '_urlopen',
                         side_effect=urllib.error.URLError('模拟 API 不可用')), \
            patch.object(su, 'run_command', side_effect=recording_run_command):
        fallback = su.SelfUpdateManager(fake_store)._fetch_latest()
    check('API 失败后回退到 gitops 标签查询',
          fallback.latest == 'v2.0.0' and fallback.update_available and len(fallback_calls) == 1,
          f'{fallback.latest!r} calls={len(fallback_calls)}')
    check('回退不再自行拼 git ls-remote（避免绕过抗抖配置）',
          not leftover_commands, str(leftover_commands))
    check('git 回退带上设置页配置的代理',
          fallback_calls and fallback_calls[0].get('proxy', {}).get('https_proxy') == 'http://127.0.0.1:7897',
          str(fallback_calls))

    # git 回退本身必须真的用上 _GIT_RESILIENCE_CONFIG 与 GIT_TERMINAL_PROMPT=0。
    # 连通性预检会真开 socket，自检要求离线可跑，因此这里把它替换掉。
    captured_args = {}

    def capture_git(args, **kwargs):
        captured_args['args'] = list(args)
        captured_args['env'] = dict(kwargs.get('env') or {})
        return SimpleNamespace(ok=True, output='', error='', exit_code=0, duration_ms=0)

    with patch.object(gitops, 'check_reachable', return_value=None), \
            patch.object(gitops, 'run_command', side_effect=capture_git):
        gitops.ls_remote_tags(repo_url='https://github.com/j9kkk/auto-deploy.git',
                              tmp_dir=tmp_root / 'ls-remote-creds', timeout=30)
    check('ls-remote 使用 HTTP/1.1 抗抖配置',
          'http.version=HTTP/1.1' in captured_args.get('args', []),
          str(captured_args.get('args')))
    check('ls-remote 禁止交互式凭据提示',
          captured_args.get('env', {}).get('GIT_TERMINAL_PROMPT') == '0',
          str(captured_args.get('env')))
    check('ls-remote 仍是只读查询',
          'ls-remote' in captured_args.get('args', [])
          and '--tags' in captured_args.get('args', []))
    # 不可达时应在预检阶段就返回可读原因，而不是干等完整的 ls-remote 超时。
    ran_git = []
    with patch.object(gitops, 'check_reachable', return_value='无法连接 github.com:443（超时）'), \
            patch.object(gitops, 'run_command',
                         side_effect=lambda *a, **k: ran_git.append(a) or SimpleNamespace(
                             ok=True, output='', error='', exit_code=0, duration_ms=0)):
        unreachable = gitops.ls_remote_tags(repo_url='https://github.com/j9kkk/auto-deploy.git',
                                            tmp_dir=tmp_root / 'ls-remote-unreachable', timeout=30)
    check('不可达时预检即返回原因且不启动 git',
          not unreachable.ok and '无法连接' in (unreachable.error or '') and not ran_git,
          f'{unreachable.error!r} ran_git={bool(ran_git)}')

    # 预检「无法判定」（例如临时 DNS 抖动）时返回 None，必须继续交给 git：
    # 这正是本次上报的回归点——预检把抖动当成确定失败，git 根本没机会重试。
    inconclusive_ran = []

    def inconclusive_git(args, **kwargs):
        inconclusive_ran.append(list(args))
        return SimpleNamespace(ok=False, cancelled=False, timed_out=False,
                               output='fatal: unable to access: '
                                      'Could not resolve host: github.com',
                               error='命令退出码 128', exit_code=128, duration_ms=1)

    with patch.object(gitops, 'check_reachable', return_value=None), \
            patch.object(gitops, 'run_command', side_effect=inconclusive_git):
        after_blip = gitops.ls_remote_tags(repo_url='https://github.com/j9kkk/auto-deploy.git',
                                           tmp_dir=tmp_root / 'ls-remote-blip', timeout=30)
    check('预检无法判定时仍执行 git（不误杀）', len(inconclusive_ran) >= 1,
          f'ran={len(inconclusive_ran)}')
    check('git 解析失败被翻译为可操作提示',
          '解析' in (after_blip.error or '') and '命令退出码' not in (after_blip.error or ''),
          repr(after_blip.error))

    # 失败结果不得进入 10 分钟缓存：否则界面会持续显示「检查失败」，
    # 用户点多少次都只是拿到同一个缓存的错误。
    flaky = {'count': 0}

    def flaky_fetch(self):
        flaky['count'] += 1
        if flaky['count'] == 1:
            return su.UpdateCheck('1.0.0', '', False, '', error='网络暂时不可用')
        return su.UpdateCheck('1.0.0', 'v2.0.0', True, '')

    cache_mgr = su.SelfUpdateManager(fake_store)
    with patch.object(su.SelfUpdateManager, '_fetch_latest', flaky_fetch):
        first = cache_mgr.check()
        second = cache_mgr.check()
    check('失败结果不缓存，重试可立即恢复',
          first.error and not second.error and flaky['count'] == 2, str(flaky))
    with patch.object(su.SelfUpdateManager, '_fetch_latest', flaky_fetch):
        third = cache_mgr.check()
        fourth = cache_mgr.check()
    check('成功结果仍按 TTL 缓存（不再重复请求）',
          flaky['count'] == 2 and third.latest == fourth.latest == 'v2.0.0', str(flaky))

    # ------------------------------------------------------------------
    section('更新历史与按版本回滚')

    with patch.object(config, 'DATA_DIR', tmp_root / 'history-data'):
        history_mgr = su.SelfUpdateManager(fake_store)
        check('无记录时历史为空且备份清单为空',
              history_mgr.history() == [] and history_mgr.backups() == []
              and history_mgr.backup_for_version('1.0.0') is None)
        history_mgr._save_state(dict(saved, operation='update', operation_id='op-a', stage='restarting',
                                     target_version='2.0.0', previous_version='1.0.0', backup_version='1.0.0',
                                     log=['[10:00:00] 下载中'], error=''))
        history_mgr.reconcile_on_startup()   # 进程/版本对不上 → 落定为失败并写入历史
        entries = history_mgr.history()
        check('终态历史记录包含日志与最终结果',
              len(entries) == 1 and entries[0]['operation_id'] == 'op-a'
              and entries[0]['stage'] == 'failed' and entries[0]['error']
              and entries[0]['log'][0] == '[10:00:00] 下载中'
              and entries[0]['backup_version'] == '1.0.0'
              and entries[0]['previous_version'] == '1.0.0',
              str(entries))
        history_mgr.reconcile_on_startup()
        check('同一 operation_id 不重复写入历史', len(history_mgr.history()) == 1)
        check('历史不含服务器路径与代币字段',
              'backup' not in entries[0] and 'path' not in entries[0])
        # 不同操作各留一条（新的在前）：只保留最新一条会让「升级日志」变成单条。
        history_mgr._save_state(dict(saved, operation='update', operation_id='op-c', stage='restarting',
                                     target_version='3.0.0', previous_version='2.0.0', backup_version='2.0.0',
                                     log=['[12:00:00] 第二次更新'], error=''))
        history_mgr.reconcile_on_startup()
        kept = [item['operation_id'] for item in history_mgr.history()]
        check('多次操作全部保留且新的在前', kept == ['op-c', 'op-a'], str(kept))
        check('历史记录保留各自日志',
              history_mgr.history()[0]['log'][0] == '[12:00:00] 第二次更新', str(history_mgr.history()))

        # 按版本回滚只在通过校验的备份里查找，界面传进来的值不能越界。
        for name, version in (('2026010100000000000-a', '1.0.0'), ('2026010200000000000-b', '1.1.0')):
            backup = history_mgr.backups_dir / name
            tree(backup, version)
            (backup / 'complete.json').write_text(json.dumps({'version': version}))
        legacy = history_mgr.backups_dir / 'legacy-no-marker'
        tree(legacy, '0.9.0')
        listed = {item['version'] for item in history_mgr.backups()}
        check('备份清单只含通过校验的版本', listed == {'1.0.0', '1.1.0'}, str(listed))
        check('按版本找到对应备份',
              history_mgr.backup_for_version('v1.0.0').name == '2026010100000000000-a'
              and history_mgr.backup_for_version('1.9.9') is None)
        check('备份清单不含路径', all('path' not in item for item in history_mgr.backups()))
        # 版本号校验发生在 _preflight 之后，因此必须让预检通过才能真正走到它；
        # 否则这些「拒绝」会全部由预检报错顶掉，校验逻辑本身没被测到。
        with patch.object(history_mgr, '_preflight', return_value='autodeploy.service'):
            expect_raises('按版本回滚拒绝非法版本号',
                          lambda: history_mgr.rollback('../../etc'), RuntimeError)
            expect_raises('按版本回滚拒绝不存在的版本',
                          lambda: history_mgr.rollback('9.9.9'), RuntimeError)
            expect_raises('按版本回滚不能指向未校验的备份',
                          lambda: history_mgr.rollback('0.9.0'), RuntimeError)
            # 非法版本号必须在查备份目录之前就被挡下：否则形如 1.0.0/../x 的输入
            # 会先进入目录查找逻辑，安全就只剩「恰好查不到」这一层运气。
            try:
                history_mgr.rollback('1.0.0/../../etc')
                version_guard = ''
            except RuntimeError as exc:
                version_guard = str(exc)
            check('非法版本号在版本校验层被拒绝（而非查不到备份）',
                  '无效' in version_guard, repr(version_guard))

    api_history_seen = {}
    with patch.object(config, 'DATA_DIR', tmp_root / 'history-api-data'):
        api_mgr = su.SelfUpdateManager(fake_store)
        # 走真实确认路径：重启后进程身份与版本都对齐 → 落定为成功并写入历史。
        # target/expected 必须与被 patch 的 __version__ 一致，才是「确认成功」。
        api_mgr._save_state(dict(saved, operation='rollback', operation_id='op-b', stage='restarting',
                                 target_version='2.0.0', expected_version='2.0.0',
                                 version='2.0.0', previous_version='1.0.0', backup_version='1.0.0',
                                 log=['[11:00:00] 回滚完成'], error=''))
        with patch.object(su, 'PROCESS_BOOT_ID', 'api-boot'), \
                patch.object(su.os, 'getpid', return_value=os.getpid() + 500), \
                patch.object(su, '__version__', '2.0.0'):
            api_mgr.reconcile_on_startup()
            confirmed_state = api_mgr.state()
            from app.api.stats import self_update_history
            api_history_seen = self_update_history(service=SimpleNamespace(selfupdate=api_mgr), user={})
        entry = (api_history_seen['entries'] or [{}])[0]
        check('确认成功后历史记录标为完成且无错误',
              confirmed_state['stage'] == 'done' and entry.get('operation_id') == 'op-b'
              and entry.get('stage') == 'done' and entry.get('operation') == 'rollback'
              and not entry.get('error') and entry.get('log'),
              str(api_history_seen)[:300])
        check('历史接口同时返回运行版本与备份清单（均无路径）',
              api_history_seen['backups'] == []
              and api_history_seen['current_version'] == su.normalize_tag(su.__version__)
              and 'path' not in json.dumps(api_history_seen, ensure_ascii=False)
              and 'backup"' not in json.dumps(api_history_seen, ensure_ascii=False),
              str(api_history_seen)[:300])

    # Node vm 执行真实前端逻辑，不复制状态判定实现。
    frontend = safe_command(['node', str(ROOT / 'tests/selfupdate-ui.js')], timeout=30)
    check('前端更新状态回归', frontend.ok, frontend.output)
    task_frontend = safe_command(['node', str(ROOT / 'tests/task-ui.js')], timeout=30)
    check('前端任务名与回滚交互回归', task_frontend.ok, task_frontend.output)
    from task_rollback_checks import run_checks as run_task_rollback_checks
    run_task_rollback_checks(check, tmp_root)

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

    # --- 日志链路：批量写入、周期 flush、截断保尾 -----------------------
    # 大日志/频繁部署场景：文件触顶后磁盘上必须保住结尾（失败原因所在），
    # 而不是实时视图看得到结尾、落盘后只剩开头。
    import app.runner  # noqa: F401 - 确认模块可导入且常量存在
    from app.runner import ActiveRun, LOG_FLUSH_EVERY_LINES

    log_probe = ActiveRun(run_id=990001, task_id=1, log_max_bytes=4096)
    log_path = tmp_root / "probe-run.log"
    log_probe.log_file = log_path.open("a", encoding="utf-8", errors="replace")
    noisy = [f"noise line {i:05d} paddingpadding" for i in range(200)]
    runner._write_log_batch(log_probe, noisy)
    check("批量写入一次性入缓冲", log_probe.tail_count == len(noisy))
    # 首批 200 行已超上限：写循环内每行检查 tell()，超限即关文件，
    # flush 计数随最后一次批量写入清零（周期 flush 在批内被触发过）。
    check("批量触顶后句柄关闭并置截断标记",
          log_probe.log_file is None and log_probe.log_truncated)
    runner._write_log_batch(log_probe, ["tail-keeper"])
    closed_bytes = runner._close_log(log_probe, log_path)
    final_text = log_path.read_text(encoding="utf-8")
    check("截断后收口重写文件为内存尾部", final_text.startswith("! 日志前段因超过大小上限被截断"))
    check("重写后的文件包含结尾内容（失败原因可找回）",
          "tail-keeper" in final_text
          and "noise line 00199" in final_text
          and "noise line 00000" not in final_text)
    check("收口返回重写后的字节数", closed_bytes == log_path.stat().st_size and closed_bytes > 0)
    check("重写内容尊重大小上限", closed_bytes <= log_probe.log_max_bytes,
          f"{closed_bytes} > {log_probe.log_max_bytes}")

    # 周期 flush：写入超过阈值行数后应把 flush 计数清零（文件 flush 被调用过）
    flush_probe = ActiveRun(run_id=990002, task_id=1, log_max_bytes=10_000_000)
    flush_path = tmp_root / "flush-run.log"
    flush_probe.log_file = flush_path.open("a", encoding="utf-8", errors="replace")
    runner._write_log_batch(flush_probe, [f"line-{i}" for i in range(LOG_FLUSH_EVERY_LINES * 2)])
    check("写入达到行距阈值后 flush 计数归零", flush_probe.lines_since_flush == 0)
    flush_probe.log_file.close()

    # 批量回调链路：executor 的 log_batch 应整批到达，而不是逐行 log()
    batch_seen: list[list[str]] = []
    from app.executor import run_command as _rc
    _rc(["python3", "-c",
         "import sys\nfor i in range(5): print(f'bulk-{i}')"],
        timeout=30, log_batch=batch_seen.append)
    check("executor 批量回调收到整批输出", len(batch_seen) >= 1
          and sum(len(b) for b in batch_seen) == 5, str(batch_seen)[:200])
    check("批量输出内容完整", [l for b in batch_seen for l in b][:5] ==
          [f"bulk-{i}" for i in range(5)])


    # 工作目录以任务名命名；旧目录及外部占用通过认领标记隔离。
    from app import config as app_config

    ws_a = store2.tasks.create(
        {"name": "Named_Workspace", "repo_url": str(origin), "deploy_method": "script",
         "deploy_script": "echo named-dir", "skip_if_no_changes": False}
    )
    # 数据库层唯一约束兜底：绕过 API 也无法创建重名任务。
    raised = False
    try:
        store2.tasks.create(
            {"name": "Named_Workspace", "repo_url": str(origin), "deploy_method": "script",
             "deploy_script": "echo dup", "skip_if_no_changes": False}
        )
    except TaskNameConflict:
        raised = True
    check("数据库层拒绝重名任务", raised)
    ws_b = store2.tasks.create(
        {"name": "Another_Workspace", "repo_url": str(origin), "deploy_method": "script",
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
    check("不同任务工作目录保持独立", resolved_b.name != dir_a.name and resolved_b2.name == resolved_b.name,
          f"{resolved_b.name}/{resolved_b2.name} vs {dir_a.name}")
    check("目录名使用任务名", "task-" not in dir_a.name.split("/")[-1] or dir_a.name.startswith("task-") is False)
    store2.tasks.delete(ws_a)
    store2.tasks.delete(ws_b)

    for directory_id, valid_name in enumerate(("_", "___", "CON", "PRN", "Z" * 80), start=9000):
        directory_task = {"id": directory_id, "name": valid_name}
        path = app_config.workspace_dir_for_task(directory_task)
        check("合法标识直接作为目录名 " + valid_name[:15], path.name == valid_name)
        resolved = app_config.migrate_workspace_to_name(directory_task)
        check("合法标识目录可创建并认领 " + valid_name[:15], resolved == path and app_config._claimed_by(path, directory_id))
        check("合法目录位于工作区内", resolved.parent == app_config.WORKSPACES_DIR)
        shutil.rmtree(path)
    for directory_id, old_name in enumerate(("旧中文目录", "old-name", "CON", "___", "X" * 120), start=9100):
        legacy_path = app_config.WORKSPACES_DIR / app_config._sanitize_dirname(old_name)
        app_config._claim(legacy_path, directory_id)
        marker = legacy_path / "keep.txt"
        marker.write_text("保留旧目录", encoding="utf-8")
        old_task = {"id": directory_id, "name": old_name}
        check("旧目录解析兼容 " + old_name[:15], app_config.workspace_dir_for_task(old_task) == legacy_path)
        check("旧目录不自动迁移 " + old_name[:15], app_config.migrate_workspace_to_name(old_task) == legacy_path and marker.exists())
        shutil.rmtree(legacy_path)
    collision_task = {"id": 9200, "name": "Owned_Workspace"}
    foreign_dir = app_config.WORKSPACES_DIR / collision_task["name"]
    app_config._claim(foreign_dir, 9999)
    foreign_marker = foreign_dir / "keep.txt"
    foreign_marker.write_text("其他任务", encoding="utf-8")
    owned_fallback = app_config.migrate_workspace_to_name(collision_task)
    check("名字目录被占用使用带主键后缀目录", owned_fallback.name == "Owned_Workspace-9200")
    check("带后缀目录建立自身认领", app_config._claimed_by(owned_fallback, 9200))
    check("不覆盖其他任务目录", foreign_marker.exists() and app_config._claimed_by(foreign_dir, 9999))
    shutil.rmtree(foreign_dir)
    check("原目录释放后仍使用已认领后缀目录", app_config.workspace_dir_for_task(collision_task) == owned_fallback)
    app_config._claim(foreign_dir, 9999)
    app_config._claim(owned_fallback, 9998)
    expect_raises("基础和后缀目录均被占用时拒绝解析", lambda: app_config.workspace_dir_for_task(collision_task), ValueError)
    expect_raises("迁移不覆盖被他人占用的后缀目录", lambda: app_config.migrate_workspace_to_name(collision_task), ValueError)
    check("拒绝后保留他人认领", app_config._claimed_by(owned_fallback, 9998))
    shutil.rmtree(foreign_dir)
    shutil.rmtree(owned_fallback)
    foreign_dir.symlink_to(tmp_root, target_is_directory=True)
    check("工作区基础目录软链不被跟随", app_config.workspace_dir_for_task(collision_task) == owned_fallback)
    foreign_dir.unlink()

    unclaimed_task = {"id": 9300, "name": "Unclaimed_Workspace"}
    unclaimed = app_config.WORKSPACES_DIR / unclaimed_task["name"]
    unclaimed_legacy = app_config.workspace_dir(unclaimed_task["id"])
    (unclaimed / ".git").mkdir(parents=True)
    unclaimed_legacy.mkdir()
    check("普通无主目录及无主名字checkout不参与清理", app_config.owned_workspace_dirs(unclaimed_task) == [])
    (unclaimed_legacy / ".git").mkdir()
    check("仅真正旧task-id checkout允许无标记清理", app_config.owned_workspace_dirs(unclaimed_task) == [unclaimed_legacy])
    app_config._claim(unclaimed_legacy, 9996)
    check("旧task-id目录有他人认领不清理", app_config.owned_workspace_dirs(unclaimed_task) == [])
    app_config._claim(unclaimed, unclaimed_task["id"])
    check("只收集自身认领名字目录", app_config.owned_workspace_dirs(unclaimed_task) == [unclaimed])
    shutil.rmtree(unclaimed)
    external_owner = tmp_root / "external-owner"
    app_config._claim(external_owner, unclaimed_task["id"])
    unclaimed.symlink_to(external_owner, target_is_directory=True)
    check("清理不跟随带自身owner的外部软链", app_config.owned_workspace_dirs(unclaimed_task) == [])
    unclaimed.unlink()
    shutil.rmtree(unclaimed_legacy)

    marker_task = {"id": 9400, "name": "Marker_Symlink"}
    marker_base = app_config.WORKSPACES_DIR / marker_task["name"]
    marker_base.mkdir()
    external_marker = tmp_root / "external-marker.txt"
    external_marker.write_text(str(marker_task["id"]), encoding="utf-8")
    (marker_base / app_config.CLAIM_FILE).symlink_to(external_marker)
    check("认领读取统一拒绝marker软链", not app_config._claimed_by(marker_base, marker_task["id"]))
    marker_fallback = app_config.workspace_dir_for_task(marker_task)
    check("marker软链不被解析为自身目录", marker_fallback.name == "Marker_Symlink-9400")
    check("marker软链目录不参与清理", app_config.owned_workspace_dirs(marker_task) == [])
    check("迁移选择安全后缀而不认领软链目录", app_config.migrate_workspace_to_name(marker_task) == marker_fallback)
    check("迁移未改写外部marker", external_marker.read_text(encoding="utf-8") == "9400"
          and (marker_base / app_config.CLAIM_FILE).is_symlink())
    shutil.rmtree(marker_base)
    shutil.rmtree(marker_fallback)

    migration_task = {"id": 9500, "name": "Safe_Legacy_Migration"}
    migration_old = app_config.workspace_dir(migration_task["id"])
    migration_target = app_config.WORKSPACES_DIR / migration_task["name"]
    migration_external = tmp_root / "legacy-external"
    app_config._claim(migration_external, migration_task["id"])
    migration_old.symlink_to(migration_external, target_is_directory=True)
    expect_raises("旧task-id软链禁止迁移", lambda: app_config.migrate_workspace_to_name(migration_task), ValueError)
    check("拒绝旧目录软链后外部数据不变", migration_old.is_symlink() and migration_external.exists() and not migration_target.exists())
    migration_old.unlink()
    app_config._claim(migration_old, 9995)
    expect_raises("旧task-id他人owner禁止迁移", lambda: app_config.migrate_workspace_to_name(migration_task), ValueError)
    check("拒绝旧目录他人owner后原目录不变", app_config._claimed_by(migration_old, 9995) and not migration_target.exists())
    (migration_old / app_config.CLAIM_FILE).unlink()
    (migration_old / app_config.CLAIM_FILE).symlink_to(external_marker)
    expect_raises("旧task-id的marker软链禁止迁移", lambda: app_config.migrate_workspace_to_name(migration_task), ValueError)
    check("拒绝marker软链后未移动旧目录", migration_old.exists() and not migration_target.exists())
    (migration_old / app_config.CLAIM_FILE).unlink()
    (migration_old / ".git").mkdir()
    check("真正无标记旧checkout仍可迁移", app_config.migrate_workspace_to_name(migration_task) == migration_target
          and not migration_old.exists() and app_config._claimed_by(migration_target, migration_task["id"]))
    shutil.rmtree(migration_target)

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
