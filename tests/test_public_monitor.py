from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from conftest import run

from services.clock import FakeClock
from services.models import GroupOverride, PluginSettings
from services.notifications import DeliveryWorker
from services.public_monitor import PublicMonitor
from services.public_sources import SOURCE_ENDPOINTS, PublicEvent, PublicSourceClient, SourceError
from services.repository import Repository


class Samples:
    def __init__(self) -> None:
        self.rows: dict[str, list[PublicEvent]] = {}
        self.error: SourceError | None = None
        self.calls: list[str] = []

    async def fetch(self, source: str) -> list[PublicEvent]:
        self.calls.append(source)
        if self.error and source == "grok":
            raise self.error
        return self.rows.get(source, [])


async def _choose(repo, group, product, subscribe=True):
    return await repo.change_product_subscription(
        umo=f"aiocqhttp:GroupMessage:{group}", platform="aiocqhttp", group_id=group,
        group_name=group, product=product, subscribe=subscribe, group_limit=5,
    )


def test_independent_baselines_and_persisted_403_backoff(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = PluginSettings(enable_grok_bot=True, enable_claude=True)
        samples = Samples()
        samples.error = SourceError("http_403")
        async with _open(tmp_path, clock) as repo:
            monitor = PublicMonitor(repo, samples, clock, lambda: config)
            assert await monitor.poll("grok") == 900
            assert await monitor.poll("claude") == 300
            assert (await repo.public_product_status("claude"))["baseline"]
            assert (await repo.public_product_status("grok-bot"))["error"] == "http_403"
            assert not (await repo.public_product_status("grok-bot"))["baseline"]
            monitor = PublicMonitor(repo, samples, clock, lambda: config)
            assert await monitor.poll("grok") in {900, 901}
            assert samples.calls.count("grok") == 1
            clock.advance(900)
            assert await monitor.poll("grok") == 1800
            clock.advance(1800)
            assert await monitor.poll("grok") == 3600
            clock.advance(3600)
            samples.error = None
            assert await monitor.poll("grok") == 300
            assert (await repo.public_product_status("grok-bot"))["baseline"]
            assert (await repo.public_product_status("grok-bot"))["error"] is None
            assert not (await repo.public_product_status("grok-build"))["baseline"]
            assert await repo.count_deliveries() == 0

    run(scenario())


def test_public_events_through_queue_and_withdrawal(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = PluginSettings(enable_grok_bot=True, enable_grok_build=True, allow_at_all=True)
        samples = Samples()
        sent = []

        async def send(umo, text, at_all):
            sent.append((umo, text, at_all))
            return True

        async with _open(tmp_path, clock) as repo:
            await _choose(repo, "1", "grok-bot")
            await _choose(repo, "2", "grok-build")
            await _choose(repo, "3", "grok-bot")
            await _choose(repo, "4", "codex")
            monitor = PublicMonitor(repo, samples, clock, lambda: config)
            worker = DeliveryWorker(
                repo=repo, clock=clock, send=send, settings_provider=lambda: config,
                tz_provider=lambda: ZoneInfo("Asia/Shanghai"),
            )
            old = PublicEvent(
                "grok-bot", "old", "reset", clock.utcnow() - timedelta(days=1),
                "All users", "https://x.com/bot/status/1", "old",
            )
            samples.rows["grok"] = [old]
            await monitor.poll("grok")
            assert await repo.count_deliveries() == 0
            clock.advance(300)
            event = replace(old, event_id="new", state="announced", occurred_at=clock.utcnow())
            samples.rows["grok"] = [old, event]
            await monitor.poll("grok")
            assert await repo.count_deliveries("pending") == 2
            await _choose(repo, "3", "grok-bot", subscribe=False)
            await _choose(repo, "3", "grok-bot")
            for task in await repo.claim_pending_deliveries(clock.utcnow()):
                await worker._process(task, clock.utcnow())
            assert len(sent) == 1 and "已预告" in sent[0][1] and sent[0][2] is False
            assert sent[0][0].endswith(":1")
            clock.advance(300)
            new = replace(event, state="reset")
            samples.rows["grok"] = [new, replace(new, product="grok-build")]
            await monitor.poll("grok")
            await monitor.poll("grok")
            assert await repo.count_deliveries("pending") == 3
            samples.rows["grok"] = [replace(event, state="retracted")]
            await monitor.poll("grok")
            queued = await repo.claim_pending_deliveries(clock.utcnow())
            assert {(t.umo[-1], t.state_version) for t in queued} == {("1", "retracted"), ("2", "reset")}
            for task in queued:
                await worker._process(task, clock.utcnow())
            assert sum("已撤回" in text for _, text, _ in sent) == 1
            assert any("Grok Build" in text for _, text, _ in sent)
        async with _open(tmp_path, clock) as repo:
            monitor = PublicMonitor(repo, samples, clock, lambda: config)
            await monitor.poll("grok")
            assert (await repo.public_product_status("grok-bot"))["baseline"]
            assert await repo.count_deliveries("pending") == 0

    run(scenario())


def test_old_subscription_keeps_products_with_webui_override(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        async with _open(tmp_path, clock) as repo:
            old = await repo.upsert_subscription(
                umo="aiocqhttp:GroupMessage:1", platform="aiocqhttp", group_id="1", group_name="old",
            )
            assert old.products == ("codex",)
            await _choose(repo, "1", "claude")
            await repo.update_subscription_override(old.umo, GroupOverride(enable_watch=False))
            row = await repo.get_subscription(old.umo)
            assert row.products == ("claude", "codex")
            assert row.override.enable_watch is False
            assert row.subscription_id == old.subscription_id

    run(scenario())


def test_grok_cohorts_and_claude_manual_claim_from_json(monkeypatch) -> None:
    async def scenario() -> None:
        row = {
            "slug": "grok-old-slug", "provider": "xai", "state": "confirmed", "type": "hard_reset",
            "announcedAt": "2026-11-10T21:59:58Z", "products": ["Grok Bot", "Grok Build"],
            "cohort": "all users", "source": {"url": "https://x.com/grok/status/2"},
        }

        async def json_get(request):
            if request.path == "/grok":
                return web.json_response({"data": [row, dict(row, slug="personal", cohort="Your Grok Bot")]})
            return web.json_response({"events": [{
                "id": "5", "ts": "2026-11-10T21:59:58Z", "kind": "regular", "scope": "Pro+Max+Team",
                "origin": "official_post", "url": "https://x.com/ClaudeDevs/status/5",
                "summary": "We've given all Pro, Max, Ultra, and Team users one free full reset. "
                           "Use it anytime before December 10 at 2:00PM PT.",
            }]})

        app = web.Application()
        app.router.add_get("/{source}", json_get)
        runner, url = await _server(app)
        try:
            monkeypatch.setitem(SOURCE_ENDPOINTS, "grok", (url + "/grok",))
            monkeypatch.setitem(SOURCE_ENDPOINTS, "claude", (url + "/claude",))
            client = PublicSourceClient()
            assert {(e.product, e.state) for e in await client.fetch("grok") if e.event_id == "personal"} == {
                ("grok-bot", "observed"), ("grok-build", "observed"),
            }
            events = await client.fetch("grok")
            assert {e.product for e in events if e.state == "reset"} == {"grok-bot", "grok-build"}
            claude, = await client.fetch("claude")
            assert claude.state == "claimable"
            assert claude.expires_at.isoformat() == "2026-12-10T22:00:00+00:00"
            assert "Ultra" in claude.scope
        finally:
            await runner.cleanup()

    run(scenario())


def test_agy_pagination_failure_and_304_preserve_every_page(monkeypatch) -> None:
    async def scenario() -> None:
        fail = True
        second_page_calls = 0

        async def resets(request):
            nonlocal second_page_calls
            cursor = request.query.get("cursor")
            if cursor:
                second_page_calls += 1
                if fail:
                    return web.Response(status=403)
            if request.headers.get("If-None-Match"):
                return web.Response(status=304)
            event_id = "2" if cursor else "1"
            row = {
                "id": event_id, "announced_at": "2026-11-10T21:59:58Z", "models": ["gemini"],
                "source": {"url": f"https://x.com/antigravity/status/{event_id}"},
            }
            return web.json_response(
                {"data": [row], "pagination": {"has_more": not cursor, "next_cursor": "next"}},
                headers={"ETag": event_id},
            )

        async def status(_request):
            return web.json_response({"data": {"scheduled_reset": None}})

        app = web.Application()
        app.router.add_get("/resets", resets)
        app.router.add_get("/status", status)
        runner, url = await _server(app)
        try:
            monkeypatch.setitem(SOURCE_ENDPOINTS, "agy", (url + "/resets", url + "/status"))
            client = PublicSourceClient()
            with pytest.raises(SourceError, match="http_403"):
                await client.fetch("agy")
            fail = False
            assert {event.event_id for event in await client.fetch("agy")} == {"1", "2"}
            assert {event.event_id for event in await client.fetch("agy")} == {"1", "2"}
            assert second_page_calls == 3
        finally:
            await runner.cleanup()

    run(scenario())


@asynccontextmanager
async def _open(path, clock):
    repo = Repository(path / "public.db", clock=clock)
    await repo.open()
    try:
        yield repo
    finally:
        await repo.close()


async def _server(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"
