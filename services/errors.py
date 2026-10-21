"""错误码、异常类型与全局常量。

错误码在 `ErrorCode` 中集中定义（全小写下划线），跨模块只允许引用常量，禁止内联字符串字面量。
"""

from __future__ import annotations

USER_AGENT = "astrbot-plugin-codex-reset/1.3.0 (+https://github.com/kasumi-ppp/astrbot_plugin_codex_reset)"
"""采集请求必须携带的合规 UA（AC-19），全仓库唯一出处。"""

SITE_URL = "https://codex-reset.com/"
"""站点首页，用于消息署名（AC-20）。"""

SITE_BASE_URL = "https://codex-reset.com"
"""接口基地址（不带尾斜杠）。"""


class ErrorCode:
    """字符串错误码常量表（避免拼写漂移）。"""

    OK = "ok"
    NOT_MODIFIED = "not_modified"
    HTTP_4XX = "http_4xx"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_JSON = "invalid_json"
    SCHEMA_ERROR = "schema_error"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ADAPTER_EXCEPTION = "adapter_exception"
    INTERRUPTED = "interrupted"

    #: 视为「本轮采集成功（数据可用）」的错误码集合
    SUCCESS_CODES: frozenset[str] = frozenset({OK, NOT_MODIFIED})
    #: 视为「可重试的采集失败」的错误码集合（D-12：不推进任何基线）
    RETRYABLE_CODES: frozenset[str] = frozenset(
        {HTTP_429, HTTP_5XX, TIMEOUT, NETWORK, INVALID_JSON, SCHEMA_ERROR}
    )


ALL_ERROR_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.OK,
        ErrorCode.NOT_MODIFIED,
        ErrorCode.HTTP_4XX,
        ErrorCode.HTTP_429,
        ErrorCode.HTTP_5XX,
        ErrorCode.TIMEOUT,
        ErrorCode.NETWORK,
        ErrorCode.INVALID_JSON,
        ErrorCode.SCHEMA_ERROR,
        ErrorCode.ADAPTER_UNAVAILABLE,
        ErrorCode.ADAPTER_EXCEPTION,
        ErrorCode.INTERRUPTED,
    }
)


class PluginError(Exception):
    """插件内所有自定义异常的基类。"""

    def __init__(self, message: str, *, error_code: str = ErrorCode.SCHEMA_ERROR) -> None:
        super().__init__(message)
        self.error_code = error_code


class SchemaTooNewError(PluginError):
    """数据库 schema 版本高于本插件支持的版本（不允许降级写入）。"""

    def __init__(self, message: str, *, found_version: int | None = None) -> None:
        super().__init__(message, error_code=ErrorCode.SCHEMA_ERROR)
        self.found_version = found_version


class ConfigError(PluginError):
    """配置非法且无法安全回落时抛出（尽量回落 + warning，不轻易抛出）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ErrorCode.SCHEMA_ERROR)
