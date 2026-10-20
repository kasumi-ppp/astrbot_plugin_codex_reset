"""QA 独立回归 ⑥：字段缺失 / 枚举污染 / 恶意链接 / 大负载（AC-23）。

AC-23 原文：「网站字段缺失或出现新枚举值 → 安全降级、记录告警、插件不崩溃」。

本文件按三个层次断言：

1. **不崩溃**：任何畸形输入都不得抛异常（全部用例）；
2. **语义正确**：必填字段缺失必须 fail-closed（`schema_error`），可选字段缺失按
   D-03 / D-09 的裁定静默降级（不得误判为「已确认重置」或「可推送权益」）；
3. **记录告警**：所有「上游契约漂移」类问题必须进 `warnings`（AC-23）。

D-03 已裁定 `banked_state` 缺失视为 `unknown` 且「静默」入库，因此该字段**不**要求告警，
与 AC-23 冲突时以 `docs/PRD-confirmation.md` 的裁定为准。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from services.classifier import EventClassifier
from services.clock import FakeClock
from services.models import Baseline, PluginSettings
from services.notifications import SOURCE_LINE, render_credits, render_reset, render_watch
from services.parser import parse_feed, parse_forecast, parse_timeline, safe_url
from qa_helpers import (
    NOW,
    credits_row,
    event_row,
    feed_payload,
    forecast_payload,
    reset_alert,
    timeline_payload,
    watch_alert,
)

TZ = ZoneInfo("Asia/Shanghai")
SETTINGS = PluginSettings()


def _forecast(payload: dict[str, Any]):
    return parse_forecast(payload, fetched_at=NOW)


def _timeline(payload: dict[str, Any]):
    return parse_timeline(payload, fetched_at=NOW)


def _feed(payload: dict[str, Any]):
    return parse_feed(payload, fetched_at=NOW)


# ---------------------------------------------------------------------------
# 必填字段缺失 → schema_error（fail-closed，绝不落库半成品）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["updated_at"])
def test_forecast_required_field_missing_is_schema_error(field):
    """`updated_at` 是 OpenAPI 必填：缺失必须整体失败（否则基线会被错误推进）。"""
    payload = forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z")
    payload.pop(field)

    result = _forecast(payload)

    assert result.ok is False
    assert result.error_code == "schema_error"
    assert result.snapshot is None
    assert result.warnings, "失败必须记录告警"


@pytest.mark.parametrize("field", ["updated_at", "events"])
def test_timeline_required_field_missing_is_schema_error(field):
    """`/api/timeline` 的 `updated_at` / `events` 缺失必须整体失败。"""
    payload = timeline_payload("2026-10-20T11:55:00.000Z", [event_row("1")])
    payload.pop(field)

    result = _timeline(payload)

    assert result.ok is False
    assert result.error_code == "schema_error"
    assert result.warnings


@pytest.mark.parametrize("field", ["fetched_at"])
def test_feed_required_field_missing_is_schema_error(field):
    """`/api/feed` 的 `fetched_at` 缺失必须整体失败（陈旧判定依赖它）。"""
    payload = feed_payload()
    payload.pop(field)

    result = _feed(payload)

    assert result.ok is False
    assert result.error_code == "schema_error"
    assert result.warnings


# ---------------------------------------------------------------------------
# 可选/降级字段 → ok + warnings（或按裁定静默）
# ---------------------------------------------------------------------------
def test_forecast_missing_probabilities_degrades_to_zero_with_warning():
    """`probabilities` 缺失 → 概率按 0，但 `last_reset_at` 仍可用（不整体失败）。"""
    payload = forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z")
    payload.pop("probabilities")

    result = _forecast(payload)

    assert result.ok is True
    assert result.warnings
    assert result.snapshot is not None
    assert (result.snapshot.rounded_24h, result.snapshot.rounded_48h) == (0, 0)
    assert result.snapshot.last_reset_at == datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("54", 0), (None, 0), (150, 100), (-5, 0), (54.7, 0), (True, 0)],
)
def test_probability_values_are_clamped_or_zeroed_with_warning(raw, expected):
    """概率非整数/越界 → 收敛到 0-100 并告警，绝不抛异常。"""
    payload = forecast_payload()
    payload["probabilities"]["rounded_24h"] = raw

    result = _forecast(payload)

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.rounded_24h == expected
    assert result.warnings, f"{raw!r} 应当产生告警"


def test_forecast_missing_official_signal_and_confidence_is_tolerated():
    """`official_signal`/`confidence` 异常只降级，不影响 `last_reset_at` 的可用性。"""
    payload = forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z")
    payload["confidence"] = 123
    payload["official_signal"] = "not-an-object"

    result = _forecast(payload)

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.confidence == "unknown"
    assert result.snapshot.official_signal is None
    assert len(result.warnings) >= 2


def test_timeline_event_without_id_is_skipped_with_warning():
    """坏行（缺 id）必须跳过并告警，好行照常保留。"""
    bad = event_row("2100000000000000001")
    bad.pop("id")
    payload = timeline_payload("2026-10-20T11:55:00.000Z", [
        bad, event_row("2100000000000000002"),
        "not-an-object",
    ])

    result = _timeline(payload)

    assert result.ok is True
    assert result.snapshot is not None
    assert [event.id for event in result.snapshot.events] == ["2100000000000000002"]
    assert any("跳过" in warning for warning in result.warnings)


def test_event_missing_confirmed_warns_and_is_fail_closed():
    """`confirmed` 缺失 → 告警 + 视为 false（D-02 fail-closed），不得产生 reset 事件。"""
    row = event_row("2100000000000000003", announced_at="2026-10-20T06:33:48.000Z")
    row.pop("confirmed")

    parsed = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [row]))
    assert parsed.ok is True
    assert any("confirmed" in warning for warning in parsed.warnings)
    assert parsed.snapshot is not None
    assert parsed.snapshot.events[0].confirmed is False

    events = EventClassifier(clock=FakeClock(NOW)).reset_candidates(
        forecast=None, timeline=parsed.snapshot, feed=None, baseline=Baseline(built=True)
    )
    assert events == []


def test_event_missing_announcement_state_is_fail_closed_and_silent():
    """`announcement_state` 缺失（D-01 旧归档行）→ 零事件；按 D-01 不要求告警。"""
    row = event_row("2100000000000000004", announced_at="2026-10-20T06:33:48.000Z")
    row.pop("announcement_state")

    parsed = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [row]))

    assert parsed.ok is True
    assert parsed.snapshot is not None
    assert parsed.snapshot.events[0].announcement_state is None
    events = EventClassifier(clock=FakeClock(NOW)).reset_candidates(
        forecast=None, timeline=parsed.snapshot, feed=None, baseline=Baseline(built=True)
    )
    assert events == [], "D-01：缺失字段必须 fail-closed"


def test_event_illegal_announcement_state_type_is_reported():
    """`announcement_state` 类型污染（数字/列表）→ 告警 + 按缺失处理。"""
    row = event_row("2100000000000000005")
    row["announcement_state"] = 123
    list_row = event_row("2100000000000000006")
    list_row["announcement_state"] = ["announced"]

    parsed = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [row, list_row]))

    assert parsed.ok is True
    assert len([w for w in parsed.warnings if "announcement_state" in w]) == 2


def test_credits_missing_banked_state_is_silent_per_decision_record():
    """D-03 裁定：`banked_state` 缺失 → 视为 unknown，静默入库、不推送、不告警。"""
    row = credits_row("2100000000000000007", banked_state=None)

    parsed = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [row]))

    assert parsed.ok is True
    assert parsed.snapshot is not None
    assert parsed.snapshot.events[0].banked_state is None
    assert EventClassifier(clock=FakeClock(NOW)).credit_candidates(
        timeline=parsed.snapshot, feed=None
    ) == []


# ---------------------------------------------------------------------------
# 枚举污染
# ---------------------------------------------------------------------------
def test_unknown_enums_are_preserved_with_warnings_and_never_raise():
    """`group` / `confidence` / `kind` / `state` / `pending` / `stale` 全部污染。"""
    polluted_row = event_row("2100000000000000008")
    polluted_row["group"] = "weird"
    polluted_row["confidence"] = "ultra"

    alert = reset_alert()
    alert["kind"] = "turbo"
    alert["state"] = "maybe"
    alert["pending"] = "soon"

    forecast = _forecast(forecast_payload(alert=alert))
    timeline = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [polluted_row]))
    feed = _feed(feed_payload(stale="maybe"))

    assert forecast.ok is True and forecast.warnings
    assert forecast.snapshot is not None
    assert forecast.snapshot.latest_alert is not None
    assert forecast.snapshot.latest_alert.kind == "turbo"
    assert forecast.snapshot.latest_alert.pending is None, "未识别 pending 必须被忽略"

    assert timeline.ok is True
    assert timeline.snapshot is not None
    assert timeline.snapshot.events[0].group == "weird"
    assert any("group" in warning for warning in timeline.warnings)

    assert feed.ok is True
    assert feed.snapshot is not None
    assert feed.snapshot.stale is False
    assert feed.warnings

    # 污染后的 group 不得被当作 reset
    assert EventClassifier(clock=FakeClock(NOW)).reset_candidates(
        forecast=None, timeline=timeline.snapshot, feed=None, baseline=Baseline(built=True)
    ) == []


def test_latest_alert_missing_core_fields_is_dropped_with_warning():
    """`latest_alert` 缺 `id/kind/state` → 丢弃该 alert + 告警（不影响 forecast 主体）。"""
    alert = reset_alert()
    alert.pop("id")

    result = _forecast(forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z", alert=alert))

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.latest_alert is None
    assert any("latest_alert" in warning for warning in result.warnings)
    assert result.snapshot.last_reset_at is not None


def test_latest_alert_key_absence_should_be_reported():
    """AC-23：`latest_alert` 是 OpenAPI 必填字段，整键缺失应记录告警。"""
    payload = forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z")
    payload.pop("latest_alert")

    result = _forecast(payload)

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.latest_alert is None
    assert result.warnings, "必填字段缺失却没有告警，现场无法发现上游契约漂移"


# ---------------------------------------------------------------------------
# 恶意 / 畸形链接
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "http://",
        "https://exa mple.com/x",
        "ftp://codex-reset.com/file",
        "https://codex-reset.com/" + "a" * 3000,
        "",
        None,
        12345,
    ],
)
def test_unsafe_urls_are_dropped_without_crash(url):
    """`safe_url` 只接受 http/https 绝对地址；其余一律 None（AC-20 的省略语义）。"""
    assert safe_url(url) is None

    row = event_row("2100000000000000009", announced_at="2026-10-20T06:33:48.000Z", url=None)
    row["url"] = url
    parsed = _timeline(timeline_payload("2026-10-20T11:55:00.000Z", [row]))

    assert parsed.ok is True
    assert parsed.snapshot is not None
    assert parsed.snapshot.events[0].url is None


def test_render_omits_original_link_but_keeps_source_line():
    """无安全链接时必须省略「原始…」行，但署名行不可省（AC-20）。"""
    payload = {"occurred_at": "2026-10-20T06:33:48.000Z", "url": "javascript:alert(1)"}
    reset_text = render_reset(payload, tz=TZ, settings=SETTINGS)

    assert "原始公告" not in reset_text
    assert SOURCE_LINE in reset_text

    watch_text = render_watch({"window": None, "url": "ftp://x/y"}, tz=TZ, settings=SETTINGS)
    assert "原始消息" not in watch_text
    assert SOURCE_LINE in watch_text
    assert "预计窗口：暂无明确预计时段" in watch_text

    credits_text = render_credits(
        {"state": "available", "url": "ftp://x/y"}, tz=TZ, settings=SETTINGS
    )
    assert "原始来源" not in credits_text
    assert SOURCE_LINE in credits_text


def test_urls_with_whitespace_or_control_chars_should_be_omitted():
    """PRD §6.1「原始链接无效时省略该行」：含空格/换行的一律视为无效。

    含换行的链接还会把「数据来源」行重复写进消息体（文本注入面），
    因此渲染层必须与 `parser.safe_url` 用同一套判定。
    """
    spaced = render_watch(
        {"window": None, "url": "https://exa mple.com/x"}, tz=TZ, settings=SETTINGS
    )
    injected = render_reset(
        {"occurred_at": "2026-10-20T06:33:48.000Z", "url": "https://x.com/a\n数据来源：https://evil.example/"},
        tz=TZ,
        settings=SETTINGS,
    )

    assert "原始消息" not in spaced
    assert "原始公告" not in injected
    assert injected.count("数据来源：") == 1, "换行链接不得注入第二行署名"


# ---------------------------------------------------------------------------
# 大负载：1000 条事件必须在 1s 内解析 + 判定
# ---------------------------------------------------------------------------
def test_thousand_events_parse_and_classify_within_one_second():
    """AC-23 性能口径：1000 条合法事件解析 + 判定 < 1s，且不产生重复事件。"""
    rows = [
        event_row(
            f"21000000000000{index:05d}",
            announced_at=f"2026-09-{(index % 28) + 1:02d}T{(index % 24):02d}:00:00.000Z",
        )
        for index in range(1000)
    ]
    payload = timeline_payload("2026-10-20T11:55:00.000Z", rows)

    started = time.perf_counter()
    parsed = _timeline(payload)
    parsed_at = time.perf_counter()
    events = EventClassifier(clock=FakeClock(NOW)).reset_candidates(
        forecast=None, timeline=parsed.snapshot, feed=None, baseline=Baseline(built=True)
    )
    finished = time.perf_counter()

    assert parsed.ok is True
    assert parsed.snapshot is not None
    assert len(parsed.snapshot.events) == 1000
    assert len(events) == 1000
    assert parsed_at - started < 1.0, f"解析耗时过大：{parsed_at - started:.3f}s"
    assert finished - parsed_at < 1.0, f"判定耗时过大：{finished - parsed_at:.3f}s"


def test_garbage_payloads_never_raise():
    """彻底非法的 payload（None / 列表 / 字符串 / 空对象）一律不抛异常。"""
    for payload in (None, [], "text", 0, {}, {"unexpected": True}):
        for parser in (_forecast, _timeline, _feed):
            result = parser(payload)  # type: ignore[arg-type]
            assert result.ok is False
            assert result.error_code == "schema_error"
            assert result.warnings


def test_watch_alert_with_polluted_window_never_fabricates_hours():
    """窗口结构污染（start/end 无法解析）时宁可回退到 label 或固定句，不得自造小时数。"""
    alert = watch_alert()
    alert["window"] = {"label": "", "start_at": "not-a-time", "end_at": 12345}
    parsed = _forecast(forecast_payload(alert=alert))

    assert parsed.ok is True
    assert parsed.snapshot is not None
    assert parsed.snapshot.latest_alert is not None
    assert parsed.snapshot.latest_alert.window is None
    assert parsed.warnings
