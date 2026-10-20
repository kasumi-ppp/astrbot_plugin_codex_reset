"""pytest 共享夹具。

环境约束：**没有** `pytest-asyncio`，也**没有** `astrbot`。
因此所有异步用例统一用 `run(coro)`（`asyncio.run` 包装）在同步测试函数内执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 保证 `import services.*` 可用
    sys.path.insert(0, str(REPO_ROOT))

# Windows 兼容：pytest 默认 basetemp 会创建 `pytest-current` 符号链接，未开开发者模式时会
# 抛 PermissionError。这里把临时根重定向到仓库内目录（沙箱/CI 均可写）。
_TEMP_ROOT = REPO_ROOT / ".pytest-tmp"
_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_TEMP_ROOT))
tempfile.tempdir = None  # 让 tempfile.gettempdir() 重新读取环境变量

from services.classifier import EventClassifier, ForecastEvaluator  # noqa: E402
from services.clock import FakeClock  # noqa: E402  (必须在 sys.path 注入之后导入)
from services.errors import ErrorCode  # noqa: E402
from services.models import (  # noqa: E402
    Baseline,
    FetchResult,
    GroupOverride,
    LogicalEvent,
    PluginSettings,
    SubscriptionRow,
)
from services.notifications import DeliveryWorker  # noqa: E402
from services.pipeline import Pipeline  # noqa: E402
from services.repository import Repository  # noqa: E402
from services.scheduler import DEFAULT_ROTATION, SourceScheduler  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def fixture_path(name: str) -> Path:
    """返回 fixture 的绝对路径（名字可带或不带 `.json`）。"""
    return FIXTURES_DIR / (name if name.endswith(".json") else f"{name}.json")


def load_fixture_text(name: str) -> str:
    """读取 fixture 原始文本（用于故意损坏的 JSON）。"""
    return fixture_path(name).read_text(encoding="utf-8")


def load_fixture(name: str) -> Any:
    """读取并解析 JSON fixture。"""
    return json.loads(load_fixture_text(name))


def run(coro: Any) -> Any:
    """在同步测试里执行协程。"""
    return asyncio.run(coro)


def utc(
    year: int = 2026,
    month: int = 10,
    day: int = 20,
    hour: int = 12,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """构造 UTC 时间。"""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def make_logical_event(
    logical_id: str = "reset:2112432076767289678",
    state_version: str = "confirmed",
    kind: str = "reset",
    group_kind: str = "reset",
    occurred_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> LogicalEvent:
    """构造一个逻辑事件，默认值与真实样本一致。"""
    return LogicalEvent(
        logical_id=logical_id,
        kind=kind,
        group_kind=group_kind,
        state_version=state_version,
        occurred_at=occurred_at or utc(),
        payload=payload if payload is not None else {"id": "2112432076767289678"},
    )


# ---------------------------------------------------------------------------
# 仓库夹具
# ---------------------------------------------------------------------------
async def open_repo(tmp_path: Path, *, clock: FakeClock | None = None) -> Repository:
    """创建并打开一个临时仓库。"""
    repo = Repository(tmp_path / "codex-reset.db", clock=clock or FakeClock())
    await repo.open()
    return repo


def make_repo(tmp_path: Path, *, clock: FakeClock | None = None) -> Repository:
    """同步版 `open_repo`（供 `run()` 场景之外使用）。"""
    return run(open_repo(tmp_path, clock=clock))


@pytest.fixture
def clock() -> FakeClock:
    """默认从 2026-10-20 12:00 UTC 起步的假时钟。"""
    return FakeClock(utc())


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Iterable[Repository]:
    """已打开的临时仓库，退出时自动关闭。"""
    repository = make_repo(tmp_path, clock=clock)
    yield repository
    run(repository.close())


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    """空目录（用于审计等文件落盘断言）。"""
    target = tmp_path / "empty"
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# HTTP / 发送 假实现（供 T4 及后续批次复用）
# ---------------------------------------------------------------------------
class FakeResponse:
    """最小 aiohttp ClientResponse 替身（支持 `async with`）。"""

    def __init__(self, status: int = 200, body: Any = b"", headers: dict[str, Any] | None = None) -> None:
        self.status = status
        self.headers = dict(headers or {})
        if isinstance(body, bytes | bytearray):
            self._body = bytes(body)
        elif isinstance(body, str):
            self._body = body.encode("utf-8")
        else:
            self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    async def read(self) -> bytes:
        return self._body

    async def text(self, encoding: str = "utf-8") -> str:
        return self._body.decode(encoding, "replace")

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeSession:
    """按队列返回响应/异常，并记录每次请求的 URL 与请求头。"""

    def __init__(self, responses: Iterable[Any] | None = None) -> None:
        self.responses: list[Any] = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def push(self, item: Any) -> None:
        """追加一个响应（`dict`/`FakeResponse`/`Exception`）。"""
        self.responses.append(item)

    def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if not self.responses:
            raise AssertionError("FakeSession 没有可用响应")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        if isinstance(item, dict):
            return FakeResponse(
                status=item.get("status", 200),
                body=item.get("body", b""),
                headers=item.get("headers", {}),
            )
        raise TypeError(f"不支持的 FakeSession 响应类型：{type(item)!r}")

    async def close(self) -> None:
        self.closed = True


class RecordingSend:
    """记录 `(umo, text, at_all)` 的 SendFn 替身；按队列返回 True/False 或抛异常。"""

    def __init__(self, outcomes: Iterable[Any] | None = None) -> None:
        self.outcomes: list[Any] = list(outcomes or [])
        self.calls: list[tuple[str, str, bool]] = []

    async def __call__(self, umo: str, text: str, at_all: bool = False) -> bool:
        self.calls.append((umo, text, at_all))
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, BaseException):
            raise outcome
        return bool(outcome)

    @property
    def texts(self) -> list[str]:
        """已发送文本（断言模板内容用）。"""
        return [text for _, text, _ in self.calls]


SHANGHAI = ZoneInfo("Asia/Shanghai")
"""渲染/自然日测试统一使用北京时间。"""


def make_worker(
    repo: Repository,
    send: RecordingSend,
    clock: FakeClock,
    *,
    settings: PluginSettings | None = None,
    tz: ZoneInfo | None = None,
    batch: int = 10,
) -> DeliveryWorker:
    """构造投递 Worker（`settings_provider` / `tz_provider` 均从闭包读取）。"""
    plugin_settings = settings or PluginSettings()
    zone = tz or SHANGHAI
    return DeliveryWorker(
        repo=repo,
        send=send,
        clock=clock,
        settings_provider=lambda: plugin_settings,
        tz_provider=lambda: zone,
        batch=batch,
    )


# ---------------------------------------------------------------------------
# 采集 / 处理链夹具（批次 C）
# ---------------------------------------------------------------------------
class FakeClient:
    """按队列返回 `FetchResult` 的采集器替身，记录每次 `fetch` 的 endpoint。"""

    def __init__(self, results: Iterable[Any] | None = None) -> None:
        self.results: list[Any] = list(results or [])
        self.calls: list[str] = []
        self.etags: dict[str, str | None] = {}
        self.closed = False

    def push(self, result: Any) -> None:
        """追加一个待返回的 `FetchResult`。"""
        self.results.append(result)

    def set_etag(self, endpoint: str, etag: str | None) -> None:
        """记录条件请求的 ETag（`Pipeline.poll_round` 会调用）。"""
        self.etags[endpoint] = etag

    async def fetch(self, endpoint: str) -> Any:
        """返回队列中的下一个结果（队列空则直接失败，便于暴露测试漏配）。"""
        self.calls.append(endpoint)
        if not self.results:
            raise AssertionError(f"FakeClient 没有为 {endpoint} 准备响应")
        return self.results.pop(0)

    async def close(self) -> None:
        """标记已关闭。"""
        self.closed = True


@dataclass
class PipelineBundle:
    """一次构造出的「处理链 + 替身」组合，便于测试按需断言。"""

    pipeline: Any
    client: FakeClient
    send: RecordingSend
    scheduler: Any
    worker: DeliveryWorker
    repo: Repository
    clock: FakeClock
    settings: PluginSettings
    tz: ZoneInfo


def make_pipeline(
    repo: Repository,
    clock: FakeClock,
    *,
    settings: PluginSettings | None = None,
    tz: ZoneInfo | None = None,
    client: FakeClient | None = None,
    send: RecordingSend | None = None,
    rotation: tuple[str, ...] | None = None,
    spacing_seconds: int = 60,
    hooks: Any = None,
) -> PipelineBundle:
    """构造 `Pipeline`（所有外部 I/O 都是替身，零真实请求 / 零真实等待）。"""
    plugin_settings = settings or PluginSettings()
    zone = tz or SHANGHAI
    fake_client = client or FakeClient()
    recorder = send or RecordingSend()
    scheduler = SourceScheduler(
        spacing_seconds=spacing_seconds,
        clock=clock,
        rotation=rotation or DEFAULT_ROTATION,
    )
    classifier = EventClassifier(clock=clock)
    evaluator = ForecastEvaluator(
        time_zone=zone,
        confirm_samples=plugin_settings.forecast_confirm_samples,
        cooldown_hours=plugin_settings.forecast_cooldown_hours,
        daily_max=plugin_settings.forecast_daily_max_per_group,
    )
    worker = DeliveryWorker(
        repo=repo,
        send=recorder,
        clock=clock,
        settings_provider=lambda: plugin_settings,
        tz_provider=lambda: zone,
    )
    pipeline = Pipeline(
        repo=repo,
        client=fake_client,
        scheduler=scheduler,
        classifier=classifier,
        evaluator=evaluator,
        worker=worker,
        clock=clock,
        settings_provider=lambda: plugin_settings,
        tz_provider=lambda: zone,
        hooks=hooks,
    )
    return PipelineBundle(
        pipeline=pipeline,
        client=fake_client,
        send=recorder,
        scheduler=scheduler,
        worker=worker,
        repo=repo,
        clock=clock,
        settings=plugin_settings,
        tz=zone,
    )


# ---------------------------------------------------------------------------
# 合成 payload 构造器（三端点）
# ---------------------------------------------------------------------------
def ok_fetch(
    endpoint: str,
    payload: dict[str, Any],
    *,
    fetched_at: datetime,
    batch: str | None = None,
    checked_at: datetime | None = None,
    status: int = 200,
) -> FetchResult:
    """构造一次成功的采集结果。"""
    return FetchResult(
        endpoint=endpoint,
        ok=True,
        status=status,
        error_code=ErrorCode.OK,
        payload=payload,
        fetched_at=fetched_at,
        batch=batch,
        checked_at=checked_at,
    )


def error_fetch(
    endpoint: str,
    error_code: str,
    *,
    status: int = 0,
    retry_after: int | None = None,
    fetched_at: datetime | None = None,
) -> FetchResult:
    """构造一次失败的采集结果。"""
    return FetchResult(
        endpoint=endpoint,
        ok=False,
        status=status,
        error_code=error_code,
        fetched_at=fetched_at,
        retry_after=retry_after,
    )


def forecast_payload(
    *,
    updated_at: str,
    last_reset_at: str | None = None,
    alert: dict[str, Any] | None = None,
    p24: int = 22,
    p48: int = 39,
    confidence: str = "low",
) -> dict[str, Any]:
    """合成 `/api/forecast` payload。"""
    return {
        "mode": "model",
        "updated_at": updated_at,
        "probabilities": {"rounded_24h": p24, "rounded_48h": p48},
        "confidence": confidence,
        "last_reset_at": last_reset_at,
        "latest_alert": alert,
    }


def timeline_payload(*, updated_at: str, events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """合成 `/api/timeline` payload。"""
    return {"updated_at": updated_at, "events": list(events)}


def feed_payload(
    *,
    fetched_at: str,
    events: Iterable[dict[str, Any]] = (),
    stale: bool = False,
    observations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """合成 `/api/feed` payload。"""
    return {
        "fetched_at": fetched_at,
        "stale": stale,
        "tweets": [],
        "banked_observations": list(observations),
        "events": list(events),
        "newest_post_at": fetched_at,
    }


def reset_row(
    event_id: str,
    announced_at: str,
    *,
    confirmed: bool = True,
    announcement_state: str | None = "announced",
    summary: str = "Codex 全球重置已记录。",
) -> dict[str, Any]:
    """构造一条 timeline/feed 的 reset 行。"""
    return {
        "id": event_id,
        "date": announced_at[:10],
        "type": "reset",
        "group": "reset",
        "summary": summary,
        "url": f"https://x.com/thsottiaux/status/{event_id}",
        "announced_at": announced_at,
        "confirmed": confirmed,
        "preview": False,
        "announcement_state": announcement_state,
        "confidence": "medium",
        "source": "live",
        "source_label": "Live radar feed",
    }


def credits_row(
    event_id: str,
    announced_at: str,
    *,
    state: str = "available",
    summary: str = "Banked reset granted.",
) -> dict[str, Any]:
    """构造一条 timeline/feed 的 credits 行。"""
    return {
        "id": event_id,
        "date": announced_at[:10],
        "type": "credits",
        "group": "credits",
        "summary": summary,
        "url": f"https://x.com/thsottiaux/status/{event_id}",
        "announced_at": announced_at,
        "banked_state": state,
        "confirmed": True,
        "preview": False,
        "announcement_state": "announced",
    }


def watch_alert(
    alert_id: str,
    *,
    state: str = "active",
    start_at: str | None = None,
    end_at: str | None = None,
    source_at: str | None = None,
    corrected: bool = False,
    summary: str = "Reset likely in the announced window.",
) -> dict[str, Any]:
    """构造 `latest_alert`（watch 类）。"""
    window: dict[str, Any] | None = None
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
        "summary": summary,
        "source_at": source_at,
        "url": f"https://x.com/thsottiaux/status/{alert_id}",
        "window": window,
        "corrected": corrected,
    }


def reset_alert(
    alert_id: str,
    *,
    confirmed_at: str | None = None,
    source_at: str | None = None,
    summary: str = "Reset is done.",
) -> dict[str, Any]:
    """构造 `latest_alert`（reset/confirmed 类）。"""
    return {
        "id": alert_id,
        "kind": "reset",
        "state": "confirmed",
        "summary": summary,
        "source_at": source_at,
        "confirmed_at": confirmed_at,
        "url": f"https://x.com/thsottiaux/status/{alert_id}",
        "window": None,
        "corrected": False,
    }


async def create_subscription(
    repo: Repository,
    *,
    umo: str = "aiocqhttp:GroupMessage:1001",
    group_id: str = "1001",
    group_name: str = "测试群",
    override: GroupOverride | None = None,
) -> SubscriptionRow:
    """创建（或更新）一条订阅记录。"""
    return await repo.upsert_subscription(
        umo=umo,
        platform=umo.split(":", 1)[0] if umo else "aiocqhttp",
        group_id=group_id,
        group_name=group_name,
        override=override,
    )


async def mark_baseline_built(repo: Repository, *, last_reset_at: datetime | None = None) -> None:
    """把首轮基线标记为已建立（让后续轮次进入「可推送」状态）。"""
    await repo.save_baseline(
        Baseline(built=True, last_reset_at=last_reset_at, built_at=utc())
    )
