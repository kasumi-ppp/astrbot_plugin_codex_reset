"""配置层测试：校验、回落、稀疏覆盖合并、WebUI 卡片写回规则。"""

from __future__ import annotations

from services.config import (
    DEFAULT_TIME_ZONE,
    build_plugin_settings,
    group_settings_to_dict,
    override_from_card,
    override_to_card,
    parse_group_overrides,
    parse_group_settings,
    parse_override,
    parse_settings_json,
    parse_timezone,
    resolve_group_settings,
    settings_to_json,
)
from services.models import GroupOverride, GroupSettings


# ---------------------------------------------------------------------------
# 全局默认 + 校验
# ---------------------------------------------------------------------------
def test_defaults_are_frozen_values() -> None:
    settings = build_plugin_settings({})
    assert settings.enable_monitor is True
    assert settings.defaults == GroupSettings()
    assert settings.request_spacing_seconds == 60
    assert settings.time_zone == DEFAULT_TIME_ZONE
    assert settings.forecast_confirm_samples == 2
    assert settings.warnings == ()


def test_thresholds_are_clamped_to_0_100() -> None:
    settings = build_plugin_settings({"threshold_24h": 150, "threshold_48h": -5})
    assert settings.defaults.threshold_24h == 100
    assert settings.defaults.threshold_48h == 0
    assert any("threshold_24h" in warning for warning in settings.warnings)
    assert any("threshold_48h" in warning for warning in settings.warnings)


def test_spacing_below_60_is_raised_with_warning() -> None:
    settings = build_plugin_settings({"request_spacing_seconds": 30})
    assert settings.request_spacing_seconds == 60
    assert any("request_spacing_seconds" in warning for warning in settings.warnings)


def test_invalid_timezone_falls_back_to_shanghai() -> None:
    settings = build_plugin_settings({"time_zone": "Mars/Olympus"})
    assert settings.time_zone == DEFAULT_TIME_ZONE
    assert any("time_zone" in warning for warning in settings.warnings)


def test_non_int_value_falls_back_to_default_with_warning() -> None:
    settings = build_plugin_settings({"forecast_cooldown_hours": "abc", "forecast_daily_max_per_group": None})
    assert settings.forecast_cooldown_hours == 12
    assert settings.forecast_daily_max_per_group == 2
    assert any("forecast_cooldown_hours" in warning for warning in settings.warnings)


def test_other_ranges_are_enforced() -> None:
    settings = build_plugin_settings(
        {
            "forecast_confirm_samples": 1,
            "forecast_cooldown_hours": 999,
            "forecast_daily_max_per_group": 99,
            "max_source_age_minutes": 0,
            "max_catchup_hours": 48,
        }
    )
    assert settings.forecast_confirm_samples == 2
    assert settings.forecast_cooldown_hours == 72
    assert settings.forecast_daily_max_per_group == 10
    assert settings.max_source_age_minutes == 1
    assert settings.max_catchup_hours == 24


def test_show_source_link_cannot_be_disabled() -> None:
    settings = build_plugin_settings({"show_source_link": False})
    assert settings.show_source_link is False  # 值保留（供渲染层告警），但渲染仍输出署名
    assert any("show_source_link" in warning for warning in settings.warnings)


def test_parse_timezone_valid_and_invalid() -> None:
    zone, warning = parse_timezone("UTC")
    assert warning is None
    assert zone.key == "UTC"

    zone, warning = parse_timezone("Not/AZone")
    assert warning is not None
    assert zone.key == DEFAULT_TIME_ZONE


# ---------------------------------------------------------------------------
# 稀疏覆盖 → 完整生效配置
# ---------------------------------------------------------------------------
def test_resolve_group_settings_inherits_unspecified_fields() -> None:
    plugin = build_plugin_settings({"threshold_24h": 40})
    override = GroupOverride(threshold_24h=80)
    resolved = resolve_group_settings(plugin, override)
    assert resolved.threshold_24h == 80  # 覆盖生效
    assert resolved.threshold_48h == plugin.defaults.threshold_48h  # 其余继承
    assert resolved.enable_watch is True


