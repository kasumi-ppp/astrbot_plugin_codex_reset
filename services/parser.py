"""三端点 JSON 解析：fail-closed + 逐字段降级（AC-23）。

设计原则：
- 只有「无法建立任何可用证据」的字段缺失才整份失败（`schema_error`）；
- 其余问题一律「降级 + `warnings` 累积」，保证插件不因上游字段演进崩溃；
- 日志只记录字段名与事件 id，绝不记录聊天正文（`[codex-reset][parser]` 前缀）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .errors import ErrorCode
from .models import (
    AlertWindow,
    BankedObservation,
    FeedSnapshot,
    ForecastSnapshot,
    LatestAlert,
    ParseResult,
    TimelineEvent,
    TimelineSnapshot,
)

logger = logging.getLogger(__name__)

GROUP_KINDS: frozenset[str] = frozenset({"reset", "credits", "boost", "unlock"})
ANNOUNCEMENT_STATES: frozenset[str] = frozenset({"announced", "none"})
BANKED_STATES: frozenset[str] = frozenset({"announced", "arriving", "available", "unknown"})
ALERT_KINDS: frozenset[str] = frozenset({"watch", "reset"})
ALERT_STATES: frozenset[str] = frozenset({"active", "expired", "confirmed"})
ALERT_PENDING_STATES: frozenset[str] = frozenset({"done", "on_its_way"})
CONFIDENCE_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})
MAX_URL_LENGTH = 2048


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _warn(warnings: list[str], message: str) -> None:
    """记录一条解析告警（同时写结构化日志，便于现场排查）。"""
    warnings.append(message)
    logger.warning("[codex-reset][parser] %s", message)


def parse_iso(value: Any) -> datetime | None:
    """解析 ISO8601 时间并归一化为 UTC；非法输入返回 `None`（绝不抛异常）。"""
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            candidate = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def coerce_enum(value: Any, allowed: Iterable[str], default: str | None) -> tuple[str | None, bool]:
    """枚举收敛：返回 `(值, 是否被识别)`；未识别时保留原串（若为字符串）。"""
    allowed_set = frozenset(allowed)
    if isinstance(value, str):
        text = value.strip()
        if text in allowed_set:
            return text, True
        if text:
            return text, False
        return default, False
    return default, False


def safe_url(value: Any) -> str | None:
    """安全链接：只接受 http/https，拒绝伪协议、控制字符与超长串。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_URL_LENGTH:
        return None
    for char in text:
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return text


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _probability(value: Any, field_name: str, warnings: list[str]) -> int:
    """概率取整数值；非 int → 0 + warning（不整体失败，`last_reset_at` 仍是有效证据）。"""
    if isinstance(value, bool):
        _warn(warnings, f"{field_name} 为布尔值，按 0 处理")
        return 0
    if isinstance(value, int):
        clamped = max(0, min(100, value))
        if clamped != value:
            _warn(warnings, f"{field_name}={value} 超出 0-100，已收敛为 {clamped}")
        return clamped
    if isinstance(value, float) and value.is_integer():
        return _probability(int(value), field_name, warnings)
    _warn(warnings, f"{field_name} 非整数（{value!r}），按 0 处理")
    return 0


# ---------------------------------------------------------------------------
# latest_alert
# ---------------------------------------------------------------------------
def parse_alert_window(raw: Any, warnings: list[str]) -> AlertWindow | None:
    """解析 `latest_alert.window`；缺失或结构非法返回 None。"""
    if not isinstance(raw, Mapping):
        return None
    label = _nonempty_str(raw.get("label")) or ""
    start_at = parse_iso(raw.get("start_at"))
    end_at = parse_iso(raw.get("end_at"))
    target_at = parse_iso(raw.get("target_at"))
    if raw.get("start_at") is not None and start_at is None:
        _warn(warnings, "latest_alert.window.start_at 无法解析，已丢弃")
    if raw.get("end_at") is not None and end_at is None:
        _warn(warnings, "latest_alert.window.end_at 无法解析，已丢弃")
    time_zone = _nonempty_str(raw.get("time_zone"))
    target_kind = _nonempty_str(raw.get("target_kind"))
    if not label and start_at is None and end_at is None and target_at is None:
        return None
    return AlertWindow(
        label=label,
        start_at=start_at,
        end_at=end_at,
        time_zone=time_zone,
        target_kind=target_kind,
        target_at=target_at,
    )


