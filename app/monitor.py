"""Host and task resource monitoring engine.

Collects CPU, memory, disk, network, and Docker container resource utilization
using Python standard library with multi-platform fallbacks (Linux / macOS).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .deployer import command_exists, compose_project_name, directory_size
from .schedule import iso, utcnow

logger = logging.getLogger("autodeploy.monitor")

# Beszel 风格宿主机穿透探针路径（容器挂载时自动生效）
HOST_PROC_DIR = config.DATA_DIR.parent / "host" / "proc"  # /host/proc
HOST_ROOT_DIR = Path("/host/root")


def _proc_file(rel_path: str) -> Path | None:
    """Return /host/proc/<rel_path> if present (container probe mode), else /proc/<rel_path>."""
    candidate = Path("/host/proc") / rel_path
    if candidate.exists():
        return candidate
    local = Path("/proc") / rel_path
    return local if local.exists() else None


@dataclass
class HostSnapshot:
    timestamp: str
    cpu_percent: float
    cpu_cores: int
    load_avg: list[float]  # [1m, 5m, 15m]
    memory_total: int      # bytes
    memory_used: int       # bytes
    memory_free: int       # bytes
    memory_percent: float
    swap_total: int        # bytes
    swap_used: int         # bytes
    swap_percent: float
    disk_total: int        # bytes
    disk_used: int         # bytes
    disk_free: int         # bytes
    disk_percent: float
    app_data_bytes: int    # bytes (AutoDeploy 自身数据目录真实大小)
    disk_read_rate: float  # KB/s
    disk_write_rate: float # KB/s
    net_rx_rate: float     # KB/s
    net_tx_rate: float     # KB/s
    uptime_seconds: int
    hostname: str
    os_name: str


class HostMonitor:
    """Collects host resource utilization metrics using standard library."""

    def __init__(self) -> None:
        self._last_cpu_times: tuple[int, int] | None = None  # (total, idle)
        self._last_net_bytes: tuple[float, int, int] | None = None  # (timestamp, rx, tx)
        self._last_disk_sectors: tuple[float, int, int] | None = None  # (timestamp, read_sec, write_sec)
        self._cpu_count = os.cpu_count() or 1

    def sample_host(self) -> HostSnapshot:
        """Sample host resources and calculate rates."""
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        
        cpu_percent = self._get_cpu_percent()
        load_avg = list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0]
        mem = self._get_memory()
        swap = self._get_swap()
        disk = self._get_disk()
        disk_read_rate, disk_write_rate = self._get_disk_io_rates(now)
        net_rx_rate, net_tx_rate = self._get_net_rates(now)
        uptime = self._get_uptime()
        hostname, os_name = self._get_system_info()

        return HostSnapshot(
            timestamp=now_iso,
            cpu_percent=round(cpu_percent, 1),
            cpu_cores=self._cpu_count,
            load_avg=[round(x, 2) for x in load_avg],
            memory_total=mem["total"],
            memory_used=mem["used"],
            memory_free=mem["free"],
            memory_percent=round(mem["percent"], 1),
            swap_total=swap["total"],
            swap_used=swap["used"],
            swap_percent=round(swap["percent"], 1),
            disk_total=disk["total"],
            disk_used=disk["used"],
            disk_free=disk["free"],
            disk_percent=round(disk["percent"], 1),
            app_data_bytes=disk.get("app_data_bytes", 0),
            disk_read_rate=round(disk_read_rate, 1),
            disk_write_rate=round(disk_write_rate, 1),
            net_rx_rate=round(net_rx_rate, 1),
            net_tx_rate=round(net_tx_rate, 1),
            uptime_seconds=int(uptime),
            hostname=hostname,
            os_name=os_name,
        )

    def _get_system_info(self) -> tuple[str, str]:
        """Detect hostname and operating system name, recognizing Docker Desktop VM."""
        hostname = platform.node()
        os_name = f"{platform.system()} {platform.release()}"
        
        # 识别 Docker Desktop 虚拟环境（如 macOS / Windows 上运行 Docker）
        if "linuxkit" in os_name.lower():
            os_name = "Docker Desktop (macOS / Windows 宿主)"
            hostname = "Docker 宿主虚拟环境"
        return hostname, os_name

    def _get_cpu_percent(self) -> float:
        """Calculate overall CPU percent since last sample or quick sample."""
        stat_path = _proc_file("stat")
        if stat_path:
            try:
                with open(stat_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("cpu "):
                            fields = [int(x) for x in line.split()[1:]]
                            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                            total = sum(fields)
                            if self._last_cpu_times:
                                last_total, last_idle = self._last_cpu_times
                                d_total = total - last_total
                                d_idle = idle - last_idle
                                self._last_cpu_times = (total, idle)
                                if d_total > 0:
                                    return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100))
                            self._last_cpu_times = (total, idle)
                            break
            except Exception as e:
                logger.debug("Failed reading /proc/stat: %s", e)

        # Fallback using load average divided by cpu count as rough approximation
        try:
            load = os.getloadavg()[0]
            return max(0.0, min(100.0, (load / self._cpu_count) * 100))
        except Exception:
            return 0.0

    def _get_memory(self) -> dict[str, Any]:
        """Read system memory information."""
        meminfo_path = _proc_file("meminfo")
        if meminfo_path:
            try:
                data: dict[str, int] = {}
                with open(meminfo_path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split(":")
                        if len(parts) == 2:
                            val = parts[1].strip().split()[0]
                            data[parts[0].strip()] = int(val) * 1024  # kB to bytes
                total = data.get("MemTotal", 0)
                available = data.get("MemAvailable", data.get("MemFree", 0) + data.get("Buffers", 0) + data.get("Cached", 0))
                used = max(0, total - available)
                percent = (used / total * 100) if total > 0 else 0.0
                return {"total": total, "used": used, "free": available, "percent": percent}
            except Exception as e:
                logger.debug("Failed reading meminfo: %s", e)

        # Fallback for macOS / BSD
        total_mem = 0
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            total_mem = int(out)
        except Exception:
            total_mem = 8 * 1024 * 1024 * 1024  # 8GB default fallback

        # Estimate used from vm_stat if available
        free_bytes = total_mem // 3  # default fallback
        try:
            vm = subprocess.check_output(["vm_stat"], text=True)
            page_size = 4096
            free_pages = 0
            for line in vm.splitlines():
                if "Pages free:" in line:
                    free_pages = int(line.split(":")[1].strip().rstrip("."))
            free_bytes = free_pages * page_size
        except Exception:
            pass

        used_mem = max(0, total_mem - free_bytes)
        return {
            "total": total_mem,
            "used": used_mem,
            "free": free_bytes,
            "percent": (used_mem / total_mem * 100) if total_mem else 0.0,
        }

    def _get_swap(self) -> dict[str, Any]:
        """Read system swap information."""
        meminfo_path = _proc_file("meminfo")
        if meminfo_path:
            try:
                data: dict[str, int] = {}
                with open(meminfo_path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split(":")
                        if len(parts) == 2:
                            data[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
                total = data.get("SwapTotal", 0)
                free = data.get("SwapFree", 0)
                used = max(0, total - free)
                percent = (used / total * 100) if total > 0 else 0.0
                return {"total": total, "used": used, "free": free, "percent": percent}
            except Exception:
                pass
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}

    def _get_disk(self) -> dict[str, Any]:
        """Read host disk usage and actual application data footprint."""
        # 优先探测宿主机只读挂载探针 /host/root（Beszel 风格穿透）
        probe_root = HOST_ROOT_DIR if HOST_ROOT_DIR.exists() else (config.DATA_DIR if config.DATA_DIR.exists() else Path("/"))
        app_data_bytes = 0
        if config.DATA_DIR.exists():
            try:
                app_data_bytes = directory_size(config.DATA_DIR)
            except Exception:
                pass
        try:
            total, used, free = shutil.disk_usage(str(probe_root))
            percent = (used / total * 100) if total > 0 else 0.0
            return {
                "total": total,
                "used": used,
                "free": free,
                "percent": percent,
                "app_data_bytes": app_data_bytes,
            }
        except Exception:
            return {"total": 0, "used": 0, "free": 0, "percent": 0.0, "app_data_bytes": app_data_bytes}

    def _get_disk_io_rates(self, now: float) -> tuple[float, float]:
        """Calculate aggregate disk read and write throughput in KB/s from diskstats."""
        total_read_sec = 0
        total_write_sec = 0
        diskstats_path = _proc_file("diskstats")
        if diskstats_path:
            try:
                with open(diskstats_path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 14:
                            dev = parts[2]
                            # 过滤虚拟或非物理块设备（如 loop, ram, zram）
                            if dev.startswith(("loop", "ram", "zram")):
                                continue
                            # 过滤磁盘分区（如 sda1, nvme0n1p1），只统计整盘以避免双重累计
                            if any(dev.endswith(str(i)) for i in range(10)) and not dev.startswith(("nvme", "mmcblk")):
                                continue
                            if "p" in dev and any(dev.endswith(f"p{i}") for i in range(10)):
                                continue
                            total_read_sec += int(parts[5])
                            total_write_sec += int(parts[9])
            except Exception:
                pass

        if self._last_disk_sectors:
            last_time, last_r, last_w = self._last_disk_sectors
            dt = max(0.1, now - last_time)
            # Linux 扇区固定为 512 字节 (0.5 KB)
            read_rate = max(0.0, (total_read_sec - last_r) * 0.5 / dt)
            write_rate = max(0.0, (total_write_sec - last_w) * 0.5 / dt)
            self._last_disk_sectors = (now, total_read_sec, total_write_sec)
            return read_rate, write_rate

        self._last_disk_sectors = (now, total_read_sec, total_write_sec)
        return 0.0, 0.0

    def _get_net_rates(self, now: float) -> tuple[float, float]:
        """Calculate network receive and transmit rates in KB/s."""
        total_rx = 0
        total_tx = 0
        netdev_path = _proc_file("net/dev")
        if netdev_path:
            try:
                with open(netdev_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[2:]
                    for line in lines:
                        parts = line.strip().split()
                        if len(parts) >= 10:
                            iface = parts[0].rstrip(":")
                            if iface != "lo":
                                total_rx += int(parts[1])
                                total_tx += int(parts[9])
            except Exception:
                pass

        if self._last_net_bytes:
            last_time, last_rx, last_tx = self._last_net_bytes
            dt = max(0.1, now - last_time)
            rx_rate = max(0.0, (total_rx - last_rx) / 1024.0 / dt)
            tx_rate = max(0.0, (total_tx - last_tx) / 1024.0 / dt)
            self._last_net_bytes = (now, total_rx, total_tx)
            return rx_rate, tx_rate

        self._last_net_bytes = (now, total_rx, total_tx)
        return 0.0, 0.0

    def _get_uptime(self) -> float:
        """Get system uptime in seconds."""
        uptime_path = _proc_file("uptime")
        if uptime_path:
            try:
                with open(uptime_path, "r", encoding="utf-8") as f:
                    return float(f.readline().split()[0])
            except Exception:
                pass
        try:
            # macOS sysctl kern.boottime
            out = subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True)
            # { sec = 1710000000, usec = ... }
            sec = int(out.split("sec =")[1].split(",")[0].strip())
            return max(0.0, time.time() - sec)
        except Exception:
            return 0.0


class TaskResourceMonitor:
    """Monitors resources occupied by deployed tasks (Docker containers, processes)."""

    @classmethod
    def get_task_resources(cls, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collect resource usage for each task, matching Docker containers or system services."""
        docker_stats = get_monitor().get_docker_stats()
        results = []

        for task in tasks:
            task_id = task.get("id")
            task_name = task.get("name", "")
            deploy_method = task.get("deploy_method", "script")
            enabled = bool(task.get("enabled", False))

            matched_containers = []
            # compose_inline 与 docker_compose 一样按固定项目名起容器，
            # 只是 compose 文件来自任务内容而非发布目录。
            if deploy_method in {"docker_compose", "compose_inline"}:
                proj_name = compose_project_name(task)
                matched_containers = [c for c in docker_stats.values() if c.get("compose_project") == proj_name]
            elif deploy_method == "docker":
                # 纯 docker 任务支持按镜像名匹配（含 tag 或去 tag），或容器名精确等于任务名
                image = (task.get("docker_image") or "").strip().lower()
                image_base = image.split(":")[0] if ":" in image else image
                name_slug = compose_project_name(task).replace("autodeploy-", "")
                for c in docker_stats.values():
                    c_img = (c.get("image") or "").lower()
                    c_img_base = c_img.split(":")[0] if ":" in c_img else c_img
                    c_name = (c.get("name") or "").lower()
                    if (image and (c_img == image or c_img_base == image_base)) or (c_name == task_name.lower() or c_name == name_slug):
                        matched_containers.append(c)

            # Aggregate container stats
            total_cpu = 0.0
            total_mem_bytes = 0
            mem_limit_bytes = 0
            status_summary = "stopped"
            container_details = []

            for c in matched_containers:
                total_cpu += c.get("cpu_percent", 0.0)
                total_mem_bytes += c.get("mem_usage_bytes", 0)
                mem_limit_bytes = max(mem_limit_bytes, c.get("mem_limit_bytes", 0))
                if c.get("status") == "running":
                    status_summary = "running"
                container_details.append({
                    "container_id": c.get("id", "")[:12],
                    "container_name": c.get("name"),
                    "status": c.get("status"),
                    "image": c.get("image"),
                    "cpu_percent": c.get("cpu_percent"),
                    "mem_usage_human": c.get("mem_usage_human"),
                    "mem_usage_bytes": c.get("mem_usage_bytes"),
                    "mem_limit_bytes": c.get("mem_limit_bytes"),
                    "net_io": c.get("net_io"),
                    "block_io": c.get("block_io"),
                    "pids": c.get("pids"),
                })

            mem_percent = (total_mem_bytes / mem_limit_bytes * 100) if mem_limit_bytes > 0 else 0.0

            results.append({
                "task_id": task_id,
                "task_name": task_name,
                "deploy_method": deploy_method,
                "enabled": enabled,
                "status": status_summary if matched_containers else ("not_deployed" if deploy_method in ("docker", "docker_compose") else "native"),
                "container_count": len(matched_containers),
                "cpu_percent": round(total_cpu, 1),
                "memory_used_bytes": total_mem_bytes,
                "memory_percent": round(mem_percent, 1),
                "containers": container_details,
            })

        return results

    @classmethod
    def _collect_docker_stats(cls) -> dict[str, dict[str, Any]]:
        """Run docker stats once and return parsed stats by container ID/Name."""
        stats_map: dict[str, dict[str, Any]] = {}
        try:
            # 1. Fetch container labels to identify compose projects
            ps_proc = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            labels_by_name: dict[str, dict[str, str]] = {}
            if ps_proc.returncode == 0:
                for line in ps_proc.stdout.strip().splitlines():
                    if not line.strip():
                        continue
                    try:
                        info = json.loads(line)
                        name = info.get("Names") or info.get("Name") or ""
                        labels_str = info.get("Labels") or ""
                        project = ""
                        for part in labels_str.split(","):
                            if part.startswith("com.docker.compose.project="):
                                project = part.split("=", 1)[1]
                        labels_by_name[name] = {
                            "project": project,
                            "status": "running" if "Up" in info.get("Status", "") else "exited",
                            "image": info.get("Image", ""),
                        }
                    except Exception:
                        continue

            # 2. Fetch live stats
            stats_proc = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if stats_proc.returncode == 0:
                for line in stats_proc.stdout.strip().splitlines():
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        name = raw.get("Name", "")
                        cid = raw.get("ID", "")
                        cpu_str = raw.get("CPUPerc", "0%").rstrip("%")
                        cpu_perc = float(cpu_str) if cpu_str else 0.0

                        # Memory parse e.g. "12.5MiB / 7.668GiB"
                        mem_usage_str = raw.get("MemUsage", "0B / 0B")
                        used_bytes, limit_bytes = cls._parse_mem_usage(mem_usage_str)

                        net_rx, net_tx = cls._parse_io_pair(raw.get("NetIO", ""))
                        block_read, block_write = cls._parse_io_pair(raw.get("BlockIO", ""))

                        meta = labels_by_name.get(name, {})
                        stats_map[cid] = {
                            "id": cid,
                            "name": name,
                            "compose_project": meta.get("project", ""),
                            "image": meta.get("image", ""),
                            "status": meta.get("status", "running"),
                            "cpu_percent": cpu_perc,
                            "mem_usage_human": mem_usage_str.split("/")[0].strip(),
                            "mem_usage_bytes": used_bytes,
                            "mem_limit_bytes": limit_bytes,
                            "net_io": raw.get("NetIO", "--"),
                            "block_io": raw.get("BlockIO", "--"),
                            # docker stats 给的是自容器启动以来的累计字节数，
                            # 速率必须由两次采样的差值算出。
                            "net_rx_total": net_rx,
                            "net_tx_total": net_tx,
                            "block_read_total": block_read,
                            "block_write_total": block_write,
                            "pids": int(raw.get("PIDs", 0) or 0),
                        }
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("Failed querying docker stats: %s", e)

        return stats_map

    @staticmethod
    def _parse_mem_usage(mem_str: str) -> tuple[int, int]:
        """Convert '20.5MiB / 1.5GiB' to bytes tuple."""
        units = {
            "kib": 1024, "kb": 1000,
            "mib": 1024**2, "mb": 1000**2,
            "gib": 1024**3, "gb": 1000**3,
            "tib": 1024**4, "tb": 1000**4,
            "b": 1,
        }
        def parse_part(part: str) -> int:
            part = part.strip().lower()
            for u, mul in sorted(units.items(), key=lambda x: len(x[0]), reverse=True):
                if part.endswith(u):
                    num = float(part[:-len(u)].strip())
                    return int(num * mul)
            return 0

        parts = mem_str.split("/")
        used = parse_part(parts[0]) if len(parts) > 0 else 0
        limit = parse_part(parts[1]) if len(parts) > 1 else 0
        return used, limit

    @classmethod
    def _parse_io_pair(cls, text: str) -> tuple[int, int]:
        """Convert docker stats 'NetIO' / 'BlockIO' 文本（如 '1.2kB / 3.4MB'）为字节元组。"""
        parts = (text or "").split("/")
        if len(parts) < 2:
            return 0, 0
        return cls._parse_bytes(parts[0]), cls._parse_bytes(parts[1])

    @staticmethod
    def _parse_bytes(text: str) -> int:
        """Convert a single docker stats size token ('8.19kB') to bytes."""
        token = (text or "").strip().lower()
        if not token or token == "--":
            return 0
        units = {
            "kib": 1024, "kb": 1000,
            "mib": 1024**2, "mb": 1000**2,
            "gib": 1024**3, "gb": 1000**3,
            "tib": 1024**4, "tb": 1000**4,
            "b": 1,
        }
        for unit, multiplier in sorted(units.items(), key=lambda x: len(x[0]), reverse=True):
            if token.endswith(unit):
                try:
                    return int(float(token[:-len(unit)].strip()) * multiplier)
                except ValueError:
                    return 0
        try:
            return int(float(token))
        except ValueError:
            return 0


