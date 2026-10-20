"""端到端集成测试：假采集器 → Pipeline → DeliveryWorker → RecordingSend。

覆盖 PRD 的四类消息链路：**确认 → 权益 → 预测 → 预告 → 更正**（更正必须有一条已
推送的预告作为前提，因此共 5 条）。断言真实渲染文本，包括来源署名（AC-20）与
「确认 / 预测」措辞互斥（AC-01 / AC-11 / F03）。
"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    FakeClock,
    create_subscription,
    credits_row,
    feed_payload,
    forecast_payload,
    make_pipeline,
    mark_baseline_built,
    ok_fetch,
    open_repo,
    reset_alert,
    reset_row,
    run,
    timeline_payload,
    utc,
    watch_alert,
)

from services.notifications import SOURCE_LINE

UMO = "aiocqhttp:GroupMessage:1001"
RESET_ID = "2112432076767289678"
RESET_AT = "2026-10-20T06:33:48Z"
CREDITS_ID = "2095651088502591861"
WATCH_ID = "9000000000000000001"


def test_end_to_end_four_message_kinds(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock(utc(2026, 10, 20, 5, 0))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6, 0))
            clock.set_now(utc(2026, 10, 20, 12, 0))
            bundle = make_pipeline(repo, clock)

            window = {
                "start_at": "2026-10-20T12:00:00Z",
                "end_at": "2026-10-20T12:08:00Z",
                "source_at": "2026-10-20T11:30:00Z",
            }
            rounds: list[tuple[str, dict[str, object]]] = [
                # 1) forecast：确认重置 + last_reset_at 前进
                #    → PRD §2：确认重置后下一槽位强制 timeline（P1-01）
                (
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        alert=reset_alert(RESET_ID, confirmed_at=RESET_AT, source_at=RESET_AT),
                        p24=42,
                        p48=50,
                    ),
                ),
                # 2) timeline：额外重置权益到账
                (
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T12:00:30Z",
                        events=[credits_row(CREDITS_ID, "2026-10-20T11:00:00Z")],
                    ),
                ),
                # 3) timeline：强制槽位补采一次（轮换指针未被强制改变，因此仍落 timeline）
                ("timeline", timeline_payload(updated_at="2026-10-20T12:02:00Z", events=[])),
                # 4) forecast：第 1 次达标采样
                (
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T12:01:30Z", last_reset_at=RESET_AT, p24=58, p48=60
                    ),
                ),
                # 5) feed：常规轮次
                ("feed", feed_payload(fetched_at="2026-10-20T12:02:30Z")),
                # 6) forecast：第 2 次达标 → 概率提醒
                (
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T12:03:30Z", last_reset_at=RESET_AT, p24=62, p48=75
                    ),
                ),
                # 7) timeline：无新增
                ("timeline", timeline_payload(updated_at="2026-10-20T12:06:00Z", events=[])),
                # 8) forecast：新 Watch（概率回落，避免再推预测）→ 强制 timeline
                (
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T12:05:30Z",
                        last_reset_at=RESET_AT,
                        p24=30,
                        p48=35,
                        alert=watch_alert(
                            WATCH_ID,
                            source_at=window["source_at"],
                            start_at=window["start_at"],
                            end_at=window["end_at"],
                        ),
                    ),
                ),
                # 9) timeline：窗口已过 + 无窗口内确认 → 一条更正
                ("timeline", timeline_payload(updated_at="2026-10-20T12:08:00Z", events=[])),
            ]

            for index, (endpoint, payload) in enumerate(rounds):
                moment = clock.utcnow()
                bundle.client.push(
                    ok_fetch(
                        endpoint,
                        payload,
                        fetched_at=moment,
                        batch=f"batch-{index}",
                        checked_at=moment,
                    )
                )
                outcome = await bundle.pipeline.poll_round(moment)
                assert outcome is not None, f"第 {index + 1} 轮应当有槽位可采"
                assert bundle.client.calls[-1] == endpoint, (
                    f"第 {index + 1} 轮槽位应为 {endpoint}，实际 {bundle.client.calls[-1]}"
                )
                await bundle.worker.run_once(moment)
                clock.advance(61)

            kinds = [task.kind for task in await repo.list_deliveries()]
            assert kinds == ["reset", "credits", "forecast", "watch", "watch"]

            texts = bundle.send.texts
            assert len(texts) == 5

            reset_text = texts[0]
            assert "✅【Codex 全球重置已确认】" in reset_text
            assert "已确认" in reset_text
            assert "实验性" not in reset_text
            assert f"https://x.com/thsottiaux/status/{RESET_ID}" in reset_text

            credits_text = texts[1]
            assert "🎟️【Codex 额外重置权益动态】" in credits_text
            assert "网站记录该权益已可用" in credits_text

            forecast_text = texts[2]
            assert "📈" in forecast_text
            assert "实验性" in forecast_text
            assert "已确认" not in forecast_text  # F03 绝不使用「已确认」措辞
            assert "不代表官方承诺" in forecast_text

            watch_text = texts[3]
            assert "⚠️【Codex 全球重置提前预警】" in watch_text
            assert "尚未确认完成" in watch_text
            assert "已确认" not in watch_text

            correction_text = texts[4]
            assert "🔁【更正】" in correction_text
            assert "未获确认" in correction_text

            for text in texts:
                assert SOURCE_LINE in text  # AC-20：每条消息都有署名行
            for umo, _text, at_all in bundle.send.calls:
                assert umo == UMO
                assert at_all is False  # 默认不 @全体
            # 全程唯一出站通道就是 SendFn；没有额外投递残留
            assert await repo.count_deliveries("delivered") == 5
            assert await repo.count_deliveries("pending") == 0
        finally:
            await repo.close()

    run(scenario())


def test_end_to_end_confirmed_reset_then_window_is_silent(tmp_path: Path) -> None:
    """窗口内确认重置 → 预告不再补「未获确认」更正（批次 C 语义修正的端到端回归）。"""

    async def scenario() -> None:
        clock = FakeClock(utc(2026, 10, 20, 5, 0))
        repo = await open_repo(tmp_path, clock=clock)
        try:
            await create_subscription(repo, umo=UMO, group_id="1001")
            # 水位与 forecast.last_reset_at 对齐：本轮只应产生「预告」与「窗口内确认」
            await mark_baseline_built(repo, last_reset_at=utc(2026, 10, 20, 6, 33, 48))
            clock.set_now(utc(2026, 10, 20, 12, 0))
            bundle = make_pipeline(repo, clock)

            timeline_payloads = [
                (
                    "forecast",
                    forecast_payload(
                        updated_at="2026-10-20T11:59:00Z",
                        last_reset_at=RESET_AT,
                        p24=20,
                        p48=30,
                        alert=watch_alert(
                            WATCH_ID,
                            source_at="2026-10-20T11:30:00Z",
                            start_at="2026-10-20T12:00:00Z",
                            end_at="2026-10-20T12:08:00Z",
                        ),
                    ),
                ),
                (
                    "timeline",
                    timeline_payload(
                        updated_at="2026-10-20T12:00:30Z",
                        events=[reset_row(RESET_ID, "2026-10-20T12:03:00Z")],
                    ),
                ),
                (
                    "timeline",
                    timeline_payload(updated_at="2026-10-20T12:10:00Z", events=[]),
                ),
            ]
            for index, (endpoint, payload) in enumerate(timeline_payloads):
                moment = clock.utcnow()
                bundle.client.push(
                    ok_fetch(
                        endpoint,
                        payload,
                        fetched_at=moment,
                        batch=f"wbatch-{index}",
                        checked_at=moment,
                    )
                )
                outcome = await bundle.pipeline.poll_round(moment)
                assert outcome is not None
                await bundle.worker.run_once(moment)
                clock.advance(61)

            kinds = [task.kind for task in await repo.list_deliveries()]
            # 1) 预告 + 2) 窗口内确认的重置；第 3 轮窗口过期但重置已落在窗口内 → 无更正
            assert kinds == ["watch", "reset"]
            assert await repo.count_deliveries() == 2
        finally:
            await repo.close()

    run(scenario())
