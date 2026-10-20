"""QA 独立回归 ⑦：命令限频边界（PRD §4 冻结限频 + §4.5 规则）。

PRD §4 命令表给出的秒数：查询 15s、subscribe/unsubscribe/settings/set 5s、
groups 10s、test 60s；§4.5 要求「命中冷却回复剩余秒数，**不触发上游请求**」。

边界口径：`(umo, 命令签名)` 维度；`t < limit` 拒绝、`t >= limit` 放行；
被拒的那次**不得**刷新计时起点（否则连续点命令会永久锁死）。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
import math
from datetime import datetime
from typing import Any

import pytest

from conftest import SHANGHAI, FakeClock, run

from services.commands import RATE_LIMITS, CommandService
from services.models import CommandRequest, PluginSettings
from qa_helpers import NOW, PRIVATE_UMO, UMO, open_repo_or_fail


class EmptyRead:
    """`ReadModel` 空实现：本文件只关心限频，不关心展示内容。"""

    def forecast(self) -> Any:
        return None

    def timeline(self) -> Any:
        return None

    def feed(self) -> Any:
        return None

    def freshness(self, now: datetime) -> tuple[datetime | None, bool]:
        return None, True


def _request(subcommand: str, *, umo: str = UMO) -> CommandRequest:
    return CommandRequest(
        umo=umo,
        is_group=True,
        group_id="1001",
        platform="aiocqhttp",
        sender_id="10001",
        is_admin=True,
        subcommand=subcommand,
        args=(),
        received_at=NOW,
    )


def _service(repo, clock: FakeClock) -> CommandService:
    return CommandService(
        repo=repo,
        read=EmptyRead(),
        settings_provider=lambda: PluginSettings(),
        sink=None,
        clock=clock,
        tz_provider=lambda: SHANGHAI,
    )


@pytest.mark.parametrize("command", sorted(RATE_LIMITS))
def test_rate_limit_boundaries_for_every_command(command):
    """逐命令验证「14.9s 拒绝 / 15.1s 放行」（限频值取自 PRD 冻结表）。"""
    limit = RATE_LIMITS[command]
    clock = FakeClock(NOW)
    service = CommandService(
        repo=object(),  # check_rate 不触碰仓库
        read=EmptyRead(),
        settings_provider=lambda: PluginSettings(),
        sink=None,
        clock=clock,
        tz_provider=lambda: SHANGHAI,
    )

    assert service.check_rate(UMO, command, clock.utcnow()) == 0, "首次必须放行"

    clock.advance(limit - 0.1)
    remaining = service.check_rate(UMO, command, clock.utcnow())
    assert remaining > 0, f"{command} 在 {limit - 0.1}s 时不应放行"
    assert remaining == math.ceil(0.1), "剩余秒数应向上取整到 1"

    clock.advance(0.2)  # 累计 limit + 0.1
    assert service.check_rate(UMO, command, clock.utcnow()) == 0, (
        f"{command} 在 {limit + 0.1}s 时必须放行（被拒的那次不得刷新起点）"
    )


def test_rate_limit_is_scoped_per_command_and_per_umo():
    """限频键是 `(umo, 子命令)`：不同子命令、不同群互不影响。"""
    clock = FakeClock(NOW)
    service = CommandService(
        repo=object(),
        read=EmptyRead(),
        settings_provider=lambda: PluginSettings(),
        sink=None,
        clock=clock,
        tz_provider=lambda: SHANGHAI,
    )

    assert service.check_rate(UMO, "status", clock.utcnow()) == 0
    assert service.check_rate(UMO, "forecast", clock.utcnow()) == 0, "不同子命令互不影响"
    assert service.check_rate(PRIVATE_UMO, "status", clock.utcnow()) == 0, "不同会话互不影响"
    assert service.check_rate(UMO, "status", clock.utcnow()) > 0, "同一 (umo, 子命令) 必须被拦"

    clock.advance(15.1)
    assert service.check_rate(UMO, "status", clock.utcnow()) == 0


def test_handle_returns_remaining_seconds_and_never_touches_repo(tmp_path):
    """命中限频时 `handle` 必须回「请等待 N 秒后再试」，且不查库、不发请求。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            clock = FakeClock(NOW)
            service = _service(repo, clock)
            first = await service.handle(_request("status"))
            clock.advance(10)
            blocked = await service.handle(_request("status"))
            clock.advance(5.1)  # 合计 15.1s
            allowed = await service.handle(_request("status"))
            return first, blocked, allowed

        finally:
            await repo.close()

    first, blocked, allowed = run(scenario())

    assert first.error is None
    assert blocked.error == "rate_limited"
    assert "请等待 5 秒后再试" in blocked.text, "10s 时剩余应为 5s"
    assert allowed.error is None


def test_admin_denied_calls_do_not_consume_rate_limit(tmp_path):
    """越权拒绝发生在限频之前：普通成员连点管理指令不得把自己的计时器点满。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            clock = FakeClock(NOW)
            service = _service(repo, clock)
            denied = CommandRequest(
                umo=UMO,
                is_group=True,
                group_id="1001",
                platform="aiocqhttp",
                sender_id="20002",
                is_admin=False,
                subcommand="subscribe",
                received_at=NOW,
            )
            first = await service.handle(denied)
            second = await service.handle(denied)
            third = await service.handle(denied)
            return first, second, third

        finally:
            await repo.close()

    first, second, third = run(scenario())

    for reply in (first, second, third):
        assert reply.error == "forbidden", "越权文案必须始终如一，不能被限频文案覆盖"


def test_test_command_has_sixty_second_cooldown(tmp_path):
    """`/cr test` 限频 60s（PRD 命令表），且不受其他命令的计时影响。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            clock = FakeClock(NOW)
            service = _service(repo, clock)
            first = await service.handle(_request("test"))
            clock.advance(59)
            blocked = await service.handle(_request("test"))
            clock.advance(1.5)
            allowed = await service.handle(_request("test"))
            return first, blocked, allowed

        finally:
            await repo.close()

    first, blocked, allowed = run(scenario())

    assert "🧪【测试消息】" in first.text
    assert blocked.error == "rate_limited"
    assert "请等待 1 秒后再试" in blocked.text
    assert allowed.error is None
