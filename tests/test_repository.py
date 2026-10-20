"""持久层测试：DDL/版本校验、唯一约束、别名、并发认领、重启恢复、D-12 快照语义。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import make_logical_event, run, utc

from services.clock import FakeClock
from services.errors import ErrorCode, SchemaTooNewError
from services.models import Baseline, FetchResult, ForecastState, GroupOverride
from services.repository import Repository

UMO = "aiocqhttp:GroupMessage:1001"


async def _subscribe(repo: Repository, umo: str = UMO, override: GroupOverride | None = None):
    return await repo.upsert_subscription(
        umo=umo,
        platform="aiocqhttp",
        group_id=umo.rsplit(":", 1)[-1],
        group_name="测试群",
        override=override or GroupOverride(),
    )


def _ok_fetch(endpoint: str, payload: dict[str, object], *, clock: FakeClock) -> FetchResult:
    return FetchResult(
        endpoint=endpoint,
        ok=True,
        status=200,
        error_code=ErrorCode.OK,
        payload=payload,
        fetched_at=clock.utcnow(),
        batch="1792491580134",
        cache_status="HIT",
        etag='"abc123"',
    )


# ---------------------------------------------------------------------------
# 建表 / 版本
# ---------------------------------------------------------------------------
def test_schema_created_with_version_two(repo: Repository) -> None:
    async def scenario() -> None:
        assert await repo.meta_get("schema_version") == "2"
        conn = sqlite3.connect(repo.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            columns = {row[1] for row in conn.execute("PRAGMA table_info(source_snapshots)").fetchall()}
        finally:
            conn.close()
        expected = {
            "meta",
            "subscriptions",
            "source_snapshots",
            "logical_events",
            "deliveries",
            "forecast_state",
        }
        assert expected.issubset(tables)
        assert "etag" in columns  # 架构修正：条件请求所需的 ETag 列
        assert "raw_json" in columns

    run(scenario())


def test_schema_version_too_new_raises(tmp_path: Path, clock: FakeClock) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES "
            "('schema_version', '3', '2026-10-20T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    repo = Repository(db_path, clock=clock)
    with pytest.raises(SchemaTooNewError):
        run(repo.open())
    run(repo.close())


def test_schema_version_missing_is_initialised(tmp_path: Path, clock: FakeClock) -> None:
    repo = Repository(tmp_path / "fresh.db", clock=clock)

    async def scenario() -> None:
        await repo.open()
        assert await repo.meta_get("schema_version") == "2"
        await repo.close()

    run(scenario())


# ---------------------------------------------------------------------------
# 订阅
# ---------------------------------------------------------------------------
def test_subscription_upsert_settings_and_delete(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        assert sub.umo == UMO
        assert sub.subscription_id.startswith("sub:")
        assert sub.override == GroupOverride()  # 默认「无群级覆盖」，全部继承全局默认

        updated = GroupOverride(enable_watch=False, threshold_24h=80)
        assert await repo.update_subscription_override(UMO, updated) is True
        reloaded = await repo.get_subscription(UMO)
        assert reloaded is not None
        assert reloaded.override == updated
        assert reloaded.version == sub.version + 1

        # 稀疏落库：settings_json 只含显式覆盖的字段（改动 1 的核心断言）
        conn = sqlite3.connect(repo.db_path)
        try:
            stored = conn.execute(
                "SELECT settings_json FROM subscriptions WHERE umo = ?", (UMO,)
            ).fetchone()
        finally:
            conn.close()
        assert json.loads(stored[0]) == {"enable_watch": False, "threshold_24h": 80}

        # 旧方法名保留为别名，语义相同
        assert await repo.update_subscription_settings(UMO, GroupOverride(enable_credits=False))
        legacy = await repo.get_subscription(UMO)
        assert legacy is not None
        assert legacy.override.enable_credits is False
        conn = sqlite3.connect(repo.db_path)
        try:
            sparse = conn.execute(
                "SELECT settings_json FROM subscriptions WHERE umo = ?", (UMO,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert json.loads(sparse) == {"enable_credits": False}

        # 订阅 umo 唯一：重复 subscribe 只更新不新增
        again = await _subscribe(repo)
        assert again.subscription_id == sub.subscription_id
        assert len(await repo.list_subscriptions()) == 1

        # 级联删除投递与预测状态
        await repo.enqueue_delivery(sub=reloaded, ev=make_logical_event())
        assert await repo.count_deliveries() == 1
        assert await repo.delete_subscription(UMO) is True
        assert await repo.list_subscriptions() == []
        assert await repo.count_deliveries() == 0

    run(scenario())


def test_corrupt_settings_json_falls_back_to_empty_override(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        conn = sqlite3.connect(repo.db_path)
        try:
            conn.execute(
                "UPDATE subscriptions SET settings_json = ? WHERE subscription_id = ?",
                ("{not json", sub.subscription_id),
            )
            conn.commit()
        finally:
            conn.close()
        row = await repo.get_subscription(UMO)
        assert row is not None
        assert row.override == GroupOverride()

    run(scenario())


def test_subscription_health_reports_queue_counts(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event())
        health = await repo.subscription_health()
        assert len(health) == 1
        assert health[0]["subscription_id"] == sub.subscription_id
        assert health[0]["pending_count"] == 1
        assert len(health[0]["umo_hash"]) == 8  # 不打完整 umo

    run(scenario())


# ---------------------------------------------------------------------------
# 逻辑事件 / 别名
# ---------------------------------------------------------------------------
def test_record_logical_event_dedupes_on_primary_key(repo: Repository) -> None:
    async def scenario() -> None:
        ev = make_logical_event()
        assert await repo.record_logical_event(ev, baseline=False) is True
        assert await repo.record_logical_event(ev, baseline=False) is False
        assert len(await repo.list_events(group_kind="reset", limit=10)) == 1

    run(scenario())


def test_baseline_events_are_stored_without_deliveries(repo: Repository) -> None:
    async def scenario() -> None:
        ev = make_logical_event()
        assert await repo.record_logical_event(ev, baseline=True) is True
        assert await repo.count_deliveries() == 0  # baseline 只入库不投递（AC-06）

    run(scenario())


def test_alias_roundtrip_keeps_first_canonical(repo: Repository) -> None:
    async def scenario() -> None:
        assert await repo.find_logical_by_alias("reset:2112432076767289678") is None
        await repo.add_alias("reset:2112432076767289678", "reset:ts:2026-10-20T06:33:48+00:00")
        assert (
            await repo.find_logical_by_alias("reset:2112432076767289678")
            == "reset:ts:2026-10-20T06:33:48+00:00"
        )
        # 重复登记不覆盖首个 canonical（D-08）
        await repo.add_alias("reset:2112432076767289678", "reset:other")
        assert (
            await repo.find_logical_by_alias("reset:2112432076767289678")
            == "reset:ts:2026-10-20T06:33:48+00:00"
        )

    run(scenario())


def test_latest_state_version(repo: Repository) -> None:
    async def scenario() -> None:
        await repo.record_logical_event(make_logical_event(state_version="h24:0"), baseline=False)
        await repo.record_logical_event(make_logical_event(state_version="h24:1"), baseline=False)
        assert await repo.latest_state_version("reset:2112432076767289678") in {"h24:0", "h24:1"}

    run(scenario())


# ---------------------------------------------------------------------------
# 投递队列
# ---------------------------------------------------------------------------
def test_duplicate_enqueue_returns_false(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        ev = make_logical_event()
        assert await repo.enqueue_delivery(sub=sub, ev=ev) is True
        assert await repo.enqueue_delivery(sub=sub, ev=ev) is False
        assert await repo.count_deliveries() == 1
        task = (await repo.list_deliveries())[0]
        assert task.status == "pending"
        assert task.attempt == 0

    run(scenario())


def test_concurrent_enqueue_creates_single_row(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        ev = make_logical_event()
        results = await asyncio.gather(*[repo.enqueue_delivery(sub=sub, ev=ev) for _ in range(10)])
        assert sum(1 for flag in results if flag) == 1
        assert await repo.count_deliveries() == 1

    run(scenario())


def test_claim_pending_deliveries_is_concurrency_safe(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event())
        now = utc(2026, 10, 20, 12)
        batches = await asyncio.gather(
            *[repo.claim_pending_deliveries(now, 10) for _ in range(10)]
        )
        claimed = [task for batch in batches for task in batch]
        assert len(claimed) == 1  # 并发认领不重复
        assert claimed[0].status == "inflight"
        assert claimed[0].attempt == 1
        assert await repo.count_deliveries("pending") == 0

    run(scenario())


def test_claim_respects_next_attempt_at(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        now = utc(2026, 10, 20, 12)
        await repo.enqueue_delivery(
            sub=sub,
            ev=make_logical_event(),
            next_attempt_at=now + timedelta(seconds=30),
        )
        assert await repo.claim_pending_deliveries(now, 10) == []
        assert len(await repo.claim_pending_deliveries(now + timedelta(seconds=31), 10)) == 1

    run(scenario())


def test_requeue_inflight_as_uncertain(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event())
        now = utc(2026, 10, 20, 12)
        claimed = await repo.claim_pending_deliveries(now, 10)
        assert len(claimed) == 1

        assert await repo.requeue_inflight_as_uncertain(now) == 1
        uncertain = await repo.list_deliveries(status="uncertain")
        assert len(uncertain) == 1
        assert uncertain[0].error_code == ErrorCode.INTERRUPTED
        assert await repo.claim_pending_deliveries(now, 10) == []  # 不自动重发（F08）

    run(scenario())


def test_mark_delivered_and_failed(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        now = utc(2026, 10, 20, 12)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event(state_version="confirmed"))
        task = (await repo.claim_pending_deliveries(now, 10))[0]
        await repo.mark_delivered(task, now=now, message_hash="deadbeef")
        delivered = await repo.get_subscription(UMO)
        assert delivered is not None
        assert delivered.last_success_at == now

        # 失败：attempt < 3 → retry，写 next_attempt_at
        await repo.enqueue_delivery(
            sub=sub, ev=make_logical_event(logical_id="reset:2", state_version="confirmed")
        )
        task2 = (await repo.claim_pending_deliveries(now, 10))[0]
        retry_at = now + timedelta(seconds=30)
        await repo.mark_failed(
            task2,
            now=now,
            error_code=ErrorCode.ADAPTER_UNAVAILABLE,
            next_attempt_at=retry_at,
        )
        retry_rows = await repo.list_deliveries(status="retry")
        assert len(retry_rows) == 1
        assert retry_rows[0].next_attempt_at == retry_at

        # attempt >= 3 → 终态 failed
        task2.attempt = 3
        await repo.mark_failed(
            task2,
            now=now,
            error_code=ErrorCode.ADAPTER_UNAVAILABLE,
            next_attempt_at=retry_at,
        )
        failed = await repo.list_deliveries(status="failed")
        assert len(failed) == 1
        assert failed[0].next_attempt_at is None

    run(scenario())


def test_mark_uncertain_is_terminal(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        now = utc(2026, 10, 20, 12)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event())
        task = (await repo.claim_pending_deliveries(now, 10))[0]
        await repo.mark_uncertain(task, now=now, error_code=ErrorCode.TIMEOUT)
        assert task.status == "uncertain"
        assert await repo.claim_pending_deliveries(now, 10) == []
        current = await repo.get_subscription(UMO)
        assert current is not None
        assert current.uncertain_count == 1

    run(scenario())


def test_mark_skipped_is_terminal_and_not_counted_as_uncertain(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        now = utc(2026, 10, 20, 12)
        await repo.enqueue_delivery(sub=sub, ev=make_logical_event())
        task = (await repo.claim_pending_deliveries(now, 10))[0]
        await repo.mark_skipped(task, now=now, reason="category_disabled")

        assert task.status == "skipped"
        assert task.error_code == "category_disabled"
        assert task.next_attempt_at is None
        assert await repo.count_deliveries("skipped") == 1
        assert await repo.claim_pending_deliveries(now, 10) == []  # 终态，无重试

        current = await repo.get_subscription(UMO)
        assert current is not None
        assert current.uncertain_count == 0  # 不计入 uncertain

    run(scenario())


# ---------------------------------------------------------------------------
# 快照（D-12）
# ---------------------------------------------------------------------------
def test_upsert_snapshot_ok_then_failure_does_not_overwrite_raw_json(
    repo: Repository, clock: FakeClock
) -> None:
    async def scenario() -> None:
        payload = {"mode": "model", "updated_at": "2026-10-20T10:19:48.712Z"}
        row = await repo.upsert_snapshot(
            "forecast",
            _ok_fetch("forecast", payload, clock=clock),
            snapshot_payload=payload,
            updated_at=utc(2026, 10, 20, 10, 19),
        )
        assert row.ok is True
        assert row.error_code == ErrorCode.OK
        assert row.batch == "1792491580134"

        failure = FetchResult(
            endpoint="forecast",
            ok=False,
            status=502,
            error_code=ErrorCode.HTTP_5XX,
            fetched_at=clock.utcnow(),
        )
        failed_row = await repo.upsert_snapshot(
            "forecast", failure, snapshot_payload=None, updated_at=None
        )
        assert failed_row.ok is False
        assert failed_row.error_code == ErrorCode.HTTP_5XX
        # 失败轮次不得覆盖 raw_json / fetched_at / updated_at
        assert failed_row.fetched_at == row.fetched_at
        assert failed_row.updated_at == row.updated_at

        conn = sqlite3.connect(repo.db_path)
        try:
            stored = conn.execute(
                "SELECT raw_json, fetched_at, ok, error_code FROM source_snapshots WHERE endpoint='forecast'"
            ).fetchone()
        finally:
            conn.close()
        assert json.loads(stored[0]) == payload
        assert stored[2] == 0
        assert stored[3] == ErrorCode.HTTP_5XX

    run(scenario())


def test_not_modified_snapshot_keeps_payload(repo: Repository, clock: FakeClock) -> None:
    async def scenario() -> None:
        payload = {"events": []}
        first = await repo.upsert_snapshot(
            "timeline",
            _ok_fetch("timeline", payload, clock=clock),
            snapshot_payload=payload,
            updated_at=utc(2026, 10, 20, 10, 19),
        )
        assert first.ok is True

        not_modified = FetchResult(
            endpoint="timeline",
            ok=False,
            status=304,
            error_code=ErrorCode.NOT_MODIFIED,
            fetched_at=clock.utcnow(),
            batch="1792491580134",
        )
        row = await repo.upsert_snapshot("timeline", not_modified, snapshot_payload=None, updated_at=None)
        assert row.ok is True
        assert row.error_code == ErrorCode.NOT_MODIFIED
        assert row.fetched_at == first.fetched_at
        assert await repo.snapshot_etag("timeline") == '"abc123"'

    run(scenario())


# ---------------------------------------------------------------------------
# 预测状态 / 基线 / 审计
# ---------------------------------------------------------------------------
def test_forecast_state_default_and_roundtrip(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        default = await repo.get_forecast_state(sub.subscription_id)
        assert default == ForecastState(subscription_id=sub.subscription_id)
        assert default.armed_24h is True
        assert default.epoch_seq == 0

        state = ForecastState(
            subscription_id=sub.subscription_id,
            hits_24h=2,
            armed_24h=False,
            cooldown_until=utc(2026, 10, 21),
            day_key="2026-10-20",
            day_count=1,
            epoch_seq=3,
            alert_seq=2,
            last_prob_24h=54,
        )
        await repo.save_forecast_state(state, now=utc(2026, 10, 20, 12))
        loaded = await repo.get_forecast_state(sub.subscription_id)
        assert loaded.hits_24h == 2
        assert loaded.armed_24h is False
        assert loaded.cooldown_until == utc(2026, 10, 21)
        assert loaded.epoch_seq == 3
        assert loaded.alert_seq == 2  # 改动 3：当日第 2 条预测的版本能力
        assert loaded.last_prob_24h == 54

        assert await repo.reset_all_forecast_epochs(utc(2026, 10, 20, 13)) == 1
        after = await repo.get_forecast_state(sub.subscription_id)
        assert after.epoch_seq == 4
        assert after.alert_seq == 2  # epoch 变化不影响 alert_seq
        assert after.armed_24h is True
        assert after.hits_24h == 0

    run(scenario())


def test_legacy_forecast_state_table_gets_alert_seq_column(tmp_path: Path, clock: FakeClock) -> None:
    """改动 3：旧库（无 alert_seq 列）在 open() 时自动 ALTER，不丢数据。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES "
            "('schema_version', '2', '2026-10-20T00:00:00+00:00')"
        )
        conn.execute(
            "CREATE TABLE forecast_state ("
            "subscription_id TEXT PRIMARY KEY, hits_24h INTEGER NOT NULL DEFAULT 0,"
            " hits_48h INTEGER NOT NULL DEFAULT 0, armed_24h INTEGER NOT NULL DEFAULT 1,"
            " armed_48h INTEGER NOT NULL DEFAULT 1, cooldown_until TEXT,"
            " day_key TEXT NOT NULL DEFAULT '', day_count INTEGER NOT NULL DEFAULT 0,"
            " epoch_seq INTEGER NOT NULL DEFAULT 0, last_prob_24h INTEGER, last_prob_48h INTEGER,"
            " last_sample_at TEXT, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO forecast_state (subscription_id, hits_24h, epoch_seq, updated_at)"
            " VALUES ('sub:legacy', 1, 5, '2026-10-20T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    repo = Repository(db_path, clock=clock)

    async def scenario() -> None:
        await repo.open()
        conn2 = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn2.execute("PRAGMA table_info(forecast_state)").fetchall()}
            stored = conn2.execute(
                "SELECT epoch_seq, alert_seq FROM forecast_state WHERE subscription_id='sub:legacy'"
            ).fetchone()
        finally:
            conn2.close()
        assert "alert_seq" in columns
        assert stored == (5, 0)  # 旧行默认 0，epoch 保留
        await repo.close()

    run(scenario())


