"""处理链集成测试：AC-03 / 04 / 13 / 14 / 17 / 18 / 21 + D-11 / 陈旧抑制 / 补发。

所有外部 I/O 都是替身（`FakeClient` / `RecordingSend` / `FakeClock`），
断言的是**数据库状态**与**投递队列内容**，不触碰网络与真实 QQ。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from conftest import (
    SHANGHAI,
    FakeClient,
    FakeClock,
    RecordingSend,
    create_subscription,
    credits_row,
    error_fetch,
    feed_payload,
    forecast_payload,
    make_pipeline,
    mark_baseline_built,
    ok_fetch,
    open_repo,
    reset_alert,
    reset_row,
    run,
    timeline_payload,
    utc,
    watch_alert,
)

from services.classifier import EventClassifier, ForecastEvaluator
from services.commands import CommandService
from services.errors import ErrorCode
from services.models import (
    Baseline,
    CommandRequest,
    GroupOverride,
    LogicalEvent,
    PluginSettings,
)
from services.notifications import DeliveryWorker
from services.pipeline import Pipeline
from services.scheduler import SourceScheduler

UMO = "aiocqhttp:GroupMessage:1001"
UMO_B = "aiocqhttp:GroupMessage:2002"


async def _fresh_repo(
    tmp_path: Path,
    *,
    subscribed: bool = True,
    extra: tuple[tuple[str, str, GroupOverride | None], ...] = (),
):
    """建库 + 订阅 + 已建立基线，时钟推进到 12:00 UTC。

    `extra` 里的订阅同样在 05:00 建立，保证事件时间（≈06:33/11:10）晚于
    `subscribed_at` —— 否则会被「首次订阅不追送历史」正确拦掉。
    """
    clock = FakeClock(utc(2026, 10, 20, 5, 0))
    repo = await open_repo(tmp_path, clock=clock)
    if subscribed:
        await create_subscription(repo, umo=UMO, group_id="1001")
        # 基线水位 06:00Z：让 `last_reset_at=06:33:48Z` 构成「前进」（AC-03）
        await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6, 0))
    for umo, group_id, override in extra:
        await create_subscription(repo, umo=umo, group_id=group_id, override=override)
    clock.set_now(utc(2026, 10, 20, 12, 0))
    return clock, repo


RESET_ID = "2112432076767289678"
RESET_AT = "2026-10-20T06:33:48Z"
WATCH_ID = "9000000000000000001"


# ---------------------------------------------------------------------------
# AC-03：forecast 先到（仅 last_reset_at 前进），timeline 补齐后不重复
# ---------------------------------------------------------------------------
def test_forecast_then_timeline_does_not_duplicate(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            first = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(updated_at="2026-10-20T11:59:00Z", last_reset_at=RESET_AT),
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="batch-1",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert first.judged is True
            assert first.baseline_only is False
            assert first.deliveries_created == 1
            assert [event.kind for event in first.new_events] == ["reset"]
            # 临时时间键：等 timeline 补齐后靠别名迁移
            assert first.new_events[0].logical_id.startswith("reset:ts:")

            second = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="batch-2",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert second.deliveries_created == 0
            assert second.new_events == ()
            assert await repo.count_deliveries() == 1

            # 别名已把正式 id 指向同一 canonical，后续同事实永远只有 1 条
            assert await repo.find_logical_by_alias(f"reset:{RESET_ID}") == (
                first.new_events[0].logical_id
            )
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-04：三端点同事实 → 同群仅 1 条
# ---------------------------------------------------------------------------
def test_three_endpoints_same_fact_yields_single_delivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            alert = reset_alert(RESET_ID, confirmed_at=RESET_AT, source_at=RESET_AT)
            forecast_outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=alert,
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="b-f",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert forecast_outcome.deliveries_created == 1

            timeline_outcome = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="b-t",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert timeline_outcome.deliveries_created == 0

            feed_outcome = await bundle.pipeline.ingest(
                "feed",
                ok_fetch(
                    "feed",
                    feed_payload(
                        fetched_at="2026-10-20T11:59:40Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 40),
                    batch="b-d",
                    checked_at=utc(2026, 10, 20, 11, 59, 40),
                ),
            )
            assert feed_outcome.deliveries_created == 0

            deliveries = await repo.list_deliveries()
            assert len(deliveries) == 1
            assert deliveries[0].kind == "reset"
            assert await repo.count_deliveries() == 1
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-13 / AC-14：类别开关按群生效；空订阅零投递
# ---------------------------------------------------------------------------
def test_two_subscriptions_only_receive_their_categories(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(
            tmp_path, extra=((UMO_B, "2002", GroupOverride(enable_credits=False)),)
        )
        try:
            bundle = make_pipeline(repo, clock)
            outcome = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[
                            reset_row(RESET_ID, RESET_AT),
                            credits_row("2095651088502591861", "2026-10-20T11:10:00Z"),
                        ],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="b-13",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert outcome.deliveries_created == 3  # A: reset + credits；B: 仅 reset

            by_umo: dict[str, set[str]] = {}
            for task in await repo.list_deliveries():
                by_umo.setdefault(task.umo, set()).add(task.kind)
            assert by_umo[UMO] == {"reset", "credits"}
            assert by_umo[UMO_B] == {"reset"}
        finally:
            await repo.close()

    run(scenario())


def test_empty_subscriptions_yield_zero_deliveries(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path, subscribed=False)
        try:
            await mark_baseline_built(repo)
            bundle = make_pipeline(repo, clock)
            outcome = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="b-14",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert outcome.judged is True
            assert outcome.deliveries_created == 0
            assert await repo.count_deliveries() == 0
            assert len(await repo.list_events(group_kind="reset", limit=10)) == 1
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-06：首轮基线只入库不推送，且要等 forecast + timeline 都成功才算 built
# ---------------------------------------------------------------------------
def test_first_round_baseline_records_without_pushing(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path, subscribed=True)
        try:
            await repo.save_baseline(Baseline())  # 显式清空基线（模拟首次安装）
            bundle = make_pipeline(repo, clock)
            first = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT),
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="b-base-1",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert first.baseline_only is True
            assert first.deliveries_created == 0
            assert await repo.count_deliveries() == 0
            assert len(await repo.list_events(group_kind="reset", limit=10)) == 1
            # 只有 forecast 成功 → 基线尚未建立（AC-06 不提前解除抑制）
            assert (await repo.baseline()).built is False

            second = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="b-base-2",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert second.baseline_only is True
            assert second.deliveries_created == 0
            baseline = await repo.baseline()
            assert baseline.built is True
            assert baseline.last_reset_at == utc(2026, 10, 20, 6, 33, 48)
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-17：同库重开 → inflight 转 uncertain，不重发历史
# ---------------------------------------------------------------------------
def test_restart_reuses_database_without_resending(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        alert = reset_alert(RESET_ID, confirmed_at=RESET_AT)
        payload = forecast_payload(
            updated_at="2026-10-20T11:59:00Z", last_reset_at=RESET_AT, alert=alert
        )
        bundle = make_pipeline(repo, clock)
        first = await bundle.pipeline.ingest(
            "forecast",
            ok_fetch(
                "forecast",
                payload,
                fetched_at=utc(2026, 10, 20, 11, 59),
                batch="b-17-1",
                checked_at=utc(2026, 10, 20, 11, 59),
            ),
        )
        assert first.deliveries_created == 1

        claimed = await repo.claim_pending_deliveries(clock.utcnow(), limit=10)
        assert len(claimed) == 1  # 模拟进程在发送前被强杀
        await repo.close()

        reopened = await open_repo(tmp_path, clock=clock)
        try:
            bundle2 = make_pipeline(reopened, clock)
            await bundle2.pipeline.bootstrap(clock.utcnow())
            assert await reopened.count_deliveries("uncertain") == 1
            assert await reopened.count_deliveries("pending") == 0
            assert len(await reopened.list_subscriptions()) == 1
            # 缓存已从 source_snapshots 恢复（不依赖网络）
            assert bundle2.pipeline.forecast() is not None
            assert bundle2.pipeline.freshness(clock.utcnow())[0] is not None

            # 重启后再次采到同一事实（新批次）：不产生新投递
            again = await bundle2.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    payload,
                    fetched_at=utc(2026, 10, 20, 12, 1),
                    batch="b-17-2",
                    checked_at=utc(2026, 10, 20, 12, 1),
                ),
            )
            assert again.judged is True
            assert again.deliveries_created == 0
            assert again.new_events == ()
            assert await reopened.count_deliveries() == 1
        finally:
            await reopened.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-18：429 / 502 / 坏 JSON → 不阻塞、无投递、按 2/4/8/15 分钟退避
# ---------------------------------------------------------------------------
def test_fetch_failures_never_deliver_and_backoff(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            before = await repo.baseline()
            bundle = make_pipeline(repo, clock)
            for code, status in ((ErrorCode.HTTP_429, 429), (ErrorCode.HTTP_5XX, 502)):
                outcome = await bundle.pipeline.ingest(
                    "forecast",
                    error_fetch(
                        "forecast",
                        code,
                        status=status,
                        retry_after=120 if code == ErrorCode.HTTP_429 else None,
                        fetched_at=clock.utcnow(),
                    ),
                )
                assert outcome.degraded is True
                assert outcome.judged is False
                assert outcome.snapshot_stored is False

            schema_failure = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch("forecast", {}, fetched_at=clock.utcnow(), batch="bad"),
            )
            assert schema_failure.error_code == ErrorCode.SCHEMA_ERROR
            assert schema_failure.degraded is True
            assert await repo.count_deliveries() == 0
            # D-12：失败轮次不推进基线
            assert await repo.baseline() == before
            row = await repo.get_snapshot("forecast")
            assert row is not None and row.error_code == ErrorCode.SCHEMA_ERROR

            # 退避：429 带 Retry-After=120 → 下一轮直接 None，且不再发请求
            clock.set_now(utc(2026, 10, 20, 12, 0))
            poller = make_pipeline(repo, clock)
            poller.client.push(
                error_fetch(
                    "forecast",
                    ErrorCode.HTTP_429,
                    status=429,
                    retry_after=120,
                    fetched_at=clock.utcnow(),
                )
            )
            outcome = await poller.pipeline.poll_round(clock.utcnow())
            assert outcome is not None and outcome.error_code == ErrorCode.HTTP_429
            assert poller.scheduler.next_due_at == clock.utcnow() + timedelta(seconds=120)
            assert await poller.pipeline.poll_round(clock.utcnow()) is None
            assert poller.client.calls == ["forecast"]  # 退避期间零额外请求
            assert await repo.count_deliveries() == 0
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# D-11：同 batch 且 checked_at 未前移 → 不重复判定
# ---------------------------------------------------------------------------
def test_same_batch_is_not_judged_twice(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            payload = forecast_payload(
                updated_at="2026-10-20T11:59:00Z",
                last_reset_at=RESET_AT,
                alert=reset_alert(RESET_ID, confirmed_at=RESET_AT),
            )
            first = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    payload,
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="same-batch",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert first.judged is True
            second = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    payload,
                    fetched_at=utc(2026, 10, 20, 12, 0),
                    batch="same-batch",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert second.judged is False
            assert second.deliveries_created == 0
            row = await repo.get_snapshot("forecast")
            assert row is not None and row.fetched_at == utc(2026, 10, 20, 11, 59)
            assert await repo.count_deliveries() == 1
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 陈旧抑制：feed.stale 与源端过旧都只入库、不投递
# ---------------------------------------------------------------------------
def test_stale_sources_are_stored_but_not_delivered(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            stale_feed = await bundle.pipeline.ingest(
                "feed",
                ok_fetch(
                    "feed",
                    feed_payload(
                        fetched_at="2026-10-20T11:59:00Z",
                        stale=True,
                        events=[credits_row("2095651088502591861", "2026-10-20T11:10:00Z")],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="b-stale",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            assert stale_feed.judged is True
            assert stale_feed.degraded is True
            assert stale_feed.deliveries_created == 0
            assert await repo.get_snapshot_raw("feed") is not None
            assert len(await repo.list_events(group_kind="credits", limit=10)) == 1

            old_forecast = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T10:00:00Z",  # 2 小时前（> max_source_age_minutes）
                        last_reset_at=RESET_AT,
                        alert=reset_alert("9000000000000000123", confirmed_at=RESET_AT),
                    ),
                    fetched_at=utc(2026, 10, 20, 10, 0),
                    batch="b-old",
                    checked_at=utc(2026, 10, 20, 10, 0),
                ),
            )
            assert old_forecast.degraded is True
            assert old_forecast.deliveries_created == 0
            assert await repo.count_deliveries() == 0
            # 数据仍然可查（入库但抑制推送）
            assert len(await repo.list_events(group_kind="reset", limit=10)) == 1
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 补发：默认关闭；开启后每群最多 1 条摘要，且永不包含 watch / forecast
# ---------------------------------------------------------------------------
def test_catchup_is_disabled_by_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            await repo.record_logical_event(
                LogicalEvent(
                    logical_id=f"reset:{RESET_ID}",
                    kind="reset",
                    group_kind="reset",
                    state_version="confirmed",
                    occurred_at=utc(2026, 10, 20, 11, 0),
                    payload={"summary": "reset"},
                ),
                baseline=False,
            )
            await repo.close()

            reopened = await open_repo(tmp_path, clock=clock)
            try:
                bundle = make_pipeline(reopened, clock)
                await bundle.pipeline.bootstrap(utc(2026, 10, 20, 12, 0))
                assert await reopened.count_deliveries() == 0
            finally:
                await reopened.close()
        finally:
            pass

    run(scenario())


def test_catchup_enabled_sends_one_summary_per_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path, extra=((UMO_B, "2002", None),))
        settings = PluginSettings(enable_catchup=True, max_catchup_hours=6)
        try:
            await repo.record_logical_event(
                LogicalEvent(
                    logical_id=f"reset:{RESET_ID}",
                    kind="reset",
                    group_kind="reset",
                    state_version="confirmed",
                    occurred_at=utc(2026, 10, 20, 11, 0),
                    payload={"summary": "reset"},
                ),
                baseline=False,
            )
            await repo.record_logical_event(
                LogicalEvent(
                    logical_id="watch:9000000000000000001",
                    kind="watch",
                    group_kind="watch",
                    state_version="active:nowindow",
                    occurred_at=utc(2026, 10, 20, 11, 30),
                    payload={"summary": "watch"},
                ),
                baseline=False,
            )
            await repo.close()

            reopened = await open_repo(tmp_path, clock=clock)
            try:
                bundle = make_pipeline(reopened, clock, settings=settings)
                await bundle.pipeline.bootstrap(utc(2026, 10, 20, 12, 0))
                deliveries = await reopened.list_deliveries()
                assert len(deliveries) == 2  # 每群 1 条
                assert {task.kind for task in deliveries} == {"catchup"}
                names = {task.logical_id for task in deliveries}
                assert len(names) == 1  # 同一次启动共用同一个摘要 id
                # 摘要内只含 reset（watch 永不补发）
                for task in deliveries:
                    events = task.payload.get("events") or []
                    assert [item["kind"] for item in events] == ["reset"]
            finally:
                await reopened.close()
        finally:
            pass

    run(scenario())


# ---------------------------------------------------------------------------
# AC-21：命令处理期间 0 次上游请求（只读缓存）
# ---------------------------------------------------------------------------
def test_command_handle_never_triggers_fetch(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(updated_at="2026-10-20T11:59:00Z"),
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="b-cmd",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            service = CommandService(
                repo=repo,
                read=bundle.pipeline,
                settings_provider=lambda: bundle.settings,
                sink=None,
                clock=clock,
                tz_provider=lambda: SHANGHAI,
            )
            reply = await service.handle(
                CommandRequest(
                    umo=UMO,
                    is_group=True,
                    group_id="1001",
                    platform="aiocqhttp",
                    sender_id="42",
                    is_admin=True,
                    subcommand="status",
                )
            )
            assert reply.text
            assert "数据来源：https://codex-reset.com/" in reply.text
            assert bundle.client.calls == []  # 命令层绝不触发采集
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# P1-01：确认重置 / 新 Watch → 下一槽位强制 timeline（PRD §2）
# ---------------------------------------------------------------------------
def test_confirmed_reset_forces_timeline_on_next_slot(tmp_path: Path) -> None:
    """确认重置入队后，下一次 `next_endpoint()` 必须是 `timeline`（不能等到 1-3 个槽位后）。"""

    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT),
                    ),
                    fetched_at=clock.utcnow(),
                ),
            )
            assert [event.kind for event in outcome.new_events] == ["reset"]
            assert bundle.scheduler.pending_endpoint == "timeline"

            clock.advance(61)
            assert bundle.scheduler.next_endpoint(clock.utcnow()) == "timeline"
            assert bundle.scheduler.pending_endpoint is None  # 一次性
        finally:
            await repo.close()

    run(scenario())


def test_new_watch_forces_timeline_on_next_slot(tmp_path: Path) -> None:
    """新 Watch（非更正）同样抢占下一槽位（PRD §2「出现新 Watch 或确认重置后」）。"""

    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        alert=watch_alert(WATCH_ID, source_at="2026-10-20T11:30:00Z"),
                    ),
                    fetched_at=clock.utcnow(),
                ),
            )
            assert [event.kind for event in outcome.new_events] == ["watch"]
            assert bundle.scheduler.pending_endpoint == "timeline"
        finally:
            await repo.close()

    run(scenario())


def test_baseline_round_does_not_force_timeline(tmp_path: Path) -> None:
    """首轮基线（baseline_mode）只入库不投递，也不得抢占采集槽位。"""

    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path, subscribed=False)
        try:
            await repo.save_baseline(Baseline())
            bundle = make_pipeline(repo, clock)
            outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT),
                    ),
                    fetched_at=clock.utcnow(),
                ),
            )
            assert outcome.baseline_only is True
            assert bundle.scheduler.pending_endpoint is None
        finally:
            await repo.close()

    run(scenario())


def test_watch_correction_does_not_force_timeline() -> None:
    """更正事件复用 `kind="watch"`，但带 `reason` → 不算「新 Watch」，不得抢槽位。"""

    def event(kind: str, payload: dict[str, object]) -> LogicalEvent:
        return LogicalEvent(
            logical_id=f"{kind}:2100000000000000000",
            kind=kind,
            group_kind=kind,
            state_version="v",
            occurred_at=utc(2026, 10, 20, 12),
            payload=payload,
        )

    correction = event("watch", {"reason": "expired"})
    fresh_watch = event("watch", {})
    reset = event("reset", {})

    assert Pipeline._force_reason([correction]) is None  # noqa: SLF001
    assert Pipeline._force_reason([fresh_watch]) == "new_watch"  # noqa: SLF001
    assert Pipeline._force_reason([reset]) == "confirmed_reset"  # noqa: SLF001


# ---------------------------------------------------------------------------
# P2-05：timeline 单侧即可建立基线（forecast 长期 5xx 不得导致永久零通知）
# ---------------------------------------------------------------------------
def test_baseline_builds_from_timeline_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path, subscribed=False)
        try:
            await repo.save_baseline(Baseline())
            bundle = make_pipeline(repo, clock)
            await bundle.pipeline.ingest(
                "forecast", error_fetch("forecast", ErrorCode.HTTP_5XX, status=502)
            )
            assert (await repo.baseline()).built is False

            await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert (await repo.baseline()).built is True, (
                "timeline 已能独立证明 reset，forecast 失败不应让基线永久不建立"
            )
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# P2-04：F07 更正只发给「原消息已送达」的群
# ---------------------------------------------------------------------------
def test_watch_correction_only_reaches_groups_that_got_the_watch(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        try:
            bundle = make_pipeline(repo, clock)
            watch = watch_alert(
                WATCH_ID,
                source_at="2026-10-20T11:30:00Z",
                start_at="2026-10-20T12:00:00Z",
                end_at="2026-10-20T12:08:00Z",
            )
            watch_outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(updated_at="2026-10-20T11:59:00Z", alert=watch),
                    fetched_at=clock.utcnow(),
                ),
            )
            assert [event.kind for event in watch_outcome.new_events] == ["watch"]
            await bundle.worker.run_once(clock.utcnow())  # A 的预告真正送达

            # B 在预告之后才订阅（订阅时间早于「更正」发生时刻，因此不会被水位过滤掉）
            await create_subscription(repo, umo=UMO_B, group_id="2002")

            clock.set_now(utc(2026, 10, 20, 12, 8, 5))
            outcome = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(updated_at="2026-10-20T12:08:00Z", events=[]),
                    fetched_at=clock.utcnow(),
                ),
            )
            assert [event.payload.get("reason") for event in outcome.new_events] == ["window_elapsed"]

            deliveries = await repo.list_deliveries()
            correction_rows = [
                task for task in deliveries if task.payload.get("reason") is not None
            ]
            assert len(correction_rows) == 1, "更正只能发给原预告已送达的群（A）"
            assert correction_rows[0].umo == UMO
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# B：全局配置热生效（PRD §5「配置更改经验证后立即生效」）
# ---------------------------------------------------------------------------
def test_evaluate_all_uses_current_global_settings(tmp_path: Path) -> None:
    """构造时注入的 evaluator 参数不得被沿用：每次判定按**当前**全局配置重建。"""

    async def scenario() -> None:
        clock, repo = await _fresh_repo(tmp_path)
        box = {
            "settings": PluginSettings(
                forecast_confirm_samples=2,
                forecast_cooldown_hours=0,
                forecast_daily_max_per_group=5,
            )
        }
        recorder = RecordingSend()
        try:
            pipeline = Pipeline(
                repo=repo,
                client=FakeClient(),
                scheduler=SourceScheduler(spacing_seconds=60, clock=clock),
                classifier=EventClassifier(clock=clock),
                # 构造时故意给一个「永远不会达标」的值：若实现仍用它，下面一次也推不出去
                evaluator=ForecastEvaluator(time_zone=SHANGHAI, confirm_samples=99),
                worker=DeliveryWorker(
                    repo=repo,
                    send=recorder,
                    clock=clock,
                    settings_provider=lambda: box["settings"],
                    tz_provider=lambda: SHANGHAI,
                ),
                clock=clock,
                settings_provider=lambda: box["settings"],
                tz_provider=lambda: SHANGHAI,
            )

            async def sample(updated_at: str, p24: int, p48: int) -> int:
                await pipeline.ingest(
                    "forecast",
                    ok_fetch(
                        "forecast",
                        forecast_payload(updated_at=updated_at, p24=p24, p48=p48),
                        fetched_at=clock.utcnow(),
                    ),
                )
                return await pipeline.evaluate_all(clock.utcnow())

            # 1) confirm_samples=2：第 1 次采样不达标
            assert await sample("2026-10-20T11:59:00Z", 60, 80) == 0
            # 2) 第 2 次采样达标 → 入队（证明用的是 plugin 的 2，而不是构造时的 99）
            clock.advance(61)
            assert await sample("2026-10-20T12:00:30Z", 60, 80) == 1

            # 3) 热改 confirm_samples=1：回落重新武装后再达标一次就应立刻推送
            box["settings"] = replace(box["settings"], forecast_confirm_samples=1)
            clock.advance(61)
            assert await sample("2026-10-20T12:01:30Z", 30, 40) == 0  # 滞回：重新武装
            clock.advance(61)
            assert await sample("2026-10-20T12:02:30Z", 60, 80) == 1, (
                "改了 forecast_confirm_samples 之后，下一次 evaluate_all 必须立即按新值判定"
            )
        finally:
            await repo.close()

    run(scenario())