def parse_latest_alert(raw: Any) -> tuple[LatestAlert | None, tuple[str, ...]]:
    """解析 `forecast.latest_alert`；缺 `id/kind/state` 直接丢弃该 alert（+warning）。"""
    warnings: list[str] = []
    if raw is None:
        return None, ()
    if not isinstance(raw, Mapping):
        _warn(warnings, "latest_alert 不是 JSON 对象，已丢弃")
        return None, tuple(warnings)

    alert_id = _nonempty_str(raw.get("id"))
    missing = [field for field in ("id", "kind", "state") if _nonempty_str(raw.get(field)) is None]
    if missing or alert_id is None:
        _warn(warnings, f"latest_alert 缺少必填字段 {missing or ['id']}，已丢弃该 alert")
        return None, tuple(warnings)

    kind, kind_known = coerce_enum(raw.get("kind"), ALERT_KINDS, "watch")
    if not kind_known:
        _warn(warnings, f"latest_alert.kind={kind!r} 未识别，保留原值（不视为 watch/reset）")
    state, state_known = coerce_enum(raw.get("state"), ALERT_STATES, "active")
    if not state_known:
        _warn(warnings, f"latest_alert.state={state!r} 未识别，保留原值")

    pending_raw = _nonempty_str(raw.get("pending"))
    pending: str | None = None
    if pending_raw is not None:
        if pending_raw in ALERT_PENDING_STATES:
            pending = pending_raw
        else:
            _warn(warnings, f"latest_alert.pending={pending_raw!r} 未识别，已忽略")

    corrected = raw.get("corrected") is True

    alert = LatestAlert(
        id=alert_id,
        kind=kind or "watch",
        state=state or "active",
        summary=_str_or_none(raw.get("summary")) or "",
        source_at=parse_iso(raw.get("source_at")),
        url=safe_url(raw.get("url")),
        score=_int_or_none(raw.get("score")),
        window=parse_alert_window(raw.get("window"), warnings),
        pending=pending,
        corrected=corrected,
        confirmation_source=_nonempty_str(raw.get("confirmation_source")),
        observed_at=parse_iso(raw.get("observed_at")),
        confirmed_at=parse_iso(raw.get("confirmed_at")),
        confirmation_url=safe_url(raw.get("confirmation_url")),
    )
    if alert.url is None and _nonempty_str(raw.get("url")) is not None:
        _warn(warnings, f"latest_alert.id={alert_id} 的 url 不安全或非法，已清空")
    return alert, tuple(warnings)