def test_baseline_roundtrip(repo: Repository) -> None:
    async def scenario() -> None:
        empty = await repo.baseline()
        assert empty.built is False

        baseline = Baseline(
            built=True,
            last_reset_at=utc(2026, 10, 20, 6),
            watch_source_at=utc(2026, 10, 19, 20),
            built_at=utc(2026, 10, 20, 10),
        )
        await repo.save_baseline(baseline)
        restored = await repo.baseline()
        assert restored == baseline

    run(scenario())


def test_audit_appends_jsonl_without_message_body(repo: Repository) -> None:
    async def scenario() -> None:
        await repo.audit(
            {
                "operator_id": "42",
                "umo": UMO,
                "action": "subscribe",
                "result": "ok",
            }
        )
        files = list(repo.audit_dir.glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record) == {"operator_id", "umo_hash", "action", "result", "at"}
        assert record["operator_id"] == "42"
        assert record["action"] == "subscribe"
        assert UMO not in lines[0]  # 不落完整 umo
        assert record["at"].endswith("+00:00")

    run(scenario())


def test_meta_helpers(repo: Repository) -> None:
    async def scenario() -> None:
        await repo.meta_set("runtime:owner", "1234:boot")
        assert await repo.meta_get("runtime:owner") == "1234:boot"
        values = await repo.meta_get_all("runtime:")
        assert values == {"runtime:owner": "1234:boot"}
        assert await repo.meta_delete("runtime:owner") is True
        assert await repo.meta_get("runtime:owner") is None

    run(scenario())


