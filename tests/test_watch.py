"""AC-05 / AC-06 / AC-07 / AC-08：Watch 提前预警、基线水位、更正与无窗口文案。"""

from __future__ import annotations

import re
from typing import Any

from conftest import SHANGHAI, load_fixture, utc

from services.classifier import EventClassifier
from services.clock import FakeClock
from services.models import Baseline, ForecastSnapshot, GroupSettings, LogicalEvent
from services.notifications import render_watch
from services.parser import parse_forecast

FETCHED_AT = utc(2026, 10, 20, 12, 0)
NO_FAKE_HOURS = re.compile(r"\d+\s*(小时|点)")


def _forecast(name: str, **alert_overrides: Any) -> ForecastSnapshot:
    payload = load_fixture(name)
    if alert_overrides:
        alert = payload.get("latest_alert") or {}
        payload["latest_alert"] = {**alert, **alert_overrides}
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    return result.snapshot


def _classifier(now: Any = FETCHED_AT) -> EventClassifier:
    return EventClassifier(clock=FakeClock(now))


def _active_event() -> LogicalEvent:
    events = _classifier().watch_candidates(
        forecast=_forecast("forecast_watch_active"), baseline=Baseline()
    )
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# AC-05：新 active Watch → 一条预警，措辞非确认
# ---------------------------------------------------------------------------
def test_active_watch_yields_single_prewarning() -> None:
    event = _active_event()
    assert event.logical_id == "watch:9000000000000000001"
    assert event.kind == "watch"
    assert event.group_kind == "watch"
    assert event.state_version == "active:2026-10-21T12:00:00+00:00"
    assert event.occurred_at == utc(2026, 10, 20, 11, 0)
    assert event.payload["alert_id"] == "9000000000000000001"
    assert event.payload["state"] == "active"
    assert event.payload["window"] == {
        "label": "20:00 - 22:00 Asia/Shanghai",
        "start_at": "2026-10-21T12:00:00+00:00",
        "end_at": "2026-10-21T14:00:00+00:00",
        "time_zone": "Asia/Shanghai",
        "target_kind": None,
        "target_at": None,
    }
    assert event.payload["pending"] is None
    assert event.payload["score"] == 78


def test_watch_text_is_not_a_confirmation() -> None:
    text = render_watch(_active_event().payload, tz=SHANGHAI, settings=GroupSettings())
    assert "⚠️【Codex 全球重置提前预警】" in text
    assert "提前预警" in text
    assert "这不是已重置通知" in text
    assert "已确认" not in text
    assert "即将重置" not in text
    # 同一天窗口 → 今日 HH:MM—HH:MM（北京时间）
    assert "预计窗口：今日 20:00—22:00（北京时间）" in text
    assert "数据来源：https://codex-reset.com/" in text
    assert "原始消息：https://x.com/thsottiaux/status/9000000000000000001" in text


# ---------------------------------------------------------------------------
# AC-06：空基线 + expired 历史 Alert → 零事件
# ---------------------------------------------------------------------------
def test_expired_alert_with_empty_baseline_never_notifies() -> None:
    baseline = Baseline(built=False)
    assert _classifier().watch_candidates(
        forecast=_forecast("forecast_watch_expired"), baseline=baseline
    ) == []


def test_watch_candidates_requires_active_state() -> None:
    for state in ("expired", "confirmed"):
        assert _classifier().watch_candidates(
            forecast=_forecast("forecast_watch_active", state=state), baseline=Baseline()
        ) == []


def test_baseline_source_at_watermark_blocks_old_alert() -> None:
    baseline = Baseline(
        built=True,
        watch_source_at=utc(2026, 10, 20, 11, 30),
        built_at=utc(2026, 10, 20, 11, 40),
    )
    # source_at=11:00 早于水位 → 不是新预告
    assert _classifier().watch_candidates(
        forecast=_forecast("forecast_watch_active"), baseline=baseline
    ) == []
    newer = Baseline(
        built=True,
        watch_source_at=utc(2026, 10, 20, 10, 0),
        built_at=utc(2026, 10, 20, 10, 30),
    )
    assert len(
        _classifier().watch_candidates(forecast=_forecast("forecast_watch_active"), baseline=newer)
    ) == 1


