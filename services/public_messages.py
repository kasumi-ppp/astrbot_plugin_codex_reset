"""Product labels, switch checks, and plain-text quota notices."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import GroupSettings, PluginSettings
from .public_sources import PRODUCTS, SOURCE_ENDPOINTS, SOURCES

PRODUCT_LABELS = {"codex": "Codex", **PRODUCTS}
PRODUCT_LABELS["antigravity-gemini"] = "Antigravity 内的 Gemini 额度"
ORIGINS = {
    product: SOURCE_ENDPOINTS[source][0].split("/api/", 1)[0] + "/"
    for source, products in SOURCES.items()
    for product in products
}
PUBLIC_STATE_LABELS = {
    "announced": "已预告", "reset": "已重置", "claimable": "可领取", "retracted": "更正：公告已撤回",
    "watch": "可能重置", "forecast": "可能重置",
}


def product_enabled(settings: PluginSettings, product: str) -> bool:
    return settings.enable_monitor and bool(getattr(settings, "enable_" + product.replace("-", "_"), False))


def public_notice_enabled(settings: GroupSettings, state: str, payload: Mapping[str, Any]) -> bool:
    if state in {"watch", "announced"}:
        return settings.enable_watch
    if state == "reset":
        return settings.enable_confirmed_reset
    if state == "claimable":
        return settings.enable_credits
    if state == "forecast":
        p24, p48 = payload["probabilities"]
        return settings.enable_forecast and (
            0 < settings.threshold_24h <= p24 or 0 < settings.threshold_48h <= p48
        )
    return state == "retracted"


def render_public(payload: Mapping[str, Any], *, test: bool = False) -> str | None:
    product = payload.get("product")
    state = payload.get("state")
    if product not in ORIGINS or state not in PUBLIC_STATE_LABELS:
        return None
    when = datetime.fromisoformat(payload["occurred_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
    prefix = "🧪【测试消息】" if test else ""
    lines = [
        f"{prefix}【{PRODUCT_LABELS[product]} · {PUBLIC_STATE_LABELS[state]}】",
        f"适用范围：{str(payload.get('scope') or '范围待确认')[:180]}",
        f"{'信号时间' if state in {'watch', 'forecast'} else '公告时间'}：{when:%Y-%m-%d %H:%M}（北京时间）",
    ]
    if state == "claimable":
        lines.append("请在对应产品的用量设置中查看领取资格。")
    if state == "claimable" and payload.get("expires_at"):
        expiry = datetime.fromisoformat(payload["expires_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
        lines.append(f"领取期限：{expiry:%Y-%m-%d %H:%M}（北京时间）")
    if state == "forecast":
        p24, p48 = payload["probabilities"]
        lines.append(f"WhenReset 估算：24h {p24}% · 48h {p48}%（尚未确认）")
    elif state == "watch":
        lines.append("上游预警，尚未确认。")
        if expiry := payload.get("expires_at"):
            until = datetime.fromisoformat(expiry).astimezone(ZoneInfo("Asia/Shanghai"))
            lines.append(f"预警有效至：{until:%m-%d %H:%M}（北京时间）")
    if payload.get("summary") and state != "forecast":
        lines.append(" ".join(str(payload["summary"]).split())[:460])
    if payload.get("source_url"):
        lines.append(f"{'预测数据' if state == 'forecast' else '原帖'}：{payload['source_url']}")
    lines.append(f"数据来源：{ORIGINS[product]}")
    return "\n".join(lines)
