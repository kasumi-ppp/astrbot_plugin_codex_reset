"""PRD §2「出现新 Watch 或确认重置后，下一槽位强制 timeline」的独立验证（P1-01 回归）。

断言来源：
- PRD §2 采集节奏：出现新 Watch 或确认重置 → **下一槽位强制 timeline**；
- AC-19：任意两次请求开始时间间隔 ≥ `spacing_seconds`（强制不得插队）；
- AC-18：退避期间不得采集（强制也不得取消上游要求的冷却）；
- architecture §D：`force_timeline()` 只插入一次性优先级，不破坏轮换指针。

全部用例零真实网络、零真实等待（FakeClock + FakeClient）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import (
    FakeClient,
    FakeClock,
    create_subscription,
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

from services.errors import ErrorCode
from services.models import LogicalEvent
from services.pipeline import Pipeline
from services.scheduler import SourceScheduler

UMO = "aiocqhttp:GroupMessage:1001"
RESET_ID = "2112432076767289678"
RESET_AT = "2026-10-20T06:33:48Z"
WATCH_ID = "9000000000000000001"


def _event(kind: str, **payload: object) -> LogicalEvent:
    """构造一条最小 LogicalEvent，用于直接验证强制规则。"""
    return LogicalEvent(
        logical_id=f"{kind}:1",
        kind=kind,
        group_kind=kind,
        state_version="v1",
        occurred_at=utc(2026, 10, 20, 6),
        payload=dict(payload),
    )


# ----------------------------------------------------------------------
# 单元级：轮换序列 / 一次性 / 间距 / 退避
# ----------------------------------------------------------------------
def test_forced_slot_wins_over_rotation_and_keeps_pointer():
    """强制槽位优先于轮换，且**不推进**轮换指针（下一轮仍按原序列走）。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=60, clock=clock, rotation=("feed", "forecast"))
    pointer_before = scheduler.pointer

    scheduler.force_timeline("confirmed_reset")
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"
    assert scheduler.pointer == pointer_before, "强制槽位不得移动轮换指针"

    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) == "feed", "强制只生效一次，之后回到轮换"
    assert scheduler.pointer == (pointer_before + 1) % 2


def test_forced_slot_does_not_violate_min_spacing():
    """AC-19：强制不得插队，仍须等满 `spacing_seconds`。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=60, clock=clock, rotation=("feed",))
    assert scheduler.next_endpoint(clock.utcnow()) == "feed"

    scheduler.force_timeline("new_watch")
    clock.advance(59)
    assert scheduler.next_endpoint(clock.utcnow()) is None, "未满 60s 不得采集（即使已被强制）"
    clock.advance(1)
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"


def test_forced_slot_waits_out_backoff():
    """AC-18：timeline 退避中时，强制排队的 timeline 必须等退避结束（不得取消冷却）。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=60, clock=clock, rotation=("feed",))
    scheduler.next_endpoint(clock.utcnow())
    due = scheduler.record_result("timeline", ErrorCode.HTTP_5XX, clock.utcnow())
    scheduler.force_timeline("confirmed_reset")

    clock.advance(61)
    assert scheduler.next_endpoint(clock.utcnow()) is None, "退避未结束不得采集"
    clock.set_now(due)
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline", "退避结束后强制槽位仍保留"


# ----------------------------------------------------------------------
# 规则级：哪些事件才触发强制
# ----------------------------------------------------------------------
def test_confirmed_reset_requests_forced_timeline():
    assert Pipeline._force_reason([_event("reset")]) == "confirmed_reset"


def test_new_watch_requests_forced_timeline():
    assert Pipeline._force_reason([_event("watch")]) == "new_watch"


def test_watch_correction_never_requests_forced_timeline():
    """更正复用 `kind="watch"`，靠 payload.reason 区分：更正不得抢占下一槽位。"""
    corrected = _event("watch", reason="window_passed")
    assert Pipeline._force_reason([corrected]) is None


@pytest.mark.parametrize("kind", ["credits", "forecast"])
def test_other_kinds_never_request_forced_timeline(kind):
    assert Pipeline._force_reason([_event(kind)]) is None


