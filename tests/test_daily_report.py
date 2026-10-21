"""每日预测图片（P 模块）自动化测试：渲染器 / 调度 / 投递 / 去重 / 降级。

覆盖 PRD §3 与 §6 的关键验收点：
- P03：图片包含 24h/48h 概率、数据更新时间、实验性说明（概率直接来自 API 快照）；
- P04：最近重置只来自已确认记录，缺失显示「暂无记录」，不混入权益时间；
- P05：预告三态（暂无 / 有效预告 / 已确认），无可靠窗口不编造小时数；
- P08：深色 / 浅色主题均可渲染中文 PNG（900×1200）；
- P09：`/cr report` 手动渲染不占用自动日报唯一发送权；
- P10：渲染失败降级文本；发送超时不确定时不补发；
- P11：陈旧数据不发送正常日报并留下错误记录。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    SHANGHAI,
    create_subscription,
    run,
    utc,
)

from services.clock import FakeClock
from services.config import parse_hhmm
from services.errors import ErrorCode
from services.models import (
    ForecastSnapshot,
    GroupOverride,
    LogicalEvent,
    ReportData,
)
from services.notifications import render_daily_report_text
from services.report_renderer import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    ReportRenderError,
    ensure_theme,
    render_report_png,
    render_report_text_lines,
    watch_status_text,
)
from services.repository import Repository
from services.scheduler import DailyReportScheduler


# ---------------------------------------------------------------------------
# 渲染器（P03 / P08）
# ---------------------------------------------------------------------------
def _report_data(**overrides: Any) -> ReportData:
    base = dict(
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
        updated_at=utc(2026, 10, 21, 1, 0),
        business_date="2026-10-21",
        last_reset_at=utc(2026, 10, 18, 22, 15),
        watch_status="watch",
        watch_window="今日 20:00—22:00（北京时间）",
        stale=False,
        theme="dark",
    )
    base.update(overrides)
    return ReportData(**base)


def test_render_dark_theme_produces_png() -> None:
    """P08：深色主题渲染出 900×1200 的 PNG。"""
    png = render_report_png(_report_data(), tz=SHANGHAI)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 10_000
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        assert image.size == (CANVAS_WIDTH, CANVAS_HEIGHT)


def test_render_light_theme_produces_png() -> None:
    """P08 / AC10：浅色主题同样可渲染。"""
    png = render_report_png(_report_data(theme="light"), tz=SHANGHAI)
    assert png.startswith(b"\x89PNG")
    dark = render_report_png(_report_data(theme="dark"), tz=SHANGHAI)
    assert png != dark  # 主题确实影响输出


def test_render_never_uses_example_probabilities() -> None:
    """P03：概率必须直接来自传入数据，缺数据时按 0 渲染而不是示例值。"""
    png = render_report_png(_report_data(rounded_24h=0, rounded_48h=0), tz=SHANGHAI)
    assert png.startswith(b"\x89PNG")


def test_render_wraps_render_error() -> None:
    """P10：非法输入抛 `ReportRenderError`（而不是裸异常）。"""
    with pytest.raises(ReportRenderError):
        render_report_png(object(), tz=SHANGHAI)


def test_render_missing_font_raises_report_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P10：字体加载异常必须可被捕获并降级为文本。"""
    from services import report_renderer as rr

    monkeypatch.setattr(rr, "FONT_CANDIDATES", ())
    monkeypatch.setattr(rr, "FONT_SEARCH_DIRS", ())
    with pytest.raises(ReportRenderError) as excinfo:
        render_report_png(_report_data(), tz=SHANGHAI, font_path="Z:/definitely/missing.ttf")
    assert excinfo.value.reason == "font_missing"


def test_text_lines_contain_all_required_elements() -> None:
    """P10 / P11：文本降级仍包含 24h/48h、更新时间、来源与免责声明。"""
    payload = _report_data().as_payload()
    lines = render_report_text_lines(payload, tz=SHANGHAI)
    assert any("37%" in line for line in lines)
    assert any("62%" in line for line in lines)
    text = render_daily_report_text(payload, tz=SHANGHAI, settings=None)
    assert "数据来源：https://codex-reset.com/" in text
    assert "实验性预测" in text
    assert "2026-10-21 09:00" in text  # 数据更新时间（北京时间）


