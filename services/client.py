"""采集客户端：aiohttp GET 三端点，状态码/异常 → `FetchResult`（永不抛异常）。

`fetch()` 的唯一失败方式是 `ValueError`（未知 endpoint，属于编程错误）。
其余一切异常都映射为错误码，保证轮询循环不会因上游异常而终止（AC-18）。
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from .clock import Clock
from .errors import SITE_BASE_URL, USER_AGENT, ErrorCode
from .models import FetchResult
from .parser import parse_iso

logger = logging.getLogger(__name__)

ENDPOINTS: tuple[str, ...] = ("forecast", "timeline", "feed")
DEFAULT_BASE_URL = SITE_BASE_URL
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_RETRY_AFTER_S = 60
MAX_RETRY_AFTER_S = 3600


def _header(headers: Any, name: str) -> str | None:
    """大小写不敏感读取响应头（兼容 aiohttp CIMultiDict 与测试用普通 dict）。"""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return str(value)
    try:
        for key, value in dict(headers).items():
            if str(key).lower() == name.lower():
                return str(value)
    except (TypeError, ValueError):
        return None
    return None


def parse_retry_after(value: Any) -> int:
    """解析 `Retry-After`：支持秒数与 HTTP-date；失败返回默认 60s。"""
    if value is None:
        return DEFAULT_RETRY_AFTER_S
    text = str(value).strip()
    if not text:
        return DEFAULT_RETRY_AFTER_S
    try:
        seconds = int(float(text))
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return DEFAULT_RETRY_AFTER_S
        if when is None:
            return DEFAULT_RETRY_AFTER_S
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delta = (when - datetime.now(UTC)).total_seconds()
        seconds = int(delta)
    if seconds <= 0:
        return DEFAULT_RETRY_AFTER_S
    return min(seconds, MAX_RETRY_AFTER_S)


class SourceClient:
    """codex-reset.com 三个公开 JSON 端点的采集器。"""

    def __init__(
        self,
        *,
        session: Any = None,
        session_factory: Any = None,
        clock: Clock,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = USER_AGENT,
        etag_provider: Any = None,
    ) -> None:
        self._clock = clock
        self._timeout_s = float(timeout_s)
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._session = session
        self._session_factory = session_factory
        self._owns_session = False
        self._etag_provider = etag_provider
        self._etags: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        if self._session_factory is not None:
            candidate = self._session_factory()
            if inspect.isawaitable(candidate):
                candidate = await candidate
            self._session = candidate
            self._owns_session = True
            return candidate
        timeout = aiohttp.ClientTimeout(
            total=self._timeout_s,
            connect=min(DEFAULT_CONNECT_TIMEOUT_S, self._timeout_s),
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._owns_session = True
        return self._session

    async def close(self) -> None:
        """关闭**自己创建**的 session；外部注入的 session 由注入方负责。"""
        if self._session is not None and self._owns_session:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover - 关闭失败不影响退出
                logger.warning("[codex-reset][client] session 关闭异常", exc_info=True)
        self._session = None
        self._owns_session = False

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    def set_etag(self, endpoint: str, etag: str | None) -> None:
        """登记某端点的 ETag（供下一轮条件请求使用）。"""
        if etag:
            self._etags[endpoint] = etag
        else:
            self._etags.pop(endpoint, None)

    def _etag_for(self, endpoint: str) -> str | None:
        if self._etag_provider is not None:
            value = self._etag_provider(endpoint)
            if value:
                return str(value)
        return self._etags.get(endpoint)

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int(round((self._clock.monotonic() - started) * 1000)))

    async def fetch(self, endpoint: str) -> FetchResult:
        """采集单个端点；异常与状态码全部映射为 `FetchResult`。"""
        if endpoint not in ENDPOINTS:
            raise ValueError(f"不支持的 endpoint: {endpoint!r}（仅 {ENDPOINTS}）")

        started = self._clock.monotonic()
        url = f"{self._base_url}/api/{endpoint}"
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        etag = self._etag_for(endpoint)
        if etag:
            headers["If-None-Match"] = etag

        try:
            session = await self._ensure_session()
            async with session.get(url, headers=headers) as response:
                status = int(getattr(response, "status", 0))
                raw_headers = getattr(response, "headers", None)
                fetched_at = self._clock.utcnow()
                batch = _header(raw_headers, "x-published-batch")
                cache_status = _header(raw_headers, "x-published-cache-status")
                checked_at = parse_iso(_header(raw_headers, "x-published-checked-at"))
                new_etag = _header(raw_headers, "ETag")
                latency_ms = self._elapsed_ms(started)

                common: dict[str, Any] = {
                    "endpoint": endpoint,
                    "status": status,
                    "fetched_at": fetched_at,
                    "batch": batch,
                    "cache_status": cache_status,
                    "checked_at": checked_at,
                    "latency_ms": latency_ms,
                    "etag": new_etag,
                }

                if status == 304:
                    logger.info("[codex-reset][client] endpoint=%s status=304 batch=%s", endpoint, batch)
                    return FetchResult(ok=False, error_code=ErrorCode.NOT_MODIFIED, **common)

                if status == 429:
                    retry_after = parse_retry_after(_header(raw_headers, "Retry-After"))
                    logger.warning(
                        "[codex-reset][client] endpoint=%s status=429 retry_after=%s",
                        endpoint,
                        retry_after,
                    )
                    return FetchResult(
                        ok=False,
                        error_code=ErrorCode.HTTP_429,
                        retry_after=retry_after,
                        **common,
                    )

                if status >= 500:
                    logger.warning("[codex-reset][client] endpoint=%s status=%s", endpoint, status)
                    return FetchResult(ok=False, error_code=ErrorCode.HTTP_5XX, **common)

                if status != 200:
                    logger.warning("[codex-reset][client] endpoint=%s status=%s", endpoint, status)
                    return FetchResult(ok=False, error_code=ErrorCode.HTTP_4XX, **common)

                body = await response.read()
                text = (
                    body.decode("utf-8", "replace")
                    if isinstance(body, bytes | bytearray)
                    else str(body)
                )
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "[codex-reset][client] endpoint=%s JSON 解码失败 len=%s", endpoint, len(text)
                    )
                    return FetchResult(ok=False, error_code=ErrorCode.INVALID_JSON, **common)
                if not isinstance(payload, dict):
                    logger.warning(
                        "[codex-reset][client] endpoint=%s JSON 顶层不是对象（%s）",
                        endpoint,
                        type(payload).__name__,
                    )
                    return FetchResult(ok=False, error_code=ErrorCode.INVALID_JSON, **common)

                if new_etag:
                    self._etags[endpoint] = new_etag
                logger.info(
                    "[codex-reset][client] endpoint=%s ok batch=%s cache=%s latency_ms=%s",
                    endpoint,
                    batch,
                    cache_status,
                    latency_ms,
                )
                return FetchResult(
                    ok=True,
                    error_code=ErrorCode.OK,
                    payload=payload,
                    **common,
                )

        except TimeoutError:
            logger.warning(
                "[codex-reset][client] endpoint=%s timeout latency_ms=%s",
                endpoint,
                self._elapsed_ms(started),
            )
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                status=0,
                error_code=ErrorCode.TIMEOUT,
                fetched_at=self._clock.utcnow(),
                latency_ms=self._elapsed_ms(started),
            )
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning(
                "[codex-reset][client] endpoint=%s network error=%s latency_ms=%s",
                endpoint,
                type(exc).__name__,
                self._elapsed_ms(started),
            )
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                status=0,
                error_code=ErrorCode.NETWORK,
                fetched_at=self._clock.utcnow(),
                latency_ms=self._elapsed_ms(started),
            )
        except Exception as exc:
            # 采集永不冒泡（AC-18）：任何未预期异常都降级为 network 错误码。
            logger.error(
                "[codex-reset][client] endpoint=%s 未预期异常 %s: %s",
                endpoint,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return FetchResult(
                endpoint=endpoint,
                ok=False,
                status=0,
                error_code=ErrorCode.NETWORK,
                fetched_at=self._clock.utcnow(),
                latency_ms=self._elapsed_ms(started),
            )


__all__ = ["DEFAULT_BASE_URL", "ENDPOINTS", "SourceClient", "parse_retry_after"]
