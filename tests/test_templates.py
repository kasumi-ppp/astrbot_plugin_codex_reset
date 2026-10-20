"""AC-20 与模板冻结：署名行、F03 禁用词、时间格式、截断、更正文案。"""

from __future__ import annotations

from conftest import SHANGHAI, load_fixture, utc

from services.classifier import EventClassifier
from services.clock import FakeClock
from services.models import (
    Baseline,
    ForecastDecision,
    GroupSettings,
    LogicalEvent,
    PluginSettings,
)
from services.notifications import (
    MAX_MESSAGE_CHARS,
    SOURCE_LINE,
    TRUNCATED_SUFFIX,
    banked_state_cn,
    confidence_cn,
    format_day,
    format_time,
    render_catchup_summary,
    render_correction,
    render_credits,
    render_forecast_push,
    render_reset,
    render_test,
    render_watch,
)
from services.parser import parse_forecast, parse_timeline

SOURCE_URL = "https://codex-reset.com/"
SETTINGS = GroupSettings()
PLUGIN = PluginSettings()
NO_SOURCE = PluginSettings(show_source_link=False)


def _reset_payload(url: str | None = "https://x.com/thsottiaux/status/2112432076767289678") -> dict:
    return {
        "event_id": "2112432076767289678",
        "url": url,
        "summary": "reset",
        "occurred_at": "2026-10-20T06:33:48+00:00",
        "aliases": ["reset:2112432076767289678"],
    }


def _forecast_decision() -> ForecastDecision:
    return ForecastDecision(
        logical_id="forecast:sub:test:h24",
        state_version="h24:0:1",
        window_kind="h24",
        rounded_24h=54,
        rounded_48h=73,
        confidence="low",
        updated_at=utc(2026, 10, 20, 8, 0),
    )


def _all_renders() -> dict[str, str]:
    return {
        "reset": render_reset(_reset_payload(), tz=SHANGHAI, settings=SETTINGS),
        "watch": render_watch(
            {
                "state": "active",
                "window": None,
                "url": "https://x.com/thsottiaux/status/900",
                "pending": None,
            },
            tz=SHANGHAI,
            settings=SETTINGS,
        ),
        "forecast": render_forecast_push(_forecast_decision(), tz=SHANGHAI, settings=SETTINGS),
        "credits": render_credits(
            {"state": "announced", "date": "2026-09-03", "url": None}, tz=SHANGHAI, settings=SETTINGS
        ),
        "correction": render_correction(
            {"reason": "expired", "window": None, "url": None}, tz=SHANGHAI, settings=SETTINGS
        ),
        "catchup": render_catchup_summary(
            [
                LogicalEvent(
                    logical_id="reset:1",
                    kind="reset",
                    group_kind="reset",
                    state_version="confirmed",
                    occurred_at=utc(2026, 10, 20, 6, 33),
                    payload={},
                )
            ],
            tz=SHANGHAI,
            settings=SETTINGS,
        ),
        "test": render_test(tz=SHANGHAI, settings=SETTINGS),
    }


# ---------------------------------------------------------------------------
# AC-20：署名行（不可关闭）+ 原文行只在有安全链接时出现
# ---------------------------------------------------------------------------
def test_every_template_contains_source_line() -> None:
    renders = _all_renders()
    assert set(renders) == {"reset", "watch", "forecast", "credits", "correction", "catchup", "test"}
    for name, text in renders.items():
        assert SOURCE_LINE in text, name
        assert text.strip() != ""
        assert "@全体成员" not in text  # 模板永不写 @全体（由 main.py 用 At 组件实现）
        assert len(text) <= MAX_MESSAGE_CHARS, name


def test_source_line_cannot_be_disabled() -> None:
    for text in (
        render_reset(_reset_payload(), tz=SHANGHAI, settings=NO_SOURCE),
        render_watch({"state": "active", "window": None}, tz=SHANGHAI, settings=NO_SOURCE),
        render_forecast_push(_forecast_decision(), tz=SHANGHAI, settings=NO_SOURCE),
        render_credits({"state": "arriving"}, tz=SHANGHAI, settings=NO_SOURCE),
        render_correction({"reason": "corrected"}, tz=SHANGHAI, settings=NO_SOURCE),
        render_catchup_summary([], tz=SHANGHAI, settings=NO_SOURCE),
        render_test(tz=SHANGHAI, settings=NO_SOURCE),
    ):
        assert SOURCE_LINE in text
        assert SOURCE_URL in text


def test_missing_url_omits_original_line() -> None:
    reset_text = render_reset(_reset_payload(url=None), tz=SHANGHAI, settings=SETTINGS)
    assert "原始公告：" not in reset_text
    assert SOURCE_LINE in reset_text

    unsafe_text = render_reset(_reset_payload(url="javascript:alert(1)"), tz=SHANGHAI, settings=SETTINGS)
    assert "原始公告：" not in unsafe_text

    watch_text = render_watch(
        {"state": "active", "window": None, "url": None}, tz=SHANGHAI, settings=SETTINGS
    )
    assert "原始消息：" not in watch_text

    correction_text = render_correction({"reason": "expired", "url": ""}, tz=SHANGHAI, settings=SETTINGS)
    assert "原始消息：" not in correction_text