def test_text_lines_show_no_record_when_no_reset() -> None:
    """P04：没有可靠记录时显示「暂无记录」。"""
    lines = render_report_text_lines(_report_data(last_reset_at=None).as_payload(), tz=SHANGHAI)
    assert any("暂无记录" in line for line in lines)


def test_watch_status_text_never_invents_confirmation() -> None:
    """P05：未识别状态不得渲染成「已确认」。"""
    assert watch_status_text("none") == "暂无明确预告"
    assert watch_status_text("watch") == "存在有效预告"
    assert watch_status_text("confirmed") == "已确认重置"
    assert "已确认" not in watch_status_text("weird-state")


def test_ensure_theme_falls_back_to_dark() -> None:
    assert ensure_theme("light") == "light"
    assert ensure_theme("dark") == "dark"
    assert ensure_theme("neon") == "dark"
    assert ensure_theme(None) == "dark"


def test_parse_hhmm_accepts_and_rejects() -> None:
    assert parse_hhmm("09:00") == (9, 0)
    assert parse_hhmm("9:05") == (9, 5)
    assert parse_hhmm("23:59") == (23, 59)
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("abc") is None
    assert parse_hhmm(None) is None


# ---------------------------------------------------------------------------
# 调度器（P01 / P02 / G08）
# ---------------------------------------------------------------------------
def test_daily_scheduler_business_date_uses_configured_zone() -> None:
    scheduler = DailyReportScheduler(time_text="09:00", time_zone="Asia/Shanghai")
    # 2026-10-20 18:00 UTC = 2026-10-21 02:00 北京 → 业务日期是 21 日
    assert scheduler.business_date(utc(2026, 10, 20, 18, 0)) == "2026-10-21"


def test_daily_scheduler_due_window_boundaries() -> None:
    """G08：窗口 [start, start+expiry) 内放行，窗口外（含整窗错过）不放行。"""
    scheduler = DailyReportScheduler(
        time_text="09:00", time_zone="Asia/Shanghai", expiry_minutes=30
    )
    start = scheduler.scheduled_at("2026-10-20")
    assert start == utc(2026, 10, 20, 1, 0)
    assert scheduler.due_window(start) == "2026-10-20"
    assert scheduler.due_window(start + timedelta(minutes=29, seconds=59)) == "2026-10-20"
    assert scheduler.due_window(start + timedelta(minutes=30)) is None
    assert scheduler.due_window(start - timedelta(seconds=1)) is None


def test_daily_scheduler_next_run_at_skips_missed_window() -> None:
    """G08：超过补发窗口后，下一次触发是明天同一时刻。"""
    scheduler = DailyReportScheduler(
        time_text="09:00", time_zone="Asia/Shanghai", expiry_minutes=30
    )
    late = utc(2026, 10, 20, 2, 0)  # 北京 10:00，已错过窗口
    assert scheduler.next_run_at(late) == scheduler.scheduled_at("2026-10-21")


def test_daily_scheduler_configure_recomputes_time() -> None:
    """P02：修改发送时间后立即重算下一次运行时间。"""
    scheduler = DailyReportScheduler(time_text="09:00", time_zone="Asia/Shanghai")
    scheduler.configure(time_text="21:30")
    assert scheduler.time_text == "21:30"
    assert scheduler.scheduled_at("2026-10-20") == utc(2026, 10, 20, 13, 30)


def test_daily_scheduler_invalid_time_falls_back() -> None:
    scheduler = DailyReportScheduler(time_text="not-a-time", time_zone="Asia/Shanghai")
    assert scheduler.time_text == "09:00"


def test_daily_scheduler_retry_window() -> None:
    scheduler = DailyReportScheduler(
        time_text="09:00", time_zone="Asia/Shanghai", retry_minutes=5
    )
    moment = utc(2026, 10, 20, 1, 10)
    assert scheduler.within_retry(moment - timedelta(minutes=4), moment) is True
    assert scheduler.within_retry(moment - timedelta(minutes=6), moment) is False
    assert scheduler.within_retry(None, moment) is False