# ----------------------------------------------------------------------
# 集成级：跨轮次真的把下一槽位换成 timeline
# ----------------------------------------------------------------------
def test_confirmed_reset_makes_next_round_timeline(tmp_path):
    """轮换序列是 feed→forecast 时，第 1 轮出现确认重置 → 第 2 轮必须是 timeline。"""

    async def scenario():
        clock = FakeClock(utc(2026, 10, 20, 12))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6))
            bundle = make_pipeline(
                repo,
                clock,
                client=FakeClient(),
                rotation=("forecast", "feed"),
            )
            moment = clock.utcnow()
            bundle.client.push(
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(
                            RESET_ID, confirmed_at=RESET_AT, source_at=RESET_AT
                        ),
                        p24=42,
                        p48=50,
                    ),
                    fetched_at=moment,
                    batch="b0",
                    checked_at=moment,
                )
            )
            bundle.client.push(
                ok_fetch(
                    "timeline",
                    timeline_payload(updated_at="2026-10-20T12:00:30Z", events=[]),
                    fetched_at=moment,
                    batch="b1",
                    checked_at=moment,
                )
            )
            first = await bundle.pipeline.poll_round(moment)
            clock.advance(61)
            second = await bundle.pipeline.poll_round(clock.utcnow())
            return first, second, bundle
        finally:
            await repo.close()

    first, second, bundle = run(scenario())

    assert first is not None and first.endpoint == "forecast"
    assert first.new_events, "首轮必须识别出确认重置"
    # 轮换指针在第 1 轮后指向 feed；被强制后本轮必须是 timeline
    assert second is not None and second.endpoint == "timeline"
    assert bundle.client.calls == ["forecast", "timeline"]


def test_repeated_identical_event_does_not_force_again(tmp_path):
    """同一 watch 连续两轮出现（第二轮并非新事件）→ 不得重复强制，避免预测端点被饿死。"""

    async def scenario():
        clock = FakeClock(utc(2026, 10, 20, 12))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6))
            bundle = make_pipeline(repo, clock, client=FakeClient(), rotation=("forecast", "feed"))
            alert = watch_alert(
                WATCH_ID,
                start_at="2026-10-20T12:00:00Z",
                end_at="2026-10-20T13:00:00Z",
                source_at="2026-10-20T11:30:00Z",
            )
            # FakeClient 是 FIFO：按「forecast → 强制 timeline → forecast」顺序预置响应
            for index, reload_at in enumerate(
                ("2026-10-20T11:59:00Z", "2026-10-20T12:59:00Z")
            ):
                moment = clock.utcnow()
                bundle.client.push(
                    ok_fetch(
                        "forecast",
                        forecast_payload(updated_at=reload_at, last_reset_at=RESET_AT, alert=alert),
                        fetched_at=moment,
                        batch=f"f{index}",
                        checked_at=moment,
                    )
                )
                if index == 0:
                    bundle.client.push(
                        ok_fetch(
                            "timeline",
                            timeline_payload(updated_at="2026-10-20T12:00:30Z", events=[]),
                            fetched_at=moment,
                            batch="t0",
                            checked_at=moment,
                        )
                    )
                    # 轮换为 ("forecast", "feed")：强制 timeline 之后的一轮是 feed
                    bundle.client.push(
                        ok_fetch(
                            "feed",
                            feed_payload(fetched_at="2026-10-20T12:00:30Z", events=[]),
                            fetched_at=moment,
                            batch="d0",
                            checked_at=moment,
                        )
                    )

            rounds = []
            for _ in range(3):
                rounds.append(await bundle.pipeline.poll_round(clock.utcnow()))
                clock.advance(61)
            return rounds, bundle
        finally:
            await repo.close()

    rounds, bundle = run(scenario())

    assert [item.endpoint for item in rounds] == ["forecast", "timeline", "feed"], (
        "同一 watch 的第二次出现不得再次强制 timeline；强制后应回到正常轮换"
    )
    assert bundle.client.calls.count("timeline") == 1


