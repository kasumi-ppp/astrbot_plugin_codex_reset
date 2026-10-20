"""QA 独立回归 ④：管理越权与只读查询（AC-15 / AC-16 / AC-21 / AC-22 + PRD §4 权限规则）。

PRD 口径：

- 修改与诊断类命令一律要求 AstrBot 管理员（`event.is_admin()`），普通成员越权返回
  「仅管理员可执行…」且**配置无任何变化**，查询类命令不受影响（PRD §4.1/§4.2）；
- `subscribe/unsubscribe` 必须来自真实群会话，禁止裸群号拼接 umo（PRD §4.3）；
- `/cr groups` 建议私聊，群内只回提示（PRD §4 命令表）；
- `/cr test` 不写库、不建投递（Q-3 + AC-22）；
- 命令处理零上游请求，缓存命中即时返回（AC-21）。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from conftest import SHANGHAI, FakeClock, create_subscription, run

from services.commands import ADMIN_DENIED_TEXT, GROUP_ONLY_TEXT, GROUPS_PRIVATE_TEXT, CommandService
from services.models import CommandRequest, GroupOverride, LogicalEvent, PluginSettings
from services.notifications import SOURCE_LINE
from services.parser import parse_feed, parse_forecast, parse_timeline
from qa_helpers import (
    NOW,
    PRIVATE_UMO,
    REAL_RESET_ID,
    UMO,
    credits_row,
    event_row,
    feed_payload,
    forecast_payload,
    open_repo_or_fail,
    reset_alert,
    timeline_payload,
)

ADMIN_COMMANDS = ("subscribe", "unsubscribe", "settings", "set", "groups", "test")
QUERY_COMMANDS = ("status", "forecast", "latest", "credits", "history", "help")


class StubRead:
    """只读视图替身：持有真实解析后的快照，**没有**任何上游客户端。

    命令层一旦误调 `SourceClient` 就会因缺少属性而暴露，从而守住 AC-21 的「零请求」。
    """

    def __init__(self) -> None:
        self._forecast = parse_forecast(
            forecast_payload(last_reset_at="2026-10-20T06:33:48.000Z", alert=reset_alert()),
            fetched_at=NOW,
        ).snapshot
        self._timeline = parse_timeline(
            timeline_payload("2026-10-20T11:55:00.000Z", [
                event_row(REAL_RESET_ID, announced_at="2026-10-20T06:33:48.000Z"),
                credits_row("2107975486798164736", banked_state="available"),
            ]),
            fetched_at=NOW,
        ).snapshot
        self._feed = parse_feed(feed_payload(), fetched_at=NOW).snapshot

    def forecast(self) -> Any:
        return self._forecast

    def timeline(self) -> Any:
        return self._timeline

    def feed(self) -> Any:
        return self._feed

    def freshness(self, now: datetime) -> tuple[datetime | None, bool]:
        return NOW, False


class RecordingSink:
    """记录 WebUI 卡片同步调用（AC-16：退订后卡片必须被移除）。"""

    def __init__(self) -> None:
        self.synced: list[str] = []
        self.dropped: list[str] = []

    async def sync_group(self, sub: Any) -> bool:
        self.synced.append(sub.subscription_id)
        return True

    async def drop_group(self, subscription_id: str) -> bool:
        self.dropped.append(subscription_id)
        return True


def _request(
    subcommand: str,
    *,
    admin: bool = False,
    group: bool = True,
    args: tuple[str, ...] = (),
) -> CommandRequest:
    return CommandRequest(
        umo=UMO if group else PRIVATE_UMO,
        is_group=group,
        group_id="1001" if group else "",
        platform="aiocqhttp",
        sender_id="10001" if admin else "20002",
        is_admin=admin,
        subcommand=subcommand,
        args=args,
        received_at=NOW,
    )


def _service(repo, *, clock=None, settings=None) -> CommandService:
    return CommandService(
        repo=repo,
        read=StubRead(),
        settings_provider=lambda: settings or PluginSettings(),
        sink=RecordingSink(),
        clock=clock or FakeClock(NOW),
        tz_provider=lambda: SHANGHAI,
    )


async def _seed_subscription(repo, *, override: GroupOverride | None = None):
    return await create_subscription(repo, umo=UMO, group_id="1001", override=override)


# ---------------------------------------------------------------------------
# PRD §4.2：普通成员越权必须被拒，且配置零变化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ADMIN_COMMANDS)
def test_non_admin_is_rejected_for_every_admin_command(tmp_path, command):
    """AC-15：6 个管理指令对普通成员一律返回固定拒绝文案。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)  # 先让群处于已订阅状态，确保拒绝不是「未订阅」
            service = _service(repo)
            before = await repo.get_subscription(UMO)
            reply = await service.handle(_request(command, admin=False, args=("p24", "60")))
            after = await repo.get_subscription(UMO)
            return reply, before, after

        finally:
            await repo.close()

    reply, before, after = run(scenario())

    assert reply.error == "forbidden"
    assert ADMIN_DENIED_TEXT in reply.text
    assert "/cr help" in reply.text
    assert before is not None and after is not None
    assert after.override.as_sparse_dict() == before.override.as_sparse_dict(), "越权调用不得改动配置"