def test_baseline_without_source_at_uses_built_at() -> None:
    forecast = _forecast("forecast_watch_active", source_at=None)
    early = Baseline(built=True, built_at=utc(2026, 10, 20, 13, 0))
    assert _classifier(utc(2026, 10, 20, 12, 0)).watch_candidates(
        forecast=forecast, baseline=early
    ) == []
    late = Baseline(built=True, built_at=utc(2026, 10, 20, 11, 0))
    assert len(
        _classifier(utc(2026, 10, 20, 12, 0)).watch_candidates(forecast=forecast, baseline=late)
    ) == 1


# ---------------------------------------------------------------------------
# AC-07：active → expired / corrected / window_elapsed 各产生一次更正
# ---------------------------------------------------------------------------
def test_active_then_expired_yields_single_correction() -> None:
    previous = _active_event()
    corrections = _classifier(utc(2026, 10, 22, 0, 0)).watch_corrections(
        forecast=_forecast("forecast_watch_active", state="expired"),
        previous=previous,
        last_reset_at=None,
        now=utc(2026, 10, 22, 0, 0),
    )
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction.logical_id == previous.logical_id
    assert correction.correction_reason == "expired"
    assert correction.state_version == "expired:2026-10-21T12:00:00+00:00"
    assert correction.state_version != previous.state_version
    assert correction.payload["reason"] == "expired"
    assert correction.payload["previous_state"] == "active"


def test_corrected_flag_yields_correction() -> None:
    previous = _active_event()
    corrections = _classifier(utc(2026, 10, 20, 13, 0)).watch_corrections(
        forecast=_forecast("forecast_watch_corrected"),
        previous=previous,
        last_reset_at=None,
        now=utc(2026, 10, 20, 13, 0),
    )
    assert len(corrections) == 1
    assert corrections[0].state_version == "corrected"
    assert corrections[0].correction_reason == "corrected"


def test_window_elapsed_without_reset_yields_correction() -> None:
    previous = _active_event()
    now = utc(2026, 10, 21, 15, 0)  # 晚于 window.end_at=14:00Z
    corrections = _classifier(now).watch_corrections(
        forecast=_forecast("forecast_watch_active"),
        previous=previous,
        last_reset_at=None,
        now=now,
    )
    assert len(corrections) == 1
    assert corrections[0].state_version == "expired_window:2026-10-21T14:00:00+00:00"
    assert corrections[0].correction_reason == "window_elapsed"


def test_window_elapsed_is_suppressed_when_reset_landed_after_the_window() -> None:
    """`last_reset_at >= window.end_at` 时由重置确认消息接管，不再补「预告过期」。"""
    previous = _active_event()
    now = utc(2026, 10, 21, 15, 0)
    corrections = _classifier(now).watch_corrections(
        forecast=_forecast("forecast_watch_active"),
        previous=previous,
        last_reset_at=utc(2026, 10, 21, 14, 30),
        now=now,
    )
    assert corrections == []


def test_expired_takes_priority_over_corrected() -> None:
    previous = _active_event()
    corrections = _classifier(utc(2026, 10, 22, 0, 0)).watch_corrections(
        forecast=_forecast(
            "forecast_watch_active", id="9000000000000000001", state="expired", corrected=True
        ),
        previous=previous,
        last_reset_at=None,
        now=utc(2026, 10, 22, 0, 0),
    )
    assert corrections[0].correction_reason == "expired"


