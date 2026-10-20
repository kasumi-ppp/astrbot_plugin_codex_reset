"""静态契约测试：配置 schema / 元数据 / 入口可编译 / 分层依赖。

这些断言不跑业务逻辑，只守住「发布形态」的硬约束：
- `_conf_schema.json` 19 个键与 PRD §5 逐字一致（改名即破坏已确认范围）；
- `group_overrides` 的 template_list 结构可被 WebUI 正确渲染；
- `metadata.yaml` 关键字段正确；
- `main.py` 可 `compile()`（无 astrbot 环境也能做语法校验）；
- `services/**` 绝不 `import astrbot`（分层硬约束）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from services.config import build_plugin_settings
from services.models import PluginSettings

ROOT = Path(__file__).resolve().parents[1]

#: PRD §5 冻结的 18 个全局字段 + `group_overrides`（共 19 个键）
GLOBAL_FIELDS: dict[str, tuple[str, Any]] = {
    "enable_monitor": ("bool", True),
    "enable_confirmed_reset": ("bool", True),
    "enable_watch": ("bool", True),
    "enable_forecast": ("bool", True),
    "enable_credits": ("bool", True),
    "threshold_24h": ("int", 50),
    "threshold_48h": ("int", 70),
    "forecast_confirm_samples": ("int", 2),
    "forecast_cooldown_hours": ("int", 12),
    "forecast_daily_max_per_group": ("int", 2),
    "time_zone": ("string", "Asia/Shanghai"),
    "request_spacing_seconds": ("int", 60),
    "max_source_age_minutes": ("int", 10),
    "max_catchup_hours": ("int", 6),
    "enable_catchup": ("bool", False),
    "allow_at_all": ("bool", False),
    "show_source_link": ("bool", True),
    "admin_only_subscription": ("bool", True),
}

OVERRIDE_CARD_FIELDS: tuple[str, ...] = (
    "subscription_id",
    "group_name",
    "_version",
    "enable_confirmed_reset",
    "enable_watch",
    "enable_forecast",
    "enable_credits",
    "threshold_24h",
    "threshold_48h",
)


def _schema() -> dict[str, Any]:
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _parse_metadata(text: str) -> dict[str, Any]:
    """极简 YAML 解析（只支持本文件用到的标量、行内列表与块列表）。"""
    data: dict[str, Any] = {}
    list_key: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if list_key is not None:
                data[list_key].append(stripped[2:].strip().strip("'\""))
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []
            list_key = key
            continue
        list_key = None
        if value.startswith("[") and value.endswith("]"):
            data[key] = [
                item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()
            ]
        else:
            data[key] = value.strip("'\"")
    return data


# ---------------------------------------------------------------------------
# _conf_schema.json
# ---------------------------------------------------------------------------
def test_schema_has_exactly_the_frozen_fields() -> None:
    schema = _schema()
    assert len(schema) == 19
    assert set(schema) == {*GLOBAL_FIELDS, "group_overrides"}

    for name, (expected_type, expected_default) in GLOBAL_FIELDS.items():
        entry = schema[name]
        assert entry["type"] == expected_type, name
        assert entry["default"] == expected_default, name
        assert isinstance(entry["description"], str) and entry["description"], name


def test_schema_sliders_and_hints() -> None:
    schema = _schema()
    for name in ("threshold_24h", "threshold_48h"):
        slider = schema[name]["slider"]
        assert (slider["min"], slider["max"], slider["step"]) == (0, 100, 1)
    spacing = schema["request_spacing_seconds"]["slider"]
    assert (spacing["min"], spacing["max"], spacing["step"]) == (60, 600, 10)
    assert schema["show_source_link"]["hint"] == "数据来源署名不可关闭（许可要求）"
    assert "IANA" in schema["time_zone"]["hint"]


def test_schema_group_overrides_template_list_structure() -> None:
    entry = _schema()["group_overrides"]
    assert entry["type"] == "template_list"
    assert entry["default"] == []
    templates = entry["templates"]
    assert set(templates) == {"group_card"}

    card = templates["group_card"]
    assert card["display_item"] == "group_name"
    items = card["items"]
    assert set(items) == set(OVERRIDE_CARD_FIELDS)
    for hidden in ("subscription_id", "group_name", "_version"):
        assert items[hidden]["invisible"] is True, hidden
    assert items["_version"]["type"] == "int"
    for name in ("enable_confirmed_reset", "enable_watch", "enable_forecast", "enable_credits"):
        assert items[name]["type"] == "bool"
    for name in ("threshold_24h", "threshold_48h"):
        assert items[name]["type"] == "int"
        assert (items[name]["slider"]["min"], items[name]["slider"]["max"]) == (0, 100)


def test_schema_defaults_match_plugin_settings_defaults() -> None:
    schema = _schema()
    raw = {
        name: entry["default"]
        for name, entry in schema.items()
        if name != "group_overrides"
    }
    settings = build_plugin_settings(raw)
    assert settings == PluginSettings()  # 默认值不得漂移
    assert settings.warnings == ()


# ---------------------------------------------------------------------------
# metadata.yaml
# ---------------------------------------------------------------------------
def test_metadata_yaml_is_parseable_and_correct() -> None:
    metadata = _parse_metadata((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "astrbot_plugin_codex_reset"
    assert metadata["display_name"] == "Codex 重置雷达"
    assert metadata["version"] == "v1.2.0"
    assert metadata["author"] == "kasumi-ppp"
    assert metadata["repo"] == "https://github.com/kasumi-ppp/astrbot_plugin_codex_reset"
    assert metadata["astrbot_version"] == ">=4.16,<5"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["desc"] and metadata["short_desc"]


# ---------------------------------------------------------------------------
# main.py 入口形态（无 astrbot 环境也能校验语法）
# ---------------------------------------------------------------------------
def test_main_py_compiles_and_declares_plugin() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    compile(source, "main.py", "exec")  # 语法错误会直接抛
    assert '@register(\n    PLUGIN_NAME,' in source or '@register(PLUGIN_NAME' in source
    assert 'PLUGIN_NAME = "astrbot_plugin_codex_reset"' in source
    assert 'PLUGIN_VERSION = "1.2.0"' in source
    # 单指令 + GreedyStr 手工分派（不用 command_group）
    assert '@filter.command("cr", alias={"codexreset"})' in source
    assert "GreedyStr" in source
    assert "@filter.command_group" not in source
    # 两个后台任务句柄与生命周期
    assert "self._tasks" in source
    assert "asyncio.gather(*tasks, return_exceptions=True)" in source


def test_main_py_declares_soft_single_instance_health_and_hot_reload() -> None:
    """P2-07 / B：软单实例、端点健康 meta、配置热更新的关键符号必须在源码里出现。

    `main.py` 依赖 astrbot（本地不可导入），因此这里只做**静态**守卫：一旦有人删掉
    `runtime:owner` / `health:` / 只读模式赋值 / 配置监听任务，测试立刻失败。
    """
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'RUNTIME_OWNER_KEY = "runtime:owner"' in source
    assert 'RUNTIME_HEARTBEAT_KEY = "runtime:heartbeat_at"' in source
    assert 'HEALTH_KEY_PREFIX = "health:"' in source
    # 只读采集模式：检测到他实例时置位
    assert "self._readonly = True" in source
    assert "只读采集模式" in source
    # 端点健康 meta 写入使用 `health:{endpoint}` 前缀
    assert "f\"{HEALTH_KEY_PREFIX}{outcome.endpoint}\"" in source
    assert "error_code" in source
    # 配置热更新：按内容摘要（sha1）判断，且任务在 initialize 中始终创建
    assert "_config_watch_loop" in source
    assert "sha1" in source
    assert 'name="codex-reset-config-watch"' in source
    assert "set_spacing(" in source


def test_main_py_single_instance_and_watch_are_ast_wired() -> None:
    """AST 断言：`_claim_runtime_owner` 确实比较「他实例 + 心跳阈值」，热更新确实调 set_spacing。"""

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert "_claim_runtime_owner" in functions
    claim_source = ast.get_source_segment(
        (ROOT / "main.py").read_text(encoding="utf-8"), functions["_claim_runtime_owner"]
    )
    assert claim_source is not None
    assert "RUNTIME_OWNER_KEY" in claim_source
    assert "RUNTIME_HEARTBEAT_KEY" in claim_source
    assert "request_spacing_seconds" in claim_source, "心跳阈值必须与采集间隔挂钩（2×spacing）"

    assert "_reload_config_if_changed" in functions
    assert "_sync_poll_task" in functions
    reload_source = ast.get_source_segment(
        (ROOT / "main.py").read_text(encoding="utf-8"), functions["_reload_config_if_changed"]
    )
    assert reload_source is not None
    assert "set_spacing" in reload_source
    assert "_apply_config_cards" in reload_source

    # `_apply_config_cards` 必须「先分组、再按版本取最新」（P2-06）
    cards_source = ast.get_source_segment(
        (ROOT / "main.py").read_text(encoding="utf-8"), functions["_apply_config_cards"]
    )
    assert cards_source is not None
    assert "grouped" in cards_source
    assert "_card_version" in cards_source


def test_services_layer_never_imports_astrbot() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "services").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import astrbot", "from astrbot")):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_requirements_cover_runtime_dependencies() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for package in ("aiohttp", "aiosqlite", "tzdata"):
        assert package in requirements
