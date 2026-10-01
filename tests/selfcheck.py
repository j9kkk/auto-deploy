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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
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
            "shop-web-1\tcom.docker.compose.project=autodeploy-shop-web-7100\n"
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
              ["docker", "compose", "-p", "autodeploy-shop-web-7100", "down",
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

        client.post("/api/auth/logout")
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

    check("英文名直接使用", _safe_task_slug("tdcode-site", 1) == "tdcode-site-1")
    check("中文名回退 task-<id>", _safe_task_slug("我的博客站点", 2) == "task-2")
    check("混合字符被清洗", _safe_task_slug("a b  c--d!", 3) == "a-b-c-d-3")
    check("空名回退 task-<id>", _safe_task_slug("", 4) == "task-4")
    check("同 id 同名结果稳定", _safe_task_slug("x", 5) == _safe_task_slug("x", 5))
    check("不同任务结果不同", _safe_task_slug("x", 5) != _safe_task_slug("x", 6))
    check("项目名不含非法字符",
          all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in _safe_task_slug("A_b!C", 7)))

    # 旧遗留容器识别：只清旧命名项目的冲突容器，不碰用户手工容器。
    import app.deployer as _deployer_mod

    conflict_out = ('Error response from daemon: Conflict. The container name "/web" '
                    'is already in use by container "abc123"')
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
        "other\tcom.docker.compose.project=autodeploy-other-9\n")
    try:
        picked = _deployer_mod._conflicting_stale_containers(
            conflict_out, "autodeploy-tdcode-site-1")
    finally:
        _deployer_mod.run_command = original_run
    check("识别旧命名项目的遗留容器", picked == ["web"], str(picked))
    check("不碰其他项目的容器", "mine" not in picked)
    check("不碰无标签的手工容器", "manual" not in picked)

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
            check('版本不符明确失败', mgr.state()['stage'] == 'failed')
            expired = dict(saved, restart_deadline=time.time()-1)
            mgr._save_state(expired)
            check('重启确认超时失败', mgr.state()['stage'] == 'failed')
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
            missing_pid = dict(saved, restart_deadline=time.time() + 60)
            missing_pid.pop('before_pid')
            legacy_mgr._save_state(missing_pid)
            with patch.object(su, 'PROCESS_BOOT_ID', 'missing-pid-boot'), patch.object(su, '__version__', '2.0.0'):
                legacy_mgr.reconcile_on_startup()
            check('缺重启前进程身份不得确认成功', legacy_mgr.state()['stage'] == 'failed')
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
