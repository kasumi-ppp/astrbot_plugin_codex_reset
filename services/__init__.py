"""Codex 重置雷达的纯 Python 内核层。

硬约束：本包内任何模块都不得依赖 AstrBot（astrbot 包），相关适配只允许出现在 `main.py`。
依赖方向严格单向：
`main → pipeline/commands → classifier/notifications/repository → parser/client/models/clock`。
"""

from __future__ import annotations

__all__ = [
    "classifier",
    "client",
    "clock",
    "config",
    "errors",
    "models",
    "notifications",
    "parser",
    "repository",
    "scheduler",
]

__version__ = "1.3.0"