class MonitorService:
    """Coordinates host and task metric sampling with an in-memory ring buffer for trends."""

    def __init__(self, history_size: int = 360) -> None:
        self.host_monitor = HostMonitor()
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        # 容器维度时序与主机共用「内存环形缓冲 + 定期落库」两层结构。
        # 存批而不是散点：同一批共享时间戳，正好对应一次 docker stats 采样，
        # 180 批 ≈ 最近 1 小时（docker stats 约 20 秒一轮）。
        self.container_batches: deque[list[dict[str, Any]]] = deque(maxlen=180)
        self._container_totals: dict[str, tuple[float, int, int, int, int]] = {}
        self._container_lock = threading.Lock()
        self._last_sample_time = 0.0
        # docker stats 一次要 1~3 秒，绝不能同步压在 API 请求路径上：
        # 后台线程按周期刷新缓存，API 直接读最近一次结果。
        self._docker_stats: dict[str, dict[str, Any]] = {}
        self._docker_stats_at = 0.0
        self._docker_stats_lock = threading.Lock()
        self._docker_refreshing = False

    def tick(self) -> None:
        """Called periodically (e.g. from scheduler or request) to record snapshots."""
        now = time.time()
        # Sample at most once every 10 seconds to avoid excessive CPU usage
        if now - self._last_sample_time < 10.0:
            return
        snapshot = self.host_monitor.sample_host()
        self.history.append(asdict(snapshot))
        self._last_sample_time = now
        self._maybe_refresh_docker_stats(now)

    def _maybe_refresh_docker_stats(self, now: float, force: bool = False) -> None:
        """Kick a background refresh of docker stats if the cache is stale."""
        if self._docker_refreshing:
            return
        if not force and now - self._docker_stats_at < DOCKER_STATS_TTL_SECONDS:
            return
        if not command_exists("docker"):
            return
        self._docker_refreshing = True

        def _refresh() -> None:
            try:
                stats = TaskResourceMonitor._collect_docker_stats()
                with self._docker_stats_lock:
                    self._docker_stats = stats
                    self._docker_stats_at = time.time()
                # docker stats 只给累计量（NetIO / BlockIO），速率靠相邻两次采样的
                # 差值算出，因此速率采样必须紧跟刷新，而不是另起一套定时器。
                self._record_container_samples(stats, time.time())
            except Exception as e:  # noqa: BLE001 - sampling must never raise
                logger.debug("刷新 docker stats 缓存失败: %s", e)
            finally:
                self._docker_refreshing = False

        threading.Thread(target=_refresh, name="docker-stats-sampler", daemon=True).start()

    def _record_container_samples(self, stats: dict[str, dict[str, Any]], now: float) -> None:
        """Turn one docker stats snapshot into per-container rate samples."""
        timestamp = iso(utcnow())
        samples: list[dict[str, Any]] = []
        totals: dict[str, tuple[float, int, int, int, int]] = {}
        for cid, c in stats.items():
            net_rx = int(c.get("net_rx_total") or 0)
            net_tx = int(c.get("net_tx_total") or 0)
            blk_r = int(c.get("block_read_total") or 0)
            blk_w = int(c.get("block_write_total") or 0)
            previous = self._container_totals.get(cid)
            rx_rate = tx_rate = read_rate = write_rate = 0.0
            if previous:
                last_at, last_rx, last_tx, last_br, last_bw = previous
                dt = now - last_at
                if dt > 0.5:
                    # 容器重启后累计量会归零，负差值按 0 处理，避免出现负速率尖峰。
                    rx_rate = max(0.0, (net_rx - last_rx) / 1024.0 / dt)
                    tx_rate = max(0.0, (net_tx - last_tx) / 1024.0 / dt)
                    read_rate = max(0.0, (blk_r - last_br) / 1024.0 / dt)
                    write_rate = max(0.0, (blk_w - last_bw) / 1024.0 / dt)
            totals[cid] = (now, net_rx, net_tx, blk_r, blk_w)

            limit = int(c.get("mem_limit_bytes") or 0)
            used = int(c.get("mem_usage_bytes") or 0)
            samples.append({
                "timestamp": timestamp,
                "container_id": c.get("id") or cid,
                "container_name": c.get("name") or "",
                "compose_project": c.get("compose_project") or "",
                "status": c.get("status") or "",
                "cpu_percent": round(float(c.get("cpu_percent") or 0.0), 2),
                "memory_bytes": used,
                "memory_percent": round((used / limit * 100) if limit > 0 else 0.0, 2),
                "disk_read_rate": round(read_rate, 1),
                "disk_write_rate": round(write_rate, 1),
                "net_rx_rate": round(rx_rate, 1),
                "net_tx_rate": round(tx_rate, 1),
            })

        if not samples:
            return
        with self._container_lock:
            # 只保留当前仍在的容器，避免长期运行后累计量字典无界增长；
            # 容器消失后同名重建时也自然从零开始算速率。
            self._container_totals = totals
            self.container_batches.append(samples)

    def get_container_batches(self) -> list[list[dict[str, Any]]]:
        """Return recent per-container sample batches, oldest first."""
        with self._container_lock:
            return [list(batch) for batch in self.container_batches]

    def get_container_cycle(self) -> list[dict[str, Any]]:
        """Latest sampled batch, one entry per container (used by the persistence tick)."""
        with self._container_lock:
            if not self.container_batches:
                return []
            return list(self.container_batches[-1])

    def get_docker_stats(self) -> dict[str, dict[str, Any]]:
        """Return cached docker stats, populating synchronously on first call."""
        with self._docker_stats_lock:
            if not self._docker_stats and command_exists("docker"):
                try:
                    self._docker_stats = TaskResourceMonitor._collect_docker_stats()
                    self._docker_stats_at = time.time()
                    return dict(self._docker_stats)
                except Exception:
                    pass

        self._maybe_refresh_docker_stats(time.time())
        with self._docker_stats_lock:
            return dict(self._docker_stats)

    def get_host_now(self) -> dict[str, Any]:
        """Get instant host snapshot, updating ring buffer."""
        snapshot = self.host_monitor.sample_host()
        snap_dict = asdict(snapshot)
        self.history.append(snap_dict)
        self._last_sample_time = time.time()
        return snap_dict

    def get_history(self, limit: int = 60) -> list[dict[str, Any]]:
        """Return the most recent N history snapshots."""
        items = list(self.history)
        if limit and len(items) > limit:
            items = items[-limit:]
        return items

    def get_tasks_resources(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Get live resource usage across all tasks."""
        return TaskResourceMonitor.get_task_resources(tasks)


# How long the cached docker stats stay fresh (seconds).
DOCKER_STATS_TTL_SECONDS = 20


# Global singleton instance for the process
_monitor_instance = MonitorService()


def get_monitor() -> MonitorService:
    return _monitor_instance
