"""SQLite 持久层（aiosqlite）：六表 + meta 别名 + 投递队列 + 审计 JSONL。

并发模型（架构 §D）：
- 所有写操作都在 `self._write_lock` 保护的 `BEGIN IMMEDIATE … COMMIT` 事务内；
- `PRAGMA journal_mode=WAL` 让读不加锁；
- 时间列一律存 ISO8601 UTC 字符串，字典序即时间序，可直接 `WHERE next_attempt_at <= ?` 比较。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .clock import Clock, ensure_utc
from .config import override_to_json, parse_override_json
from .errors import ErrorCode, PluginError, SchemaTooNewError
from .models import (
    Baseline,
    DailyReportRow,
    DeliveryTask,
    FetchResult,
    ForecastState,
    GroupOverride,
    LogicalEvent,
    SnapshotRow,
    SubscriptionRow,
)
from .notifications import event_priority_for

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS subscriptions (
  subscription_id TEXT PRIMARY KEY, umo TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, settings_json TEXT NOT NULL DEFAULT '{}',
  subscribed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_success_at TEXT, last_failure_at TEXT,
  uncertain_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_subscriptions_group ON subscriptions(platform, group_id);

CREATE TABLE IF NOT EXISTS source_snapshots (
  endpoint TEXT PRIMARY KEY,
  ok INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT 'ok',
  fetched_at TEXT, updated_at TEXT, checked_at TEXT,
  published_batch TEXT, cache_status TEXT, payload_hash TEXT, raw_json TEXT,
  etag TEXT,
  last_seen_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS logical_events (
  logical_id TEXT NOT NULL, state_version TEXT NOT NULL,
  kind TEXT NOT NULL, group_kind TEXT NOT NULL,
  occurred_at TEXT NOT NULL, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  baseline INTEGER NOT NULL DEFAULT 0, source_refs_json TEXT NOT NULL DEFAULT '[]',
  payload_json TEXT NOT NULL, PRIMARY KEY (logical_id, state_version));
CREATE INDEX IF NOT EXISTS idx_logical_id ON logical_events(logical_id);
CREATE INDEX IF NOT EXISTS idx_logical_group ON logical_events(group_kind, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_logical_kind_time ON logical_events(kind, occurred_at);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id) ON DELETE CASCADE,
  umo TEXT NOT NULL, logical_id TEXT NOT NULL, state_version TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  sent_at TEXT, error_code TEXT, message_hash TEXT,
  priority INTEGER NOT NULL DEFAULT 5,
  UNIQUE (subscription_id, logical_id, state_version));
CREATE INDEX IF NOT EXISTS idx_deliveries_queue ON deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_sub ON deliveries(subscription_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_logical ON deliveries(logical_id);

CREATE TABLE IF NOT EXISTS forecast_state (
  subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(subscription_id) ON DELETE CASCADE,
  hits_24h INTEGER NOT NULL DEFAULT 0, hits_48h INTEGER NOT NULL DEFAULT 0,
  armed_24h INTEGER NOT NULL DEFAULT 1, armed_48h INTEGER NOT NULL DEFAULT 1,
  cooldown_until TEXT, day_key TEXT NOT NULL DEFAULT '', day_count INTEGER NOT NULL DEFAULT 0,
  epoch_seq INTEGER NOT NULL DEFAULT 0, alert_seq INTEGER NOT NULL DEFAULT 0,
  last_prob_24h INTEGER, last_prob_48h INTEGER,
  last_sample_at TEXT, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS daily_reports (
  subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id) ON DELETE CASCADE,
  business_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt INTEGER NOT NULL DEFAULT 0,
  theme TEXT, image_sha1 TEXT, error_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  sent_at TEXT, next_attempt_at TEXT,
  PRIMARY KEY (subscription_id, business_date));
CREATE INDEX IF NOT EXISTS idx_daily_reports_date ON daily_reports(business_date, status);
"""


def _to_iso(value: datetime | None) -> str | None:
    """datetime → ISO8601 UTC 字符串（D-07：存储层只存 UTC）。"""
    if value is None:
        return None
    return ensure_utc(value).isoformat()


def _parse_db_dt(text: str | None) -> datetime | None:
    """解析数据库中的 ISO8601 时间；损坏时返回 None（不抛异常）。"""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("[codex-reset][repo] 时间字段无法解析：%r", text)
        return None
    return ensure_utc(parsed)


def _new_id(prefix: str, seed: str) -> str:
    return f"{prefix}:{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _hash_umo(umo: str) -> str:
    """日志/审计用的 umo 指纹（不打完整 umo）。"""
    return hashlib.sha1(umo.encode("utf-8")).hexdigest()[:8]


class MetaView:
    """`atomic_meta()` 产出的事务内 meta 视图（只暴露 `get/set/delete`）。

    所有操作复用同一个 `aiosqlite.Connection`，因此与调用方共享同一个
    `BEGIN IMMEDIATE` 事务；`MetaView` 自身不开事务，避免二次获取写锁。
    """

    __slots__ = ("_clock", "_conn")

    def __init__(self, conn: aiosqlite.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    async def get(self, key: str) -> str | None:
        """读取单键（事务内可见的已提交值）。"""
        cursor = await self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["value"])

    async def set(self, key: str, value: str) -> None:
        """写入单键（UPSERT）。"""
        await self._conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _to_iso(self._clock.utcnow())),
        )

    async def delete(self, key: str) -> bool:
        """删除单键，返回是否命中。"""
        cursor = await self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        return cursor.rowcount > 0