def test_alert_disappearance_produces_no_correction() -> None:
    """D-04：列表消失 ≠ 撤回，绝不产生任何更正消息。"""
    previous = _active_event()
    payload = load_fixture("forecast_watch_active")
    payload["latest_alert"] = None
    forecast = parse_forecast(payload, fetched_at=FETCHED_AT).snapshot
    assert forecast is not None
    assert (
        _classifier().watch_corrections(
            forecast=forecast, previous=previous, last_reset_at=None, now=utc(2026, 10, 21, 15)
        )
        == []
    )
    # 换成另一个 alert.id 也不算更正
    assert (
        _classifier().watch_corrections(
            forecast=_forecast("forecast_watch_expired"),
            previous=previous,
            last_reset_at=None,
            now=utc(2026, 10, 21, 15),
        )
        == []
    )


def test_no_previous_event_means_no_correction() -> None:
    assert (
        _classifier().watch_corrections(
            forecast=_forecast("forecast_watch_expired"),
            previous=None,
            last_reset_at=None,
            now=utc(2026, 10, 22),
        )
        == []
    )


# ---------------------------------------------------------------------------
# AC-08：无窗口 → 固定句，且不得出现自造小时数
# ---------------------------------------------------------------------------
def test_nowindow_text_uses_fixed_phrase_without_fake_hours() -> None:
    event = _classifier().watch_candidates(
        forecast=_forecast("forecast_watch_nowindow"), baseline=Baseline()
    )[0]
    assert event.state_version == "active:nowindow"
    text = render_watch(event.payload, tz=SHANGHAI, settings=GroupSettings())
    assert "预计窗口：暂无明确预计时段" in text
    assert NO_FAKE_HOURS.search(text) is None
    assert "小时" not in text


def test_label_only_window_uses_label_verbatim() -> None:
    payload = {
        "state": "active",
        "window": {"label": "3 AM - 6 AM UTC", "start_at": None, "end_at": None},
        "url": None,
        "pending": None,
    }
    text = render_watch(payload, tz=SHANGHAI, settings=GroupSettings())
    assert "预计窗口：3 AM - 6 AM UTC" in text


def test_cross_day_window_renders_full_timestamps() -> None:
    payload = {
        "state": "active",
        "window": {
            "label": "跨天",
            "start_at": "2026-10-21T15:00:00+00:00",  # 北京时间 10-21 23:00
            "end_at": "2026-10-21T17:00:00+00:00",  # 北京时间 10-22 01:00
        },
        "url": None,
        "pending": None,
    }
    text = render_watch(payload, tz=SHANGHAI, settings=GroupSettings())
    assert "预计窗口：2026-10-21 23:00 — 2026-10-22 01:00（北京时间）" in text


# ---------------------------------------------------------------------------
# pending=on_its_way 追加行
# ---------------------------------------------------------------------------
def test_on_its_way_appends_supplementary_line() -> None:
    event = _classifier().watch_candidates(
        forecast=_forecast("forecast_watch_on_its_way"), baseline=Baseline()
    )[0]
    assert event.payload["pending"] == "on_its_way"
    text = render_watch(event.payload, tz=SHANGHAI, settings=GroupSettings())
    assert "补充：网站尚未测得实际重置，确认待定。" in text
    assert "预计窗口：暂无明确预计时段" in text


def test_window_elapsed_is_suppressed_when_reset_landed_inside_the_window() -> None:
    """批次 C 语义修正：确认重置落在窗口内 → 预告已被履行，不得再发「未获确认」。"""
    previous = _active_event()
    now = utc(2026, 10, 21, 15, 0)
    corrections = _classifier(now).watch_corrections(
        forecast=_forecast("forecast_watch_active"),
        previous=previous,
        last_reset_at=utc(2026, 10, 21, 12, 30),  # 窗口 12:00Z–14:00Z 之内
        now=now,
    )
    assert corrections == []


def test_window_elapsed_is_suppressed_when_reset_equals_window_start() -> None:
    """边界：`last_reset_at == window.start_at` 视为窗口内确认，同样抑制更正。"""
    previous = _active_event()
    now = utc(2026, 10, 21, 15, 0)
    corrections = _classifier(now).watch_corrections(
        forecast=_forecast("forecast_watch_active"),
        previous=previous,
        last_reset_at=utc(2026, 10, 21, 12, 0),
        now=now,
    )
    assert corrections == []