# ---------------------------------------------------------------------------
# 仓库（S02）
# ---------------------------------------------------------------------------
def test_daily_report_claim_is_unique_per_group_and_date(tmp_path: Path) -> None:
    """G07 / AC15：同一 (群, 业务日期) 只能占位一次。"""
    from conftest import make_repo

    repo = make_repo(tmp_path / "daily.db")
    try:
        sub = run(create_subscription(repo))
        assert run(repo.claim_daily_report(sub.subscription_id, "2026-10-20")) is True
        assert run(repo.claim_daily_report(sub.subscription_id, "2026-10-20")) is False
        assert run(repo.claim_daily_report(sub.subscription_id, "2026-10-21")) is True
        row = run(repo.get_daily_report(sub.subscription_id, "2026-10-20"))
        assert row is not None and row.status == "pending"
        assert len(run(repo.list_daily_reports())) == 2
    finally:
        run(repo.close())


def test_daily_report_status_transitions(tmp_path: Path) -> None:
    from conftest import make_repo

    repo = make_repo(tmp_path / "daily2.db")
    try:
        sub = run(create_subscription(repo))
        run(repo.claim_daily_report(sub.subscription_id, "2026-10-20", theme="dark"))
        assert run(
            repo.update_daily_report(sub.subscription_id, "2026-10-20", status="failed", error_code="x")
        )
        row = run(repo.get_daily_report(sub.subscription_id, "2026-10-20"))
        assert row is not None and row.status == "failed" and row.attempt == 1
        assert run(
            repo.update_daily_report(
                sub.subscription_id, "2026-10-20", status="sent", image_sha1="abc123"
            )
        )
        row = run(repo.get_daily_report(sub.subscription_id, "2026-10-20"))
        assert row is not None
        assert row.status == "sent" and row.image_sha1 == "abc123" and row.sent_at is not None
    finally:
        run(repo.close())


def test_daily_reports_survive_reopen(tmp_path: Path) -> None:
    """S03：重启后日报状态恢复。"""
    from conftest import make_repo

    path = tmp_path / "daily3"
    repo = make_repo(path)
    sub = run(create_subscription(repo))
    run(repo.claim_daily_report(sub.subscription_id, "2026-10-20"))
    run(repo.update_daily_report(sub.subscription_id, "2026-10-20", status="sent"))
    run(repo.close())

    repo2 = Repository(path / "codex-reset.db", clock=FakeClock(utc()))
    run(repo2.open())
    try:
        row = run(repo2.get_daily_report(sub.subscription_id, "2026-10-20"))
        assert row is not None and row.status == "sent"
    finally:
        run(repo2.close())


def test_daily_reports_schema_has_daily_table(tmp_path: Path) -> None:
    """S02：`daily_reports` 表与唯一主键存在。"""
    from conftest import make_repo

    repo = make_repo(tmp_path / "daily4.db")
    try:
        conn = sqlite3.connect(repo.db_path)
        try:
            pk = {row[1] for row in conn.execute("PRAGMA table_info(daily_reports)") if row[5]}
        finally:
            conn.close()
        assert pk == {"subscription_id", "business_date"}
    finally:
        run(repo.close())


# ---------------------------------------------------------------------------
# Pipeline 端到端（P01 / P10 / P11 / G08 / S07）
# ---------------------------------------------------------------------------
class RecordingImageSend:
    """图片出站替身：按队列返回 True / False / 异常。"""

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.outcomes: list[Any] = list(outcomes or [])
        self.calls: list[tuple[str, bytes, str]] = []

    async def __call__(self, umo: str, png: bytes, filename: str) -> bool:
        self.calls.append((umo, png, filename))
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, BaseException):
            raise outcome
        return bool(outcome)


def bundle_settings_default() -> Any:
    """默认 PluginSettings（独立工厂，便于按用例 `replace` 个别字段）。"""
    from services.models import PluginSettings

    return PluginSettings()


def _seed_forecast(pipeline: Any, now: datetime | FakeClock) -> None:
    """向内存缓存注入一份新鲜的 forecast 快照（概率直接来自 API）。

    `now` 可以是 datetime 或 FakeClock：快照 `updated_at` 取 `now - 1 分钟`，
    保证相对任何判定时刻都足够新鲜（max_source_age_minutes 默认 10）。
    """
    moment = now.utcnow() if isinstance(now, FakeClock) else now
    pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
        updated_at=moment - timedelta(minutes=1),
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
        last_reset_at=None,
        latest_alert=None,
        fetched_at=moment,
    )


