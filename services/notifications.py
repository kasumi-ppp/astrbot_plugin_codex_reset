"""消息模板（逐字冻结）+ 投递 Worker。

模板约束（`docs/PRD-confirmation.md` §6，验收 AC-20 / AC-05 / AC-08 / AC-11 / AC-12）：
- 每条消息都有一行 `数据来源：https://codex-reset.com/`，**不可关闭**（`show_source_link=false`
  只记 warning，仍然渲染）；
- `原始公告 / 原始消息 / 原始来源` 三行只在存在**安全** http(s) 链接时出现；
- 只有 F01 可以出现「已确认」「已记录一次全球额度重置」；F03 禁止「已确认」「即将重置」「必将」；
- Watch 无窗口时固定输出「预计窗口：暂无明确预计时段」，绝不编造小时数；
- 单条消息 ≤1200 字符，超出截断并追加「（内容过长已截断）」。

`DeliveryWorker` 是唯一有 I/O 的类：认领 → 门控 → 渲染 → 发送 → 落库。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .clock import Clock, ensure_utc
from .config import resolve_group_settings, timezone_cn_name
from .errors import SITE_URL, ErrorCode
from .models import (
    DeliveryTask,
    ForecastDecision,
    GroupSettings,
    LogicalEvent,
    PluginSettings,
    SendFn,
)
from .parser import safe_url

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要，避免运行期同层循环导入
    from .repository import Repository

logger = logging.getLogger(__name__)

SOURCE_LINE = f"数据来源：{SITE_URL}"
"""署名行（AC-20）：`notifications.py` 唯一定义，其他模块只允许引用。"""

SITE_HOST = "codex-reset.com"
"""站点主机名（文案里统一称「codex-reset.com 记录 / 监控站」）。"""

MAX_MESSAGE_CHARS = 1200
"""单条消息长度上限（U-4），超出截断而非分段发送（保去重简单）。"""

TRUNCATED_SUFFIX = "（内容过长已截断）"

NO_WINDOW_TEXT = "暂无明确预计时段"
"""Watch 无窗口时的固定表述（AC-08）。"""

CONFIDENCE_CN: dict[str, str] = {"low": "低", "medium": "中", "high": "高"}

BANKED_STATE_CN: dict[str, str] = {
    "announced": "已宣布（尚不能视为所有账号已到账）",
    "arriving": "正在发放（网站监测账户未全部到账）",
    "available": "网站记录该权益已可用（不代表所有账号可用）",
}

#: 事件类别 → 群级开关字段（`correction` 跟随原类别，故按 kind 判定）
CATEGORY_FLAGS: dict[str, str] = {
    "reset": "enable_confirmed_reset",
    "watch": "enable_watch",
    "forecast": "enable_forecast",
    "credits": "enable_credits",
}

#: 投递失败的错误码（`adapter_unavailable` 表示平台找不到会话，可重试）
RETRYABLE_ERROR = ErrorCode.ADAPTER_UNAVAILABLE

#: 更正事件的 `payload["reason"]` 取值（分类器写入，模板据此切标题）
CORRECTION_REASONS: frozenset[str] = frozenset(
    {"expired", "corrected", "window_elapsed", "downgraded", "credits_state"}
)


def is_correction(payload: Mapping[str, Any]) -> bool:
    """payload 是否属于 F07 更正事件（更正复用原 `kind`，只靠 reason 区分）。"""
    reason = payload.get("reason")
    return isinstance(reason, str) and reason in CORRECTION_REASONS


# ---------------------------------------------------------------------------
# 基础格式化（纯函数）
# ---------------------------------------------------------------------------
def _tz_name(tz: Any) -> str:
    """取时区的 IANA 名；无法取得时退回 `str(tz)`。"""
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return str(tz)


def format_time(dt: datetime | None, tz: ZoneInfo, *, with_zone: bool = True) -> str:
    """时间格式化：`2026-10-20 14:33（北京时间）`（D-07：转换只发生在渲染层）。"""
    if dt is None:
        return ""
    local = ensure_utc(dt).astimezone(tz)
    text = local.strftime("%Y-%m-%d %H:%M")
    if with_zone:
        return f"{text}（{timezone_cn_name(_tz_name(tz))}）"
    return text


def format_day(dt: datetime | None, tz: ZoneInfo) -> str:
    """日期格式化：`YYYY-MM-DD`（缺失返回空串，调用方整行省略）。"""
    if dt is None:
        return ""
    return ensure_utc(dt).astimezone(tz).strftime("%Y-%m-%d")


def confidence_cn(value: str | None) -> str:
    """置信度中文：`low→低 / medium→中 / high→高`，未识别 → 原串 + 提示。"""
    if not value:
        return "未知"
    mapped = CONFIDENCE_CN.get(value)
    if mapped is not None:
        return mapped
    return f"{value}（未识别置信度）"


def banked_state_cn(value: str | None) -> str:
    """权益状态文案（PRD §6.4 冻结，逐字匹配）。"""
    if not value:
        return "未知"
    return BANKED_STATE_CN.get(value, f"{value}（未识别状态）")


def _safe_url(value: Any) -> str | None:
    """渲染层链接校验：与 `parser.safe_url` **完全同口径**（AC-20「如无链接则省略原文行」）。

    P2-02：早期这里有一份「只判协议 + netloc」的重复实现，会让含空白/控制字符的 URL
    通过校验——含 `\\n` 的链接甚至能在消息体内注入第二行「数据来源：」。现在直接复用
    解析层实现（伪协议 / 无 host / 含空白 / 控制字符 / 超长 一律返回 None），
    保证「parser 清洗过的才可能出现在渲染层」这一纵深防御不出现缺口。
    """
    return safe_url(value)


def _parse_time(value: Any) -> datetime | None:
    """从 payload 里取时间（ISO 字符串或 datetime）。"""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)
    return None


def _payload_time(payload: Mapping[str, Any], key: str) -> datetime | None:
    return _parse_time(payload.get(key))


def _source_line(settings: Any) -> str:
    """署名行；`show_source_link=False` 不被支持（只记 warning，仍然渲染）。"""
    if settings is not None and getattr(settings, "show_source_link", True) is False:
        logger.warning("[codex-reset][notify] show_source_link=false 不被支持，署名行仍然渲染")
    return SOURCE_LINE


def _truncate(text: str) -> str:
    """按 U-4 截断到 1200 字符并追加提示。"""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    keep = MAX_MESSAGE_CHARS - len(TRUNCATED_SUFFIX)
    return text[:keep] + TRUNCATED_SUFFIX


def _compose(head: Sequence[str], footer: Sequence[str]) -> str:
    """`head` 与 `footer` 之间固定一个空行，`footer` 至少包含署名行。"""
    return _truncate("\n".join([*head, "", *footer]))


# ---------------------------------------------------------------------------
# 窗口文案（Watch / 更正共用，绝不自造时间）
# ---------------------------------------------------------------------------
def _window_of(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = payload.get("window")
    return raw if isinstance(raw, Mapping) else None


def _window_span(window: Mapping[str, Any] | None, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """取 (start, end)；任一端缺失返回 None（不猜另一端）。"""
    if not isinstance(window, Mapping):
        return None
    start = _parse_time(window.get("start_at"))
    end = _parse_time(window.get("end_at"))
    if start is None or end is None:
        return None
    return ensure_utc(start), ensure_utc(end)


def _watch_window_text(payload: Mapping[str, Any], tz: ZoneInfo) -> str:
    """`预计窗口` 的取值：无窗口 → 固定句；同日 → 今日 HH:MM—HH:MM；跨日 → 全量。"""
    window = _window_of(payload)
    if window is None:
        return NO_WINDOW_TEXT
    label = str(window.get("label") or "").strip()
    span = _window_span(window, tz)
    if span is None:
        if label:
            return label
        single = _parse_time(window.get("start_at")) or _parse_time(window.get("end_at"))
        if single is not None:
            return format_time(single, tz)
        return NO_WINDOW_TEXT
    start, end = span
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    zone = timezone_cn_name(_tz_name(tz))
    if local_start.date() == local_end.date():
        return f"今日 {local_start:%H:%M}—{local_end:%H:%M}（{zone}）"
    return f"{local_start:%Y-%m-%d %H:%M} — {local_end:%Y-%m-%d %H:%M}（{zone}）"


def _window_brief(payload: Mapping[str, Any], tz: ZoneInfo) -> str:
    """更正消息里的短窗口：`20:00—22:00` / 跨日全量 / 空串（不编造）。"""
    window = _window_of(payload)
    span = _window_span(window, tz)
    if span is None:
        return ""
    start, end = span
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    if local_start.date() == local_end.date():
        return f"{local_start:%H:%M}—{local_end:%H:%M}"
    return f"{local_start:%Y-%m-%d %H:%M} — {local_end:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# 模板（PRD §6 逐字）
# ---------------------------------------------------------------------------
def render_reset(payload: Mapping[str, Any], *, tz: ZoneInfo, settings: Any) -> str:
    """F01 确认重置（PRD §6.1）。"""
    head = [
        "✅【Codex 全球重置已确认】",
        "",
        "codex-reset.com 已记录一次全球额度重置。",
    ]
    occurred = _payload_time(payload, "occurred_at")
    if occurred is not None:
        head.append(f"确认记录时间：{format_time(occurred, tz)}")
    head.append("提醒：个人账号额度生效可能存在延迟，请以 Codex 实际状态为准。")

    footer: list[str] = []
    url = _safe_url(payload.get("url"))
    if url:
        footer.append(f"原始公告：{url}")
    footer.append(_source_line(settings))
    return _compose(head, footer)


def render_watch(payload: Mapping[str, Any], *, tz: ZoneInfo, settings: Any) -> str:
    """F02 提前预警（PRD §6.2）：措辞必须「非确认」。"""
    state = str(payload.get("state") or "active")
    head = [
        "⚠️【Codex 全球重置提前预警】",
        "",
        "检测到有效重置信号（尚未确认完成）。",
        f"预计窗口：{_watch_window_text(payload, tz)}",
        f"当前状态：监控站 Watch {state}",
        "提醒：这不是已重置通知，到时仍需等待确认。",
    ]
    if str(payload.get("pending") or "") == "on_its_way":
        head.append("补充：网站尚未测得实际重置，确认待定。")

    footer: list[str] = []
    url = _safe_url(payload.get("url"))
    if url:
        footer.append(f"原始消息：{url}")
    footer.append(_source_line(settings))
    return _compose(head, footer)


def render_forecast_push(decision: ForecastDecision, *, tz: ZoneInfo, settings: Any) -> str:
    """F03 概率提醒（PRD §6.3）：必须含「实验性」「不代表官方承诺」。"""
    if decision.window_kind == "h48":
        title = "📈【未来 48 小时重置预测】"
    else:
        title = "📈【Codex 重置概率升高】"
    head = [
        title,
        "",
        f"未来 24 小时：{int(decision.rounded_24h)}%",
        f"未来 48 小时：{int(decision.rounded_48h)}%",
        f"模型置信度：{confidence_cn(decision.confidence)}",
    ]
    if decision.updated_at is not None:
        head.append(f"统计更新时间：{format_time(decision.updated_at, tz)}")

    footer = ["⚠️ 这是独立站点实验性预测，不代表官方承诺。", _source_line(settings)]
    return _compose(head, footer)


def render_credits(payload: Mapping[str, Any], *, tz: ZoneInfo, settings: Any) -> str:
    """F04 额外重置权益（PRD §6.4 + D-10）。"""
    state = str(payload.get("state") or payload.get("banked_state") or "unknown")
    if payload.get("after_announcement") is False:
        # D-10：监测账户观测事实，不得输出「已宣布」
        status_text = "网站监测账户已观测到该权益到账"
    else:
        status_text = banked_state_cn(state)

    day_text = str(payload.get("date") or "").strip()
    if not day_text:
        day_text = format_day(_payload_time(payload, "occurred_at"), tz)

    head = ["🎟️【Codex 额外重置权益动态】", "", "事件：Banked Reset", f"状态：{status_text}"]
    if day_text:
        head.append(f"更新日期：{day_text}")
    head.append("详情：请查看原始消息与个人账号实际信息。")

    footer: list[str] = []
    url = _safe_url(payload.get("url"))
    if url:
        footer.append(f"原始来源：{url}")
    footer.append(_source_line(settings))
    return _compose(head, footer)


def render_correction(payload: Mapping[str, Any], *, tz: ZoneInfo, settings: Any) -> str:
    """F07 更正（PRD §6.5）：预告过期 / 网站更正 / 重置降级 / 权益回退。"""
    reason = str(payload.get("reason") or "")
    state_text = str(payload.get("new_state") or payload.get("state") or "none")

    if reason == "downgraded":
        head = [
            "🔁【更正】此前记录的重置已被网站更新",
            "",
            f"网站已将此前记录状态调整为 {state_text}，请以此为准，本插件不再重复推送该事件。",
        ]
    elif reason == "corrected":
        head = [
            "🔁【更正】此前的重置预告已被网站更正",
            "",
            "网站已更正此前的重置信号，请以此为准，本插件不再重复推送该事件。",
            "原预告状态：已失效（corrected）",
        ]
    elif reason in {"expired", "window_elapsed"}:
        brief = _window_brief(payload, tz)
        subject = f"此前预告（{brief}）" if brief else "此前预告"
        head = [
            "🔁【更正】此前的重置预告未获确认",
            "",
            f"{subject}已过期，截至现在未见确认重置记录。",
            "原预告状态：已失效（expired）",
            "说明：本次更正仅同步网站数据变化，不代表重置不会发生。",
        ]
    elif reason == "credits_state":
        head = [
            f"🔁【更正】额外重置权益状态已更新为 {banked_state_cn(state_text)}",
            "",
            "网站已更新该权益状态，请以实际账号信息为准。",
        ]
    else:
        head = [
            "🔁【更正】网站更新了此前的事件状态",
            "",
            "网站已更新此前的记录，请以此为准，本插件不再重复推送该事件。",
        ]

    footer: list[str] = []
    url = _safe_url(payload.get("url"))
    if url:
        footer.append(f"原始消息：{url}")
    footer.append(_source_line(settings))
    return _compose(head, footer)


def render_catchup_summary(
    events: Sequence[LogicalEvent],
    *,
    tz: ZoneInfo,
    settings: Any,
) -> str:
    """启动补发摘要（Q-4：每群每次启动最多 1 条）。"""
    max_hours = int(getattr(settings, "max_catchup_hours", 6) or 6)
    head = [f"📌【补发摘要】共 {len(events)} 条更新", ""]
    for event in events:
        head.append(f"· {format_time(event.occurred_at, tz)} {_event_label(event)}")
    head.extend(
        [
            "",
            f"补发范围仅限最近 {max_hours} 小时内的全球重置与权益状态变化，"
            "每群每次启动最多 1 条。",
        ]
    )
    return _compose(head, [_source_line(settings)])


def _event_label(event: LogicalEvent) -> str:
    """摘要里的事件短标签（避免在 F03/非 F01 之外出现「已确认」字样）。"""
    if event.kind == "reset":
        return "Codex 全球重置（网站确认记录）"
    if event.kind == "credits":
        state = str(event.payload.get("state") or "")
        return f"额外重置权益：{banked_state_cn(state)}"
    return str(event.kind)


def render_test(*, tz: ZoneInfo, settings: Any) -> str:
    """`/cr test`（PRD §6.6）：样例文本，固定时间，不改动任何真实状态。"""
    sample = datetime(2026, 1, 1, 0, 0, tzinfo=tz)
    head = [
        "🧪【测试消息】以下为样例，非真实事件",
        "",
        "✅【Codex 全球重置已确认】（示例）",
        "codex-reset.com 已记录一次全球额度重置。",
        f"确认记录时间：{format_time(sample, tz)}",
        "",
        "本次为 /cr test 生成，不改变任何真实事件状态。",
    ]
    return _compose(head, [_source_line(settings)])


# ---------------------------------------------------------------------------
# 预测事件 payload ↔ 决策对象（pipeline 入队 / worker 渲染共用，保证字段一致）
# ---------------------------------------------------------------------------
def decision_payload(decision: ForecastDecision) -> dict[str, Any]:
    """`ForecastDecision` → 事件/投递 payload（`Decision` 无法直接 JSON 化）。"""
    return {
        "window_kind": decision.window_kind,
        "rounded_24h": int(decision.rounded_24h),
        "rounded_48h": int(decision.rounded_48h),
        "confidence": decision.confidence,
        "updated_at": ensure_utc(decision.updated_at).isoformat(),
    }


def decision_from_payload(
    payload: Mapping[str, Any],
    *,
    logical_id: str,
    state_version: str,
) -> ForecastDecision | None:
    """payload → `ForecastDecision`；字段不全时返回 None（worker 记 `unknown_kind`）。"""
    window_kind = payload.get("window_kind")
    if window_kind not in {"h24", "h48", "both"}:
        return None
    updated_at = _parse_time(payload.get("updated_at"))
    if updated_at is None:
        return None
    try:
        rounded_24h = int(payload.get("rounded_24h") or 0)
        rounded_48h = int(payload.get("rounded_48h") or 0)
    except (TypeError, ValueError):
        return None
    confidence = payload.get("confidence")
    return ForecastDecision(
        logical_id=logical_id,
        state_version=state_version,
        window_kind=str(window_kind),
        rounded_24h=rounded_24h,
        rounded_48h=rounded_48h,
        confidence=str(confidence) if confidence is not None else "unknown",
        updated_at=updated_at,
    )


def _events_from_payload(payload: Mapping[str, Any]) -> list[LogicalEvent]:
    """补发摘要 payload → 事件列表（`payload["events"]`，批次 C 由 pipeline 组装）。"""
    raw = payload.get("events")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    events: list[LogicalEvent] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        occurred = _parse_time(item.get("occurred_at"))
        if occurred is None:
            continue
        events.append(
            LogicalEvent(
                logical_id=str(item.get("logical_id") or "catchup:item"),
                kind=str(item.get("kind") or "reset"),
                group_kind=str(item.get("group_kind") or item.get("kind") or "reset"),
                state_version=str(item.get("state_version") or "confirmed"),
                occurred_at=occurred,
                payload=dict(item.get("payload") or {}),
            )
        )
    return events


# ---------------------------------------------------------------------------
# 投递 Worker
# ---------------------------------------------------------------------------
DEFAULT_SEND_TIMEOUT_SECONDS = 30.0
"""单条发送的超时上限（F08「超时/中断记 uncertain 不自动重发」）。

