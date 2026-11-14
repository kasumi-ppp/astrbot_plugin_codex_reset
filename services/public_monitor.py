"""Poll public quota announcements and persist product-specific outcomes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from .clock import Clock
from .log import logger
from .models import LogicalEvent, PluginSettings
from .public_messages import product_enabled
from .public_sources import SOURCES, PublicEvent, PublicSourceClient, SourceError
from .repository import Repository

_STATE_ORDER = {"observed": 0, "announced": 1, "reset": 2, "claimable": 2, "retracted": 3}


def _logical(event: PublicEvent) -> LogicalEvent:
    return LogicalEvent(
        logical_id=f"public:{event.product}:{event.event_id}",
        state_version=event.state,
        kind="public_event",
        group_kind="public_event",
        occurred_at=event.occurred_at,
        source_refs=(event.source_url,),
        payload={
            "product": event.product,
            "state": event.state,
            "occurred_at": event.occurred_at.isoformat(),
            "scope": event.scope,
            "source_url": event.source_url,
            "summary": event.summary,
            "expires_at": event.expires_at.isoformat() if event.expires_at else None,
        },
    )


class PublicMonitor:
    def __init__(
        self, repo: Repository, client: PublicSourceClient, clock: Clock,
        settings: Callable[[], PluginSettings],
    ) -> None:
        self.repo = repo
        self.client = client
        self.clock = clock
        self.settings = settings

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            for source in SOURCES:
                tasks.create_task(self._loop(source), name=f"codex-reset-public-{source}")

    async def _loop(self, source: str) -> None:
        while True:
            try:
                delay = await self.poll(source)
            except Exception:
                logger.exception("[codex-reset][public] source=%s monitor failed", source)
                delay = 300
            await asyncio.sleep(delay)

    async def poll(self, source: str) -> int:
        products = [p for p in SOURCES[source] if product_enabled(self.settings(), p)]
        if not products:
            return 5
        now = self.clock.utcnow()
        retry_at = await self.repo.meta_get(f"public:source:{source}:retry_at")
        if retry_at:
            seconds = (datetime.fromisoformat(retry_at) - now).total_seconds()
            if seconds > 0:
                return max(1, int(seconds + 1))
        try:
            events = await self.client.fetch(source)
            now = self.clock.utcnow()
            for product in products:
                if not product_enabled(self.settings(), product):
                    continue
                ordered = sorted(
                    (e for e in events if e.product == product
                     and (e.expires_at is None or e.expires_at > now or e.state != "claimable")),
                    key=lambda e: (e.event_id, _STATE_ORDER[e.state], e.occurred_at),
                )
                # A retracted record must win over older states supplied in the same snapshot.
                retracted = {e.event_id for e in ordered if e.state == "retracted"}
                logical = [
                    _logical(e) for e in ordered
                    if e.event_id not in retracted or e.state == "retracted"
                ]
                seeded, deliveries = await self.repo.record_public_snapshot(product, logical, now=now)
                logger.info(
                    "[codex-reset][public] product=%s baseline=%s events=%s deliveries=%s",
                    product, seeded, len(logical), deliveries,
                )
        except Exception as exc:
            code = exc.code if isinstance(exc, SourceError) else "internal_error"
            key = f"public:source:{source}:failures"
            previous = int(await self.repo.meta_get(key) or "0")
            count = min(previous + 1, 3)
            delay = (900, 1800, 3600)[count - 1] if code == "http_403" else (300, 600, 900)[count - 1]
            if isinstance(exc, SourceError) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            async with self.repo.atomic_meta() as meta:
                await meta.set(key, str(count))
                retry_at = self.clock.utcnow() + timedelta(seconds=delay)
                await meta.set(f"public:source:{source}:retry_at", retry_at.isoformat())
                for product in products:
                    await meta.set(f"public:{product}:error", code)
            logger.warning(
                "[codex-reset][public] source=%s error=%s next_in=%ss",
                source, code, delay, exc_info=not isinstance(exc, SourceError),
            )
            return delay
        async with self.repo.atomic_meta() as meta:
            await meta.delete(f"public:source:{source}:failures")
            await meta.delete(f"public:source:{source}:retry_at")
        return 300
