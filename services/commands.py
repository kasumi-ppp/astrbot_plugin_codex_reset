"""命令层：`CommandRequest → CommandReply`（路由 / 权限 / 限频 / 群配置双写）。

硬约束（架构 §H「只读原则」）：
- 本模块**永不**调用 `SourceClient`，所有查询只读内存缓存（`ReadModel`）与 SQLite；
- 出站消息只经 `CommandReply.text` 返回，由 `main.py` 交给平台；
- 群配置的**唯一真源**是 DB（`subscriptions.settings_json` 稀疏覆盖），
  WebUI 卡片只是同步出口，写入一律走 `GroupConfigSink`。

文案全部为中文纯文本，单条 ≤1200 字符，且每条都带
`数据来源：https://codex-reset.com/`（AC-20；`/cr test` 由模板自带）。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .clock import Clock, ensure_utc
from .config import OVERRIDE_FIELDS, resolve_group_settings
from .models import (
    CommandReply,
    CommandRequest,
    GroupOverride,
    GroupSettings,
    PluginSettings,
    ReadModel,
    SubscriptionRow,
    TimelineEvent,
)
from .notifications import (
    MAX_MESSAGE_CHARS,
    SOURCE_LINE,
    TRUNCATED_SUFFIX,
    banked_state_cn,
    confidence_cn,
    format_day,
    format_time,
    render_daily_report_text,
    render_test,
)

logger = logging.getLogger(__name__)

#: `(umo, 子命令)` 维度的限频秒数（PRD §4 冻结）
RATE_LIMITS: dict[str, int] = {
    "status": 15,
    "forecast": 15,
    "latest": 15,
    "credits": 15,
    "history": 15,
    "help": 15,
    "report": 60,
    "subscribe": 5,
    "unsubscribe": 5,
    "settings": 5,
    "set": 5,
    "groups": 10,
    "test": 60,
}

#: 只读查询（群成员即可）
QUERY_COMMANDS: frozenset[str] = frozenset(
    {"", "status", "forecast", "latest", "credits", "history", "help"}
)

#: 管理指令（必须 `is_admin`）
ADMIN_COMMANDS: frozenset[str] = frozenset(
    {"subscribe", "unsubscribe", "settings", "set", "groups", "test", "report"}
)

#: 会写数据库/配置的指令：只读模式（另一实例持锁）下必须拒绝（P2-10）
WRITE_COMMANDS: frozenset[str] = frozenset({"subscribe", "unsubscribe", "set"})

ADMIN_DENIED_TEXT = "仅管理员可执行该指令。使用 /cr help 查看权限说明。"
READONLY_TEXT = "🔒 只读模式：检测到另一个插件实例正在运行，本实例仅支持查询，写操作已停用。"
GROUP_ONLY_TEXT = "请在目标 QQ 群内执行 /cr subscribe（订阅来源必须取自真实群会话）。"
GROUPS_PRIVATE_TEXT = "请在私聊中执行 /cr groups（群列表含推送健康信息）。"
SET_USAGE = (
    "用法：/cr set reset|watch|forecast|credits|daily on|off；/cr set p24|p48 0-100 的整数"
)
HISTORY_USAGE = "用法：/cr history [1-10]（默认 3 条，最多 10 条）"
REPORT_USAGE = "用法：/cr report（在群内手动生成当前概率预测图片）"
TEST_USAGE = "用法：/cr test [image]（默认文本；image 为预测图片发送测试）"
REPORT_NO_DATA_TEXT = "暂无数据：还没有可用的预测快照，无法生成图片，请稍后再试。"
REPORT_NOT_GROUP_TEXT = "请在群聊中执行 /cr report（预测图片面向群发送）。"
NO_DATA_TEXT = "暂无数据：尚未完成首轮采集，请稍后再试。"
NOT_SUBSCRIBED_TEXT = "本群尚未订阅，请管理员执行 /cr subscribe。"

HISTORY_DEFAULT = 3
HISTORY_MAX = 10
GROUPS_MAX_ROWS = 50

#: 类别字段 → 中文短名（状态行 / 配置回显共用）
FLAG_LABELS: tuple[tuple[str, str], ...] = (
    ("enable_confirmed_reset", "重置"),
    ("enable_watch", "预告"),
    ("enable_forecast", "预测"),
    ("enable_credits", "权益"),
    ("enable_daily_report", "日报"),
)

FIELD_LABELS: dict[str, str] = {
    "enable_confirmed_reset": "确认重置",
    "enable_watch": "提前预告",
    "enable_forecast": "概率预测",
    "enable_credits": "额外权益",
    "enable_daily_report": "每日预测图片",
    "threshold_24h": "24h 阈值",
    "threshold_48h": "48h 阈值",
}

#: `/cr set <alias>` → 覆盖字段名
BOOL_ALIASES: dict[str, str] = {
    "reset": "enable_confirmed_reset",
    "watch": "enable_watch",
    "forecast": "enable_forecast",
    "credits": "enable_credits",
    "daily": "enable_daily_report",
}
THRESHOLD_ALIASES: dict[str, str] = {"p24": "threshold_24h", "p48": "threshold_48h"}

KIND_LABELS: dict[str, str] = {
    "reset": "全球重置（已确认）",
    "watch": "提前预告",
    "forecast": "概率预测",
    "credits": "额外重置权益",
    "catchup": "补发摘要",
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""排序兜底时间锚点（timezone-aware，避免与 naive datetime 比较）。"""

