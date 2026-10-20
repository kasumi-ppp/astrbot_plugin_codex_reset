"""Run inside a real AstrBot environment; all state stays in a temporary directory.

Usage: python runtime_smoke.py /path/to/plugin
No live platform, upstream requests, or QQ messages are used.
"""

import asyncio
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path


async def smoke(plugin_root):
    package = types.ModuleType("codex_reset_acceptance")
    package.__path__ = [str(plugin_root)]
    sys.modules[package.__name__] = package
    module = importlib.import_module(f"{package.__name__}.main")

    from astrbot.core.star.filter.command import CommandFilter

    command = CommandFilter("cr", handler_md=types.SimpleNamespace(handler=module.CodexResetRadar.cr))
    empty = command.validate_and_convert_params([], command.handler_params)
    parsed = command.validate_and_convert_params(["set", "p24", "60"], command.handler_params)
    assert empty == {"args": ""}, empty
    assert parsed == {"args": "set p24 60"}, parsed

    class Context:
        async def send_message(self, *args):
            raise AssertionError("Smoke test must never send to a live platform")

    event = types.SimpleNamespace(
        unified_msg_origin="acceptance:GroupMessage:smoke",
        message_str="",
        get_group_id=lambda: "smoke",
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "acceptance",
        is_admin=lambda: True,
        plain_result=lambda text: text,
    )

    with tempfile.TemporaryDirectory(prefix="codex-reset-smoke-") as directory:
        module.get_astrbot_data_path = lambda: directory
        config = {"enable_monitor": False}
        plugin = module.CodexResetRadar(Context(), config)
        try:
            await plugin.initialize()
            assert not plugin._readonly
            for args in ("", "help", "subscribe", parsed["args"], "settings", "unsubscribe"):
                replies = [reply async for reply in plugin.cr(event, args)]
                assert replies and "数据来源：https://codex-reset.com/" in replies[0], replies
                assert "指令处理失败" not in replies[0], replies
            assert not await plugin.repo.list_subscriptions()
        finally:
            await plugin.terminate()
        restored = module.CodexResetRadar(Context(), config)
        try:
            await restored.initialize()
            assert not restored._readonly
        finally:
            await restored.terminate()
        remaining = [task.get_name() for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        assert not remaining, remaining
    print(json.dumps({"package_import": True, "real_parser": True, "commands": True,
                      "clean_reload": True, "orphan_tasks": 0, "qq_messages_sent": 0}))


if __name__ == "__main__":
    asyncio.run(smoke(Path(sys.argv[1]).resolve()))
