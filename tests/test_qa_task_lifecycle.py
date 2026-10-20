"""QA 独立回归 ⑤：后台任务生命周期 / 阻塞 / 适配层契约。

环境事实：本机**没有** `astrbot`，因此这里做两件事：

1. **静态审计** `main.py`（AST），把架构 §A.4 的硬约束变成可执行断言：
   `terminate` 必 `cancel()` + `gather()`、`initialize` 幂等、`_send` 兜底异常、
   `/cr` 的 `GreedyStr` 必须**无默认值**（否则框架会把第三参丢掉）；
2. **最小替身**注入 `sys.modules` 后真实实例化 `CodexResetRadar`，验证
   `initialize → terminate` 不残留 asyncio 任务、`_apply_config_cards` 的
   过期版本 / 未知 id / 重复 id 处理、`_send` 的 `Comp.At(qq="all")` 条件。

另外覆盖：`enable_monitor=false` 时 `poll_round` 必须直接返回 None 且不发请求。
"""

from __future__ import annotations

# ruff: noqa: I001 - conftest 属于测试基础设施，与 services 分块导入
import ast
import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock, run, utc

from services.notifications import DeliveryWorker
from services.models import CommandRequest
from qa_helpers import UMO, built_baseline, env, open_repo_or_fail

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "main.py"


# ---------------------------------------------------------------------------
# 静态审计
# ---------------------------------------------------------------------------
def _main_tree() -> ast.Module:
    return ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename="main.py")


def _plugin_class(tree: ast.Module) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CodexResetRadar":
            return node
    raise AssertionError("main.py 中找不到 CodexResetRadar 类")


def _method(class_node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"CodexResetRadar 缺少 async 方法 {name}")


def _calls(node: ast.AST) -> list[str]:
    """收集所有调用的「名字」：`f()` → f；`obj.m()` → m。"""
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.append(func.attr)
            elif isinstance(func, ast.Name):
                names.append(func.id)
    return names


def test_terminate_cancels_and_gathers_all_tasks():
    """架构 §A.4：`terminate()` 必须 `cancel()` 每个任务后再 `gather(..., return_exceptions=True)`。"""
    terminate = _method(_plugin_class(_main_tree()), "terminate")

    calls = _calls(terminate)
    assert "cancel" in calls, "terminate 必须取消后台任务"
    assert "gather" in calls, "terminate 必须等待任务结束（gather）"
    keywords = set()
    for child in ast.walk(terminate):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "gather":
            keywords |= {kw.arg for kw in child.keywords}
    assert "return_exceptions" in keywords, "gather 必须 return_exceptions=True，避免取消异常冒泡"


def test_initialize_is_idempotent():
    """架构 §A.4：`initialize()` 若已启动过任务必须直接返回（防热重载双启动）。"""
    initialize = _method(_plugin_class(_main_tree()), "initialize")

    guards = [
        node
        for node in ast.walk(initialize)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "_tasks"
        and any(isinstance(child, ast.Return) for child in node.body)
    ]
    assert guards, "initialize 缺少 `if self._tasks: return` 幂等判断"


