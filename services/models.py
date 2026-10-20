"""数据模型层：跨模块传递的全部数据结构。

约定（D-07）：
- 除 `DeliveryTask` / `ForecastState`（可变运行时状态）外，全部为 `frozen=True, slots=True`；
- 时间字段一律 timezone-aware UTC，序列化格式 `isoformat()`；
- 本模块零副作用，不 import 任何第三方库。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

SendFn = Callable[[str, str, bool], Awaitable[bool]]
"""唯一出站通道：`(umo, text, at_all) -> bool`。

第三参 `at_all` 仅对 F01（确认重置）可能为 True，由 `main.py` 决定是否真的换成
`Comp.At(qq="all")`（仅 aiocqhttp 且 `allow_at_all=true`）；`render_*` 系列模板
**不接收**该参数，也绝不在文本里写「@全体成员」。返回值语义：True=平台已接受该消息，
False=平台找不到目标会话（可重试）。"""


# ---------------------------------------------------------------------------
# 上游快照
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AlertWindow:
    """`latest_alert.window`（Watch 预告的时间窗口，D-09：只用于展示）。"""

    label: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    time_zone: str | None = None
    target_kind: str | None = None
    target_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LatestAlert:
    """`forecast.latest_alert`：网站当前最强信号。"""

    id: str
    kind: str  # watch | reset
    state: str  # active | expired | confirmed
    summary: str
    source_at: datetime | None = None
    url: str | None = None
    score: int | None = None
    window: AlertWindow | None = None
    pending: str | None = None  # done | on_its_way
    corrected: bool = False
    confirmation_source: str | None = None
    observed_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmation_url: str | None = None


@dataclass(frozen=True, slots=True)
class ForecastSnapshot:
    """`/api/forecast` 快照。"""

    updated_at: datetime
    rounded_24h: int
    rounded_48h: int
    confidence: str
    last_reset_at: datetime | None = None
    latest_alert: LatestAlert | None = None
    official_signal: dict[str, Any] | None = None
    fetched_at: datetime | None = None
    batch: str | None = None
    checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """`/api/timeline` 与 `/api/feed` 的事件同构表示。"""

    id: str
    date: str
    type: str
    group: str
    summary: str
    text: str | None = None
    url: str | None = None
    announced_at: datetime | None = None
    effective_at: datetime | None = None
    official_window: str | None = None
    announcement_state: str | None = None  # announced|none|None（缺失即 fail-closed，D-01）
    banked_state: str | None = None  # announced|arriving|available|unknown|None（D-03）
    banked_grant: bool = False
    confirmed: bool = False
    preview: bool = False
    source: str | None = None
    source_label: str | None = None
    confidence: str | None = None
    reset_kind: str | None = None
    observed_variant: str | None = None
    audience: tuple[str, ...] = ()
    incident_links: tuple[str, ...] = ()
    reason_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TimelineSnapshot:
    """`/api/timeline` 快照。"""

    updated_at: datetime
    events: tuple[TimelineEvent, ...]
    fetched_at: datetime | None = None
    batch: str | None = None
    checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BankedObservation:
    """`feed.banked_observations[]`：监测账户观测事实（D-10，非公告）。"""

    id: str
    kind: str
    source: str
    banked_state: str
    title: str
    text: str
    at: datetime | None = None
    after_announcement: bool = True


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    """`/api/feed` 快照。"""

    fetched_at: datetime
    stale: bool
    tweets: tuple[dict[str, Any], ...] = ()
    banked_observations: tuple[BankedObservation, ...] = ()
    events: tuple[TimelineEvent, ...] = ()
    newest_post_at: datetime | None = None
    batch: str | None = None
    checked_at: datetime | None = None


# ---------------------------------------------------------------------------
# 逻辑事件与投递
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LogicalEvent:
    """归并后的逻辑事件（去重键 = logical_id + state_version）。"""

    logical_id: str
    kind: str
    group_kind: str
    state_version: str
    occurred_at: datetime
    payload: dict[str, Any]
    source_refs: tuple[str, ...] = ()
    correction_reason: str | None = None


@dataclass(slots=True)
class DeliveryTask:
    """投递队列中的一行（可变状态机）。"""

    id: int
    subscription_id: str
    umo: str
    logical_id: str
    state_version: str
    kind: str
    payload: dict[str, Any]
    status: str = "pending"
    attempt: int = 0
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_code: str | None = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GroupOverride:
    """群级**稀疏**覆盖：`None` 表示未覆盖（继承全局默认）。

    持久化为只含非 None 字段的 JSON（WebUI `group_overrides` 卡片与
    `subscriptions.settings_json` 之间的同步单元）。
    """

    enable_confirmed_reset: bool | None = None
    enable_watch: bool | None = None
    enable_forecast: bool | None = None
    enable_credits: bool | None = None
    threshold_24h: int | None = None
    threshold_48h: int | None = None

    def as_sparse_dict(self) -> dict[str, Any]:
        """导出仅包含「已覆盖字段」的字典（用于写入 WebUI 卡片 / JSON）。"""
        result: dict[str, Any] = {}
        for name in ("enable_confirmed_reset", "enable_watch", "enable_forecast", "enable_credits"):
            value = getattr(self, name)
            if value is not None:
                result[name] = bool(value)
        for name in ("threshold_24h", "threshold_48h"):
            value = getattr(self, name)
            if value is not None:
                result[name] = int(value)
        return result

    def is_empty(self) -> bool:
        """是否完全没有群级覆盖。"""
        return not self.as_sparse_dict()


@dataclass(frozen=True, slots=True)
class GroupSettings:
    """群级**完整生效**配置（6 个字段均有具体值）。"""

    enable_confirmed_reset: bool = True
    enable_watch: bool = True
    enable_forecast: bool = True
    enable_credits: bool = True
    threshold_24h: int = 50
    threshold_48h: int = 70


@dataclass(frozen=True, slots=True)
class PluginSettings:
    """全局配置（`_conf_schema.json` → PluginSettings）。"""

    enable_monitor: bool = True
    defaults: GroupSettings = field(default_factory=GroupSettings)
    forecast_confirm_samples: int = 2
    forecast_cooldown_hours: int = 12
    forecast_daily_max_per_group: int = 2
    time_zone: str = "Asia/Shanghai"
    request_spacing_seconds: int = 60
    max_source_age_minutes: int = 10
    max_catchup_hours: int = 6
    enable_catchup: bool = False
    allow_at_all: bool = False
    show_source_link: bool = True
    admin_only_subscription: bool = True
    group_limit: int = 50
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 采集 / 调度
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FetchResult:
    """单次 HTTP 采集结果（永不抛异常，全部映射到 `error_code`）。"""

    endpoint: str
    ok: bool
    status: int
    error_code: str
    payload: dict[str, Any] | None = None
    fetched_at: datetime | None = None
    batch: str | None = None
    cache_status: str | None = None
    checked_at: datetime | None = None
    retry_after: int | None = None
    latency_ms: int = 0
    etag: str | None = None


# ---------------------------------------------------------------------------
# 命令层
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CommandRequest:
    """由 `main.py` 从 AstrBot 事件归一化而来的命令请求。"""

    umo: str
    is_group: bool
    group_id: str
    platform: str
    sender_id: str
    is_admin: bool
    subcommand: str
    args: tuple[str, ...] = ()
    raw_text: str = ""
    received_at: datetime | None = None
    request_id: str = ""
    group_name: str = ""
    platform_id: str = ""


@dataclass(frozen=True, slots=True)
class CommandReply:
    """命令响应（`silent=True` 表示不回复）。"""

    text: str
    silent: bool = False
    error: str | None = None
    audit: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 运行时状态
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """一轮采集 + 判定的结果摘要。"""

    endpoint: str
    error_code: str
    judged: bool = False
    baseline_only: bool = False
    snapshot_stored: bool = False
    degraded: bool = False
    new_events: tuple[LogicalEvent, ...] = ()
    deliveries_created: int = 0


@dataclass(frozen=True, slots=True)
class ForecastDecision:
    """预测评估器给出的推送决策（未被 state 修改）。"""

    logical_id: str
    state_version: str
    window_kind: str  # h24 | h48 | both
    rounded_24h: int
    rounded_48h: int
    confidence: str
    updated_at: datetime


@dataclass(slots=True)
class ForecastState:
    """单群预测状态机（D-05 / D-06 / U-1）。

    `epoch_seq` 只在**确认重置**时 +1；`alert_seq` 每产生一条预测消息 +1，
    二者共同构成预测事件的 `state_version`（`{window_kind}:{epoch_seq}:{alert_seq}`），
    保证「同日第二波概率升高」不会被 `UNIQUE` 吞掉。
    """

    subscription_id: str
    hits_24h: int = 0
    hits_48h: int = 0
    armed_24h: bool = True
    armed_48h: bool = True
    cooldown_until: datetime | None = None
    day_key: str = ""
    day_count: int = 0
    epoch_seq: int = 0
    alert_seq: int = 0
    last_prob_24h: int | None = None
    last_prob_48h: int | None = None
    last_sample_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Baseline:
    """首轮基线（F01 的 `last_reset_at` 水位与 F02 的 `source_at` 水位）。"""

    built: bool = False
    last_reset_at: datetime | None = None
    watch_source_at: datetime | None = None
    built_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """`source_snapshots` 一行的读模型。"""

    endpoint: str
    ok: bool
    error_code: str
    fetched_at: datetime | None = None
    updated_at: datetime | None = None
    checked_at: datetime | None = None
    batch: str | None = None
    cache_status: str | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionRow:
    """`subscriptions` 一行的读模型。

    `override` 是**稀疏**群级覆盖（`settings_json` 只存显式覆盖过的字段）；
    生效配置一律用 `config.resolve_group_settings(plugin, row.override)` 计算，
    因此调整全局默认值能自动影响所有未覆盖的群。
    """

    subscription_id: str
    umo: str
    platform: str
    group_id: str
    group_name: str
    enabled: bool
    override: GroupOverride
    subscribed_at: datetime | None = None
    updated_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    uncertain_count: int = 0
    version: int = 1


@dataclass(frozen=True, slots=True)
class ParseResult[T]:
    """解析结果：`ok=False` 时 `snapshot is None` 且带 `error_code`（fail-closed）。"""

    ok: bool
    error_code: str
    snapshot: T | None = None
    warnings: tuple[str, ...] = ()


@runtime_checkable
class ReadModel(Protocol):
    """命令层只读视图（绝不触发上游请求，AC-21）。"""

    def forecast(self) -> ForecastSnapshot | None:
        """最近一次成功的 forecast 快照。"""
        ...

    def timeline(self) -> TimelineSnapshot | None:
        """最近一次成功的 timeline 快照。"""
        ...

    def feed(self) -> FeedSnapshot | None:
        """最近一次成功的 feed 快照。"""
        ...

    def freshness(self, now: datetime) -> tuple[datetime | None, bool]:
        """返回 (最近一次成功采集时间, 是否陈旧)。"""
        ...

    def snapshot_rows(self) -> dict[str, SnapshotRow]:
        """三个端点的快照状态行（`/cr status` 的只读诊断行使用）。"""
        ...


__all__ = [
    "AlertWindow",
    "BankedObservation",
    "Baseline",
    "CommandReply",
    "CommandRequest",
    "DeliveryTask",
    "FeedSnapshot",
    "FetchResult",
    "ForecastDecision",
    "ForecastSnapshot",
    "ForecastState",
    "GroupOverride",
    "GroupSettings",
    "IngestOutcome",
    "LatestAlert",
    "LogicalEvent",
    "ParseResult",
    "PluginSettings",
    "ReadModel",
    "SendFn",
    "SnapshotRow",
    "SubscriptionRow",
    "TimelineEvent",
    "TimelineSnapshot",
]