def test_feed_endpoint_reset_also_forces_timeline(tmp_path):
    """强制规则与端点无关：feed 里出现确认重置同样把下一槽位换成 timeline。"""

    async def scenario():
        clock = FakeClock(utc(2026, 10, 20, 12))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6))
            bundle = make_pipeline(repo, clock, client=FakeClient(), rotation=("feed", "forecast"))
            moment = clock.utcnow()
            bundle.client.push(
                ok_fetch(
                    "feed",
                    feed_payload(
                        fetched_at="2026-10-20T11:59:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=moment,
                    batch="b0",
                    checked_at=moment,
                )
            )
            bundle.client.push(
                ok_fetch(
                    "timeline",
                    timeline_payload(updated_at="2026-10-20T12:00:30Z", events=[]),
                    fetched_at=moment,
                    batch="b1",
                    checked_at=moment,
                )
            )
            first = await bundle.pipeline.poll_round(moment)
            clock.advance(61)
            second = await bundle.pipeline.poll_round(clock.utcnow())
            return first, second, bundle
        finally:
            await repo.close()

    first, second, bundle = run(scenario())

    assert first is not None and first.endpoint == "feed"
    assert first.new_events, "feed 中的确认重置必须入库"
    assert second is not None and second.endpoint == "timeline"
    assert bundle.client.calls == ["feed", "timeline"]


def test_forced_timeline_round_still_classifies_normally(tmp_path):
    """强制轮次不是「空转」：taken 的 timeline 快照仍要正常判定并推进基线。"""

    async def scenario():
        clock = FakeClock(utc(2026, 10, 20, 12))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6))
            bundle = make_pipeline(repo, clock, client=FakeClient(), rotation=("forecast", "feed"))
            moment = clock.utcnow()
            bundle.client.push(
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT, source_at=RESET_AT),
                    ),
                    fetched_at=moment,
                    batch="b0",
                    checked_at=moment,
                )
            )
            await bundle.pipeline.poll_round(moment)
            clock.advance(61)
            moment = clock.utcnow()
            bundle.client.push(
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T12:00:30Z",
                        events=[reset_row(RESET_ID, RESET_AT)],
                    ),
                    fetched_at=moment,
                    batch="b1",
                    checked_at=moment,
                )
            )
            outcome = await bundle.pipeline.poll_round(moment)
            baseline = await repo.baseline()
            return outcome, baseline
        finally:
            await repo.close()

    outcome, baseline = run(scenario())

    assert outcome is not None and outcome.endpoint == "timeline"
    assert outcome.error_code == ErrorCode.OK
    assert outcome.judged is True, "强制轮次仍须走判定（不是只取快照）"
    assert baseline.built is True
    assert baseline.last_reset_at == utc(2026, 10, 20, 6, 33, 48)


def test_spacing_config_change_keeps_forced_slot_pending():
    """热更新 `spacing_seconds` 不得丢掉已排队的强制槽位（配置改动 ≠ 取消语义）。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=600, clock=clock, rotation=("feed",))
    scheduler.next_endpoint(clock.utcnow())
    scheduler.record_result("feed", ErrorCode.OK, clock.utcnow())
    scheduler.force_timeline("confirmed_reset")

    scheduler.set_spacing(60)
    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"


def test_replacing_pending_reason_keeps_single_pending_slot():
    """连续两次强制只保留一个槽位（后到的原因覆盖先到的），不会插两条 timeline。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=60, clock=clock, rotation=("feed", "forecast"))
    scheduler.force_timeline("new_watch")
    scheduler.force_timeline("confirmed_reset")

    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"
    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) == "feed", "只允许插入一条强制槽位"


def test_forced_slot_never_returns_unknown_endpoint():
    """强制值必须是合法 endpoint（否则 pipeline 会在 `ingest` 抛 ValueError）。"""
    clock = FakeClock(utc(2026, 10, 20, 12))
    scheduler = SourceScheduler(spacing_seconds=60, clock=clock, rotation=("feed",))
    scheduler.force_timeline("confirmed_reset")
    endpoint = scheduler.next_endpoint(clock.utcnow())
    assert endpoint == "timeline"


def test_baseline_only_round_does_not_force_timeline(tmp_path):
    """基线首轮（baseline_only）不得强制 timeline：历史事件不是「新 Watch」。"""

    async def scenario():
        clock = FakeClock(utc(2026, 10, 20, 12))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            bundle = make_pipeline(repo, clock, client=FakeClient(), rotation=("forecast", "feed"))
            moment = clock.utcnow()
            bundle.client.push(
                ok_fetch(
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT, source_at=RESET_AT),
                    ),
                    fetched_at=moment,
                    batch="b0",
                    checked_at=moment,
                )
            )
            bundle.client.push(
                ok_fetch(
                    "feed",
                    feed_payload(fetched_at="2026-10-20T11:59:30Z"),
                    fetched_at=moment,
                    batch="b1",
                    checked_at=moment,
                )
            )
            first = await bundle.pipeline.poll_round(moment)
            clock.advance(61)
            second = await bundle.pipeline.poll_round(clock.utcnow())
            return first, second, bundle
        finally:
            await repo.close()

    first, second, bundle = run(scenario())

    assert first is not None and first.baseline_only is True
    assert second is not None and second.endpoint == "feed", (
        "基线轮次的历史事件不得触发强制 timeline"
    )
    assert bundle.client.calls == ["forecast", "feed"]


def test_force_reason_is_pure_helper_without_side_effects():
    """`_force_reason` 只读：不得修改传入事件（避免判定逻辑篡改入库数据）。"""
    event = _event("reset")
    snapshot = replace(event)
    assert Pipeline._force_reason([event]) == "confirmed_reset"
    assert event == snapshot