# ---------------------------------------------------------------------------
# timeline / feed 事件
# ---------------------------------------------------------------------------
def parse_event(row: Mapping[str, Any]) -> tuple[TimelineEvent | None, tuple[str, ...]]:
    """解析单条事件；缺 `id` 视为无效行（跳过），其余字段逐项降级。"""
    warnings: list[str] = []
    if not isinstance(row, Mapping):
        _warn(warnings, "事件行不是 JSON 对象，已跳过")
        return None, tuple(warnings)

    event_id = _nonempty_str(row.get("id"))
    if event_id is None:
        _warn(warnings, "事件缺少 id 或 id 非字符串，已跳过")
        return None, tuple(warnings)

    group = _nonempty_str(row.get("group")) or ""
    if not group:
        _warn(warnings, f"事件 {event_id} 缺少 group，按空值处理（group_known=False）")
    elif group not in GROUP_KINDS:
        _warn(
            warnings,
            f"事件 {event_id} group={group!r} 未识别（group_known=False），保留原值且不视为 reset",
        )

    event_type = _nonempty_str(row.get("type")) or group
    announced_at = parse_iso(row.get("announced_at"))
    if row.get("announced_at") is not None and announced_at is None:
        _warn(warnings, f"事件 {event_id} announced_at 无法解析，已置空")

    date_text = _nonempty_str(row.get("date")) or ""
    if not date_text and announced_at is not None:
        date_text = announced_at.strftime("%Y-%m-%d")
    if not date_text:
        _warn(warnings, f"事件 {event_id} 缺少 date，已置空")

    summary = _str_or_none(row.get("summary")) or ""

    announcement_state: str | None = None
    raw_state = row.get("announcement_state")
    if raw_state is None:
        announcement_state = None  # D-01：缺失即 fail-closed，不视为 announced
    elif isinstance(raw_state, str) and raw_state.strip():
        value = raw_state.strip()
        if value in ANNOUNCEMENT_STATES:
            announcement_state = value
        else:
            announcement_state = value
            _warn(warnings, f"事件 {event_id} announcement_state={value!r} 未识别，原样保留")
    else:
        _warn(warnings, f"事件 {event_id} announcement_state 类型非法，按缺失处理")

    banked_state: str | None = None
    raw_banked = row.get("banked_state")
    if isinstance(raw_banked, str) and raw_banked.strip():
        value = raw_banked.strip()
        if value in BANKED_STATES:
            banked_state = value
        else:
            _warn(warnings, f"事件 {event_id} banked_state={value!r} 未识别，按 unknown 处理")
    elif raw_banked is not None:
        _warn(warnings, f"事件 {event_id} banked_state 类型非法，按 unknown 处理")

    confirmed = False
    if "confirmed" in row and row.get("confirmed") is not None:
        raw_confirmed = row.get("confirmed")
        if isinstance(raw_confirmed, bool):
            confirmed = raw_confirmed
        else:
            _warn(warnings, f"事件 {event_id} confirmed 非布尔值（{raw_confirmed!r}），按 false 处理")
    else:
        _warn(warnings, f"事件 {event_id} 缺少 confirmed，按 false 处理（fail-closed）")

    banked_grant = False
    raw_grant = row.get("banked_grant")
    if isinstance(raw_grant, bool):
        banked_grant = raw_grant
    elif raw_grant is not None:
        _warn(warnings, f"事件 {event_id} banked_grant 非布尔值（{raw_grant!r}），按 false 处理")

    preview = row.get("preview") is True
    url = safe_url(row.get("url"))
    if url is None and _nonempty_str(row.get("url")):
        _warn(warnings, f"事件 {event_id} 的 url 不安全或非法，已清空")

    confidence_raw = _nonempty_str(row.get("confidence"))
    confidence: str | None = confidence_raw
    if confidence_raw is not None and confidence_raw not in CONFIDENCE_VALUES:
        _warn(warnings, f"事件 {event_id} confidence={confidence_raw!r} 未识别，原样保留")

    official_window = _str_or_none(row.get("official_window"))
    if isinstance(official_window, str) and not official_window.strip():
        official_window = None

    effective_at = parse_iso(row.get("effective_at"))
    if row.get("effective_at") is not None and effective_at is None:
        _warn(warnings, f"事件 {event_id} effective_at 无法解析，已置空")

    event = TimelineEvent(
        id=event_id,
        date=date_text,
        type=event_type,
        group=group,
        summary=summary,
        text=_str_or_none(row.get("text")),
        url=url,
        announced_at=announced_at,
        effective_at=effective_at,
        official_window=official_window,
        announcement_state=announcement_state,
        banked_state=banked_state,
        banked_grant=banked_grant,
        confirmed=confirmed,
        preview=preview,
        source=_nonempty_str(row.get("source")),
        source_label=_nonempty_str(row.get("source_label")),
        confidence=confidence,
        reset_kind=_nonempty_str(row.get("reset_kind")),
        observed_variant=_nonempty_str(row.get("observed_variant")),
        audience=_str_tuple(row.get("audience")),
        incident_links=_str_tuple(row.get("incident_links")),
        reason_tags=_str_tuple(row.get("reason_tags")),
    )
    return event, tuple(warnings)


def parse_banked_observation(row: Any) -> tuple[BankedObservation | None, tuple[str, ...]]:
    """解析 `feed.banked_observations[]`；缺 id 跳过。"""
    warnings: list[str] = []
    if not isinstance(row, Mapping):
        _warn(warnings, "banked_observation 不是 JSON 对象，已跳过")
        return None, tuple(warnings)
    obs_id = _nonempty_str(row.get("id"))
    if obs_id is None:
        _warn(warnings, "banked_observation 缺少 id，已跳过")
        return None, tuple(warnings)

    banked_state, known = coerce_enum(row.get("banked_state"), BANKED_STATES - {"unknown"}, "unknown")
    if not known:
        _warn(warnings, f"banked_observation {obs_id} banked_state={banked_state!r} 未识别，按 unknown 处理")

    after_announcement = True
    raw_after = row.get("after_announcement")
    if isinstance(raw_after, bool):
        after_announcement = raw_after
    elif raw_after is not None:
        _warn(warnings, f"banked_observation {obs_id} after_announcement 非布尔值，按 true 处理")

    observation = BankedObservation(
        id=obs_id,
        kind=_nonempty_str(row.get("kind")) or "",
        source=_nonempty_str(row.get("source")) or "",
        banked_state=banked_state or "unknown",
        title=_str_or_none(row.get("title")) or "",
        text=_str_or_none(row.get("text")) or "",
        at=parse_iso(row.get("at")),
        after_announcement=after_announcement,
    )
    return observation, tuple(warnings)


