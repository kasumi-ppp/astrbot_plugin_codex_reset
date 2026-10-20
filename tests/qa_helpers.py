"""QA 独立测试用的 payload 工厂与环境装配。

与工程师的 `conftest` 构造器**刻意分开**：这里只产出「上游原始 JSON」，
由测试自己调用真正的 `services.parser` / `services.pipeline` / `services.commands`，
因此断言的是 PRD 条款在真实代码路径上的表现，而不是替身的返回值。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from conftest import FakeClock, PipelineBundle, create_subscription, make_pipeline, open_repo, utc

from services.errors import ErrorCode
from services.models import Baseline, FetchResult, PluginSettings
from services.repository import Repository

UMO = "aiocqhttp:GroupMessage:1001"
UMO_B = "aiocqhttp:GroupMessage:2002"
PRIVATE_UMO = "aiocqhttp:FriendMessage:42"

#: 测试里统一使用的「现在」（采集轮次时间）
NOW = utc(2026, 10, 20, 12)

#: 上游真实样本里的确认重置 id（D-08 同源 id 别名依据）
REAL_RESET_ID = "2112432076767289678"
#: `announcement_state=none` 的真实行（AC-02 前半）
NONE_ROW_ID = "2111492673890824348"
#: `announced` 但 `confirmed=false` 的真实陷阱行（AC-02 后半 / D-02）
TRAP_ROW_ID = "2109045392684032299"


# ---------------------------------------------------------------------------
# payload 构造器（只产出上游原始 JSON）
# ---------------------------------------------------------------------------
def event_row(
    event_id: str,
    *,
    announced_at: str = "2026-10-20T06:33:48.000Z",
    group: str = "reset",
    summary: str = "Codex 全球额度重置已记录。",
    url: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造一条 timeline/feed 行；默认即「已确认重置」的合规形态。

    `extra` 用于覆盖或追加字段（例如 `announcement_state="none"`、`confirmed=False`）；
    需要「删除某字段」时由调用方 `pop`。
    """
    row: dict[str, Any] = {
        "id": event_id,
        "date": announced_at[:10],
        "type": group,
        "group": group,
        "summary": summary,
        "text": summary,
        "url": url if url is not None else f"https://x.com/thsottiaux/status/{event_id}",
        "announced_at": announced_at,
        "effective_at": announced_at,
        "official_window": None,
        "announcement_state": "announced",
        "banked_state": None,
        "banked_grant": False,
        "confirmed": True,
        "preview": False,
        "source": "live",
        "source_label": "Live radar feed",
        "confidence": "medium",
        "reset_kind": None,
        "observed_variant": None,
        "audience": ["web"],
        "incident_links": [],
        "reason_tags": [],
    }
    row.update(extra)
    return row


def confirmed_reset_row(event_id: str, announced_at: str) -> dict[str, Any]:
    """构造一条**合规**的确认重置行（F01 条件① 三条同时满足）。"""
    return event_row(event_id, announced_at=announced_at)


def credits_row(
    event_id: str,
    *,
    announced_at: str = "2026-10-07T00:00:00.000Z",
    banked_state: str | None = "available",
    banked_grant: bool = True,
    summary: str = "Banked reset granted.",
) -> dict[str, Any]:
    """构造一条 `group=credits` 行（注意：真实样本里没有 `announcement_state`）。"""
    row = event_row(event_id, announced_at=announced_at, group="credits", summary=summary)
    row.pop("announcement_state")
    row["banked_state"] = banked_state
    row["banked_grant"] = banked_grant
    row["reset_kind"] = "banked"
    return row