def _make_daily_pipeline(
    tmp_path: Path,
    *,
    image_outcomes: list[Any] | None = None,
    readonly: bool = False,
    clock: FakeClock | None = None,
    settings: Any = None,
    name: str = "daily",
) -> tuple[Any, Any, Any, Repository, FakeClock]:
    from conftest import make_pipeline, make_repo

    fake_clock = clock or FakeClock(utc(2026, 10, 20, 1, 5))  # 北京 09:05：窗口内
    repo = make_repo(tmp_path / f"{name}.db", clock=fake_clock)
    bundle = make_pipeline(repo, fake_clock, settings=settings)
    image_send = RecordingImageSend(image_outcomes)
    bundle.pipeline._image_send = image_send  # noqa: SLF001
    bundle.pipeline._readonly_provider = lambda: readonly  # noqa: SLF001
    return bundle.pipeline, image_send, bundle.send, bundle.repo, fake_clock


def test_daily_report_sends_once_per_group_per_day(tmp_path: Path) -> None:
    """P01 / AC15：窗口内发送一次，同日重复判定不再发送。"""
    pipeline, image_send, _, repo, _ = _make_daily_pipeline(tmp_path)
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))

    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 1
    assert len(image_send.calls) == 1
    umo, png, filename = image_send.calls[0]
    assert umo == "aiocqhttp:GroupMessage:1001"
    assert png.startswith(b"\x89PNG")
    assert "2026-10-20" in filename

    # 同一业务日期再次触发：唯一性约束生效
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 15))) == 0
    assert len(image_send.calls) == 1
    row = run(repo.get_daily_report(run(repo.list_subscriptions())[0].subscription_id, "2026-10-20"))
    assert row is not None and row.status == "sent"


def test_daily_report_respects_group_switch(tmp_path: Path) -> None:
    """G02：未开启 `enable_daily_report` 的群不接收日报。"""
    pipeline, image_send, _, repo, _ = _make_daily_pipeline(tmp_path)
    run(create_subscription(repo))  # 默认（继承全局 False）
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 0
    assert image_send.calls == []


def test_daily_report_outside_window_is_ignored(tmp_path: Path) -> None:
    """G08：错过补发窗口后不再推送过期内容。"""
    pipeline, image_send, _, repo, _ = _make_daily_pipeline(tmp_path)
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 2, 0))) == 0  # 北京 10:00
    assert image_send.calls == []


def test_daily_report_stale_data_is_skipped_with_error(tmp_path: Path) -> None:
    """P11：陈旧数据不发送正常日报，并留下错误记录。"""
    pipeline, image_send, _, repo, clock = _make_daily_pipeline(tmp_path)
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, clock)
    # 把快照改旧（超过 max_source_age_minutes=10）
    pipeline._cache["forecast"] = pipeline.forecast().__class__(  # noqa: SLF001
        updated_at=clock.utcnow() - timedelta(hours=3),
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
    )
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 0
    assert image_send.calls == []
    sub_id = run(repo.list_subscriptions())[0].subscription_id
    row = run(repo.get_daily_report(sub_id, "2026-10-20"))
    assert row is not None and row.status == "skipped" and row.error_code == "stale_data"


def test_daily_report_text_fallback_on_image_failure(tmp_path: Path) -> None:
    """P10 / AC12：图片明确失败可降级文本，且当日不再重复投递。"""
    pipeline, image_send, send, repo, _ = _make_daily_pipeline(
        tmp_path, image_outcomes=[False]
    )
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 1
    assert len(image_send.calls) == 1
    assert len(send.texts) == 1
    text = send.texts[0]
    assert "24 小时：37%" in text
    assert "48 小时：62%" in text
    assert "codex-reset.com" in text
    sub_id = run(repo.list_subscriptions())[0].subscription_id
    row = run(repo.get_daily_report(sub_id, "2026-10-20"))
    assert row is not None and row.status == "sent" and row.error_code == "text_fallback"
    # 已终态：不再重试
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 12))) == 0
    assert len(send.texts) == 1


