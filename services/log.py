"""Service-layer logger proxy configured by the plugin entrypoint."""

from __future__ import annotations

from typing import Any


class _NullLogger:
    """No-op logger used when services are imported without AstrBot."""

    def debug(self, *_args: object, **_kwargs: object) -> None:
        pass

    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass

    def error(self, *_args: object, **_kwargs: object) -> None:
        pass

    def exception(self, *_args: object, **_kwargs: object) -> None:
        pass


class _LoggerProxy:
    """Stable proxy whose target can be replaced after service modules are imported."""

    def __init__(self) -> None:
        self._target: Any = _NullLogger()

    def configure(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


logger = _LoggerProxy()


def set_logger(astrbot_logger: Any) -> None:
    """Inject AstrBot's logger without importing AstrBot from the service layer."""
    if astrbot_logger is None:
        raise TypeError("astrbot_logger must not be None")
    logger.configure(astrbot_logger)