# ---------------------------------------------------------------------------
# 批次 C：新增读接口（get_snapshot_raw / list_events_since / has_delivery）
# ---------------------------------------------------------------------------
def test_get_snapshot_raw_returns_payload_and_survives_failures(
    repo: Repository, clock: FakeClock
) -> None:
    async def scenario() -> None:
        assert await repo.get_snapshot_raw("forecast") is None

        payload = {
            "updated_at": "2026-10-20T10:00:00Z",
            "probabilities": {"rounded_24h": 22, "rounded_48h": 39},
        }
        await repo.upsert_snapshot(
            "forecast",
            _ok_fetch("forecast", payload, clock=clock),
            snapshot_payload=payload,
            updated_at=clock.utcnow(),
        )
        assert await repo.get_snapshot_raw("forecast") == payload

        # D-12：失败轮次不得覆盖 raw_json
        await repo.upsert_snapshot(
            "forecast",
            FetchResult(
                endpoint="forecast",
                ok=False,
                status=502,
                error_code=ErrorCode.HTTP_5XX,
                fetched_at=clock.utcnow(),
            ),
        )
        assert await repo.get_snapshot_raw("forecast") == payload
        assert (await repo.get_snapshot("forecast")).error_code == ErrorCode.HTTP_5XX

        # 脏数据：返回 None 而不是抛异常
        conn = sqlite3.connect(repo.db_path)
        try:
            conn.execute(
                "UPDATE source_snapshots SET raw_json = ? WHERE endpoint = 'forecast'", ("{broken",)
            )
            conn.commit()
        finally:
            conn.close()
        assert await repo.get_snapshot_raw("forecast") is None

    run(scenario())