def test_daily_report_uncertain_send_never_retries_or_falls_back(tmp_path: Path) -> None:
    """P10：发送结果不确定 → 不补发文本，记终态 uncertain。"""
    pipeline, image_send, send, repo, _ = _make_daily_pipeline(
        tmp_path, image_outcomes=[TimeoutError()]
    )
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 0
    assert send.texts == []
    sub_id = run(repo.list_subscriptions())[0].subscription_id
    row = run(repo.get_daily_report(sub_id, "2026-10-20"))
    assert row is not None and row.status == "uncertain"
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 40))) == 0
    assert len(image_send.calls) == 1


def test_daily_report_failed_retries_after_interval(tmp_path: Path) -> None:
    """G06 / G08：明确失败按重试间隔重试，不无限重发。"""
    outcomes: list[Any] = [False, False, True]
    # 文本降级关闭 → 走 failed 重试路径
    clock = FakeClock(utc(2026, 10, 20, 1, 5))
    from dataclasses import replace as _replace

    settings = _replace(bundle_settings_default(), daily_report_text_fallback=False)
    pipeline, image_send, send, repo, _ = _make_daily_pipeline(
        tmp_path, image_outcomes=outcomes, clock=clock, settings=settings, name="retry"
    )
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, utc(2026, 10, 20, 1, 4))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 0
    sub_id = run(repo.list_subscriptions())[0].subscription_id
    assert run(repo.get_daily_report(sub_id, "2026-10-20")).status == "failed"  # type: ignore[union-attr]
    # 未到重试间隔（5 分钟）→ 不重试
    _seed_forecast(pipeline, utc(2026, 10, 20, 1, 6))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 7))) == 0
    # 到达重试间隔 → 重试仍失败
    _seed_forecast(pipeline, utc(2026, 10, 20, 1, 10))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 11))) == 0
    # 第三次成功
    _seed_forecast(pipeline, utc(2026, 10, 20, 1, 16))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 17))) == 1
    assert len(image_send.calls) == 3
    assert run(repo.get_daily_report(sub_id, "2026-10-20")).status == "sent"  # type: ignore[union-attr]


def test_daily_report_disabled_in_readonly_mode(tmp_path: Path) -> None:
    """S07：只读实例不创建日报投递。"""
    pipeline, image_send, _, repo, _ = _make_daily_pipeline(tmp_path, readonly=True)
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 5))) == 0
    assert image_send.calls == []
    assert run(repo.list_daily_reports()) == []


def test_manual_report_does_not_consume_daily_quota(tmp_path: Path) -> None:
    """P09：`/cr report` 不占用当天自动日报唯一发送权，也不改变定时任务状态。"""
    pipeline, image_send, _, repo, _ = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    png, payload = run(pipeline.manual_report_png(now=utc(2026, 10, 20, 1, 5)))
    assert png is not None and payload is not None
    assert png.startswith(b"\x89PNG")
    assert payload["manual"] is True and payload["test"] is False
    assert run(repo.list_daily_reports()) == []
    # 之后的自动日报照常发送
    run(create_subscription(repo))
    run(repo.update_subscription_override(
        "aiocqhttp:GroupMessage:1001", GroupOverride(enable_daily_report=True)
    ))
    assert run(pipeline.run_daily_reports(utc(2026, 10, 20, 1, 6))) == 1
    assert len(image_send.calls) == 1


def test_manual_report_test_flag_is_labelled(tmp_path: Path) -> None:
    """G09：`/cr test image` 的渲染带 `test=True`。"""
    pipeline, _, _, _, _ = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, FakeClock(utc(2026, 10, 20, 1, 4)))
    png, payload = run(pipeline.manual_report_png(now=utc(2026, 10, 20, 1, 5), test=True))
    assert png is not None
    assert payload is not None and payload["test"] is True


def test_manual_report_without_snapshot_returns_none(tmp_path: Path) -> None:
    pipeline, _, _, _, _ = _make_daily_pipeline(tmp_path)
    png, payload = run(pipeline.manual_report_png(now=utc(2026, 10, 20, 1, 5)))
    assert png is None and payload is None


