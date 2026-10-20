"""配置解析与校验。

分三层（本层的核心语义）：
1. `PluginSettings`：全局默认值 + 全局校验（`_conf_schema.json` → 本文件）；
2. `GroupOverride`：**稀疏**群级覆盖，字段为 `None` 表示「未覆盖 = 继承全局默认」；
3. `GroupSettings`：**完整生效**配置，由 `resolve_group_settings()` 合并得到。

WebUI 卡片写回规则（`override_from_card`）：卡片值等于全局默认值 → 不写入覆盖，
保持「继承」语义；只有真正不同的值才落库为覆盖，避免全局默认调整后群配置被冻结。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import GroupOverride, GroupSettings, PluginSettings

logger = logging.getLogger(__name__)

DEFAULT_TIME_ZONE = "Asia/Shanghai"
DEFAULT_SPACING_SECONDS = 60
MIN_SPACING_SECONDS = 60
DEFAULT_GROUP_LIMIT = 50

#: 可被群覆盖的 6 个字段（顺序固定，用于卡片读写与日志）
OVERRIDE_FIELDS: tuple[str, ...] = (
    "enable_confirmed_reset",
    "enable_watch",
    "enable_forecast",
    "enable_credits",
    "threshold_24h",
    "threshold_48h",
)

BOOL_OVERRIDE_FIELDS: tuple[str, ...] = (
    "enable_confirmed_reset",
    "enable_watch",
    "enable_forecast",
    "enable_credits",
)

TRUE_TEXT = frozenset({"true", "1", "yes", "on"})
FALSE_TEXT = frozenset({"false", "0", "no", "off"})


# ---------------------------------------------------------------------------
# 基础类型转换（全部带 warning，绝不抛异常）
# ---------------------------------------------------------------------------
def _coerce_bool(data: Mapping[str, object], key: str, default: bool, warnings: list[str]) -> bool:
    """从原始配置读取 bool；非法值回落默认并收集 warning。"""
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in TRUE_TEXT:
            return True
        if text in FALSE_TEXT:
            return False
    warnings.append(f"{key} 需要布尔值（收到 {value!r}），已回落默认 {default}")
    return default


def _coerce_int(
    data: Mapping[str, object],
    key: str,
    default: int,
    lo: int,
    hi: int,
    warnings: list[str],
) -> int:
    """从原始配置读取 int 并收敛到 [lo, hi]；非法值回落默认 + warning。"""
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool):
        warnings.append(f"{key} 需要整数（收到布尔 {value!r}），已回落默认 {default}")
        return default
    if isinstance(value, str):
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            warnings.append(f"{key} 需要整数（收到 {value!r}），已回落默认 {default}")
            return default
    if not isinstance(value, int):
        warnings.append(f"{key} 需要整数（收到 {value!r}），已回落默认 {default}")
        return default
    clamped = max(lo, min(hi, value))
    if clamped != value:
        warnings.append(f"{key}={value} 超出合法区间 {lo}-{hi}，已收敛为 {clamped}")
    return clamped


def parse_timezone(name: str) -> tuple[ZoneInfo, str | None]:
    """解析 IANA 时区；非法时回落 `Asia/Shanghai` 并返回 warning 文本。"""
    text = (name or "").strip()
    if text:
        try:
            return ZoneInfo(text), None
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            warning = f"time_zone={text!r} 非法（{exc}），已回落 {DEFAULT_TIME_ZONE}"
            logger.warning("[codex-reset][config] %s", warning)
            return ZoneInfo(DEFAULT_TIME_ZONE), warning
    warning = f"time_zone 为空或类型非法（{name!r}），已回落 {DEFAULT_TIME_ZONE}"
    logger.warning("[codex-reset][config] %s", warning)
    return ZoneInfo(DEFAULT_TIME_ZONE), warning


def timezone_cn_name(name: str) -> str:
    """时区中文名：用于消息渲染（`Asia/Shanghai→北京时间`、`UTC→UTC`）。"""
    if name in {"Asia/Shanghai", "Asia/Chongqing", "PRC"}:
        return "北京时间"
    if name in {"UTC", "Etc/UTC"}:
        return "UTC"
    return name


# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
def build_plugin_settings(raw: Mapping[str, object] | None) -> PluginSettings:
    """把 AstrBot 原始配置解析成 `PluginSettings`（永不抛异常，全部降级 + warning）。"""
    data: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
    warnings: list[str] = []
    if not isinstance(raw, Mapping):
        warnings.append("配置对象不是字典，全部使用默认值")

    defaults = GroupSettings(
        enable_confirmed_reset=_coerce_bool(data, "enable_confirmed_reset", True, warnings),
        enable_watch=_coerce_bool(data, "enable_watch", True, warnings),
        enable_forecast=_coerce_bool(data, "enable_forecast", True, warnings),
        enable_credits=_coerce_bool(data, "enable_credits", True, warnings),
        threshold_24h=_coerce_int(data, "threshold_24h", 50, 0, 100, warnings),
        threshold_48h=_coerce_int(data, "threshold_48h", 70, 0, 100, warnings),
    )

    raw_tz = data.get("time_zone", DEFAULT_TIME_ZONE)
    if not isinstance(raw_tz, str) or not raw_tz.strip():
        warnings.append(f"time_zone 需要字符串（收到 {raw_tz!r}），已回落 {DEFAULT_TIME_ZONE}")
        time_zone = DEFAULT_TIME_ZONE
    else:
        _, tz_warning = parse_timezone(raw_tz)
        time_zone = DEFAULT_TIME_ZONE if tz_warning is not None else raw_tz.strip()
        if tz_warning is not None:
            warnings.append(tz_warning)

    spacing = _coerce_int(data, "request_spacing_seconds", DEFAULT_SPACING_SECONDS, 1, 3600, warnings)
    if spacing < MIN_SPACING_SECONDS:
        warnings.append(
            f"request_spacing_seconds={spacing} 小于合规下限 {MIN_SPACING_SECONDS}s，已抬升为 "
            f"{MIN_SPACING_SECONDS}s（AC-19）"
        )
        spacing = MIN_SPACING_SECONDS

    samples = _coerce_int(data, "forecast_confirm_samples", 2, 1, 100, warnings)
    if samples < 2:
        warnings.append(f"forecast_confirm_samples={samples} 低于下限 2，已抬升为 2")
        samples = 2

    cooldown = _coerce_int(data, "forecast_cooldown_hours", 12, 1, 72, warnings)
    daily_max = _coerce_int(data, "forecast_daily_max_per_group", 2, 1, 10, warnings)
    max_age = _coerce_int(data, "max_source_age_minutes", 10, 1, 120, warnings)
    max_catchup = _coerce_int(data, "max_catchup_hours", 6, 0, 24, warnings)
    group_limit = _coerce_int(data, "group_limit", DEFAULT_GROUP_LIMIT, 1, 500, warnings)

    show_source_link = _coerce_bool(data, "show_source_link", True, warnings)
    if not show_source_link:
        warnings.append("show_source_link=false 不被支持：署名行不可关闭，渲染层仍会输出")

    settings = PluginSettings(
        enable_monitor=_coerce_bool(data, "enable_monitor", True, warnings),
        defaults=defaults,
        forecast_confirm_samples=samples,
        forecast_cooldown_hours=cooldown,
        forecast_daily_max_per_group=daily_max,
        time_zone=time_zone,
        request_spacing_seconds=spacing,
        max_source_age_minutes=max_age,
        max_catchup_hours=max_catchup,
        enable_catchup=_coerce_bool(data, "enable_catchup", False, warnings),
        allow_at_all=_coerce_bool(data, "allow_at_all", False, warnings),
        show_source_link=show_source_link,
        admin_only_subscription=_coerce_bool(data, "admin_only_subscription", True, warnings),
        group_limit=group_limit,
        warnings=tuple(warnings),
    )
    for warning in warnings:
        logger.warning("[codex-reset][config] %s", warning)
    return settings


# ---------------------------------------------------------------------------
# 群级配置：完整值 / 稀疏覆盖
# ---------------------------------------------------------------------------
def parse_group_settings(raw: Mapping[str, object] | None) -> GroupSettings:
    """解析**完整**群配置（`subscriptions.settings_json` 反序列化入口）。"""
    data: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
    warnings: list[str] = []
    settings = GroupSettings(
        enable_confirmed_reset=_coerce_bool(data, "enable_confirmed_reset", True, warnings),
        enable_watch=_coerce_bool(data, "enable_watch", True, warnings),
        enable_forecast=_coerce_bool(data, "enable_forecast", True, warnings),
        enable_credits=_coerce_bool(data, "enable_credits", True, warnings),
        threshold_24h=_coerce_int(data, "threshold_24h", 50, 0, 100, warnings),
        threshold_48h=_coerce_int(data, "threshold_48h", 70, 0, 100, warnings),
    )
    if warnings:
        logger.warning("[codex-reset][config] 群配置存在非法字段：%s", "; ".join(warnings))
    return settings


def group_settings_to_dict(settings: GroupSettings) -> dict[str, object]:
    """把完整群配置导出为 dict（6 个字段全部输出）。"""
    return {
        "enable_confirmed_reset": bool(settings.enable_confirmed_reset),
        "enable_watch": bool(settings.enable_watch),
        "enable_forecast": bool(settings.enable_forecast),
        "enable_credits": bool(settings.enable_credits),
        "threshold_24h": int(settings.threshold_24h),
        "threshold_48h": int(settings.threshold_48h),
    }


def settings_to_json(settings: GroupSettings) -> str:
    """序列化完整群配置为 JSON 字符串（写入 `subscriptions.settings_json`）。"""
    return json.dumps(group_settings_to_dict(settings), ensure_ascii=False, sort_keys=True)


def parse_settings_json(text: str | None) -> GroupSettings:
    """反序列化 `settings_json`；损坏时回落默认值并告警（不抛异常）。"""
    if not text:
        return GroupSettings()
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("[codex-reset][config] settings_json 无法解析，已回落默认群配置")
        return GroupSettings()
    if not isinstance(raw, Mapping):
        logger.warning("[codex-reset][config] settings_json 不是对象，已回落默认群配置")
        return GroupSettings()
    return parse_group_settings(raw)


def resolve_group_settings(plugin: PluginSettings, override: GroupOverride | None) -> GroupSettings:
    """逐字段合并：`override.x if override.x is not None else plugin.defaults.x`。"""
    base = plugin.defaults
    if override is None:
        return base
    values: dict[str, object] = {}
    for name in OVERRIDE_FIELDS:
        value = getattr(override, name)
        values[name] = getattr(base, name) if value is None else value
    return GroupSettings(**values)  # type: ignore[arg-type]


def parse_override(raw: Mapping[str, object] | None) -> GroupOverride:
    """解析**稀疏**覆盖（缺失与 `None` 都视为「未覆盖」）。"""
    data: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
    warnings: list[str] = []
    values: dict[str, object] = {}
    for name in BOOL_OVERRIDE_FIELDS:
        if data.get(name) is None:
            continue
        if isinstance(data[name], bool) or isinstance(data[name], str):
            values[name] = _coerce_bool(data, name, True, warnings)
    for name in ("threshold_24h", "threshold_48h"):
        if data.get(name) is None:
            continue
        if isinstance(data[name], int | str) and not isinstance(data[name], bool):
            values[name] = _coerce_int(data, name, 50, 0, 100, warnings)
    if warnings:
        logger.warning("[codex-reset][config] 群覆盖存在非法字段：%s", "; ".join(warnings))
    return GroupOverride(**values)  # type: ignore[arg-type]


def override_to_json(override: GroupOverride | None) -> str:
    """把稀疏覆盖序列化为 JSON 字符串（`subscriptions.settings_json` 唯一写入口）。

    只写显式覆盖过的字段，绝不全量落 6 个值，否则「全局默认值只影响未覆盖字段」会失效。
    """
    sparse = (override or GroupOverride()).as_sparse_dict()
    return json.dumps(sparse, ensure_ascii=False, sort_keys=True)


def parse_override_json(text: str | None) -> GroupOverride:
    """反序列化 `settings_json` → 稀疏覆盖；损坏或非对象 → 空覆盖 + warning。"""
    if not text:
        return GroupOverride()
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("[codex-reset][config] settings_json 无法解析，按「无群级覆盖」处理")
        return GroupOverride()
    if not isinstance(raw, Mapping):
        logger.warning("[codex-reset][config] settings_json 不是对象，按「无群级覆盖」处理")
        return GroupOverride()
    return parse_override(raw)


def override_from_card(card: Mapping[str, object] | None, plugin: PluginSettings) -> GroupOverride:
    """把 WebUI 卡片（完整值）转成稀疏覆盖：等于全局默认值的字段不写键。"""
    data: Mapping[str, object] = card if isinstance(card, Mapping) else {}
    base = plugin.defaults
    values: dict[str, object] = {}
    for name in OVERRIDE_FIELDS:
        if data.get(name) is None:
            continue
        default_value = getattr(base, name)
        if name in BOOL_OVERRIDE_FIELDS:
            if not isinstance(data[name], bool | str):
                continue
            parsed: object = _coerce_bool(data, name, bool(default_value), [])
        else:
            if not isinstance(data[name], int | str) or isinstance(data[name], bool):
                continue
            lo, hi = 0, 100
            parsed = _coerce_int(data, name, int(default_value), lo, hi, [])
        if parsed != default_value:
            values[name] = parsed
    return GroupOverride(**values)  # type: ignore[arg-type]


def parse_group_overrides(
    plugin: PluginSettings,
    items: object,
) -> dict[str, GroupOverride]:
    """解析 `group_overrides` template_list → `{subscription_id: GroupOverride}`。"""
    result: dict[str, GroupOverride] = {}
    if not isinstance(items, list | tuple):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            logger.warning("[codex-reset][config] group_overrides 条目不是对象，已跳过")
            continue
        subscription_id = item.get("subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id.strip():
            logger.warning("[codex-reset][config] group_overrides 条目缺少 subscription_id，已跳过")
            continue
        result[subscription_id.strip()] = override_from_card(item, plugin)
    return result


def override_to_card(
    override: GroupOverride,
    plugin: PluginSettings,
    *,
    subscription_id: str,
    group_name: str = "",
) -> dict[str, object]:
    """稀疏覆盖 → WebUI 卡片（卡片永远是完整值，缺失字段填全局默认）。"""
    card: dict[str, object] = {"subscription_id": subscription_id, "group_name": group_name}
    for name in OVERRIDE_FIELDS:
        value = getattr(override, name)
        card[name] = getattr(plugin.defaults, name) if value is None else value
    return card


__all__ = [
    "DEFAULT_TIME_ZONE",
    "MIN_SPACING_SECONDS",
    "OVERRIDE_FIELDS",
    "build_plugin_settings",
    "group_settings_to_dict",
    "override_from_card",
    "override_to_card",
    "override_to_json",
    "parse_group_overrides",
    "parse_group_settings",
    "parse_override",
    "parse_override_json",
    "parse_settings_json",
    "parse_timezone",
    "resolve_group_settings",
    "settings_to_json",
    "timezone_cn_name",
]