没有这个上限时，平台适配器静默挂起会把 `run_once` 与整条 `delivery_loop` 一起阻塞，
后续消息全部积压（而 `poll_loop` 独立存活，`/cr status` 看起来完全正常）。
"""


class DeliveryWorker:
    """队列消费者：`claim → 门控 → 渲染 → send → mark_*`，每 5s 一轮。"""

    def __init__(
        self,
        *,
        repo: Repository,
        send: SendFn,
        clock: Clock,
        settings_provider: Callable[[], PluginSettings],
        tz_provider: Callable[[], ZoneInfo],
        batch: int = 10,
        retry_delays: tuple[int, ...] = (0, 30, 120),
        interval_seconds: float = 5.0,
        send_timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    ) -> None:
        """依赖全部由外部注入，Worker 自身不读配置、不建连接。"""
        self._repo = repo
        self._send = send
        self._clock = clock
        self._settings_provider = settings_provider
        self._tz_provider = tz_provider
        self._batch = max(1, int(batch))
        self._retry_delays = tuple(int(delay) for delay in retry_delays) or (0,)
        self._interval = float(interval_seconds)
        self._send_timeout = max(0.01, float(send_timeout))
        self._stopped = False

    # -- 主循环 ---------------------------------------------------------
    async def run_once(self, now: datetime) -> int:
        """处理一批到期投递，返回成功发送条数（异常不外抛）。"""
        moment = ensure_utc(now)
        tasks = await self._repo.claim_pending_deliveries(moment, self._batch)
        delivered = 0
        for task in tasks:
            try:
                if await self._process(task, moment):
                    delivered += 1
            except Exception:  # 单条异常不得阻断整批（AC-18 同类要求）
                logger.exception(
                    "[codex-reset][delivery] 处理异常 id=%s kind=%s", task.id, task.kind
                )
                await self._fail_safely(task, moment)
        if tasks:
            logger.info(
                "[codex-reset][delivery] batch claimed=%s delivered=%s", len(tasks), delivered
            )
        return delivered

    async def run(self) -> None:
        """常驻循环：异常一律捕获记日志，绝不冒泡到 AstrBot 事件循环。"""
        while not self._stopped:
            try:
                await self.run_once(self._clock.utcnow())
            except asyncio.CancelledError:  # pragma: no cover - 由上层取消
                raise
            except Exception:  # pragma: no cover - 防御式兜底
                logger.exception("[codex-reset][delivery] run_once 失败")
            try:
                await self._clock.sleep(self._interval)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # pragma: no cover
                logger.exception("[codex-reset][delivery] sleep 失败")

    def stop(self) -> None:
        """请求退出 `run()`（不打断进行中的 `run_once`）。"""
        self._stopped = True

    # -- 单条处理 -------------------------------------------------------
    async def _process(self, task: DeliveryTask, now: datetime) -> bool:
        """返回 True 表示已送达。"""
        sub = await self._repo.get_subscription(task.umo)
        if sub is None or not sub.enabled:
            await self._repo.mark_skipped(task, now=now, reason="subscription_inactive")
            return False

        plugin = self._settings_provider()
        settings = resolve_group_settings(plugin, sub.override)
        if not self._category_enabled(task.kind, settings):
            await self._repo.mark_skipped(task, now=now, reason="category_disabled")
            return False

        if task.kind == "catchup":
            events = [
                event for event in _events_from_payload(task.payload)
                if self._category_enabled(event.kind, settings)
            ]
            if not events:
                await self._repo.mark_skipped(task, now=now, reason="category_disabled")
                return False
            text = render_catchup_summary(events, tz=self._tz_provider(), settings=plugin)
        else:
            text = self._render(task, tz=self._tz_provider(), settings=plugin)

        if text is None:
            # 未知 kind / payload 残缺：重试不会变好，直接终态 failed
            await self._repo.mark_failed(
                task,
                now=now,
                error_code="unknown_kind",
                next_attempt_at=None,
                terminal=True,
            )
            return False

        at_all = task.kind == "reset" and bool(getattr(plugin, "allow_at_all", False))
        try:
            # P1-02：单条发送必须有上限，否则适配器挂起会阻塞整条投递链
            accepted = await asyncio.wait_for(
                self._send(task.umo, text, at_all), timeout=self._send_timeout
            )
        except TimeoutError:
            # F08：超时/中断记 uncertain，**不重发**（避免重复刷屏）
            # Python 3.11+ 的 `asyncio.TimeoutError` 即内置 `TimeoutError`，
            # 因此 `asyncio.wait_for` 超时与平台自身抛出的超时走同一分支。
            logger.warning(
                "[codex-reset][delivery] 发送超时 id=%s kind=%s timeout=%.1fs -> uncertain",
                task.id,
                task.kind,
                self._send_timeout,
            )
            await self._repo.mark_uncertain(task, now=now, error_code=ErrorCode.TIMEOUT)
            return False
        except Exception:
            logger.exception(
                "[codex-reset][delivery] send 异常 id=%s kind=%s", task.id, task.kind
            )
            await self._repo.mark_failed(
                task,
                now=now,
                error_code=ErrorCode.ADAPTER_EXCEPTION,
                next_attempt_at=self._next_attempt(now, task),
            )
            return False

        if accepted:
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            await self._repo.mark_delivered(task, now=now, message_hash=digest)
            return True

        await self._repo.mark_failed(
            task,
            now=now,
            error_code=RETRYABLE_ERROR,
            next_attempt_at=self._next_attempt(now, task),
        )
        return False

    @staticmethod
    def _category_enabled(kind: str, settings: GroupSettings) -> bool:
        """类别门控：correction 跟随原类别；补发摘要只在 reset/credits 至少开启一个时发。"""
        if kind == "catchup":
            return bool(settings.enable_confirmed_reset or settings.enable_credits)
        flag = CATEGORY_FLAGS.get(kind)
        if flag is None:
            return True  # 未知 kind 交给渲染层判 failed
        return bool(getattr(settings, flag, False))

    def _next_attempt(self, now: datetime, task: DeliveryTask) -> datetime | None:
        """按 `retry_delays` 计算下一次尝试时间（`attempt` 在认领时已 +1）。"""
        index = min(max(int(task.attempt), 1), len(self._retry_delays) - 1)
        return ensure_utc(now) + timedelta(seconds=self._retry_delays[index])

    def _render(self, task: DeliveryTask, *, tz: ZoneInfo, settings: PluginSettings) -> str | None:
        """按 kind 选模板；未知 kind 返回 None（调用方记 failed/unknown_kind）。

        更正事件复用原事件的 `kind`（类别门控才能「跟随原类别」），靠 payload 里的
        `reason` 区分，因此必须在 kind 分派**之前**判断。
        """
        if is_correction(task.payload):
            return render_correction(task.payload, tz=tz, settings=settings)
        if task.kind == "reset":
            return render_reset(task.payload, tz=tz, settings=settings)
        if task.kind == "watch":
            return render_watch(task.payload, tz=tz, settings=settings)
        if task.kind == "forecast":
            decision = decision_from_payload(
                task.payload, logical_id=task.logical_id, state_version=task.state_version
            )
            if decision is None:
                return None
            return render_forecast_push(decision, tz=tz, settings=settings)
        if task.kind == "credits":
            return render_credits(task.payload, tz=tz, settings=settings)
        if task.kind in {"correction", "correction_watch", "correction_reset"}:
            return render_correction(task.payload, tz=tz, settings=settings)
        if task.kind == "catchup":
            return render_catchup_summary(
                _events_from_payload(task.payload), tz=tz, settings=settings
            )
        return None

    async def _fail_safely(self, task: DeliveryTask, now: datetime) -> None:
        """兜底落库：即使处理逻辑抛异常也要让任务离开 `inflight`。"""
        try:
            await self._repo.mark_failed(
                task,
                now=now,
                error_code=ErrorCode.ADAPTER_EXCEPTION,
                next_attempt_at=self._next_attempt(now, task),
            )
        except Exception:  # pragma: no cover - 数据库已不可用时只能放弃
            logger.exception("[codex-reset][delivery] 兜底落库失败 id=%s", task.id)


__all__ = [
    "BANKED_STATE_CN",
    "CATEGORY_FLAGS",
    "CONFIDENCE_CN",
    "CORRECTION_REASONS",
    "DeliveryWorker",
    "MAX_MESSAGE_CHARS",
    "NO_WINDOW_TEXT",
    "RETRYABLE_ERROR",
    "SITE_HOST",
    "SOURCE_LINE",
    "TRUNCATED_SUFFIX",
    "banked_state_cn",
    "confidence_cn",
    "decision_from_payload",
    "decision_payload",
    "format_day",
    "format_time",
    "is_correction",
    "render_catchup_summary",
    "render_correction",
    "render_credits",
    "render_forecast_push",
    "render_reset",
    "render_test",
    "render_watch",
]