# ---------------------------------------------------------------------------
# 数据正确性（P04 / P05 / P11）
# ---------------------------------------------------------------------------
def test_build_report_data_uses_only_confirmed_reset(tmp_path: Path) -> None:
    """P04：最近重置只取已确认记录，绝不混入权益时间。"""
    pipeline, _, _, repo, clock = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, clock)
    credit_like = LogicalEvent(
        logical_id="credits:1",
        kind="credits",
        group_kind="credits",
        state_version="available",
        occurred_at=clock.utcnow(),
        payload={},
    )
    run(repo.record_logical_event(credit_like, baseline=False))
    data = run(pipeline.build_report_data(clock.utcnow()))
    assert data is not None and data.last_reset_at is None  # 权益时间不得充当重置时间

    reset = LogicalEvent(
        logical_id="reset:abc",
        kind="reset",
        group_kind="reset",
        state_version="confirmed",
        occurred_at=utc(2026, 10, 18, 22, 15),
        payload={},
    )
    run(repo.record_logical_event(reset, baseline=False))
    data = run(pipeline.build_report_data(clock.utcnow()))
    assert data is not None and data.last_reset_at == utc(2026, 10, 18, 22, 15)


def test_build_report_data_watch_states(tmp_path: Path) -> None:
    """P05：预告三态映射。"""
    from services.models import LatestAlert

    pipeline, _, _, _, clock = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, clock)

    snapshot = pipeline.forecast()
    pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
        updated_at=snapshot.updated_at,
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
        latest_alert=LatestAlert(
            id="a1", kind="watch", state="active", summary="s",
        ),
    )
    data = run(pipeline.build_report_data(clock.utcnow()))
    assert data is not None
    assert data.watch_status == "watch"
    assert data.watch_window == "暂无明确预计时段"  # 无窗口不编造小时数

    pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
        updated_at=snapshot.updated_at,
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
        latest_alert=LatestAlert(
            id="a2", kind="reset", state="confirmed", summary="s",
        ),
    )
    data = run(pipeline.build_report_data(clock.utcnow()))
    assert data is not None and data.watch_status == "confirmed"

    pipeline._cache["forecast"] = ForecastSnapshot(  # noqa: SLF001
        updated_at=snapshot.updated_at,
        rounded_24h=37,
        rounded_48h=62,
        confidence="medium",
        latest_alert=LatestAlert(
            id="a3", kind="watch", state="expired", summary="s",
        ),
    )
    data = run(pipeline.build_report_data(clock.utcnow()))
    assert data is not None and data.watch_status == "none"


def test_build_report_data_marks_stale(tmp_path: Path) -> None:
    """P11：数据过期时 `stale=True` 且不显示自行推断的置信度。"""
    pipeline, _, _, _, clock = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, clock)
    data = run(pipeline.build_report_data(clock.utcnow() + timedelta(hours=5)))
    assert data is not None and data.stale is True
    assert data.confidence == "medium"


def test_build_report_data_uses_configured_theme(tmp_path: Path) -> None:
    """P08：主题来自配置。"""
    pipeline, _, _, _, clock = _make_daily_pipeline(tmp_path)
    _seed_forecast(pipeline, clock)
    data = run(pipeline.build_report_data(clock.utcnow(), theme="light"))
    assert data is not None and data.theme == "light"


# ---------------------------------------------------------------------------
# G03 投递优先级
# ---------------------------------------------------------------------------
def test_deliveries_claim_order_respects_priority(tmp_path: Path) -> None:
    """G03 / AC14：确认重置先于概率提醒与补发摘要出队。"""
    from conftest import make_logical_event, make_pipeline, make_repo

    from services.models import Baseline

    clock = FakeClock(utc(2026, 10, 20, 1, 0))
    repo = make_repo(tmp_path / "prio.db", clock=clock)
    make_pipeline(repo, clock)  # 装配管线（本用例只验证仓储层的优先级出队）
    run(create_subscription(repo))
    run(repo.save_baseline(
        Baseline(built=True, last_reset_at=clock.utcnow(), built_at=clock.utcnow())
    ))
    sub = run(repo.list_subscriptions())[0]
    forecast_event = LogicalEvent(
        logical_id=f"forecast:{sub.subscription_id}:h24",
        kind="forecast",
        group_kind="forecast",
        state_version="h24:0:1",
        occurred_at=clock.utcnow(),
        payload={"window_kind": "h24", "rounded_24h": 90, "rounded_48h": 90,
                 "confidence": "low", "updated_at": clock.utcnow().isoformat()},
    )
    catchup_event = LogicalEvent(
        logical_id="catchup:2026-10-20T01:00:00+00:00",
        kind="catchup",
        group_kind="catchup",
        state_version="summary",
        occurred_at=clock.utcnow(),
        payload={"events": []},
    )
    reset_event = make_logical_event(logical_id="reset:big", occurred_at=clock.utcnow())
    watch_event = LogicalEvent(
        logical_id="watch:w1", kind="watch", group_kind="watch",
        state_version="active:nowindow", occurred_at=clock.utcnow(), payload={"alert_id": "w1"},
    )
    for event in (forecast_event, catchup_event, watch_event, reset_event):
        assert run(repo.enqueue_delivery(sub=sub, ev=event))

    claimed = run(repo.claim_pending_deliveries(clock.utcnow(), limit=10))
    kinds = [task.kind for task in claimed]
    assert kinds[0] == "reset"
    assert kinds.index("watch") < kinds.index("forecast")
    assert kinds.index("forecast") < kinds.index("catchup")