def test_list_events_since_filters_by_kind_and_time(repo: Repository) -> None:
    async def scenario() -> None:
        base = utc(2026, 10, 20, 12, 0)
        await repo.record_logical_event(
            make_logical_event("reset:1", occurred_at=base - timedelta(hours=2)), baseline=False
        )
        await repo.record_logical_event(
            make_logical_event(
                "credits:1",
                kind="credits",
                group_kind="credits",
                occurred_at=base - timedelta(hours=1),
            ),
            baseline=False,
        )
        await repo.record_logical_event(
            make_logical_event(
                "watch:1",
                kind="watch",
                group_kind="watch",
                occurred_at=base - timedelta(minutes=30),
            ),
            baseline=False,
        )

        recent = await repo.list_events_since(
            kinds=("reset", "credits"), since=base - timedelta(hours=6), limit=10
        )
        # 升序返回，且 watch 永不进入补发范围
        assert [ev.logical_id for ev in recent] == ["reset:1", "credits:1"]

        windowed = await repo.list_events_since(
            kinds=("reset", "credits"), since=base - timedelta(minutes=90), limit=10
        )
        assert [ev.logical_id for ev in windowed] == ["credits:1"]

        # kinds 为空 → 空结果（绝不退化成「全部类别」）
        assert await repo.list_events_since(kinds=(), since=base) == []
        assert await repo.list_events_since(kinds=("reset",), since=base) == []

    run(scenario())