# ---------------------------------------------------------------------------
# 三端点入口
# ---------------------------------------------------------------------------
def _fail(error_code: str, warnings: tuple[str, ...]) -> ParseResult[Any]:
    return ParseResult(ok=False, error_code=error_code, snapshot=None, warnings=warnings)


def parse_forecast(
    payload: Any,
    *,
    fetched_at: datetime,
    batch: str | None = None,
    checked_at: datetime | None = None,
) -> ParseResult[ForecastSnapshot]:
    """解析 `/api/forecast`。`updated_at` 非法 → schema_error；其余逐字段降级。"""
    warnings: list[str] = []
    if not isinstance(payload, Mapping):
        _warn(warnings, "forecast payload 不是 JSON 对象")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    updated_at = parse_iso(payload.get("updated_at"))
    if updated_at is None:
        _warn(warnings, f"forecast.updated_at 非法（{payload.get('updated_at')!r}）")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    rounded_24h = 0
    rounded_48h = 0
    probabilities = payload.get("probabilities")
    if not isinstance(probabilities, Mapping):
        _warn(warnings, "forecast.probabilities 缺失或非对象，概率按 0 处理（last_reset_at 仍可用）")
    else:
        rounded_24h = _probability(probabilities.get("rounded_24h"), "probabilities.rounded_24h", warnings)
        rounded_48h = _probability(probabilities.get("rounded_48h"), "probabilities.rounded_48h", warnings)

    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, str) and confidence_raw.strip():
        confidence = confidence_raw.strip()
        if confidence not in CONFIDENCE_VALUES:
            _warn(warnings, f"forecast.confidence={confidence!r} 未识别，原样保留")
    else:
        confidence = "unknown"
        _warn(warnings, f"forecast.confidence 类型非法（{confidence_raw!r}），按 unknown 处理")

    official_signal: dict[str, Any] | None = None
    raw_signal = payload.get("official_signal")
    if isinstance(raw_signal, Mapping):
        official_signal = dict(raw_signal)
    elif raw_signal is not None:
        _warn(warnings, "forecast.official_signal 非对象，已忽略")

    last_reset_at = parse_iso(payload.get("last_reset_at"))
    if payload.get("last_reset_at") is not None and last_reset_at is None:
        _warn(warnings, f"forecast.last_reset_at 非法（{payload.get('last_reset_at')!r}），已置空")

    # AC-23：`latest_alert` 是上游 OpenAPI 的必填字段。整键缺失说明契约漂移，必须告警；
    # 键存在但为 `null` 是「当前没有告警」的正常形态，保持静默。
    if "latest_alert" not in payload:
        _warn(warnings, "forecast.latest_alert 缺失（OpenAPI 必填）")

    latest_alert, alert_warnings = parse_latest_alert(payload.get("latest_alert"))
    for message in alert_warnings:
        warnings.append(message)

    snapshot = ForecastSnapshot(
        updated_at=updated_at,
        rounded_24h=rounded_24h,
        rounded_48h=rounded_48h,
        confidence=confidence,
        last_reset_at=last_reset_at,
        latest_alert=latest_alert,
        official_signal=official_signal,
        fetched_at=fetched_at,
        batch=batch,
        checked_at=checked_at,
    )
    return ParseResult(ok=True, error_code=ErrorCode.OK, snapshot=snapshot, warnings=tuple(warnings))


def parse_timeline(
    payload: Any,
    *,
    fetched_at: datetime,
    batch: str | None = None,
    checked_at: datetime | None = None,
) -> ParseResult[TimelineSnapshot]:
    """解析 `/api/timeline`；`events` 非 list → schema_error，坏行跳过。"""
    warnings: list[str] = []
    if not isinstance(payload, Mapping):
        _warn(warnings, "timeline payload 不是 JSON 对象")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    updated_at = parse_iso(payload.get("updated_at"))
    if updated_at is None:
        _warn(warnings, f"timeline.updated_at 非法（{payload.get('updated_at')!r}）")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        _warn(warnings, f"timeline.events 不是列表（{type(raw_events).__name__}）")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    events: list[TimelineEvent] = []
    skipped = 0
    for row in raw_events:
        event, event_warnings = parse_event(row)
        warnings.extend(event_warnings)
        if event is None:
            skipped += 1
            continue
        events.append(event)
    if skipped:
        _warn(warnings, f"timeline 共跳过 {skipped} 条无效事件")

    snapshot = TimelineSnapshot(
        updated_at=updated_at,
        events=tuple(events),
        fetched_at=fetched_at,
        batch=batch,
        checked_at=checked_at,
    )
    return ParseResult(ok=True, error_code=ErrorCode.OK, snapshot=snapshot, warnings=tuple(warnings))


