"""QA 独立回归 ②：历史刷屏（AC-06 / AC-17 / F08 补发子项 / Q-4）。

PRD 口径：

- 首轮启动**只建基线不推送**（F01 幂等列 + AC-06 + 架构 E.5）；
- 重启后 `logical_events` 已含 `(logical_id, state_version)` → 零重发（AC-17）；
- `enable_catchup` 默认 `false`；开启后仅补发 `max_catchup_hours` 内的 **reset / credits**，
  **永不补发历史预测与历史 Watch**，且每群每次启动最多 1 条（F08 子项 + Q-4）。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
from conftest import run, utc

from services.models import Baseline, PluginSettings
from qa_helpers import (
    NOW,
    REAL_RESET_ID,
    UMO,
    UMO_B,
    built_baseline,
    confirmed_reset_row,
    credits_row,
    env,
    forecast_payload,
    make_env,
    ok_fetch,
    timeline_payload,
    watch_alert,
)

HISTORICAL = [
    confirmed_reset_row(f"21000000000000001{i:02d}", f"2026-10-0{i}T0{i}:00:00.000Z")
    for i in range(1, 7)
]
NEW_EVENT_AT = "2026-10-20T06:33:48.000Z"


def _timeline(rows, updated_at: str = "2026-10-20T11:55:00.000Z") -> dict:
    return timeline_payload(updated_at, rows)


def test_first_round_with_six_historical_resets_delivers_nothing(tmp_path):
    """AC-06：空库 + 首轮 timeline 含 6 条历史 confirmed reset → 0 投递，只入库。"""

    async def scenario():
        async with env(tmp_path) as (repo, bundle, clock):
            clock.set_now(NOW)
            # 槽位 1：forecast（无 reset 线索）
            await bundle.pipeline.ingest(
                "forecast", ok_fetch("forecast", forecast_payload(), at=NOW)
            )
            # 槽位 2：timeline 一次性带来 6 条历史确认重置
            outcome = await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline(HISTORICAL), at=NOW)
            )
            return (
                outcome,
                await repo.count_deliveries(),
                await repo.baseline(),
                await repo.list_events(group_kind="reset", limit=20),
            )

    outcome, deliveries, baseline, events = run(scenario())

    assert outcome.baseline_only is True, "首轮必须走基线模式"
    assert outcome.deliveries_created == 0
    assert deliveries == 0, "首轮历史事件绝不允许刷屏"
    assert baseline.built is True, "forecast+timeline 均成功后应建立基线"
    assert len(events) == len(HISTORICAL), "历史事件仍需入库供 /cr history 查询"


def test_second_round_with_one_new_event_delivers_exactly_one(tmp_path):
    """AC-06 语义 + AC-17：第二轮只新增 1 条 → 恰好 1 条投递，且不重复推历史。"""

    async def scenario():
        async with env(tmp_path) as (repo, bundle, clock):
            clock.set_now(NOW)
            await bundle.pipeline.ingest(
                "forecast", ok_fetch("forecast", forecast_payload(), at=NOW)
            )
            await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline(HISTORICAL), at=NOW)
            )
            # 第二轮：同一批 + 1 条新的确认重置
            rows = [*HISTORICAL, confirmed_reset_row(REAL_RESET_ID, NEW_EVENT_AT)]
            outcome = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch("timeline", _timeline(rows, "2026-10-20T11:58:00.000Z"), at=NOW),
            )
            return outcome, await repo.list_deliveries()

    outcome, deliveries = run(scenario())

    assert outcome.baseline_only is False
    assert outcome.deliveries_created == 1
    assert len(deliveries) == 1, "第二轮的 6 条历史事件必须因已入库而不重发"
    assert deliveries[0].logical_id == f"reset:{REAL_RESET_ID}"
    assert deliveries[0].umo == UMO


def test_same_history_from_feed_and_forecast_never_duplicates(tmp_path):
    """AC-04 反向守门：历史事实再从 feed / forecast 出现一次，投递数仍然只有 1。"""

    async def scenario():
        async with env(tmp_path) as (repo, bundle, clock):
            clock.set_now(NOW)
            await bundle.pipeline.ingest(
                "forecast", ok_fetch("forecast", forecast_payload(), at=NOW)
            )
            await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline(HISTORICAL), at=NOW)
            )
            rows = [*HISTORICAL, confirmed_reset_row(REAL_RESET_ID, NEW_EVENT_AT)]
            await bundle.pipeline.ingest(
                "timeline",
                ok_fetch("timeline", _timeline(rows, "2026-10-20T11:58:00.000Z"), at=NOW),
            )
            # feed 再报一遍同一批（同 id）
            await bundle.pipeline.ingest(
                "feed",
                ok_fetch(
                    "feed",
                    {
                        "fetched_at": "2026-10-20T11:59:00.000Z",
                        "stale": False,
                        "tweets": [],
                        "banked_observations": [],
                        "events": rows,
                        "newest_post_at": NEW_EVENT_AT,
                    },
                    at=NOW,
                ),
            )
            # forecast 的 last_reset_at 再前进到同一时刻
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:30.000Z", last_reset_at=NEW_EVENT_AT
                    ),
                    at=NOW,
                ),
            )
            return await repo.count_deliveries()

    assert run(scenario()) == 1


def test_restart_with_catchup_disabled_replays_nothing(tmp_path):
    """AC-17：同一 DB 重建后 `bootstrap` 不产生任何新投递（`enable_catchup` 默认 false）。"""

    async def scenario():
        async with env(tmp_path) as (repo, bundle, clock):
            clock.set_now(NOW)
            await bundle.pipeline.ingest(
                "forecast", ok_fetch("forecast", forecast_payload(), at=NOW)
            )
            await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline(HISTORICAL), at=NOW)
            )

        async with env(tmp_path) as (repo2, bundle2, clock2):
            clock2.set_now(utc(2026, 10, 20, 12, 5))
            await bundle2.pipeline.bootstrap(clock2.utcnow())
            return (
                await repo2.count_deliveries(),
                await repo2.list_deliveries(),
                len(await repo2.list_events(group_kind="reset", limit=20)),
                bundle2.settings.enable_catchup,
            )

    deliveries, rows, event_count, catchup_enabled = run(scenario())

    assert catchup_enabled is False, "PRD §2 冻结：enable_catchup 默认 false"
    assert deliveries == 0
    assert rows == []
    assert event_count == len(HISTORICAL), "重启必须保留历史事件（AC-17）"


def test_subscription_created_after_event_never_backfills(tmp_path):
    """「首次订阅不追送历史」：事件早于 `subscribed_at` → 即使基线已建立也不投递。"""

    async def scenario():
        async with env(
            tmp_path,
            subscribed_at=utc(2026, 10, 20, 11, 0),
            baseline=built_baseline(),
        ) as (repo, bundle, clock):
            clock.set_now(NOW)
            # 事件发生在订阅之前
            old = confirmed_reset_row("2100000000000000900", "2026-10-20T09:00:00.000Z")
            outcome = await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline([old]), at=NOW)
            )
            return outcome, await repo.count_deliveries()

    outcome, deliveries = run(scenario())

    assert outcome.new_events != (), "事件本身应入库"
    assert outcome.deliveries_created == 0
    assert deliveries == 0


def test_catchup_merges_into_single_summary_and_never_replays_watch(tmp_path):
    """F08/Q-4：开启补发后每群最多 1 条摘要，只含 reset/credits，绝不补发 Watch/预测。"""
    settings = PluginSettings(enable_catchup=True, max_catchup_hours=6)

    async def scenario():
        async with env(tmp_path, umos=(UMO, UMO_B)) as (repo, bundle, clock):
            clock.set_now(utc(2026, 10, 20, 11, 0))
            # 首轮基线：forecast 带一个 watch（历史 Watch，不得补发）
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T10:50:00.000Z",
                        alert=watch_alert(source_at="2026-10-20T10:00:00.000Z"),
                    ),
                    at=clock.utcnow(),
                ),
            )
            rows = [
                confirmed_reset_row("2100000000000000801", "2026-10-20T09:00:00.000Z"),
                confirmed_reset_row("2100000000000000802", "2026-10-20T10:00:00.000Z"),
                confirmed_reset_row("2100000000000000803", "2026-10-20T10:30:00.000Z"),
                credits_row(
                    "2100000000000000804",
                    announced_at="2026-10-20T10:40:00.000Z",
                    banked_state="available",
                ),
            ]
            await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline", _timeline(rows, "2026-10-20T10:55:00.000Z"), at=clock.utcnow()
                ),
            )
            assert await repo.count_deliveries() == 0, "首轮基线模式不得投递"

        async with env(tmp_path, umos=(UMO, UMO_B), settings=settings) as (repo2, bundle2, clock2):
            clock2.set_now(utc(2026, 10, 20, 11, 5))
            await bundle2.pipeline.bootstrap(clock2.utcnow())
            return await repo2.list_deliveries()

    deliveries = run(scenario())

    assert len(deliveries) == 2, "每个群最多 1 条补发摘要"
    assert {row.umo for row in deliveries} == {UMO, UMO_B}
    for row in deliveries:
        assert row.kind == "catchup"
        items = row.payload.get("events") or []
        kinds = {item.get("kind") for item in items}
        assert kinds == {"reset", "credits"}, "补发摘要不得包含 watch / forecast"
        assert len(items) == 4


def test_catchup_respects_max_catchup_hours(tmp_path):
    """Q-4：超出 `max_catchup_hours` 的事件一律不补（默认 6h）。"""
    settings = PluginSettings(enable_catchup=True, max_catchup_hours=6)

    async def scenario():
        async with env(tmp_path) as (repo, bundle, clock):
            clock.set_now(utc(2026, 10, 20, 6, 0))
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(updated_at="2026-10-20T05:50:00.000Z"),
                    at=clock.utcnow(),
                ),
            )
            # 两条事件：01:00（5h 前，应补）与 10-19 20:00（10h 前，不应补）
            rows = [
                confirmed_reset_row("2100000000000000811", "2026-10-20T01:00:00.000Z"),
                confirmed_reset_row("2100000000000000812", "2026-10-19T20:00:00.000Z"),
            ]
            await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline", _timeline(rows, "2026-10-20T05:55:00.000Z"), at=clock.utcnow()
                ),
            )

        async with env(tmp_path, settings=settings) as (repo2, bundle2, clock2):
            clock2.set_now(utc(2026, 10, 20, 6, 5))  # 距 01:00 为 5h05m，距 20:00 为 10h05m
            await bundle2.pipeline.bootstrap(clock2.utcnow())
            return await repo2.list_deliveries()

    deliveries = run(scenario())

    assert len(deliveries) == 1
    items = deliveries[0].payload.get("events") or []
    assert [item["logical_id"] for item in items] == ["reset:2100000000000000811"]


def test_baseline_object_is_persisted_and_reusable(tmp_path):
    """基线持久化：`built` / `last_reset_at` 必须能被下一轮读回（AC-03 前置条件）。"""

    async def scenario():
        repo, bundle, clock = await make_env(tmp_path)
        try:
            clock.set_now(NOW)
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z"),
                    at=NOW,
                ),
            )
            await bundle.pipeline.ingest(
                "timeline", ok_fetch("timeline", _timeline(HISTORICAL), at=NOW)
            )
            return await repo.baseline(), await repo.list_events(group_kind="reset", limit=20)
        finally:
            await repo.close()

    baseline, events = run(scenario())

    assert isinstance(baseline, Baseline)
    assert baseline.built is True
    assert baseline.last_reset_at == clock_expectation()
    assert len(events) == len(HISTORICAL)


def clock_expectation():
    """首轮基线 必须记录站点给出的 `last_reset_at`（UTC 归一化后）。"""
    return utc(2026, 10, 20, 6, 33, 48)
