"""Read and format confirmed resets for a group’s subscribed products."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .errors import SITE_URL
from .public_messages import ORIGINS, PRODUCT_LABELS

if TYPE_CHECKING:
    from .repository import Repository

RESET_REPORT_TITLE = "最近确认重置"
NO_SUBSCRIPTION_TEXT = "本群尚未订阅，请管理员使用 /cr subscribe <产品>。"
_BEIJING = ZoneInfo("Asia/Shanghai")


async def read_reset_summary(
    repo: Repository, products: Iterable[str], *, codex_fallback: datetime | None = None
) -> dict[str, str | None]:
    """Read the latest confirmation for each selected product in display order."""
    selected = set(products)
    rows = {}
    for product in PRODUCT_LABELS:
        if product in selected:
            reset_at = await repo.latest_confirmed_reset(product, codex_fallback=codex_fallback)
            rows[product] = reset_at.isoformat() if reset_at is not None else None
    return rows


def reset_summary_rows(rows: Mapping[str, str | None]) -> list[tuple[str, str]]:
    return [
        (
            PRODUCT_LABELS[product],
            f"已重置 · {datetime.fromisoformat(when).astimezone(_BEIJING):%Y-%m-%d %H:%M}"
            if when else "暂无确认记录",
        )
        for product, when in rows.items()
    ]


def reset_summary_sources(rows: Mapping[str, str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(SITE_URL if p == "codex" else ORIGINS[p] for p in rows))


def render_reset_summary_text(rows: Mapping[str, str | None], *, test: bool = False) -> str:
    title = f"📡【{RESET_REPORT_TITLE} · 北京时间】"
    if test:
        title = f"🧪【测试消息】{title}"
    lines = [title, *(f"{label}：{state}" for label, state in reset_summary_rows(rows))]
    if sources := reset_summary_sources(rows):
        lines.append("数据来源：" + "、".join(sources))
    return "\n".join(lines)
