"""Host and Task Resource Monitoring API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..monitor import get_monitor
from ..schedule import from_iso, iso, utcnow
from ..service import Service
from .deps import current_user, get_service

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# 各时间跨度的窗口长度与聚合粒度（秒）。图表横轴按序号等距排布，
# 若把 10 秒级内存采样与 5 分钟级落库点混在一起，横轴会失真，
# 因此所有来源统一先落到同一套时间桶里再返回。
RANGE_WINDOW_SECONDS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
RANGE_BUCKET_SECONDS = {"1h": 20, "24h": 300, "7d": 1800}

HOST_METRIC_KEYS = (
    "cpu_percent", "memory_percent",
    "disk_read_rate", "disk_write_rate",
    "net_rx_rate", "net_tx_rate",
)
CONTAINER_METRIC_KEYS = HOST_METRIC_KEYS + ("memory_bytes",)

# 落库点数低于该值时视为「刚上线」，用内存缓冲补点，否则长周期图是空白。
MIN_PERSISTED_POINTS = 30

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _epoch_of(value: Any) -> int | None:
    moment = from_iso(str(value or ""))
    if moment is None:
        return None
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _bucket_rows(
    rows: list[dict[str, Any]],
    bucket_seconds: int,
    metric_keys: tuple[str, ...],
    *,
    identity: bool,
    since_epoch: int,
) -> list[dict[str, Any]]:
    """Average rows into epoch-aligned buckets.

    ``identity`` keeps the per-container fields; host history only needs the
    timestamp.  Bucket boundaries match the SQL grouping used for the persisted
    rows so both sources line up on the same x positions.
    """
    bucket = max(1, int(bucket_seconds))
    slots: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        epoch = _epoch_of(row.get("timestamp"))
        if epoch is None or epoch < since_epoch:
            continue
        start = epoch // bucket * bucket
        # 主机序列没有容器身份，必须显式归到同一个桶里；
        # 否则行里残留的 container_id 会把它们拆成多条时间线。
        ident = str(row.get("container_id") or "") if identity else ""
        slot = slots.get((start, ident))
        if slot is None:
            slot = {"_start": start, "_count": 0, "_id": ident, "_name": "", "_project": ""}
            for key in metric_keys:
                slot[key] = 0.0
            slots[(start, ident)] = slot
        slot["_count"] += 1
        for key in metric_keys:
            slot[key] += float(row.get(key) or 0.0)
        # 同一桶内后写入的是更新的样本，名称与项目以它为准。
        if row.get("container_name"):
            slot["_name"] = str(row["container_name"])
        if row.get("compose_project"):
            slot["_project"] = str(row["compose_project"])

    result: list[dict[str, Any]] = []
    for slot in sorted(slots.values(), key=lambda s: (s["_start"], s["_id"])):
        item: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(slot["_start"], tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        if identity:
            item["container_id"] = slot["_id"]
            item["container_name"] = slot["_name"]
            item["compose_project"] = slot["_project"]
        count = max(1, int(slot["_count"]))
        for key in metric_keys:
            item[key] = round(slot[key] / count, 2)
        result.append(item)
    return result


def _distinct_points(rows: list[dict[str, Any]]) -> int:
    return len({str(r.get("timestamp") or "") for r in rows if r.get("timestamp")})


def _merge_batches(batches: list[list[dict[str, Any]]], cutoff: str) -> list[dict[str, Any]]:
    """Flatten in-memory sample batches, keeping only points inside the window."""
    merged: list[dict[str, Any]] = []
    for batch in batches:
        for sample in batch:
            if str(sample.get("timestamp") or "") >= cutoff:
                merged.append(sample)
    return merged


@router.get("/host")
def get_host_metrics(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Get real-time host resource metrics (CPU, memory, disk, network, load)."""
    monitor = get_monitor()
    return monitor.get_host_now()