def timeline_payload(updated_at: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """构造 `/api/timeline` 响应体。"""
    return {"updated_at": updated_at, "events": list(rows)}


def forecast_payload(
    updated_at: str = "2026-10-20T11:50:00.000Z",
    *,
    last_reset_at: str | None = None,
    alert: dict[str, Any] | None = None,
    p24: int = 22,
    p48: int = 39,
    confidence: str = "low",
) -> dict[str, Any]:
    """构造 `/api/forecast` 响应体（字段名与 OpenAPI 一致）。"""
    return {
        "mode": "model",
        "updated_at": updated_at,
        "probabilities": {"rounded_24h": p24, "rounded_48h": p48},
        "confidence": confidence,
        "last_reset_at": last_reset_at,
        "official_signal": None,
        "latest_alert": alert,
    }


def reset_alert(
    alert_id: str = REAL_RESET_ID,
    *,
    source_at: str = "2026-10-20T06:33:48.000Z",
) -> dict[str, Any]:
    """构造 `latest_alert`（reset / confirmed，F01 条件③）。"""
    return {
        "id": alert_id,
        "kind": "reset",
        "state": "confirmed",
        "summary": "we're still cooking... and reset. Enjoy.",
        "source_at": source_at,
        "url": f"https://x.com/thsottiaux/status/{alert_id}",
        "score": None,
        "window": None,
        "corrected": False,
    }


def watch_alert(
    alert_id: str = "9000000000000000001",
    *,
    state: str = "active",
    source_at: str = "2026-10-20T11:00:00.000Z",
    start_at: str | None = "2026-10-21T12:00:00.000Z",
    end_at: str | None = "2026-10-21T14:00:00.000Z",
    corrected: bool = False,
) -> dict[str, Any]:
    """构造 `latest_alert`（watch 类）。"""
    window = None
    if start_at is not None or end_at is not None:
        window = {
            "label": "20:00 - 22:00 Asia/Shanghai",
            "start_at": start_at,
            "end_at": end_at,
            "time_zone": "Asia/Shanghai",
        }
    return {
        "id": alert_id,
        "kind": "watch",
        "state": state,
        "summary": "Reset likely in the announced window.",
        "source_at": source_at,
        "url": f"https://x.com/thsottiaux/status/{alert_id}",
        "score": 70,
        "window": window,
        "corrected": corrected,
    }


def feed_payload(
    fetched_at: str = "2026-10-20T11:55:00.000Z",
    *,
    events: Sequence[dict[str, Any]] = (),
    stale: bool = False,
    observations: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """构造 `/api/feed` 响应体。"""
    return {
        "fetched_at": fetched_at,
        "source": "x",
        "stale": stale,
        "tweets": [],
        "banked_observations": list(observations),
        "events": list(events),
        "newest_post_at": fetched_at,
    }


def ok_fetch(
    endpoint: str,
    payload: dict[str, Any],
    *,
    at: datetime = NOW,
    batch: str | None = None,
) -> FetchResult:
    """构造一次成功采集结果（默认无 batch，避免触发 D-11 批次去重）。"""
    return FetchResult(
        endpoint=endpoint,
        ok=True,
        status=200,
        error_code=ErrorCode.OK,
        payload=payload,
        fetched_at=at,
        batch=batch,
        checked_at=None,
    )


# ---------------------------------------------------------------------------
# 环境装配
# ---------------------------------------------------------------------------
async def make_env(
    tmp_path: Any,
    *,
    umos: Sequence[str] = (UMO,),
    subscribed_at: datetime = utc(2026, 9, 1),
    baseline: Baseline | None = None,
    settings: PluginSettings | None = None,
) -> tuple[Repository, PipelineBundle, FakeClock]:
    """建库 + 建订阅（订阅时间远早于测试事件，确保「首订阅不追送历史」不干扰结论）。

    返回 `(repo, bundle, clock)`；`clock` 默认停在 2026-09-01，调用方可自由 `set_now`。
    """
    clock = FakeClock(subscribed_at)
    repo = await open_repo(tmp_path, clock=clock)
    bundle = make_pipeline(repo, clock, settings=settings)
    for index, umo in enumerate(umos):
        await create_subscription(repo, umo=umo, group_id=str(1001 + index))
    if baseline is not None:
        await repo.save_baseline(baseline)
    return repo, bundle, clock


@asynccontextmanager
async def env(
    tmp_path: Any,
    *,
    umos: Sequence[str] = (UMO,),
    subscribed_at: datetime = utc(2026, 9, 1),
    baseline: Baseline | None = None,
    settings: PluginSettings | None = None,
) -> AsyncIterator[tuple[Repository, PipelineBundle, FakeClock]]:
    """`make_env` 的上下文管理器版本：退出时关闭仓库（避免 aiosqlite 线程悬挂告警）。"""
    repo, bundle, clock = await make_env(
        tmp_path,
        umos=umos,
        subscribed_at=subscribed_at,
        baseline=baseline,
        settings=settings,
    )
    try:
        yield repo, bundle, clock
    finally:
        await repo.close()


def built_baseline(last_reset_at: datetime | None = None) -> Baseline:
    """构造「已经在运行」的基线（用于跳过 AC-06 的首轮基线模式）。"""
    return Baseline(built=True, last_reset_at=last_reset_at, built_at=utc(2026, 10, 19))


async def open_repo_or_fail(tmp_path: Any, *, clock: FakeClock | None = None) -> Repository:
    """打开一个临时仓库（时钟默认停在 `NOW`），供命令层测试使用。"""
    return await open_repo(tmp_path, clock=clock or FakeClock(NOW))


def reset_events(events: Sequence[Any]) -> list[Any]:
    """从逻辑事件列表里筛出 reset 类（断言用）。"""
    return [event for event in events if getattr(event, "kind", "") == "reset"]


def iso_z(value: datetime) -> str:
    """`datetime` → 上游风格的 ISO（毫秒 + Z）。"""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
