"""逻辑事件分类器 + 预测状态机（纯函数，零 I/O）。

硬约束（架构 §A.1）：
- 本模块 **禁止** import `repository` / `pipeline`，也不做任何持久化；
- 别名解析、`record_logical_event`、`enqueue_delivery` 一律由 `pipeline` 调用仓库完成；
- `ForecastEvaluator.step()` 返回**新的** `state`，绝不原地修改入参，
  使「采样 → 决策 → 落库」可以在同一事务里安全重放。

判定口径全部来自 `docs/PRD-confirmation.md`（D-01 ~ D-06、D-10）与架构 §C.3。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .clock import Clock, ensure_utc
from .models import (
    AlertWindow,
    Baseline,
    FeedSnapshot,
    ForecastDecision,
    ForecastSnapshot,
    ForecastState,
    GroupSettings,
    LatestAlert,
    LogicalEvent,
    TimelineEvent,
    TimelineSnapshot,
)

#: reset 事件的 `state_version` 常量（PRD §2）
RESET_STATE_VERSION = "confirmed"

#: 触发「临时时间键」与正式 id 视为同一事实的时间容差（AC-03/04）
RESET_TS_TOLERANCE = timedelta(seconds=2)

#: credits 中允许推送的状态（`unknown`/None 静默入库，D-03）
CREDIT_PUSH_STATES: frozenset[str] = frozenset({"announced", "arriving", "available"})

#: Watch 更正原因的优先级（互斥，高者优先）
CORRECTION_PRIORITY: tuple[str, ...] = ("expired", "corrected", "window_elapsed")


# ---------------------------------------------------------------------------
# 纯工具
# ---------------------------------------------------------------------------
def _iso(value: datetime | None) -> str | None:
    """datetime → ISO8601 UTC 字符串（payload 内时间统一此格式）。"""
    if value is None:
        return None
    return ensure_utc(value).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    """从 payload 里的 ISO 字符串（或 datetime）还原 UTC 时间；非法返回 None。"""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)
    return None


def _date_start(text: str | None) -> datetime | None:
    """`YYYY-MM-DD` → 当日 00:00 UTC（缺失/非法返回 None）。"""
    if not isinstance(text, str) or len(text.strip()) != 10:
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _window_dict(window: AlertWindow | None) -> dict[str, Any] | None:
    """`AlertWindow` → JSON 友好的 dict（时间转 ISO 字符串，便于落库/渲染复用）。"""
    if window is None:
        return None
    return {
        "label": window.label,
        "start_at": _iso(window.start_at),
        "end_at": _iso(window.end_at),
        "time_zone": window.time_zone,
        "target_kind": window.target_kind,
        "target_at": _iso(window.target_at),
    }


def _dedupe_sorted(events: Sequence[LogicalEvent]) -> list[LogicalEvent]:
    """同一轮内按 `(occurred_at, logical_id)` 升序去重；同 logical_id 保留最早一条。"""
    ordered = sorted(events, key=lambda ev: (ev.occurred_at, ev.logical_id))
    seen: set[str] = set()
    result: list[LogicalEvent] = []
    for event in ordered:
        if event.logical_id in seen:
            continue
        seen.add(event.logical_id)
        result.append(event)
    return result


# ---------------------------------------------------------------------------
# id / 版本生成（架构 §C.3）
# ---------------------------------------------------------------------------
def build_logical_id(
    kind: str,
    *,
    event_id: str | None = None,
    alert_id: str | None = None,
    window_kind: str | None = None,
    anchor_at: datetime | None = None,
) -> str:
    """生成确定性 `logical_id`（同源 id 优先，其次时间锚点）。

    - `reset`：`reset:{event_id or alert_id}`，两者都无 → `reset:ts:{iso(anchor_at)}`；
    - `watch`：`watch:{alert_id or event_id}`；
    - `credits`：`credits:{event_id or alert_id}`；
    - `forecast`：`forecast:{window_kind}`（群维度前缀由调用方拼接）；
    - 其他：`{kind}:ts:{iso(anchor_at)}`。
    """
    identifier = event_id or alert_id
    if kind == "forecast":
        return f"forecast:{window_kind or 'unknown'}"
    if kind == "catchup":
        return f"catchup:{_iso(anchor_at) or 'now'}"
    if identifier:
        return f"{kind}:{identifier}"
    return f"{kind}:ts:{_iso(anchor_at) or 'unknown'}"


def build_state_version(
    kind: str,
    *,
    state: str | None = None,
    window_start_at: datetime | None = None,
    banked_state: str | None = None,
    window_kind: str | None = None,
    epoch_seq: int = 0,
    alert_seq: int = 0,
    reason: str | None = None,
) -> str | None:
    """生成 `state_version`；返回 `None` 表示「不产生版本」（不入库、不推送）。

    `reason` 非空时表示这是一条更正消息，版本由原因决定：

    - `expired` → `expired:{window_start_at iso | 'nowindow'}`
    - `corrected` → `corrected`
    - `window_elapsed` → `expired_window:{iso(window_start_at)}`
      （此时 `window_start_at` 传入窗口**结束**时间作为版本锚点）
    - `downgraded` → `downgraded:{state | 'none'}`

    非更正场景：

    - `reset` → 常量 `confirmed`
    - `watch` → `{state}:{window_start_at iso | 'nowindow'}`
    - `credits` → `banked_state`（`unknown`/缺失 → `None`）
    - `forecast` → `{window_kind}:{epoch_seq}:{alert_seq}`
    - `catchup` → `summary`
    """
    if reason == "expired":
        return f"expired:{_iso(window_start_at) or 'nowindow'}"
    if reason == "corrected":
        return "corrected"
    if reason == "window_elapsed":
        return f"expired_window:{_iso(window_start_at) or 'unknown'}"
    if reason == "downgraded":
        return f"downgraded:{state or 'none'}"

    if kind == "reset":
        return RESET_STATE_VERSION
    if kind == "watch":
        return f"{state or 'active'}:{_iso(window_start_at) or 'nowindow'}"
    if kind == "credits":
        if banked_state is None or banked_state not in CREDIT_PUSH_STATES:
            return None
        return banked_state
    if kind == "forecast":
        if window_kind is None:
            return None
        return f"{window_kind}:{int(epoch_seq)}:{int(alert_seq)}"
    if kind == "catchup":
        return "summary"
    return state


# ---------------------------------------------------------------------------
# 分类器
# ---------------------------------------------------------------------------
class EventClassifier:
    """把三端点快照归并为逻辑事件（同轮内去重，跨轮别名由 pipeline 完成）。"""

    def __init__(self, *, clock: Clock) -> None:
        """`clock` 只用于「没有更权威时间」时的兜底锚点。"""
        self._clock = clock

    # -- reset ---------------------------------------------------------
    def reset_candidates(
        self,
        *,
        forecast: ForecastSnapshot | None,
        timeline: TimelineSnapshot | None,
        feed: FeedSnapshot | None,
        baseline: Baseline,
    ) -> list[LogicalEvent]:
        """F01 候选：timeline/feed 确认行 + `latest_alert(reset/confirmed)` + `last_reset_at` 前进。"""
        fallback = forecast.updated_at if forecast is not None else self._clock.utcnow()
        candidates: list[LogicalEvent] = []

        for source, via in ((timeline, "timeline"), (feed, "feed")):
            for row in self._reset_rows(source):
                candidates.append(self._reset_event_from_row(row, fallback=fallback, via=via))

        alert = forecast.latest_alert if forecast is not None else None
        if forecast is not None and alert is not None and self._is_confirmed_reset_alert(alert):
            candidates.append(self._reset_event_from_alert(alert, snapshot=forecast))

        deduped = _dedupe_sorted(candidates)

        last_reset_at = forecast.last_reset_at if forecast is not None else None
        if (
            last_reset_at is not None
            and baseline.last_reset_at is not None
            and ensure_utc(last_reset_at) > ensure_utc(baseline.last_reset_at)
        ):
            target = ensure_utc(last_reset_at)
            matched = any(
                abs(ensure_utc(ev.occurred_at) - target) <= RESET_TS_TOLERANCE for ev in deduped
            )
            if not matched:
                deduped.append(self._reset_event_from_timestamp(target))
                deduped = _dedupe_sorted(deduped)

        return deduped

    @staticmethod
    def _reset_rows(source: TimelineSnapshot | FeedSnapshot | None) -> list[TimelineEvent]:
        """取 `group=reset` 且 `announced + confirmed + not preview` 的行（D-01/D-02）。"""
        if source is None:
            return []
        rows: list[TimelineEvent] = []
        for row in source.events:
            if row.group != "reset":
                continue
            if row.announcement_state != "announced":
                continue  # 缺失即 fail-closed，绝不当作确认
            if row.confirmed is not True:
                continue  # AC-02：announced 但 confirmed=false 不推送
            if row.preview is not False:
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _is_confirmed_reset_alert(alert: LatestAlert | None) -> bool:
        """`latest_alert` 是否构成确认重置线索。

        `corrected=True` 表示网站已更正该条 alert（F07），它不再是确认线索：
        既不能当新重置推送，也不会再产生「更正」事件（同一条 fact 不能先报后撤）。
        """
        return (
            alert is not None
            and alert.kind == "reset"
            and alert.state == "confirmed"
            and not alert.corrected
        )

    def _reset_aliases(
        self,
        *,
        event_id: str | None,
        url: str | None,
        occurred_at: datetime,
    ) -> list[str]:
        """别名集合：正式 id / url / 时间键（每个 reset 候选都带 `reset:ts:`）。"""
        aliases: list[str] = []
        if event_id:
            aliases.append(f"reset:{event_id}")
        if url:
            aliases.append(f"reset:url:{url}")
        aliases.append(f"reset:ts:{_iso(occurred_at)}")
        # 去重并保持顺序（dict 保序）
        return list(dict.fromkeys(aliases))

    def _reset_event_from_row(
        self,
        row: TimelineEvent,
        *,
        fallback: datetime,
        via: str,
    ) -> LogicalEvent:
        """timeline/feed 行 → 确认重置事件。"""
        occurred_at = row.announced_at or row.effective_at or _date_start(row.date) or fallback
        occurred_at = ensure_utc(occurred_at)
        logical_id = build_logical_id("reset", event_id=row.id, anchor_at=occurred_at)
        payload: dict[str, Any] = {
            "event_id": row.id,
            "url": row.url,
            "summary": row.summary,
            "source_label": row.source_label,
            "via": via,
            "occurred_at": _iso(occurred_at),
            "announcement_state": row.announcement_state,
            "aliases": self._reset_aliases(event_id=row.id, url=row.url, occurred_at=occurred_at),
        }
        return LogicalEvent(
            logical_id=logical_id,
            kind="reset",
            group_kind="reset",
            state_version=RESET_STATE_VERSION,
            occurred_at=occurred_at,
            payload=payload,
            source_refs=(f"event:{row.id}",),
        )

    def _reset_event_from_alert(
        self,
        alert: LatestAlert,
        *,
        snapshot: ForecastSnapshot,
    ) -> LogicalEvent:
        """`latest_alert(reset/confirmed)` → 确认重置事件（与 timeline 行同源）。"""
        occurred_at = (
            alert.confirmed_at
            or alert.observed_at
            or alert.source_at
            or snapshot.updated_at
        )
        occurred_at = ensure_utc(occurred_at)
        payload: dict[str, Any] = {
            "event_id": alert.id,
            "url": alert.url or alert.confirmation_url,
            "summary": alert.summary,
            "source_label": alert.confirmation_source,
            "via": "forecast_alert",
            "occurred_at": _iso(occurred_at),
            "announcement_state": "announced",
            "aliases": self._reset_aliases(
                event_id=alert.id,
                url=alert.url or alert.confirmation_url,
                occurred_at=occurred_at,
            ),
        }
        return LogicalEvent(
            logical_id=build_logical_id("reset", alert_id=alert.id, anchor_at=occurred_at),
            kind="reset",
            group_kind="reset",
            state_version=RESET_STATE_VERSION,
            occurred_at=occurred_at,
            payload=payload,
            source_refs=(f"alert:{alert.id}",),
        )

    def _reset_event_from_timestamp(self, occurred_at: datetime) -> LogicalEvent:
        """仅 `last_reset_at` 前进时的临时键事件（等 timeline 补齐后走别名迁移）。"""
        moment = ensure_utc(occurred_at)
        payload: dict[str, Any] = {
            "event_id": None,
            "url": None,
            "summary": "",
            "source_label": "forecast.last_reset_at",
            "via": "forecast_last_reset",
            "occurred_at": _iso(moment),
            "announcement_state": "announced",
            "aliases": self._reset_aliases(event_id=None, url=None, occurred_at=moment),
        }
        return LogicalEvent(
            logical_id=build_logical_id("reset", anchor_at=moment),
            kind="reset",
            group_kind="reset",
            state_version=RESET_STATE_VERSION,
            occurred_at=moment,
            payload=payload,
            source_refs=("forecast:last_reset_at",),
        )

    # -- watch ---------------------------------------------------------
    def watch_candidates(
        self,
        *,
        forecast: ForecastSnapshot | None,
        baseline: Baseline,
    ) -> list[LogicalEvent]:
        """F02 候选：新 `latest_alert(watch/active)`，且严格晚于本地水位。"""
        alert = forecast.latest_alert if forecast is not None else None
        if alert is None or alert.kind != "watch" or alert.state != "active":
            return []
        if not self._passes_watch_baseline(alert, baseline):
            return []

        occurred_at = ensure_utc(alert.source_at or self._clock.utcnow())
        window_start = alert.window.start_at if alert.window is not None else None
        version = build_state_version("watch", state="active", window_start_at=window_start)
        payload: dict[str, Any] = {
            "alert_id": alert.id,
            "state": alert.state,
            "summary": alert.summary,
            "url": alert.url,
            "window": _window_dict(alert.window),
            "pending": alert.pending,
            "source_at": _iso(alert.source_at),
            "score": alert.score,
            "occurred_at": _iso(occurred_at),
        }
        return [
            LogicalEvent(
                logical_id=build_logical_id("watch", alert_id=alert.id, anchor_at=occurred_at),
                kind="watch",
                group_kind="watch",
                state_version=version or "active:nowindow",
                occurred_at=occurred_at,
                payload=payload,
                source_refs=(f"alert:{alert.id}",),
            )
        ]

    def _passes_watch_baseline(self, alert: LatestAlert, baseline: Baseline) -> bool:
        """基线水位：`source_at` 晚于水位；无 `source_at` 时要求采集晚于基线构建时刻。"""
        if not baseline.built:
            return True
        if alert.source_at is not None:
            if baseline.watch_source_at is None:
                return True
            return ensure_utc(alert.source_at) > ensure_utc(baseline.watch_source_at)
        if baseline.built_at is None:
            return True
        return ensure_utc(self._clock.utcnow()) > ensure_utc(baseline.built_at)

    def watch_corrections(
        self,
        *,
        forecast: ForecastSnapshot | None,
        previous: LogicalEvent | None,
        last_reset_at: datetime | None,
        now: datetime,
    ) -> list[LogicalEvent]:
        """F07 候选：已推送 watch 的过期/更正/窗口过期（三种原因互斥）。

        D-04：列表里「消失」不产生任何事件，只有站点字段权威变化才补更正。
        `window_elapsed` 只在**确认重置没有落在窗口内**时成立（否则预告已被履行）。
        """
        if previous is None or previous.kind != "watch":
            return []
        alert = forecast.latest_alert if forecast is not None else None
        if alert is None:
            return []  # 缺失 ≠ 撤回（D-04）
        previous_alert_id = previous.payload.get("alert_id")
        if not isinstance(previous_alert_id, str) or previous_alert_id != alert.id:
            return []

        window = self._resolve_window(alert, previous.payload)
        start_at = _parse_iso(window.get("start_at"))
        end_at = _parse_iso(window.get("end_at"))
        previous_state = str(previous.payload.get("state") or "active")
        moment = ensure_utc(now)

        reason: str | None = None
        version: str | None = None
        if alert.state == "expired":
            reason = "expired"
            version = build_state_version("watch", reason="expired", window_start_at=start_at)
        elif alert.corrected is True:
            reason = "corrected"
            version = build_state_version("watch", reason="corrected")
        elif (
            end_at is not None
            and moment > end_at
            and previous_state == "active"
            and self._reset_predates_window(last_reset_at, start_at, end_at)
        ):
            reason = "window_elapsed"
            version = build_state_version("watch", reason="window_elapsed", window_start_at=end_at)

        if reason is None or version is None:
            return []

        payload: dict[str, Any] = dict(previous.payload)
        payload.update(
            {
                "alert_id": alert.id,
                "state": alert.state,
                "summary": alert.summary,
                "url": alert.url,
                "window": window or None,
                "reason": reason,
                "previous_state": previous_state,
                "occurred_at": _iso(moment),
            }
        )
        return [
            LogicalEvent(
                logical_id=previous.logical_id,
                kind="watch",
                group_kind="watch",
                state_version=version,
                occurred_at=moment,
                payload=payload,
                source_refs=previous.source_refs,
                correction_reason=reason,
            )
        ]

    @staticmethod
    def _reset_predates_window(
        last_reset_at: datetime | None,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> bool:
        """确认重置是否**发生在窗口开始之前**（决定要不要发「未获确认」更正）。

        语义修正（批次 C）：只要重置确认发生在窗口内（`last_reset_at >= window.start_at`），
        该预告实际上已被履行，不得再补一条「未获确认」的更正。窗口缺少 `start_at` 时
        退化为与 `end_at` 比较（旧口径），避免「无窗口 → 反而必发更正」的误报。
        """
        if last_reset_at is None:
            return True
        moment = ensure_utc(last_reset_at)
        if start_at is not None:
            return moment < ensure_utc(start_at)
        if end_at is not None:
            return moment < ensure_utc(end_at)
        return True

    @staticmethod
    def _resolve_window(alert: LatestAlert, previous_payload: Mapping[str, Any]) -> dict[str, Any]:
        """窗口优先级：`latest_alert.window` > 上次已入库 payload.window（D-09）。"""
        current = _window_dict(alert.window)
        if current is not None:
            return current
        raw = previous_payload.get("window")
        return dict(raw) if isinstance(raw, Mapping) else {}

    # -- credits -------------------------------------------------------
    def credit_candidates(
        self,
        *,
        timeline: TimelineSnapshot | None,
        feed: FeedSnapshot | None,
    ) -> list[LogicalEvent]:
        """F04 候选：`group=credits` 且 `banked_state` 属于可推送三态（D-03）。"""
        observations: dict[str, bool] = {}
        if feed is not None:
            for observation in feed.banked_observations:
                observations[observation.id] = bool(observation.after_announcement)
        fallback = self._clock.utcnow()

        candidates: list[LogicalEvent] = []
        for source in (timeline, feed):
            if source is None:
                continue
            for row in source.events:
                if row.group != "credits":
                    continue
                version = build_state_version("credits", banked_state=row.banked_state)
                if version is None:
                    continue  # unknown/None：静默入库可查，绝不推送
                occurred_at = row.announced_at or row.effective_at or _date_start(row.date) or fallback
                occurred_at = ensure_utc(occurred_at)
                payload: dict[str, Any] = {
                    "event_id": row.id,
                    "state": row.banked_state,
                    "summary": row.summary,
                    "url": row.url,
                    "date": row.date,
                    "after_announcement": observations.get(row.id, True),
                    "observed_variant": row.observed_variant,
                    "source_label": row.source_label,
                    "occurred_at": _iso(occurred_at),
                }
                candidates.append(
                    LogicalEvent(
                        logical_id=build_logical_id("credits", event_id=row.id, anchor_at=occurred_at),
                        kind="credits",
                        group_kind="credits",
                        state_version=version,
                        occurred_at=occurred_at,
                        payload=payload,
                        source_refs=(f"event:{row.id}",),
                    )
                )
        return _dedupe_sorted(candidates)

    # -- reset 更正 -----------------------------------------------------
    def reset_corrections(
        self,
        *,
        timeline: TimelineSnapshot | None,
        previous: LogicalEvent | None,
    ) -> list[LogicalEvent]:
        """F07：已入库 reset 被站点降级（`announcement_state` 变化或 `confirmed=false`）。"""
        if previous is None or previous.kind != "reset":
            return []
        event_id = previous.payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return []  # 临时时间键事件没有权威行可比对
        row = self._find_event(timeline, event_id)
        if row is None:
            return []  # 找不到同 id 行 ≠ 撤回（D-04）

        downgraded = row.announcement_state != "announced" or row.confirmed is not True
        if not downgraded:
            return []

        moment = ensure_utc(self._clock.utcnow())
        version = build_state_version("reset", reason="downgraded", state=row.announcement_state)
        if version is None:  # pragma: no cover - `downgraded` 分支必然有值
            return []
        payload: dict[str, Any] = dict(previous.payload)
        payload.update(
            {
                "event_id": event_id,
                "state": row.announcement_state or "none",
                "summary": row.summary or previous.payload.get("summary", ""),
                "url": row.url or previous.payload.get("url"),
                "reason": "downgraded",
                "occurred_at": _iso(moment),
            }
        )
        return [
            LogicalEvent(
                logical_id=previous.logical_id,
                kind="reset",
                group_kind="reset",
                state_version=version,
                occurred_at=moment,
                payload=payload,
                source_refs=previous.source_refs,
                correction_reason="downgraded",
            )
        ]

    @staticmethod
    def _find_event(timeline: TimelineSnapshot | None, event_id: str) -> TimelineEvent | None:
        if timeline is None:
            return None
        for row in timeline.events:
            if row.id == event_id:
                return row
        return None


# ---------------------------------------------------------------------------
# 预测状态机
# ---------------------------------------------------------------------------
class ForecastEvaluator:
    """24h/48h 概率提醒状态机（D-05 单日上限、D-06 滞回 + 0=关闭、U-1 版本号）。

    全局参数（`forecast_confirm_samples` / `forecast_cooldown_hours` /
    `forecast_daily_max_per_group`）不属于 `GroupSettings`，因此从构造参数注入；
    默认值与 `PluginSettings` 默认值一致。
    """

    def __init__(
        self,
        *,
        time_zone: ZoneInfo,
        confirm_samples: int = 2,
        cooldown_hours: int = 12,
        daily_max: int = 2,
    ) -> None:
        """`time_zone` 只用于「自然日」计数（D-05/D-07）。"""
        self._tz = time_zone
        self._confirm_samples = max(1, int(confirm_samples))
        self._cooldown_hours = max(0, int(cooldown_hours))
        self._daily_max = max(1, int(daily_max))

    def day_key(self, now: datetime) -> str:
        """配置时区下的自然日键（`YYYY-MM-DD`）。"""
        return ensure_utc(now).astimezone(self._tz).strftime("%Y-%m-%d")

    def bump_epoch(self, state: ForecastState) -> ForecastState:
        """确认重置后调用：`epoch_seq+1`、清空命中、重新武装（`alert_seq` 不动）。"""
        return replace(
            state,
            epoch_seq=int(state.epoch_seq) + 1,
            hits_24h=0,
            hits_48h=0,
            armed_24h=True,
            armed_48h=True,
        )

    def step(
        self,
        state: ForecastState,
        settings: GroupSettings,
        snapshot: ForecastSnapshot,
        now: datetime,
        *,
        fresh: bool,
    ) -> tuple[ForecastState, ForecastDecision | None]:
        """采样一次并返回 `(新 state, 决策)`；决策为 `None` 时不产生任何消息。

        `fresh=False`（数据陈旧或非新自动通知依据）时**不采样**，原样返回，
        避免用陈旧快照推进 `consecutive_hits`。
        """
        if not fresh:
            return state, None
        if state.last_sample_at is not None and snapshot.updated_at <= state.last_sample_at:
            return state, None  # 同一快照重复判定不重复计数（D-11）

        moment = ensure_utc(now)
        updated = replace(state)
        updated.last_prob_24h = int(snapshot.rounded_24h)
        updated.last_prob_48h = int(snapshot.rounded_48h)
        updated.last_sample_at = ensure_utc(snapshot.updated_at)

        today = self.day_key(moment)
        if updated.day_key != today:
            updated.day_key = today
            updated.day_count = 0  # 自然日归零，cooldown / epoch 不清（D-05）

        thresholds = {"h24": int(settings.threshold_24h), "h48": int(settings.threshold_48h)}
        probabilities = {"h24": int(snapshot.rounded_24h), "h48": int(snapshot.rounded_48h)}
        hits = {"h24": int(updated.hits_24h), "h48": int(updated.hits_48h)}
        armed = {"h24": bool(updated.armed_24h), "h48": bool(updated.armed_48h)}

        for window in ("h24", "h48"):
            threshold = thresholds[window]
            if threshold <= 0:
                # D-06：0 = 关闭该窗口的自动预测提醒（不是「永远达标」）
                hits[window] = 0
                armed[window] = True
                continue
            probability = probabilities[window]
            hits[window] = hits[window] + 1 if probability >= threshold else 0
            if probability <= max(0, threshold - 10):
                armed[window] = True  # 滞回：回落到 threshold-10 以下才重新武装

        updated.hits_24h, updated.hits_48h = hits["h24"], hits["h48"]
        updated.armed_24h, updated.armed_48h = armed["h24"], armed["h48"]

        eligible = [
            window
            for window in ("h24", "h48")
            if thresholds[window] > 0 and hits[window] >= self._confirm_samples and armed[window]
        ]
        if not eligible:
            return updated, None
        if updated.cooldown_until is not None and ensure_utc(updated.cooldown_until) > moment:
            return updated, None
        if int(updated.day_count) >= self._daily_max:
            return updated, None

        window_kind = "both" if len(eligible) == 2 else eligible[0]
        updated.alert_seq = int(updated.alert_seq) + 1
        updated.day_count = int(updated.day_count) + 1
        updated.cooldown_until = moment + timedelta(hours=self._cooldown_hours)
        for window in eligible:
            hits[window] = 0
            armed[window] = False
        updated.hits_24h, updated.hits_48h = hits["h24"], hits["h48"]
        updated.armed_24h, updated.armed_48h = armed["h24"], armed["h48"]

        version = build_state_version(
            "forecast",
            window_kind=window_kind,
            epoch_seq=updated.epoch_seq,
            alert_seq=updated.alert_seq,
        )
        decision = ForecastDecision(
            logical_id=f"forecast:{updated.subscription_id}:{window_kind}",
            state_version=version or f"{window_kind}:{updated.epoch_seq}:{updated.alert_seq}",
            window_kind=window_kind,
            rounded_24h=int(snapshot.rounded_24h),
            rounded_48h=int(snapshot.rounded_48h),
            confidence=snapshot.confidence,
            updated_at=ensure_utc(snapshot.updated_at),
        )
        return updated, decision


__all__ = [
    "CORRECTION_PRIORITY",
    "CREDIT_PUSH_STATES",
    "RESET_STATE_VERSION",
    "RESET_TS_TOLERANCE",
    "EventClassifier",
    "ForecastEvaluator",
    "build_logical_id",
    "build_state_version",
]
