"""Schedule parsing: fixed intervals and 5-field cron expressions.

The scheduler needs to answer two questions: "when is the next run due?" and
"is this schedule valid?".  Both are answered here without any third-party
dependency, using naive-UTC datetimes so behaviour does not depend on the host
timezone database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),  # 0 = Sunday, matching cron tradition
)

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAY_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 365 * 24 * 3600


class ScheduleError(ValueError):
    """Raised when a schedule string cannot be interpreted."""


@dataclass(frozen=True)
class CronExpression:
    """A parsed 5-field cron expression."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    # A cron `day-of-month`/`day-of-week` pair is an OR when both are
    # restricted, so the raw text is kept to reproduce that rule.
    day_restricted: bool
    weekday_restricted: bool

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        # Python: Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        cron_weekday = (moment.weekday() + 1) % 7
        day_ok = moment.day in self.days
        weekday_ok = cron_weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        if self.day_restricted:
            return day_ok
        if self.weekday_restricted:
            return weekday_ok
        return True

    def next_after(self, moment: datetime, *, limit_days: int = 366 * 2) -> datetime | None:
        """First matching minute strictly after ``moment`` (naive UTC)."""
        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = candidate + timedelta(days=limit_days)
        while candidate <= deadline:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None


def _parse_field(
    raw: str, name: str, low: int, high: int, names: dict[str, int] | None
) -> tuple[frozenset[int], bool]:
    """Expand one cron field into the set of matching values."""
    wildcard = raw in {"*", "?"}
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"{name} 字段包含空项")
        step = 1
        if "/" in part:
            base, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise ScheduleError(f"{name} 字段的步长无效: {part!r}")
            step = int(step_raw)
            part = base or "*"
        if part in {"*", "?"}:
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_raw, _, end_raw = part.partition("-")
            start = _resolve_value(start_raw, name, low, high, names)
            end = _resolve_value(end_raw, name, low, high, names)
            if start > end:
                raise ScheduleError(f"{name} 字段区间颠倒: {part!r}")
        else:
            start = end = _resolve_value(part, name, low, high, names)
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"{name} 字段未匹配任何取值")
    if any(v < low or v > high for v in values):
        raise ScheduleError(f"{name} 字段超出范围 {low}-{high}")
    return frozenset(values), (not wildcard)


def _resolve_value(
    raw: str, name: str, low: int, high: int, names: dict[str, int] | None
) -> int:
    token = raw.strip().lower()
    if names and token in names:
        return names[token]
    if not re.fullmatch(r"\d+", token):
        raise ScheduleError(f"{name} 字段取值无效: {raw!r}")
    value = int(token)
    if value < low or value > high:
        raise ScheduleError(f"{name} 字段超出范围 {low}-{high}: {value}")
    return value


def parse_cron(expression: str) -> CronExpression:
    """Parse a 5-field cron expression.

    Supports ``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n``, comma lists and the
    three-letter month/weekday names.  ``@hourly``-style shortcuts are expanded.
    """
    text = (expression or "").strip()
    if not text:
        raise ScheduleError("cron 表达式为空")
    shortcut = {
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
        "@monthly": "0 0 1 * *",
        "@weekly": "0 0 * * 0",
        "@daily": "0 0 * * *",
        "@midnight": "0 0 * * *",
        "@hourly": "0 * * * *",
    }
    if text.startswith("@"):
        key = text.lower()
        if key not in shortcut:
            raise ScheduleError(f"不支持的 cron 快捷方式: {text!r}")
        text = shortcut[key]

    parts = text.split()
    if len(parts) != 5:
        raise ScheduleError("cron 表达式必须包含 5 个字段: 分 时 日 月 周")

    parsed = []
    for (name, low, high), raw in zip(CRON_FIELDS, parts):
        names = None
        if name == "month":
            names = _MONTH_NAMES
        elif name == "weekday":
            names = _WEEKDAY_NAMES
        parsed.append(_parse_field(raw, name, low, high, names))

    minutes, hours, days, months, weekdays = (p[0] for p in parsed)
    return CronExpression(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        day_restricted=parsed[2][1],
        weekday_restricted=parsed[4][1],
    )


def parse_interval(expression: str) -> int:
    """Parse ``30s`` / ``15m`` / ``6h`` / ``2d`` into seconds."""
    text = (expression or "").strip().lower()
    match = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)?", text)
    if not match:
        raise ScheduleError(f"无法解析间隔: {expression!r}")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }[unit]
    seconds = amount * multiplier
    if seconds < MIN_INTERVAL_SECONDS:
        raise ScheduleError(f"间隔不能小于 {MIN_INTERVAL_SECONDS} 秒")
    if seconds > MAX_INTERVAL_SECONDS:
        raise ScheduleError("间隔不能超过 365 天")
    return seconds


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.replace(microsecond=0).isoformat() + "Z"


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def validate_schedule(schedule_type: str, expression: str) -> str | None:
    """Return an error message when the schedule is unusable, else None."""
    kind = (schedule_type or "").strip().lower()
    if kind == "cron":
        try:
            parse_cron(expression)
        except ScheduleError as exc:
            return str(exc)
        return None
    if kind == "interval":
        try:
            parse_interval(expression)
        except ScheduleError as exc:
            return str(exc)
        return None
    if kind in {"manual", ""}:
        return None
    return f"不支持的调度类型: {schedule_type!r}"


def next_run_time(
    schedule_type: str,
    expression: str,
    *,
    reference: datetime | None = None,
    enabled: bool = True,
) -> datetime | None:
    """Compute the next due time, or None for manual/paused tasks."""
    if not enabled:
        return None
    kind = (schedule_type or "").strip().lower()
    if kind in {"manual", ""}:
        return None
    now = reference or utcnow()
    if kind == "interval":
        seconds = parse_interval(expression)
        return now + timedelta(seconds=seconds)
    if kind == "cron":
        return parse_cron(expression).next_after(now)
    raise ScheduleError(f"不支持的调度类型: {schedule_type!r}")


def describe_schedule(schedule_type: str, expression: str) -> str:
    """Short human-readable summary used in the UI."""
    kind = (schedule_type or "").strip().lower()
    if kind == "interval":
        try:
            seconds = parse_interval(expression)
        except ScheduleError:
            return "间隔（无效）"
        if seconds % 86400 == 0:
            return f"每 {seconds // 86400} 天"
        if seconds % 3600 == 0:
            return f"每 {seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"每 {seconds // 60} 分钟"
        return f"每 {seconds} 秒"
    if kind == "cron":
        return f"cron: {expression}"
    return "仅手动触发"


def default_schedule_expression(schedule_type: str) -> str:
    if (schedule_type or "").lower() == "cron":
        return "0 */6 * * *"
    return "1h"


def iter_cron_minutes(expression: str, start: datetime, count: int) -> Iterable[datetime]:
    """Yield the next ``count`` fire times, used for the schedule preview."""
    cron = parse_cron(expression)
    moment = start
    for _ in range(count):
        following = cron.next_after(moment)
        if following is None:
            return
        yield following
        moment = following