# ---------------------------------------------------------------------------
# 时间格式（D-07）
# ---------------------------------------------------------------------------
def test_format_time_and_day() -> None:
    moment = utc(2026, 10, 20, 6, 33, 48)
    assert format_time(moment, SHANGHAI) == "2026-10-20 14:33（北京时间）"
    assert format_time(moment, SHANGHAI, with_zone=False) == "2026-10-20 14:33"
    assert format_day(moment, SHANGHAI) == "2026-10-20"
    assert format_time(None, SHANGHAI) == ""
    assert format_day(None, SHANGHAI) == ""


def test_format_time_zone_names() -> None:
    from zoneinfo import ZoneInfo

    moment = utc(2026, 10, 20, 6, 33)
    assert format_time(moment, ZoneInfo("UTC")) == "2026-10-20 06:33（UTC）"
    assert format_time(moment, ZoneInfo("America/Los_Angeles")).endswith("（America/Los_Angeles）")


def test_reset_template_confirmation_time_is_local() -> None:
    text = render_reset(_reset_payload(), tz=SHANGHAI, settings=SETTINGS)
    assert "确认记录时间：2026-10-20 14:33（北京时间）" in text


# ---------------------------------------------------------------------------
# F01 / F03 措辞分离
# ---------------------------------------------------------------------------
def test_reset_template_contains_confirmation_wording() -> None:
    text = render_reset(_reset_payload(), tz=SHANGHAI, settings=SETTINGS)
    assert "✅【Codex 全球重置已确认】" in text
    assert "已确认" in text
    assert "已记录一次全球额度重置" in text
    assert "原始公告：https://x.com/thsottiaux/status/2112432076767289678" in text


def test_forecast_template_forbidden_words() -> None:
    for window_kind in ("h24", "h48", "both"):
        decision = _forecast_decision()
        decision = ForecastDecision(
            logical_id=decision.logical_id,
            state_version=f"{window_kind}:0:1",
            window_kind=window_kind,
            rounded_24h=54,
            rounded_48h=73,
            confidence="low",
            updated_at=decision.updated_at,
        )
        text = render_forecast_push(decision, tz=SHANGHAI, settings=SETTINGS)
        for forbidden in ("已确认", "即将重置", "必将"):
            assert forbidden not in text, (window_kind, forbidden)
        assert "实验性" in text
        assert "不代表官方承诺" in text
        assert "未来 24 小时：54%" in text
        assert "未来 48 小时：73%" in text


def test_forecast_titles_by_window_kind() -> None:
    base = _forecast_decision()
    for window_kind, title in (
        ("h24", "📈【Codex 重置概率升高】"),
        ("both", "📈【Codex 重置概率升高】"),
        ("h48", "📈【未来 48 小时重置预测】"),
    ):
        decision = ForecastDecision(
            logical_id=base.logical_id,
            state_version=base.state_version,
            window_kind=window_kind,
            rounded_24h=base.rounded_24h,
            rounded_48h=base.rounded_48h,
            confidence=base.confidence,
            updated_at=base.updated_at,
        )
        assert title in render_forecast_push(decision, tz=SHANGHAI, settings=SETTINGS)


# ---------------------------------------------------------------------------
# 映射函数
# ---------------------------------------------------------------------------
def test_confidence_mapping() -> None:
    assert confidence_cn("low") == "低"
    assert confidence_cn("medium") == "中"
    assert confidence_cn("high") == "高"
    assert confidence_cn("ultra") == "ultra（未识别置信度）"
    assert confidence_cn(None) == "未知"


def test_banked_state_mapping_is_verbatim() -> None:
    assert banked_state_cn("announced") == "已宣布（尚不能视为所有账号已到账）"
    assert banked_state_cn("arriving") == "正在发放（网站监测账户未全部到账）"
    assert banked_state_cn("available") == "网站记录该权益已可用（不代表所有账号可用）"
    assert banked_state_cn("unknown") == "unknown（未识别状态）"


# ---------------------------------------------------------------------------
# Watch / 更正模板
# ---------------------------------------------------------------------------
def test_watch_template_has_fixed_phrase_when_no_window() -> None:
    text = render_watch({"state": "active", "window": None}, tz=SHANGHAI, settings=SETTINGS)
    assert "预计窗口：暂无明确预计时段" in text
    assert "当前状态：监控站 Watch active" in text
    assert "已确认" not in text


