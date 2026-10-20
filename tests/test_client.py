"""采集客户端测试：状态码/异常 → FetchResult 映射、header 解析、条件请求、session 归属。"""

from __future__ import annotations

from datetime import UTC, datetime

import aiohttp
import pytest
from conftest import FakeSession, load_fixture, load_fixture_text, run, utc

from services.client import SourceClient, parse_retry_after
from services.clock import FakeClock
from services.errors import USER_AGENT, ErrorCode

FORECAST_URL = "https://codex-reset.com/api/forecast"


def _client(session: FakeSession | None = None, *, clock: FakeClock | None = None, **kwargs: object):
    return SourceClient(session=session, clock=clock or FakeClock(utc()), **kwargs)


def test_fetch_ok_parses_headers_and_payload() -> None:
    payload = load_fixture("forecast_current")
    headers = load_fixture("headers_304")
    headers["ETag"] = '"v1-abc"'
    headers["x-published-checked-at"] = "2026-10-20T10:19:40.637Z"
    session = FakeSession([{"status": 200, "body": payload, "headers": headers}])
    client = _client(session)

    async def scenario() -> None:
        result = await client.fetch("forecast")
        assert result.ok is True
        assert result.error_code == ErrorCode.OK
        assert result.status == 200
        assert result.batch == "1792491580134"
        assert result.cache_status == "HIT"
        assert result.checked_at == datetime(2026, 10, 20, 10, 19, 40, 637000, tzinfo=UTC)
        assert result.etag == '"v1-abc"'
        assert result.payload is not None
        assert result.payload["probabilities"]["rounded_24h"] == 22
        assert result.retry_after is None

    run(scenario())


def test_request_headers_carry_ua_and_accept() -> None:
    session = FakeSession([{"status": 200, "body": {"ok": True}}])
    client = _client(session)

    async def scenario() -> None:
        await client.fetch("timeline")
        assert session.calls[0]["url"] == "https://codex-reset.com/api/timeline"
        assert session.calls[0]["headers"]["User-Agent"] == USER_AGENT
        assert session.calls[0]["headers"]["Accept"] == "application/json"
        assert "If-None-Match" not in session.calls[0]["headers"]

    run(scenario())


def test_conditional_request_uses_etag() -> None:
    session = FakeSession([{"status": 304, "headers": load_fixture("headers_304")}])
    client = _client(session)
    client.set_etag("forecast", '"cached-etag"')

    async def scenario() -> None:
        result = await client.fetch("forecast")
        assert result.ok is False
        assert result.error_code == ErrorCode.NOT_MODIFIED
        assert result.status == 304
        assert result.batch == "1792491580134"
        assert session.calls[0]["headers"]["If-None-Match"] == '"cached-etag"'

    run(scenario())


def test_429_maps_retry_after_seconds() -> None:
    session = FakeSession([{"status": 429, "headers": load_fixture("headers_429")}])
    client = _client(session)

    async def scenario() -> None:
        result = await client.fetch("forecast")
        assert result.ok is False
        assert result.error_code == ErrorCode.HTTP_429
        assert result.retry_after == 120
        assert result.batch == "1792491580134"

    run(scenario())


def test_429_lowercase_header_and_missing_header_default() -> None:
    session = FakeSession(
        [
            {"status": 429, "headers": {"retry-after": "90"}},
            {"status": 429, "headers": load_fixture("headers_502")},
        ]
    )
    client = _client(session)

    async def scenario() -> None:
        first = await client.fetch("forecast")
        assert first.retry_after == 90
        second = await client.fetch("forecast")
        assert second.retry_after == 60  # 缺失 → 默认 60s

    run(scenario())


def test_5xx_and_4xx_mapping() -> None:
    session = FakeSession(
        [
            {"status": 502, "headers": load_fixture("headers_502")},
            {"status": 503},
            {"status": 404},
            {"status": 403},
        ]
    )
    client = _client(session)

    async def scenario() -> None:
        assert (await client.fetch("forecast")).error_code == ErrorCode.HTTP_5XX
        assert (await client.fetch("timeline")).error_code == ErrorCode.HTTP_5XX
        assert (await client.fetch("feed")).error_code == ErrorCode.HTTP_4XX
        assert (await client.fetch("forecast")).error_code == ErrorCode.HTTP_4XX

    run(scenario())


def test_invalid_json_maps_to_invalid_json() -> None:
    session = FakeSession(
        [
            {"status": 200, "body": load_fixture_text("corrupt_invalid")},
            {"status": 200, "body": "[1, 2, 3]"},
        ]
    )
    client = _client(session)

    async def scenario() -> None:
        first = await client.fetch("timeline")
        assert first.ok is False
        assert first.error_code == ErrorCode.INVALID_JSON
        assert first.payload is None
        second = await client.fetch("feed")
        assert second.error_code == ErrorCode.INVALID_JSON

    run(scenario())


def test_timeout_and_network_errors_never_raise() -> None:
    session = FakeSession([TimeoutError(), aiohttp.ClientError("boom"), OSError("reset")])
    client = _client(session)

    async def scenario() -> None:
        timeout = await client.fetch("forecast")
        assert timeout.ok is False
        assert timeout.error_code == ErrorCode.TIMEOUT
        assert timeout.status == 0

        network = await client.fetch("timeline")
        assert network.error_code == ErrorCode.NETWORK

        os_error = await client.fetch("feed")
        assert os_error.error_code == ErrorCode.NETWORK

    run(scenario())


def test_unexpected_exception_is_mapped_not_raised() -> None:
    session = FakeSession([RuntimeError("weird")])
    client = _client(session)

    async def scenario() -> None:
        result = await client.fetch("forecast")
        assert result.ok is False
        assert result.error_code == ErrorCode.NETWORK

    run(scenario())


def test_unknown_endpoint_raises_value_error() -> None:
    client = _client(FakeSession([]))

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await client.fetch("status")

    run(scenario())


def test_close_only_closes_own_session() -> None:
    injected = FakeSession([])
    client = _client(injected)

    async def scenario() -> None:
        await client.close()
        assert injected.closed is False  # 外部注入的 session 不归我们关

    run(scenario())

    created = FakeSession([{"status": 200, "body": {"ok": True}}])
    factory_client = SourceClient(
        session_factory=lambda: created,
        clock=FakeClock(utc()),
    )

    async def scenario_factory() -> None:
        result = await factory_client.fetch("forecast")
        assert result.ok is True
        await factory_client.close()
        assert created.closed is True

    run(scenario_factory())


def test_parse_retry_after_supports_http_date_and_garbage() -> None:
    assert parse_retry_after("120") == 120
    assert parse_retry_after("120.9") == 120
    assert parse_retry_after(None) == 60
    assert parse_retry_after("garbage") == 60
    assert parse_retry_after("-5") == 60
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 60  # 过去时间 → 默认