@router.get("/tasks")
def get_tasks_metrics(
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    """Get real-time container and process resource usage for all deployed tasks."""
    monitor = get_monitor()
    tasks = service.store.tasks.list_all()
    return monitor.get_tasks_resources(tasks)


@router.get("/history")
def get_host_history(
    range: str = Query("1h", pattern="^(1h|24h|7d)$", description="时间范围：1h / 24h / 7d"),
    limit: int = Query(360, ge=5, le=1000, description="返回的历史点数上限"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    """Get host metric trend snapshots for charts with multi-range support."""
    monitor = get_monitor()
    window = RANGE_WINDOW_SECONDS[range]
    bucket = RANGE_BUCKET_SECONDS[range]
    cutoff_at = utcnow() - timedelta(seconds=window)
    cutoff = iso(cutoff_at)
    since_epoch = int((cutoff_at.replace(tzinfo=timezone.utc) - _EPOCH).total_seconds())

    if range == "1h":
        rows = monitor.get_history(limit=0)
    else:
        rows = service.store.metrics.query_range(cutoff, limit=5000)
        if _distinct_points(rows) < MIN_PERSISTED_POINTS:
            # 新装系统还没有长周期落库，先用内存缓冲补上最近一段。
            rows = rows + monitor.get_history(limit=0)

    bucketed = _bucket_rows(
        rows, bucket, HOST_METRIC_KEYS, identity=False, since_epoch=since_epoch,
    )
    return bucketed[-limit:] if len(bucketed) > limit else bucketed


def _pivot_containers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pivot per-container rows into column arrays aligned to a shared timeline.

    Column arrays keep 7-day payloads small (one object per point per container
    would multiply the JSON size) and let the frontend reuse index-based charts.
    """
    timestamps = sorted({str(r.get("timestamp") or "") for r in rows if r.get("timestamp")})
    position = {ts: index for index, ts in enumerate(timestamps)}
    containers: dict[str, dict[str, Any]] = {}
    for row in rows:
        timestamp = str(row.get("timestamp") or "")
        index = position.get(timestamp)
        if index is None:
            continue
        cid = str(row.get("container_id") or "")
        if not cid:
            continue
        entry = containers.get(cid)
        if entry is None:
            entry = {
                "id": cid,
                "name": str(row.get("container_name") or ""),
                "project": str(row.get("compose_project") or ""),
            }
            for key in CONTAINER_METRIC_KEYS:
                entry[key] = [None] * len(timestamps)
            containers[cid] = entry
        if row.get("container_name"):
            entry["name"] = str(row["container_name"])
        if row.get("compose_project"):
            entry["project"] = str(row["compose_project"])
        for key in CONTAINER_METRIC_KEYS:
            value = row.get(key)
            entry[key][index] = None if value is None else float(value)
    ordered = sorted(containers.values(), key=lambda c: (c["name"].casefold(), c["id"]))
    return {"timestamps": timestamps, "containers": ordered}


@router.get("/containers/history")
def get_container_history(
    range: str = Query("1h", pattern="^(1h|24h|7d)$", description="时间范围：1h / 24h / 7d"),
    service: Service = Depends(get_service),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Get per-container CPU / memory / disk I/O / network trend snapshots."""
    monitor = get_monitor()
    window = RANGE_WINDOW_SECONDS[range]
    bucket = RANGE_BUCKET_SECONDS[range]
    cutoff_at = utcnow() - timedelta(seconds=window)
    cutoff = iso(cutoff_at)
    since_epoch = int((cutoff_at.replace(tzinfo=timezone.utc) - _EPOCH).total_seconds())

    if range == "1h":
        rows = _merge_batches(monitor.get_container_batches(), cutoff)
    else:
        rows = service.store.container_metrics.query_bucketed(cutoff, bucket)
        if _distinct_points(rows) < MIN_PERSISTED_POINTS:
            # 落库点太少说明刚上线：用内存缓冲补齐最近一段（与主机图一致）。
            rows = rows + _merge_batches(monitor.get_container_batches(), cutoff)

    bucketed = _bucket_rows(
        rows, bucket, CONTAINER_METRIC_KEYS, identity=True, since_epoch=since_epoch,
    )
    return _pivot_containers(bucketed)