def test_resolve_group_settings_without_override_returns_defaults() -> None:
    plugin = build_plugin_settings({"enable_watch": False})
    assert resolve_group_settings(plugin, None) == plugin.defaults


def test_sparse_override_serialisation_only_keeps_set_fields() -> None:
    override = GroupOverride(enable_forecast=False, threshold_24h=88)
    assert override.as_sparse_dict() == {"enable_forecast": False, "threshold_24h": 88}
    assert override.is_empty() is False
    assert GroupOverride().is_empty() is True


def test_override_from_card_skips_values_equal_to_defaults() -> None:
    plugin = build_plugin_settings({})
    card = {
        "subscription_id": "sub:abc",
        "group_name": "测试群",
        "enable_confirmed_reset": True,  # 等于默认 → 不写键
        "enable_watch": True,
        "enable_forecast": True,
        "enable_credits": True,
        "threshold_24h": 50,
        "threshold_48h": 70,
    }
    assert override_from_card(card, plugin).as_sparse_dict() == {}


def test_override_from_card_writes_only_changed_fields() -> None:
    plugin = build_plugin_settings({})
    card = {
        "enable_confirmed_reset": True,
        "enable_watch": False,
        "enable_forecast": True,
        "enable_credits": True,
        "threshold_24h": 80,
        "threshold_48h": 70,
    }
    override = override_from_card(card, plugin)
    assert override.as_sparse_dict() == {"enable_watch": False, "threshold_24h": 80}
    resolved = resolve_group_settings(plugin, override)
    assert resolved.enable_watch is False
    assert resolved.threshold_24h == 80
    assert resolved.threshold_48h == 70


def test_override_from_card_clamps_out_of_range_threshold() -> None:
    plugin = build_plugin_settings({})
    override = override_from_card({"threshold_24h": 150}, plugin)
    assert override.threshold_24h == 100


def test_override_to_card_fills_defaults_for_inherited_fields() -> None:
    plugin = build_plugin_settings({"threshold_48h": 65})
    override = GroupOverride(threshold_24h=80)
    card = override_to_card(override, plugin, subscription_id="sub:abc", group_name="群")
    assert card["threshold_24h"] == 80
    assert card["threshold_48h"] == 65
    assert card["subscription_id"] == "sub:abc"


def test_parse_override_ignores_missing_and_none() -> None:
    override = parse_override({"threshold_24h": None, "enable_watch": False})
    assert override.threshold_24h is None
    assert override.enable_watch is False
    assert parse_override(None).is_empty() is True


def test_parse_group_overrides_indexes_by_subscription_id() -> None:
    plugin = build_plugin_settings({})
    items = [
        {"subscription_id": "sub:a", "enable_watch": False},
        {"subscription_id": "", "enable_watch": True},  # 缺 id → 跳过
        "not-a-mapping",
        {"subscription_id": "sub:b", "threshold_24h": 90},
    ]
    overrides = parse_group_overrides(plugin, items)
    assert set(overrides) == {"sub:a", "sub:b"}
    assert overrides["sub:a"].enable_watch is False
    assert overrides["sub:b"].threshold_24h == 90


# ---------------------------------------------------------------------------
# 完整群配置序列化
# ---------------------------------------------------------------------------
def test_settings_json_roundtrip() -> None:
    original = GroupSettings(enable_credits=False, threshold_24h=30, threshold_48h=60)
    restored = parse_settings_json(settings_to_json(original))
    assert restored == original


def test_parse_settings_json_broken_text_falls_back() -> None:
    assert parse_settings_json("not json") == GroupSettings()
    assert parse_settings_json(None) == GroupSettings()
    assert parse_settings_json("[1,2]") == GroupSettings()


def test_group_settings_to_dict_has_all_seven_fields() -> None:
    payload = group_settings_to_dict(GroupSettings())
    assert set(payload) == {
        "enable_confirmed_reset",
        "enable_watch",
        "enable_forecast",
        "enable_credits",
        "enable_daily_report",
        "threshold_24h",
        "threshold_48h",
    }


def test_parse_group_settings_clamps_and_defaults() -> None:
    settings = parse_group_settings({"threshold_24h": -1, "enable_watch": "nope"})
    assert settings.threshold_24h == 0
    assert settings.enable_watch is True  # 非法 → 默认