def test_has_delivery_reports_existing_rows(repo: Repository) -> None:
    async def scenario() -> None:
        sub = await _subscribe(repo)
        event = make_logical_event("reset:77")
        assert await repo.has_delivery(sub.subscription_id, event.logical_id) is False
        assert await repo.enqueue_delivery(sub=sub, ev=event) is True
        assert await repo.has_delivery(sub.subscription_id, event.logical_id) is True
        assert await repo.has_delivery("sub:not-exist", event.logical_id) is False

        # 任何终态都算「已处理过」，补发不得重复推送
        tasks = await repo.claim_pending_deliveries(utc(2026, 10, 20, 12, 1), limit=5)
        assert len(tasks) == 1
        await repo.mark_skipped(tasks[0], now=utc(2026, 10, 20, 12, 1), reason="category_disabled")
        assert await repo.has_delivery(sub.subscription_id, event.logical_id) is True

    run(scenario())


def test_has_delivered_only_accepts_delivered_rows(repo: Repository) -> None:
    """P2-04：`has_delivered` 只认 `delivered`（F07「仅发给原消息已送达的群」）。"""

    async def scenario() -> None:
        sub = await _subscribe(repo)
        event = make_logical_event("watch:900")
        now = utc(2026, 10, 20, 12, 1)

        assert await repo.has_delivered(sub.subscription_id, event.logical_id) is False
        assert await repo.enqueue_delivery(sub=sub, ev=event) is True
        # pending 不算已送达
        assert await repo.has_delivered(sub.subscription_id, event.logical_id) is False

        tasks = await repo.claim_pending_deliveries(now, limit=5)
        assert len(tasks) == 1
        await repo.mark_delivered(tasks[0], now=now, message_hash="abc123")
        assert await repo.has_delivered(sub.subscription_id, event.logical_id) is True

        # 其他群没有该 logical_id 的送达记录
        assert await repo.has_delivered("sub:not-exist", event.logical_id) is False

    run(scenario())


