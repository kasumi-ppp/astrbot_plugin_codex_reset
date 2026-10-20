"""解析层测试：三端点正常路径 + 逐字段降级（AC-23）+ 安全链接 + 时间解析。"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

from conftest import load_fixture, load_fixture_text, utc

from services.errors import ErrorCode
from services.parser import (
    coerce_enum,
    group_is_known,
    parse_event,
    parse_feed,
    parse_forecast,
    parse_iso,
    parse_latest_alert,
    parse_timeline,
    safe_url,
)

FETCHED_AT = utc(2026, 10, 20, 10, 20)


def _forecast_payload() -> dict[str, Any]:
    return copy.deepcopy(load_fixture("forecast_current"))


def _timeline_payload() -> dict[str, Any]:
    return copy.deepcopy(load_fixture("timeline_current"))


def _feed_payload() -> dict[str, Any]:
    return copy.deepcopy(load_fixture("feed_current"))


# ---------------------------------------------------------------------------
# parse_iso
# ---------------------------------------------------------------------------
def test_parse_iso_supports_z_offset_microseconds_and_naive() -> None:
    assert parse_iso("2026-10-20T06:33:48.000Z") == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)
    assert parse_iso("2026-10-20T14:33:48+08:00") == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)
    assert parse_iso("2026-10-20T06:33:48.123456Z") == datetime(
        2026, 10, 20, 6, 33, 48, 123456, tzinfo=UTC
    )
    # naive 视为 UTC（D-07）
    assert parse_iso("2026-10-20T06:33:48") == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)


def test_parse_iso_returns_none_for_invalid_values() -> None:
    for value in (None, "", "not-a-date", 12345, {}, "2026-13-40T99:99:99Z"):
        assert parse_iso(value) is None


# ---------------------------------------------------------------------------
# safe_url / coerce_enum
# ---------------------------------------------------------------------------
def test_safe_url_accepts_http_and_https_only() -> None:
    assert safe_url("https://x.com/thsottiaux/status/1") == "https://x.com/thsottiaux/status/1"
    assert safe_url("http://codex-reset.com/a") == "http://codex-reset.com/a"


def test_safe_url_rejects_pseudo_schemes_whitespace_and_long_values() -> None:
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("data:text/html;base64,AAAA") is None
    assert safe_url("/relative/path") is None
    assert safe_url("") is None
    assert safe_url(None) is None
    assert safe_url("https://x.com/a b") is None
    assert safe_url("https://x.com/\x00evil") is None
    long_url = "https://x.com/" + "a" * 2100
    assert safe_url(long_url) is None


def test_coerce_enum_reports_known_state() -> None:
    assert coerce_enum("reset", {"reset", "credits"}, None) == ("reset", True)
    assert coerce_enum("weird", {"reset", "credits"}, None) == ("weird", False)
    assert coerce_enum(None, {"reset"}, "reset") == ("reset", False)
    assert group_is_known("credits") is True
    assert group_is_known("weird") is False


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------
def test_parse_forecast_current_sample() -> None:
    result = parse_forecast(_forecast_payload(), fetched_at=FETCHED_AT, batch="1792491580134")
    assert result.ok is True
    assert result.error_code == ErrorCode.OK
    assert result.warnings == ()
    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.rounded_24h == 22
    assert snapshot.rounded_48h == 39
    assert snapshot.confidence == "low"
    assert snapshot.updated_at == datetime(2026, 10, 20, 10, 19, 48, 712000, tzinfo=UTC)
    assert snapshot.last_reset_at == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)
    assert snapshot.batch == "1792491580134"
    assert snapshot.latest_alert is not None
    assert snapshot.latest_alert.id == "2112432076767289678"
    assert snapshot.latest_alert.kind == "reset"
    assert snapshot.latest_alert.state == "confirmed"
    assert snapshot.latest_alert.url == "https://x.com/thsottiaux/status/2112432076767289678"


def test_parse_forecast_invalid_updated_at_fails_closed() -> None:
    payload = _forecast_payload()
    payload["updated_at"] = "yesterday"
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is False
    assert result.error_code == ErrorCode.SCHEMA_ERROR
    assert result.snapshot is None


def test_parse_forecast_missing_probabilities_degrades_but_keeps_last_reset_at() -> None:
    payload = _forecast_payload()
    del payload["probabilities"]
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    snapshot = result.snapshot
    assert snapshot is not None
    assert (snapshot.rounded_24h, snapshot.rounded_48h) == (0, 0)
    # AC-03 依赖 last_reset_at，概率缺失不得整体失败
    assert snapshot.last_reset_at == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)
    assert any("probabilities" in warning for warning in result.warnings)


def test_parse_forecast_non_int_probabilities_become_zero() -> None:
    payload = _forecast_payload()
    payload["probabilities"] = {"rounded_24h": "high", "rounded_48h": 73.5}
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert (result.snapshot.rounded_24h, result.snapshot.rounded_48h) == (0, 0)
    assert len(result.warnings) >= 2


def test_parse_forecast_unknown_confidence_is_preserved() -> None:
    payload = _forecast_payload()
    payload["confidence"] = "ultra"
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.confidence == "ultra"
    assert any("ultra" in warning for warning in result.warnings)


def test_parse_forecast_alert_without_id_is_discarded() -> None:
    payload = _forecast_payload()
    del payload["latest_alert"]["id"]
    result = parse_forecast(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.latest_alert is None
    assert any("latest_alert" in warning for warning in result.warnings)


def test_parse_latest_alert_rejects_non_mapping() -> None:
    alert, warnings = parse_latest_alert("nope")
    assert alert is None
    assert warnings


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------
def test_parse_timeline_current_slice() -> None:
    result = parse_timeline(_timeline_payload(), fetched_at=FETCHED_AT)
    assert result.ok is True
    snapshot = result.snapshot
    assert snapshot is not None
    assert len(snapshot.events) == 12
    by_id = {event.id: event for event in snapshot.events}

    confirmed = by_id["2112432076767289678"]
    assert (confirmed.group, confirmed.announcement_state, confirmed.confirmed) == (
        "reset",
        "announced",
        True,
    )
    none_state = by_id["2111492673890824348"]
    assert none_state.announcement_state == "none"
    assert none_state.confirmed is False
    trap = by_id["2109045392684032299"]
    assert trap.announcement_state == "announced"
    assert trap.confirmed is False  # D-02：只判 state 会误报

    # 归档行缺 announcement_state → None（fail-closed，D-01）
    archive_missing = [event for event in snapshot.events if event.announcement_state is None]
    assert archive_missing, "fixture 应保留至少 1 条缺 announcement_state 的归档行"

    credits = by_id["2095651088502591861"]
    assert credits.banked_state == "announced"
    assert credits.banked_grant is True


def test_parse_timeline_credits_flow_fixture() -> None:
    payload = load_fixture("timeline_credits_flow")
    result = parse_timeline(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    states = [event.banked_state for event in result.snapshot.events]
    assert states == ["announced", "available"]


def test_parse_timeline_events_not_a_list_fails_closed() -> None:
    payload = _timeline_payload()
    payload["events"] = {"id": "1"}
    result = parse_timeline(payload, fetched_at=FETCHED_AT)
    assert result.ok is False
    assert result.error_code == ErrorCode.SCHEMA_ERROR


def test_parse_timeline_skips_events_without_id() -> None:
    payload = _timeline_payload()
    broken = copy.deepcopy(payload["events"][0])
    del broken["id"]
    payload["events"].append(broken)
    result = parse_timeline(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert len(result.snapshot.events) == 12  # 无效行被跳过
    assert any("id" in warning for warning in result.warnings)


def test_parse_event_missing_required_fields_never_raises() -> None:
    base = _timeline_payload()["events"][0]
    for field in ("date", "type", "group", "summary", "announced_at", "confirmed", "url"):
        row = copy.deepcopy(base)
        row.pop(field, None)
        event, _warnings = parse_event(row)
        assert event is not None, f"缺 {field} 不应丢弃整条事件"
    empty: dict[str, Any] = {}
    event, warnings = parse_event(empty)
    assert event is None
    assert warnings


def test_parse_event_unknown_group_is_preserved_but_flagged() -> None:
    payload = load_fixture("corrupt_enum")
    result = parse_timeline(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    event = result.snapshot.events[0]
    assert event.group == "weird"  # 保留原值，不当作 reset
    assert event.banked_state is None  # quantum → None
    assert event.url is None  # javascript: → 清空
    assert any("group_known=False" in warning for warning in result.warnings)


def test_parse_event_confirmed_missing_fails_closed() -> None:
    row = copy.deepcopy(_timeline_payload()["events"][0])
    row.pop("confirmed")
    event, warnings = parse_event(row)
    assert event is not None
    assert event.confirmed is False
    assert any("confirmed" in warning for warning in warnings)


def test_parse_timeline_updated_at_invalid_fails_closed() -> None:
    payload = _timeline_payload()
    payload["updated_at"] = 0
    result = parse_timeline(payload, fetched_at=FETCHED_AT)
    assert result.ok is False
    assert result.error_code == ErrorCode.SCHEMA_ERROR


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------
def test_parse_feed_current_sample() -> None:
    result = parse_feed(_feed_payload(), fetched_at=FETCHED_AT)
    assert result.ok is True
    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.stale is False
    assert len(snapshot.tweets) == 3
    assert len(snapshot.events) == 5
    assert snapshot.newest_post_at == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)
    observation = next(item for item in snapshot.banked_observations if item.id == "2105008012525769969")
    assert observation.after_announcement is False  # D-10：观测事实，非公告


def test_parse_feed_invalid_fetched_at_fails_closed() -> None:
    payload = _feed_payload()
    payload["fetched_at"] = "soon"
    result = parse_feed(payload, fetched_at=FETCHED_AT)
    assert result.ok is False
    assert result.error_code == ErrorCode.SCHEMA_ERROR


def test_parse_feed_non_bool_stale_becomes_false_with_warning() -> None:
    payload = _feed_payload()
    payload["stale"] = "maybe"
    result = parse_feed(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.stale is False
    assert any("stale" in warning for warning in result.warnings)


def test_parse_feed_skips_banked_observation_without_id() -> None:
    payload = _feed_payload()
    payload["banked_observations"] = [{"kind": "banked"}]
    result = parse_feed(payload, fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.banked_observations == ()


# ---------------------------------------------------------------------------
# 坏输入：非 dict / 非法 JSON
# ---------------------------------------------------------------------------
def test_non_mapping_payloads_return_schema_error_for_all_endpoints() -> None:
    for parser in (parse_forecast, parse_timeline, parse_feed):
        result = parser(["not", "a", "mapping"], fetched_at=FETCHED_AT)
        assert result.ok is False
        assert result.error_code == ErrorCode.SCHEMA_ERROR
        assert result.snapshot is None
        assert result.warnings


def test_broken_json_text_is_rejected_before_parsing() -> None:
    text = load_fixture_text("corrupt_invalid")
    try:
        json.loads(text)
    except json.JSONDecodeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("corrupt_invalid.json 必须是非法 JSON")

    # client 层判 invalid_json；parser 层收到原始字符串则 schema_error，均不抛异常
    result = parse_timeline(text, fetched_at=FETCHED_AT)
    assert result.ok is False
    assert result.error_code == ErrorCode.SCHEMA_ERROR