def test_non_admin_still_gets_full_query_after_denial(tmp_path):
    """AC-15 后半：被拒之后同一个人仍可正常查询（查询类不受权限影响）。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            service = _service(repo)
            denied = await service.handle(_request("subscribe", admin=False))
            status = await service.handle(_request("status", admin=False))
            return denied, status, await repo.list_subscriptions()

        finally:
            await repo.close()

    denied, status, subs = run(scenario())

    assert denied.error == "forbidden"
    assert status.error is None
    assert "📡" in status.text
    assert subs == [], "被拒的订阅绝不允许落库"


# ---------------------------------------------------------------------------
# 查询类命令全部可用（非空 + 署名）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", QUERY_COMMANDS)
def test_query_commands_are_available_to_members_with_source_line(tmp_path, command):
    """AC-20 + AC-15：6 个查询命令对普通成员均可用，且每条都带来源署名。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await repo.record_logical_event(
                LogicalEvent(
                    logical_id=f"reset:{REAL_RESET_ID}",
                    kind="reset",
                    group_kind="reset",
                    state_version="confirmed",
                    occurred_at=datetime(2026, 10, 20, 6, 33, 48, tzinfo=UTC),
                    payload={"summary": "reset recorded", "url": "https://x.com/a/1"},
                ),
                baseline=True,
            )
            service = _service(repo)
            return await service.handle(_request(command, admin=False))

        finally:
            await repo.close()

    reply = run(scenario())

    assert reply.error is None, reply.text
    assert reply.text.strip(), "查询回复不得为空"
    assert SOURCE_LINE in reply.text, "AC-20：所有展示上游数据的消息必须带署名"
    assert ADMIN_DENIED_TEXT not in reply.text


def test_status_reports_monitor_switch_and_freshness(tmp_path):
    """F05/AC-21：`/cr status` 必须包含监控开关、概率、订阅群数与新鲜度信息。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            service = _service(repo)
            return await service.handle(_request("status", admin=False))

        finally:
            await repo.close()

    reply = run(scenario())

    for token in ("监控开关", "最新预测", "24h 22%", "48h 39%", "统计更新", "订阅群数：1"):
        assert token in reply.text, f"缺少字段：{token}"


def test_history_clamps_argument_to_ten(tmp_path):
    """PRD §4：`/cr history [n]` 默认 3、上限 10，超出截断并提示。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            for index in range(12):
                await repo.record_logical_event(
                    LogicalEvent(
                        logical_id=f"reset:2100000000000000{index:03d}",
                        kind="reset",
                        group_kind="reset",
                        state_version="confirmed",
                        occurred_at=datetime(2026, 10, 1 + index, tzinfo=UTC),
                        payload={"summary": f"event {index}"},
                    ),
                    baseline=True,
                )
            clock = FakeClock(NOW)
            service = _service(repo, clock=clock)
            big = await service.handle(_request("history", args=("99",)))
            clock.advance(16)  # history 限频 15s
            bad = await service.handle(_request("history", args=("abc",)))
            return big, bad

        finally:
            await repo.close()

    big, bad = run(scenario())

    assert "最近 10 条" in big.text
    assert big.text.count("\n· ") == 10, "history 必须截断到 10 条"
    assert "仅显示最近 10 条" in big.text
    assert "用法：" in bad.text


# ---------------------------------------------------------------------------
# PRD §4 命令表：groups 私聊、subscribe 群内
# ---------------------------------------------------------------------------
def test_groups_in_group_session_only_returns_hint(tmp_path):
    """`/cr groups` 在群会话必须只回「请私聊」，不得泄露群列表。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            service = _service(repo)
            return await service.handle(_request("groups", admin=True, group=True))

        finally:
            await repo.close()

    reply = run(scenario())

    assert GROUPS_PRIVATE_TEXT in reply.text
    assert "订阅群与推送健康" not in reply.text


def test_groups_in_private_lists_subscription_health(tmp_path):
    """私聊 `/cr groups` 返回群列表 + 推送健康（最近成功/失败、uncertain 计数）。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            service = _service(repo)
            return await service.handle(_request("groups", admin=True, group=False))

        finally:
            await repo.close()

    reply = run(scenario())

    assert "订阅群与推送健康" in reply.text
    assert "测试群" in reply.text
    assert "uncertain：" in reply.text