def test_correction_templates() -> None:
    expired = render_correction(
        {
            "reason": "expired",
            "window": {
                "start_at": "2026-10-20T12:00:00+00:00",
                "end_at": "2026-10-20T14:00:00+00:00",
            },
            "url": "https://x.com/thsottiaux/status/900",
        },
        tz=SHANGHAI,
        settings=SETTINGS,
    )
    assert "🔁【更正】此前的重置预告未获确认" in expired
    assert "此前预告（20:00—22:00）已过期，截至现在未见确认重置记录。" in expired
    assert "原预告状态：已失效（expired）" in expired
    assert "原始消息：https://x.com/thsottiaux/status/900" in expired

    corrected = render_correction({"reason": "corrected"}, tz=SHANGHAI, settings=SETTINGS)
    assert "🔁【更正】此前的重置预告已被网站更正" in corrected
    assert "已失效（corrected）" in corrected

    downgraded = render_correction(
        {"reason": "downgraded", "state": "none", "url": None}, tz=SHANGHAI, settings=SETTINGS
    )
    assert "🔁【更正】此前记录的重置已被网站更新" in downgraded
    assert "网站已将此前记录状态调整为 none" in downgraded

    credits_reset = render_correction(
        {"reason": "credits_state", "state": "announced"}, tz=SHANGHAI, settings=SETTINGS
    )
    assert "🔁【更正】额外重置权益状态已更新为 已宣布（尚不能视为所有账号已到账）" in credits_reset


def test_correction_without_window_does_not_invent_times() -> None:
    text = render_correction({"reason": "window_elapsed", "window": None}, tz=SHANGHAI, settings=SETTINGS)
    assert "此前预告已过期" in text
    assert "今日" not in text
    assert "暂无明确预计时段" not in text


# ---------------------------------------------------------------------------
# 补发摘要 / 测试消息 / 截断
# ---------------------------------------------------------------------------
def test_catchup_summary_lists_events() -> None:
    events = [
        LogicalEvent(
            logical_id="reset:1",
            kind="reset",
            group_kind="reset",
            state_version="confirmed",
            occurred_at=utc(2026, 10, 20, 6, 33),
            payload={},
        ),
        LogicalEvent(
            logical_id="credits:2",
            kind="credits",
            group_kind="credits",
            state_version="available",
            occurred_at=utc(2026, 10, 20, 7, 0),
            payload={"state": "available"},
        ),
    ]
    text = render_catchup_summary(events, tz=SHANGHAI, settings=PLUGIN)
    assert "📌【补发摘要】共 2 条更新" in text
    assert "Codex 全球重置（网站确认记录）" in text
    assert "额外重置权益：网站记录该权益已可用（不代表所有账号可用）" in text
    assert "6 小时" in text  # max_catchup_hours 默认值来自 PluginSettings


def test_catchup_summary_is_truncated_beyond_limit() -> None:
    events = [
        LogicalEvent(
            logical_id=f"reset:{index}",
            kind="reset",
            group_kind="reset",
            state_version="confirmed",
            occurred_at=utc(2026, 10, 20, 6, index % 60),
            payload={},
        )
        for index in range(60)
    ]
    text = render_catchup_summary(events, tz=SHANGHAI, settings=PLUGIN)
    assert len(text) <= MAX_MESSAGE_CHARS
    assert text.endswith(TRUNCATED_SUFFIX)


def test_test_template_is_labelled_as_sample() -> None:
    text = render_test(tz=SHANGHAI, settings=SETTINGS)
    assert "🧪【测试消息】以下为样例，非真实事件" in text
    assert "确认记录时间：2026-01-01 00:00（北京时间）" in text
    assert "本次为 /cr test 生成，不改变任何真实事件状态。" in text
    assert SOURCE_LINE in text


# ---------------------------------------------------------------------------
# 与真实 fixture 串起来：分类器 → 模板（端到端文本）
# ---------------------------------------------------------------------------
def test_real_fixture_reset_renders_from_classifier_payload() -> None:
    timeline = parse_timeline(
        load_fixture("timeline_current"), fetched_at=utc(2026, 10, 20, 10, 20)
    ).snapshot
    assert timeline is not None
    events = EventClassifier(clock=FakeClock(utc(2026, 10, 20, 10, 20))).reset_candidates(
        forecast=None, timeline=timeline, feed=None, baseline=Baseline()
    )
    target = next(event for event in events if event.payload["event_id"] == "2112432076767289678")
    text = render_reset(target.payload, tz=SHANGHAI, settings=SETTINGS)
    assert "✅【Codex 全球重置已确认】" in text
    assert "确认记录时间：2026-10-20 14:33（北京时间）" in text
    assert "原始公告：https://x.com/thsottiaux/status/2112432076767289678" in text


def test_real_watch_fixture_renders_window() -> None:
    forecast = parse_forecast(
        load_fixture("forecast_watch_active"), fetched_at=utc(2026, 10, 20, 12, 0)
    ).snapshot
    assert forecast is not None
    events = EventClassifier(clock=FakeClock(utc(2026, 10, 20, 12, 0))).watch_candidates(
        forecast=forecast, baseline=Baseline()
    )
    text = render_watch(events[0].payload, tz=SHANGHAI, settings=SETTINGS)
    assert "预计窗口：今日 20:00—22:00（北京时间）" in text


def test_no_template_uses_utc_datetime_naively() -> None:
    """渲染层只能出现中文时区名，不允许裸 UTC 偏移量（D-07 单一转换点）。"""
    for text in _all_renders().values():
        assert "+00:00" not in text
        assert "Z " not in text
