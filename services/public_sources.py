"""Adapters for independently operated public quota reset timelines."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp

PRODUCTS = {
    "claude": "Claude",
    "grok-bot": "Grok Bot",
    "grok-build": "Grok Build",
    "antigravity-gemini": "Antigravity Gemini",
}
SOURCES = {
    "claude": ("claude",),
    "grok": ("grok-bot", "grok-build"),
    "agy": ("antigravity-gemini",),
}
SOURCE_ENDPOINTS = {
    "claude": ("https://claudereset.org/api/v1/timeline.json",),
    "grok": ("https://quotaresets.com/api/v1/events.json",),
    "agy": ("https://www.agyresets.com/api/v1/resets", "https://www.agyresets.com/api/v1/status"),
}
_MAX_BYTES = 1_000_000
_UA = "AstrBot Codex Reset Radar/1.6.2 (+https://github.com/kasumi-ppp/astrbot_plugin_codex_reset)"


@dataclass(frozen=True, slots=True)
class PublicEvent:
    product: str
    event_id: str
    state: str
    occurred_at: datetime
    scope: str
    source_url: str
    summary: str
    expires_at: datetime | None = None


class SourceError(Exception):
    def __init__(self, code: str, retry_after: int | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None


def _text(obj: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str | int | float) and str(value).strip():
            return str(value).strip()
    return ""


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    rows = data.get(key) if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceError("invalid_json_shape")
    return rows


_CLAIM = re.compile(
    r"use (?:it|them) (?:anytime|before|by)|(?:claim|redeem|activate) (?:it|them|your)|"
    r"reset for free|banked reset", re.I,
)
_PROMISE = re.compile(
    r"\b(?:will|going to|plan to|intend to|we[’']ll)\b.{0,65}\breset\b|"
    r"\breset\b.{0,35}\b(?:will|scheduled to)\b", re.I,
)
_DONE = re.compile(
    r"\b(?:(?:just|already) reset|we[’']re resetting|(?:have|has|we[’']ve) (?:just |also |already )*reset|"
    r"(?:limits|quota|usage) (?:were|was|are now|have been) reset)\b", re.I,
)
_COHORT = re.compile(
    r"\b(?:all|everyone|subscribers|paid plans?|affected users|eligible users|"
    r"users (?:in|on|with)|plans?|accounts|cursor pro|supergrok|customers|members)\b", re.I,
)
_PERSONAL = re.compile(r"^(?:your|you|single|individual|one user|unknown|scope not stated)\b", re.I)


def _permalink(value: Any, authors: set[str] | None = None) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/([0-9]+)", parts.path.rstrip("/"))
    if parts.scheme != "https" or parts.netloc.lower() not in {"x.com", "twitter.com", "www.x.com"}:
        return ""
    if not match or (authors is not None and match[1].lower() not in authors):
        return ""
    return f"https://x.com/{match[1]}/status/{match[2]}"


def _claim_deadline(evidence: str, announced: datetime) -> datetime | None:
    match = re.search(
        r"\b(?:before|by|until)\s+([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?"
        r"\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s+PT\b", evidence, re.I,
    )
    if not match:
        return None
    month, day, year, hour, minute, period = match.groups()
    pacific = ZoneInfo("America/Los_Angeles")
    local = announced.astimezone(pacific)
    try:
        month_number = datetime.strptime(month[:3].title(), "%b").month
        inferred_year = local.year + int(month_number < local.month)
        hour_number = int(hour)
        if not 1 <= hour_number <= 12:
            return None
        deadline = datetime(
            int(year) if year else inferred_year, month_number, int(day),
            hour_number % 12 + (12 if period.upper() == "PM" else 0),
            int(minute or 0), tzinfo=pacific,
        )
    except ValueError:
        return None
    return deadline.astimezone(UTC) if deadline > local else None


def _normalize(source: str, row: dict[str, Any], *, scheduled: bool = False) -> list[PublicEvent]:
    source_data = row.get("source")
    details = source_data if isinstance(source_data, dict) else {}
    summary = _text(row, "summary", "text", "title")
    evidence = _text(details, "excerpt") or summary
    scope = _text(row, "scope", "cohort")
    state = "observed"
    if source == "claude":
        when = _date(row.get("ts"))
        url = _permalink(row.get("url")) if row.get("origin") == "official_post" else ""
        event_id = _text(row, "id")
        kind = _text(row, "kind")
        if kind in {"retracted", "withdrawn"}:
            state = "retracted"
        elif kind == "banked" or (_CLAIM.search(evidence) and re.search(r"\breset", evidence, re.I)):
            state = "claimable"
        elif _DONE.search(evidence):
            state = "reset"
        elif _PROMISE.search(evidence):
            state = "announced"
        products = ("claude",)
        # The published scope can omit plans explicitly listed in the announcement.
        if re.search(r"\ball users\b|\beveryone\b", evidence, re.I) and scope.lower() == "all":
            scope = "全部用户"
        else:
            plans = re.findall(r"\b(Pro|Max|Ultra|Team|Enterprise|Free)\b", evidence, re.I)
            scope = " / ".join(dict.fromkeys(plan.title() for plan in plans))
    elif source == "grok":
        when = _date(row.get("announcedAt"))
        url = _permalink(details.get("url"))
        event_id = _text(row, "resetId", "slug")
        raw_products = row.get("products")
        products = tuple(
            key for key, label in (("grok-bot", "grok bot"), ("grok-build", "grok build"))
            if isinstance(raw_products, list)
            and any(isinstance(value, str) and value.strip().lower() == label for value in raw_products)
        )
        kind = _text(row, "type")
        reported_state = _text(row, "state")
        if reported_state == "retracted":
            state = "retracted"
        elif scope and not _PERSONAL.search(scope) and _COHORT.search(scope):
            if reported_state == "confirmed" and kind == "hard_reset":
                state = "reset"
            elif reported_state == "confirmed" and kind in {"banked_reset", "usage_credit"}:
                state = "claimable"
            elif reported_state in {"likely", "announced"} and kind in {
                "hard_reset", "banked_reset", "usage_credit",
            } and _PROMISE.search(evidence):
                state = "announced"
    else:
        when = _date(row.get("announced_at"))
        url = _permalink(details.get("url"))
        event_id = _text(row, "id")
        refs = row.get("sources")
        if isinstance(refs, list):
            primary = next(
                (ref for ref in refs if isinstance(ref, dict) and ref.get("role") == "primary"
                 and _permalink(ref.get("url"))),
                None,
            )
            if primary is not None:
                url = _permalink(primary["url"])
                event_id = _text(primary, "id") or event_id
            promise = next(
                (ref for ref in refs if isinstance(ref, dict) and ref.get("role") == "promise"
                 and _permalink(ref.get("url"))),
                None,
            )
            if promise is not None:
                event_id = _text(promise, "id") or event_id
        models = row.get("models")
        if isinstance(models, list):
            covers_gemini = any(str(model).lower() in {"gemini", "all"} for model in models)
        else:
            covers_gemini = scheduled and bool(re.search(r"\bgemini\b", summary, re.I))
        products = ("antigravity-gemini",) if covers_gemini else ()
        plans = row.get("plans")
        if isinstance(plans, list):
            scope = " / ".join(str(plan) for plan in plans if plan != "unspecified")
        if _text(row, "status", "state") in {"retracted", "withdrawn", "cancelled"}:
            state = "retracted"
        elif scheduled:
            state = "announced" if _PROMISE.search(evidence) else "observed"
        else:
            state = "reset"
    if when is None or not event_id or not url:
        return []
    expiry = _date(row.get("expires_at") or row.get("expiresAt") or row.get("valid_until"))
    if expiry is None and state == "claimable":
        # Relative phrases are not converted into an assumed deadline.
        match = re.search(r"\b\d{4}-\d{2}-\d{2}T[0-9:+.Z-]+", evidence)
        if match:
            expiry = _date(match[0])
    if expiry is None and state == "claimable":
        expiry = _claim_deadline(evidence, when)
    return [
        PublicEvent(key, event_id, state, when, scope or "范围待确认", url, summary, expiry)
        for key in products
    ]


class PublicSourceClient:
    """Bounded async JSON client with ETag snapshots committed only after valid parsing."""

    def __init__(self, *, session: Any = None, timeout_s: float = 12.0, user_agent: str = _UA):
        self._session = session
        self._timeout = aiohttp.ClientTimeout(
            total=timeout_s, connect=min(4.0, timeout_s), sock_read=min(6.0, timeout_s)
        )
        self._ua = user_agent
        self._etag: dict[str, str] = {}
        self._snapshot: dict[str, Any] = {}

    async def _get_json(self, url: str) -> Any:
        headers = {"User-Agent": self._ua, "Accept": "application/json", "Accept-Encoding": "identity"}
        if url in self._etag:
            headers["If-None-Match"] = self._etag[url]
        own = self._session is None
        session = self._session or aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with session.get(
                url, headers=headers, timeout=self._timeout, allow_redirects=False
            ) as resp:
                if resp.status == 304:
                    if url not in self._snapshot:
                        raise SourceError("unexpected_not_modified")
                    return self._snapshot[url], True, None
                if resp.status in (403, 429):
                    retry = _retry_after(resp.headers.get("Retry-After"))
                    raise SourceError(f"http_{resp.status}", retry)
                if resp.status < 200 or resp.status >= 300:
                    raise SourceError(f"http_{resp.status}")
                if not (resp.content_type == "application/json" or resp.content_type.endswith("+json")):
                    raise SourceError("invalid_content_type")
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > _MAX_BYTES:
                        raise SourceError("response_too_large")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise SourceError("invalid_json") from exc
                return payload, False, resp.headers.get("ETag")
        except SourceError:
            raise
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise SourceError("network_error") from exc
        finally:
            if own:
                await session.close()

    async def fetch(self, source: str) -> list[PublicEvent]:
        if source not in SOURCES:
            raise ValueError(f"unknown source: {source}")
        try:
            async with asyncio.timeout(60):
                return await self._fetch(source)
        except TimeoutError as exc:
            raise SourceError("network_error") from exc

    async def _fetch(self, source: str) -> list[PublicEvent]:
        roots = SOURCE_ENDPOINTS[source]
        combined: list[PublicEvent] = []
        pending: dict[str, tuple[str | None, Any]] = {}
        used_urls: set[str] = set()
        for index, root in enumerate(roots):
            cursor: str | None = None
            seen: set[str] = set()
            for _page in range(20):
                url = root if cursor is None else _with_cursor(root, cursor)
                used_urls.add(url)
                payload, not_modified, etag = await self._get_json(url)
                if not not_modified:
                    pending[url] = etag, payload
                if source == "agy" and index == 1:
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, dict) or "scheduled_reset" not in data:
                        raise SourceError("invalid_json_shape")
                    scheduled = data["scheduled_reset"]
                    if scheduled is not None:
                        if not isinstance(scheduled, dict):
                            raise SourceError("invalid_json_shape")
                        combined.extend(_normalize(source, scheduled, scheduled=True))
                    break
                rows = _rows(payload, "events" if source == "claude" else "data")
                id_fields = ("resetId", "slug") if source == "grok" else ("id",)
                if any(not _text(row, *id_fields) for row in rows):
                    raise SourceError("invalid_event_identity")
                for row in rows:
                    combined.extend(_normalize(source, row))
                if source != "agy":
                    break
                page = payload.get("pagination")
                if not isinstance(page, dict) or not isinstance(page.get("has_more"), bool):
                    raise SourceError("invalid_pagination")
                if not page["has_more"]:
                    break
                next_cursor = page.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                    raise SourceError("invalid_pagination")
                seen.add(next_cursor)
                cursor = next_cursor
            else:
                raise SourceError("pagination_page_limit")
        # Retain snapshots only for URLs observed in this successful round.
        for url in list(self._snapshot):
            if any(url == root or url.startswith(root + "?") for root in roots):
                if url not in used_urls:
                    self._snapshot.pop(url, None)
                    self._etag.pop(url, None)
        for url, (etag, payload) in pending.items():
            if etag:
                self._etag[url] = etag
                self._snapshot[url] = payload
            else:
                self._etag.pop(url, None)
                self._snapshot.pop(url, None)
        return combined


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(1, int(float(value)))
    except (ValueError, OverflowError):
        try:
            dt = parsedate_to_datetime(value)
            return max(1, int((dt - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _with_cursor(url: str, cursor: str) -> str:
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + urlencode({"cursor": cursor})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
