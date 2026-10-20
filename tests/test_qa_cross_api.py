"""QA 独立回归 ③：跨接口重复（AC-03 / AC-04 / D-08 / 架构 C.3 别名归并）。

PRD 口径：

- F01 的三条来源（timeline 行 / `latest_alert` / `forecast.last_reset_at`）必须归并为
  **同一个 `logical_id`**，同群同 `logical_id` 只发 1 条（F01 幂等列）；
- AC-03：`forecast.last_reset_at` 先前进 → 可先提示；timeline 补齐后**不重复**；
- D-08：`latest_alert.id` 就是 timeline 事件 id 的同源别名，不靠时间模糊匹配；
- AC-04：三端点同时报同一事实 → 同群最多一条确认通知。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
from conftest import run, utc

from qa_helpers import (
    NOW,
    REAL_RESET_ID,
    UMO,
    built_baseline,
    confirmed_reset_row,
    env,
    forecast_payload,
    ok_fetch,
    reset_alert,
    timeline_payload,
)

RESET_AT = "2026-10-20T06:33:48.000Z"
TS_LOGICAL_ID = "reset:ts:2026-10-20T06:33:48+00:00"

BASELINE = built_baseline(last_reset_at=utc(2026, 10, 19))


def _timeline(rows, updated_at: str = "2026-10-20T11:55:00.000Z") -> dict:
    return timeline_payload(updated_at, rows)


def _feed(rows, fetched_at: str = "2026-10-20T11:56:00.000Z") -> dict:
    return {
        "fetched_at": fetched_at,
        "stale": False,
        "tweets": [],
        "banked_observations": [],
        "events": rows,
        "newest_post_at": RESET_AT,
    }


def test_forecast_first_then_timeline_then_feed_yields_one_delivery(tmp_path):
    """AC-03 + AC-04：forecast 先给 `last_reset_at`（无 alert id）→ timeline → feed → 始终 1 条。"""

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            # 轮 1：只有 forecast 的时间线索引（无 alert id）
            first = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch("forecast", forecast_payload(last_reset_at=RESET_AT), at=NOW),
            )
            after_first = await repo.list_deliveries()
            # 轮 2：timeline 补齐同一事实（真实 id）
            second = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    _timeline([confirmed_reset_row(REAL_RESET_ID, RESET_AT)], "2026-10-20T11:57:00Z"),
                    at=NOW,
                ),
            )
            after_second = await repo.list_deliveries()
            # 轮 3：feed 再报同一条
            third = await bundle.pipeline.ingest("feed", ok_fetch("feed", _feed([
                confirmed_reset_row(REAL_RESET_ID, RESET_AT),
            ]), at=NOW))
            after_third = await repo.list_deliveries()
            # 轮 4：forecast 出现 reset/confirmed alert（同源 id）
            fourth = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00.000Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(),
                    ),
                    at=NOW,
                ),
            )
            events = await repo.list_events(group_kind="reset", limit=20)
            alias = await repo.find_logical_by_alias(f"reset:{REAL_RESET_ID}")
            return (
                (first, second, third, fourth),
                after_first,
                after_second,
                after_third,
                await repo.list_deliveries(),
                events,
                alias,
            )

    (first, second, third, fourth), d1, d2, d3, d4, events, alias = run(scenario())

    # 轮 1 必须先给出 1 条（AC-03：可先按时间线索提示）
    assert first.deliveries_created == 1
    assert len(d1) == 1
    assert d1[0].logical_id == TS_LOGICAL_ID, "无 alert id 时应使用临时时间键"

    # 后续三轮一律不得新增投递
    assert second.deliveries_created == 0
    assert third.deliveries_created == 0
    assert fourth.deliveries_created == 0
    assert len(d2) == 1
    assert len(d3) == 1
    assert len(d4) == 1

    # canonical 只有 1 个，且 timeline 的真实 id 已登记为该 canonical 的别名（D-08）
    assert len(events) == 1
    assert events[0].logical_id == TS_LOGICAL_ID
    assert alias == TS_LOGICAL_ID
    assert d4[0].umo == UMO


def test_alert_idsuffices_without_last_reset_at(tmp_path):
    """F01 条件③：只靠 `latest_alert(reset/confirmed)` 也应产出 1 条，且用同源 id。"""

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            outcome = await bundle.pipeline.ingest(
                "forecast",
                ok_fetch("forecast", forecast_payload(alert=reset_alert()), at=NOW),
            )
            return outcome, await repo.list_deliveries()

    outcome, deliveries = run(scenario())

    assert outcome.deliveries_created == 1
    assert deliveries[0].logical_id == f"reset:{REAL_RESET_ID}"


def test_same_round_two_ids_for_one_fact_keep_single_canonical(tmp_path):
    """架构 C.3「同轮归一化」：feed 一轮里两条不同 id 指向同一事实 → 只保留 canonical。"""

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            rows = [
                confirmed_reset_row("2100000000000000a01", RESET_AT),
                confirmed_reset_row("2100000000000000b02", RESET_AT),
            ]
            outcome = await bundle.pipeline.ingest(
                "feed", ok_fetch("feed", _feed(rows), at=NOW)
            )
            return (
                outcome,
                await repo.list_deliveries(),
                await repo.list_events(group_kind="reset", limit=20),
                await repo.find_logical_by_alias("reset:2100000000000000b02"),
            )

    outcome, deliveries, events, alias = run(scenario())

    assert len(events) == 1, "同一事实在一轮内只能有一个 canonical"
    assert outcome.deliveries_created == 1
    assert len(deliveries) == 1
    # 别名指向 canonical（保留的是按 (occurred_at, logical_id) 升序的第一个）
    assert alias == events[0].logical_id
    assert deliveries[0].logical_id == events[0].logical_id


def test_timeout_key_and_event_and_alert_all_resolve_to_one_canonical(tmp_path):
    """三条来源（ts 键 / timeline 行 / alert id）在同轮出现时仍只入库 1 条、投递 1 条。"""

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            # 时间键先入库（轮 1）
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch("forecast", forecast_payload(last_reset_at=RESET_AT), at=NOW),
            )
            # 轮 2：feed 行 + 同轮 forecast alert 同时出现（同一端点内 alert 也会被评估）
            await bundle.pipeline.ingest(
                "feed", ok_fetch("feed", _feed([confirmed_reset_row(REAL_RESET_ID, RESET_AT)]), at=NOW)
            )
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:30.000Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(),
                    ),
                    at=NOW,
                ),
            )
            return (
                await repo.list_deliveries(),
                await repo.list_events(group_kind="reset", limit=20),
            )

    deliveries, events = run(scenario())

    assert len(deliveries) == 1
    assert len(events) == 1
    assert events[0].logical_id == TS_LOGICAL_ID


def test_replaying_identical_snapshots_never_enqueues_twice(tmp_path):
    """AC-04 幂等守门：同一事实的快照反复采集（不同 batch）也只保留 1 条投递。"""

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            payload = _timeline([confirmed_reset_row(REAL_RESET_ID, RESET_AT)])
            for index in range(3):
                await bundle.pipeline.ingest(
                    "timeline",
                    ok_fetch("timeline", payload, at=clock.utcnow(), batch=f"batch-{index}"),
                )
                clock.advance(120)
            return await repo.count_deliveries()

    assert run(scenario()) == 1


def test_cross_round_one_second_skew_should_still_be_one_delivery(tmp_path):
    """AC-03 的意图：timeline 补齐「同一事实」后不得再发一条。

    实现用 `reset:ts:{occurred_at}` 精确字符串做别名，跨轮时间戳有 1s 抖动时
    会产生第二个 canonical → 第二条投递。此用例按 PRD 意图断言 1 条。
    """

    async def scenario():
        async with env(tmp_path, baseline=BASELINE) as (repo, bundle, clock):
            clock.set_now(NOW)
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch("forecast", forecast_payload(last_reset_at=RESET_AT), at=NOW),
            )
            await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    _timeline([confirmed_reset_row(REAL_RESET_ID, "2026-10-20T06:33:49.000Z")]),
                    at=NOW,
                ),
            )
            return await repo.count_deliveries()

    assert run(scenario()) == 1