HELP_TEXT = "\n".join(
    [
        "📖【Codex 重置雷达 · 指令说明】",
        "",
        "查询（群成员）：",
        "/cr 或 /cr status  监控状态、最近重置、预告状态与数据新鲜度",
        "/cr forecast      24h/48h 重置概率详情",
        "/cr latest        最近一次已确认重置",
        "/cr credits       最近额外重置权益动态",
        "/cr history [n]   最近 n 条确认重置（默认 3，最多 10）",
        "/cr help          本说明",
        "",
        "管理（AstrBot 管理员）：",
        "/cr subscribe     订阅当前群",
        "/cr unsubscribe   取消订阅当前群",
        "/cr settings      查看本群生效配置与来源",
        "/cr set <项> <值> 修改本群配置（reset/watch/forecast/credits/daily on|off；p24/p48 0-100）",
        "/cr report        手动生成当前概率预测图片（不影响每日自动日报）",
        "/cr groups        私聊查看订阅群与推送健康",
        "/cr test [image]  发送测试消息（image 为预测图片测试；不入库、不建立投递）",
        "",
        "备用指令：/codexreset 与 /cr 完全等价（当 /cr 被其他插件占用时使用）。",
        "权限模型：管理指令要求 AstrBot 管理员（event.is_admin）；订阅必须来自真实群会话。",
        "每日预测图片需管理员按群执行 /cr set daily on 开启（默认关闭）。",
        "数据来自第三方站点 codex-reset.com，非官方接口，请自行判断风险。",
    ]
)


class DailyReportProvider(Protocol):
    """`/cr report` 与 `/cr test image` 的图片协作方（`Pipeline` 实现）。

    命令层只消费 `(png, payload)`，不感知渲染细节；`png is None` 表示没有可用
    预测快照或渲染失败（此时 `payload` 仍可能给出，用于文本降级）。
    """

    async def manual_report_png(
        self, *, now: datetime, test: bool = False, theme: str | None = None
    ) -> tuple[bytes | None, dict[str, Any] | None]:
        """手动渲染预测图片；失败或无数据返回 `(None, ...)`。"""
        ...


class GroupConfigSink(Protocol):
    """群配置卡片同步出口（`main.py` 实现，命令层不感知 WebUI 细节）。"""

    async def sync_group(self, sub: SubscriptionRow) -> bool:
        """把某订阅的生效覆盖写入 `group_overrides` 卡片。"""
        ...

    async def drop_group(self, subscription_id: str) -> bool:
        """移除某订阅对应的卡片。"""
        ...


class _NullSink:
    """无 WebUI 场景的空实现（测试与纯 DB 部署）。"""

    async def sync_group(self, sub: SubscriptionRow) -> bool:
        """默认视为成功（无需写卡片）。"""
        return True

    async def drop_group(self, subscription_id: str) -> bool:
        """默认视为成功。"""
        return True


