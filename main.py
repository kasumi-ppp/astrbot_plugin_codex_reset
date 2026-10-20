"""AstrBot 插件入口：注册、配置注入、命令适配、后台任务编排。

**唯一允许 `import astrbot` 的文件**（硬约束：`services/**` 必须零 AstrBot 依赖）。
本文件只做「适配」，不含任何判定逻辑：
- `AstrBotConfig` → `PluginSettings`（`services.config`）
- `AstrMessageEvent` → `CommandRequest`（`services.models`）
- `CommandReply.text` → `event.plain_result`
- `SendFn` → `context.send_message(umo, MessageChain)`
- WebUI `group_overrides` 卡片 ↔ `subscriptions.settings_json` 双向同步

命令注册说明（重要）：AstrBot 4.29 的 `filter.command_group` 在裸 `/cr` 时会抛
「参数不足」并打印命令树，无法实现「`/cr` 等同 `/cr status`」。因此这里用**单条指令
+ `GreedyStr` 手工分派**（仍是官方 parser，不做正则截取群聊文本）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, register

from services.classifier import EventClassifier, ForecastEvaluator
from services.client import SourceClient
from services.clock import RealClock
from services.commands import CommandService
from services.config import (
    build_plugin_settings,
    override_from_card,
    override_to_card,
    parse_timezone,
)
from services.errors import SchemaTooNewError
from services.models import CommandRequest, PluginSettings, SubscriptionRow
from services.notifications import DeliveryWorker
from services.pipeline import NullPipelineHooks, Pipeline, PipelineHooks
from services.repository import Repository
from services.scheduler import SourceScheduler

try:  # AstrBot ≥ 4.5 提供数据目录工具；旧版本回落到工作目录
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 仅在旧版 AstrBot 触发

    def get_astrbot_data_path() -> str:
        """旧版本兜底：使用当前工作目录下的 `data/`。"""
        return str(Path.cwd() / "data")


try:  # GreedyStr 在 4.x 的导出位置前后有调整，两处都尝试
    from astrbot.api.event.filter import GreedyStr
except ImportError:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.star.filter.command import GreedyStr

try:  # 仅用于类型标注，运行期允许缺失
    from astrbot.core.config.astrbot_config import AstrBotConfig
except Exception:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore[misc,assignment]


PLUGIN_NAME = "astrbot_plugin_codex_reset"
PLUGIN_VERSION = "1.2.0"
PLUGIN_AUTHOR = "kasumi-ppp"
PLUGIN_REPO = "https://github.com/kasumi-ppp/astrbot_plugin_codex_reset"
PLUGIN_DESC = "读取 codex-reset.com 公开数据，向订阅群推送重置确认、预告、概率与权益动态"

DB_FILENAME = "codex_reset.db"
AUDIT_DIRNAME = "audit"
POLL_TICK_SECONDS = 5.0

HEARTBEAT_INTERVAL_SECONDS = 30.0
"""常驻心跳的最小写入间隔（判据阈值 `2×spacing` 最少 120s，30s 足够且不制造 WAL 写放大）。"""

RUNTIME_OWNER_KEY = "runtime:owner"
"""当前持锁实例标识（`pid:8位随机`，架构 §A.4 软单实例）。"""

RUNTIME_HEARTBEAT_KEY = "runtime:heartbeat_at"
"""最近一次采集轮次时间（心跳）；用于判断「他实例是否仍在运行」。"""

RUNTIME_SHUTDOWN_KEY = "runtime:shutdown_at"
"""最近一次干净退出的记录，值格式 `"{owner}|{iso}"`（**必须带身份**）。

用途：插件热重载/重启时心跳可能仍在 `2×spacing` 内，若只按心跳判定会把
「刚退出的自己」误认成「另一个活实例」而永久进入只读模式。退出时写该键，
启动时若「标记里的 owner == 记录中的 owner」且 `shutdown_at >= heartbeat_at`，
说明**那个持锁实例**是干净退出，可直接接管。

为什么必须带 owner（P1-04）：只读实例退出时也会写标记。若标记不带身份，
第三实例会把「只读实例的退出」误当成「持锁实例的干净退出」而在 owner 仍活着时
抢锁，两个实例同时可写。带身份后，只有「记录里的 owner 自己写的退出标记」才算数。
"""

HEALTH_KEY_PREFIX = "health:"
"""端点健康 meta 键前缀（`health:{endpoint}` → `{"error_code": ..., "at": ...}`）。"""

SHORT_NAME_HINT = "提示：/cr 可能被其他插件占用，如无响应请改用 /codexreset。"
"""短名冲突提示（`/cr help` 末尾追加）。"""


def _card_version(card: dict[str, Any]) -> int:
    """卡片版本号：缺失/非法（含 `bool`）一律视为 -1（不参与版本比对）。

    `bool` 是 `int` 的子类，`_version: true` 若被当成 1 会与真实版本混用，故显式排除。
    """
    value = card.get("_version")
    if isinstance(value, bool) or not isinstance(value, int):
        return -1
    return value


def _parse_iso(text: Any) -> datetime | None:
    """解析 meta 里的 ISO 时间；非法返回 None（心跳判据宁松不严）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _split_shutdown(value: Any) -> tuple[str | None, datetime | None]:
    """解析 `runtime:shutdown_at`（`"{owner}|{iso}"`）→ `(owner, 时间)`。

    旧格式（纯 ISO，无身份）一律返回 `(None, None)`：**不认**为干净退出（fail-closed）。
    否则「只读实例写的标记」会被当成「持锁实例干净退出」，在 owner 仍活着时放行第二个写者（P1-04）。
    """
    if not isinstance(value, str) or "|" not in value:
        return None, None
    owner, _, stamp = value.partition("|")
    if not owner.strip() or not stamp.strip():
        return None, None
    return owner.strip(), _parse_iso(stamp)


