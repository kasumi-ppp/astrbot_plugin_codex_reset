"""AC-24 与 F08：队列去重、并发发送、重试 / uncertain / skipped 状态机。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from typing import Any

from conftest import RecordingSend, load_fixture, make_worker, run, utc

from services.classifier import EventClassifier
from services.clock import FakeClock
from services.models import (
    Baseline,
    ForecastDecision,
    GroupOverride,
    LogicalEvent,
    PluginSettings,
)
from services.notifications import decision_payload
from services.parser import parse_timeline
from services.repository import Repository

UMO = "aiocqhttp:GroupMessage:2002"


async def _subscribe(
    repo: Repository,
    *,
    override: GroupOverride | None = None,
    enabled: bool = True,
) -> Any:
    sub = await repo.upsert_subscription(
        umo=UMO,
        platform="aiocqhttp",
        group_id="2002",
        group_name="投递测试群",
        override=override or GroupOverride(),
    )
    if not enabled:
        # 仓库没有暴露 enabled 写入口（正常路径是 /cr unsubscribe 删除订阅），
        # 这里用一条独立连接模拟「历史残留投递 + 已停用订阅」。
        conn = sqlite3.connect(repo.db_path)
        try:
            conn.execute("UPDATE subscriptions SET enabled = 0 WHERE umo = ?", (UMO,))
            conn.commit()
        finally:
            conn.close()
        refreshed = await repo.get_subscription(UMO)
        assert refreshed is not None
        return refreshed
    return sub


def _event(
    logical_id: str,
    *,
    kind: str = "reset",
    state_version: str = "confirmed",
    payload: dict[str, Any] | None = None,
) -> LogicalEvent:
    return LogicalEvent(
        logical_id=logical_id,
        kind=kind,
        group_kind=kind,
        state_version=state_version,
        occurred_at=utc(2026, 10, 20, 6, 33),
        payload=payload if payload is not None else {"occurred_at": "2026-10-20T06:33:48+00:00"},
    )


# ---------------------------------------------------------------------------
# AC-24：并发入队只 1 行；两个 Worker 并发只发 1 次
# ---------------------------------------------------------------------------
def test_concurrent_enqueue_creates_single_row(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        event = _event("reset:2112432076767289678")
        results = await asyncio.gather(
            *[repo.enqueue_delivery(sub=sub, ev=event) for _ in range(10)]
        )
        assert sum(1 for created in results if created) == 1
        assert await repo.count_deliveries() == 1

    run(scenario())


def test_two_workers_concurrently_send_once(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:2112432076767289678"))

        send_one = RecordingSend()
        send_two = RecordingSend()
        worker_one = make_worker(repo, send_one, clock)
        worker_two = make_worker(repo, send_two, clock)

        now = clock.utcnow()
        await asyncio.gather(worker_one.run_once(now), worker_two.run_once(now))

        assert len(send_one.calls) + len(send_two.calls) == 1  # 认领是原子的
        assert await repo.count_deliveries("delivered") == 1
        assert await repo.count_deliveries("inflight") == 0

    run(scenario())


def test_ten_concurrent_run_once_only_one_delivery(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend()
        workers = [make_worker(repo, send, clock) for _ in range(10)]
        now = clock.utcnow()
        await asyncio.gather(*[worker.run_once(now) for worker in workers])
        assert len(send.calls) == 1

    run(scenario())


# ---------------------------------------------------------------------------
# 送达 / 重试 / 不确定
# ---------------------------------------------------------------------------
def test_send_outcomes_deliver_retry_and_uncertain(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        await repo.enqueue_delivery(
            sub=sub,
            ev=_event("watch:2", kind="watch", state_version="active:nowindow", payload={
                "state": "active",
                "window": None,
                "url": None,
            }),
        )
        await repo.enqueue_delivery(
            sub=sub,
            ev=_event(
                "credits:3",
                kind="credits",
                state_version="announced",
                payload={"state": "announced", "date": "2026-09-03", "url": None},
            ),
        )

        send = RecordingSend([True, False, TimeoutError()])
        worker = make_worker(repo, send, clock)
        now = clock.utcnow()
        delivered = await worker.run_once(now)
        assert delivered == 1
        assert len(send.calls) == 3

        rows = {task.logical_id: task for task in await repo.list_deliveries()}
        assert rows["reset:1"].status == "delivered"
        assert rows["watch:2"].status == "retry"
        assert rows["watch:2"].next_attempt_at == now + timedelta(seconds=30)
        assert rows["credits:3"].status == "uncertain"  # 超时不重发（F08）
        assert rows["credits:3"].next_attempt_at is None

        # 同一时刻再跑一轮：uncertain 与未到期的 retry 都不应被重发
        again = RecordingSend()
        delivered_again = await make_worker(repo, again, clock).run_once(now)
        assert delivered_again == 0
        assert again.calls == []

    run(scenario())


def test_three_failures_become_terminal_failed(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend([False, False, False])
        worker = make_worker(repo, send, clock)

        now = clock.utcnow()
        await worker.run_once(now)
        await worker.run_once(now + timedelta(seconds=30))
        await worker.run_once(now + timedelta(seconds=150))

        rows = await repo.list_deliveries()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].attempt == 3
        assert rows[0].next_attempt_at is None
        assert rows[0].error_code == "adapter_unavailable"

        current = await repo.get_subscription(UMO)
        assert current is not None
        assert current.last_failure_at is not None
        assert current.uncertain_count == 1

        # 终态不再被认领
        assert await worker.run_once(now + timedelta(hours=1)) == 0

    run(scenario())


# ---------------------------------------------------------------------------
# skipped：订阅停用 / 类别关闭
# ---------------------------------------------------------------------------
def test_disabled_subscription_is_skipped(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo, enabled=False)
        assert sub.enabled is False
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 0
        assert send.calls == []
        rows = await repo.list_deliveries()
        assert rows[0].status == "skipped"
        assert rows[0].error_code == "subscription_inactive"

    run(scenario())


def test_disabled_category_is_skipped(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo, override=GroupOverride(enable_confirmed_reset=False))
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 0
        assert send.calls == []
        rows = await repo.list_deliveries()
        assert rows[0].status == "skipped"
        assert rows[0].error_code == "category_disabled"

        current = await repo.get_subscription(UMO)
        assert current is not None
        assert current.uncertain_count == 0  # skipped 不计入 uncertain

    run(scenario())


def test_unknown_kind_fails_without_sending(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(
            sub=sub, ev=_event("weird:1", kind="boost", state_version="1", payload={})
        )
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 0
        assert send.calls == []
        rows = await repo.list_deliveries()
        assert rows[0].status == "failed"
        assert rows[0].error_code == "unknown_kind"

    run(scenario())


# ---------------------------------------------------------------------------
# SendFn 第三参 at_all：只有确认重置 + allow_at_all 才为 True
# ---------------------------------------------------------------------------
def test_at_all_only_for_reset_when_enabled(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        await repo.enqueue_delivery(
            sub=sub,
            ev=_event("watch:2", kind="watch", state_version="active:nowindow", payload={"state": "active"}),
        )
        send = RecordingSend()
        worker = make_worker(repo, send, clock, settings=PluginSettings(allow_at_all=True))
        await worker.run_once(clock.utcnow())

        assert [call[2] for call in send.calls] == [True, False]

    run(scenario())


def test_at_all_disabled_by_default(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend()
        await make_worker(repo, send, clock).run_once(clock.utcnow())
        assert send.calls[0][2] is False

    run(scenario())


# ---------------------------------------------------------------------------
# 预测事件 payload ↔ 模板
# ---------------------------------------------------------------------------
def test_forecast_delivery_renders_from_payload(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        decision = ForecastDecision(
            logical_id="forecast:sub:x:h24",
            state_version="h24:0:1",
            window_kind="h24",
            rounded_24h=54,
            rounded_48h=73,
            confidence="low",
            updated_at=utc(2026, 10, 20, 8, 0),
        )
        event = LogicalEvent(
            logical_id=decision.logical_id,
            kind="forecast",
            group_kind="forecast",
            state_version=decision.state_version,
            occurred_at=decision.updated_at,
            payload=decision_payload(decision),
        )
        await repo.enqueue_delivery(sub=sub, ev=event)
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 1

        text = send.calls[0][1]
        assert "📈【Codex 重置概率升高】" in text
        assert "未来 24 小时：54%" in text
        assert "实验性" in text
        assert "已确认" not in text
        assert "数据来源：https://codex-reset.com/" in text

        rows = await repo.list_deliveries()
        assert rows[0].status == "delivered"
        conn = sqlite3.connect(repo.db_path)
        try:
            stored = conn.execute(
                "SELECT message_hash FROM deliveries WHERE logical_id = ?", (event.logical_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert isinstance(stored, str)
        assert len(stored) == 12  # sha1(text)[:12]

    run(scenario())


# ---------------------------------------------------------------------------
# 更正事件：复用原类别 kind，但必须用更正模板渲染
# ---------------------------------------------------------------------------
def test_correction_event_uses_correction_template(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(
            sub=sub,
            ev=_event(
                "watch:2",
                kind="watch",
                state_version="expired:2026-10-21T12:00:00+00:00",
                payload={
                    "reason": "expired",
                    "alert_id": "2",
                    "state": "expired",
                    "window": None,
                    "url": None,
                },
            ),
        )
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 1
        text = send.calls[0][1]
        assert "🔁【更正】此前的重置预告未获确认" in text
        assert "提前预警" not in text

    run(scenario())


def test_correction_follows_original_category_gate(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo, override=GroupOverride(enable_watch=False))
        await repo.enqueue_delivery(
            sub=sub,
            ev=_event(
                "watch:2",
                kind="watch",
                state_version="corrected",
                payload={"reason": "corrected", "alert_id": "2", "url": None},
            ),
        )
        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 0
        assert send.calls == []
        rows = await repo.list_deliveries()
        assert rows[0].status == "skipped"  # 门控跟随原类别（watch）

    run(scenario())


# ---------------------------------------------------------------------------
# 跨模块契约：分类器 → 仓库（别名/去重）→ Worker → SendFn
# ---------------------------------------------------------------------------
def test_classifier_to_delivery_end_to_end(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        timeline = parse_timeline(
            load_fixture("timeline_current"), fetched_at=clock.utcnow()
        ).snapshot
        assert timeline is not None
        events = EventClassifier(clock=clock).reset_candidates(
            forecast=None, timeline=timeline, feed=None, baseline=Baseline()
        )
        target = next(
            event for event in events if event.payload["event_id"] == "2112432076767289678"
        )

        assert await repo.record_logical_event(target, baseline=False) is True
        for alias in target.payload["aliases"]:
            await repo.add_alias(alias, target.logical_id)
        assert await repo.enqueue_delivery(sub=sub, ev=target) is True

        send = RecordingSend()
        assert await make_worker(repo, send, clock).run_once(clock.utcnow()) == 1
        text = send.calls[0][1]
        assert "✅【Codex 全球重置已确认】" in text
        assert "数据来源：https://codex-reset.com/" in text

        # 跨轮别名命中同一 canonical：同版本不再入库、不再投递（AC-03/AC-04 的契约基础）
        assert await repo.find_logical_by_alias("reset:2112432076767289678") == target.logical_id
        assert await repo.record_logical_event(target, baseline=False) is False
        assert await repo.enqueue_delivery(sub=sub, ev=target) is False
        assert await repo.count_deliveries() == 1
        assert await repo.latest_state_version(target.logical_id) == "confirmed"

    run(scenario())


# ---------------------------------------------------------------------------
# run() 循环与 stop()
# ---------------------------------------------------------------------------
def test_stopped_worker_run_returns_immediately(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        worker = make_worker(repo, RecordingSend(), clock)
        worker.stop()
        await asyncio.wait_for(worker.run(), timeout=5.0)  # 已置位 → 立即返回

    run(scenario())


def test_run_loop_processes_queue_then_can_be_stopped(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=_event("reset:1"))
        send = RecordingSend()
        worker = make_worker(repo, send, clock)

        task = asyncio.ensure_future(worker.run())
        for _ in range(50):
            if send.calls:
                break
            await asyncio.sleep(0)
        worker.stop()
        await asyncio.wait_for(task, timeout=5.0)
        assert len(send.calls) == 1

    run(scenario())
