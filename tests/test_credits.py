"""AC-12：额外重置权益（credits）分类、状态版本与冻结文案。"""

from __future__ import annotations

from typing import Any

from conftest import SHANGHAI, load_fixture, utc

from services.classifier import EventClassifier
from services.clock import FakeClock
from services.models import FeedSnapshot, GroupSettings, TimelineSnapshot
from services.notifications import banked_state_cn, render_credits
from services.parser import parse_feed, parse_timeline

FETCHED_AT = utc(2026, 10, 20, 10, 20)
ANNOUNCED_ID = "2095651088502591861"
AVAILABLE_ID = "2107975486798164736"


def _timeline(name: str = "timeline_credits_flow") -> TimelineSnapshot:
    result = parse_timeline(load_fixture(name), fetched_at=FETCHED_AT)
    assert result.ok is True
    assert result.snapshot is not None
    return result.snapshot


def _feed(payload: dict[str, Any]) -> FeedSnapshot:
    result = parse_feed(payload)
    assert result.ok is True
    assert result.snapshot is not None
    return result.snapshot


def _classifier() -> EventClassifier:
    return EventClassifier(clock=FakeClock(FETCHED_AT))


# ---------------------------------------------------------------------------
# AC-12：announced → available 各 1 条，且不进入 reset 类别
# ---------------------------------------------------------------------------
def test_credits_announced_then_available_yield_two_versions() -> None:
    events = _classifier().credit_candidates(timeline=_timeline(), feed=None)
    by_id = {event.payload["event_id"]: event for event in events}

    announced = by_id[ANNOUNCED_ID]
    assert announced.logical_id == f"credits:{ANNOUNCED_ID}"
    assert announced.kind == "credits"
    assert announced.group_kind == "credits"  # 不得进 reset 类别
    assert announced.state_version == "announced"
    assert announced.occurred_at == utc(2026, 9, 3, 23, 12, 9)

    available = by_id[AVAILABLE_ID]
    assert available.state_version == "available"
    assert available.group_kind != "reset"
    assert available.payload["observed_variant"] == "after_announcement"

    versions = sorted(event.state_version for event in events)
    assert versions == ["announced", "available"]
    # 无 window / 无 credits 额外字段泄漏
    assert all(event.kind != "reset" for event in events)


def test_credits_unknown_is_silent() -> None:
    events = _classifier().credit_candidates(timeline=_timeline("timeline_credits_unknown"), feed=None)
    assert events == []  # unknown 缺失 → 不产生版本、不入库、不推送


def test_credits_same_id_in_timeline_and_feed_is_deduplicated() -> None:
    timeline = _timeline()
    feed = _feed(
        {
            "fetched_at": "2026-10-20T10:19:48.808Z",
            "stale": False,
            "events": list(load_fixture("timeline_credits_flow")["events"]),
        }
    )
    events = _classifier().credit_candidates(timeline=timeline, feed=feed)
    assert len(events) == 2  # 两个 id 各一条，跨端点去重


def test_after_announcement_comes_from_feed_observation() -> None:
    feed = _feed(
        {
            "fetched_at": "2026-10-20T10:19:48.808Z",
            "stale": False,
            "banked_observations": [
                {
                    "id": AVAILABLE_ID,
                    "kind": "banked",
                    "source": "monitoring",
                    "banked_state": "available",
                    "title": "Landed on monitoring accounts",
                    "text": "Tibo has not announced it on X yet",
                    "at": "2026-10-07T23:24:55.000Z",
                    "after_announcement": False,
                }
            ],
        }
    )
    events = _classifier().credit_candidates(timeline=_timeline(), feed=feed)
    available = next(event for event in events if event.payload["event_id"] == AVAILABLE_ID)
    assert available.payload["after_announcement"] is False

    unaffected = next(event for event in events if event.payload["event_id"] == ANNOUNCED_ID)
    assert unaffected.payload["after_announcement"] is True  # 未命中观测 → 默认 True


def test_after_announcement_false_uses_observation_wording() -> None:
    """D-10：观测事实不得输出「已宣布」。"""
    payload = {
        "event_id": AVAILABLE_ID,
        "state": "available",
        "date": "2026-10-07",
        "url": None,
        "after_announcement": False,
    }
    text = render_credits(payload, tz=SHANGHAI, settings=GroupSettings())
    assert "状态：网站监测账户已观测到该权益到账" in text
    assert "已宣布" not in text


# ---------------------------------------------------------------------------
# 冻结文案（PRD §6.4）
# ---------------------------------------------------------------------------
def test_banked_state_wording_is_frozen() -> None:
    assert banked_state_cn("announced") == "已宣布（尚不能视为所有账号已到账）"
    assert banked_state_cn("arriving") == "正在发放（网站监测账户未全部到账）"
    assert banked_state_cn("available") == "网站记录该权益已可用（不代表所有账号可用）"
    assert banked_state_cn(None) == "未知"


def test_credits_template_matches_frozen_wording() -> None:
    events = _classifier().credit_candidates(timeline=_timeline(), feed=None)
    announced = next(event for event in events if event.payload["event_id"] == ANNOUNCED_ID)
    text = render_credits(announced.payload, tz=SHANGHAI, settings=GroupSettings())
    assert "🎟️【Codex 额外重置权益动态】" in text
    assert "事件：Banked Reset" in text
    assert "状态：已宣布（尚不能视为所有账号已到账）" in text
    assert "更新日期：2026-09-03" in text
    assert "详情：请查看原始消息与个人账号实际信息。" in text
    assert "原始来源：https://x.com/thsottiaux/status/2095651088502591861" in text
    assert "数据来源：https://codex-reset.com/" in text
    assert "已确认" not in text  # 权益消息不是重置确认


def test_credits_omits_original_line_without_safe_url() -> None:
    payload = {"event_id": AVAILABLE_ID, "state": "available", "date": "2026-10-07", "url": None}
    text = render_credits(payload, tz=SHANGHAI, settings=GroupSettings())
    assert "原始来源：" not in text  # 无安全链接 → 省略原文行（AC-20）
    assert "数据来源：https://codex-reset.com/" in text
