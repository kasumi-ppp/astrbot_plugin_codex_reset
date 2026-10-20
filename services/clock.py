"""时钟抽象：生产用 `RealClock`，测试用 `FakeClock`（零真实等待）。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def ensure_utc(value: datetime) -> datetime:
    """把任意 datetime 归一化为 timezone-aware UTC。naive 视为 UTC（D-07）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """当前 UTC 时间（timezone-aware）。"""
    return datetime.now(UTC)


@runtime_checkable
class Clock(Protocol):
    """时间来源协议：所有涉及「现在」的逻辑都必须经过它，便于回放与测试。"""

    def utcnow(self) -> datetime:
        """返回当前 timezone-aware UTC 时间。"""
        ...

    async def sleep(self, seconds: float) -> None:
        """异步等待指定秒数。"""
        ...

    def monotonic(self) -> float:
        """单调时钟读数（秒），用于计算延迟。"""
        ...


class RealClock:
    """真实时钟实现。"""

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """可推进的假时钟，`sleep` 不真实等待但会推进内部时间并记录调用。"""

    def __init__(
        self,
        now: datetime | None = None,
        *,
        monotonic_start: float = 1000.0,
    ) -> None:
        self._now: datetime = (
            ensure_utc(now) if now is not None else datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        )
        self._monotonic: float = float(monotonic_start)
        self.sleeps: list[float] = []

    def utcnow(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        if seconds > 0:
            self.advance(seconds)

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> datetime:
        """把假时钟向前推进 `seconds` 秒（同时推进单调时钟）。"""
        delta = float(seconds)
        self._now = self._now + timedelta(seconds=delta)
        self._monotonic += delta
        return self._now

    def set_now(self, value: datetime) -> None:
        """强制设定当前时间（单调时钟不变，模拟跳变/跨日）。"""
        self._now = ensure_utc(value)

    @property
    def sleeps_total(self) -> float:
        """累计 sleep 秒数（测试断言用）。"""
        return sum(self.sleeps)