class CommandService:
    """子命令路由 + 权限 + 限频 + 配置编排。"""

    def __init__(
        self,
        *,
        repo: Any,
        read: ReadModel,
        settings_provider: Callable[[], PluginSettings],
        sink: GroupConfigSink | None,
        clock: Clock,
        tz_provider: Callable[[], ZoneInfo],
        readonly_provider: Callable[[], bool] | None = None,
        daily: DailyReportProvider | None = None,
    ) -> None:
        """依赖注入；`repo` 只需提供本文件用到的仓储方法（便于测试替身）。

        `readonly_provider` 返回 True 时（另一实例持锁），所有**写**指令一律拒绝，
        查询指令不受影响——只读实例仍然是一个可用的查询入口。
        `daily` 提供 `/cr report` 与 `/cr test image` 的图片渲染（缺省 None 时这两个
        子命令回复「功能不可用」而不是崩溃）。
        """
        self._repo = repo
        self._read = read
        self._settings_provider = settings_provider
        self._sink: GroupConfigSink = sink or _NullSink()
        self._clock = clock
        self._tz_provider = tz_provider
        self._readonly = readonly_provider or (lambda: False)
        self._daily = daily
        self._last_seen: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def handle(self, req: CommandRequest) -> CommandReply:
        """处理一次命令请求；任何异常都由上层兜底，这里只处理业务分支。"""
        sub = (req.subcommand or "").strip().lower().lstrip("/")
        if sub == "":
            sub = "status"

        if sub not in RATE_LIMITS:
            text = f"{HELP_TEXT}\n\n未知子命令：{sub or req.subcommand}"
            return CommandReply(text=self._finalize(text), error="unknown_command")

        plugin = self._settings_provider()
        now = ensure_utc(req.received_at) if req.received_at is not None else self._clock.utcnow()

        if sub in ADMIN_COMMANDS and not req.is_admin:
            await self._audit(req, f"{sub}:denied", "forbidden", at=now)
            return CommandReply(text=self._finalize(ADMIN_DENIED_TEXT), error="forbidden")

        # 权限判定在前（越权者看到的永远是权限文案），只读判定在后且不消耗限频额度
        if sub in WRITE_COMMANDS and self._readonly():
            await self._audit(req, f"{sub}:readonly", "readonly", at=now)
            return CommandReply(text=self._finalize(READONLY_TEXT), error="readonly")

        wait = self.check_rate(req.umo, sub, now)
        if wait > 0:
            return CommandReply(text=self._finalize(f"请等待 {wait} 秒后再试。"), error="rate_limited")

        tz = self._tz_provider()
        if sub == "status":
            text = await self._render_status(plugin, now, tz)
        elif sub == "forecast":
            text = self._render_forecast(now, tz)
        elif sub == "latest":
            text = await self._render_latest(now, tz)
        elif sub == "credits":
            text = self._render_credits(now, tz)
        elif sub == "history":
            text = await self._render_history(req, now, tz)
        elif sub == "help":
            text = HELP_TEXT
        elif sub == "subscribe":
            text = await self._subscribe(req, plugin, tz)
        elif sub == "unsubscribe":
            text = await self._unsubscribe(req)
        elif sub == "settings":
            text = await self._settings(req, plugin)
        elif sub == "set":
            text = await self._set(req, plugin)
        elif sub == "groups":
            text = await self._groups(req, plugin, tz)
        elif sub == "report":
            return await self._report(req, plugin, tz)
        elif sub == "test":
            return await self._test(req, plugin, tz)
        else:  # 理论不可达：`RATE_LIMITS` 与上方分支一一对应
            text = HELP_TEXT
        return CommandReply(text=self._finalize(text))

    # ------------------------------------------------------------------
    # 限频
    # ------------------------------------------------------------------
    def check_rate(self, umo: str, subcommand: str, now: datetime) -> int:
        """返回 0 表示放行，>0 表示需要等待的秒数（`(umo, 子命令)` 维度）。"""
        key_sub = "status" if subcommand in {"", "/"} else subcommand
        limit = RATE_LIMITS.get(key_sub)
        if limit is None or limit <= 0:
            return 0
        key = (umo, key_sub)
        current = self._clock.monotonic()
        last = self._last_seen.get(key)
        if last is not None:
            elapsed = current - last
            if elapsed < limit:
                return max(1, int(math.ceil(limit - elapsed)))
        self._last_seen[key] = current
        return 0

    # ------------------------------------------------------------------
    # 查询（只读缓存）
    # ------------------------------------------------------------------
    async def _render_status(
        self, plugin: PluginSettings, now: datetime, tz: ZoneInfo
    ) -> str:
        latest, stale = self._read.freshness(now)
        subs = await self._repo.list_subscriptions()
        lines = ["📡【Codex 重置雷达 · 状态】", ""]
        lines.append(
            "监控开关：" + ("开启" if plugin.enable_monitor else "已关闭（enable_monitor=false）")
        )
        lines.append(
            "最近采集：" + (format_time(latest, tz) if latest is not None else "暂无记录")
        )
        forecast = self._read.forecast()
        if forecast is None:
            lines.append("最新预测：还没有可用的 forecast 快照")
        else:
            lines.append(
                f"最新预测：24h {int(forecast.rounded_24h)}% / 48h {int(forecast.rounded_48h)}%"
                f"（置信度：{confidence_cn(forecast.confidence)}）"
            )
            lines.append(f"统计更新：{format_time(forecast.updated_at, tz)}")
        timeline = self._read.timeline()
        if timeline is not None:
            updated = format_time(timeline.updated_at, tz)
            lines.append(f"时间线：{len(timeline.events)} 条记录，更新于 {updated}")
        feed = self._read.feed()
        if feed is not None:
            lines.append(
                "信息流：" + ("⚠️ 站点标记为陈旧" if feed.stale else "正常")
                + f"，最近采集 {format_time(feed.fetched_at, tz)}"
            )
        lines.append(f"订阅群数：{len(subs)}")
        lines.append(await self._last_reset_line(tz))
        lines.append(await self._watch_line(tz))
        lines.extend(await self._diagnostic_lines())
        if stale:
            lines.append("")
            lines.append(self._stale_line(latest, tz))
        return "\n".join(lines)

    async def _last_reset_line(self, tz: ZoneInfo) -> str:
        """`/cr status` 的「最近一次确认重置」行（C01，只读缓存与 SQLite）。"""
        getter = getattr(self._repo, "list_events", None)
        if callable(getter):
            try:
                events = await self._repo.list_events(
                    group_kind="reset", limit=1, exclude_corrections=True
                )
            except Exception:  # 诊断行不得影响查询本身
                logger.warning("[codex-reset][commands] 读取最近重置失败", exc_info=True)
                events = []
            if events:
                return f"最近确认重置：{format_time(events[0].occurred_at, tz)}"
        forecast = self._read.forecast()
        if forecast is not None and forecast.last_reset_at is not None:
            return f"最近确认重置：{format_time(forecast.last_reset_at, tz)}"
        return "最近确认重置：暂无记录"

    async def _watch_line(self, tz: ZoneInfo) -> str:
        """`/cr status` 的「当前有效预告状态」行（C01）。"""
        from .report_renderer import watch_status_text  # 局部导入避免同层循环

        forecast = self._read.forecast()
        alert = forecast.latest_alert if forecast is not None else None
        if alert is None:
            return "预告状态：暂无明确预告"
        if alert.kind == "reset" and alert.state == "confirmed" and not alert.corrected:
            line = f"预告状态：{watch_status_text('confirmed')}"
            confirmed_at = alert.confirmed_at or alert.observed_at or alert.source_at
            if confirmed_at is not None:
                line = f"{line}（{format_time(confirmed_at, tz)}）"
            return line
        if alert.kind == "watch" and alert.state == "active":
            line = f"预告状态：{watch_status_text('watch')}"
            window = self._watch_window_line(alert, tz)
            if window:
                line = f"{line}｜{window}"
            return line
        return "预告状态：暂无明确预告"

    @staticmethod
    def _watch_window_line(alert: Any, tz: ZoneInfo) -> str:
        """预告窗口短文案（无窗口时返回空串，绝不编造小时数）。"""
        from .classifier import _window_dict  # 局部导入避免同层循环
        from .notifications import watch_window_text

        window = _window_dict(alert.window)
        if window is None:
            return ""
        text = watch_window_text({"window": window}, tz)
        return "" if text == "暂无明确预计时段" else text

    async def _diagnostic_lines(self) -> list[str]:
        """只读诊断行（P2-05 / F10）：基线是否建立、forecast 端点是否在失败。

        只读缓存与 DB，**不发任何请求**（AC-21）。这些行是「长期零通知」唯一的
        现场线索：基线未建立时自动通知整体暂缓，forecast 失败时概率与 Watch 不更新。
        """
        lines: list[str] = []
        baseline = None
        getter = getattr(self._repo, "baseline", None)
        if callable(getter):
            try:
                baseline = await getter()
            except Exception:  # pragma: no cover - 诊断行不得影响查询本身
                baseline = None
                logger.warning("[codex-reset][commands] 读取基线失败", exc_info=True)

        if baseline is not None and not bool(getattr(baseline, "built", True)):
            lines.append("⚠️ 基线尚未建立（timeline 未成功采集），自动通知暂缓。")
            return lines

        rows_provider = getattr(self._read, "snapshot_rows", None)
        rows = rows_provider() if callable(rows_provider) else {}
        forecast_row = rows.get("forecast") if isinstance(rows, dict) else None
        if forecast_row is not None and not bool(getattr(forecast_row, "ok", True)):
            lines.append("⚠️ forecast 端点最近采集失败，概率与 Watch 可能不更新。")
        return lines

    def _render_forecast(self, now: datetime, tz: ZoneInfo) -> str:
        forecast = self._read.forecast()
        if forecast is None:
            return "\n".join(self._append_stale([NO_DATA_TEXT], now, tz))
        lines = [
            "📈【重置概率预测】",
            "",
            f"未来 24 小时：{int(forecast.rounded_24h)}%",
            f"未来 48 小时：{int(forecast.rounded_48h)}%",
            f"模型置信度：{confidence_cn(forecast.confidence)}",
            f"统计更新时间：{format_time(forecast.updated_at, tz)}",
            "",
            "⚠️ 这是独立站点实验性预测，不代表官方承诺。",
        ]
        return "\n".join(self._append_stale(lines, now, tz))

    async def _render_latest(self, now: datetime, tz: ZoneInfo) -> str:
        events = await self._repo.list_events(group_kind="reset", limit=1)
        if not events:
            return "\n".join(self._append_stale(["🛰️【最近事件】", "", "暂无已确认的重置记录。"], now, tz))
        event = events[0]
        payload = event.payload if isinstance(event.payload, dict) else {}
        summary = str(payload.get("summary") or "").strip() or "（站点未提供摘要）"
        lines = [
            "🛰️【最近一次已确认重置】",
            "",
            f"时间：{format_time(event.occurred_at, tz)}",
            f"摘要：{summary}",
        ]
        url = str(payload.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            lines.append(f"原始公告：{url}")
        return "\n".join(self._append_stale(lines, now, tz))

    def _render_credits(self, now: datetime, tz: ZoneInfo) -> str:
        rows = self._credit_rows()
        if not rows:
            empty = ["🎟️【额外重置权益】", "", "暂无权益动态记录。"]
            return "\n".join(self._append_stale(empty, now, tz))
        lines = ["🎟️【额外重置权益动态】", ""]
        for row in rows[:3]:
            day = row.date or format_day(row.announced_at, tz)
            state = banked_state_cn(row.banked_state)
            summary = (row.summary or "").strip()
            lines.append(f"· {day} {state}")
            if summary:
                lines.append(f"  {summary[:60]}")
        lines.append("")
        lines.append("说明：以上为 codex-reset.com 记录的公开动态，不代表个人账号一定到账。")
        return "\n".join(self._append_stale(lines, now, tz))

    def _credit_rows(self) -> list[TimelineEvent]:
        """合并 timeline / feed 的 credits 行，按 announced_at 倒序去重。"""
        seen: set[str] = set()
        rows: list[TimelineEvent] = []
        for snapshot in (self._read.timeline(), self._read.feed()):
            if snapshot is None:
                continue
            for event in snapshot.events:
                if event.group != "credits" or event.id in seen:
                    continue
                seen.add(event.id)
                rows.append(event)
        rows.sort(
            key=lambda item: (
                ensure_utc(item.announced_at or item.effective_at or _EPOCH),
                item.id,
            ),
            reverse=True,
        )
        return rows

    async def _render_history(
        self, req: CommandRequest, now: datetime, tz: ZoneInfo
    ) -> str:
        raw = req.args[0].strip() if req.args else ""
        limit = HISTORY_DEFAULT
        if raw:
            try:
                limit = int(raw)
            except ValueError:
                return HISTORY_USAGE
            if limit < 1:
                return HISTORY_USAGE
        truncated = limit > HISTORY_MAX
        limit = min(limit, HISTORY_MAX)
        events = await self._repo.list_events(
            group_kind="reset", limit=limit, exclude_corrections=True
        )
        if not events:
            empty = ["🗂️【确认重置历史】", "", "暂无已确认的重置记录。"]
            return "\n".join(self._append_stale(empty, now, tz))
        lines = [f"🗂️【最近 {len(events)} 条确认重置】", ""]
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            summary = str(payload.get("summary") or "").strip()
            label = KIND_LABELS.get(event.kind, event.kind)
            line = f"· {format_time(event.occurred_at, tz)} {label}"
            if summary:
                line += f"：{summary[:40]}"
            lines.append(line)
        if truncated:
            lines.append(f"（仅显示最近 {HISTORY_MAX} 条）")
        return "\n".join(self._append_stale(lines, now, tz))

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    async def _subscribe(
        self, req: CommandRequest, plugin: PluginSettings, tz: ZoneInfo
    ) -> str:
        if not req.is_group or not req.group_id:
            return GROUP_ONLY_TEXT
        existing = await self._repo.get_subscription(req.umo)
        if existing is not None:
            settings = resolve_group_settings(plugin, existing.override)
            return "本群已订阅：\n" + self._describe_settings(settings)
        subscriptions = await self._repo.list_subscriptions()
        if len(subscriptions) >= max(1, int(plugin.group_limit)):
            return f"订阅失败：已达群上限 {int(plugin.group_limit)} 个，请先取消其他群订阅。"
        row = await self._repo.upsert_subscription(
            umo=req.umo,
            platform=req.platform,
            group_id=req.group_id,
            group_name=req.group_name or f"群 {req.group_id}",
            override=GroupOverride(),
        )
        synced = await self._sync_sink(row)
        settings = resolve_group_settings(plugin, row.override)
        lines = ["✅ 订阅成功：本群将接收 Codex 重置相关推送。", "", self._describe_settings(settings)]
        if not synced:
            lines.append("")
            lines.append("提示：WebUI 配置卡片同步失败，配置已在数据库生效。")
        await self._audit(req, "subscribe", "ok")
        return "\n".join(lines)

    async def _unsubscribe(self, req: CommandRequest) -> str:
        existing = await self._repo.get_subscription(req.umo)
        if existing is None:
            return NOT_SUBSCRIBED_TEXT
        deleted = await self._repo.delete_subscription(req.umo)
        if not deleted:
            return NOT_SUBSCRIBED_TEXT
        await self._drop_sink(existing.subscription_id)
        await self._audit(req, "unsubscribe", "ok")
        return "✅ 已取消订阅：本群将不再接收任何自动推送。"

    async def _settings(self, req: CommandRequest, plugin: PluginSettings) -> str:
        row = await self._repo.get_subscription(req.umo)
        if row is None:
            return NOT_SUBSCRIBED_TEXT
        settings = resolve_group_settings(plugin, row.override)
        lines = ["⚙️【本群生效配置】", ""]
        for field, label in FIELD_LABELS.items():
            value = getattr(settings, field)
            if isinstance(value, bool):
                shown = "开启" if value else "关闭"
            else:
                shown = f"{int(value)}%"
            lines.append(f"{label}：{shown}（{self._source_of(row, field)}）")
        lines.append("")
        lines.append("修改：/cr set reset on|off；/cr set p24 0-100")
        return "\n".join(lines)

    async def _set(self, req: CommandRequest, plugin: PluginSettings) -> str:
        row = await self._repo.get_subscription(req.umo)
        if row is None:
            return NOT_SUBSCRIBED_TEXT
        if len(req.args) < 2:
            return SET_USAGE
        field_alias = req.args[0].strip().lower()
        value_text = req.args[1].strip()

        parsed: bool | int
        if field_alias in BOOL_ALIASES:
            lowered = value_text.lower()
            if lowered not in {"on", "off"}:
                return f"用法：/cr set {field_alias} on|off"
            parsed = lowered == "on"
            field = BOOL_ALIASES[field_alias]
        elif field_alias in THRESHOLD_ALIASES:
            try:
                parsed = int(value_text)
            except ValueError:
                return f"用法：/cr set {field_alias} 0-100 的整数（合法区间 0-100）"
            if not 0 <= parsed <= 100:
                return (
                    f"{field_alias}={value_text} 超出合法区间，已拒绝。"
                    f"用法：/cr set {field_alias} 0-100 的整数（合法区间 0-100）"
                )
            field = THRESHOLD_ALIASES[field_alias]
        else:
            return SET_USAGE

        values = row.override.as_sparse_dict()
        default_value = getattr(plugin.defaults, field)
        if parsed == default_value:
            values.pop(field, None)  # 等于全局默认 → 回到「继承」语义
        else:
            values[field] = parsed
        new_override = GroupOverride(**values)  # type: ignore[arg-type]
        updated = await self._repo.update_subscription_override(req.umo, new_override)
        if not updated:
            return NOT_SUBSCRIBED_TEXT
        refreshed = await self._repo.get_subscription(req.umo) or row
        await self._sync_sink(refreshed)
        await self._audit(req, f"set:{field}", "ok")

        settings = resolve_group_settings(plugin, refreshed.override)
        effective = getattr(settings, field)
        shown = ("开启" if effective else "关闭") if isinstance(effective, bool) else f"{int(effective)}%"
        return "\n".join(
            [
                f"✅ 已更新：{FIELD_LABELS.get(field, field)} → {shown}",
                f"生效值来源：{self._source_of(refreshed, field)}",
                "",
                "当前配置：",
                self._describe_settings(settings),
            ]
        )

    async def _groups(
        self, req: CommandRequest, plugin: PluginSettings, tz: ZoneInfo
    ) -> str:
        if req.is_group:
            return GROUPS_PRIVATE_TEXT
        health = await self._repo.subscription_health()
        if not health:
            return "📋【订阅群列表】\n\n暂无订阅群。"
        overrides = {
            row.subscription_id: row.override for row in await self._repo.list_subscriptions()
        }
        lines = ["📋【订阅群与推送健康】", ""]
        for index, item in enumerate(health[:GROUPS_MAX_ROWS], start=1):
            settings = resolve_group_settings(plugin, overrides.get(item["subscription_id"]))
            enabled = "".join(
                label for field, label in FLAG_LABELS if bool(getattr(settings, field, False))
            ) or "无"
            success = (
                format_time(item["last_success_at"], tz, with_zone=False)
                if item.get("last_success_at") is not None
                else "—"
            )
            failure = (
                format_time(item["last_failure_at"], tz, with_zone=False)
                if item.get("last_failure_at") is not None
                else "—"
            )
            name = item.get("group_name") or f"群 {item.get('group_id') or ''}"
            lines.append(
                f"{index}. {name}({item.get('group_id') or '—'})"
                f" 类别：{enabled}"
                f" 最近成功：{success} 最近失败：{failure}"
                f" uncertain：{int(item.get('uncertain_count') or 0)}"
            )
        if len(health) > GROUPS_MAX_ROWS:
            lines.append(f"（共 {len(health)} 个群，仅显示前 {GROUPS_MAX_ROWS} 个）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 图片命令（P09 / G09）
    # ------------------------------------------------------------------
    async def _report(
        self, req: CommandRequest, plugin: PluginSettings, tz: ZoneInfo
    ) -> CommandReply:
        """`/cr report`：手动生成当前概率预测图片（P09）。

        - 只读缓存，不触发上游请求，不绕开限流（AC-21）；
        - 不写 `daily_reports`，因此**不占用**当天自动日报的唯一发送权；
        - 不改变定时任务的发送状态。
        """
        if self._daily is None:
            return CommandReply(
                text=self._finalize("图片功能不可用：当前实例未启用预测图片渲染。"),
                error="report_unavailable",
            )
        if not req.is_group or not req.group_id:
            return CommandReply(text=self._finalize(REPORT_NOT_GROUP_TEXT), error="not_group")
        await self._audit(req, "report", "ok")
        png, payload = await self._daily.manual_report_png(now=self._clock.utcnow(), test=False)
        if png is None:
            if payload is not None:
                text = f"{REPORT_NO_DATA_TEXT}\n{render_daily_report_text(payload, tz=tz, settings=plugin)}"
                return CommandReply(text=self._finalize(text), error="render_failed")
            return CommandReply(text=self._finalize(REPORT_NO_DATA_TEXT), error="no_data")
        text = render_daily_report_text(payload or {}, tz=tz, settings=plugin)
        return CommandReply(text=self._finalize(text), image=png)

    async def _test(self, req: CommandRequest, plugin: PluginSettings, tz: ZoneInfo) -> CommandReply:
        """`/cr test [image]`（G09）：`image` 为预测图片发送测试，仍标注「测试」。

        测试消息**不入库、不建立投递**，也不影响真实事件与日报的去重状态。
        """
        args = [arg.strip().lower() for arg in req.args if arg.strip()]
        if args and args[0] not in {"image", "img", "图片"}:
            return CommandReply(text=self._finalize(TEST_USAGE), error="bad_args")
        if not args:
            return CommandReply(text=render_test(tz=tz, settings=plugin))
        if self._daily is None:
            return CommandReply(
                text=self._finalize("图片功能不可用：当前实例未启用预测图片渲染。"),
                error="report_unavailable",
            )
        if not req.is_group or not req.group_id:
            return CommandReply(
                text=self._finalize("请在群聊中执行 /cr test image。"), error="not_group"
            )
        await self._audit(req, "test:image", "ok")
        png, payload = await self._daily.manual_report_png(now=self._clock.utcnow(), test=True)
        if png is None:
            if payload is not None:
                text = (
                    "🧪【测试消息】图片生成失败，已降级为文本（样例，非真实事件）\n"
                    + render_daily_report_text({**payload, "test": True}, tz=tz, settings=plugin)
                )
                return CommandReply(text=self._finalize(text), error="render_failed")
            return CommandReply(text=self._finalize(REPORT_NO_DATA_TEXT), error="no_data")
        text = render_daily_report_text({**payload, "test": True}, tz=tz, settings=plugin)
        return CommandReply(text=self._finalize(text), image=png)

    # ------------------------------------------------------------------
    # 审批 / 审计 / 渲染辅助
    # ------------------------------------------------------------------
    async def _audit(
        self,
        req: CommandRequest,
        action: str,
        result: str,
        *,
        at: datetime | None = None,
    ) -> None:
        """写一条审计（失败绝不阻断命令）。"""
        moment = at or (
            ensure_utc(req.received_at) if req.received_at is not None else self._clock.utcnow()
        )
        try:
            await self._repo.audit(
                {
                    "operator_id": req.sender_id,
                    "umo": req.umo,
                    "action": action,
                    "result": result,
                    "at": moment,
                }
            )
        except Exception:  # 审计是旁路，绝不能影响指令响应
            logger.warning("[codex-reset][commands] audit 失败 action=%s", action, exc_info=True)

    async def _sync_sink(self, sub: SubscriptionRow) -> bool:
        """把订阅写入 WebUI 卡片；失败只记 warning（DB 仍是真源）。"""
        try:
            return bool(await self._sink.sync_group(sub))
        except Exception:
            logger.warning(
                "[codex-reset][commands] sync_group 失败 sub_id=%s", sub.subscription_id, exc_info=True
            )
            return False

    async def _drop_sink(self, subscription_id: str) -> bool:
        """移除卡片；失败只记 warning。"""
        try:
            return bool(await self._sink.drop_group(subscription_id))
        except Exception:
            logger.warning(
                "[codex-reset][commands] drop_group 失败 sub_id=%s",
                subscription_id,
                exc_info=True,
            )
            return False

    @staticmethod
    def _source_of(row: SubscriptionRow, field: str) -> str:
        """生效值来源：群覆盖 / 全局默认。"""
        if field not in OVERRIDE_FIELDS:
            return "全局默认"
        return "全局默认" if getattr(row.override, field) is None else "群覆盖"

    @staticmethod
    def _describe_settings(settings: GroupSettings) -> str:
        """6 个字段的单行回显（订阅成功 / `/cr set` 复用）。"""
        flags = "、".join(
            f"{label}{'开' if bool(getattr(settings, field, False)) else '关'}"
            for field, label in FLAG_LABELS
        )
        return (
            f"{flags}；24h 阈值 {int(settings.threshold_24h)}%；48h 阈值 {int(settings.threshold_48h)}%"
        )

    def _append_stale(
        self, lines: list[str], now: datetime, tz: ZoneInfo
    ) -> list[str]:
        """陈旧时追加一行提示（PRD §4：查询一律带数据新鲜度）。"""
        latest, stale = self._read.freshness(now)
        if stale:
            lines.append("")
            lines.append(self._stale_line(latest, tz))
        return lines

    @staticmethod
    def _stale_line(latest: datetime | None, tz: ZoneInfo) -> str:
        """陈旧提示行（PRD §4「数据可能已过时」）。"""
        when = format_time(latest, tz, with_zone=False) if latest is not None else "未知"
        return f"⚠️ 数据可能已过时（最近采集：{when}）。"

    def _finalize(self, text: str) -> str:
        """统一追加署名行并按 U-4 截断到 1200 字符。"""
        body = (text or "").rstrip()
        if not body:
            return body
        combined = body if SOURCE_LINE in body else f"{body}\n\n{SOURCE_LINE}"
        if len(combined) <= MAX_MESSAGE_CHARS:
            return combined
        keep = MAX_MESSAGE_CHARS - len(TRUNCATED_SUFFIX)
        return combined[:keep] + TRUNCATED_SUFFIX


__all__ = [
    "ADMIN_COMMANDS",
    "ADMIN_DENIED_TEXT",
    "CommandService",
    "DailyReportProvider",
    "GroupConfigSink",
    "QUERY_COMMANDS",
    "RATE_LIMITS",
]