class Repository:
    """六表持久层。生命周期：`open()` → 业务调用 → `close()`。"""

    def __init__(self, db_path: Path | str, *, clock: Clock, audit_dir: Path | str | None = None) -> None:
        self._db_path = Path(db_path)
        self._clock = clock
        self._audit_dir = Path(audit_dir) if audit_dir is not None else self._db_path.parent / "audit"
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._tx_owner: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        """数据库文件路径（测试与诊断用）。"""
        return self._db_path

    @property
    def audit_dir(self) -> Path:
        """审计 JSONL 目录。"""
        return self._audit_dir

    async def open(self) -> None:
        """连接、设置 PRAGMA、校验 schema 版本、建表。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        self._conn = conn
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")

            found = await self._read_schema_version()
            if found is not None and found > SCHEMA_VERSION:
                raise SchemaTooNewError(
                    f"数据库 schema_version={found} 高于插件支持的 {SCHEMA_VERSION}，拒绝写入",
                    found_version=found,
                )
            await conn.executescript(_DDL)
            await self._ensure_columns()
            if found is None or found < SCHEMA_VERSION:
                await self.meta_set("schema_version", str(SCHEMA_VERSION))
                logger.info(
                    "[codex-reset][repo] schema 初始化/升级完成 old=%s new=%s",
                    found,
                    SCHEMA_VERSION,
                )
        except BaseException:
            await conn.close()
            self._conn = None
            raise
        logger.info("[codex-reset][repo] opened db=%s", self._db_path.name)

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("[codex-reset][repo] closed db=%s", self._db_path.name)

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise PluginError("Repository 尚未 open()", error_code=ErrorCode.SCHEMA_ERROR)
        return self._conn

    async def _ensure_columns(self) -> None:
        """旧库补列：SQLite 不支持 `ADD COLUMN IF NOT EXISTS`，只能先查 `table_info`。

        - `forecast_state.alert_seq`（v1.2 批次 A/B 之前的库）；
        - `deliveries.priority`（v1.3.0 G03 消息优先级，兼容迁移默认 5）。

        注意：索引必须**在补列之后**创建 —— 旧库的 `deliveries` 表没有 `priority` 列，
        若把 `idx_deliveries_priority` 放进 `_DDL`，`executescript` 会先因「no such column」失败。
        """
        conn = self._connection()
        cursor = await conn.execute("PRAGMA table_info(forecast_state)")
        rows = await cursor.fetchall()
        await cursor.close()
        columns = {str(row["name"]) for row in rows}
        if columns and "alert_seq" not in columns:
            await conn.execute(
                "ALTER TABLE forecast_state ADD COLUMN alert_seq INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("[codex-reset][repo] migrate forecast_state + alert_seq")

        cursor = await conn.execute("PRAGMA table_info(deliveries)")
        rows = await cursor.fetchall()
        await cursor.close()
        columns = {str(row["name"]) for row in rows}
        if columns and "priority" not in columns:
            await conn.execute(
                "ALTER TABLE deliveries ADD COLUMN priority INTEGER NOT NULL DEFAULT 5"
            )
            logger.info("[codex-reset][repo] migrate deliveries + priority")
        if columns:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deliveries_priority"
                " ON deliveries(status, priority, id)"
            )

    async def _read_schema_version(self) -> int | None:
        conn = self._connection()
        try:
            cursor = await conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = await cursor.fetchone()
        except aiosqlite.Error:
            return None
        await cursor.close()
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """单写锁 + `BEGIN IMMEDIATE` 事务上下文。

        事务内**禁止**再开事务：`asyncio.Lock` 不可重入，嵌套会永久自锁且没有任何报错。
        这里用 task 身份做快速失败——只拦「同一 task 重入」，其它 task 仍正常排队等锁。
        """
        current = asyncio.current_task()
        if current is not None and self._tx_owner is current:
            raise RuntimeError(
                "禁止在 atomic_meta() 事务内嵌套 meta 写（asyncio.Lock 不可重入，会永久自锁）"
            )
        conn = self._connection()
        async with self._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            self._tx_owner = current
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            finally:
                self._tx_owner = None
            await conn.commit()

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    async def meta_get(self, key: str) -> str | None:
        """读取 meta 单键。"""
        conn = self._connection()
        cursor = await conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["value"])

    async def meta_set(self, key: str, value: str) -> None:
        """写入 meta 单键（UPSERT）。"""
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _to_iso(self._clock.utcnow())),
            )

    async def meta_get_all(self, prefix: str = "") -> dict[str, str]:
        """按前缀读取 meta（`prefix=""` 即全部）。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{prefix}%",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def meta_delete(self, key: str) -> bool:
        """删除 meta 单键，返回是否命中。"""
        async with self._tx() as conn:
            cursor = await conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            return cursor.rowcount > 0

    @asynccontextmanager
    async def atomic_meta(self) -> AsyncIterator[MetaView]:
        """把「读 → 判定 → 写」收进**一个** `BEGIN IMMEDIATE` 事务。

        软单实例的抢占必须原子：先 `meta_get` 再 `meta_set` 的话，两个实例会
        各自读到「无主」快照、双双认定自己持锁（P2-11）。本上下文在退出时统一
        commit，异常则 rollback。

        注意：事务内**禁止**再调用 `meta_set`/`meta_delete`（会二次获取写锁而自锁），
        一律走 `MetaView.set/delete`。
        """
        async with self._tx() as conn:
            yield MetaView(conn, self._clock)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    async def upsert_snapshot(
        self,
        endpoint: str,
        fetch: FetchResult,
        *,
        snapshot_payload: Mapping[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> SnapshotRow:
        """写入采集结果。

        D-12：失败轮次**只**更新 `ok=0 / error_code / last_seen_at`，
        绝不覆盖 `raw_json / fetched_at / updated_at`（避免"漏发即永久漏发"）。
        """
        now = self._clock.utcnow()
        raw_json = (
            json.dumps(dict(snapshot_payload), ensure_ascii=False, sort_keys=True)
            if snapshot_payload is not None
            else None
        )
        payload_hash = hashlib.sha1(raw_json.encode("utf-8")).hexdigest()[:16] if raw_json else None
        now_iso = _to_iso(now)

        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO source_snapshots (endpoint, ok, error_code, last_seen_at)"
                " VALUES (?, 0, ?, ?) ON CONFLICT(endpoint) DO NOTHING",
                (endpoint, fetch.error_code, now_iso),
            )
            if fetch.error_code == ErrorCode.NOT_MODIFIED:
                await conn.execute(
                    "UPDATE source_snapshots SET ok=1, error_code=?,"
                    " checked_at=COALESCE(?, checked_at),"
                    " cache_status=COALESCE(?, cache_status),"
                    " published_batch=COALESCE(?, published_batch),"
                    " etag=COALESCE(?, etag), last_seen_at=? WHERE endpoint=?",
                    (
                        ErrorCode.NOT_MODIFIED,
                        _to_iso(fetch.checked_at),
                        fetch.cache_status,
                        fetch.batch,
                        fetch.etag,
                        now_iso,
                        endpoint,
                    ),
                )
            elif fetch.ok:
                await conn.execute(
                    "UPDATE source_snapshots SET ok=1, error_code=?, fetched_at=?, updated_at=?,"
                    " checked_at=?, published_batch=?, cache_status=?, etag=?,"
                    " payload_hash=COALESCE(?, payload_hash), raw_json=COALESCE(?, raw_json),"
                    " last_seen_at=? WHERE endpoint=?",
                    (
                        ErrorCode.OK,
                        _to_iso(fetch.fetched_at or now),
                        _to_iso(updated_at or now),
                        _to_iso(fetch.checked_at),
                        fetch.batch,
                        fetch.cache_status,
                        fetch.etag,
                        payload_hash,
                        raw_json,
                        now_iso,
                        endpoint,
                    ),
                )
            else:
                await conn.execute(
                    "UPDATE source_snapshots SET ok=0, error_code=?, last_seen_at=? WHERE endpoint=?",
                    (fetch.error_code, now_iso, endpoint),
                )

        row = await self.get_snapshot(endpoint)
        if row is None:  # pragma: no cover - INSERT 保证必然存在
            raise PluginError(f"source_snapshots 写入失败：{endpoint}")
        logger.info(
            "[codex-reset][repo] snapshot endpoint=%s ok=%s error=%s batch=%s",
            endpoint,
            int(row.ok),
            row.error_code,
            row.batch,
        )
        return row

    async def get_snapshot(self, endpoint: str) -> SnapshotRow | None:
        """读取某端点的快照状态行。"""
        conn = self._connection()
        cursor = await conn.execute("SELECT * FROM source_snapshots WHERE endpoint = ?", (endpoint,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return SnapshotRow(
            endpoint=str(row["endpoint"]),
            ok=bool(row["ok"]),
            error_code=str(row["error_code"]),
            fetched_at=_parse_db_dt(row["fetched_at"]),
            updated_at=_parse_db_dt(row["updated_at"]),
            checked_at=_parse_db_dt(row["checked_at"]),
            batch=row["published_batch"],
            cache_status=row["cache_status"],
            last_seen_at=_parse_db_dt(row["last_seen_at"]),
        )

    async def snapshot_etag(self, endpoint: str) -> str | None:
        """读取某端点最近一次的 ETag（用于条件请求）。"""
        conn = self._connection()
        cursor = await conn.execute("SELECT etag FROM source_snapshots WHERE endpoint = ?", (endpoint,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        value = row["etag"]
        return None if value is None else str(value)

    async def get_snapshot_raw(self, endpoint: str) -> dict[str, Any] | None:
        """读取某端点最近一次成功采集的原始 JSON（`raw_json` 反序列化）。

        重启恢复（`Pipeline.bootstrap`）用它重建内存缓存。`raw_json` 为空或损坏时
        返回 `None` 并记 WARNING（绝不抛异常，避免历史脏数据阻断启动）。
        """
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT raw_json FROM source_snapshots WHERE endpoint = ?", (endpoint,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        raw = row["raw_json"]
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[codex-reset][repo] snapshot raw_json 无法解析 endpoint=%s", endpoint)
            return None
        if not isinstance(parsed, dict):
            logger.warning(
                "[codex-reset][repo] snapshot raw_json 顶层不是对象 endpoint=%s type=%s",
                endpoint,
                type(parsed).__name__,
            )
            return None
        return parsed

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    async def list_subscriptions(self) -> list[SubscriptionRow]:
        """列出全部订阅（按订阅时间升序）。"""
        conn = self._connection()
        cursor = await conn.execute("SELECT * FROM subscriptions ORDER BY subscribed_at, subscription_id")
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._subscription_from_row(row) for row in rows]

    async def get_subscription(self, umo: str) -> SubscriptionRow | None:
        """按 umo 读取订阅。"""
        conn = self._connection()
        cursor = await conn.execute("SELECT * FROM subscriptions WHERE umo = ?", (umo,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return self._subscription_from_row(row)

    async def get_subscription_by_id(self, subscription_id: str) -> SubscriptionRow | None:
        """按 subscription_id 读取订阅。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return self._subscription_from_row(row)

    async def upsert_subscription(
        self,
        *,
        umo: str,
        platform: str,
        group_id: str,
        group_name: str,
        override: GroupOverride | None = None,
    ) -> SubscriptionRow:
        """新增或更新订阅（`umo` 唯一；`subscription_id` 由 umo 派生，稳定不变）。

        `settings_json` **只**保存稀疏覆盖（`override.as_sparse_dict()`），
        未覆盖字段继续继承全局默认值（改动 1）。
        """
        now = self._clock.utcnow()
        now_iso = _to_iso(now)
        subscription_id = _new_id("sub", umo)
        settings_json = override_to_json(override)
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO subscriptions (subscription_id, umo, platform, group_id, group_name,"
                " enabled, settings_json, subscribed_at, updated_at, version)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1)"
                " ON CONFLICT(umo) DO UPDATE SET platform=excluded.platform,"
                " group_id=excluded.group_id, group_name=excluded.group_name,"
                " settings_json=excluded.settings_json, updated_at=excluded.updated_at,"
                " version=subscriptions.version + 1",
                (
                    subscription_id,
                    umo,
                    platform,
                    group_id,
                    group_name,
                    settings_json,
                    now_iso,
                    now_iso,
                ),
            )
        row = await self.get_subscription(umo)
        if row is None:  # pragma: no cover
            raise PluginError(f"订阅写入失败：{umo}")
        logger.info(
            "[codex-reset][repo] subscription upsert sub_id=%s umo_hash=%s group=%s",
            row.subscription_id,
            _hash_umo(umo),
            row.group_id,
        )
        return row

    async def delete_subscription(self, umo: str) -> bool:
        """删除订阅（级联删除 deliveries 与 forecast_state）。"""
        async with self._tx() as conn:
            cursor = await conn.execute("DELETE FROM subscriptions WHERE umo = ?", (umo,))
            deleted = cursor.rowcount > 0
        if deleted:
            logger.info("[codex-reset][repo] subscription deleted umo_hash=%s", _hash_umo(umo))
        return deleted

    async def update_subscription_override(self, umo: str, override: GroupOverride) -> bool:
        """更新某群的**稀疏**覆盖（`/cr set` 与 WebUI 卡片的唯一写入口）。"""
        async with self._tx() as conn:
            cursor = await conn.execute(
                "UPDATE subscriptions SET settings_json=?, updated_at=?, version=version + 1"
                " WHERE umo = ?",
                (override_to_json(override), _to_iso(self._clock.utcnow()), umo),
            )
            return cursor.rowcount > 0

    async def update_subscription_settings(self, umo: str, override: GroupOverride) -> bool:
        """兼容别名（语义已改为稀疏覆盖，等价于 `update_subscription_override`）。"""
        return await self.update_subscription_override(umo, override)

    async def touch_subscription_health(
        self,
        subscription_id: str,
        *,
        ok: bool,
        uncertain_delta: int = 0,
    ) -> None:
        """更新推送健康度（最近成功/失败时间、uncertain 计数）。"""
        now_iso = _to_iso(self._clock.utcnow())
        column = "last_success_at" if ok else "last_failure_at"
        async with self._tx() as conn:
            await conn.execute(
                f"UPDATE subscriptions SET {column}=?, uncertain_count=MAX(0, uncertain_count + ?),"
                " updated_at=? WHERE subscription_id=?",
                (now_iso, int(uncertain_delta), now_iso, subscription_id),
            )

    async def subscription_health(self) -> list[dict[str, Any]]:
        """`/cr groups` 使用的订阅 + 队列健康汇总。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT s.subscription_id, s.umo, s.platform, s.group_id, s.group_name, s.enabled,"
            " s.last_success_at, s.last_failure_at, s.uncertain_count,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.subscription_id = s.subscription_id"
            "   AND d.status = 'pending') AS pending_count,"
            " (SELECT COUNT(*) FROM deliveries d WHERE d.subscription_id = s.subscription_id"
            "   AND d.status = 'retry') AS retry_count"
            " FROM subscriptions s ORDER BY s.subscribed_at, s.subscription_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            umo = str(row["umo"])
            result.append(
                {
                    "subscription_id": str(row["subscription_id"]),
                    "umo_hash": _hash_umo(umo),
                    "platform": str(row["platform"]),
                    "group_id": str(row["group_id"]),
                    "group_name": str(row["group_name"]),
                    "enabled": bool(row["enabled"]),
                    "last_success_at": _parse_db_dt(row["last_success_at"]),
                    "last_failure_at": _parse_db_dt(row["last_failure_at"]),
                    "uncertain_count": int(row["uncertain_count"]),
                    "pending_count": int(row["pending_count"]),
                    "retry_count": int(row["retry_count"]),
                }
            )
        return result

    def _subscription_from_row(self, row: aiosqlite.Row) -> SubscriptionRow:
        return SubscriptionRow(
            subscription_id=str(row["subscription_id"]),
            umo=str(row["umo"]),
            platform=str(row["platform"]),
            group_id=str(row["group_id"]),
            group_name=str(row["group_name"]),
            enabled=bool(row["enabled"]),
            override=parse_override_json(row["settings_json"]),
            subscribed_at=_parse_db_dt(row["subscribed_at"]),
            updated_at=_parse_db_dt(row["updated_at"]),
            last_success_at=_parse_db_dt(row["last_success_at"]),
            last_failure_at=_parse_db_dt(row["last_failure_at"]),
            uncertain_count=int(row["uncertain_count"]),
            version=int(row["version"]),
        )

    # ------------------------------------------------------------------
    # 逻辑事件 + 别名
    # ------------------------------------------------------------------
    async def record_logical_event(self, ev: LogicalEvent, *, baseline: bool) -> bool:
        """记录逻辑事件；返回 True 表示 `(logical_id, state_version)` 首次出现。"""
        now_iso = _to_iso(self._clock.utcnow())
        async with self._tx() as conn:
            cursor = await conn.execute(
                "INSERT INTO logical_events (logical_id, state_version, kind, group_kind,"
                " occurred_at, first_seen_at, updated_at, baseline, source_refs_json, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(logical_id, state_version) DO NOTHING",
                (
                    ev.logical_id,
                    ev.state_version,
                    ev.kind,
                    ev.group_kind,
                    _to_iso(ev.occurred_at),
                    now_iso,
                    now_iso,
                    int(bool(baseline)),
                    json.dumps(list(ev.source_refs), ensure_ascii=False),
                    json.dumps(dict(ev.payload), ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            first_time = cursor.rowcount > 0
        if first_time:
            logger.info(
                "[codex-reset][repo] logical_event new id=%s ver=%s kind=%s baseline=%s",
                ev.logical_id,
                ev.state_version,
                ev.kind,
                int(bool(baseline)),
            )
        return first_time

    async def find_logical_by_alias(self, alias: str) -> str | None:
        """按别名查询 canonical `logical_id`（承载 D-08）。"""
        return await self.meta_get(f"alias:{alias}")

    async def find_logical_by_time(
        self,
        *,
        kind: str,
        occurred_at: datetime,
        tolerance_seconds: int = 2,
    ) -> str | None:
        """按 `(kind, occurred_at ± tolerance)` 查最近的 canonical `logical_id`。

        跨轮采集的时间戳存在秒级抖动时，`reset:ts:{ISO}` 这类精确别名会落空，
        同一个事实会被当成第二个事件再推一次。这里用时间容差把它重新收敛到
        已登记的 canonical。容差**固定 2s**：真实两次重置的间隔以天计，
        因此不会把不同事件误并；`ORDER BY occurred_at ASC` 保证多候选时结果稳定。
        """
        moment = ensure_utc(occurred_at)
        window = timedelta(seconds=max(0, int(tolerance_seconds)))
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT logical_id FROM logical_events WHERE kind = ?"
            " AND occurred_at BETWEEN ? AND ?"
            " ORDER BY occurred_at ASC, logical_id ASC LIMIT 1",
            (kind, _to_iso(moment - window), _to_iso(moment + window)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["logical_id"])

    async def add_alias(self, alias: str, logical_id: str) -> None:
        """登记别名；已存在则保留原 canonical（首次登记优先）。"""
        now_iso = _to_iso(self._clock.utcnow())
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO NOTHING",
                (f"alias:{alias}", logical_id, now_iso),
            )

    async def latest_state_version(self, logical_id: str) -> str | None:
        """某 logical_id 最新写入的 state_version。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT state_version FROM logical_events WHERE logical_id = ?"
            " ORDER BY updated_at DESC, state_version DESC LIMIT 1",
            (logical_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["state_version"])

    async def get_logical_event(self, logical_id: str, state_version: str) -> LogicalEvent | None:
        """按主键读取逻辑事件。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT * FROM logical_events WHERE logical_id = ? AND state_version = ?",
            (logical_id, state_version),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._logical_from_row(row)

    async def list_events(
        self,
        *,
        group_kind: str | None = None,
        limit: int = 10,
        exclude_corrections: bool = False,
    ) -> list[LogicalEvent]:
        """按发生时间倒序列出逻辑事件（`/cr history` 等只读命令使用）。

        `exclude_corrections=True` 时排除「更正」行（`state_version` 以
        `downgraded` 开头）：站点把某条 reset 降级为未确认时，更正事件同样落在
        `group_kind="reset"`，若不过滤会被 `/cr history` 当成「已确认」列出（P2-09）。
        """
        conn = self._connection()
        clauses: list[str] = []
        params: list[Any] = []
        if group_kind is not None:
            clauses.append("group_kind = ?")
            params.append(group_kind)
        if exclude_corrections:
            clauses.append("state_version NOT LIKE 'downgraded%'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # SQLite 的 `LIMIT -1` 等于「无上限」，负数必须收敛为 0（否则 history 会绕过上限）
        params.append(max(0, int(limit)))
        cursor = await conn.execute(
            f"SELECT * FROM logical_events{where} ORDER BY occurred_at DESC, logical_id LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._logical_from_row(row) for row in rows]

    async def list_events_since(
        self,
        *,
        kinds: Sequence[str],
        since: datetime,
        limit: int = 100,
    ) -> list[LogicalEvent]:
        """按 `occurred_at >= since` 且 `group_kind IN kinds` 升序列出事件（补发用）。

        `kinds` 为空时直接返回空列表（绝不退化成「全部类别」，防止把 watch/forecast
        误纳入补发摘要）。
        """
        kind_list = tuple(dict.fromkeys(str(kind) for kind in kinds if str(kind).strip()))
        if not kind_list:
            return []
        conn = self._connection()
        placeholders = ", ".join("?" for _ in kind_list)
        params: list[Any] = [_to_iso(since), *kind_list, int(limit)]
        cursor = await conn.execute(
            "SELECT * FROM logical_events WHERE occurred_at >= ?"
            f" AND group_kind IN ({placeholders})"
            " ORDER BY occurred_at ASC, logical_id ASC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._logical_from_row(row) for row in rows]

    def _logical_from_row(self, row: aiosqlite.Row) -> LogicalEvent:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        try:
            refs = json.loads(row["source_refs_json"])
        except (TypeError, ValueError):
            refs = []
        return LogicalEvent(
            logical_id=str(row["logical_id"]),
            kind=str(row["kind"]),
            group_kind=str(row["group_kind"]),
            state_version=str(row["state_version"]),
            occurred_at=_parse_db_dt(row["occurred_at"]) or datetime(1970, 1, 1, tzinfo=UTC),
            payload=payload if isinstance(payload, dict) else {},
            source_refs=tuple(refs) if isinstance(refs, list) else (),
        )

    # ------------------------------------------------------------------
    # 投递队列
    # ------------------------------------------------------------------
    async def enqueue_delivery(
        self,
        *,
        sub: SubscriptionRow | str,
        ev: LogicalEvent,
        status: str = "pending",
        next_attempt_at: datetime | None = None,
        priority: int | None = None,
    ) -> bool:
        """入队；`UNIQUE(subscription_id, logical_id, state_version)` 保证同群同版本只入队一次。

        `priority` 缺省时按事件 kind 取 `EVENT_PRIORITY`（G03：确认重置 > 预告/更正 > 权益
        > 概率提醒/补发）。
        """
        if isinstance(sub, SubscriptionRow):
            subscription_id = sub.subscription_id
            umo = sub.umo
        else:
            subscription_id = sub
            row = await self.get_subscription_by_id(subscription_id)
            if row is None:
                raise PluginError(f"订阅不存在：{subscription_id}")
            umo = row.umo
        resolved_priority = (
            int(priority) if priority is not None else event_priority_for(ev.kind)
        )
        now_iso = _to_iso(self._clock.utcnow())
        async with self._tx() as conn:
            cursor = await conn.execute(
                "INSERT INTO deliveries (subscription_id, umo, logical_id, state_version, kind,"
                " payload_json, status, attempt, next_attempt_at, created_at, updated_at, priority)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)"
                " ON CONFLICT(subscription_id, logical_id, state_version) DO NOTHING",
                (
                    subscription_id,
                    umo,
                    ev.logical_id,
                    ev.state_version,
                    ev.kind,
                    json.dumps(dict(ev.payload), ensure_ascii=False, sort_keys=True, default=str),
                    status,
                    _to_iso(next_attempt_at),
                    now_iso,
                    now_iso,
                    resolved_priority,
                ),
            )
            created = cursor.rowcount > 0
        if created:
            logger.info(
                "[codex-reset][repo] delivery enqueued id=%s ver=%s kind=%s sub_id=%s priority=%s",
                ev.logical_id,
                ev.state_version,
                ev.kind,
                subscription_id,
                resolved_priority,
            )
        return created

    async def claim_pending_deliveries(self, now: datetime, limit: int = 10) -> list[DeliveryTask]:
        """单事务认领：`pending/retry` 且到期 → `inflight` + `attempt+1`（并发安全）。

        排序按 `priority ASC, id ASC`（G03）：同一批里确认重置永远排在概率提醒/补发之前，
        因此高优先级消息不会因为先积压了几条低优先级消息而延后。
        """
        now_iso = _to_iso(now)
        tasks: list[DeliveryTask] = []
        async with self._tx() as conn:
            cursor = await conn.execute(
                "SELECT * FROM deliveries WHERE status IN ('pending', 'retry')"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                " ORDER BY priority ASC, id ASC LIMIT ?",
                (now_iso, int(limit)),
            )
            rows = await cursor.fetchall()
            for row in rows:
                attempt = int(row["attempt"]) + 1
                await conn.execute(
                    "UPDATE deliveries SET status='inflight', attempt=?, updated_at=? WHERE id=?",
                    (attempt, now_iso, int(row["id"])),
                )
                task = self._task_from_row(row)
                task.status = "inflight"
                task.attempt = attempt
                task.updated_at = ensure_utc(now)
                tasks.append(task)
        if tasks:
            logger.info("[codex-reset][repo] claimed=%s", len(tasks))
        return tasks

    async def mark_delivered(
        self,
        task: DeliveryTask,
        *,
        now: datetime,
        message_hash: str,
    ) -> None:
        """标记送达（终态），并刷新订阅健康度。"""
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE deliveries SET status='delivered', updated_at=?, sent_at=?,"
                " error_code=?, message_hash=?, next_attempt_at=NULL WHERE id=?",
                (now_iso, now_iso, ErrorCode.OK, message_hash, int(task.id)),
            )
            await conn.execute(
                "UPDATE subscriptions SET last_success_at=?, updated_at=? WHERE subscription_id=?",
                (now_iso, now_iso, task.subscription_id),
            )
        task.status = "delivered"
        task.updated_at = ensure_utc(now)
        logger.info(
            "[codex-reset][repo] delivered id=%s ver=%s attempt=%s",
            task.logical_id,
            task.state_version,
            task.attempt,
        )

    async def mark_failed(
        self,
        task: DeliveryTask,
        *,
        now: datetime,
        error_code: str,
        next_attempt_at: datetime | None,
        terminal: bool = False,
    ) -> None:
        """标记失败：`attempt>=3`（或 `terminal=True`）转终态 `failed`，否则 `retry`。

        `terminal=True` 用于「重试也不会变好」的确定性错误（例如未知模板 kind）。
        """
        now_iso = _to_iso(now)
        final = terminal or task.attempt >= 3
        status = "failed" if final else "retry"
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE deliveries SET status=?, attempt=?, updated_at=?, next_attempt_at=?,"
                " error_code=? WHERE id=?",
                (
                    status,
                    int(task.attempt),
                    now_iso,
                    None if final else _to_iso(next_attempt_at),
                    error_code,
                    int(task.id),
                ),
            )
            if final:
                await conn.execute(
                    "UPDATE subscriptions SET last_failure_at=?, uncertain_count=uncertain_count+1,"
                    " updated_at=? WHERE subscription_id=?",
                    (now_iso, now_iso, task.subscription_id),
                )
        task.status = status
        task.error_code = error_code
        task.next_attempt_at = None if final else next_attempt_at
        task.updated_at = ensure_utc(now)
        log = logger.error if final else logger.warning
        log(
            "[codex-reset][repo] delivery %s id=%s ver=%s attempt=%s error=%s",
            status,
            task.logical_id,
            task.state_version,
            task.attempt,
            error_code,
        )

    async def mark_skipped(self, task: DeliveryTask, *, now: datetime, reason: str) -> None:
        """标记跳过（终态）：订阅已删除/停用或类别开关关闭。

        `skipped` 不重试、不累加 `uncertain_count`、也不刷新 `last_failure_at`，
        避免「管理员关闭某类通知」被误算成推送健康度下跌。
        """
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE deliveries SET status='skipped', updated_at=?, error_code=?,"
                " next_attempt_at=NULL WHERE id=?",
                (now_iso, reason, int(task.id)),
            )
        task.status = "skipped"
        task.error_code = reason
        task.next_attempt_at = None
        task.updated_at = ensure_utc(now)
        logger.info(
            "[codex-reset][repo] delivery skipped id=%s ver=%s kind=%s reason=%s",
            task.logical_id,
            task.state_version,
            task.kind,
            reason,
        )

    async def mark_uncertain(self, task: DeliveryTask, *, now: datetime, error_code: str) -> None:
        """标记不确定（终态，不自动重发，F08），并累加 uncertain 计数。"""
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE deliveries SET status='uncertain', updated_at=?, error_code=?,"
                " next_attempt_at=NULL WHERE id=?",
                (now_iso, error_code, int(task.id)),
            )
            await conn.execute(
                "UPDATE subscriptions SET last_failure_at=?, uncertain_count=uncertain_count+1,"
                " updated_at=? WHERE subscription_id=?",
                (now_iso, now_iso, task.subscription_id),
            )
        task.status = "uncertain"
        task.error_code = error_code
        task.next_attempt_at = None
        task.updated_at = ensure_utc(now)
        logger.warning(
            "[codex-reset][repo] delivery uncertain id=%s ver=%s error=%s",
            task.logical_id,
            task.state_version,
            error_code,
        )

    async def requeue_inflight_as_uncertain(self, now: datetime) -> int:
        """重启恢复：`inflight` → `uncertain`（不自动重发），返回受影响行数。"""
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            cursor = await conn.execute(
                "UPDATE deliveries SET status='uncertain', error_code=?, updated_at=?,"
                " next_attempt_at=NULL WHERE status='inflight'",
                (ErrorCode.INTERRUPTED, now_iso),
            )
            count = cursor.rowcount
        if count:
            logger.warning("[codex-reset][repo] inflight -> uncertain count=%s", count)
        return count

    async def list_deliveries(
        self,
        *,
        subscription_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[DeliveryTask]:
        """按条件列出投递行（诊断与测试使用）。"""
        conn = self._connection()
        clauses: list[str] = []
        params: list[Any] = []
        if subscription_id is not None:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        cursor = await conn.execute(f"SELECT * FROM deliveries{where} ORDER BY id LIMIT ?", params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._task_from_row(row) for row in rows]

    async def has_delivery(self, subscription_id: str, logical_id: str) -> bool:
        """某群是否已经为某 `logical_id` 建过投递行（补发去重依据）。

        任何状态（含 `skipped` / `failed` / `uncertain`）都算「已处理过」，
        避免补发把管理员主动关闭的类别或已失败的旧事件重新推一遍。
        """
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT 1 FROM deliveries WHERE subscription_id = ? AND logical_id = ? LIMIT 1",
            (subscription_id, logical_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def has_delivered(self, subscription_id: str, logical_id: str) -> bool:
        """某群是否**已送达**某 `logical_id`（F07 更正过滤依据）。

        与 `has_delivery` 的区别：只认 `status='delivered'`。「原消息已送达」是更正
        的前置条件——从未收到预告的群不该收到「此前的预告未获确认」。
        """
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT 1 FROM deliveries WHERE subscription_id = ? AND logical_id = ?"
            " AND status = 'delivered' LIMIT 1",
            (subscription_id, logical_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def count_deliveries(self, status: str | None = None) -> int:
        """统计投递行数（测试与 `/cr groups` 使用）。"""
        conn = self._connection()
        if status is None:
            cursor = await conn.execute("SELECT COUNT(*) AS c FROM deliveries")
        else:
            cursor = await conn.execute("SELECT COUNT(*) AS c FROM deliveries WHERE status = ?", (status,))
        row = await cursor.fetchone()
        await cursor.close()
        return 0 if row is None else int(row["c"])

    def _task_from_row(self, row: aiosqlite.Row) -> DeliveryTask:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        priority = row["priority"] if "priority" in row.keys() else 5
        return DeliveryTask(
            id=int(row["id"]),
            subscription_id=str(row["subscription_id"]),
            umo=str(row["umo"]),
            logical_id=str(row["logical_id"]),
            state_version=str(row["state_version"]),
            kind=str(row["kind"]),
            payload=payload if isinstance(payload, dict) else {},
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            next_attempt_at=_parse_db_dt(row["next_attempt_at"]),
            created_at=_parse_db_dt(row["created_at"]),
            updated_at=_parse_db_dt(row["updated_at"]),
            error_code=row["error_code"],
            priority=int(priority) if priority is not None else 5,
        )

    # ------------------------------------------------------------------
    # 预测状态
    # ------------------------------------------------------------------
    async def get_forecast_state(self, subscription_id: str) -> ForecastState:
        """读取预测状态；不存在时返回默认 `ForecastState`。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT * FROM forecast_state WHERE subscription_id = ?", (subscription_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return ForecastState(subscription_id=subscription_id)
        return ForecastState(
            subscription_id=str(row["subscription_id"]),
            hits_24h=int(row["hits_24h"]),
            hits_48h=int(row["hits_48h"]),
            armed_24h=bool(row["armed_24h"]),
            armed_48h=bool(row["armed_48h"]),
            cooldown_until=_parse_db_dt(row["cooldown_until"]),
            day_key=str(row["day_key"]),
            day_count=int(row["day_count"]),
            epoch_seq=int(row["epoch_seq"]),
            alert_seq=int(row["alert_seq"]),
            last_prob_24h=row["last_prob_24h"],
            last_prob_48h=row["last_prob_48h"],
            last_sample_at=_parse_db_dt(row["last_sample_at"]),
        )

    async def save_forecast_state(self, state: ForecastState, *, now: datetime) -> None:
        """UPSERT 预测状态。"""
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            await conn.execute(
                "INSERT INTO forecast_state (subscription_id, hits_24h, hits_48h, armed_24h,"
                " armed_48h, cooldown_until, day_key, day_count, epoch_seq, alert_seq, last_prob_24h,"
                " last_prob_48h, last_sample_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(subscription_id) DO UPDATE SET hits_24h=excluded.hits_24h,"
                " hits_48h=excluded.hits_48h, armed_24h=excluded.armed_24h,"
                " armed_48h=excluded.armed_48h, cooldown_until=excluded.cooldown_until,"
                " day_key=excluded.day_key, day_count=excluded.day_count,"
                " epoch_seq=excluded.epoch_seq, alert_seq=excluded.alert_seq,"
                " last_prob_24h=excluded.last_prob_24h,"
                " last_prob_48h=excluded.last_prob_48h, last_sample_at=excluded.last_sample_at,"
                " updated_at=excluded.updated_at",
                (
                    state.subscription_id,
                    int(state.hits_24h),
                    int(state.hits_48h),
                    int(bool(state.armed_24h)),
                    int(bool(state.armed_48h)),
                    _to_iso(state.cooldown_until),
                    state.day_key,
                    int(state.day_count),
                    int(state.epoch_seq),
                    int(state.alert_seq),
                    state.last_prob_24h,
                    state.last_prob_48h,
                    _to_iso(state.last_sample_at),
                    now_iso,
                ),
            )

    async def reset_all_forecast_epochs(self, now: datetime) -> int:
        """确认重置后：全部群 `epoch_seq+1`、清空命中计数并重新武装（D-05/U-1）。"""
        now_iso = _to_iso(now)
        async with self._tx() as conn:
            cursor = await conn.execute(
                "UPDATE forecast_state SET epoch_seq=epoch_seq+1, hits_24h=0, hits_48h=0,"
                " armed_24h=1, armed_48h=1, updated_at=?",
                (now_iso,),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # 每日报告（v1.3.0 P01 / G08 / S02 / S03）
    # ------------------------------------------------------------------
    async def claim_daily_report(
        self,
        subscription_id: str,
        business_date: str,
        *,
        theme: str | None = None,
    ) -> bool:
        """占位某群某业务日期的日报；返回 True 表示本次运行拥有该 (群, 日期) 的唯一发送权。

        主键 `PRIMARY KEY (subscription_id, business_date)` 是每日唯一性的**最终兜底**：
        即使调度器因为重启/竞态重复判定，同群同日期也只会成功占位一次（AC15）。
        """
        now_iso = _to_iso(self._clock.utcnow())
        async with self._tx() as conn:
            cursor = await conn.execute(
                "INSERT INTO daily_reports (subscription_id, business_date, status, attempt,"
                " theme, created_at, updated_at)"
                " VALUES (?, ?, 'pending', 0, ?, ?, ?)"
                " ON CONFLICT(subscription_id, business_date) DO NOTHING",
                (subscription_id, business_date, theme, now_iso, now_iso),
            )
            created = cursor.rowcount > 0
        if created:
            logger.info(
                "[codex-reset][repo] daily_report claimed sub_id=%s date=%s",
                subscription_id,
                business_date,
            )
        return created

    async def get_daily_report(self, subscription_id: str, business_date: str) -> DailyReportRow | None:
        """读取某群某业务日期的日报行。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT * FROM daily_reports WHERE subscription_id = ? AND business_date = ?",
            (subscription_id, business_date),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else self._daily_report_from_row(row)

    async def update_daily_report(
        self,
        subscription_id: str,
        business_date: str,
        *,
        status: str,
        error_code: str | None = None,
        image_sha1: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        """更新日报状态并 `attempt+1`（除 `sent` 外每次推进都算一次尝试）。"""
        now_iso = _to_iso(self._clock.utcnow())
        bump = ", attempt=attempt+1" if status != "sent" else ""
        set_next = _to_iso(next_attempt_at) if next_attempt_at is not None else None
        sent_clause = ", sent_at=?" if status == "sent" else ""
        params: list[Any] = [status, now_iso, error_code, image_sha1, set_next]
        if status == "sent":
            params.append(now_iso)
        params.extend([subscription_id, business_date])
        async with self._tx() as conn:
            cursor = await conn.execute(
                "UPDATE daily_reports SET status=?, updated_at=?, error_code=?, image_sha1=?,"
                f" next_attempt_at=?{bump}{sent_clause}"
                " WHERE subscription_id=? AND business_date=?",
                params,
            )
            updated = cursor.rowcount > 0
        if updated:
            logger.info(
                "[codex-reset][repo] daily_report %s sub_id=%s date=%s error=%s",
                status,
                subscription_id,
                business_date,
                error_code,
            )
        return updated

    async def list_daily_reports(self, *, limit: int = 50) -> list[DailyReportRow]:
        """按业务日期倒序列出日报行（诊断与测试使用）。"""
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT * FROM daily_reports ORDER BY business_date DESC, subscription_id LIMIT ?",
            (max(0, int(limit)),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._daily_report_from_row(row) for row in rows]

    async def latest_daily_report_error(self) -> tuple[str | None, str | None]:
        """返回最近一条 failed / uncertain 日报的 `(business_date, error_code)`。

        只读查询（命令层 `/cr status` 展示最近图片失败原因用）；无失败记录时返回
        `(None, None)`。`uncertain` 同样视为「值得展示的失败原因」（发送结果不确定）。
        """
        conn = self._connection()
        cursor = await conn.execute(
            "SELECT business_date, error_code FROM daily_reports"
            " WHERE status IN ('failed', 'uncertain')"
            " ORDER BY updated_at DESC, business_date DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None, None
        return str(row["business_date"]), row["error_code"]

    def _daily_report_from_row(self, row: aiosqlite.Row) -> DailyReportRow:
        return DailyReportRow(
            subscription_id=str(row["subscription_id"]),
            business_date=str(row["business_date"]),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            theme=row["theme"],
            image_sha1=row["image_sha1"],
            error_code=row["error_code"],
            created_at=_parse_db_dt(row["created_at"]),
            updated_at=_parse_db_dt(row["updated_at"]),
            sent_at=_parse_db_dt(row["sent_at"]),
            next_attempt_at=_parse_db_dt(row["next_attempt_at"]),
        )

    # ------------------------------------------------------------------
    # 基线
    # ------------------------------------------------------------------
    async def baseline(self) -> Baseline:
        """读取首轮基线。"""
        values = await self.meta_get_all("baseline:")
        built = str(values.get("baseline:built", "0")).strip() in {"1", "true", "True"}
        return Baseline(
            built=built,
            last_reset_at=_parse_db_dt(values.get("baseline:last_reset_at")),
            watch_source_at=_parse_db_dt(values.get("baseline:watch_source_at")),
            built_at=_parse_db_dt(values.get("baseline:built_at")),
        )

    async def save_baseline(self, b: Baseline) -> None:
        """写入首轮基线（4 个 meta 键，单事务）。"""
        now_iso = _to_iso(self._clock.utcnow())
        pairs: Sequence[tuple[str, str | None]] = (
            ("baseline:built", "1" if b.built else "0"),
            ("baseline:last_reset_at", _to_iso(b.last_reset_at)),
            ("baseline:watch_source_at", _to_iso(b.watch_source_at)),
            ("baseline:built_at", _to_iso(b.built_at)),
        )
        async with self._tx() as conn:
            for key, value in pairs:
                if value is None:
                    await conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                    continue
                await conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " updated_at=excluded.updated_at",
                    (key, value, now_iso),
                )

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    async def audit(self, record: Mapping[str, Any]) -> None:
        """追加审计记录到 `audit-YYYY-MM.jsonl` 并输出 INFO 日志（不含消息正文）。"""
        now = record.get("at")
        when = ensure_utc(now) if isinstance(now, datetime) else self._clock.utcnow()
        umo_value = record.get("umo")
        umo_hash = record.get("umo_hash")
        if not isinstance(umo_hash, str) or not umo_hash:
            umo_hash = _hash_umo(umo_value) if isinstance(umo_value, str) else ""
        entry = {
            "operator_id": str(record.get("operator_id", "")),
            "umo_hash": umo_hash,
            "action": str(record.get("action", "")),
            "result": str(record.get("result", "")),
            "at": when.isoformat(),
        }
        line = json.dumps(entry, ensure_ascii=False)
        path = self._audit_dir / f"audit-{when.strftime('%Y-%m')}.jsonl"
        try:
            await asyncio.to_thread(self._append_line, path, line)
        except OSError as exc:  # 审计失败不阻断业务
            logger.error("[codex-reset][audit] 写入失败 path=%s err=%s", path.name, exc)
        logger.info(
            "[codex-reset][audit] action=%s result=%s umo_hash=%s",
            entry["action"],
            entry["result"],
            entry["umo_hash"],
        )

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


__all__ = ["SCHEMA_VERSION", "Repository"]
