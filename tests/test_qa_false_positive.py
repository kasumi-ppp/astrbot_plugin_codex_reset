"""QA 独立回归 ①：误报确认（AC-01 / AC-02 / D-01 / D-02 / D-03 / F04 触发条件）。

断言口径直接来自 `docs/PRD-confirmation.md`：

- F01 的唯一充分必要条件：`group=="reset"` **且** `announcement_state=="announced"`
  **且** `confirmed is True`（D-02：只判 state 会误报真实陷阱行 `2109045392684032299`）；
- D-01：`announcement_state` 缺失（旧归档行）走 fail-closed，绝不当作确认；
- 摘要文本出现 reset、`preview=true`、`group=boost/unlock` 一律不得产生 reset 事件；
- D-03：credits 的推送状态只看 `banked_state`，`unknown`/缺失要「静默入库不推送」。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
from datetime import UTC, datetime

import pytest

from conftest import FakeClock, run

from services.classifier import EventClassifier
from services.models import Baseline
from services.parser import parse_forecast, parse_timeline
from qa_helpers import (
    NONE_ROW_ID,
    NOW,
    REAL_RESET_ID,
    TRAP_ROW_ID,
    UMO,
    built_baseline,
    confirmed_reset_row,
    credits_row,
    event_row,
    forecast_payload,
    make_env,
    ok_fetch,
    reset_alert,
    timeline_payload,
    watch_alert,
)


def _timeline(rows):
    """原始行 → `TimelineSnapshot`（走真正的 parser，不手搓 dataclass）。"""
    result = parse_timeline(timeline_payload("2026-10-20T11:55:00.000Z", rows), fetched_at=NOW)
    assert result.ok is True, result.warnings
    return result.snapshot


def _classifier() -> EventClassifier:
    return EventClassifier(clock=FakeClock(NOW))


def _reset_events(timeline=None, forecast=None):
    return _classifier().reset_candidates(
        forecast=forecast, timeline=timeline, feed=None, baseline=Baseline(built=True)
    )


# ---------------------------------------------------------------------------
# AC-01：合规行必须恰好产出 1 个确认事件（同时守住「不要把所有都杀掉」）
# ---------------------------------------------------------------------------
def test_fully_confirmed_row_yields_exactly_one_event():
    """AC-01：`group=reset` + `announced` + `confirmed=true` → 1 个事件、版本 `confirmed`。"""
    events = _reset_events(_timeline([confirmed_reset_row(REAL_RESET_ID, "2026-10-20T06:33:48.000Z")]))

    assert len(events) == 1
    event = events[0]
    assert event.kind == "reset"
    assert event.logical_id == f"reset:{REAL_RESET_ID}"
    assert event.state_version == "confirmed"
    assert event.occurred_at == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AC-02：两个真实陷阱行必须零事件
# ---------------------------------------------------------------------------
def test_announcement_state_none_yields_zero_events():
    """AC-02 前半：真实行 `2111492673890824348`（`announcement_state=none`，confirmed=true）不得推送。

    注意这一行的 `confirmed` 是 true —— 只有 `announcement_state` 挡住它，正是 AC-02 的设计意图。
    """
    row = event_row(
        NONE_ROW_ID,
        announced_at="2026-10-17T09:00:00.000Z",
        announcement_state="none",
        confirmed=True,
    )
    assert _reset_events(_timeline([row])) == [], "announcement_state=none 不应被当作已确认重置"


def test_announced_but_unconfirmed_yields_zero_events():
    """AC-02 后半 / D-02：真实行 `2109045392684032299`（announced 但 confirmed=false）。"""
    row = event_row(
        TRAP_ROW_ID,
        announced_at="2026-10-11T03:00:00.000Z",
        announcement_state="announced",
        confirmed=False,
    )
    events = _reset_events(_timeline([row]))

    assert events == [], "只判 announcement_state 会误报 D-02 陷阱行"


@pytest.mark.parametrize("bad_state", [None, "pending", "ANNOUNCED", 123, ["announced"]])
def test_non_announced_announcement_state_yields_zero_events(bad_state):
    """D-01：缺失/未知/大小写不符的状态值一律 fail-closed（含类型污染）。"""
    row = confirmed_reset_row("2100000000000000001", "2026-10-10T00:00:00.000Z")
    if bad_state is None:
        row.pop("announcement_state")
    else:
        row["announcement_state"] = bad_state

    assert _reset_events(_timeline([row])) == []


@pytest.mark.parametrize("bad_confirmed", [None, "true", 1, 0])
def test_non_boolean_confirmed_yields_zero_events(bad_confirmed):
    """D-02：`confirmed` 必须是 JSON 布尔 true；字符串/数字/None 全部 fail-closed。"""
    row = confirmed_reset_row("2100000000000000002", "2026-10-10T00:00:00.000Z")
    if bad_confirmed is None:
        row.pop("confirmed")
    else:
        row["confirmed"] = bad_confirmed

    assert _reset_events(_timeline([row])) == []


# ---------------------------------------------------------------------------
# 摘要文本 / preview / 其他 group 枚举
# ---------------------------------------------------------------------------
def test_summary_text_containing_reset_is_not_a_trigger():
    """PRD §1「不得用摘要里出现 reset 就当作重置」：credits 行摘要写满 reset 也不算。"""
    row = credits_row("2107975486798164736", banked_state=None)
    row["summary"] = "Codex global reset confirmed! reset reset reset"
    snapshot = _timeline([row])

    assert _reset_events(snapshot) == []
    assert _classifier().credit_candidates(timeline=snapshot, feed=None) == []


def test_preview_row_yields_zero_events():
    """`preview=true` 是站点预览/占位行，不得当作已确认重置。"""
    row = confirmed_reset_row("2100000000000000003", "2026-10-20T06:00:00.000Z")
    row["preview"] = True

    assert _reset_events(_timeline([row])) == []


@pytest.mark.parametrize("group", ["boost", "unlock", "credits", "weird", "RESET", ""])
def test_other_group_values_yield_zero_reset_events(group):
    """只有 `group=="reset"`（且其余条件满足）才是 F01 触发源。"""
    row = confirmed_reset_row("2100000000000000004", "2026-10-20T06:00:00.000Z")
    row["group"] = group
    row["type"] = group

    assert _reset_events(_timeline([row])) == []


# ---------------------------------------------------------------------------
# F01 条件③：latest_alert 只认 kind=reset 且 state=confirmed
# ---------------------------------------------------------------------------
def test_alert_kind_reset_but_state_active_is_not_confirmed():
    """`kind=reset` 但 `state=active` 只是信号，不是确认（F01 只认 state=confirmed）。"""
    alert = reset_alert()
    alert["state"] = "active"
    forecast = parse_forecast(forecast_payload(alert=alert), fetched_at=NOW).snapshot
    assert forecast is not None

    assert _reset_events(forecast=forecast) == []


def test_alert_kind_watch_with_confirmed_state_is_not_a_reset():
    """`kind=watch` 即使 state 被污染成 confirmed，也不得算作确认重置。"""
    alert = watch_alert(state="confirmed")
    forecast = parse_forecast(forecast_payload(alert=alert), fetched_at=NOW).snapshot
    assert forecast is not None

    assert _reset_events(forecast=forecast) == []


def test_alert_reset_confirmed_yields_single_event():
    """F01 条件③：`kind=reset && state=confirmed` → 1 个事件，id 与 timeline 同源（D-08）。"""
    forecast = parse_forecast(forecast_payload(alert=reset_alert()), fetched_at=NOW).snapshot
    assert forecast is not None

    events = _reset_events(forecast=forecast)

    assert len(events) == 1
    assert events[0].logical_id == f"reset:{REAL_RESET_ID}"


# ---------------------------------------------------------------------------
# D-03：credits 只看 banked_state
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["unknown", None, "weird"])
def test_credits_without_pushable_state_is_silent(state):
    """D-03：`unknown` / 缺失 / 未识别状态 → 不产生事件（静默入库、可查询、不推送）。"""
    row = credits_row("2107913674593644711", banked_state=state)
    snapshot = _timeline([row])

    assert _classifier().credit_candidates(timeline=snapshot, feed=None) == []


@pytest.mark.parametrize("state", ["announced", "arriving", "available"])
def test_credits_pushable_states_yield_one_event_each(state):
    """F04：三种可推送状态各产生 1 个事件，且 `state_version == banked_state`。"""
    row = credits_row("2102463847714247142", banked_state=state)
    events = _classifier().credit_candidates(timeline=_timeline([row]), feed=None)

    assert len(events) == 1
    assert events[0].kind == "credits"
    assert events[0].state_version == state


# ---------------------------------------------------------------------------
# 集成层：一整批陷阱行在真实处理链里必须零投递
# ---------------------------------------------------------------------------
def test_trap_batch_produces_zero_deliveries(tmp_path):
    """AC-02 集成口径：基线已建立 + 已订阅，一批陷阱行仍必须 0 投递、0 reset 事件。"""
    none_row = event_row(
        NONE_ROW_ID,
        announced_at="2026-10-17T09:00:00.000Z",
        announcement_state="none",
        confirmed=True,
    )
    trap_row = event_row(
        TRAP_ROW_ID,
        announced_at="2026-10-11T03:00:00.000Z",
        announcement_state="announced",
        confirmed=False,
    )
    preview_row = confirmed_reset_row("2100000000000000005", "2026-10-20T06:00:00.000Z")
    preview_row["preview"] = True
    boost_row = confirmed_reset_row("2100000000000000006", "2026-10-20T06:30:00.000Z")
    boost_row["group"] = "boost"
    credits_trap = credits_row("2100000000000000007", banked_state="unknown")
    credits_trap["summary"] = "reset confirmed, reset, reset"

    async def scenario():
        repo, bundle, clock = await make_env(tmp_path, baseline=built_baseline())
        clock.set_now(NOW)
        payload = timeline_payload("2026-10-20T11:55:00.000Z", [
            none_row, trap_row, preview_row, boost_row, credits_trap,
        ])
        outcome = await bundle.pipeline.ingest("timeline", ok_fetch("timeline", payload, at=NOW))
        return outcome, await repo.count_deliveries(), await repo.list_events(limit=50)

    outcome, deliveries, events = run(scenario())

    assert outcome.judged is True
    assert outcome.baseline_only is False
    assert outcome.deliveries_created == 0
    assert deliveries == 0
    assert events == [], "陷阱行不得写入任何逻辑事件"


def test_pipeline_still_delivers_the_one_genuine_reset(tmp_path):
    """反向守门：同批里唯一的合规行必须且只能产生 1 条投递（避免「全杀」假绿）。"""
    traps = [
        event_row(TRAP_ROW_ID, announced_at="2026-10-11T03:00:00.000Z", confirmed=False),
        event_row(NONE_ROW_ID, announced_at="2026-10-17T09:00:00.000Z", announcement_state="none"),
    ]

    async def scenario():
        repo, bundle, clock = await make_env(tmp_path, baseline=built_baseline())
        clock.set_now(NOW)
        payload = timeline_payload(
            "2026-10-20T11:55:00.000Z",
            [*traps, confirmed_reset_row(REAL_RESET_ID, "2026-10-20T06:33:48.000Z")],
        )
        outcome = await bundle.pipeline.ingest("timeline", ok_fetch("timeline", payload, at=NOW))
        return outcome, await repo.list_deliveries()

    outcome, deliveries = run(scenario())

    assert outcome.deliveries_created == 1
    assert len(deliveries) == 1
    assert deliveries[0].logical_id == f"reset:{REAL_RESET_ID}"
    assert deliveries[0].umo == UMO
