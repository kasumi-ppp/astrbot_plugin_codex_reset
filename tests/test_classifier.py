"""AC-01 / AC-02：reset 确认判定、陷阱行零事件、id 与版本生成。"""

from __future__ import annotations

from conftest import load_fixture, utc

from services.classifier import (
    EventClassifier,
    build_logical_id,
    build_state_version,
)
from services.clock import FakeClock
from services.models import Baseline, ForecastSnapshot, TimelineSnapshot
from services.parser import parse_feed, parse_forecast, parse_timeline

FETCHED_AT = utc(2026, 10, 20, 10, 20)


def _timeline(name: str) -> TimelineSnapshot:
    result = parse_timeline(load_fixture(name), fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    return result.snapshot


def _forecast(name: str) -> ForecastSnapshot:
    result = parse_forecast(load_fixture(name), fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    return result.snapshot


def _classifier(clock: FakeClock | None = None) -> EventClassifier:
    return EventClassifier(clock=clock or FakeClock(FETCHED_AT))


def _alert_payload(*, corrected: bool, state: str = "confirmed", kind: str = "reset") -> dict:
    """构造一份带 `latest_alert` 的 forecast 原始 JSON（字段名与 OpenAPI 一致）。"""
    return {
        "mode": "model",
        "updated_at": "2026-10-20T10:15:00Z",
        "probabilities": {"rounded_24h": 20, "rounded_48h": 30},
        "confidence": "low",
        "last_reset_at": None,
        "official_signal": None,
        "latest_alert": {
            "id": "2112432076767289678",
            "kind": kind,
            "state": state,
            "summary": "reset",
            "source_at": "2026-10-20T06:33:48Z",
            "url": "https://x.com/thsottiaux/status/2112432076767289678",
            "corrected": corrected,
        },
    }


def test_corrected_reset_alert_is_not_a_new_confirmation() -> None:
    """A / F07：`latest_alert.corrected=true` 不得再当作新确认（网站已更正该条 alert）。"""
    classifier = _classifier()
    baseline = Baseline(built=True, last_reset_at=utc(2026, 10, 20, 6, 0))

    def candidates(corrected: bool) -> list:
        result = parse_forecast(
            _alert_payload(corrected=corrected), fetched_at=FETCHED_AT
        )
        assert result.ok is True and result.snapshot is not None
        assert result.snapshot.latest_alert is not None
        return classifier.reset_candidates(
            forecast=result.snapshot, timeline=None, feed=None, baseline=baseline
        )

    assert candidates(corrected=False), "未被更正的 reset/confirmed alert 仍应产出候选"
    assert candidates(corrected=True) == [], "被更正的 alert 不得产生任何 reset 候选"

    result = parse_forecast(_alert_payload(corrected=True), fetched_at=FETCHED_AT)
    assert result.snapshot is not None
    assert (
        EventClassifier._is_confirmed_reset_alert(result.snapshot.latest_alert) is False
    )


# ---------------------------------------------------------------------------
# id / 版本生成规则（架构 §C.3）
# ---------------------------------------------------------------------------
def test_build_logical_id_and_state_version_rules() -> None:
    anchor = utc(2026, 10, 20, 6, 33, 48)
    assert build_logical_id("reset", event_id="2112") == "reset:2112"
    assert build_logical_id("reset", alert_id="2112") == "reset:2112"
    assert build_logical_id("reset", anchor_at=anchor) == "reset:ts:2026-10-20T06:33:48+00:00"
    assert build_logical_id("watch", alert_id="900") == "watch:900"
    assert build_logical_id("credits", event_id="2095") == "credits:2095"
    assert build_logical_id("forecast", window_kind="both") == "forecast:both"

    assert build_state_version("reset") == "confirmed"
    assert build_state_version("watch", state="active", window_start_at=None) == "active:nowindow"
    assert (
        build_state_version("watch", state="active", window_start_at=utc(2026, 10, 21, 12))
        == "active:2026-10-21T12:00:00+00:00"
    )
    assert build_state_version("credits", banked_state="available") == "available"
    assert build_state_version("credits", banked_state="unknown") is None
    assert build_state_version("credits", banked_state=None) is None
    # 改动 3：`{window_kind}:{epoch_seq}:{alert_seq}`，当日第 2 条不再被 UNIQUE 吞掉
    assert build_state_version("forecast", window_kind="h24", epoch_seq=0, alert_seq=1) == "h24:0:1"
    assert build_state_version("forecast", window_kind="both", epoch_seq=2, alert_seq=3) == "both:2:3"
    assert build_state_version("forecast") is None

    # 更正版本
    assert build_state_version("watch", reason="expired", window_start_at=None) == "expired:nowindow"
    assert build_state_version("watch", reason="expired", window_start_at=anchor) == (
        "expired:2026-10-20T06:33:48+00:00"
    )
    assert build_state_version("watch", reason="corrected") == "corrected"
    assert build_state_version("watch", reason="window_elapsed", window_start_at=anchor) == (
        "expired_window:2026-10-20T06:33:48+00:00"
    )
    assert build_state_version("reset", reason="downgraded", state="none") == "downgraded:none"


# ---------------------------------------------------------------------------
# AC-01：确认行 → 1 条事件
# ---------------------------------------------------------------------------
def test_announced_confirmed_row_yields_single_event() -> None:
    payload = load_fixture("timeline_current")
    target = {
        "id": "2112432076767289678",
        "date": "2026-10-20",
        "type": "reset",
        "group": "reset",
        "summary": "Day 15/ Nevermind, we're still cooking... and reset. Enjoy.",
        "url": "https://x.com/thsottiaux/status/2112432076767289678",
        "announced_at": "2026-10-20T06:33:48.000Z",
        "preview": False,
        "announcement_state": "announced",
        "confirmed": True,
    }
    timeline = parse_timeline(
        {"updated_at": payload["updated_at"], "events": [target]}, fetched_at=FETCHED_AT
    ).snapshot
    assert isinstance(timeline, TimelineSnapshot)

    events = _classifier().reset_candidates(
        forecast=None, timeline=timeline, feed=None, baseline=Baseline()
    )
    assert len(events) == 1
    event = events[0]
    assert event.logical_id == "reset:2112432076767289678"
    assert event.state_version == "confirmed"
    assert event.group_kind == "reset"
    assert event.occurred_at == utc(2026, 10, 20, 6, 33, 48)
    # 别名：正式 id + url + 时间键（时间键用于临时键 → 正式 id 的迁移）
    assert event.payload["aliases"] == [
        "reset:2112432076767289678",
        "reset:url:https://x.com/thsottiaux/status/2112432076767289678",
        "reset:ts:2026-10-20T06:33:48+00:00",
    ]
    assert event.payload["via"] == "timeline"
    assert event.payload["event_id"] == "2112432076767289678"


def test_preview_row_is_never_a_confirmed_reset() -> None:
    timeline = parse_timeline(
        {
            "updated_at": "2026-10-20T10:19:48.808Z",
            "events": [
                {
                    "id": "2112432076767289678",
                    "date": "2026-10-20",
                    "group": "reset",
                    "summary": "preview only",
                    "announced_at": "2026-10-20T06:33:48.000Z",
                    "preview": True,
                    "announcement_state": "announced",
                    "confirmed": True,
                }
            ],
        },
        fetched_at=FETCHED_AT,
    ).snapshot
    assert isinstance(timeline, TimelineSnapshot)
    assert (
        _classifier().reset_candidates(
            forecast=None, timeline=timeline, feed=None, baseline=Baseline()
        )
        == []
    )


# ---------------------------------------------------------------------------
# AC-02：陷阱行与 none 行零候选
# ---------------------------------------------------------------------------
def test_trap_row_announced_but_unconfirmed_yields_zero() -> None:
    timeline = _timeline("timeline_reset_trap")
    assert timeline.events[0].announcement_state == "announced"
    assert timeline.events[0].confirmed is False
    assert (
        _classifier().reset_candidates(
            forecast=None, timeline=timeline, feed=None, baseline=Baseline()
        )
        == []
    )


def test_none_row_yields_zero() -> None:
    timeline = _timeline("timeline_reset_none")
    assert timeline.events[0].announcement_state == "none"
    assert (
        _classifier().reset_candidates(
            forecast=None, timeline=timeline, feed=None, baseline=Baseline()
        )
        == []
    )


def test_full_timeline_skips_trap_and_none_rows() -> None:
    timeline = _timeline("timeline_current")
    events = _classifier().reset_candidates(
        forecast=None, timeline=timeline, feed=None, baseline=Baseline()
    )
    ids = {event.payload["event_id"] for event in events}
    assert "2112432076767289678" in ids
    assert "2109045392684032299" not in ids  # announced + confirmed=false
    assert "2111492673890824348" not in ids  # announcement_state=none
    assert all(event.state_version == "confirmed" for event in events)
    # 同轮去重：每个 logical_id 只出现一次
    assert len({event.logical_id for event in events}) == len(events)


# ---------------------------------------------------------------------------
# 三端点归并（AC-03 / AC-04 的分类器侧）
# ---------------------------------------------------------------------------
def test_latest_alert_confirmed_merges_with_timeline_row() -> None:
    forecast = _forecast("forecast_current")  # latest_alert = reset/confirmed/2112432076767289678
    timeline = _timeline("timeline_current")
    events = _classifier().reset_candidates(
        forecast=forecast, timeline=timeline, feed=None, baseline=Baseline()
    )
    same = [event for event in events if event.payload["event_id"] == "2112432076767289678"]
    assert len(same) == 1  # D-08：同源 id 归并为一条
    assert same[0].logical_id == "reset:2112432076767289678"
    assert same[0].payload["aliases"][0] == "reset:2112432076767289678"


def test_last_reset_at_advance_creates_temporary_key() -> None:
    forecast = ForecastSnapshot(
        updated_at=utc(2026, 10, 20, 12, 0),
        rounded_24h=22,
        rounded_48h=39,
        confidence="low",
        last_reset_at=utc(2026, 10, 20, 11, 30),
        latest_alert=None,
    )
    baseline = Baseline(built=True, last_reset_at=utc(2026, 10, 20, 6, 33, 48))
    events = _classifier().reset_candidates(
        forecast=forecast, timeline=None, feed=None, baseline=baseline
    )
    assert len(events) == 1
    assert events[0].logical_id == "reset:ts:2026-10-20T11:30:00+00:00"
    assert events[0].state_version == "confirmed"
    assert events[0].payload["aliases"] == ["reset:ts:2026-10-20T11:30:00+00:00"]


def test_last_reset_at_without_baseline_does_not_notify() -> None:
    forecast = ForecastSnapshot(
        updated_at=utc(2026, 10, 20, 12, 0),
        rounded_24h=22,
        rounded_48h=39,
        confidence="low",
        last_reset_at=utc(2026, 10, 20, 11, 30),
        latest_alert=None,
    )
    assert (
        _classifier().reset_candidates(
            forecast=forecast, timeline=None, feed=None, baseline=Baseline(built=False)
        )
        == []
    )


def test_last_reset_at_matching_row_within_tolerance_is_not_duplicated() -> None:
    forecast = _forecast("forecast_current")
    baseline = Baseline(built=True, last_reset_at=utc(2026, 10, 20, 6, 0))
    events = _classifier().reset_candidates(
        forecast=forecast, timeline=_timeline("timeline_current"), feed=None, baseline=baseline
    )
    assert not any(event.logical_id.startswith("reset:ts:") for event in events)


def test_feed_reset_row_is_classified_with_feed_via() -> None:
    feed_payload = {
        "fetched_at": "2026-10-20T10:19:48.808Z",
        "stale": False,
        "events": [
            {
                "id": "2112432076767289678",
                "date": "2026-10-20",
                "group": "reset",
                "summary": "reset confirmed",
                "announced_at": "2026-10-20T06:33:48.000Z",
                "preview": False,
                "announcement_state": "announced",
                "confirmed": True,
            }
        ],
    }
    feed = parse_feed(feed_payload).snapshot
    assert feed is not None
    events = _classifier().reset_candidates(
        forecast=None, timeline=None, feed=feed, baseline=Baseline()
    )
    assert len(events) == 1
    assert events[0].payload["via"] == "feed"


# ---------------------------------------------------------------------------
# reset 更正（降级）
# ---------------------------------------------------------------------------
def test_reset_correction_on_downgrade() -> None:
    previous = _classifier().reset_candidates(
        forecast=None,
        timeline=parse_timeline(
            {
                "updated_at": "2026-10-20T10:19:48.808Z",
                "events": [
                    {
                        "id": "2112432076767289678",
                        "date": "2026-10-20",
                        "group": "reset",
                        "summary": "reset confirmed",
                        "announced_at": "2026-10-20T06:33:48.000Z",
                        "preview": False,
                        "announcement_state": "announced",
                        "confirmed": True,
                    }
                ],
            },
            fetched_at=FETCHED_AT,
        ).snapshot,
        feed=None,
        baseline=Baseline(),
    )[0]

    downgraded = parse_timeline(
        {
            "updated_at": "2026-10-20T11:19:48.808Z",
            "events": [
                {
                    "id": "2112432076767289678",
                    "date": "2026-10-20",
                    "group": "reset",
                    "summary": "reset downgraded",
                    "announced_at": "2026-10-20T06:33:48.000Z",
                    "preview": False,
                    "announcement_state": "none",
                    "confirmed": False,
                }
            ],
        },
        fetched_at=FETCHED_AT,
    ).snapshot
    assert isinstance(downgraded, TimelineSnapshot)

    corrections = _classifier().reset_corrections(timeline=downgraded, previous=previous)
    assert len(corrections) == 1
    assert corrections[0].logical_id == previous.logical_id
    assert corrections[0].state_version == "downgraded:none"
    assert corrections[0].correction_reason == "downgraded"
    assert corrections[0].state_version != previous.state_version


def test_reset_correction_ignores_missing_row() -> None:
    previous = _classifier().reset_candidates(
        forecast=None, timeline=_timeline("timeline_current"), feed=None, baseline=Baseline()
    )[0]
    empty = TimelineSnapshot(updated_at=utc(2026, 10, 20, 11), events=())
    assert _classifier().reset_corrections(timeline=empty, previous=previous) == []


def test_reset_correction_keeps_state_when_row_unchanged() -> None:
    timeline = _timeline("timeline_current")
    previous = _classifier().reset_candidates(
        forecast=None, timeline=timeline, feed=None, baseline=Baseline()
    )[0]
    assert _classifier().reset_corrections(timeline=timeline, previous=previous) == []


def test_classifier_uses_clock_for_missing_timestamps() -> None:
    clock = FakeClock(utc(2026, 10, 20, 12, 0))
    classifier = EventClassifier(clock=clock)
    timeline = parse_timeline(
        {
            "updated_at": "2026-10-20T10:19:48.808Z",
            "events": [
                {
                    "id": "1",
                    "date": "2026-10-20",
                    "group": "reset",
                    "summary": "",
                    "preview": False,
                    "announcement_state": "announced",
                    "confirmed": True,
                }
            ],
        },
        fetched_at=FETCHED_AT,
    ).snapshot
    assert isinstance(timeline, TimelineSnapshot)
    events = classifier.reset_candidates(
        forecast=None, timeline=timeline, feed=None, baseline=Baseline()
    )
    assert events[0].occurred_at == utc(2026, 10, 20, 0, 0)  # date 00:00 UTC 优先


def test_classifier_is_pure_no_repository_import() -> None:
    """纯函数约束（架构 §A.1）：分类器不得 import repository / pipeline。"""
    import inspect

    from services import classifier as classifier_module

    source = inspect.getsource(classifier_module)
    assert "from .repository" not in source
    assert "from .pipeline" not in source
    assert "import repository" not in source


def test_stale_feed_fixture_parses_and_yields_no_candidates() -> None:
    """`feed.stale=true` 本身由 pipeline 抑制；分类器只需保证不误报。"""
    feed = parse_feed(load_fixture("feed_stale")).snapshot
    assert feed is not None
    assert feed.stale is True
    classifier = _classifier()
    assert (
        classifier.reset_candidates(
            forecast=None, timeline=None, feed=feed, baseline=Baseline()
        )
        == []
    )
    assert classifier.credit_candidates(timeline=None, feed=feed) == []
