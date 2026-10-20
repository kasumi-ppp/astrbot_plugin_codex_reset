"""调度测试：轮换序列、≥60s 间隔、强制 timeline、2/4/8/15 分钟退避。"""

from __future__ import annotations

from datetime import timedelta

from conftest import utc

from services.clock import FakeClock
from services.errors import USER_AGENT, ErrorCode
from services.scheduler import BACKOFF_SCHEDULE, DEFAULT_ROTATION, SourceScheduler


def _scheduler(clock: FakeClock, *, spacing: int = 60, jitter: float = 0.0) -> SourceScheduler:
    return SourceScheduler(spacing_seconds=spacing, clock=clock, jitter=jitter)


def test_rotation_sequence_matches_spec() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock)
    sequence: list[str] = []
    for _ in range(8):
        endpoint = scheduler.next_endpoint(clock.utcnow())
        assert endpoint is not None
        sequence.append(endpoint)
        scheduler.record_result(endpoint, ErrorCode.OK, clock.utcnow())
        clock.advance(60)
    assert sequence == ["forecast", "timeline", "forecast", "feed"] * 2
    assert DEFAULT_ROTATION == ("forecast", "timeline", "forecast", "feed")


def test_two_requests_are_at_least_spacing_seconds_apart() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, spacing=60)

    first_at = clock.utcnow()
    assert scheduler.next_endpoint(first_at) == "forecast"
    scheduler.record_result("forecast", ErrorCode.OK, first_at)

    clock.advance(59)
    assert scheduler.next_endpoint(clock.utcnow()) is None  # 未满 60s 一律不出队

    clock.advance(1)
    second_at = clock.utcnow()
    assert scheduler.next_endpoint(second_at) == "timeline"
    assert (second_at - first_at).total_seconds() >= 60


def test_force_timeline_preempts_next_slot() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock)
    assert scheduler.next_endpoint(clock.utcnow()) == "forecast"
    scheduler.record_result("forecast", ErrorCode.OK, clock.utcnow())

    scheduler.force_timeline("new_watch")
    assert scheduler.pending_endpoint == "timeline"

    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"
    assert scheduler.pending_endpoint is None  # 只生效一次

    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"  # 轮换指针未被破坏


def test_backoff_schedule_is_two_four_eight_fifteen_minutes() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, jitter=0.0)

    now = clock.utcnow()
    assert scheduler.next_endpoint(now) == "forecast"

    expected = [120, 240, 480, 900, 900]
    for index, delay in enumerate(expected, start=1):
        now = clock.utcnow()
        due = scheduler.record_result("forecast", ErrorCode.NETWORK, now)
        assert (due - now).total_seconds() == delay, f"第 {index} 次失败退避应为 {delay}s"
        assert scheduler.consecutive_failures() == index
        clock.set_now(due)
        assert scheduler.next_endpoint(clock.utcnow()) is not None

    assert scheduler.backoff_seconds(1) == BACKOFF_SCHEDULE[0]
    assert scheduler.backoff_seconds(99) == BACKOFF_SCHEDULE[-1]


def test_success_resets_failure_counter_and_backoff() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, jitter=0.0)
    now = clock.utcnow()
    scheduler.next_endpoint(now)
    scheduler.record_result("forecast", ErrorCode.HTTP_5XX, now)
    scheduler.record_result("forecast", ErrorCode.HTTP_5XX, now)
    assert scheduler.consecutive_failures() == 2

    due = scheduler.record_result("forecast", ErrorCode.OK, now)
    assert scheduler.consecutive_failures() == 0
    assert (due - now).total_seconds() == 60

    due = scheduler.record_result("forecast", ErrorCode.NETWORK, now)
    assert (due - now).total_seconds() == 120  # 重新从 2 分钟起


def test_429_uses_retry_after_but_stays_bounded() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, jitter=0.0)
    now = clock.utcnow()
    scheduler.next_endpoint(now)

    due = scheduler.record_result("forecast", ErrorCode.HTTP_429, now, retry_after=180)
    assert (due - now).total_seconds() == 180

    due = scheduler.record_result("forecast", ErrorCode.HTTP_429, now, retry_after=5)
    assert (due - now).total_seconds() == 60  # 过小的 Retry-After 抬到最小退避

    due = scheduler.record_result("forecast", ErrorCode.HTTP_429, now, retry_after=99999)
    assert (due - now).total_seconds() == 900  # 上限 900s


def test_not_modified_counts_as_success() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock)
    now = clock.utcnow()
    scheduler.next_endpoint(now)
    scheduler.record_result("forecast", ErrorCode.HTTP_5XX, now)
    due = scheduler.record_result("forecast", ErrorCode.NOT_MODIFIED, now)
    assert scheduler.consecutive_failures() == 0
    assert (due - now).total_seconds() == 60


def test_jitter_adds_upper_bound_noise() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, jitter=5.0)
    now = clock.utcnow()
    scheduler.next_endpoint(now)
    first = scheduler.record_result("forecast", ErrorCode.NETWORK, now)
    base = (first - now).total_seconds()
    assert 120 <= base <= 125
    clock.set_now(first)
    second = scheduler.record_result("forecast", ErrorCode.NETWORK, clock.utcnow())
    assert 240 <= (second - clock.utcnow()).total_seconds() <= 245


def test_spacing_and_rotation_properties() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, spacing=90)
    assert scheduler.spacing_seconds == 90
    assert scheduler.rotation == DEFAULT_ROTATION
    now = clock.utcnow()
    scheduler.next_endpoint(now)
    assert scheduler.last_request_started_at == now
    assert scheduler.next_due_at == now + timedelta(seconds=90)
    scheduler.reset()
    assert scheduler.next_due_at is None
    assert scheduler.pointer == 0


def test_set_spacing_takes_effect_on_next_slot() -> None:
    """B / PRD §5：改 `request_spacing_seconds` 后无需重启，下一槽位即按新间隔判定。"""
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, spacing=600)

    start = clock.utcnow()
    assert scheduler.next_endpoint(start) == "forecast"
    scheduler.record_result("forecast", ErrorCode.OK, start)
    clock.advance(60)
    assert scheduler.next_endpoint(clock.utcnow()) is None  # 旧间隔 600s 尚未到

    scheduler.set_spacing(30)
    assert scheduler.spacing_seconds == 30
    assert scheduler.next_endpoint(clock.utcnow()) == "timeline"  # 新间隔立即生效


def test_set_spacing_clamps_to_at_least_one_second() -> None:
    clock = FakeClock(utc())
    scheduler = _scheduler(clock, spacing=60)
    scheduler.set_spacing(0)
    assert scheduler.spacing_seconds == 1
    scheduler.set_spacing(-5)
    assert scheduler.spacing_seconds == 1


def test_user_agent_is_compliant() -> None:
    assert "astrbot-plugin-codex-reset" in USER_AGENT
    assert "https://github.com/kasumi-ppp/astrbot_plugin_codex_reset" in USER_AGENT
