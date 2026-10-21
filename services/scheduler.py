"""采集槽位调度：轮换 / ≥60s 间隔 / 强制 timeline / 2-4-8-15 分钟退避（AC-19、AC-18）。

不变量：
1. **任意两次请求开始时间的间隔 ≥ `spacing_seconds`**（由 `_last_request_started_at` 保证，
   即使上游响应很慢也不会插队）；
2. 未到 `next_due_at`（退避中或刚采集完）时 `next_endpoint()` 返回 `None`；
3. `force_timeline()` 只插入一次性优先级，不破坏轮换指针。
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import Clock, ensure_utc
from .config import parse_hhmm
from .errors import ErrorCode

logger = logging.getLogger(__name__)

DEFAULT_ROTATION: tuple[str, ...] = ("forecast", "timeline", "forecast", "feed")
BACKOFF_SCHEDULE: tuple[int, ...] = (120, 240, 480, 900)
MAX_BACKOFF_SECONDS = 900
MIN_BACKOFF_SECONDS = 60

DEFAULT_DAILY_TIME = "09:00"
DEFAULT_DAILY_EXPIRY_MINUTES = 30
DEFAULT_DAILY_RETRY_MINUTES = 5


class SourceScheduler:
    """决定「这一轮该采哪个端点、什么时候才能再采」。"""

    def __init__(
        self,
        *,
        spacing_seconds: int,
        clock: Clock,
        rotation: tuple[str, ...] = DEFAULT_ROTATION,
        jitter: float = 0.0,
    ) -> None:
        if not rotation:
            raise ValueError("rotation 不能为空")
        self._spacing = max(1, int(spacing_seconds))
        self._clock = clock
        self._rotation = tuple(rotation)
        self._jitter = max(0.0, float(jitter))
        self._pointer = 0
        self._pending: str | None = None
        self._pending_reason: str | None = None
        self._failures = 0
        self._next_due_at: datetime | None = None
        self._last_request_started_at: datetime | None = None
        self._rng = random.Random()

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def spacing_seconds(self) -> int:
        """两次请求之间的最小间隔（秒）。"""
        return self._spacing

    @property
    def rotation(self) -> tuple[str, ...]:
        """轮换序列。"""
        return self._rotation

    @property
    def pointer(self) -> int:
        """下一个待取槽位下标。"""
        return self._pointer

    @property
    def next_due_at(self) -> datetime | None:
        """下一次允许采集的时间（None 表示立即允许）。"""
        return self._next_due_at

    @property
    def last_request_started_at(self) -> datetime | None:
        """最近一次请求开始时间。"""
        return self._last_request_started_at

    @property
    def pending_endpoint(self) -> str | None:
        """被强制提速的端点（一次性）。"""
        return self._pending

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def next_endpoint(self, now: datetime) -> str | None:
        """返回本轮可采集的端点；未到槽位或退避中返回 None。"""
        if self._last_request_started_at is not None:
            elapsed = (now - self._last_request_started_at).total_seconds()
            if elapsed < self._spacing:
                return None
        if self._next_due_at is not None and now < self._next_due_at:
            return None

        if self._pending is not None:
            endpoint = self._pending
            reason = self._pending_reason or ""
            self._pending = None
            self._pending_reason = None
            logger.info("[codex-reset][scheduler] forced endpoint=%s reason=%s", endpoint, reason)
        else:
            endpoint = self._rotation[self._pointer]
            self._pointer = (self._pointer + 1) % len(self._rotation)

        self._last_request_started_at = now
        self._next_due_at = now + timedelta(seconds=self._spacing)
        return endpoint

    def record_result(
        self,
        endpoint: str,
        error_code: str,
        now: datetime,
        *,
        retry_after: int | None = None,
    ) -> datetime:
        """登记一次采集结果并返回下一次允许采集的时间。"""
        if error_code in ErrorCode.SUCCESS_CODES:
            self._failures = 0
            self._next_due_at = now + timedelta(seconds=self._spacing)
            logger.info(
                "[codex-reset][scheduler] endpoint=%s ok next_due_at=%s",
                endpoint,
                self._next_due_at.isoformat(),
            )
            return self._next_due_at

        self._failures += 1
        delay = float(self.backoff_seconds(self._failures))
        is_rate_limited = error_code == ErrorCode.HTTP_429 and retry_after is not None
        if is_rate_limited:
            # 429 必须遵守服务端 Retry-After；仅将过短值抬到全局最小间隔。
            delay = max(float(retry_after), float(MIN_BACKOFF_SECONDS))
        if self._jitter > 0:
            delay += self._rng.uniform(0.0, self._jitter)
        if not is_rate_limited:
            # 普通错误退避（含抖动）仍受 15 分钟上限约束。
            delay = min(delay, float(MAX_BACKOFF_SECONDS))
        self._next_due_at = now + timedelta(seconds=delay)
        logger.warning(
            "[codex-reset][scheduler] endpoint=%s failed error=%s failures=%s backoff_s=%.1f",
            endpoint,
            error_code,
            self._failures,
            delay,
        )
        return self._next_due_at

    def force_timeline(self, reason: str) -> None:
        """下一槽位强制 timeline（出现新 Watch 或确认重置后调用）。"""
        self._pending = "timeline"
        self._pending_reason = reason
        logger.info("[codex-reset][scheduler] force_timeline reason=%s", reason)

    def set_spacing(self, seconds: int) -> None:
        """热更新最小请求间隔（配置经验证后立即生效，PRD §5）。

        除了改间隔本身，还要把**正常槽位**的 `_next_due_at` 按新间隔重算一次，
        否则「600s → 30s」这类调小要等满一个旧周期才生效。退避中的等待
        （`_failures > 0`）保持不变：那是上游要求的冷却，不能被配置改动取消。
        """
        previous = self._spacing
        self._spacing = max(1, int(seconds))
        if self._failures == 0 and self._last_request_started_at is not None:
            self._next_due_at = self._last_request_started_at + timedelta(seconds=self._spacing)
        logger.info(
            "[codex-reset][scheduler] spacing_seconds %s -> %s", previous, self._spacing
        )

    def consecutive_failures(self) -> int:
        """当前连续失败次数（成功即清零）。"""
        return self._failures

    def backoff_seconds(self, failures: int) -> int:
        """按连续失败次数返回退避秒数：2/4/8/15 分钟，上限 900s。"""
        if failures <= 0:
            return 0
        index = min(failures, len(BACKOFF_SCHEDULE)) - 1
        return min(BACKOFF_SCHEDULE[index], MAX_BACKOFF_SECONDS)

    def reset(self) -> None:
        """重置调度状态（测试与配置热更新使用）。"""
        self._pointer = 0
        self._pending = None
        self._pending_reason = None
        self._failures = 0
        self._next_due_at = None
        self._last_request_started_at = None


class DailyReportScheduler:
    """每日预测图片的「业务日期 + 发送窗口」计算器（P01 / P02 / G08）。

    只做纯时间计算，不做任何 I/O 与发送判定：
    - 业务日期按 `daily_report_timezone`（默认 Asia/Shanghai）取自然日；
    - 发送窗口 = `[当日 scheduled_at, scheduled_at + expiry_minutes]`；
    - 窗口外（含错过整窗）一律不补发，避免在不合适的时间推送过期内容。
    """

    def __init__(
        self,
        *,
        time_text: str = DEFAULT_DAILY_TIME,
        time_zone: Any,
        expiry_minutes: int = DEFAULT_DAILY_EXPIRY_MINUTES,
        retry_minutes: int = DEFAULT_DAILY_RETRY_MINUTES,
        clock: Clock | None = None,
    ) -> None:
        """`time_zone` 为 IANA 名或 `ZoneInfo`；非法时区由上层回落（config 层已校验）。"""
        self._zone = _as_zone(time_zone)
        self._retry_minutes = max(0, int(retry_minutes))
        self._expiry_minutes = max(0, int(expiry_minutes))
        self._clock = clock
        # 先给出与 `configure` 内部一致的初始值，避免首次 configure 比较未定义属性
        self._hour, self._minute = parse_hhmm(DEFAULT_DAILY_TIME) or (9, 0)
        self._time_text = f"{self._hour:02d}:{self._minute:02d}"
        self.configure(time_text=time_text)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def configure(
        self,
        *,
        time_text: str,
        expiry_minutes: int | None = None,
        retry_minutes: int | None = None,
        time_zone: Any = None,
    ) -> None:
        """热更新发送时间 / 窗口 / 时区（P02：修改后立即重算下一次运行时间）。

        配置未变化时静默返回：调度器每 30s 会被 pipeline 重建/刷新一次，
        若每次都写 INFO 日志会把日志刷满。
        """
        parsed = parse_hhmm(time_text)
        if parsed is None:
            logger.warning(
                "[codex-reset][scheduler] daily_report_time=%r 非法，回落 %s",
                time_text,
                DEFAULT_DAILY_TIME,
            )
            parsed = parse_hhmm(DEFAULT_DAILY_TIME) or (9, 0)
        zone = _as_zone(time_zone) if time_zone is not None else self._zone
        expiry = max(0, int(expiry_minutes)) if expiry_minutes is not None else self._expiry_minutes
        retry = max(0, int(retry_minutes)) if retry_minutes is not None else self._retry_minutes
        unchanged = (
            parsed == (self._hour, self._minute)
            and zone.key == self._zone.key
            and expiry == self._expiry_minutes
            and retry == self._retry_minutes
        )
        self._hour, self._minute = parsed
        self._time_text = f"{self._hour:02d}:{self._minute:02d}"
        self._zone = zone
        self._expiry_minutes = expiry
        self._retry_minutes = retry
        if unchanged:
            return
        logger.info(
            "[codex-reset][scheduler] daily report time=%s tz=%s expiry=%smin retry=%smin",
            self._time_text,
            self._zone.key,
            self._expiry_minutes,
            self._retry_minutes,
        )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def time_text(self) -> str:
        """规范化后的 `HH:MM`。"""
        return self._time_text

    @property
    def time_zone(self) -> ZoneInfo:
        """调度时区。"""
        return self._zone

    @property
    def expiry_minutes(self) -> int:
        """最大补发窗口（分钟）。"""
        return self._expiry_minutes

    @property
    def retry_minutes(self) -> int:
        """明确失败后的重试间隔（分钟）。"""
        return self._retry_minutes

    # ------------------------------------------------------------------
    # 时间计算
    # ------------------------------------------------------------------
    def business_date(self, now: datetime) -> str:
        """调度时区下的业务日期（`YYYY-MM-DD`）。"""
        return ensure_utc(now).astimezone(self._zone).strftime("%Y-%m-%d")

    def scheduled_at(self, business_date: str) -> datetime:
        """业务日期当日的发送时刻（UTC）。"""
        try:
            local_day = datetime.strptime(business_date, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(
                "[codex-reset][scheduler] business_date=%r 非法，按今天处理", business_date
            )
            local_day = ensure_utc(self._clock.utcnow() if self._clock else datetime.now(UTC))
            local_day = local_day.astimezone(self._zone).date()
        local = datetime(
            local_day.year, local_day.month, local_day.day, self._hour, self._minute,
            tzinfo=self._zone,
        )
        return local.astimezone(UTC)

    def window_end(self, business_date: str) -> datetime:
        """补发窗口结束时刻（`scheduled_at + expiry_minutes`）。"""
        return self.scheduled_at(business_date) + timedelta(minutes=self._expiry_minutes)

    def due_window(self, now: datetime) -> str | None:
        """若 `now` 落在某个业务日期的发送窗口内，返回该业务日期；否则返回 None。

        窗口含起点、不含终点（`start <= now < end`）：到达终点的下一秒即视为错过，
        与「超过窗口后跳过当日报告」的语义一致。
        """
        moment = ensure_utc(now)
        for offset in (0, -1):
            # 用「今天 / 昨天」两个候选日判断，覆盖跨日窗口（如 00:00 发送、时区西移）
            candidate = (moment + timedelta(days=offset)).astimezone(self._zone)
            date_text = candidate.strftime("%Y-%m-%d")
            start = self.scheduled_at(date_text)
            if start <= moment < self.window_end(date_text):
                return date_text
        return None

    def next_run_at(self, now: datetime) -> datetime:
        """下一次应触发发送的时刻（含补发窗口判定，供日志与测试断言）。"""
        moment = ensure_utc(now)
        pending = self.due_window(moment)
        if pending is not None:
            return self.scheduled_at(pending)
        for offset in (0, 1, 2):
            candidate = (moment + timedelta(days=offset)).astimezone(self._zone)
            date_text = candidate.strftime("%Y-%m-%d")
            start = self.scheduled_at(date_text)
            if start > moment:
                return start
        return self.window_end(self.business_date(moment))

    def retry_delay_seconds(self) -> float:
        """明确失败后的重试间隔（秒）。"""
        return float(self._retry_minutes * 60)

    def within_retry(self, last_attempt_at: datetime | None, now: datetime) -> bool:
        """上一次失败后是否还没到重试时间。"""
        if last_attempt_at is None:
            return False
        if self._retry_minutes <= 0:
            return False
        return ensure_utc(now) < ensure_utc(last_attempt_at) + timedelta(
            minutes=self._retry_minutes
        )


def _as_zone(value: Any) -> ZoneInfo:
    """把 IANA 名 / ZoneInfo 统一成 `ZoneInfo`；非法回落 Asia/Shanghai。"""
    if isinstance(value, ZoneInfo):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning("[codex-reset][scheduler] 时区 %r 非法，回落 Asia/Shanghai", value)
    return ZoneInfo("Asia/Shanghai")


__all__ = [
    "BACKOFF_SCHEDULE",
    "DEFAULT_DAILY_EXPIRY_MINUTES",
    "DEFAULT_DAILY_RETRY_MINUTES",
    "DEFAULT_DAILY_TIME",
    "DEFAULT_ROTATION",
    "DailyReportScheduler",
    "MAX_BACKOFF_SECONDS",
    "SourceScheduler",
]
