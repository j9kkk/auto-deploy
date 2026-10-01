"""Isolated rollback regressions; no server, external commands or real data access."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def run_checks(ok, TMP):
    """Run with the selfcheck callback and temporary root supplied by the caller."""
    with tempfile.TemporaryDirectory(prefix="task-rollback-", dir=TMP) as directory:
        root = Path(directory).resolve()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"AUTODEPLOY_DATA_DIR": str(root / "data")}))
            from app import config

            stack.enter_context(patch.multiple(
                config, DATA_DIR=root / "data", DB_PATH=root / "data" / "check.db",
                RELEASES_DIR=root / "data" / "releases", LOGS_DIR=root / "data" / "logs",
                WORKSPACES_DIR=root / "data" / "workspaces", ARTIFACTS_DIR=root / "data" / "artifacts",
                load_settings=lambda: config.Settings(),
            ))
            from fastapi import FastAPI
            from app import deployer
            from app.api import deps, tasks
            from app.db import Database
            from app.executor import CommandResult
            from app.store import Store

            # Any accidental script/restart invocation fails before spawning anything.
            command_guard = stack.enter_context(patch.object(
                deployer, "run_command", side_effect=AssertionError("回归测试禁止执行真实命令"),
            ))
            swaps = stack.enter_context(patch.object(deployer, "swap_symlink", wraps=deployer.swap_symlink))
            store = Store(Database(root / "check.db"))
            stack.callback(store.close)

            class AdmissionLock:
                def __init__(self):
                    self.lock = threading.RLock()
                    self.held = False
                    self.on_enter = None

                def __enter__(self):
                    self.lock.acquire()
                    self.held = True
                    if self.on_enter:
                        callback, self.on_enter = self.on_enter, None
                        callback()
                    return self

                def __exit__(self, *_args):
                    self.held = False
                    self.lock.release()

            lock = AdmissionLock()
            blocked = [False]
            dispatched = []
            outcomes = []
            rollback_log = []

            def fake_execute_rollback(run_id, task_row, selected_run, **kwargs):
                dispatched.append((run_id, task_row, selected_run, kwargs))
                # 复用真实 rollback_task 的校验与执行语义：切换软链、必要时执行
                # 命令（由外层 command_guard/fake_command 接管），并按结果落终态。
                lines: list[str] = []
                ok_flag, message = deployer.rollback_task(
                    task_row,
                    log=lines.append,
                    timeout=kwargs.get("timeout", 300),
                    kill_grace_seconds=kwargs.get("kill_grace_seconds", 7),
                    selected_run=selected_run,
                )
                rollback_log.extend(lines)
                rollback_log.append(message)
                status = "success" if ok_flag else "failed"
                store.runs.mark_running(run_id)
                store.runs.mark_finished(
                    run_id, status=status, exit_code=0 if ok_flag else 1, error="" if ok_flag else message
                )
                store.tasks.record_run_finished(
                    int(task_row["id"]), run_id=run_id, status=status, duration_ms=1
                )
                outcomes.append((run_id, ok_flag, message))

            service = SimpleNamespace(
                store=store, settings=config.Settings(), scheduler=SimpleNamespace(_lock=lock),
                selfupdate=SimpleNamespace(deployment_blocked=lambda: blocked[0]),
                runner=SimpleNamespace(execute_rollback=fake_execute_rollback),
            )
            service.settings.kill_grace_seconds = 7
            app = FastAPI()
            app.state.service = service
            app.include_router(tasks.router)
            admin = {"username": "回滚测试管理员", "is_admin": True}
            app.dependency_overrides[deps.current_user] = lambda: admin

            async def send_request(body, task_id):
                messages = []
                sent = False

                async def receive():
                    nonlocal sent
                    if sent:
                        return {"type": "http.disconnect"}
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}

                async def send(message):
                    messages.append(message)

                path = f"/api/tasks/{task_id}/rollback"
                await app({
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
                    "query_string": b"", "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 1), "server": ("rollback.test", 80), "root_path": "",
                }, receive, send)
                status = next(m["status"] for m in messages if m["type"] == "http.response.start")
                result = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
                return status, json.loads(result)

            def request(payload=None, *, task_id=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                result = asyncio.run(send_request(body, task_id or task["id"]))
                if result[0] == 202:
                    # API 在独立线程里跑回滚，等它落终态再断言，避免竞态。
                    run_id = result[1]["run_id"]
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        row = store.runs.get(run_id)
                        if row is not None and row["status"] not in ("queued", "running"):
                            break
                        time.sleep(0.02)
                return result

            task_id = store.tasks.create({
                "name": "回滚隔离测试", "repo_url": "https://example.invalid/rollback.git",
                "deploy_method": "release", "target_dir": str(root / "target"),
            })
            task = store.tasks.get(task_id)
            other_id = store.tasks.create({"name": "另一个任务", "repo_url": "https://example.invalid/other.git"})
            other = store.tasks.get(other_id)
            releases, current = deployer.resolve_release_paths(task)
            releases.mkdir(parents=True)
            old = releases / "20260101-010101-aaaaaaaa"
            middle = releases / "20260102-010101-bbbbbbbb"
            latest = releases / "20260103-010101-cccccccc"
            for release in (old, middle, latest):
                release.mkdir()

            def add_run(path, *, owner=None, status="success"):
                run_id = store.runs.create(owner or task)
                store.runs.update_outputs(run_id, release_dir=str(path) if path else "")
                store.runs.update_checkout_info(run_id, commit_after="a" * 40)
                store.runs.mark_finished(run_id, status=status, exit_code=0 if status == "success" else 1)
                return run_id

            old_id = add_run(old)
            add_run(middle)
            latest_id = add_run(latest)
            deployer.swap_symlink(current, latest)
            # 回滚已异步化：接口只做准入并返回 202 + 新 run_id，切换由后台 runner 完成。
            status, result = request({"run_id": old_id})
            ok("指定运行回滚到所选版本而非最近版本", status == 202
               and result["run_id"] > 0 and dispatched
               and dispatched[0][1]["id"] == int(task["id"])
               and (dispatched[0][2] or {}).get("id") == old_id
               and current.resolve() == old)
            deployer.swap_symlink(current, latest)
            status, result = request()
            ok("无请求体兼容上一版本回滚", status == 202 and result["run_id"] > 0
               and len(dispatched) == 2 and dispatched[1][2] is None
               and current.resolve() == middle)
            deployer.swap_symlink(current, latest)

            def rejected(label, payload, expected=400):
                """非法回滚请求的两层防线：接口 4xx 拒绝，或 202 受理后后台运行失败。"""
                before = os.readlink(current)
                calls = command_guard.call_count
                swap_count = swaps.call_count
                status, result = request(payload)
                if status == 202:
                    # 已受理：请求本身合法，但后台校验应判失败且不切换软链。
                    row = store.runs.get(result["run_id"])
                    ok(label, row is not None and row["status"] == "failed"
                       and os.readlink(current) == before
                       and command_guard.call_count == calls and swaps.call_count == swap_count,
                       f"运行状态 {(row or {}).get('status')} 错误 {(row or {}).get('error')}")
                    return
                ok(label, status == expected and os.readlink(current) == before
                   and command_guard.call_count == calls and swaps.call_count == swap_count, str(result))

            rejected("跨任务运行不可回滚", {"run_id": add_run(old, owner=other)})
            for run_status in ("failed", "cancelled", "skipped"):
                rejected(f"拒绝{run_status}运行版本", {"run_id": add_run(old, status=run_status)})
            rejected("无发布目录运行不可回滚", {"run_id": add_run(None)})
            rejected("当前版本不可再次回滚", {"run_id": latest_id})
            rejected("不存在的运行不可回滚", {"run_id": 999999})
            missing = releases / "20260104-010101-dddddddd"
            missing.mkdir()
            missing_id = add_run(missing)
            missing.rmdir()
            rejected("已删除目录不可回滚", {"run_id": missing_id})
            outside = root / "20260105-010101-eeeeeeee"
            outside.mkdir()
            (outside / "保留.txt").write_text("不能修改", encoding="utf-8")
            rejected("外部目录不可回滚", {"run_id": add_run(outside)})
            rejected("上级路径穿越不可回滚", {"run_id": add_run(str(releases / ".." / "releases" / old.name))})
            rejected("相对路径不可回滚", {"run_id": add_run(old.name)})
            rejected("发布根目录不可作为版本", {"run_id": add_run(releases)})
            wrong = releases / "不是发布版本"
            wrong.mkdir()
            rejected("错误目录名不可回滚", {"run_id": add_run(wrong)})
            nested = old / "20260106-010101-ffffffff"
            nested.mkdir()
            rejected("嵌套目录不可回滚", {"run_id": add_run(nested)})
            regular = releases / "20260107-010101-11111111"
            regular.write_text("不是目录", encoding="utf-8")
            rejected("普通文件不可作为版本", {"run_id": add_run(regular)})
            linked = releases / "20260108-010101-22222222"
            linked.symlink_to(outside, target_is_directory=True)
            rejected("版本软链外逃不可回滚", {"run_id": add_run(linked)})
            linked.unlink()
            linked.symlink_to(old, target_is_directory=True)
            rejected("发布目录别名软链不可回滚", {"run_id": add_run(linked)})
            linked.unlink()
            linked.symlink_to(linked)
            rejected("循环软链不可回滚", {"run_id": add_run(linked)})
            linked.unlink()
            deployer.swap_symlink(current, outside)
            rejected("current 外逃时拒绝切换", {"run_id": old_id})
            deployer.swap_symlink(current, latest)
            saved_root = releases.with_name("releases-backup")
            releases.rename(saved_root)
            releases.symlink_to(saved_root, target_is_directory=True)
            rejected("发布根目录软链不可回滚", {"run_id": old_id})
            releases.unlink()
            saved_root.rename(releases)
            ok("越界拒绝未修改外部内容", (outside / "保留.txt").read_text(encoding="utf-8") == "不能修改")

            for invalid in (True, False, 0, -1, "1", "../1", 1.2, None, 2**63, [old_id]):
                rejected(f"无效运行编号被拒绝 {invalid!r}", {"run_id": invalid}, 422)
            rejected("空对象不降级为上一版回滚", {}, 422)
            rejected("禁止客户端注入发布路径", {"run_id": old_id, "release_dir": str(old)}, 422)
            rejected("禁止仅提交客户端目录", {"release_dir": str(old)}, 422)

            active_id = store.runs.create(task)
            rejected("排队任务阻止回滚", {"run_id": old_id}, 409)
            store.runs.mark_running(active_id)
            rejected("运行中任务阻止回滚", {"run_id": old_id}, 409)
            store.runs.mark_finished(active_id, status="cancelled", exit_code=1)
            active_on_entry = []
            lock.on_enter = lambda: active_on_entry.append(store.runs.create(task))
            rejected("取得调度锁后重新检查活动状态", {"run_id": old_id}, 409)
            store.runs.mark_finished(active_on_entry[0], status="cancelled", exit_code=1)
            lock.on_enter = lambda: blocked.__setitem__(0, True)
            rejected("取得调度锁后检查系统更新阻塞", {"run_id": old_id}, 409)
            blocked[0] = False
            original_has_active = store.runs.has_active_for_task
            checks_under_lock = []

            def check_active(value):
                checks_under_lock.append(lock.held)
                return original_has_active(value)

            with patch.object(store.runs, "has_active_for_task", check_active):
                rejected("锁内校验完成后仍拒绝当前版本", {"run_id": latest_id})
            ok("活动运行检查始终在调度锁内", checks_under_lock == [True])

            app.dependency_overrides[deps.current_user] = lambda: {"username": "只读用户", "is_admin": False}
            rejected("非管理员禁止回滚", {"run_id": old_id}, 403)
            app.dependency_overrides.pop(deps.current_user)
            rejected("未登录禁止回滚", {"run_id": old_id}, 401)
            app.dependency_overrides[deps.current_user] = lambda: admin

            # Historical ancestor aliases are equivalent paths, not escapes.
            alias = root / "target-alias"
            alias.symlink_to(root / "target", target_is_directory=True)
            alias_id = add_run(alias / "releases" / old.name)
            status, result = request({"run_id": alias_id})
            ok("运行记录祖先软链别名可回滚", status == 202 and current.resolve() == old)
            deployer.swap_symlink(current, latest)
            if str(old).startswith("/private/"):
                mac_alias = Path(str(old)[len("/private"):])
                if mac_alias.exists() and mac_alias.resolve() == old:
                    status, result = request({"run_id": add_run(mac_alias)})
                    ok("macOS临时目录祖先别名可回滚", status == 202 and current.resolve() == old)
                    deployer.swap_symlink(current, latest)

            # Legacy previous_release accepted non-hidden direct child names.
            wrong.rmdir()
            legacy = releases / "legacy-release"
            legacy.mkdir()
            status, result = request()
            ok("旧无请求体回滚兼容非标准历史目录名", status == 202 and current.resolve() == legacy)
            legacy.rmdir()
            deployer.swap_symlink(current, missing)
            status, result = request()
            ok("旧无请求体回滚兼容根内悬空current", status == 202 and current.resolve() == latest)

            # Exercise the actual generator, including nogit and collision suffix,
            # through the default layout with a configured ancestor alias.
            (root / "data").mkdir(exist_ok=True)
            data_alias = root / "data-alias"
            data_alias.symlink_to(root / "data", target_is_directory=True)
            with patch.object(config, "RELEASES_DIR", data_alias / "releases"):
                other_releases, other_current = deployer.resolve_release_paths(other)
                with patch.object(deployer.time, "strftime", return_value="20260201-010101"):
                    generated_old = deployer.next_release_dir(other_releases, "")
                    generated_old.mkdir()
                    generated_new = deployer.next_release_dir(other_releases, "")
                    generated_new.mkdir()
                generated_id = add_run(generated_old, owner=other)
                deployer.swap_symlink(other_current, generated_new)
                status, result = request({"run_id": generated_id}, task_id=other_id)
                ok("默认发布布局兼容祖先别名与nogit目录", status == 202 and other_current.resolve() == generated_old.resolve())
                deployer.swap_symlink(other_current, generated_new)
                status, result = request(task_id=other_id)
                ok("默认布局上一版不误选当前且兼容生成序号", status == 202
                   and generated_new.name.endswith("-nogit-2") and other_current.resolve() == generated_old.resolve())
                generated_new_id = add_run(generated_new, owner=other)
                status, result = request({"run_id": generated_new_id}, task_id=other_id)
                ok("指定运行兼容真实生成的重名序号", status == 202 and other_current.resolve() == generated_new.resolve())

            store.tasks.update(task_id, {"target_dir": str(root / "changed-target")})
            rejected("目标目录配置改变时不跨旧根回滚", {"run_id": old_id})
            store.tasks.update(task_id, {"target_dir": str(root / "target"), "deploy_method": "systemd", "service_name": "changed.service"})
            rejected("部署方式改变时不运行错误服务命令", {"run_id": old_id})
            for method in ("docker", "docker_compose", "rsync", "artifact"):
                store.tasks.update(task_id, {"deploy_method": method})
                matching_id = add_run(old, owner={**task, "deploy_method": method})
                status, result = request({"run_id": matching_id})
                limitation = "不会自动重新打包、替换已有产物或执行部署" if method == "artifact" else "不会自动重新部署容器或同步远端"
                outcome = next(o for o in outcomes if o[0] == result["run_id"])
                ok(f"{method}明确实际回滚限制", status == 202 and outcome[1] and limitation in outcome[2])
                deployer.swap_symlink(current, latest)
            ok("普通回滚不执行容器或远端命令", command_guard.call_count == 0)

            store.tasks.update(task_id, {"deploy_method": "systemd", "service_name": "isolated-test.service", "rollback_script": "true"})
            systemd_id = add_run(old, owner={**task, "deploy_method": "systemd"})
            calls = []

            def fake_command(args, **kwargs):
                calls.append((args, kwargs, lock.held))
                return CommandResult(command="隔离模拟", exit_code=0, output="", duration_ms=0)

            with patch.object(deployer, "run_command", fake_command), patch.object(deployer, "command_exists", return_value=True):
                status, result = request({"run_id": systemd_id})
            outcome = next(o for o in outcomes if o[0] == result["run_id"])
            ok("指定版本复用回滚脚本与systemd语义", status == 202 and len(calls) == 2
               and calls[0][1]["cwd"] == old
               # AUTODEPLOY_RUN_ID/AUTODEPLOY_COMMIT 均指向所选历史版本。
               and calls[0][1]["env"]["AUTODEPLOY_RUN_ID"] == str(systemd_id)
               and calls[0][1]["env"]["AUTODEPLOY_COMMIT"] == "a" * 40
               and calls[1][0] == ["systemctl", "restart", "isolated-test.service"]
               # 回滚在锁外后台执行：准入校验在锁内，实际执行不应占住调度锁。
               and not any(call[2] for call in calls) and outcome[1])
            ok("明确提示使用当前脚本和服务配置而非历史快照", status == 202
               and any("历史运行未保存这些配置的快照" in line for line in rollback_log))
            ok("所有命令均为模拟未实际重启", command_guard.call_count == 0)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    failures = []
    passed = []

    def check(label, condition, detail=""):
        (passed if condition else failures).append(label)
        if not condition:
            print(f"失败：{label} {detail}")

    with tempfile.TemporaryDirectory(prefix="rollback-checks-") as temporary:
        run_checks(check, Path(temporary))
    print(f"回滚回归通过 {len(passed)} 项，失败 {len(failures)} 项")
    raise SystemExit(bool(failures))
