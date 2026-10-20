"""Deployment regressions absent from standalone kernel tests."""

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import RecordingSend, create_subscription, make_worker, open_repo, run, utc

from services.clock import FakeClock
from services.models import GroupOverride, LogicalEvent


def test_package_import_does_not_require_global_services(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = f"""
import importlib, pathlib, sys, types
root = pathlib.Path({str(root)!r})
sys.path.insert(0, str(root / 'tests'))
from test_qa_task_lifecycle import _install_astrbot_stub
_install_astrbot_stub()
sys.path[:] = [p for p in sys.path if pathlib.Path(p or '.').resolve() != root]
for name in list(sys.modules):
    if name == 'services' or name.startswith('services.'):
        del sys.modules[name]
package = types.ModuleType('acceptance_plugin')
package.__path__ = [str(root)]
sys.modules['acceptance_plugin'] = package
module = importlib.import_module('acceptance_plugin.main')
assert module.CodexResetRadar.__module__ == 'acceptance_plugin.main'
assert 'services' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("reset,credits", [(True, False), (False, True), (False, False)])
def test_queued_catchup_rechecks_each_category(tmp_path, reset, credits):
    async def scenario():
        clock = FakeClock(utc())
        repo = await open_repo(tmp_path, clock=clock)
        try:
            sub = await create_subscription(repo, umo="aiocqhttp:GroupMessage:1001", group_id="1001")
            events = [
                {
                    "logical_id": kind,
                    "kind": kind,
                    "group_kind": kind,
                    "state_version": "available",
                    "occurred_at": clock.utcnow().isoformat(),
                    "payload": {"state": "available"},
                }
                for kind in ("reset", "credits")
            ]
            event = LogicalEvent(
                logical_id="catchup:test", kind="catchup", group_kind="catchup",
                state_version="summary", occurred_at=clock.utcnow(), payload={"events": events},
            )
            await repo.enqueue_delivery(sub=sub, ev=event)
            await repo.update_subscription_override(
                sub.umo, GroupOverride(enable_confirmed_reset=reset, enable_credits=credits)
            )
            send = RecordingSend()
            await make_worker(repo, send, clock).run_once(clock.utcnow())
            if not reset and not credits:
                assert not send.texts
            else:
                assert len(send.texts) == 1
                assert "共 1 条更新" in send.texts[0]
                assert ("Codex 全球重置（网站确认记录）" in send.texts[0]) is reset
                assert ("额外重置权益：" in send.texts[0]) is credits
        finally:
            await repo.close()

    run(scenario())