def test_has_delivered_ignores_uncertain_rows(repo: Repository) -> None:
    """`uncertain`（可能已发出但不确定）不算已送达，因此不触发更正。"""

    async def scenario() -> None:
        sub = await _subscribe(repo)
        event = make_logical_event("watch:901")
        await repo.enqueue_delivery(sub=sub, ev=event)
        now = utc(2026, 10, 20, 12, 1)
        tasks = await repo.claim_pending_deliveries(now, limit=5)
        await repo.mark_uncertain(tasks[0], now=now, error_code=ErrorCode.TIMEOUT)
        assert await repo.has_delivery(sub.subscription_id, event.logical_id) is True
        assert await repo.has_delivered(sub.subscription_id, event.logical_id) is False

    run(scenario())


# ---------------------------------------------------------------------------
# P2-03：跨轮 ±2s 时间容差归并
# ---------------------------------------------------------------------------
def test_find_logical_by_time_matches_within_tolerance(repo: Repository) -> None:
    async def scenario() -> None:
        anchor = utc(2026, 10, 20, 6, 33, 48)
        event = make_logical_event("reset:2100000000000000001", occurred_at=anchor)
        assert await repo.record_logical_event(event, baseline=False) is True

        # ±1s 抖动仍归并到同一 canonical
        for delta in (-1, 0, 1):
            hit = await repo.find_logical_by_time(
                kind="reset",
                occurred_at=anchor + timedelta(seconds=delta),
                tolerance_seconds=2,
            )
            assert hit == event.logical_id

        # ±3s 超出容差：不同事件不得误并
        for delta in (-3, 3):
            assert (
                await repo.find_logical_by_time(
                    kind="reset",
                    occurred_at=anchor + timedelta(seconds=delta),
                    tolerance_seconds=2,
                )
                is None
            )

        # kind 必须匹配（watch 不得命中 reset 行）
        assert (
            await repo.find_logical_by_time(
                kind="watch", occurred_at=anchor, tolerance_seconds=2
            )
            is None
        )

    run(scenario())