class _PluginHooks(NullPipelineHooks):
    """把轮询结果旁路到 `main.py`（心跳写入 + 结构化日志）。"""

    def __init__(self, plugin: CodexResetRadar) -> None:
        """仅持有插件引用，不缓存任何状态。"""
        self._plugin = plugin

    async def on_round(self, outcome: Any) -> None:
        """每轮结束写心跳与端点健康，便于现场确认采集仍存活。"""
        plugin = self._plugin
        plugin.last_round_at = plugin.clock.utcnow()
        at_text = plugin.last_round_at.isoformat()
        try:
            await plugin.repo.meta_set(RUNTIME_HEARTBEAT_KEY, at_text)
        except Exception:  # 心跳失败不影响采集
            logger.warning("[codex-reset][main] 心跳写入失败", exc_info=True)
        try:
            # P2-07：端点健康（`/cr status` 与现场排查用它区分「端点失败」与「数据陈旧」）
            await plugin.repo.meta_set(
                f"{HEALTH_KEY_PREFIX}{outcome.endpoint}",
                json.dumps(
                    {"error_code": outcome.error_code, "at": at_text}, ensure_ascii=False
                ),
            )
        except Exception:  # 健康位写入失败同样不影响采集
            logger.warning("[codex-reset][main] 端点健康写入失败", exc_info=True)
        logger.info(
            "[codex-reset][main] round endpoint=%s error=%s judged=%s deliveries=%s",
            outcome.endpoint,
            outcome.error_code,
            int(outcome.judged),
            outcome.deliveries_created,
        )

    async def on_monitor_disabled(self) -> None:
        """`enable_monitor=false`：本轮不采集（不产生日志噪音）。"""
        logger.debug("[codex-reset][main] enable_monitor=false，跳过本轮采集")


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_VERSION,
    repo=PLUGIN_REPO,
)
class CodexResetRadar(Star):
    """Codex 重置雷达插件主体（Star 子类）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        """构造时只保存配置与基础设施句柄，不做 I/O（真正的初始化在 `initialize`）。"""
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self.clock = RealClock()
        self.last_round_at: datetime | None = None
        self._plugin_settings: PluginSettings = build_plugin_settings(self.config)
        self._tz = parse_timezone(self._plugin_settings.time_zone)[0]
        self._tasks: list[asyncio.Task[Any]] = []
        self._repo: Repository | None = None
        self._client: SourceClient | None = None
        self._scheduler: SourceScheduler | None = None
        self._pipeline: Pipeline | None = None
        self._worker: DeliveryWorker | None = None
        self._commands: CommandService | None = None
        self._readonly = False
        self._stopping = False
        self._short_name_conflict = False
        self._owner: str | None = None
        self._owner_candidate: str | None = None
        self._released = False
        self._marker_written = False
        self._last_heartbeat_at: datetime | None = None
        self._stop_error = False
        self._poll_task: asyncio.Task[Any] | None = None
        self._config_digest: str | None = None

    # ------------------------------------------------------------------
    # 路径 / 配置辅助
    # ------------------------------------------------------------------
    @property
    def plugin_name(self) -> str:
        """插件唯一名（`self.name` 由 AstrBot 注入；缺失时回退常量）。"""
        raw = getattr(self, "name", "") or ""
        text = str(raw).strip()
        return text or PLUGIN_NAME

    @property
    def data_dir(self) -> Path:
        """插件私有数据目录（**必须**位于持久化卷内）。"""
        return Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name

    @property
    def db_path(self) -> Path:
        """SQLite 数据库文件路径。"""
        return self.data_dir / DB_FILENAME

    @property
    def audit_dir(self) -> Path:
        """审计 JSONL 目录。"""
        return self.data_dir / AUDIT_DIRNAME

    @property
    def repo(self) -> Repository:
        """已打开的仓库（未初始化时抛 `RuntimeError`，避免静默空指针）。"""
        if self._repo is None:
            raise RuntimeError("Repository 尚未初始化")
        return self._repo

    @property
    def pipeline(self) -> Pipeline:
        """处理链实例。"""
        if self._pipeline is None:
            raise RuntimeError("Pipeline 尚未初始化")
        return self._pipeline

    def current_settings(self) -> PluginSettings:
        """当前生效的全局配置（供 Worker / Commands 动态读取）。"""
        return self._plugin_settings

    def current_timezone(self) -> Any:
        """当前生效时区。"""
        return self._tz

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """建库、恢复缓存、同步卡片、启动两个后台任务（幂等）。"""
        if self._tasks:
            logger.info("[codex-reset][main] initialize 已执行，跳过重复启动")
            return

        settings = build_plugin_settings(self.config)
        self._plugin_settings = settings
        self._tz = parse_timezone(settings.time_zone)[0]
        for warning in settings.warnings:
            logger.warning("[codex-reset][main] 配置告警：%s", warning)
        if not settings.admin_only_subscription:
            # 观察项：V1.2 仍只放行 `is_admin`，该字段只是预留位，避免管理员误以为已放宽
            logger.warning(
                "[codex-reset][main] admin_only_subscription=false 在 V1.2 仅为预留字段，不会放宽权限"
            )

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        repo = Repository(self.db_path, clock=self.clock, audit_dir=self.audit_dir)
        try:
            await repo.open()
        except SchemaTooNewError as exc:
            logger.error(
                "[codex-reset][main] 数据库 schema 高于插件支持版本，进入只读模式：%s", exc
            )
            self._readonly = True
        except Exception:
            logger.error("[codex-reset][main] 数据库打开失败，插件将不可用", exc_info=True)
            self._readonly = True
        self._repo = repo

        if not self._readonly:
            try:
                await self._claim_runtime_owner()
            except Exception:  # pragma: no cover - 检测失败不应阻断启动
                logger.warning("[codex-reset][main] 单实例检测失败（继续正常启动）", exc_info=True)

        self._client = SourceClient(clock=self.clock)
        self._scheduler = SourceScheduler(
            spacing_seconds=self._plugin_settings.request_spacing_seconds, clock=self.clock
        )
        classifier = EventClassifier(clock=self.clock)
        evaluator = ForecastEvaluator(
            time_zone=self._tz,
            confirm_samples=self._plugin_settings.forecast_confirm_samples,
            cooldown_hours=self._plugin_settings.forecast_cooldown_hours,
            daily_max=self._plugin_settings.forecast_daily_max_per_group,
        )
        self._worker = DeliveryWorker(
            repo=repo,
            send=self._send,
            clock=self.clock,
            settings_provider=self.current_settings,
            tz_provider=self.current_timezone,
        )
        hooks: PipelineHooks = _PluginHooks(self)
        self._pipeline = Pipeline(
            repo=repo,
            client=self._client,
            scheduler=self._scheduler,
            classifier=classifier,
            evaluator=evaluator,
            worker=self._worker,
            clock=self.clock,
            settings_provider=self.current_settings,
            tz_provider=self.current_timezone,
            hooks=hooks,
        )
        self._commands = CommandService(
            repo=repo,
            read=self._pipeline,
            settings_provider=self.current_settings,
            sink=self,
            clock=self.clock,
            tz_provider=self.current_timezone,
            readonly_provider=lambda: self._readonly,
        )

        if not self._readonly:
            try:
                await self._pipeline.bootstrap(self.clock.utcnow())
            except Exception:
                logger.error("[codex-reset][main] bootstrap 失败（继续以空缓存运行）", exc_info=True)
        await self._apply_config_cards()
        self._detect_short_name_conflict()

        # 先记录一次文件摘要，避免自己的写回被当成「外部改动」而触发无意义的热更新
        self._config_digest = self._digest_of(self._config_path())

        if self._plugin_settings.enable_monitor and not self._readonly:
            self._start_poll_task()
        else:
            logger.info(
                "[codex-reset][main] 不启动采集任务（enable_monitor=%s readonly=%s）",
                self._plugin_settings.enable_monitor,
                self._readonly,
            )
        if self._readonly:
            # 只读实例（schema 过新 / 另一实例持锁）不投递：投递也是写路径
            logger.warning("[codex-reset][main] 只读模式：不启动投递任务")
        else:
            self._tasks.append(
                asyncio.create_task(self._delivery_loop(), name="codex-reset-delivery")
            )
        # 配置热更新任务**始终**创建（即使 enable_monitor=false）：
        # 否则「先关监控、之后在 WebUI 打开」要重启才生效（PRD §5）。
        self._tasks.append(
            asyncio.create_task(self._config_watch_loop(), name="codex-reset-config-watch")
        )
        logger.info(
            "[codex-reset][main] 初始化完成 data_dir=%s tasks=%s version=%s readonly=%s",
            self.data_dir,
            len(self._tasks),
            PLUGIN_VERSION,
            self._readonly,
        )

    async def terminate(self) -> None:
        """停 Worker、取消任务、关 session、关数据库（幂等、无孤儿任务）。

        **释放运行权的两步（写 `runtime:shutdown_at` + 关库）放在 `finally`**：
        任何一步抛异常都不能跳过它们，否则会留下「心跳新鲜 + 无退出标记」的残局，
        下一次启动会把自己误判成「仍在运行的另一个实例」而永久只读（P1-03）。
        """
        self._stopping = True
        tasks: list[asyncio.Task[Any]] = []
        try:
            if self._worker is not None:
                try:
                    self._worker.stop()
                except Exception:
                    # 不敢静默：`stop()` 若将来带上 flush/落盘语义，这里就是唯一线索
                    logger.error("[codex-reset][main] worker.stop 异常", exc_info=True)
                    self._stop_error = True
            self._poll_task = None
            tasks = list(self._tasks)
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:  # pragma: no cover - 关闭异常不应阻塞退出
                    logger.warning("[codex-reset][main] client.close 异常", exc_info=True)
        finally:
            await self._release_runtime()
        logger.info("[codex-reset][main] 已停止 tasks=%s", len(tasks))

    async def _release_runtime(self) -> None:
        """写干净退出记录并关闭数据库（`terminate` 的 `finally` 分支，幂等）。

        记录值带 owner 身份：只有「记录里的 owner 自己写的退出标记」才允许下一次启动
        据此接管，避免只读实例的退出把持锁实例「洗白」成已退出（P1-04）。

        `_released` **只在 `close()` 真正成功后才置位**：若关闭失败还标记成「已释放」，
        第二次 `terminate` 会直接 return，aiosqlite 连接（非 daemon 线程）就只能等到解释器
        退出时才回收，可能拖住进程退出；标记本身用 `_marker_written` 单独去重，避免重复告警。
        """
        repo = self._repo
        if repo is None or self._released:  # pragma: no cover - 初始化失败 / 已释放
            return
        identity = self._owner or self._owner_candidate
        if identity is None:  # pragma: no cover - 理论上不可达
            identity = f"{os.getpid()}:unknown"
        if not self._marker_written:
            try:
                await repo.meta_set(
                    RUNTIME_SHUTDOWN_KEY, f"{identity}|{self.clock.utcnow().isoformat()}"
                )
                self._marker_written = True
            except Exception:  # pragma: no cover - 退出路径不因写标记失败而中断
                logger.warning("[codex-reset][main] 写入 shutdown 标记失败", exc_info=True)
        try:
            await repo.close()
        except Exception:  # pragma: no cover - 允许下一次 terminate 重试关闭
            logger.warning("[codex-reset][main] repo.close 异常", exc_info=True)
        else:
            self._released = True

    # ------------------------------------------------------------------
    # 后台循环
    # ------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        """采集循环：异常一律捕获，绝不冒泡到 AstrBot 事件循环。"""
        while not self._stopping:
            try:
                await self._pipeline.poll_round(self.clock.utcnow())
            except asyncio.CancelledError:  # pragma: no cover - 上层取消
                raise
            except Exception:
                logger.error("[codex-reset][main] 采集轮次失败", exc_info=True)
            try:
                await asyncio.sleep(POLL_TICK_SECONDS)
            except asyncio.CancelledError:  # pragma: no cover
                raise

    async def _delivery_loop(self) -> None:
        """投递循环（`DeliveryWorker.run` 自带兜底与 5s 间隔）。"""
        if self._worker is None:  # pragma: no cover - initialize 保证非空
            return
        try:
            await self._worker.run()
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:  # pragma: no cover
            logger.error("[codex-reset][main] 投递循环异常退出", exc_info=True)

    def _start_poll_task(self) -> None:
        """启动采集任务（已在运行则跳过，保证幂等）。"""
        if self._poll_task is not None and not self._poll_task.done():
            return
        task = asyncio.create_task(self._poll_loop(), name="codex-reset-poll")
        self._poll_task = task
        self._tasks.append(task)

    async def _cancel_poll_task(self) -> None:
        """取消采集任务并等待其真正退出（避免残留任务/重复启动）。"""
        task = self._poll_task
        self._poll_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if task in self._tasks:
            self._tasks.remove(task)

    # ------------------------------------------------------------------
    # 软单实例（P2-07）
    # ------------------------------------------------------------------
    async def _claim_runtime_owner(self) -> None:
        """写 `runtime:owner`；若发现「他实例持锁且心跳新鲜」则转入只读采集模式。

        判定阈值取 `2 × request_spacing_seconds`（至少两个采集周期）：正常运行的实例
        每轮都会刷新 `runtime:heartbeat_at`，因此「心跳在两轮内更新过」= 对方还活着。
        只读模式只关闭**写入**路径（不写事件、不投递、不执行写指令），`/cr status` 等读取仍完全可用。

        「读 owner/心跳/退出标记 → 判定 → 写 owner」必须在**同一个** `BEGIN IMMEDIATE`
        事务内完成：分成多次 `meta_get` + `meta_set` 时，两个实例会各自读到「无主」快照、
        双双认定自己持锁（P2-11）。
        """
        repo = self._repo
        if repo is None:  # pragma: no cover - 调用点保证已初始化
            return
        owner = f"{os.getpid()}:{uuid4().hex[:8]}"
        self._owner_candidate = owner
        spacing = max(1, int(self._plugin_settings.request_spacing_seconds))
        threshold = 2 * spacing

        async with repo.atomic_meta() as tx:
            previous = await tx.get(RUNTIME_OWNER_KEY)
            heartbeat = await tx.get(RUNTIME_HEARTBEAT_KEY)
            shutdown_at = await tx.get(RUNTIME_SHUTDOWN_KEY)

            if previous and previous != owner:
                last = _parse_iso(heartbeat)
                clean_owner, clean_exit = _split_shutdown(shutdown_at)
                same_process = previous.split(":", 1)[0] == str(os.getpid())
                if same_process:
                    # 同进程内的插件热重载（如 WebUI 保存配置后框架重载插件）
                    logger.info(
                        "[codex-reset][main] 同进程重新加载插件（owner=%s），直接接管运行权",
                        previous,
                    )
                elif (
                    clean_owner == previous
                    and last is not None
                    and clean_exit is not None
                    and clean_exit >= last
                ):
                    logger.info(
                        "[codex-reset][main] 上一实例已干净退出（%s），接管运行权", shutdown_at
                    )
                elif last is not None:
                    age = (self.clock.utcnow() - last).total_seconds()
                    if age < threshold:
                        logger.error(
                            "[codex-reset][main] 检测到另一实例正在运行（owner=%s，心跳 %.0fs 前，"
                            "阈值 %ss），本实例进入只读采集模式（不写事件、不投递）",
                            previous,
                            age,
                            threshold,
                        )
                        self._readonly = True
                        return
            self._owner = owner
            now_text = self.clock.utcnow().isoformat()
            await tx.set(RUNTIME_OWNER_KEY, owner)
            # 取得所有权时**同事务**写心跳：否则「owner 存在但心跳为空」会让下一个
            # 实例落进 fail-open 分支（无心跳 → 判定不了 → 直接抢锁），两实例同时可写（P2-12）
            await tx.set(RUNTIME_HEARTBEAT_KEY, now_text)
            self._last_heartbeat_at = self.clock.utcnow()
            # 清掉上一次的退出标记，避免下一次启动把它当成「干净退出」的依据
            await tx.delete(RUNTIME_SHUTDOWN_KEY)

    # ------------------------------------------------------------------
    # 配置热更新（PRD §5「配置更改经验证后立即生效」）
    # ------------------------------------------------------------------
    def _config_path(self) -> Path:
        """AstrBot 落盘的插件配置文件路径。"""
        return Path(get_astrbot_data_path()) / "config" / f"{self.plugin_name}_config.json"

    @staticmethod
    def _digest_of(path: Path) -> str | None:
        """文件内容 sha1（判断「外部改过配置」）；不存在/不可读返回 None。

        用内容摘要而非 mtime：`save_config()` 写回后 mtime 必变，用 mtime 会形成
        「自写 → 热更新 → 再自写」的回环；摘要比对天然免疫。
        """
        try:
            return hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError:
            return None

    async def _config_watch_loop(self) -> None:
        """轮询配置文件摘要，变化即热更新；顺带刷新常驻心跳。异常一律捕获，绝不冒泡。"""
        while not self._stopping:
            try:
                await self._touch_heartbeat()
            except asyncio.CancelledError:  # pragma: no cover - 上层取消
                raise
            except Exception:
                logger.warning("[codex-reset][main] 心跳刷新失败", exc_info=True)
            try:
                await self._reload_config_if_changed()
            except asyncio.CancelledError:  # pragma: no cover - 上层取消
                raise
            except Exception:
                logger.error("[codex-reset][main] 配置热更新失败", exc_info=True)
            try:
                await asyncio.sleep(POLL_TICK_SECONDS)
            except asyncio.CancelledError:  # pragma: no cover
                raise

    async def _touch_heartbeat(self) -> None:
        """常驻心跳：`enable_monitor=false`（无采集轮次）时也要证明本实例还活着。

        只由**持锁实例**写：只读实例写心跳等于对外宣称自己是 owner，会让真正的
        owner 在下一次判定时把自己看成「另一个实例」的竞争者（P1-04 的镜像问题）。

        写入频率被 `HEARTBEAT_INTERVAL_SECONDS` 节流（判据阈值是 `2×spacing`，最少 120s）：
        配置监听每 5s 一拍，若每拍都 `BEGIN IMMEDIATE` 写一次 meta，长期空转会产生无谓的
        WAL 写放大（P3-3）。
        """
        if self._readonly or self._owner is None or self._repo is None or self._released:
            return
        now = self.clock.utcnow()
        if self._last_heartbeat_at is not None:
            elapsed = (now - self._last_heartbeat_at).total_seconds()
            if elapsed < HEARTBEAT_INTERVAL_SECONDS:
                return
        await self._repo.meta_set(RUNTIME_HEARTBEAT_KEY, now.isoformat())
        self._last_heartbeat_at = now

    async def _reload_config_if_changed(self) -> None:
        """读文件 → 比对摘要 → 解析 → 应用（间隔 / 卡片 / 采集任务开关）。"""
        path = self._config_path()
        try:
            raw_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return
        digest = hashlib.sha1(raw_bytes).hexdigest()
        if digest == self._config_digest:
            return
        # 先记摘要：解析失败时不要每 5s 重试同一份坏文件、把日志刷满
        self._config_digest = digest

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("[codex-reset][main] 配置文件不是合法 JSON，忽略本次热更新")
            return
        if not isinstance(data, dict):
            logger.warning("[codex-reset][main] 配置文件根节点不是对象，忽略本次热更新")
            return

        previous = self._plugin_settings
        settings = build_plugin_settings(data)
        # 就地更新：保留 `AstrBotConfig` 的 save_config 能力（卡片投影还要写回）
        if isinstance(self.config, dict):
            self.config.clear()
            self.config.update(data)
        else:  # pragma: no cover - 极端配置对象
            self.config = data
        self._plugin_settings = settings
        self._tz = parse_timezone(settings.time_zone)[0]
        for warning in settings.warnings:
            logger.warning("[codex-reset][main] 配置告警：%s", warning)
        if not settings.admin_only_subscription:
            logger.warning(
                "[codex-reset][main] admin_only_subscription=false 在 V1.2 仅为预留字段，不会放宽权限"
            )
        if self._scheduler is not None:
            self._scheduler.set_spacing(settings.request_spacing_seconds)
        await self._apply_config_cards()
        await self._sync_poll_task(previous, settings)
        logger.info(
            "[codex-reset][main] 配置已热更新 spacing=%s monitor=%s tz=%s",
            settings.request_spacing_seconds,
            settings.enable_monitor,
            settings.time_zone,
        )

    async def _sync_poll_task(self, previous: PluginSettings, current: PluginSettings) -> None:
        """采集任务开关跟随 `enable_monitor`（false→true 启动，true→false 取消）。"""
        if current.enable_monitor == previous.enable_monitor:
            return
        if not current.enable_monitor:
            await self._cancel_poll_task()
            logger.warning("[codex-reset][main] enable_monitor 已关闭，采集任务已停止")
            return
        if self._readonly:
            logger.warning("[codex-reset][main] 只读采集模式下不启动采集任务")
            return
        self._start_poll_task()

    # ------------------------------------------------------------------
    # SendFn（唯一出站通道）
    # ------------------------------------------------------------------
    async def _send(self, umo: str, text: str, at_all: bool) -> bool:
        """把纯文本送到指定会话；超时原样抛出（Worker 判 uncertain）。"""
        try:
            chain = MessageChain()
            platform = umo.split(":", 1)[0] if umo else ""
            if at_all and platform == "aiocqhttp":
                chain.chain.append(At(qq="all"))
            chain.message(text)
            return bool(await self.context.send_message(umo, chain))
        except TimeoutError:
            raise
        except Exception:
            logger.error("[codex-reset][main] 发送失败 umo_hash=%s", _hash(umo), exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 命令注册（单指令 + GreedyStr 手工分派）
    # ------------------------------------------------------------------
    @filter.command("cr", alias={"codexreset"})
    async def cr(self, event: AstrMessageEvent, args: GreedyStr) -> Any:
        """`/cr` 与 `/codexreset` 等价。

        只用**一个** `GreedyStr` 参数接收全部剩余文本再自行拆分：
        AstrBot 的 `GreedyStr` 只有在「无默认值」时才会被识别为贪婪参数，
        若写成 `sub: str = "", rest: GreedyStr = ""`，`rest` 会被当成普通字符串，
        `/cr set p24 60` 的第三个参数会被框架丢弃。
        """
        try:
            parts = [part for part in str(args or "").split() if part]
            subcommand = parts[0].lower() if parts else "status"
            argv = tuple(parts[1:])
            request = self._build_request(event, subcommand, argv)
            if self._commands is None:
                yield event.plain_result("插件尚未初始化完成，请稍后重试。")
                return
            reply = await self._commands.handle(request)
            text = reply.text or ""
            if subcommand == "help" and self._short_name_conflict:
                text = f"{text}\n{SHORT_NAME_HINT}" if text else SHORT_NAME_HINT
            if text:
                yield event.plain_result(text)
        except Exception:
            logger.error("[codex-reset][main] 指令处理失败", exc_info=True)
            yield event.plain_result("指令处理失败，请查看日志")

    def _build_request(
        self, event: AstrMessageEvent, subcommand: str, args: tuple[str, ...]
    ) -> CommandRequest:
        """把 AstrBot 事件归一化为 `CommandRequest`（不做任何业务判断）。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform = str(getter() or "")
            except Exception:
                platform = ""
        group_id = ""
        group_getter = getattr(event, "get_group_id", None)
        if callable(group_getter):
            try:
                group_id = str(group_getter() or "")
            except Exception:
                group_id = ""
        sender_id = ""
        sender_getter = getattr(event, "get_sender_id", None)
        if callable(sender_getter):
            try:
                sender_id = str(sender_getter() or "")
            except Exception:
                sender_id = ""
        is_admin = False
        admin_getter = getattr(event, "is_admin", None)
        if callable(admin_getter):
            try:
                is_admin = bool(admin_getter())
            except Exception:
                is_admin = False
        return CommandRequest(
            umo=umo,
            is_group=bool(group_id),
            group_id=group_id,
            platform=platform,
            sender_id=sender_id,
            is_admin=is_admin,
            subcommand=subcommand,
            args=args,
            raw_text=str(getattr(event, "message_str", "") or ""),
            received_at=self.clock.utcnow(),
            request_id="",
            group_name=self._group_name(event),
            platform_id=platform,
        )

    @staticmethod
    def _group_name(event: AstrMessageEvent) -> str:
        """尽力从原始事件里取群名；取不到返回空串（调用方回退 `群 {group_id}`）。"""
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if raw is None:
            return ""
        direct = getattr(raw, "group_name", None)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        group = getattr(raw, "group", None)
        if isinstance(group, dict):
            name = group.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        nested = getattr(group, "name", None)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                value = getter("group_name")
            except Exception:
                return ""
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    # ------------------------------------------------------------------
    # 短名冲突检测
    # ------------------------------------------------------------------
    def _detect_short_name_conflict(self) -> None:
        """扫描 AstrBot 的 handler 注册表，判断 `/cr` 是否已被别的插件占用。"""
        self._short_name_conflict = False
        try:
            from astrbot.core.star import star_map as star_map_module
        except Exception:  # pragma: no cover - 版本差异
            return
        registry = getattr(star_map_module, "star_map", None)
        if not isinstance(registry, dict):
            return
        for module_key, meta in registry.items():
            if PLUGIN_NAME in str(module_key):
                continue
            try:
                names = list(self._command_names(meta))
            except Exception:  # pragma: no cover - 元数据结构差异
                continue
            if "cr" in names:
                self._short_name_conflict = True
                logger.warning(
                    "[codex-reset][main] /cr 可能被插件 %s 占用，请使用 /codexreset", module_key
                )
                return

    @classmethod
    def _command_names(cls, holder: Any) -> Any:
        """从任意 handler 容器里尽力抽取 `command_name`（找不到就产出空）。"""
        for attr in ("star_handlers", "handlers", "event_filters", "filters"):
            items = getattr(holder, attr, None)
            if not items:
                continue
            candidates = items if isinstance(items, list | tuple | set) else [items]
            for item in candidates:
                yield from cls._names_of(item)

    @classmethod
    def _names_of(cls, item: Any) -> Any:
        """从单个 handler / filter 上抽取命令名（含一层嵌套）。"""
        for attr in ("command_name", "command", "name"):
            value = getattr(item, attr, None)
            if isinstance(value, str) and value:
                yield value
        for attr in ("event_filters", "filters", "handler", "func", "command_filter"):
            nested = getattr(item, attr, None)
            if nested is None:
                continue
            for sub in nested if isinstance(nested, list | tuple | set) else [nested]:
                for inner in ("command_name", "command", "name"):
                    value = getattr(sub, inner, None)
                    if isinstance(value, str) and value:
                        yield value

    # ------------------------------------------------------------------
    # GroupConfigSink 实现（WebUI 卡片 ↔ DB）
    # ------------------------------------------------------------------
    async def sync_group(self, sub: SubscriptionRow) -> bool:
        """把订阅的稀疏覆盖写成 `group_overrides` 卡片（DB 为真源）。"""
        card = override_to_card(
            sub.override,
            self._plugin_settings,
            subscription_id=sub.subscription_id,
            group_name=sub.group_name or f"群 {sub.group_id}",
        )
        card["_version"] = int(sub.version)
        cards = self._config_cards()
        cards = [item for item in cards if item.get("subscription_id") != sub.subscription_id]
        cards.append(card)
        self._set_config("group_overrides", cards)
        self.save_config()
        return True

    async def drop_group(self, subscription_id: str) -> bool:
        """删除某订阅对应的卡片。"""
        cards = [
            item
            for item in self._config_cards()
            if item.get("subscription_id") != subscription_id
        ]
        self._set_config("group_overrides", cards)
        self.save_config()
        return True

    async def _apply_config_cards(self) -> None:
        """把 WebUI 卡片同步进 DB，并**按 DB 重新投影**一次。

        P2-06：判定顺序必须是「先按 `subscription_id` 分组 → 每组取 `_version` 最大的一张
        → 再与 DB version 比对」。旧实现把「重复 id」判定放在版本判定之前，于是一张过期卡
        会占住 `seen`，把同组里更新的卡当成重复丢弃 —— 这一轮任何覆盖都不生效。
        """
        if self._readonly:
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for card in self._config_cards():
            subscription_id = str(card.get("subscription_id") or "").strip()
            if not subscription_id:
                logger.warning("[codex-reset][main] 卡片缺少 subscription_id，已丢弃")
                continue
            grouped.setdefault(subscription_id, []).append(card)

        for subscription_id, group in grouped.items():
            if len(group) > 1:
                logger.warning(
                    "[codex-reset][main] 卡片重复 sub_id=%s count=%s，按 _version 取最新",
                    subscription_id,
                    len(group),
                )
            card = max(group, key=_card_version)
            card_version = _card_version(card)
            row = await self._repo.get_subscription_by_id(subscription_id) if self._repo else None
            if row is None:
                logger.warning(
                    "[codex-reset][main] 卡片对应订阅不存在 sub_id=%s，已丢弃", subscription_id
                )
                continue
            if card_version >= 0 and card_version != int(row.version):
                logger.warning(
                    "[codex-reset][main] 卡片版本过期 sub_id=%s card=%s db=%s，不覆盖数据库",
                    subscription_id,
                    card_version,
                    row.version,
                )
                continue
            override = override_from_card(card, self._plugin_settings)
            if override.as_sparse_dict() != row.override.as_sparse_dict():
                await self._repo.update_subscription_override(row.umo, override)
                logger.info(
                    "[codex-reset][main] 卡片覆盖写回 DB sub_id=%s fields=%s",
                    subscription_id,
                    sorted(override.as_sparse_dict()),
                )
        await self._project_cards()

    async def _project_cards(self) -> None:
        """按 DB 现有订阅重建卡片列表（保证卡片与库一致）。"""
        if self._readonly or self._repo is None:
            return
        rows = await self._repo.list_subscriptions()
        cards: list[dict[str, Any]] = []
        for row in rows:
            card = override_to_card(
                row.override,
                self._plugin_settings,
                subscription_id=row.subscription_id,
                group_name=row.group_name or f"群 {row.group_id}",
            )
            card["_version"] = int(row.version)
            cards.append(card)
        self._set_config("group_overrides", cards)
        self.save_config()

    def _config_cards(self) -> list[dict[str, Any]]:
        """读取 `group_overrides`（非法结构一律视为空列表）。"""
        raw = self._get_config("group_overrides", [])
        if not isinstance(raw, list):
            logger.warning("[codex-reset][main] group_overrides 不是列表，已重置为空")
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    def _get_config(self, key: str, default: Any) -> Any:
        """容错读取配置（`AstrBotConfig` 与普通 dict 皆可）。"""
        getter = getattr(self.config, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:  # pragma: no cover - 极端实现差异
                return default
        try:
            return self.config[key]  # type: ignore[index]
        except (KeyError, TypeError):
            return default

    def _set_config(self, key: str, value: Any) -> None:
        """容错写入配置（仅内存生效也可接受）。"""
        try:
            self.config[key] = value  # type: ignore[index]
        except TypeError:  # pragma: no cover - 只读配置对象
            logger.warning("[codex-reset][main] 配置不可写，%s 仅在内存生效", key)

    def save_config(self) -> None:
        """尽力持久化插件配置；`save_config()` 不存在时跳过（仅内存生效）。"""
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            logger.warning("[codex-reset][main] 当前配置对象不支持 save_config，改动仅内存生效")
            return
        try:
            saver()
        except AttributeError:  # pragma: no cover - 运行期缺失
            logger.warning("[codex-reset][main] save_config 缺失，改动仅内存生效")
            return
        except Exception:  # pragma: no cover - 磁盘/权限问题
            logger.error("[codex-reset][main] save_config 失败", exc_info=True)
            return
        # 记录写回后的内容摘要：否则热更新任务会把自己的写回误判成「外部改动」
        self._config_digest = self._digest_of(self._config_path())

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def status_snapshot(self, now: datetime) -> dict[str, Any]:
        """只读状态摘要（供 `main.py` 日志或未来 WebUI 使用）。"""
        if self._pipeline is None:
            return {"ready": False, "readonly": self._readonly}
        latest, stale = self._pipeline.freshness(now)
        return {
            "ready": True,
            "readonly": self._readonly,
            "stale": stale,
            "latest_fetched_at": None if latest is None else latest.isoformat(),
            "tasks": len(self._tasks),
            "groups": len(self._config_cards()),
        }


def _hash(value: str) -> str:
    """日志用 umo 指纹（不打完整 umo）。"""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


__all__ = ["CodexResetRadar", "PLUGIN_NAME", "PLUGIN_VERSION"]