def parse_feed(
    payload: Any,
    *,
    fetched_at: datetime | None = None,
    batch: str | None = None,
    checked_at: datetime | None = None,
) -> ParseResult[FeedSnapshot]:
    """解析 `/api/feed`；`fetched_at` 非法 → 整体失败，`stale` 非 bool → False。"""
    warnings: list[str] = []
    if not isinstance(payload, Mapping):
        _warn(warnings, "feed payload 不是 JSON 对象")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    parsed_fetched_at = parse_iso(payload.get("fetched_at"))
    if parsed_fetched_at is None:
        # 注意：调用方传入的 `fetched_at` 仅作签名兼容，缺失即失败（fail-closed）
        _warn(warnings, f"feed.fetched_at 非法（{payload.get('fetched_at')!r}）")
        return _fail(ErrorCode.SCHEMA_ERROR, tuple(warnings))

    raw_stale = payload.get("stale")
    if isinstance(raw_stale, bool):
        stale = raw_stale
    else:
        stale = False
        if raw_stale is not None:
            _warn(warnings, f"feed.stale 非布尔值（{raw_stale!r}），按 false 处理")
        elif "stale" not in payload:
            # AC-23：整键缺失属于契约漂移（缺失与 `false` 语义不同，读不出「站点是否标记陈旧」）
            _warn(warnings, "feed.stale 缺失，按 false 处理（无法判断站点是否陈旧）")

    tweets: list[dict[str, Any]] = []
    raw_tweets = payload.get("tweets")
    if isinstance(raw_tweets, list):
        for tweet in raw_tweets:
            if isinstance(tweet, Mapping):
                tweets.append(dict(tweet))
            else:
                _warn(warnings, "feed.tweets 中存在非对象条目，已跳过")
    elif raw_tweets is not None:
        _warn(warnings, "feed.tweets 不是列表，已忽略")

    observations: list[BankedObservation] = []
    raw_observations = payload.get("banked_observations")
    if isinstance(raw_observations, list):
        for row in raw_observations:
            observation, obs_warnings = parse_banked_observation(row)
            warnings.extend(obs_warnings)
            if observation is not None:
                observations.append(observation)
    elif raw_observations is not None:
        _warn(warnings, "feed.banked_observations 不是列表，已忽略")

    events: list[TimelineEvent] = []
    raw_events = payload.get("events")
    if isinstance(raw_events, list):
        for row in raw_events:
            event, event_warnings = parse_event(row)
            warnings.extend(event_warnings)
            if event is not None:
                events.append(event)
    elif raw_events is not None:
        _warn(warnings, "feed.events 不是列表，已忽略")
    else:
        # AC-23：整键缺失按空列表降级，但要留痕（否则「网站当天没有任何事件」与
        # 「上游把 events 改名了」在日志里无法区分）
        _warn(warnings, "feed.events 缺失，按空列表处理")

    snapshot = FeedSnapshot(
        fetched_at=parsed_fetched_at,
        stale=stale,
        tweets=tuple(tweets),
        banked_observations=tuple(observations),
        events=tuple(events),
        newest_post_at=parse_iso(payload.get("newest_post_at")),
        batch=batch,
        checked_at=checked_at,
    )
    return ParseResult(ok=True, error_code=ErrorCode.OK, snapshot=snapshot, warnings=tuple(warnings))


def group_is_known(group: str | None) -> bool:
    """事件 `group` 是否为已识别枚举（分类器使用，避免把未知值当 reset）。"""
    return isinstance(group, str) and group in GROUP_KINDS


__all__ = [
    "ALERT_KINDS",
    "ALERT_STATES",
    "ANNOUNCEMENT_STATES",
    "BANKED_STATES",
    "CONFIDENCE_VALUES",
    "GROUP_KINDS",
    "coerce_enum",
    "group_is_known",
    "parse_alert_window",
    "parse_banked_observation",
    "parse_event",
    "parse_feed",
    "parse_forecast",
    "parse_iso",
    "parse_latest_alert",
    "safe_url",
]