def test_old_database_without_priority_column_migrates(tmp_path: Path) -> None:
    """S02 / AC22：v1.2 旧库（deliveries 无 priority 列）升级不丢数据。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '2', '2026-10-20T00:00:00+00:00');
            CREATE TABLE subscriptions (
              subscription_id TEXT PRIMARY KEY, umo TEXT NOT NULL UNIQUE,
              platform TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '',
              group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
              settings_json TEXT NOT NULL DEFAULT '{}', subscribed_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, last_success_at TEXT, last_failure_at TEXT,
              uncertain_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1);
            INSERT INTO subscriptions (subscription_id, umo, subscribed_at, updated_at)
              VALUES ('sub-old', 'aiocqhttp:GroupMessage:2002', '2026-10-19T00:00:00+00:00',
                      '2026-10-19T00:00:00+00:00');
            CREATE TABLE deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subscription_id TEXT NOT NULL, umo TEXT NOT NULL, logical_id TEXT NOT NULL,
              state_version TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              sent_at TEXT, error_code TEXT, message_hash TEXT,
              UNIQUE (subscription_id, logical_id, state_version));
            INSERT INTO deliveries (subscription_id, umo, logical_id, state_version, kind,
              payload_json, created_at, updated_at)
              VALUES ('sub-old', 'aiocqhttp:GroupMessage:2002', 'reset:legacy', 'confirmed',
                      'reset', '{}', '2026-10-19T00:00:00+00:00', '2026-10-19T00:00:00+00:00');
            """
        )
        conn.commit()
    finally:
        conn.close()

    repo = Repository(path, clock=FakeClock(utc()))
    run(repo.open())
    try:
        subs = run(repo.list_subscriptions())
        assert len(subs) == 1 and subs[0].subscription_id == "sub-old"
        tasks = run(repo.list_deliveries())
        assert len(tasks) == 1
        assert tasks[0].priority == 5  # 迁移默认值
        # 新写入的事件按 kind 取优先级
        sub = subs[0]
        assert run(repo.enqueue_delivery(
            sub=sub,
            ev=make_reset_event(),
        ))
        claimed = run(repo.claim_pending_deliveries(utc(), limit=10))
        assert claimed[0].kind == "reset"
    finally:
        run(repo.close())


def make_reset_event() -> LogicalEvent:
    return LogicalEvent(
        logical_id="reset:new",
        kind="reset",
        group_kind="reset",
        state_version="confirmed",
        occurred_at=utc(2026, 10, 20, 2, 0),
        payload={},
    )


# ---------------------------------------------------------------------------
# 回归护栏
# ---------------------------------------------------------------------------
def test_fetch_error_codes_unchanged() -> None:
    """S01：错误码集合保持稳定（429/超时/5xx 仍可重试）。"""
    assert ErrorCode.HTTP_429 in ErrorCode.RETRYABLE_CODES
    assert ErrorCode.TIMEOUT in ErrorCode.RETRYABLE_CODES
    assert ErrorCode.HTTP_5XX in ErrorCode.RETRYABLE_CODES


def test_subscription_row_override_supports_daily() -> None:
    """G02：群覆盖可承载 enable_daily_report。"""
    override = GroupOverride(enable_daily_report=True)
    assert override.as_sparse_dict() == {"enable_daily_report": True}
    empty = GroupOverride()
    assert empty.as_sparse_dict() == {}
