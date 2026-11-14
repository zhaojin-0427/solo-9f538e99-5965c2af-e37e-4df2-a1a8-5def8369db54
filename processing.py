"""闸机刷卡事件的核心处理逻辑：CSV 解析、批内冲突检测、行程配对。

本模块不依赖 Flask / SQLite，便于单独测试。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# CSV 必需列（表头大小写不敏感，允许存在额外列）
REQUIRED_COLUMNS = ("event_id", "card_no", "event_time", "station", "direction", "revision")

# 进站与其后首个出站配对的最大时间窗
PAIR_WINDOW = timedelta(hours=24)

# 进出站类型的可接受写法
DIRECTION_ALIASES = {
    "in": "in", "entry": "in", "enter": "in", "进": "in", "进站": "in",
    "out": "out", "exit": "out", "leave": "out", "出": "out", "出站": "out",
}

# 事件时间在库内的归一化格式（定长 UTC 秒级，字符串排序即时间排序）
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class BatchError(Exception):
    """整批拒绝。消息面向用户展示，发生时不落库任何事件。"""


@dataclass(frozen=True)
class EventRow:
    """一行已校验、已归一化的刷卡事件。"""

    event_id: str
    revision: int
    card_no: str
    event_time: str  # TIME_FORMAT 的 UTC 字符串
    station: str
    direction: str  # 'in' | 'out'

    def content(self) -> tuple:
        """用于“同一事件同一修订内容是否一致”的比对。"""
        return (self.card_no, self.event_time, self.station, self.direction)


def parse_event_time(raw: str, line_no: int) -> str:
    """解析 UTC 时间，支持 ISO 8601（Z / 时区偏移 / 朴素时间按 UTC 处理）。"""
    s = (raw or "").strip()
    if not s:
        raise BatchError(f"第 {line_no} 行：event_time 为空")
    candidate = s[:-1] + "+00:00" if s[-1:] in ("Z", "z") else s
    dt = None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise BatchError(f"第 {line_no} 行：无法解析的 UTC 时间 {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(TIME_FORMAT)


def parse_csv(text: str) -> tuple[list[EventRow], int]:
    """解析上传的 CSV 文本。

    返回 (去重后的有效行, 文件内完全重复的条数)。
    任何格式错误、或同一 (event_id, revision) 内容冲突，都抛 BatchError → 整批拒绝。
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise BatchError("空文件：缺少表头行")

    col_index: dict[str, int] = {}
    for i, name in enumerate(header):
        key = (name or "").strip().lower()
        if key and key not in col_index:
            col_index[key] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
    if missing:
        raise BatchError(
            "缺少必需列：" + ", ".join(missing) +
            "（需要 " + ", ".join(REQUIRED_COLUMNS) + "）"
        )

    def cell(raw: list[str], col: str) -> str:
        i = col_index[col]
        return raw[i].strip() if i < len(raw) else ""

    rows: list[EventRow] = []
    seen: dict[tuple[str, int], EventRow] = {}
    duplicates = 0
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not (c or "").strip() for c in raw):
            continue  # 跳过空行

        event_id = cell(raw, "event_id")
        card_no = cell(raw, "card_no")
        station = cell(raw, "station")
        if not event_id:
            raise BatchError(f"第 {line_no} 行：event_id 为空")
        if not card_no:
            raise BatchError(f"第 {line_no} 行：card_no 为空")
        if not station:
            raise BatchError(f"第 {line_no} 行：station 为空")

        direction_raw = cell(raw, "direction").lower()
        direction = DIRECTION_ALIASES.get(direction_raw)
        if direction is None:
            raise BatchError(
                f"第 {line_no} 行：未知进出站类型 {direction_raw!r}（支持 in/out）")

        revision_raw = cell(raw, "revision")
        try:
            revision = int(revision_raw)
            if revision < 0:
                raise ValueError
        except ValueError:
            raise BatchError(
                f"第 {line_no} 行：修订号必须是非负整数，得到 {revision_raw!r}")

        row = EventRow(
            event_id=event_id,
            revision=revision,
            card_no=card_no,
            event_time=parse_event_time(cell(raw, "event_time"), line_no),
            station=station,
            direction=direction,
        )

        key = (row.event_id, row.revision)
        if key in seen:
            if seen[key].content() != row.content():
                raise BatchError(
                    f"第 {line_no} 行：事件 {event_id} 修订 {revision} "
                    "在文件内出现内容冲突，整批拒绝")
            duplicates += 1  # 内容完全一致的重复行，幂等忽略
            continue
        seen[key] = row
        rows.append(row)

    if not rows:
        raise BatchError("文件中没有有效数据行")
    return rows, duplicates


def pair_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """把一张卡的 canonical 事件配成行程，并找出异常。

    规则（按时间顺序扫描）：
    - 进站与其后 24 小时内的首个出站配成一段行程；
    - 未等到出站就再次进站 → 前次进站记异常（duplicate_entry）；
    - 进站后超过 24 小时才出现出站 → 进站记异常（entry_expired），该出站按孤立出站处理；
    - 没有可配对进站的出站 → 孤立出站（orphan_exit）；
    - 数据末尾仍未配对的进站 → 进站无出站（entry_without_exit）。

    返回 (trips, anomalies)。trip 含 in/out 事件与时长秒数；
    anomaly 含事件本身、atype 与中文原因说明。
    """
    # event_time 为定长 UTC 字符串，字典序即时间序；
    # 同一时刻进站排在出站前（先搭后下），再以 event_id 兜底保证确定性。
    ordered = sorted(
        events,
        key=lambda e: (e["event_time"], 0 if e["direction"] == "in" else 1, e["event_id"]),
    )

    def ts(s: str) -> datetime:
        return datetime.strptime(s, TIME_FORMAT).replace(tzinfo=timezone.utc)

    trips: list[dict] = []
    anomalies: list[dict] = []
    pending: dict | None = None

    for ev in ordered:
        if ev["direction"] == "in":
            if pending is not None:
                anomalies.append({
                    **pending,
                    "atype": "duplicate_entry",
                    "reason": "再次进站：前次进站未配对到出站",
                })
            pending = ev
            continue

        # 出站
        if pending is None:
            anomalies.append({
                **ev,
                "atype": "orphan_exit",
                "reason": "孤立出站：没有可配对的进站",
            })
            continue

        gap = ts(ev["event_time"]) - ts(pending["event_time"])
        if gap <= PAIR_WINDOW:
            trips.append({
                "in": pending,
                "out": ev,
                "duration_s": int(gap.total_seconds()),
            })
        else:
            anomalies.append({
                **pending,
                "atype": "entry_expired",
                "reason": "进站后超过 24 小时才出现出站，未配对",
            })
            anomalies.append({
                **ev,
                "atype": "orphan_exit",
                "reason": "孤立出站：24 小时内没有可配对的进站",
            })
        pending = None

    if pending is not None:
        anomalies.append({
            **pending,
            "atype": "entry_without_exit",
            "reason": "进站后没有任何出站记录",
        })

    return trips, anomalies
