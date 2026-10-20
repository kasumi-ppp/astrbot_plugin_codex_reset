"""固定处理链：采集 → 校验 → 批次去重 → 归并 → 按群评估 → 入队。

职责边界（架构 §A.3）：
- 本模块是**唯一**编排 `client / parser / classifier / repository / notifications` 的地方；
- 不做任何 AstrBot 相关适配（出站只有 `SendFn`，由 `main.py` 注入）；
- 判定逻辑全部委托给 `EventClassifier` / `ForecastEvaluator`，本模块只负责「顺序 + 落库」。

关键不变量：
1. **D-12**：失败轮次不覆盖 `raw_json`，也不推进任何基线/计数；
2. **D-11**：同 `published_batch` 且 `checked_at` 未前移 → 只更新 `last_seen_at`，不重复判定；
3. **AC-06**：`baseline.built=False` 的首轮只入库、不建投递；
4. **AC-17**：重启后 `bootstrap` 只恢复缓存，不产生新事件（`record_logical_event` 返回 False）；
5. **陈旧抑制**：`feed.stale` 或源端 `updated_at` 过旧时仍入库，但不建投递。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from .classifier import EventClassifier, ForecastEvaluator
from .client import ENDPOINTS, SourceClient
from .clock import Clock, ensure_utc
from .config import resolve_group_settings
from .errors import ErrorCode
from .models import (
    Baseline,
    FeedSnapshot,
    FetchResult,
    ForecastSnapshot,
    GroupSettings,
    IngestOutcome,
    LogicalEvent,
    ParseResult,
    PluginSettings,
    SnapshotRow,
    SubscriptionRow,
    TimelineSnapshot,
)
from .notifications import CATEGORY_FLAGS, DeliveryWorker, decision_payload
from .parser import parse_feed, parse_forecast, parse_timeline
from .repository import Repository
from .scheduler import SourceScheduler

logger = logging.getLogger(__name__)

SettingsProvider = Callable[[], PluginSettings]
TimezoneProvider = Callable[[], Any]

#: 补发允许包含的类别（F03 概率与 F02 预告**永不**补发）
CATCHUP_KINDS: tuple[str, ...] = ("reset", "credits")

#: 补发摘要单次最多纳入的事件数（防止启动时重建超大消息）
CATCHUP_EVENT_LIMIT = 200


class PipelineHooks(Protocol):
    """`main.py` 用于观测轮询结果的旁路回调（默认全部 no-op）。"""

    async def on_round(self, outcome: IngestOutcome) -> None:
        """每轮采集 + 判定结束后调用（心跳、结构化日志）。"""
        ...

    async def on_monitor_disabled(self) -> None:
        """`enable_monitor=false` 导致本轮直接返回时调用。"""
        ...


class NullPipelineHooks:
    """`PipelineHooks` 的空实现（测试与无观测诉求的场景）。"""

    async def on_round(self, outcome: IngestOutcome) -> None:
        """默认不做任何事。"""
        return None

    async def on_monitor_disabled(self) -> None:
        """默认不做任何事。"""
        return None


class Pipeline:
    """采集 + 判定 + 入队的编排器，同时充当命令层的只读视图（`ReadModel`）。"""

    def __init__(
        self,
        *,
        repo: Repository,
        client: SourceClient,
        scheduler: SourceScheduler,
        classifier: EventClassifier,
        evaluator: ForecastEvaluator,
        worker: DeliveryWorker,
        clock: Clock,
        settings_provider: SettingsProvider,
        tz_provider: TimezoneProvider,
        hooks: PipelineHooks | None = None,
    ) -> None:
        """所有依赖由外部注入（便于用假实现做零 I/O 测试）。"""
        self._repo = repo
        self._client = client
        self._scheduler = scheduler
        self._classifier = classifier
        # 仅为注入兼容保留：`evaluate_all` 一律按**当前**全局配置重建 `ForecastEvaluator`，
        # 这样热更新配置无需重启（构造时注入的实例只作基线参考）。
        self._evaluator = evaluator
        self._worker = worker
        self._clock = clock
        self._settings_provider = settings_provider
        self._tz_provider = tz_provider
        self._hooks: PipelineHooks = hooks or NullPipelineHooks()
        self._cache: dict[str, Any] = {}
        self._rows: dict[str, SnapshotRow] = {}
        self._watch_source_at: datetime | None = None

    # ------------------------------------------------------------------
    # 只读视图（ReadModel，命令层唯一数据入口）
    # ------------------------------------------------------------------
    @property
    def worker(self) -> DeliveryWorker:
        """投递 Worker（`main.py` 只用来 `stop()`，不放任意调用进来）。"""
        return self._worker

    def forecast(self) -> ForecastSnapshot | None:
        """最近一次成功的 forecast 快照（内存缓存，绝不发请求）。"""
        snapshot = self._cache.get("forecast")
        return snapshot if isinstance(snapshot, ForecastSnapshot) else None

    def timeline(self) -> TimelineSnapshot | None:
        """最近一次成功的 timeline 快照。"""
        snapshot = self._cache.get("timeline")
        return snapshot if isinstance(snapshot, TimelineSnapshot) else None

    def feed(self) -> FeedSnapshot | None:
        """最近一次成功的 feed 快照。"""
        snapshot = self._cache.get("feed")
        return snapshot if isinstance(snapshot, FeedSnapshot) else None

    def freshness(self, now: datetime) -> tuple[datetime | None, bool]:
        """返回 `(最近一次成功采集时间, 是否陈旧)`。

        陈旧判据：`feed.stale` 为真、最近一次采集超过 `max_source_age_minutes`、
        或根本没有任何快照。
        """
        moments = [
            ensure_utc(snapshot.fetched_at)
            for snapshot in self._cache.values()
            if getattr(snapshot, "fetched_at", None) is not None
        ]
        if not moments:
            return None, True
        latest = max(moments)
        plugin = self._settings_provider()
        max_age = timedelta(minutes=max(1, int(plugin.max_source_age_minutes)))
        stale = ensure_utc(now) - latest > max_age
        feed = self.feed()
        if feed is not None and bool(feed.stale):
            stale = True
        return latest, stale

    def snapshot_rows(self) -> dict[str, SnapshotRow]:
        """三个端点的快照状态行（诊断与 `/cr status` 降级行使用）。"""
        return dict(self._rows)

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------
    async def bootstrap(self, now: datetime) -> None:
        """恢复内存缓存 + 处理残留 inflight + 可选补发。

        **不产生任何新事件**：`logical_events` 不写、不改，`(logical_id, state_version)`
        已存在因此后续判定会自然返回 False（AC-17 不刷屏）。
        """
        moment = ensure_utc(now)
        for endpoint in ENDPOINTS:
            row = await self._repo.get_snapshot(endpoint)
            if row is None:
                continue
            self._rows[endpoint] = row
            raw = await self._repo.get_snapshot_raw(endpoint)
            if raw is None:
                logger.warning("[codex-reset][pipeline] bootstrap 缺少可用快照 endpoint=%s", endpoint)
                continue
            snapshot = self._parse(
                endpoint,
                raw,
                fetched_at=row.fetched_at or row.updated_at or moment,
                batch=row.batch,
                checked_at=row.checked_at,
            )
            if snapshot is None:
                logger.warning("[codex-reset][pipeline] bootstrap 快照解析失败 endpoint=%s", endpoint)
                continue
            self._cache[endpoint] = snapshot
        logger.info(
            "[codex-reset][pipeline] bootstrap 恢复缓存 endpoints=%s", sorted(self._cache)
        )

        requeued = await self._repo.requeue_inflight_as_uncertain(moment)
        if requeued:
            logger.warning("[codex-reset][pipeline] inflight -> uncertain count=%s", requeued)

        baseline = await self._repo.baseline()
        self._watch_source_at = baseline.watch_source_at
        await self._run_catchup(moment)

    # ------------------------------------------------------------------
    # 采集一轮
    # ------------------------------------------------------------------
    async def poll_round(self, now: datetime) -> IngestOutcome | None:
        """取一个槽位 → 采集 → 入库判定 → 记录调度结果；无槽位时返回 None。"""
        moment = ensure_utc(now)
        plugin = self._settings_provider()
        if not plugin.enable_monitor:
            await self._hooks.on_monitor_disabled()
            return None
        endpoint = self._scheduler.next_endpoint(moment)
        if endpoint is None:
            return None

        etag = await self._repo.snapshot_etag(endpoint)
        setter = getattr(self._client, "set_etag", None)
        if callable(setter):
            setter(endpoint, etag)

        fetch = await self._client.fetch(endpoint)
        outcome = await self.ingest(endpoint, fetch)
        self._scheduler.record_result(
            endpoint, outcome.error_code, moment, retry_after=fetch.retry_after
        )
        await self._hooks.on_round(outcome)
        if outcome.judged and not outcome.baseline_only:
            await self.evaluate_all(moment)
        return outcome

    async def ingest(self, endpoint: str, fetch: FetchResult) -> IngestOutcome:
        """把一次采集结果落到 DB、归并逻辑事件、按订阅入队。"""
        if endpoint not in ENDPOINTS:
            raise ValueError(f"不支持的 endpoint: {endpoint!r}（仅 {ENDPOINTS}）")
        moment = ensure_utc(self._clock.utcnow())

        # 规则 1：失败（含 schema_error）→ 只记错误位，不推进任何基线（D-12）
        if fetch.error_code not in ErrorCode.SUCCESS_CODES:
            return await self._record_failure(endpoint, fetch, fetch.error_code)

        # 规则 2：304 → 只更新 last_seen_at
        if fetch.error_code == ErrorCode.NOT_MODIFIED:
            await self._repo.upsert_snapshot(endpoint, fetch)
            await self._refresh_row(endpoint)
            logger.info("[codex-reset][pipeline] endpoint=%s not_modified", endpoint)
            return IngestOutcome(endpoint=endpoint, error_code=ErrorCode.NOT_MODIFIED)

        fetched_at = ensure_utc(fetch.fetched_at or moment)
        parsed = self._parse_result(
            endpoint,
            fetch.payload,
            fetched_at=fetched_at,
            batch=fetch.batch,
            checked_at=fetch.checked_at,
        )

        # 规则 3：解析失败按 schema_error 走失败分支（不覆盖 raw）
        if not parsed.ok or parsed.snapshot is None:
            return await self._record_failure(endpoint, fetch, ErrorCode.SCHEMA_ERROR)

        snapshot = parsed.snapshot

        # 规则 4：批次去重（D-11）
        if await self._is_duplicate_batch(endpoint, fetch):
            await self._repo.upsert_snapshot(
                endpoint, replace(fetch, ok=False, error_code=ErrorCode.NOT_MODIFIED)
            )
            await self._refresh_row(endpoint)
            logger.info(
                "[codex-reset][pipeline] endpoint=%s 同批次跳过判定 batch=%s", endpoint, fetch.batch
            )
            return IngestOutcome(endpoint=endpoint, error_code=ErrorCode.OK)

        # 规则 5：存快照 + 更新缓存
        row = await self._repo.upsert_snapshot(
            endpoint,
            fetch,
            snapshot_payload=fetch.payload,
            updated_at=self._source_updated_at(snapshot),
        )
        self._rows[endpoint] = row
        self._cache[endpoint] = snapshot

        # 规则 6-9：判定 + 别名归一 + 落库（首轮只做基线）
        baseline = await self._repo.baseline()
        baseline_mode = not baseline.built
        plugin = self._settings_provider()
        candidates = await self._candidates(endpoint, baseline=baseline, now=moment)
        normalized = await self._normalize(candidates)
        new_events: list[LogicalEvent] = []
        for event in normalized:
            first_time = await self._repo.record_logical_event(event, baseline=baseline_mode)
            if not first_time:
                continue
            new_events.append(event)
            if baseline_mode:
                continue
            if event.kind == "reset":
                # F03「确认重置后重新开始采样」
                await self._repo.reset_all_forecast_epochs(moment)

        # 规则 11：陈旧抑制（仍入库，但不建投递）
        suppressed = self._is_stale(endpoint, snapshot, moment, plugin)
        deliveries = 0
        if not baseline_mode and not suppressed and new_events:
            deliveries = await self._enqueue_for_subscriptions(new_events)

        # 规则 11b：采集轮换提速（PRD §2「出现新 Watch 或确认重置后，下一槽位强制 timeline」）
        if not baseline_mode:
            forced = self._force_reason(new_events)
            if forced is not None:
                self._scheduler.force_timeline(forced)

        # 规则 12：基线推进
        await self._advance_baseline(baseline, snapshot, moment)

        logger.info(
            "[codex-reset][pipeline] endpoint=%s judged baseline=%s new=%s deliveries=%s stale=%s",
            endpoint,
            int(baseline_mode),
            len(new_events),
            deliveries,
            int(suppressed),
        )
        return IngestOutcome(
            endpoint=endpoint,
            error_code=ErrorCode.OK,
            judged=True,
            baseline_only=baseline_mode,
            snapshot_stored=True,
            degraded=suppressed,
            new_events=tuple(new_events),
            deliveries_created=deliveries,
        )

    # ------------------------------------------------------------------
    # 按群评估（F03）
    # ------------------------------------------------------------------
    async def evaluate_all(self, now: datetime) -> int:
        """逐订阅采样概率并按需入队，返回新入队条数。"""
        moment = ensure_utc(now)
        plugin = self._settings_provider()
        if not plugin.enable_monitor:
            return 0
        forecast = self.forecast()
        if forecast is None:
            return 0

        fresh = self._is_fresh(plugin, moment)
        # B：全局参数（确认样本数 / 冷却 / 单日上限）必须在**每次**判定时按当前配置重建，
        # 否则 WebUI 改完 `forecast_confirm_samples` 要重启才生效（PRD §5「配置更改经验证后
        # 立即生效」）。`ForecastEvaluator` 是纯对象、无状态，重建成本可忽略。
        evaluator = ForecastEvaluator(
            time_zone=self._tz_provider(),
            confirm_samples=plugin.forecast_confirm_samples,
            cooldown_hours=plugin.forecast_cooldown_hours,
            daily_max=plugin.forecast_daily_max_per_group,
        )
        queued = 0
        for sub in await self._repo.list_subscriptions():
            if not sub.enabled:
                continue
            settings = resolve_group_settings(plugin, sub.override)
            if not settings.enable_forecast:
                # 明确跳过：不采样、不写状态（避免「关闭预测后冷却仍被消耗」）
                continue
            state = await self._repo.get_forecast_state(sub.subscription_id)
            next_state, decision = evaluator.step(
                state, settings, forecast, moment, fresh=fresh
            )
            if next_state is not state:
                await self._repo.save_forecast_state(next_state, now=moment)
            if decision is None:
                continue
            event = LogicalEvent(
                logical_id=decision.logical_id,
                kind="forecast",
                group_kind="forecast",
                state_version=decision.state_version,
                occurred_at=moment,
                payload=decision_payload(decision),
            )
            if not await self._repo.record_logical_event(event, baseline=False):
                continue
            if await self._repo.enqueue_delivery(sub=sub, ev=event):
                queued += 1
        if queued:
            logger.info("[codex-reset][pipeline] evaluate_all queued=%s", queued)
        return queued

    # ------------------------------------------------------------------
    # 判定辅助
    # ------------------------------------------------------------------
    async def _candidates(
        self,
        endpoint: str,
        *,
        baseline: Baseline,
        now: datetime,
    ) -> list[LogicalEvent]:
        """按端点决定调用哪些分类器方法（架构 §A.3 / 规格 6）。"""
        forecast = self.forecast()
        timeline = self.timeline()
        feed = self.feed()
        events: list[LogicalEvent] = []

        self._track_watch_source(forecast)

        if endpoint == "forecast":
            events.extend(
                self._classifier.reset_candidates(
                    forecast=forecast, timeline=None, feed=None, baseline=baseline
                )
            )
            events.extend(
                self._classifier.watch_candidates(forecast=forecast, baseline=baseline)
            )
        elif endpoint == "timeline":
            events.extend(
                self._classifier.reset_candidates(
                    forecast=forecast, timeline=timeline, feed=None, baseline=baseline
                )
            )
            events.extend(self._classifier.credit_candidates(timeline=timeline, feed=None))
            latest_reset = await self._latest_event("reset")
            events.extend(
                self._classifier.reset_corrections(timeline=timeline, previous=latest_reset)
            )
            events.extend(
                self._classifier.watch_corrections(
                    forecast=forecast,
                    previous=await self._latest_event("watch"),
                    last_reset_at=self._last_reset_watermark(forecast, latest_reset),
                    now=now,
                )
            )
        else:  # feed
            events.extend(
                self._classifier.reset_candidates(
                    forecast=forecast, timeline=None, feed=feed, baseline=baseline
                )
            )
            events.extend(self._classifier.credit_candidates(timeline=None, feed=feed))
        return events

    async def _latest_event(self, group_kind: str) -> LogicalEvent | None:
        """取某类别最近一条已入库事件（更正判定的 `previous`）。"""
        events = await self._repo.list_events(group_kind=group_kind, limit=1)
        return events[0] if events else None

    @staticmethod
    def _last_reset_watermark(
        forecast: ForecastSnapshot | None,
        latest_reset: LogicalEvent | None,
    ) -> datetime | None:
        """「最近一次已知确认重置」时间：取 forecast 水位与已入库 reset 事件的最大值。

        只信 `forecast.last_reset_at` 会漏掉「timeline 先确认、forecast 尚未刷新」的
        场景，导致窗口内确认的重置仍被误判成「预告未获确认」。
        """
        candidates: list[datetime] = []
        if forecast is not None and forecast.last_reset_at is not None:
            candidates.append(ensure_utc(forecast.last_reset_at))
        if latest_reset is not None:
            candidates.append(ensure_utc(latest_reset.occurred_at))
        return max(candidates) if candidates else None

    async def _normalize(self, events: Sequence[LogicalEvent]) -> list[LogicalEvent]:
        """跨端点/跨轮的别名归一（D-08）：先查别名，再登记全部别名。"""
        ordered = sorted(events, key=lambda ev: (ev.occurred_at, ev.logical_id))
        result: dict[str, LogicalEvent] = {}
        for event in ordered:
            aliases = self._aliases_of(event)
            canonical = event.logical_id
            for alias in aliases:
                hit = await self._repo.find_logical_by_alias(alias)
                if hit:
                    canonical = hit
                    break
            if canonical == event.logical_id and event.kind == "reset":
                # P2-03：别名是 `reset:ts:{ISO}` 精确字符串，跨轮 1s 抖动会落空；
                # 再用 ±2s 时间容差收敛到已登记的 canonical（容差不放大到分钟级）。
                by_time = await self._repo.find_logical_by_time(
                    kind="reset", occurred_at=event.occurred_at, tolerance_seconds=2
                )
                if by_time:
                    canonical = by_time
            for alias in aliases:
                await self._repo.add_alias(alias, canonical)
            normalized = event if canonical == event.logical_id else replace(
                event, logical_id=canonical
            )
            result.setdefault(canonical, normalized)
        return list(result.values())

    @staticmethod
    def _force_reason(events: Sequence[LogicalEvent]) -> str | None:
        """新事件是否要求下一槽位强制 `timeline`（PRD §2）。

        `watch` 的更正事件复用 `kind="watch"`，只靠 payload 的 `reason` 区分；
        更正不是「新 Watch」，不得抢占下一槽位。
        """
        for event in events:
            if event.kind == "reset":
                return "confirmed_reset"
            if event.kind == "watch" and not event.payload.get("reason"):
                return "new_watch"
        return None

    @staticmethod
    def _is_watch_correction(event: LogicalEvent) -> bool:
        """`kind="watch"` 且带非空 `reason` 的更正事件（更正复用原 kind，靠 reason 区分）。"""
        if event.kind != "watch":
            return False
        reason = event.payload.get("reason")
        return isinstance(reason, str) and bool(reason)

    @staticmethod
    def _aliases_of(event: LogicalEvent) -> list[str]:
        """取 payload 里的别名列表（缺失/类型非法时为空）。"""
        raw = event.payload.get("aliases")
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
            return []
        return [str(item) for item in raw if isinstance(item, str) and item.strip()]

    async def _enqueue_for_subscriptions(self, events: Sequence[LogicalEvent]) -> int:
        """新事件 × 订阅：类别开关 + 订阅时间水位 + F07 更正送达门槛 → 入队。"""
        plugin = self._settings_provider()
        created = 0
        for sub in await self._repo.list_subscriptions():
            if not sub.enabled:
                continue
            settings = resolve_group_settings(plugin, sub.override)
            for event in events:
                if self._predates_subscription(event, sub):
                    continue
                if not self._category_enabled(event.kind, settings):
                    continue
                if self._is_watch_correction(event) and not await self._repo.has_delivered(
                    sub.subscription_id, event.logical_id
                ):
                    # F07「仅发给原消息已送达的群」：更正复用原 watch 的 logical_id，
                    # 因此直接查该群对同一 logical_id 是否有 delivered 记录。
                    continue
                if await self._repo.enqueue_delivery(sub=sub, ev=event):
                    created += 1
        return created

    @staticmethod
    def _predates_subscription(event: LogicalEvent, sub: SubscriptionRow) -> bool:
        """首次订阅不追送历史：`occurred_at < subscribed_at` 一律跳过。"""
        if sub.subscribed_at is None:
            return False
        return ensure_utc(event.occurred_at) < ensure_utc(sub.subscribed_at)

    @staticmethod
    def _category_enabled(kind: str, settings: GroupSettings) -> bool:
        """类别开关：`catchup` 只在 reset 或 credits 至少开启一个时发送（与 Worker 一致）。"""
        if kind == "catchup":
            return bool(settings.enable_confirmed_reset or settings.enable_credits)
        flag = CATEGORY_FLAGS.get(kind)
        if flag is None:
            return True
        return bool(getattr(settings, flag, False))

    # ------------------------------------------------------------------
    # 快照 / 基线辅助
    # ------------------------------------------------------------------
    async def _record_failure(
        self, endpoint: str, fetch: FetchResult, error_code: str
    ) -> IngestOutcome:
        """失败分支：写错误位（不覆盖 raw_json、不推进基线，D-12）。"""
        failed = fetch if fetch.error_code == error_code else replace(
            fetch, ok=False, error_code=error_code
        )
        row = await self._repo.upsert_snapshot(endpoint, failed)
        self._rows[endpoint] = row
        logger.warning(
            "[codex-reset][pipeline] endpoint=%s failed error=%s status=%s",
            endpoint,
            error_code,
            fetch.status,
        )
        return IngestOutcome(
            endpoint=endpoint, error_code=error_code, degraded=True, snapshot_stored=False
        )

    async def _is_duplicate_batch(self, endpoint: str, fetch: FetchResult) -> bool:
        """D-11：同 `batch` 且 `checked_at` 未前移 → 视为同一批数据。"""
        if not fetch.batch:
            return False
        previous = await self._repo.get_snapshot(endpoint)
        if previous is None or previous.batch != fetch.batch:
            return False
        if fetch.checked_at is None or previous.checked_at is None:
            return True
        return ensure_utc(fetch.checked_at) <= ensure_utc(previous.checked_at)

    def _is_stale(
        self,
        endpoint: str,
        snapshot: Any,
        now: datetime,
        plugin: PluginSettings,
    ) -> bool:
        """陈旧抑制：`feed.stale` 或源端更新时间超过 `max_source_age_minutes`。"""
        if endpoint == "feed" and bool(getattr(snapshot, "stale", False)):
            return True
        source_at = self._source_updated_at(snapshot)
        if source_at is None:
            return True
        max_age = timedelta(minutes=max(1, int(plugin.max_source_age_minutes)))
        return ensure_utc(now) - ensure_utc(source_at) > max_age

    @staticmethod
    def _source_updated_at(snapshot: Any) -> datetime | None:
        """源端「数据更新时间」：forecast/timeline 用 `updated_at`，feed 用 `fetched_at`。"""
        updated = getattr(snapshot, "updated_at", None)
        if updated is not None:
            return ensure_utc(updated)
        fetched = getattr(snapshot, "fetched_at", None)
        return None if fetched is None else ensure_utc(fetched)

    def _is_fresh(self, plugin: PluginSettings, now: datetime) -> bool:
        """概率采样是否可用：feed 未 stale 且 forecast 未过旧。"""
        feed = self.feed()
        if feed is not None and bool(feed.stale):
            return False
        forecast = self.forecast()
        if forecast is None:
            return False
        max_age = timedelta(minutes=max(1, int(plugin.max_source_age_minutes)))
        return ensure_utc(now) - ensure_utc(forecast.updated_at) <= max_age

    async def _advance_baseline(
        self, baseline: Baseline, snapshot: Any, now: datetime
    ) -> None:
        """推进 `last_reset_at` / `watch_source_at` / `built`（规则 12）。"""
        last_reset_at = baseline.last_reset_at
        candidate_reset = getattr(snapshot, "last_reset_at", None)
        if candidate_reset is not None:
            moment = ensure_utc(candidate_reset)
            if last_reset_at is None or moment > ensure_utc(last_reset_at):
                last_reset_at = moment

        built = baseline.built
        if not built:
            await self._refresh_row("timeline")
            timeline_row = self._rows.get("timeline")
            # P2-05：timeline 已能独立证明 reset。若仍要求 forecast 也成功，forecast 端点
            # 长期 5xx 会让 `built` 永远为 False，后续所有确认重置都按 baseline_mode 只入库
            # 不投递 —— 表现为「永久零通知」且日志里看不出原因。
            built = (
                timeline_row is not None
                and bool(timeline_row.ok)
                and timeline_row.fetched_at is not None
            )

        watch_source_at = baseline.watch_source_at
        if self._watch_source_at is not None:
            if watch_source_at is None or ensure_utc(self._watch_source_at) > ensure_utc(
                watch_source_at
            ):
                watch_source_at = ensure_utc(self._watch_source_at)

        if (
            built == baseline.built
            and last_reset_at == baseline.last_reset_at
            and watch_source_at == baseline.watch_source_at
        ):
            return

        built_at = baseline.built_at
        if built and built_at is None:
            built_at = ensure_utc(now)
        await self._repo.save_baseline(
            Baseline(
                built=built,
                last_reset_at=last_reset_at,
                watch_source_at=watch_source_at,
                built_at=built_at,
            )
        )
        logger.info(
            "[codex-reset][pipeline] baseline updated built=%s last_reset_at=%s watch_source_at=%s",
            int(built),
            None if last_reset_at is None else last_reset_at.isoformat(),
            None if watch_source_at is None else watch_source_at.isoformat(),
        )

    def _track_watch_source(self, forecast: ForecastSnapshot | None) -> None:
        """记录已见 Watch 的 `source_at` 最大值（F02 基线水位）。"""
        alert = forecast.latest_alert if forecast is not None else None
        if alert is None or alert.kind != "watch" or alert.source_at is None:
            return
        candidate = ensure_utc(alert.source_at)
        if self._watch_source_at is None or candidate > ensure_utc(self._watch_source_at):
            self._watch_source_at = candidate

    async def _refresh_row(self, endpoint: str) -> None:
        """从 DB 重新读取快照状态行（避免内存行落后于真实写入）。"""
        row = await self._repo.get_snapshot(endpoint)
        if row is not None:
            self._rows[endpoint] = row

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_result(
        self,
        endpoint: str,
        payload: Any,
        *,
        fetched_at: datetime,
        batch: str | None,
        checked_at: datetime | None,
    ) -> ParseResult[Any]:
        """按端点调用解析器（失败不抛异常，错误码由 `error_code` 承载）。"""
        if endpoint == "forecast":
            return parse_forecast(
                payload, fetched_at=fetched_at, batch=batch, checked_at=checked_at
            )
        if endpoint == "timeline":
            return parse_timeline(
                payload, fetched_at=fetched_at, batch=batch, checked_at=checked_at
            )
        return parse_feed(payload, fetched_at=fetched_at, batch=batch, checked_at=checked_at)

    def _parse(
        self,
        endpoint: str,
        payload: Any,
        *,
        fetched_at: datetime,
        batch: str | None,
        checked_at: datetime | None,
    ) -> Any:
        """解析并返回快照；失败返回 None（启动恢复只告警，绝不阻断）。"""
        result = self._parse_result(
            endpoint, payload, fetched_at=fetched_at, batch=batch, checked_at=checked_at
        )
        for warning in result.warnings:
            logger.debug("[codex-reset][pipeline] %s 解析告警：%s", endpoint, warning)
        if not result.ok or result.snapshot is None:
            logger.warning(
                "[codex-reset][pipeline] %s 解析失败 error=%s", endpoint, result.error_code
            )
            return None
        return result.snapshot

    # ------------------------------------------------------------------
    # 补发（Q-4：每群每次启动最多 1 条摘要）
    # ------------------------------------------------------------------
    async def _run_catchup(self, now: datetime) -> int:
        """`enable_catchup=true` 时按群补发最近 reset/credits 摘要。"""
        plugin = self._settings_provider()
        if not plugin.enable_catchup:
            return 0
        hours = int(plugin.max_catchup_hours)
        if hours <= 0:
            return 0
        moment = ensure_utc(now)
        since = moment - timedelta(hours=hours)
        events = await self._repo.list_events_since(
            kinds=CATCHUP_KINDS, since=since, limit=CATCHUP_EVENT_LIMIT
        )
        if not events:
            return 0

        queued = 0
        for sub in await self._repo.list_subscriptions():
            if not sub.enabled:
                continue
            settings = resolve_group_settings(plugin, sub.override)
            eligible: list[LogicalEvent] = []
            for event in events:
                # 永不补发 watch / forecast（只在 CATCHUP_KINDS 里取）
                if event.kind not in CATCHUP_KINDS:
                    continue
                if self._predates_subscription(event, sub):
                    continue
                if not self._category_enabled(event.kind, settings):
                    continue
                if await self._repo.has_delivery(sub.subscription_id, event.logical_id):
                    continue
                eligible.append(event)
            if not eligible:
                continue
            latest = max(ensure_utc(event.occurred_at) for event in eligible)
            summary = LogicalEvent(
                logical_id=f"catchup:{latest.isoformat()}",
                kind="catchup",
                group_kind="catchup",
                state_version="summary",
                occurred_at=latest,
                payload={"events": [self._catchup_item(event) for event in eligible]},
            )
            if await self._repo.enqueue_delivery(sub=sub, ev=summary):
                queued += 1
        if queued:
            logger.info("[codex-reset][pipeline] catchup queued=%s", queued)
        return queued

    @staticmethod
    def _catchup_item(event: LogicalEvent) -> dict[str, Any]:
        """补发摘要里单条事件的序列化形式（`notifications` 反序列化同构）。"""
        return {
            "logical_id": event.logical_id,
            "kind": event.kind,
            "group_kind": event.group_kind,
            "state_version": event.state_version,
            "occurred_at": ensure_utc(event.occurred_at).isoformat(),
            "payload": dict(event.payload),
        }

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def describe(self, now: datetime) -> Mapping[str, Any]:
        """结构化状态摘要（`/cr status` 与心跳日志复用）。"""
        latest, stale = self.freshness(now)
        return {
            "latest_fetched_at": None if latest is None else latest.isoformat(),
            "stale": stale,
            "cached_endpoints": sorted(self._cache),
            "watch_source_at": (
                None if self._watch_source_at is None else self._watch_source_at.isoformat()
            ),
        }


__all__ = [
    "CATCHUP_EVENT_LIMIT",
    "CATCHUP_KINDS",
    "NullPipelineHooks",
    "Pipeline",
    "PipelineHooks",
]
