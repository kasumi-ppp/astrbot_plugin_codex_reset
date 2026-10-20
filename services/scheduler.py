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
from datetime import datetime, timedelta

from .clock import Clock
from .errors import ErrorCode

logger = logging.getLogger(__name__)

DEFAULT_ROTATION: tuple[str, ...] = ("forecast", "timeline", "forecast", "feed")
BACKOFF_SCHEDULE: tuple[int, ...] = (120, 240, 480, 900)
MAX_BACKOFF_SECONDS = 900
MIN_BACKOFF_SECONDS = 60


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
        self._rng = random.Random(20261020)

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
        if error_code == ErrorCode.HTTP_429 and retry_after:
            # 429 优先尊重服务端 Retry-After（上限与退避上限一致，防止无限期停摆）
            delay = min(max(float(retry_after), float(MIN_BACKOFF_SECONDS)), float(MAX_BACKOFF_SECONDS))
        if self._jitter > 0:
            delay += self._rng.uniform(0.0, self._jitter)
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


__all__ = ["BACKOFF_SCHEDULE", "DEFAULT_ROTATION", "MAX_BACKOFF_SECONDS", "SourceScheduler"]
