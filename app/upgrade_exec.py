"""独立升级执行器：在被升级容器之外完成切换、验证与恢复。

面板以 ``docker run`` 启动本模块（root 用户，挂载 Docker socket、安装目录
与同一数据卷），它读取数据卷中的升级计划，把进度与终态写回同一份状态文件，
最后退出。与被替换的 Web 容器是兄弟关系：Web 重建不影响升级推进。

只支持 update/rollback 两类操作：目标镜像、compose 项目与配置文件全部来自
面板预检，本模块再做一次交叉校验（以容器真实标签为准，防计划被篡改），
任何拿不准的上下文都会在改动之前拒绝。不接受任意命令或路径。

用法：python -m app.upgrade_exec --plan /app/data/upgrade-plan.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .executor import run_command
# selfupdate 顶层已导入 config：DATA_DIR 由 AUTODEPLOY_DATA_DIR 环境变量决定，
# 执行器容器挂载同一数据卷到相同路径，状态/计划/快照因此与面板共享。
from .selfupdate import (
    IMAGE_ENV_KEY,
    IMAGE_TAG_ENV_KEY,
    TERMINAL_STAGES,
    append_history_entry,
    clean_compose_env,
    drop_env_snapshot,
    load_state_file,
    log_line,
    marker_file_path,
    normalize_tag,
    parse_image_ref,
    read_env_snapshot,
    restore_env_snapshot,
    rewrite_env_content,
    save_env_snapshot,
    save_state_file,
    write_env_atomic,
)

RECREATE_TIMEOUT = 300
MARKER_TIMEOUT = 240
HEALTH_TIMEOUT = 240
COMPOSE_UP_TIMEOUT = 600


class ExecutorError(RuntimeError):
    """执行器业务失败（区别于意外异常，走同一套恢复逻辑）。"""


# ---------------------------------------------------------------------------
# 计划与上下文校验
# ---------------------------------------------------------------------------

def load_plan(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutorError(f"无法读取升级计划：{exc}") from exc
    if not isinstance(raw, dict):
        raise ExecutorError("升级计划格式无效")
    operation_id = str(raw.get("operation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", operation_id):
        raise ExecutorError("升级计划操作标识无效")
    if raw.get("operation") not in ("update", "rollback"):
        raise ExecutorError("升级计划操作类型无效")
    target = str(raw.get("target_image_ref") or "")
    parsed = parse_image_ref(target)
    if not parsed or not (parsed[1] or parsed[2]):
        raise ExecutorError("升级计划目标镜像引用无效")
    for key in ("web_container_id", "from_image_ref", "before_boot_id",
                "working_dir", "service", "project"):
        if not str(raw.get(key) or "").strip():
            raise ExecutorError(f"升级计划缺少 {key}")
    if not re.fullmatch(r"[0-9a-f]{12,64}", str(raw["web_container_id"])):
        raise ExecutorError("升级计划 web_container_id 无效")
    files = raw.get("config_files")
    workdir = str(raw["working_dir"])
    if not isinstance(files, list) or not files:
        raise ExecutorError("升级计划配置文件列表无效")
    try:
        workdir_resolved = str(Path(workdir).resolve())
    except OSError:
        workdir_resolved = workdir
    # macOS 等 /var 为软链的环境下，working_dir 与面板 resolve 后的文件路径
    # 前缀可能不同（/var/... vs /private/var/...），按解析后的前缀比较。
    if not all(isinstance(f, str) and (f.startswith(workdir) or f.startswith(workdir_resolved))
               for f in files):
        raise ExecutorError("升级计划配置文件列表无效")
    return raw


def compose_command(plan: dict[str, Any], *args: str) -> list[str]:
    """按原部署身份构造 compose 命令：项目名、有序配置文件、工作目录。"""
    command = ["docker", "compose", "-p", str(plan["project"]),
               "--project-directory", str(plan["working_dir"])]
    for file in plan["config_files"]:
        command += ["-f", file]
    return command + list(args)


def inspect_web_container(plan: dict[str, Any]) -> dict[str, Any]:
    """读取目标容器真实标签并与计划交叉校验（计划理论上可被篡改）。"""
    cid = str(plan["web_container_id"])
    result = run_command(
        ["docker", "inspect", cid, "--format",
         '{{json .Config.Labels}}\t{{.Config.Image}}\t{{.Image}}\t{{.State.Running}}'],
        timeout=15,
    )
    if not result.ok:
        raise ExecutorError(f"无法读取目标容器信息：{result.error or result.output[-200:]}")
    parts = result.output.strip().split("\t")
    try:
        labels = json.loads(parts[0]) if parts and parts[0] else {}
    except json.JSONDecodeError:
        labels = {}
    if not isinstance(labels, dict):
        labels = {}
    project = str(labels.get("com.docker.compose.project") or "")
    workdir = str(labels.get("com.docker.compose.project.working_dir") or "")
    service = str(labels.get("com.docker.compose.service") or "")
    files_label = str(labels.get("com.docker.compose.project.config_files") or "")

    def _canon(value: str) -> str:
        # macOS 等 /var 为软链的环境下，resolve 前后前缀不同；容器标签里存的
        # 是 compose 调用时的原样路径，按解析后的绝对路径比较才稳定。
        try:
            return str(Path(value).resolve())
        except OSError:
            return value

    real_files = sorted(_canon(item.strip()) for item in files_label.split(",") if item.strip())
    plan_files = sorted(_canon(item) for item in plan["config_files"])
    if (project != plan["project"] or _canon(workdir) != _canon(str(plan["working_dir"]))
            or service != plan["service"] or real_files != plan_files):
        raise ExecutorError("计划中的 compose 上下文与容器真实标签不一致，已中止")
    image_ref = parts[1].strip() if len(parts) > 1 else ""
    image_id = parts[2].strip() if len(parts) > 2 else ""
    running = len(parts) > 3 and parts[3].strip().lower() == "true"
    if image_ref != plan["from_image_ref"]:
        raise ExecutorError(
            f"当前镜像引用 {image_ref} 与升级前记录 {plan['from_image_ref']} 不一致，环境已变化，已中止")
    if not running:
        raise ExecutorError("目标容器不在运行状态，已中止")
    return {"old_cid": cid, "image_ref": image_ref, "image_id": image_id}


def resolve_target(plan: dict[str, Any], log) -> tuple[str, str]:
    """确定切换目标：优先使用记录的旧镜像 ID（回退），否则确保目标镜像在本地。"""
    repo = parse_image_ref(str(plan["target_image_ref"]))
    if not repo:
        raise ExecutorError("目标镜像引用无法解析")
    repo = repo[0]
    ref = str(plan["target_image_ref"])
    restore_id = str(plan.get("restore_image_id") or "")
    if restore_id:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", restore_id):
            raise ExecutorError("恢复镜像 ID 格式无效")
        local_tag = f"{repo}:rollback-{plan['operation_id'][:12]}"
        result = run_command(["docker", "tag", restore_id, local_tag], timeout=30)
        if not result.ok:
            raise ExecutorError(f"无法把旧镜像标记为 {local_tag}：{result.error or result.output[-200:]}")
        log(f"使用记录的升级前镜像 ID（本地标记 {local_tag}），恢复内容与升级前逐比特一致")
        ref = local_tag
    image_id = inspect_image_id(ref)
    if not image_id:
        if restore_id:
            raise ExecutorError("刚标记的恢复镜像不在本地，环境异常")
        log(f"目标镜像 {ref} 不在本地，开始拉取（可能需要几分钟）…")
        result = run_command(["docker", "pull", ref], timeout=1800, log=log)
        if not result.ok:
            raise ExecutorError(f"镜像拉取失败（{ref}）：{result.error or '见上方日志'}")
        image_id = inspect_image_id(ref)
        if not image_id:
            raise ExecutorError("拉取后无法读取镜像 ID")
    return ref, image_id


def inspect_image_id(ref: str) -> str:
    result = run_command(["docker", "image", "inspect", "--format", "{{.Id}}", ref], timeout=20)
    if result.ok:
        lines = result.output.strip().splitlines()
        if lines and re.fullmatch(r"sha256:[a-f0-9]{64}", lines[0].strip()):
            return lines[0].strip()
    return ""


def assert_compose_image(plan: dict[str, Any], expected_ref: str) -> None:
    """切换后验证合并配置中的镜像确实受 .env 控制并指向目标。

    这一步拦下"override 写死 image"的安装：改了 .env 也不生效，必须回退。
    """
    result = run_command([*compose_command(plan, "config", "--format", "json")],
                         timeout=60, base_env=clean_compose_env())
    if not result.ok:
        raise ExecutorError(f"compose 配置校验失败：{result.error or result.output[-300:]}")
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"compose config 输出不是有效 JSON：{exc}") from exc
    services = payload.get("services") if isinstance(payload, dict) else None
    service = services.get(str(plan["service"])) if isinstance(services, dict) else None
    image = str((service or {}).get("image") or "") if isinstance(service, dict) else ""
    if image != expected_ref:
        raise ExecutorError(
            f"合并配置的镜像为 {image or '（空）'}，不是目标 {expected_ref}；"
            "镜像不受安装目录 .env 控制（可能被 override 写死），已中止并还原")


# ---------------------------------------------------------------------------
# 验证阶段
# ---------------------------------------------------------------------------

def container_state(cid: str) -> dict[str, Any]:
    result = run_command(["docker", "inspect", cid, "--format", "{{json .State}}"], timeout=15)
    if result.ok:
        try:
            data = json.loads(result.output)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def current_service_cid(plan: dict[str, Any]) -> str:
    result = run_command([*compose_command(plan, "ps", "-q", str(plan["service"]))],
                         timeout=30, base_env=clean_compose_env())
    if result.ok:
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return ""


def wait_recreate(plan: dict[str, Any], old_cid: str, log) -> str:
    deadline = time.time() + RECREATE_TIMEOUT
    while time.time() < deadline:
        cid = current_service_cid(plan)
        if cid and cid != old_cid:
            state = container_state(cid)
            # 真实 docker inspect 的 State JSON 键是大写 Running；
            # 兼容小写（测试替身）与缺失（拿不到状态时按 cid 变化继续）。
            running = state.get("Running", state.get("running"))
            if running is not False:
                log(f"检测到重建后的新容器 {cid[:12]}")
                return cid
        time.sleep(2)
    raise ExecutorError(f"等待容器重建超时（{RECREATE_TIMEOUT} 秒）")


def wait_marker(plan: dict[str, Any], new_cid: str, log) -> dict[str, Any]:
    """等待新进程启动标记（版本、身份、容器对应关系），异常退出立即报错。"""
    deadline = time.time() + MARKER_TIMEOUT
    path = marker_file_path()
    while time.time() < deadline:
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = None
        if isinstance(marker, dict) and marker.get("operation_id") == plan["operation_id"]:
            expected = normalize_tag(str(plan.get("expected_version") or ""))
            version = normalize_tag(str(marker.get("version") or ""))
            if expected and version != expected:
                raise ExecutorError(f"新进程报告版本 {version} 与目标 {expected} 不符")
            marker_cid = str(marker.get("container_id") or "")
            # marker 里的容器 ID 来自新进程的 hostname（12 位短 ID），compose ps
            # 返回完整 64 位 ID：按前缀比较，而不是全等。
            if (marker_cid and new_cid
                    and not new_cid.startswith(marker_cid)
                    and not marker_cid.startswith(new_cid)):
                raise ExecutorError("启动标记中的容器与重建后的容器不一致")
            log("新进程已报告版本与进程身份")
            return marker
        state = container_state(new_cid)
        running = state.get("Running", state.get("running"))
        restarting = state.get("Restarting", state.get("restarting"))
        if state and running is False and not restarting:
            raise ExecutorError(f"新容器异常退出（exitCode={state.get('ExitCode')}）")
        time.sleep(2)
    raise ExecutorError(f"等待新进程启动标记超时（{MARKER_TIMEOUT} 秒）")


def wait_health(cid: str, log) -> None:
    """等待新版本就绪：优先 docker HEALTHCHECK，没有则容器内请求健康端点。"""
    deadline = time.time() + HEALTH_TIMEOUT
    logged = False
    while time.time() < deadline:
        result = run_command(["docker", "inspect", cid, "--format", "{{json .State.Health}}"], timeout=15)
        output = result.output.strip() if result.ok else ""
        if output and output != "null":
            try:
                health = json.loads(output)
                status = str((health or {}).get("Status") or "")
                if status == "healthy":
                    log("健康检查通过（docker HEALTHCHECK）")
                    return
                if status == "unhealthy":
                    raise ExecutorError("新版本健康检查失败（docker 报告 unhealthy）")
            except json.JSONDecodeError:
                pass
        else:
            probe = run_command(
                ["docker", "exec", cid, "python", "-c",
                 "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=5)"],
                timeout=30)
            if probe.ok:
                log("容器内健康端点可访问")
                return
        if not logged:
            log("等待新版本通过健康检查…")
            logged = True
        time.sleep(3)
    raise ExecutorError(f"新版本未在 {HEALTH_TIMEOUT} 秒内就绪")


def confirm_image(cid: str, target_id: str) -> None:
    if not target_id:
        return
    result = run_command(["docker", "inspect", cid, "--format", "{{.Image}}"], timeout=15)
    actual = result.output.strip() if result.ok else ""
    if actual and actual != target_id:
        raise ExecutorError(f"重建容器运行的镜像 {actual} 与目标 {target_id} 不一致")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _set_stage(state: dict[str, Any], stage: str) -> None:
    state["stage"] = stage
    if not save_state_file(state):
        raise ExecutorError("状态写入被拒绝（存在更新的操作记录），执行器退出")


def run(plan_path: Path) -> int:
    state = load_state_file()
    plan = load_plan(plan_path)
    operation_id = str(plan["operation_id"])
    if state.get("operation_id") != operation_id:
        print(f"状态文件的操作 {state.get('operation_id')!r} 与计划不符，拒绝执行", file=sys.stderr)
        return 2
    if state.get("stage") in TERMINAL_STAGES:
        print("该操作已有终态，拒绝重复执行", file=sys.stderr)
        return 2

    def log(message: str) -> None:
        log_line(state, message)
        save_state_file(state)

    progress: dict[str, Any] = {"switched": False, "switched_sha": "", "env_path": None,
                                "snapshot": None, "target_ref": ""}
    try:
        _set_stage(state, "preparing")
        context = inspect_web_container(plan)
        target_ref, target_id = resolve_target(plan, log)
        progress["target_ref"] = target_ref
        env_path = Path(str(plan["working_dir"])) / ".env"
        if env_path.is_symlink():
            raise ExecutorError(".env 是软链，拒绝切换")
        if not env_path.is_file():
            raise ExecutorError("安装目录下没有 .env")
        progress["env_path"] = env_path
        # 写入能力探针：只读挂载等问题在这一步暴露，而不是切换失败后。
        try:
            probe_fd, probe_name = tempfile.mkstemp(prefix=".upgrade-probe-", dir=str(env_path.parent))
            os.close(probe_fd)
            os.unlink(probe_name)
        except OSError as exc:
            raise ExecutorError(f"安装目录不允许创建临时文件（可能是只读挂载）：{exc}") from exc
        snapshot = read_env_snapshot(env_path)
        save_env_snapshot(operation_id, snapshot)
        progress["snapshot"] = snapshot
        state["from_image_id"] = context["image_id"]
        state["to_image_id"] = target_id
        save_state_file(state)
        log("切换前准备完成：.env 快照与新旧镜像身份均已记录")
        _set_stage(state, "prepared")
        _set_stage(state, "switching")

        repo = parse_image_ref(target_ref)
        assert repo
        new_content = rewrite_env_content(
            env_path.read_text(encoding="utf-8"),
            {IMAGE_ENV_KEY: repo[0], IMAGE_TAG_ENV_KEY: repo[1] or "latest"})
        switched_sha = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        write_env_atomic(env_path, new_content, int(snapshot.get("mode") or 0o600))
        progress["switched"] = True
        progress["switched_sha"] = switched_sha
        log(f".env 已切换：{repo[0]}:{repo[1] or 'latest'}")
        assert_compose_image(plan, target_ref)
        log("合并配置校验通过：镜像由 .env 控制并指向目标")
        result = run_command([*compose_command(plan, "up", "-d", "--no-build", str(plan["service"]))],
                             timeout=COMPOSE_UP_TIMEOUT, log=log, base_env=clean_compose_env())
        if not result.ok:
            raise ExecutorError(f"容器重建失败：{result.error or result.output[-300:]}")

        _set_stage(state, "verifying")
        new_cid = wait_recreate(plan, context["old_cid"], log)
        marker = wait_marker(plan, new_cid, log)
        wait_health(new_cid, log)
        confirm_image(new_cid, target_id)
        state.update(stage="done",
                     version=str(marker.get("version") or plan.get("expected_version") or ""),
                     confirmed_operation=str(plan["operation"]),
                     confirmed_operation_id=operation_id,
                     boot_id=str(marker.get("boot_id") or ""),
                     pid=marker.get("pid"),
                     container_id=str(marker.get("container_id") or ""),
                     error="")
        log("新版本验证通过：镜像身份、启动标记与健康检查全部一致")
        save_state_file(state)
        append_history_entry(state)
        drop_env_snapshot(operation_id)
        return 0
    except ExecutorError as exc:
        return _fail(plan, state, progress, str(exc), log)
    except Exception as exc:  # 意外异常同样走恢复，绝不停在半切换状态
        return _fail(plan, state, progress, f"升级执行器内部错误：{exc}", log)


def _fail(plan: dict[str, Any], state: dict[str, Any], progress: dict[str, Any],
          error: str, log) -> int:
    """失败收尾：切换过就自动恢复旧版本；恢复失败转人工处置。"""
    operation_id = str(plan["operation_id"])
    operation = str(plan["operation"])
    env_path: Path | None = progress.get("env_path")
    stage = "failed"
    if progress.get("switched") and env_path is not None:
        try:
            restore_env_snapshot(env_path, progress["snapshot"] or {}, progress["switched_sha"])
            log(".env 已恢复到操作前快照")
            result = run_command([*compose_command(plan, "up", "-d", "--no-build", str(plan["service"]))],
                                 timeout=600, log=log, base_env=clean_compose_env())
            if not result.ok:
                raise ExecutorError(f"恢复容器失败：{result.error or result.output[-300:]}")
            if operation == "update":
                stage = "rolled_back"
                error += "；已自动恢复到升级前版本"
            else:
                error += "；回退未完成，.env 与容器已恢复到回退前状态"
            log("旧版本容器已恢复运行")
        except Exception as restore_exc:
            stage = "recovery_required"
            error += f"；自动恢复失败：{restore_exc}；请人工处理（快照保留在数据目录 upgrade-{operation_id}.env.snapshot）"
    state.update(stage=stage, error=error)
    log(error)
    if save_state_file(state):
        append_history_entry(state)
    if stage != "recovery_required":
        drop_env_snapshot(operation_id)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.upgrade_exec", description="AutoDeploy 独立升级执行器")
    parser.add_argument("--plan", required=True, help="升级计划 JSON 路径（数据卷内）")
    args = parser.parse_args(argv)
    return run(Path(args.plan))


if __name__ == "__main__":
    sys.exit(main())