def test_send_swallows_non_timeout_exceptions():
    """`_send` 必须返回 False（而非冒泡）来把异常收敛成可重试错误；TimeoutError 原样抛出。"""
    send = _method(_plugin_class(_main_tree()), "_send")
    handlers = [node for node in ast.walk(send) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "_send 缺少异常捕获"

    caught: list[str] = []
    for handler in handlers:
        node = handler.type
        if isinstance(node, ast.Name):
            caught.append(node.id)
        elif isinstance(node, ast.Tuple):
            caught += [item.id for item in node.elts if isinstance(item, ast.Name)]

    assert "Exception" in caught or not caught, "必须兜底 Exception"
    assert "TimeoutError" in caught, "TimeoutError 必须单独处理（交给 Worker 判 uncertain）"


def test_cr_command_uses_single_defaultless_greedy_argument():
    """`main.py` 注释里的关键约束：`GreedyStr` 只有在「无默认值」时才是贪婪参数。"""
    cr = _method(_plugin_class(_main_tree()), "cr")
    params = cr.args.args

    assert [param.arg for param in params] == ["self", "event", "args"], (
        "/cr 必须只用一个参数接收剩余文本，否则 `/cr set p24 60` 的第三个参数会被框架丢弃"
    )
    annotation = params[-1].annotation
    assert annotation is not None
    assert "GreedyStr" in ast.unparse(annotation)
    assert cr.args.defaults == [], "GreedyStr 带默认值会退化成普通字符串参数"
    assert not [value for value in cr.args.kw_defaults if value is not None]


def test_apply_config_cards_is_version_aware():
    """`_apply_config_cards` 必须读取卡片 `_version` 才能拒绝过期覆盖。"""
    apply_cards = _method(_plugin_class(_main_tree()), "_apply_config_cards")
    source = ast.unparse(apply_cards)

    assert "_version" in source
    assert "get_subscription_by_id" in source, "未知 subscription_id 必须先查库再丢弃"


def test_services_layer_never_imports_astrbot():
    """T1/T11 门禁：`services/**` 必须零 AstrBot 依赖（否则无法脱离框架测试）。"""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "services").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [alias.name for alias in node.names if alias.name.split(".")[0] == "astrbot"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "astrbot":
                offenders.append(node.module or "")
    assert offenders == [], f"services/ 出现 astrbot 依赖：{offenders}"


# ---------------------------------------------------------------------------
# Pipeline / Worker 行为（无需 astrbot）
# ---------------------------------------------------------------------------
def test_poll_round_returns_none_and_never_fetches_when_monitor_disabled(tmp_path):
    """`enable_monitor=false`：不采集、不发请求、返回 None（只读模式仍可查询）。"""
    from services.models import PluginSettings

    async def scenario():
        async with env(tmp_path, baseline=built_baseline()) as (_repo, bundle, clock):
            bundle.pipeline._settings_provider = lambda: PluginSettings(enable_monitor=False)  # noqa: SLF001
            clock.set_now(utc(2026, 10, 20, 12))
            outcome = await bundle.pipeline.poll_round(clock.utcnow())
            return outcome, list(bundle.client.calls), await bundle.pipeline.evaluate_all(clock.utcnow())

    outcome, calls, queued = run(scenario())

    assert outcome is None
    assert calls == [], "关闭监控后绝不允许触发上游请求"
    assert queued == 0


def test_worker_stop_is_idempotent():
    """`DeliveryWorker.stop()` 必须可重复调用（`terminate` 与异常路径都会调）。"""

    class _Repo:
        pass

    async def _send(umo: str, text: str, at_all: bool = False) -> bool:  # pragma: no cover
        return True

    worker = DeliveryWorker(
        repo=_Repo(),  # type: ignore[arg-type]
        send=_send,
        clock=types.SimpleNamespace(utcnow=lambda: utc(2026, 10, 20, 12)),
        settings_provider=lambda: None,  # type: ignore[arg-type]
        tz_provider=lambda: None,  # type: ignore[arg-type]
    )

    worker.stop()
    worker.stop()

    assert worker._stopped is True  # noqa: SLF001


def test_hung_send_should_become_uncertain_instead_of_stalling(tmp_path):
    """F08 / P1-02：发送超时必须落成 `uncertain`（不重发），而不是把整条投递链挂住。"""
    from zoneinfo import ZoneInfo

    from services.models import LogicalEvent, PluginSettings

    async def scenario():
        repo = await open_repo_or_fail(tmp_path)
        try:
            clock = FakeClock(utc(2026, 10, 20, 12))
            sub = await repo.upsert_subscription(
                umo=UMO,
                platform="aiocqhttp",
                group_id="1001",
                group_name="测试群",
                override=None,
            )
            event = LogicalEvent(
                logical_id="reset:2100000000000000001",
                kind="reset",
                group_kind="reset",
                state_version="confirmed",
                occurred_at=utc(2026, 10, 20, 11, 0),
                payload={"occurred_at": "2026-10-20T11:00:00.000Z"},
            )
            await repo.enqueue_delivery(sub=sub, ev=event)
            # 同一批里再放一条正常投递：超时不得影响后续 pending 被处理
            follow_up = LogicalEvent(
                logical_id="credits:2100000000000000002",
                kind="credits",
                group_kind="credits",
                state_version="available",
                occurred_at=utc(2026, 10, 20, 11, 5),
                payload={"state": "available", "occurred_at": "2026-10-20T11:05:00.000Z"},
            )
            await repo.enqueue_delivery(sub=sub, ev=follow_up)

            calls = {"count": 0}

            async def hang_first(umo: str, text: str, at_all: bool = False) -> bool:
                calls["count"] += 1
                if calls["count"] == 1:
                    await asyncio.Event().wait()  # 模拟适配器在第一条上永不返回
                return True

            worker = DeliveryWorker(
                repo=repo,
                send=hang_first,
                clock=clock,
                settings_provider=lambda: PluginSettings(),
                tz_provider=lambda: ZoneInfo("Asia/Shanghai"),
                send_timeout=0.05,
            )
            # 关键断言：run_once 必须在有界时间内自然返回，而不是被外层强杀
            await asyncio.wait_for(worker.run_once(clock.utcnow()), timeout=2)
            return await repo.list_deliveries()

        finally:
            await repo.close()

    rows = run(scenario())

    assert rows, "投递行必须仍然存在（不能被静默删除）"
    by_kind = {row.kind: row for row in rows}
    assert by_kind["reset"].status == "uncertain", (
        f"超时后应为 uncertain，实际 {by_kind['reset'].status}"
    )
    assert by_kind["credits"].status == "delivered", "超时的那一条不得阻断同批后续投递"


# ---------------------------------------------------------------------------
# AstrBot 最小替身：真实实例化插件
# ---------------------------------------------------------------------------
class FakeAt:
    """`Comp.At` 替身（只关心 `qq` 参数）。"""

    def __init__(self, qq: str = "all") -> None:
        self.qq = qq


class FakeMessageChain:
    """`MessageChain` 替身：`chain` 列表 + `message()`。"""

    def __init__(self) -> None:
        self.chain: list[Any] = []

    def message(self, text: str) -> FakeMessageChain:
        self.chain.append(text)
        return self


class FakeStar:
    """`Star` 替身：只保存 context。"""

    def __init__(self, context: Any) -> None:
        self.context = context


class FakeContext:
    """`Context` 替身：记录出站消息，可注入异常。"""

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.fail_with = fail_with

    async def send_message(self, umo: str, chain: Any) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((umo, chain))
        return True


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _identity_decorator(*_args: Any, **_kwargs: Any) -> Any:
    def wrap(target: Any) -> Any:
        return target

    return wrap


def _install_astrbot_stub() -> None:
    """把最小 astrbot 替身注入 sys.modules（仅本模块使用，不影响其它测试）。"""
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "__qa_stub__", False):
        return

    import logging

    astrbot = _module("astrbot")
    astrbot.__qa_stub__ = True  # type: ignore[attr-defined]

    api = _module("astrbot.api")
    api.logger = logging.getLogger("astrbot.api")  # type: ignore[attr-defined]
    astrbot.api = api  # type: ignore[attr-defined]

    event_filter = _module(
        "astrbot.api.event.filter",
        command=_identity_decorator,
        permission_type=_identity_decorator,
        GreedyStr=type("GreedyStr", (), {}),
    )
    event = _module(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        MessageChain=FakeMessageChain,
        filter=event_filter,
    )
    event_filter.parent = event  # type: ignore[attr-defined]
    api.event = event  # type: ignore[attr-defined]

    components = _module("astrbot.api.message_components", At=FakeAt)
    api.message_components = components  # type: ignore[attr-defined]

    star_module = _module(
        "astrbot.api.star",
        Context=FakeContext,
        Star=FakeStar,
        register=_identity_decorator,
    )
    api.star = star_module  # type: ignore[attr-defined]

    core = _module("astrbot.core")
    astrbot.core = core  # type: ignore[attr-defined]

    config_pkg = _module("astrbot.core.config")
    core.config = config_pkg  # type: ignore[attr-defined]
    _module("astrbot.core.config.astrbot_config", AstrBotConfig=dict)

    utils_pkg = _module("astrbot.core.utils")
    core.utils = utils_pkg  # type: ignore[attr-defined]
    _module(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: str(Path.cwd() / "data"),
    )

    core_star = _module("astrbot.core.star")
    core.star = core_star  # type: ignore[attr-defined]
    core_star.star_map = _module("astrbot.core.star.star_map", star_map={})  # type: ignore[attr-defined]


