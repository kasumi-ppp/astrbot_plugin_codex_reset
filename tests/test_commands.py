"""命令层测试：权限 / 限频 / 只读查询 / 配置双写（AC-15 / 16 / 21 / 22 + 子命令回显）。

全部在假时钟与 SQLite 临时库上运行：不触网、不发真实 QQ 消息，
断言的是「回复文本 + 数据库状态 + sink 调用」。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from conftest import (
    SHANGHAI,
    FakeClock,
    create_subscription,
    error_fetch,
    make_pipeline,
    mark_baseline_built,
    ok_fetch,
    open_repo,
    reset_row,
    run,
    timeline_payload,
    utc,
)

from services.commands import (
    ADMIN_DENIED_TEXT,
    GROUP_ONLY_TEXT,
    GROUPS_PRIVATE_TEXT,
    HISTORY_USAGE,
    REPORT_NO_DATA_TEXT,
    SET_USAGE,
    CommandService,
)
from services.config import resolve_group_settings
from services.errors import ErrorCode
from services.models import CommandRequest, ForecastSnapshot, GroupOverride, LogicalEvent
from services.repository import Repository

UMO = "aiocqhttp:GroupMessage:1001"
PRIVATE_UMO = "aiocqhttp:FriendMessage:42"


class FakeSink:
    """记录 `sync_group` / `drop_group` 调用（模拟 WebUI 卡片写入）。"""

    def __init__(self) -> None:
        self.synced: list[str] = []
        self.dropped: list[str] = []
        self.should_fail = False

    async def sync_group(self, sub: Any) -> bool:
        """记录同步的 subscription_id。"""
        self.synced.append(sub.subscription_id)
        return not self.should_fail

    async def drop_group(self, subscription_id: str) -> bool:
        """记录被删除的 subscription_id。"""
        self.dropped.append(subscription_id)
        return not self.should_fail


def _request(
    subcommand: str = "status",
    args: tuple[str, ...] = (),
    *,
    is_admin: bool = True,
    is_group: bool = True,
    umo: str = UMO,
    group_id: str = "1001",
    group_name: str = "测试群",
    sender_id: str = "42",
) -> CommandRequest:
    """构造一个命令请求（`received_at=None` 让服务用假时钟）。"""
    return CommandRequest(
        umo=umo,
        is_group=is_group,
        group_id=group_id,
        platform="aiocqhttp",
        sender_id=sender_id,
        is_admin=is_admin,
        subcommand=subcommand,
        args=tuple(args),
        group_name=group_name,
    )


async def _boot(tmp_path: Path):
    """建库 + 处理链 + 命令服务，时钟 05:00 起步（便于构造历史事件）。"""
    clock = FakeClock(utc(2026, 10, 20, 5, 0))
    repo = await open_repo(tmp_path, clock=clock)
    bundle = make_pipeline(repo, clock)
    sink = FakeSink()
    service = CommandService(
        repo=repo,
        read=bundle.pipeline,
        settings_provider=lambda: bundle.settings,
        sink=sink,
        clock=clock,
        tz_provider=lambda: SHANGHAI,
    )
    return clock, repo, bundle, sink, service


async def _seed_reset(repo: Repository, event_id: str, announced_at: str) -> None:
    """直接入库一条已确认重置（用于 history / latest 断言）。"""
    await repo.record_logical_event(
        LogicalEvent(
            logical_id=f"reset:{event_id}",
            kind="reset",
            group_kind="reset",
            state_version="confirmed",
            occurred_at=utc(2026, 10, 20, 11, 0),
            payload={
                "event_id": event_id,
                "summary": f"事件 {event_id}",
                "url": f"https://x.com/thsottiaux/status/{event_id}",
                "occurred_at": announced_at,
            },
        ),
        baseline=False,
    )


# ---------------------------------------------------------------------------
# AC-15：非管理员被拒但仍可查询
# ---------------------------------------------------------------------------
def test_non_admin_is_rejected_but_can_query(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, sink, service = await _boot(tmp_path)
        try:
            denied = await service.handle(_request("subscribe", is_admin=False))
            assert ADMIN_DENIED_TEXT in denied.text
            assert denied.error == "forbidden"
            assert await repo.list_subscriptions() == []
            assert sink.synced == []

            status = await service.handle(_request("status", is_admin=False))
            assert status.error is None
            assert "监控开关" in status.text
            assert "数据来源：https://codex-reset.com/" in status.text
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-16：退订后零投递，且卡片被移除
# ---------------------------------------------------------------------------
def test_unsubscribe_stops_deliveries_and_drops_card(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, bundle, sink, service = await _boot(tmp_path)
        try:
            subscribed = await service.handle(_request("subscribe"))
            assert "订阅成功" in subscribed.text
            subscriptions = await repo.list_subscriptions()
            assert len(subscriptions) == 1
            assert sink.synced == [subscriptions[0].subscription_id]

            clock.set_now(utc(2026, 10, 20, 12, 0))
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6, 0))
            first = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T11:59:30Z",
                        events=[reset_row("2112432076767289678", "2026-10-20T06:33:48Z")],
                    ),
                    fetched_at=utc(2026, 10, 20, 11, 59, 30),
                    batch="b-16-1",
                    checked_at=utc(2026, 10, 20, 11, 59, 30),
                ),
            )
            assert first.deliveries_created == 1

            clock.advance(6)  # 越过 unsubscribe 的 5s 限频
            removed = await service.handle(_request("unsubscribe"))
            assert "已取消订阅" in removed.text
            assert sink.dropped == [subscriptions[0].subscription_id]
            assert await repo.list_subscriptions() == []
            # 级联删除未发送的投递
            assert await repo.count_deliveries() == 0

            clock.advance(10)
            after = await bundle.pipeline.ingest(
                "timeline",
                ok_fetch(
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T12:00:30Z",
                        events=[reset_row("2109045392684032299", "2026-10-20T06:50:00Z")],
                    ),
                    fetched_at=utc(2026, 10, 20, 12, 0, 30),
                    batch="b-16-2",
                    checked_at=utc(2026, 10, 20, 12, 0, 30),
                ),
            )
            assert after.deliveries_created == 0
            assert await repo.count_deliveries() == 0
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-22：/cr test 带「测试」标记、不入库、不建投递
# ---------------------------------------------------------------------------
def test_test_command_is_labelled_and_writes_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        _clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            reply = await service.handle(_request("test"))
            assert "🧪【测试消息】" in reply.text
            assert "测试" in reply.text
            assert "数据来源：https://codex-reset.com/" in reply.text
            assert await repo.count_deliveries() == 0
            assert await repo.list_events(limit=10) == []

            denied = await service.handle(_request("test", is_admin=False))
            assert ADMIN_DENIED_TEXT in denied.text
            assert denied.error == "forbidden"
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# AC-26：/cr report 手动通道——出图 + 等价文本、不触网、不占日报额度
# ---------------------------------------------------------------------------
def test_report_command_sends_image_and_never_fetches(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, bundle, _sink, _service = await _boot(tmp_path)
        service = CommandService(
            repo=repo,
            read=bundle.pipeline,
            settings_provider=lambda: bundle.settings,
            sink=FakeSink(),
            clock=clock,
            tz_provider=lambda: SHANGHAI,
            daily=bundle.pipeline,
        )
        try:
            # ① 无快照：明确「暂无数据」，且不触发任何上游请求
            empty = await service.handle(_request("report"))
            assert empty.error == "no_data"
            assert REPORT_NO_DATA_TEXT in empty.text
            assert empty.image is None
            assert bundle.client.calls == []

            # ② 注入新鲜快照后手动出图
            now = clock.utcnow()
            bundle.pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
                updated_at=now - timedelta(minutes=1),
                rounded_24h=37,
                rounded_48h=62,
                confidence="medium",
                last_reset_at=None,
                latest_alert=None,
                fetched_at=now,
            )
            clock.advance(61)  # report 限频 60s
            reply = await service.handle(_request("report"))
            assert reply.error is None
            assert reply.image is not None
            assert reply.image.startswith(b"\x89PNG")
            # 文本兜底内容同样齐全（来源署名 + 实验性声明）
            assert "数据来源：https://codex-reset.com/" in reply.text
            assert "实验性预测" in reply.text
            assert bundle.client.calls == []

            # ③ 手动通道不入库、不消耗当天自动日报的唯一发送权
            assert await repo.count_deliveries() == 0
            assert await repo.list_daily_reports(limit=10) == []

            # ④ 非管理员与非群会话均被拒
            clock.advance(61)
            denied = await service.handle(_request("report", is_admin=False))
            assert ADMIN_DENIED_TEXT in denied.text
            assert denied.error == "forbidden"
            clock.advance(61)
            private = await service.handle(_request("report", is_group=False, group_id=""))
            assert private.error == "not_group"
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# `/cr set daily on|off`：群级日报开关（G07 / C04）
# ---------------------------------------------------------------------------
def test_set_daily_switch_writes_override(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, bundle, sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            clock.advance(6)
            on = await service.handle(_request("set", ("daily", "on")))
            assert "每日预测图片" in on.text
            assert "开启" in on.text

            clock.advance(6)
            off = await service.handle(_request("set", ("daily", "off")))
            assert "关闭" in off.text

            stored = await repo.get_subscription("aiocqhttp:GroupMessage:1001")
            assert stored is not None
            # 全局默认 false：改回默认值时覆盖字段被删除，重新回到「继承」语义
            assert stored.override.enable_daily_report is None
            effective = resolve_group_settings(bundle.settings, stored.override)
            assert effective.enable_daily_report is False

            clock.advance(6)
            await service.handle(_request("set", ("daily", "on")))
            stored = await repo.get_subscription("aiocqhttp:GroupMessage:1001")
            assert stored is not None
            assert stored.override.enable_daily_report is True
            effective = resolve_group_settings(bundle.settings, stored.override)
            assert effective.enable_daily_report is True
            assert sink.synced  # 卡片投影已刷新
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 限频：命中即回剩余秒数，且绝不触发上游请求（AC-21）
# ---------------------------------------------------------------------------
def test_rate_limit_blocks_repeat_and_never_fetches(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            first = await service.handle(_request("status"))
            assert first.error is None
            second = await service.handle(_request("status"))
            assert second.error == "rate_limited"
            assert "请等待" in second.text and "秒后再试" in second.text
            assert bundle.client.calls == []

            clock.advance(15)
            third = await service.handle(_request("status"))
            assert third.error is None
            assert bundle.client.calls == []
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# /cr set：越界拒绝、合法写入、群覆盖来源回显
# ---------------------------------------------------------------------------
def test_set_rejects_out_of_range_threshold(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            clock.advance(6)  # set 限频 5s
            reply = await service.handle(_request("set", ("p24", "101")))
            assert "0-100" in reply.text
            assert "拒绝" in reply.text
            assert sink.synced  # 订阅时已同步过一次

            row = await repo.get_subscription(UMO)
            assert row is not None
            assert row.override.threshold_24h is None  # 非法值不落库

            clock.advance(6)
            bad_unit = await service.handle(_request("set", ("p24",)))
            assert bad_unit.text.startswith(SET_USAGE)
        finally:
            await repo.close()

    run(scenario())


def test_set_updates_override_and_marks_group_source(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            before = len(sink.synced)
            clock.advance(6)

            toggled = await service.handle(_request("set", ("reset", "off")))
            assert "已更新" in toggled.text
            assert "群覆盖" in toggled.text
            assert len(sink.synced) == before + 1

            row = await repo.get_subscription(UMO)
            assert row is not None
            assert row.override.enable_confirmed_reset is False

            clock.advance(6)
            threshold = await service.handle(_request("set", ("p24", "65")))
            assert "65%" in threshold.text

            clock.advance(6)
            settings = await service.handle(_request("settings"))
            assert "确认重置：关闭（群覆盖）" in settings.text
            assert "24h 阈值：65%（群覆盖）" in settings.text
            assert "48h 阈值：70%（全局默认）" in settings.text
        finally:
            await repo.close()

    run(scenario())


def test_set_back_to_default_clears_override(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            clock.advance(6)
            await service.handle(_request("set", ("reset", "off")))
            clock.advance(6)
            back = await service.handle(_request("set", ("reset", "on")))
            assert "已更新" in back.text
            row = await repo.get_subscription(UMO)
            assert row is not None
            assert row.override.enable_confirmed_reset is None  # 回到「继承全局默认」
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# /cr history：默认 3、上限 10、非法参数提示
# ---------------------------------------------------------------------------
def test_history_truncates_and_rejects_invalid_arguments(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            for index in range(12):
                await _seed_reset(repo, f"event-{index:02d}", "2026-10-20T11:00:00Z")

            truncated = await service.handle(_request("history", ("99",)))
            assert "最近 10 条确认重置" in truncated.text
            assert "（仅显示最近 10 条）" in truncated.text

            clock.advance(16)
            invalid = await service.handle(_request("history", ("abc",)))
            assert invalid.text.startswith(HISTORY_USAGE)

            clock.advance(16)
            zero = await service.handle(_request("history", ("0",)))
            assert zero.text.startswith(HISTORY_USAGE)
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# /cr groups：群内提示私聊；私聊返回健康表
# ---------------------------------------------------------------------------
def test_groups_requires_private_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            in_group = await service.handle(_request("groups"))
            assert in_group.text.startswith(GROUPS_PRIVATE_TEXT)

            await service.handle(_request("subscribe"))
            clock.advance(11)  # groups 限频 10s
            private = await service.handle(
                _request(
                    "groups",
                    is_group=False,
                    umo=PRIVATE_UMO,
                    group_id="",
                )
            )
            assert "订阅群与推送健康" in private.text
            assert "测试群" in private.text
            assert "uncertain" in private.text
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# /cr subscribe：私聊被拒、重复订阅回显、群上限
# ---------------------------------------------------------------------------
def test_subscribe_from_private_chat_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        _clock, repo, _bundle, sink, service = await _boot(tmp_path)
        try:
            reply = await service.handle(
                _request("subscribe", is_group=False, umo=PRIVATE_UMO, group_id="")
            )
            assert reply.text.startswith(GROUP_ONLY_TEXT)
            assert await repo.list_subscriptions() == []
            assert sink.synced == []
        finally:
            await repo.close()

    run(scenario())


def test_subscribe_again_echoes_existing_settings(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            clock.advance(6)  # subscribe 限频 5s
            again = await service.handle(_request("subscribe"))
            assert "本群已订阅" in again.text
            assert len(await repo.list_subscriptions()) == 1
            assert len(sink.synced) == 1  # 重复订阅不再写卡片
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 帮助 / 未知子命令 / 只读命令
# ---------------------------------------------------------------------------
def test_help_and_unknown_subcommand_are_self_explanatory(tmp_path: Path) -> None:
    async def scenario() -> None:
        _clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            help_reply = await service.handle(_request("help", is_admin=False))
            assert "/codexreset" in help_reply.text
            assert "/cr set" in help_reply.text
            assert "仅管理员" not in help_reply.text  # 帮助本身对所有人开放
            assert "数据来源：https://codex-reset.com/" in help_reply.text

            unknown = await service.handle(_request("nope", is_admin=False))
            assert "未知子命令：nope" in unknown.text
            assert "/cr help" in unknown.text
        finally:
            await repo.close()

    run(scenario())


def test_query_commands_read_only_cache(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            empty = await service.handle(_request("forecast"))
            assert "暂无数据" in empty.text

            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    {
                        "updated_at": "2026-10-20T11:59:00Z",
                        "probabilities": {"rounded_24h": 55, "rounded_48h": 72},
                        "confidence": "medium",
                        "last_reset_at": None,
                        "latest_alert": None,
                    },
                    fetched_at=utc(2026, 10, 20, 11, 59),
                    batch="b-q",
                    checked_at=utc(2026, 10, 20, 11, 59),
                ),
            )
            await _seed_reset(repo, "2112432076767289678", "2026-10-20T11:00:00Z")
            clock.advance(20)
            forecast = await service.handle(_request("forecast"))
            assert "未来 24 小时：55%" in forecast.text
            assert "未来 48 小时：72%" in forecast.text
            assert "置信度：中" in forecast.text
            assert "实验性" in forecast.text

            clock.advance(20)
            latest = await service.handle(_request("latest"))
            assert "最近一次已确认重置" in latest.text

            clock.advance(20)
            credits = await service.handle(_request("credits"))
            assert "额外重置权益" in credits.text
            assert bundle.client.calls == []  # 全程零上游请求
        finally:
            await repo.close()

    run(scenario())


def test_stale_data_appends_warning_line(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            # 只重建缓存、不写库：直接构造一个「无快照」的读模型状态
            reply = await service.handle(_request("status"))
            assert "⚠️ 数据可能已过时" in reply.text
            await _seed_reset(repo, "2112432076767289678", "2026-10-20T11:00:00Z")
            clock.advance(20)
            forecast = await service.handle(_request("forecast"))
            assert "⚠️ 数据可能已过时" in forecast.text
            assert forecast.text.startswith("暂无数据")
        finally:
            await repo.close()

    run(scenario())


def test_audit_records_admin_actions(tmp_path: Path) -> None:
    async def scenario() -> None:
        _clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            await service.handle(_request("subscribe", is_admin=False))
            files = list(repo.audit_dir.glob("audit-*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text(encoding="utf-8").splitlines()
            assert any('"action": "subscribe"' in line for line in lines)
            assert any('"forbidden"' in line for line in lines)
        finally:
            await repo.close()

    run(scenario())


def test_override_default_is_inherited_when_subscribing(tmp_path: Path) -> None:
    async def scenario() -> None:
        _clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            await service.handle(_request("subscribe"))
            row = await repo.get_subscription(UMO)
            assert row is not None
            assert row.override == GroupOverride()  # 订阅不写任何覆盖
            settings = await service.handle(_request("settings"))
            assert "（全局默认）" in settings.text
        finally:
            await repo.close()

    run(scenario())


def test_subscribe_is_blocked_at_group_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        from services.models import PluginSettings

        clock = FakeClock(utc(2026, 10, 20, 5, 0))
        repo = await open_repo(tmp_path, clock=clock)
        bundle = make_pipeline(repo, clock, settings=PluginSettings(group_limit=1))
        service = CommandService(
            repo=repo,
            read=bundle.pipeline,
            settings_provider=lambda: bundle.settings,
            sink=FakeSink(),
            clock=clock,
            tz_provider=lambda: SHANGHAI,
        )
        try:
            await create_subscription(repo, umo="aiocqhttp:GroupMessage:9999", group_id="9999")
            reply = await service.handle(_request("subscribe"))
            assert "群上限" in reply.text
            assert len(await repo.list_subscriptions()) == 1
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# P2-05：`/cr status` 必须能诊断「基线未建立」「forecast 端点失败」
# ---------------------------------------------------------------------------
def test_status_reports_missing_baseline(tmp_path: Path) -> None:
    """基线未建立时自动通知整体暂缓，必须在 status 里说出来（否则现象是「永久零通知」）。"""

    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            reply = await service.handle(_request("status"))
            assert "⚠️ 基线尚未建立（timeline 未成功采集），自动通知暂缓。" in reply.text
            assert "forecast 端点最近采集失败" not in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_status_reports_forecast_endpoint_failure(tmp_path: Path) -> None:
    """基线已建立但 forecast 最近失败 → 追加一行诊断（概率与 Watch 可能不更新）。"""

    async def scenario() -> None:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            await mark_baseline_built(repo)
            await bundle.pipeline.ingest(
                "forecast", error_fetch("forecast", ErrorCode.HTTP_5XX, status=502)
            )
            reply = await service.handle(_request("status"))
            assert "基线尚未建立" not in reply.text
            assert "⚠️ forecast 端点最近采集失败，概率与 Watch 可能不更新。" in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_status_shows_reset_probability_watch_and_updated_at(tmp_path: Path) -> None:
    """C01：`/cr status` 四项要素齐备（最近确认重置 / 概率 / 预告状态 / 数据更新时间）。"""

    async def scenario() -> None:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            await mark_baseline_built(repo)
            await _seed_reset(repo, "1001", "2026-10-20T02:00:00Z")
            now = clock.utcnow()
            bundle.pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
                updated_at=now - timedelta(minutes=1),
                rounded_24h=37,
                rounded_48h=62,
                confidence="medium",
                last_reset_at=utc(2026, 10, 20, 2, 0),
                latest_alert=None,
                fetched_at=now,
            )
            reply = await service.handle(_request("status"))
            assert "最新预测：24h 37% / 48h 62%" in reply.text
            assert "统计更新：" in reply.text
            # 11:00Z → 北京时间 19:00；且必须来自**已确认重置**事件而非权益
            assert "最近确认重置：2026-10-20 19:00" in reply.text
            assert "预告状态：暂无明确预告" in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_status_has_no_diagnostic_lines_when_healthy(tmp_path: Path) -> None:
    """健康状态下不得出现任何降级行（避免狼来了）。"""

    async def scenario() -> None:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            await mark_baseline_built(repo)
            await bundle.pipeline.ingest(
                "forecast",
                ok_fetch(
                    "forecast",
                    {"updated_at": "2026-10-20T04:59:00Z", "probabilities": {"rounded_24h": 10,
                                                                           "rounded_48h": 20}},
                    fetched_at=clock.utcnow(),
                ),
            )
            reply = await service.handle(_request("status"))
            assert "⚠️ 基线" not in reply.text
            assert "forecast 端点最近采集失败" not in reply.text
        finally:
            await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# P2-09 / P2-10：history 只列确认重置；只读实例拒绝写指令
# ---------------------------------------------------------------------------
def test_history_excludes_non_reset_and_downgraded_rows(tmp_path: Path) -> None:
    """`/cr history` 必须只列**确认重置**：watch/credits/预测与降级更正都不得混入。

    非 reset 类别用更晚的时间制造「若过滤失效就会排在最前」的压力；
    降级更正同样是 `group_kind="reset"`，是真实会发生且用户可见误导的一类。
    """

    async def scenario() -> str:
        clock, repo, bundle, _sink, service = await _boot(tmp_path)
        try:
            await _seed_reset(repo, "real-01", "2026-10-20T06:33:48Z")
            for kind in ("watch", "credits", "forecast"):
                await repo.record_logical_event(
                    LogicalEvent(
                        logical_id=f"{kind}:noise-01",
                        kind=kind,
                        group_kind=kind,
                        state_version="active",
                        occurred_at=utc(2026, 10, 20, 12, 0),
                        payload={
                            "summary": f"{kind} 噪声行",
                            "occurred_at": "2026-10-20T12:00:00Z",
                        },
                    ),
                    baseline=False,
                )
            await repo.record_logical_event(
                LogicalEvent(
                    logical_id="reset:revoked-01",
                    kind="reset",
                    group_kind="reset",
                    state_version="downgraded:announced",
                    occurred_at=utc(2026, 10, 20, 13, 0),
                    payload={
                        "summary": "该重置已被撤回",
                        "occurred_at": "2026-10-20T13:00:00Z",
                    },
                ),
                baseline=False,
            )
            reply = await service.handle(_request("history", ("10",)))
            return reply.text
        finally:
            await repo.close()

    text = run(scenario())

    assert "最近 1 条确认重置" in text, text
    assert "real-01" in text, text
    assert "噪声行" not in text, "非 reset 类别不得出现在确认重置历史里"
    assert "撤回" not in text, "降级更正（已撤回）不得被当成确认重置展示"


def test_readonly_instance_rejects_write_commands_but_allows_queries(tmp_path: Path) -> None:
    """P2-10：只读实例（另一实例持锁）必须拒绝写指令，查询指令照常可用。"""

    async def scenario() -> tuple[Any, Any, Any, int]:
        clock = FakeClock(utc(2026, 10, 20, 5, 0))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            bundle = make_pipeline(repo, clock)
            service = CommandService(
                repo=repo,
                read=bundle.pipeline,
                settings_provider=lambda: bundle.settings,
                sink=FakeSink(),
                clock=clock,
                tz_provider=lambda: SHANGHAI,
                readonly_provider=lambda: True,
            )
            subscribe = await service.handle(_request("subscribe"))
            set_reply = await service.handle(_request("set", ("reset", "off")))
            status = await service.handle(_request("status"))
            return subscribe, set_reply, status, len(await repo.list_subscriptions())
        finally:
            await repo.close()

    subscribe, set_reply, status, count = run(scenario())

    assert subscribe.error == "readonly", subscribe.text
    assert set_reply.error == "readonly", set_reply.text
    assert "只读模式" in subscribe.text
    assert count == 0, "只读实例不得写入订阅"
    assert status.error is None or status.error == "no_data", status.text


# ---------------------------------------------------------------------------
# 改动 2 / 3：图片失败分类提示 + 状态 / 配置双层开关展示
# ---------------------------------------------------------------------------
class FakeDaily:
    """`DailyReportProvider` 替身：返回预置的 `(png, payload, reason)`。"""

    def __init__(self, result: tuple[bytes | None, dict[str, Any] | None, str | None]) -> None:
        self.result = result
        self.calls: list[tuple[bool, str | None]] = []

    async def manual_report_png(
        self, *, now: Any, test: bool = False, theme: str | None = None
    ) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
        self.calls.append((test, theme))
        return self.result


def _report_payload() -> dict[str, Any]:
    return {
        "business_date": "2026-10-20",
        "rounded_24h": 37,
        "rounded_48h": 62,
        "confidence": "medium",
        "updated_at": "2026-10-20T01:00:00+00:00",
        "last_reset_at": None,
        "watch_status": "none",
        "watch_window": None,
        "stale": False,
        "theme": "dark",
        "manual": True,
        "test": False,
    }


async def _make_service_with_daily(tmp_path: Path, daily: FakeDaily) -> tuple[Any, Any]:
    """构造带 `daily` 替身的命令服务（必须在事件循环内调用）。"""
    clock = FakeClock(utc(2026, 10, 20, 5, 0))
    repo = await open_repo(tmp_path, clock=clock)
    bundle = make_pipeline(repo, clock)
    service = CommandService(
        repo=repo,
        read=bundle.pipeline,
        settings_provider=lambda: bundle.settings,
        sink=FakeSink(),
        clock=clock,
        tz_provider=lambda: SHANGHAI,
        daily=daily,
    )
    return service, repo


def test_report_command_classifies_render_failures(tmp_path: Path) -> None:
    """改动 2：`/cr report` 按失败类别给出中文提示，且不与「图片发送失败」叠加。"""
    async def scenario() -> tuple[str, str, str]:
        cases = {
            "font_missing": "缺少中文字体，无法生成图片",
            "font_error": "缺少中文字体，无法生成图片",
            "render_timeout": "图片渲染超时",
            "render_error": "图片渲染失败",
        }
        out: dict[str, str] = {}
        for reason, keyword in cases.items():
            service, repo = await _make_service_with_daily(
                tmp_path, FakeDaily((None, _report_payload(), reason))
            )
            try:
                reply = await service.handle(_request("report"))
                out[reason] = reply.text
                assert reply.error == "render_failed"
                assert keyword in reply.text, reply.text
                assert "图片发送失败" not in reply.text, reply.text
            finally:
                await repo.close()
        return out["font_missing"], out["render_timeout"], out["render_error"]

    run(scenario())


def test_report_command_no_data_keeps_existing_hint(tmp_path: Path) -> None:
    """改动 2：payload 为 None 时保持「暂无数据」提示。"""
    async def scenario() -> None:
        service, repo = await _make_service_with_daily(tmp_path, FakeDaily((None, None, None)))
        try:
            reply = await service.handle(_request("report"))
            assert reply.error == "no_data"
            assert REPORT_NO_DATA_TEXT in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_test_image_command_classifies_render_failures(tmp_path: Path) -> None:
    """改动 2：`/cr test image` 失败分类带「🧪【测试消息】」前缀 + 来源署名，且不含「图片发送失败」。"""
    async def scenario() -> None:
        service, repo = await _make_service_with_daily(
            tmp_path, FakeDaily((None, _report_payload(), "font_missing"))
        )
        try:
            reply = await service.handle(_request("test", args=("image",)))
            assert reply.error == "render_failed"
            assert "🧪【测试消息】" in reply.text
            assert "缺少中文字体" in reply.text
            assert "数据来源：https://codex-reset.com/" in reply.text
            assert "图片发送失败" not in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_status_shows_image_environment_and_daily_switch(tmp_path: Path) -> None:
    """改动 3：`/cr status` 展示图片环境与每日预测图片双层开关。"""
    async def scenario() -> None:
        clock, repo, _bundle, _sink, service = await _boot(tmp_path)
        try:
            reply = await service.handle(_request("status"))
            assert "图片环境：" in reply.text
            assert "每日预测图片：全局" in reply.text
            assert "本群" in reply.text
        finally:
            await repo.close()

    run(scenario())


def test_settings_shows_two_layer_daily_switch(tmp_path: Path) -> None:
    """改动 3：`/cr settings` 明确展示全局 + 本群 + 来源。"""
    async def scenario() -> None:
        from services.models import GroupSettings, PluginSettings

        clock = FakeClock(utc(2026, 10, 20, 5, 0))
        repo = await open_repo(tmp_path, clock=clock)
        # 全局开启 daily + 群默认开启 → 全局开 / 本群开（继承全局）
        settings = PluginSettings(
            enable_daily_report=True, defaults=GroupSettings(enable_daily_report=True)
        )
        bundle = make_pipeline(repo, clock, settings=settings)
        service = CommandService(
            repo=repo,
            read=bundle.pipeline,
            settings_provider=lambda: bundle.settings,
            sink=FakeSink(),
            clock=clock,
            tz_provider=lambda: SHANGHAI,
        )
        try:
            await service.handle(_request("subscribe"))
            settings_reply = await service.handle(_request("settings"))
            assert "每日预测图片：全局 开启 ｜ 本群 开启（全局默认）" in settings_reply.text

            # 本群关闭 daily → 全局开 / 本群关（群覆盖）
            clock.advance(6)
            await service.handle(_request("set", ("daily", "off")))
            clock.advance(6)
            settings_reply = await service.handle(_request("settings"))
            assert "每日预测图片：全局 开启 ｜ 本群 关闭（群覆盖）" in settings_reply.text
        finally:
            await repo.close()

    run(scenario())