def test_find_logical_by_time_returns_earliest_candidate(repo: Repository) -> None:
    """多候选时按 `occurred_at ASC` 取最早的一条（结果稳定、可复现）。"""

    async def scenario() -> None:
        anchor = utc(2026, 10, 20, 6, 33, 48)
        earlier = make_logical_event(
            "reset:2100000000000000001", occurred_at=anchor - timedelta(seconds=1)
        )
        later = make_logical_event(
            "reset:2100000000000000002", occurred_at=anchor + timedelta(seconds=1)
        )
        await repo.record_logical_event(later, baseline=False)
        await repo.record_logical_event(earlier, baseline=False)

        hit = await repo.find_logical_by_time(
            kind="reset", occurred_at=anchor, tolerance_seconds=2
        )
        assert hit == earlier.logical_id

    run(scenario())


def test_logical_kind_time_index_exists(repo: Repository) -> None:
    """P2-03：±2s 查询必须走索引（`kind, occurred_at`），不能全表扫。"""

    conn = sqlite3.connect(repo.db_path)
    try:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='logical_events'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "idx_logical_kind_time" in indexes


# ---------------------------------------------------------------------------
# P2-09 / P2-11：history 过滤降级更正、原子抢占
# ---------------------------------------------------------------------------
def test_list_events_excludes_downgraded_corrections(repo: Repository) -> None:
    """P2-09：降级更正与确认重置同为 `group_kind="reset"`，必须能显式排除。

    站点把某条 reset 降级时，`reset_corrections` 写的更正事件 group_kind 仍是
    `reset`（`state_version="downgraded:announced"`）；`/cr history` 若不排除，
    就会把「已撤回」当成「已确认」展示给用户。
    """

    async def scenario() -> tuple[list[str], list[str]]:
        await repo.record_logical_event(
            make_logical_event("reset:real", occurred_at=utc(2026, 10, 20, 6, 33)), baseline=False
        )
        await repo.record_logical_event(
            make_logical_event(
                "reset:revoked",
                state_version="downgraded:announced",
                occurred_at=utc(2026, 10, 20, 13, 0),
            ),
            baseline=False,
        )
        raw = await repo.list_events(group_kind="reset", limit=10)
        filtered = await repo.list_events(group_kind="reset", limit=10, exclude_corrections=True)
        return [e.logical_id for e in raw], [e.logical_id for e in filtered]

    raw, filtered = run(scenario())

    assert raw == ["reset:revoked", "reset:real"], "未过滤时应按时间倒序包含更正行"
    assert filtered == ["reset:real"], "过滤后只应剩下真正确认的重置"


def test_atomic_meta_rolls_back_on_exception(repo: Repository) -> None:
    """P2-11：`atomic_meta()` 内抛异常必须整体回滚，不得留下半写状态。"""

    async def scenario() -> str | None:
        with pytest.raises(RuntimeError):
            async with repo.atomic_meta() as tx:
                await tx.set("runtime:owner", "999999:deadbeef")
                raise RuntimeError("模拟判定中途失败")
        return await repo.meta_get("runtime:owner")

    assert run(scenario()) is None


def test_atomic_meta_reads_see_writes_of_same_transaction(repo: Repository) -> None:
    """P2-11：同一事务内的读必须看见本事务已写的值（读-判定-写在同一条链上）。"""

    async def scenario() -> tuple[str | None, bool]:
        async with repo.atomic_meta() as tx:
            await tx.set("runtime:owner", "111:aaaa")
            seen = await tx.get("runtime:owner")
            deleted = await tx.delete("runtime:owner")
            gone = await tx.get("runtime:owner")
        return seen, deleted and gone is None

    seen, checked = run(scenario())
    assert seen == "111:aaaa"
    assert checked is True