def _load_main(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> types.ModuleType:
    """加载 main.py，并把数据目录重定向到 tmp（避免写仓库/workdir）。"""
    _install_astrbot_stub()
    module = sys.modules.get("main") or importlib.import_module("main")
    monkeypatch.setattr(module, "get_astrbot_data_path", lambda: str(data_root))
    return module


def test_initialize_then_terminate_leaves_no_pending_tasks(tmp_path, monkeypatch):
    """架构 §A.4：`initialize` 启任务、`terminate` 全部收敛；两次调用均幂等。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await plugin.initialize()
        first_round = list(plugin._tasks)  # noqa: SLF001
        await plugin.initialize()  # 第二次必须直接返回
        second_round = list(plugin._tasks)  # noqa: SLF001

        await plugin.terminate()
        await plugin.terminate()  # 幂等

        current = asyncio.current_task()
        pending = {
            task for task in asyncio.all_tasks() if not task.done() and task is not current
        }
        return first_round, second_round, pending

    first_round, second_round, pending = run(scenario())

    assert len(first_round) == len(second_round), "initialize 必须幂等，不得重复建任务"
    assert first_round, "至少应启动投递任务"
    assert all(task.done() for task in first_round), "terminate 后不得残留未完成任务"
    assert pending == set(), f"terminate 后仍有未取消的任务：{pending}"


def test_send_builds_at_all_only_for_aiocqhttp(tmp_path, monkeypatch):
    """U-3/§J 修正②：`@全体` 只以 `Comp.At(qq="all")` 形式注入 aiocqhttp 会话。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        ctx = FakeContext()
        plugin = main.CodexResetRadar(ctx, {})
        result = await plugin._send("aiocqhttp:GroupMessage:1001", "hello", True)  # noqa: SLF001
        other = await plugin._send("telegram:GroupMessage:2002", "hello", True)  # noqa: SLF001
        no_at = await plugin._send("aiocqhttp:GroupMessage:1001", "hello", False)  # noqa: SLF001
        return result, other, no_at, ctx.sent

    result, other, no_at, sent = run(scenario())

    assert result is True and other is True and no_at is True
    first_chain = sent[0][1].chain
    assert isinstance(first_chain[0], FakeAt) and first_chain[0].qq == "all"
    assert first_chain[-1] == "hello"

    non_cqhttp = sent[1][1].chain
    assert not any(isinstance(item, FakeAt) for item in non_cqhttp), "非 aiocqhttp 平台不得注入 At"

    plain = sent[2][1].chain
    assert not any(isinstance(item, FakeAt) for item in plain), "allow_at_all=False 时不得注入 At"


def test_clean_shutdown_marker_allows_takeover_from_other_pid(tmp_path, monkeypatch):
    """只读误判回归：上一实例**干净退出**后，新 pid 必须能接管运行权。

    心跳在 `2×spacing` 内仍然新鲜（插件热重载 / 快速重启的常见情形），
    若只看心跳会把「刚退出的自己」当成「另一个活实例」而永久只读。
    `terminate()` 写入的 `runtime:shutdown_at` 必须优先于心跳判定。
    """
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        first = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await first.initialize()
        await first.repo.meta_set("runtime:heartbeat_at", first.clock.utcnow().isoformat())
        await first.terminate()  # 写 shutdown_at

        monkeypatch.setattr(main.os, "getpid", lambda: 999999)
        second = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await second.initialize()
        readonly = second._readonly  # noqa: SLF001
        has_delivery = any(task.get_name() == "codex-reset-delivery" for task in second._tasks)  # noqa: SLF001
        await second.terminate()
        return readonly, has_delivery

    readonly, has_delivery = run(scenario())

    assert readonly is False, "干净退出后不得进入只读模式"
    assert has_delivery is True, "接管成功后必须启动投递任务"


def test_live_other_instance_forces_readonly_without_delivery(tmp_path, monkeypatch):
    """软单实例：他实例心跳新鲜且未写退出标记 → 本实例只读，且不写任何状态。

    除了「不启动投递/采集任务」，还锁住三件事（P2-10/P2-11）：
    只读实例**不得**改写 owner、**不得**写 deliveries、**不得**执行写指令。
    """
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        first = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await first.initialize()
        await first.repo.meta_set("runtime:heartbeat_at", first.clock.utcnow().isoformat())
        # 模拟「对方仍在运行」：不调用 terminate，因此没有 shutdown_at
        owner_before = await first.repo.meta_get("runtime:owner")

        monkeypatch.setattr(main.os, "getpid", lambda: 999999)
        second = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await second.initialize()
        readonly = second._readonly  # noqa: SLF001
        names = [task.get_name() for task in second._tasks]  # noqa: SLF001
        owner_after = await second.repo.meta_get("runtime:owner")
        deliveries = await second.repo.count_deliveries()
        reply = await second._commands.handle(  # noqa: SLF001
            CommandRequest(
                umo="aiocqhttp:GroupMessage:7001",
                is_group=True,
                group_id="7001",
                platform="aiocqhttp",
                sender_id="1",
                is_admin=True,
                subcommand="subscribe",
                args=(),
                group_name="旁观群",
            )
        )
        await second.terminate()
        await first.terminate()
        return readonly, names, owner_before, owner_after, deliveries, reply

    readonly, names, owner_before, owner_after, deliveries, reply = run(scenario())

    assert readonly is True, "他实例心跳新鲜时必须进入只读模式"
    assert "codex-reset-delivery" not in names, "只读实例不得启动投递任务"
    assert "codex-reset-poll" not in names, "只读实例不得启动采集任务"
    assert owner_after == owner_before, "只读实例不得改写 runtime:owner"
    assert deliveries == 0, "只读实例不得写入任何投递记录"
    assert reply.error == "readonly", f"只读实例的写指令必须被拒绝：{reply.text}"


def test_send_maps_errors_and_reraises_timeout(tmp_path, monkeypatch):
    """`_send`：普通异常 → False（可重试）；TimeoutError → 原样抛出（由 Worker 判 uncertain）。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        failing = main.CodexResetRadar(FakeContext(fail_with=RuntimeError("boom")), {})
        soft = await failing._send("aiocqhttp:GroupMessage:1", "x", False)  # noqa: SLF001
        timeout = main.CodexResetRadar(FakeContext(fail_with=TimeoutError()), {})
        with pytest.raises(TimeoutError):
            await timeout._send("aiocqhttp:GroupMessage:1", "x", False)  # noqa: SLF001
        return soft

    assert run(scenario()) is False


def test_apply_config_cards_handles_stale_unknown_and_duplicate(tmp_path, monkeypatch):
    """WebUI 卡片同步：未知 id 丢弃、过期版本不覆盖、同 id 多卡按 `_version` 取最新。

    P2-06：版本判定与重复判定不得互相遮蔽 —— 过期卡在前、新卡在后时，新卡必须生效。
    """
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await plugin.initialize()
        repo = plugin.repo
        sub = await repo.upsert_subscription(
            umo=UMO,
            platform="aiocqhttp",
            group_id="1001",
            group_name="测试群",
            override=None,
        )
        sub_id = sub.subscription_id
        waybill = {}

        # A：未知 subscription_id → 丢弃，且不影响 DB
        plugin.config["group_overrides"] = [
            {"subscription_id": "sub:deadbeefdeadbeef", "enable_watch": False},
        ]
        await plugin._apply_config_cards()
        waybill["unknown"] = (await repo.get_subscription(UMO)).override.as_sparse_dict()

        # B：卡片版本落后于 DB（DB version=1）→ 不覆盖
        plugin.config["group_overrides"] = [
            {"subscription_id": sub_id, "_version": 0, "enable_watch": False},
        ]
        await plugin._apply_config_cards()
        waybill["stale"] = (await repo.get_subscription(UMO)).override.as_sparse_dict()

        # C：版本一致 → 正常写回，并投影出 _version=2 的卡片
        plugin.config["group_overrides"] = [
            {"subscription_id": sub_id, "_version": 1, "enable_credits": False},
        ]
        await plugin._apply_config_cards()
        waybill["fresh"] = (await repo.get_subscription(UMO)).override.as_sparse_dict()
        waybill["cards"] = [dict(card) for card in plugin.config["group_overrides"]]

        # D：同一轮出现两个同 id 且同版本的卡片 → 按 `_version` 取最大（并列取首个）
        plugin.config["group_overrides"] = [
            {"subscription_id": sub_id, "_version": 2, "enable_watch": False},
            {"subscription_id": sub_id, "_version": 2, "enable_credits": True},
        ]
        await plugin._apply_config_cards()
        waybill["duplicate"] = (await repo.get_subscription(UMO)).override.as_sparse_dict()

        # E：P2-06 回归 —— 过期卡在前、新卡在后，新卡必须生效（旧实现把新卡当「重复」丢弃）
        plugin.config["group_overrides"] = [
            {"subscription_id": sub_id, "_version": 2, "enable_watch": False},
            {"subscription_id": sub_id, "_version": 3, "enable_credits": False},
        ]
        await plugin._apply_config_cards()
        waybill["newest_wins"] = (await repo.get_subscription(UMO)).override.as_sparse_dict()
        waybill["final"] = await repo.get_subscription(UMO)
        await plugin.terminate()
        return waybill

    result = run(scenario())

    assert result["unknown"] == {}, "未知 id 不得改动任何订阅"
    assert result["stale"] == {}, "过期版本卡片不得覆盖数据库"
    assert result["fresh"] == {"enable_credits": False}
    assert len(result["cards"]) == 1, "投影后卡片必须与 DB 一致"
    assert result["cards"][0]["_version"] == 2
    duplicate = result["duplicate"]
    # 卡片被视为「该群的完整状态快照」：并列版本取首个卡片
    assert duplicate.get("enable_watch") is False, "并列版本应取首个卡片"
    assert "enable_credits" not in duplicate, "并列版本的第二张卡片不得生效"
    # P2-06：过期卡不得遮蔽同组更新的卡
    assert result["newest_wins"] == {"enable_credits": False}, (
        "同一 sub 的多张卡片必须按 _version 取最新，过期卡不能挡住新卡"
    )
    assert result["final"].version == 4


def test_status_snapshot_is_read_only(tmp_path, monkeypatch):
    """`status_snapshot` 只读：不建任务、不改库，且能报告 tasks/groups。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        before = plugin.status_snapshot(utc(2026, 10, 20, 12))
        await plugin.initialize()
        names = sorted(task.get_name() for task in plugin._tasks)  # noqa: SLF001
        snapshot = plugin.status_snapshot(utc(2026, 10, 20, 12))
        await plugin.terminate()
        return before, snapshot, names

    before, snapshot, names = run(scenario())

    assert before == {"ready": False, "readonly": False}
    assert snapshot["ready"] is True
    # 投递任务 + 配置热更新任务（后者即使 enable_monitor=false 也必须常驻）
    assert snapshot["tasks"] == 2
    assert names == ["codex-reset-config-watch", "codex-reset-delivery"]
    assert snapshot["groups"] == 0


def test_delivery_worker_type_is_used_by_plugin(tmp_path, monkeypatch):
    """装配检查：插件持有的 Worker 必须是 `services.DeliveryWorker`（而非自定义实现）。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await plugin.initialize()
        worker = plugin._worker  # noqa: SLF001
        await plugin.terminate()
        return worker

    assert isinstance(run(scenario()), DeliveryWorker)


# ---------------------------------------------------------------------------
# P1-03 / P2-11：退出标记必须写在 finally；抢占必须单事务
# ---------------------------------------------------------------------------
def test_terminate_writes_shutdown_marker_even_if_worker_stop_raises(tmp_path, monkeypatch):
    """P1-03：`terminate()` 任何一步失败都不得跳过「写退出标记 + 关库」。

    否则异常退出会留下「心跳新鲜 + 无退出标记」的残局：下一次启动会把它当成
    「另一个仍在运行的实例」而永久只读，只能手工删库才能恢复。
    """
    import sqlite3

    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await plugin.initialize()
        await plugin.repo.meta_set("runtime:heartbeat_at", plugin.clock.utcnow().isoformat())
        db_path = plugin.repo.db_path

        class ExplodingWorker:
            """`stop()` 必炸：模拟退出路径上的意外异常。"""

            def stop(self) -> None:
                raise RuntimeError("worker.stop 炸了")

            async def run(self) -> None:
                return None

        plugin._worker = ExplodingWorker()  # noqa: SLF001
        await plugin.terminate()
        return db_path

    db_path = run(scenario())

    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()

    assert "runtime:shutdown_at" in rows, "异常退出路径也必须写入干净退出标记"
    assert rows["runtime:shutdown_at"], "退出标记不能是空值"


def test_claim_runtime_owner_does_not_nest_meta_transactions(tmp_path, monkeypatch):
    """P2-11：抢占必须「读-判定-写」同事务，不得再走独立 `meta_set`。

    分步写会二次获取写锁并与判定脱钩：两个实例可能各自读到「无主」快照而双双持锁。
    这里把「禁止调用 `meta_set`/`meta_delete`」变成可执行约束 —— 一旦有人改回
    分步写法，本用例立即失败。
    """
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        plugin = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await plugin.initialize()

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("抢占不得使用 meta_set/meta_delete（会二次获取写锁、破坏原子性）")

        monkeypatch.setattr(plugin.repo, "meta_set", forbidden)
        monkeypatch.setattr(plugin.repo, "meta_delete", forbidden)
        await plugin._claim_runtime_owner()  # noqa: SLF001
        owner = await plugin.repo.meta_get("runtime:owner")
        readonly = plugin._readonly  # noqa: SLF001
        await plugin.terminate()  # 收尾：释放任务与数据库（meta_set 被替换，异常由 finally 兜住）
        return owner, readonly

    owner, readonly = run(scenario())

    assert owner is not None, "抢占成功后必须写入 owner"
    assert readonly is False, "无竞争时应拿到运行权"


def test_second_instance_cannot_overwrite_live_owner(tmp_path, monkeypatch):
    """P2-11（e）：后到实例看到活实例时，**不得**覆盖 owner（判定与写入原子）。"""
    main = _load_main(monkeypatch, tmp_path / "data")

    async def scenario():
        first = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await first.initialize()
        await first.repo.meta_set("runtime:heartbeat_at", first.clock.utcnow().isoformat())
        owner_before = await first.repo.meta_get("runtime:owner")

        monkeypatch.setattr(main.os, "getpid", lambda: 999999)
        second = main.CodexResetRadar(FakeContext(), {"enable_monitor": False})
        await second.initialize()
        owner_after = await second.repo.meta_get("runtime:owner")
        await second.terminate()
        await first.terminate()
        return owner_before, owner_after, second._readonly  # noqa: SLF001

    owner_before, owner_after, readonly = run(scenario())

    assert owner_before is not None
    assert readonly is True, "活实例持锁时后到者必须只读"
    assert owner_after == owner_before, "只读实例不得把 owner 改成自己"
