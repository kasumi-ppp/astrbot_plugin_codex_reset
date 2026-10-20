"""AC-09 / AC-10 / AC-11：预测状态机（连续采样、滞回、冷却、单日上限、版本号）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from conftest import SHANGHAI, load_fixture, utc

from services.classifier import ForecastEvaluator
from services.models import ForecastDecision, ForecastSnapshot, ForecastState, GroupSettings
from services.notifications import render_forecast_push
from services.parser import parse_iso

SUB_ID = "sub:test"


def _evaluator(**kwargs: int) -> ForecastEvaluator:
    return ForecastEvaluator(time_zone=SHANGHAI, **kwargs)  # type: ignore[arg-type]


def _series(name: str) -> list[ForecastSnapshot]:
    data = load_fixture(name)
    snapshots: list[ForecastSnapshot] = []
    for item in data["series"]:
        updated_at = parse_iso(item["updated_at"])
        assert updated_at is not None
        snapshots.append(
            ForecastSnapshot(
                updated_at=updated_at,
                rounded_24h=int(item["rounded_24h"]),
                rounded_48h=int(item["rounded_48h"]),
                confidence=str(data.get("confidence", "low")),
            )
        )
    return snapshots


def _snapshot(
    rounded_24h: int,
    rounded_48h: int,
    *,
    at: datetime | None = None,
    confidence: str = "low",
) -> ForecastSnapshot:
    return ForecastSnapshot(
        updated_at=at or utc(2026, 10, 20, 10, 0),
        rounded_24h=rounded_24h,
        rounded_48h=rounded_48h,
        confidence=confidence,
    )


def _drive(
    snapshots: list[ForecastSnapshot],
    *,
    settings: GroupSettings,
    evaluator: ForecastEvaluator | None = None,
    state: ForecastState | None = None,
) -> tuple[ForecastState, list[ForecastDecision]]:
    engine = evaluator or _evaluator()
    current = state or ForecastState(subscription_id=SUB_ID)
    decisions: list[ForecastDecision] = []
    for snapshot in snapshots:
        current, decision = engine.step(
            current, settings, snapshot, snapshot.updated_at, fresh=True
        )
        if decision is not None:
            decisions.append(decision)
    return current, decisions


# ---------------------------------------------------------------------------
# AC-09：42 → 51 → 54（confirm_samples=2）只触发一次
# ---------------------------------------------------------------------------
def test_two_consecutive_hits_trigger_exactly_once() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)  # 48h 关闭，只看 24h
    series = _series("prob_series_42_51_54")
    state, decisions = _drive(series[:3], settings=settings)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.logical_id == f"forecast:{SUB_ID}:h24"
    assert decision.window_kind == "h24"
    assert decision.state_version == "h24:0:1"  # 改动 3：{window_kind}:{epoch}:{alert}
    assert decision.rounded_24h == 54
    assert decision.confidence == "low"

    assert state.alert_seq == 1
    assert state.day_count == 1
    assert state.armed_24h is False
    assert state.hits_24h == 0  # 触发时清零
    assert state.cooldown_until == utc(2026, 10, 20, 22, 10)  # now + 12h

    # 第 4 个样本处于冷却期且已解除武装 → 不再触发
    state, decisions = _drive(series, settings=settings)
    assert len(decisions) == 1


def test_first_below_threshold_sample_does_not_count() -> None:
    """42 未达标 → hits 归零，故 42/51 不会触发（需要连续两次）。"""
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    series = _series("prob_series_42_51_54")[:2]
    _, decisions = _drive(series, settings=settings)
    assert decisions == []


# ---------------------------------------------------------------------------
# AC-10：长期高位不刷屏；滞回跌破才 re-arm
# ---------------------------------------------------------------------------
def test_long_high_series_triggers_at_most_once() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=70)
    state, decisions = _drive(_series("prob_series_60"), settings=settings)
    assert len(decisions) <= 1
    assert state.armed_24h is False
    assert state.day_count == 1


def test_rearm_only_after_probability_drops_below_threshold_minus_ten() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    state, decisions = _drive(_series("prob_series_60"), settings=settings)
    assert len(decisions) == 1
    assert state.armed_24h is False

    evaluator = _evaluator()
    # 45 仍高于 max(0, 50-10)=40 → 不武装
    probe, decision = evaluator.step(
        state, settings, _snapshot(45, 0, at=utc(2026, 10, 20, 15, 0)), utc(2026, 10, 20, 15, 0),
        fresh=True,
    )
    assert decision is None
    assert probe.armed_24h is False

    # 35 ≤ 40 → 重新武装（但仍然在 12h 冷却内，不推送）
    probe, decision = evaluator.step(
        probe, settings, _snapshot(35, 0, at=utc(2026, 10, 20, 15, 5)), utc(2026, 10, 20, 15, 5),
        fresh=True,
    )
    assert decision is None
    assert probe.armed_24h is True
    assert probe.hits_24h == 0


def test_cooldown_blocks_second_push_same_day() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    evaluator = _evaluator(confirm_samples=1)
    state = ForecastState(subscription_id=SUB_ID)
    state, first = evaluator.step(
        state, settings, _snapshot(60, 0, at=utc(2026, 10, 20, 10, 0)), utc(2026, 10, 20, 10, 0),
        fresh=True,
    )
    assert first is not None
    # 冷却期内再次达标
    state, second = evaluator.step(
        state, settings, _snapshot(60, 0, at=utc(2026, 10, 20, 10, 5)), utc(2026, 10, 20, 10, 5),
        fresh=True,
    )
    assert second is None
    assert state.alert_seq == 1


def test_daily_max_caps_pushes_even_after_cooldown() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    evaluator = _evaluator(cooldown_hours=0, daily_max=1, confirm_samples=1)
    state = ForecastState(subscription_id=SUB_ID)
    state, first = evaluator.step(
        state, settings, _snapshot(60, 0, at=utc(2026, 10, 20, 10, 0)), utc(2026, 10, 20, 10, 0),
        fresh=True,
    )
    assert first is not None
    # 冷却为 0，但单日上限 1 → 仍不允许第二条
    state, second = evaluator.step(
        state, settings, _snapshot(60, 0, at=utc(2026, 10, 20, 10, 5)), utc(2026, 10, 20, 10, 5),
        fresh=True,
    )
    assert second is None
    assert state.day_count == 1


# ---------------------------------------------------------------------------
# AC-11：confidence=low 仍推送，但必须标注实验性
# ---------------------------------------------------------------------------
def test_low_confidence_still_pushes_with_experimental_wording() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    _, decisions = _drive(_series("prob_series_42_51_54"), settings=settings)
    decision = decisions[0]
    assert decision.confidence == "low"

    text = render_forecast_push(decision, tz=SHANGHAI, settings=GroupSettings())
    assert "模型置信度：低" in text
    assert "实验性" in text
    assert "不代表官方承诺" in text
    for forbidden in ("已确认", "即将重置", "必将", "确认"):
        assert forbidden not in text


def test_unknown_confidence_is_marked() -> None:
    text = render_forecast_push(
        ForecastDecision(
            logical_id=f"forecast:{SUB_ID}:h24",
            state_version="h24:0:1",
            window_kind="h24",
            rounded_24h=54,
            rounded_48h=73,
            confidence="ultra",
            updated_at=utc(2026, 10, 20, 8, 0),
        ),
        tz=SHANGHAI,
        settings=GroupSettings(),
    )
    assert "模型置信度：ultra（未识别置信度）" in text


# ---------------------------------------------------------------------------
# D-06：threshold=0 关闭该窗口
# ---------------------------------------------------------------------------
def test_zero_threshold_disables_window() -> None:
    settings = GroupSettings(threshold_24h=0, threshold_48h=0)
    _, decisions = _drive([_snapshot(100, 100), _snapshot(100, 100, at=utc(2026, 10, 20, 10, 5))],
                          settings=settings)
    assert decisions == []


def test_zero_threshold_24h_keeps_48h_working() -> None:
    settings = GroupSettings(threshold_24h=0, threshold_48h=70)
    state, decisions = _drive(
        [
            _snapshot(100, 75, at=utc(2026, 10, 20, 10, 0)),
            _snapshot(100, 75, at=utc(2026, 10, 20, 10, 5)),
        ],
        settings=settings,
    )
    assert len(decisions) == 1
    assert decisions[0].window_kind == "h48"
    assert decisions[0].state_version == "h48:0:1"
    assert state.hits_24h == 0
    text = render_forecast_push(decisions[0], tz=SHANGHAI, settings=settings)
    assert "📈【未来 48 小时重置预测】" in text


def test_both_windows_merge_into_single_message() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=70)
    _, decisions = _drive(
        [
            _snapshot(60, 80, at=utc(2026, 10, 20, 10, 0)),
            _snapshot(60, 80, at=utc(2026, 10, 20, 10, 5)),
        ],
        settings=settings,
    )
    assert len(decisions) == 1
    assert decisions[0].window_kind == "both"
    assert decisions[0].state_version == "both:0:1"
    text = render_forecast_push(decisions[0], tz=SHANGHAI, settings=settings)
    assert "📈【Codex 重置概率升高】" in text
    assert "未来 24 小时：60%" in text
    assert "未来 48 小时：80%" in text


# ---------------------------------------------------------------------------
# 采样防护与自然日
# ---------------------------------------------------------------------------
def test_fresh_false_returns_state_untouched() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=70)
    evaluator = _evaluator()
    state = ForecastState(subscription_id=SUB_ID, hits_24h=1)
    snapshot = _snapshot(60, 80)
    new_state, decision = evaluator.step(state, settings, snapshot, snapshot.updated_at, fresh=False)
    assert decision is None
    assert new_state is state  # 原样返回，不采样
    assert new_state.hits_24h == 1


def test_same_snapshot_is_not_counted_twice() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=70)
    evaluator = _evaluator()
    snapshot = _snapshot(60, 80, at=utc(2026, 10, 20, 10, 0))
    state = ForecastState(subscription_id=SUB_ID)
    state, _ = evaluator.step(state, settings, snapshot, utc(2026, 10, 20, 10, 0), fresh=True)
    assert state.hits_24h == 1
    state, decision = evaluator.step(
        state, settings, snapshot, utc(2026, 10, 20, 10, 6), fresh=True
    )
    assert decision is None
    assert state.hits_24h == 1  # 同快照不重复计数（D-11）


def test_day_rollover_resets_day_count_but_keeps_epoch() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=70)
    evaluator = _evaluator()
    state = ForecastState(
        subscription_id=SUB_ID,
        day_key="2026-10-19",
        day_count=2,
        epoch_seq=3,
        last_sample_at=utc(2026, 10, 19, 23, 0),
    )
    snapshot = _snapshot(20, 20, at=utc(2026, 10, 20, 0, 30))
    new_state, decision = evaluator.step(
        state, settings, snapshot, utc(2026, 10, 20, 0, 30), fresh=True
    )
    assert decision is None
    assert new_state.day_count == 0
    assert new_state.day_key == "2026-10-20"
    assert new_state.epoch_seq == 3  # D-05：epoch 不因自然日变化


def test_bump_epoch_only_increments_epoch_and_rearms() -> None:
    evaluator = _evaluator()
    state = ForecastState(
        subscription_id=SUB_ID,
        hits_24h=2,
        hits_48h=1,
        armed_24h=False,
        armed_48h=False,
        epoch_seq=4,
        alert_seq=2,
        day_count=2,
    )
    bumped = evaluator.bump_epoch(state)
    assert bumped.epoch_seq == 5
    assert bumped.alert_seq == 2  # alert_seq 只随消息 +1
    assert bumped.hits_24h == 0
    assert bumped.hits_48h == 0
    assert bumped.armed_24h is True
    assert bumped.armed_48h is True
    assert bumped.day_count == 2  # 单日上限不因 reset 归零（D-05）
    assert state.epoch_seq == 4  # 纯函数：原 state 未被修改


def test_state_version_uses_epoch_and_alert_seq() -> None:
    settings = GroupSettings(threshold_24h=50, threshold_48h=0)
    evaluator = _evaluator()
    state = ForecastState(
        subscription_id=SUB_ID,
        epoch_seq=2,
        alert_seq=1,
        last_sample_at=utc(2026, 10, 20, 9, 0),
    )
    new_state, decision = evaluator.step(
        replace(state),
        settings,
        _snapshot(60, 0, at=utc(2026, 10, 20, 9, 5)),
        utc(2026, 10, 20, 9, 5),
        fresh=True,
    )
    # 第一次：hits=1，未达 confirm_samples=2
    assert decision is None
    new_state, decision = evaluator.step(
        new_state,
        settings,
        _snapshot(60, 0, at=utc(2026, 10, 20, 9, 10)),
        utc(2026, 10, 20, 9, 10),
        fresh=True,
    )
    assert decision is not None
    assert decision.state_version == "h24:2:2"


def test_day_key_uses_configured_timezone() -> None:
    evaluator = _evaluator()
    # 2026-10-20T16:30Z = 北京时间 2026-10-21 00:30
    assert evaluator.day_key(utc(2026, 10, 20, 16, 30)) == "2026-10-21"
    assert evaluator.day_key(utc(2026, 10, 20, 15, 59)) == "2026-10-20"