def test_subscribe_from_private_chat_is_rejected(tmp_path):
    """PRD §4.3：私聊执行 subscribe 必须被拒，且不产生任何订阅（umo 只能来自真实群）。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            service = _service(repo)
            reply = await service.handle(_request("subscribe", admin=True, group=False))
            return reply, await repo.list_subscriptions()

        finally:
            await repo.close()

    reply, subs = run(scenario())

    assert GROUP_ONLY_TEXT in reply.text
    assert subs == []


def test_subscribe_then_unsubscribe_syncs_and_drops_card(tmp_path):
    """AC-16：管理员订阅写入卡片；退订后订阅行删除、卡片被移除。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            clock = FakeClock(NOW)
            sink = RecordingSink()
            service = CommandService(
                repo=repo,
                read=StubRead(),
                settings_provider=lambda: PluginSettings(),
                sink=sink,
                clock=clock,
                tz_provider=lambda: SHANGHAI,
            )
            first = await service.handle(_request("subscribe", admin=True))
            clock.advance(6)
            second = await service.handle(_request("unsubscribe", admin=True))
            return first, second, sink, await repo.list_subscriptions(), await repo.count_deliveries()

        finally:
            await repo.close()

    first, second, sink, subs, deliveries = run(scenario())

    assert "订阅成功" in first.text
    assert len(sink.synced) == 1
    assert "已取消订阅" in second.text
    assert len(sink.dropped) == 1
    assert subs == []
    assert deliveries == 0


def test_test_command_writes_nothing_and_is_labelled(tmp_path):
    """AC-22/Q-3：`/cr test` 带测试标记，不写 deliveries、不改真实事件状态。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            service = _service(repo)
            reply = await service.handle(_request("test", admin=True))
            return reply, await repo.count_deliveries(), await repo.list_events(limit=10)

        finally:
            await repo.close()

    reply, deliveries, events = run(scenario())

    assert "🧪【测试消息】" in reply.text
    assert "本次为 /cr test 生成，不改变任何真实事件状态。" in reply.text
    assert SOURCE_LINE in reply.text
    assert deliveries == 0
    assert events == []


# ---------------------------------------------------------------------------
# `/cr set` 校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("args", "expect"),
    [
        (("p24", "101"), "合法区间 0-100"),
        (("p24", "-1"), "合法区间 0-100"),
        (("p48", "abc"), "合法区间 0-100"),
        (("reset", "maybe"), "on|off"),
        (("nope", "1"), "用法：/cr set"),
        (("p24",), "用法：/cr set"),
    ],
)
def test_set_rejects_invalid_values_without_touching_override(tmp_path, args, expect):
    """PRD §4：越界/非法参数必须拒绝并回显合法区间，且配置不变。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            service = _service(repo)
            reply = await service.handle(_request("set", admin=True, args=args))
            row = await repo.get_subscription(UMO)
            return reply, row

        finally:
            await repo.close()

    reply, row = run(scenario())

    assert expect in reply.text
    assert row is not None
    assert row.override.as_sparse_dict() == {}, "非法输入不得写入覆盖"


def test_set_threshold_equal_to_default_returns_to_inheritance(tmp_path):
    """稀疏覆盖语义：设为与全局默认相同的值 → 回到「继承」，不产生覆盖行。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            clock = FakeClock(NOW)
            service = _service(repo, clock=clock)
            changed = await service.handle(_request("set", admin=True, args=("p24", "80")))
            clock.advance(6)  # `set` 限频 5s
            back = await service.handle(_request("set", admin=True, args=("p24", "50")))
            row = await repo.get_subscription(UMO)
            return changed, back, row

        finally:
            await repo.close()

    changed, back, row = run(scenario())

    assert "已更新" in changed.text and "80%" in changed.text
    assert "50%" in back.text
    assert row is not None and row.override.as_sparse_dict() == {}


# ---------------------------------------------------------------------------
# AC-21：命令处理不触发上游请求、缓存命中即时返回
# ---------------------------------------------------------------------------
def test_command_handling_is_fast_and_never_fetches(tmp_path):
    """AC-21：`ReadModel` 替身没有 client 属性，若命令层偷偷取数会直接报错；同时耗时 < 2s。"""

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            await _seed_subscription(repo)
            service = _service(repo)
            started = time.perf_counter()
            replies = [
                await service.handle(_request(command, admin=False))
                for command in ("status", "forecast", "latest", "credits", "history", "help")
            ]
            return time.perf_counter() - started, replies

        finally:
            await repo.close()

    elapsed, replies = run(scenario())

    assert all(reply.error is None for reply in replies)
    assert elapsed < 2.0, f"缓存命中查询耗时过长：{elapsed:.3f}s"
