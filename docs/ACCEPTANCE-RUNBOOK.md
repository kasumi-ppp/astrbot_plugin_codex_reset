# 验收执行单（Runbook）· Codex 重置雷达 V1.2

> `docs/ACCEPTANCE.md` 是**覆盖矩阵**（有没有覆盖）；本文是**执行手册**（今天怎么敲、看到什么算过）。
> 二者不重复：本文不重抄 AC-01~24 场景，只给可粘贴命令、**逐字**期望文案与判定口径。
>
> **当前状态：全部判定项待填。** 编写本文时**无**真实 AstrBot、**无**真实 QQ 群；文中期望值来自源码逐条核对
> 与本机单测（`428 passed`），**不代表**任何 AC 已通过。按 PRD §3，模拟测试**不得**替代真实联调结论。
>
> 约定：`<AD>` = AstrBot 数据目录（Docker 常见 `/AstrBot/data`）；`<CT>` = 容器名（示例 `astrbot`）；
> `2001` = 你的测试群号。所有 SQL **只读**，**禁止**在生产库执行 `INSERT/UPDATE/DELETE`。

## 0. 前置检查（5 分钟）

| # | 检查项 | 怎么确认 | 不满足先修什么 |
| --- | --- | --- | --- |
| 0.1 | AstrBot `>=4.16,<5` | `docker logs <CT> 2>&1 \| head -30`；或 WebUI「关于」 | 升级镜像。低于 4.16 时 `GreedyStr` 分派异常，`/cr set p24 60` 第三参会被框架丢弃 |
| 0.2 | 平台为 `aiocqhttp`（NapCat/OneBot v11） | WebUI「消息平台」适配器类型 | `metadata.yaml` 只声明 `aiocqhttp`；换平台则 `umo` 平台段不是 `aiocqhttp`，`@全体` 永不生效（§F-2） |
| 0.3 | 插件目录名恰为 `astrbot_plugin_codex_reset` | `docker exec <CT> ls <AD>/plugins` | 改名会连带 `<AD>/plugin_data/<目录名>` 变更，历史订阅/去重状态「看起来丢了」 |
| 0.4 | WebUI 已启用且显示 `v1.2.0` | WebUI「插件」页 | 未启用则 `initialize()` 不跑，所有指令回「插件尚未初始化完成，请稍后重试。」 |
| 0.5 | `<AD>/plugin_data` 在**持久卷** | `docker inspect <CT> --format '{{json .Mounts}}'` | 未持久化 → 重建即丢订阅与去重态，AC-16/17 无法判定 |
| 0.6 | 数据目录已生成 | `docker exec <CT> ls -l <AD>/plugin_data/astrbot_plugin_codex_reset` | 应有 `codex_reset.db`(+`-wal`/`-shm`) 与 `audit/`；缺则看日志 `[codex-reset][main] 初始化` |
| 0.7 | 1 个测试群 + 1 个**非管理员**成员 + 1 个 AstrBot 管理员 | 管理员判定是 `event.is_admin()`，**与 QQ 群主/群管无关** | 全是管理员则先加普通成员小号，否则 AC-15 无法判定 |
| 0.8 | 时区 | `docker exec <CT> cat <AD>/config/astrbot_plugin_codex_reset_config.json` 看 `time_zone` | 默认 `Asia/Shanghai`；改后**不必重启**（≤5s 热更新）；非法值回落并告警 |
| 0.9 | `enable_monitor=true` | 配置文件，或 `/cr status` 首行「监控开关：开启」 | 为 false 时不采集、零通知（但**仍**消化已有投递队列） |

**期望**：0.1~0.9 全绿。 **判定**：任一不绿 → 后续项记 `BLOCKED`，不得跳过前置直接判 AC。

**一次性准备（只读查询助手，全文复用）**：

```bash
mkdir -p ev && cat > ev/dbq.py <<'PY'
import sqlite3, sys
p = "/AstrBot/data/plugin_data/astrbot_plugin_codex_reset/codex_reset.db"
con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
for s in sys.argv[1:] or ["SELECT key,value,updated_at FROM meta ORDER BY key"]:
    print(">>", s)
    for r in con.execute(s):
        print(tuple(r))
PY
# 用法：docker exec -i astrbot python3 - "SELECT ..." < ev/dbq.py
```

> 容器内若无 `python3` 则换成 `python`（`docker exec <CT> sh -lc 'command -v python3 || command -v python'`）。
> 若报 `attempt to write a readonly database`（WAL 只读限制），改用 §D-1 的 `docker cp` 副本方案。

## A. 冒烟（3 条，2 分钟）

> ⚠️ **限频陷阱**：裸 `/cr` 被规整为 `status`，与 `/cr status` **同一个限频键**（15s）。A-1 与 A-3 必须间隔 ≥15s，
> 否则会看到 `请等待 15 秒后再试。`——那是限频不是故障。`/cr help` 是独立键，可立即发。

**A-1 `/cr`**
- 操作：群里发 `/cr`。
- 期望：首行 `📡【Codex 重置雷达 · 状态】`，并**逐行**含（顺序固定）：
  `监控开关：开启`（关时 `监控开关：已关闭（enable_monitor=false）`）；`最近采集：<时间>` 或 `最近采集：暂无记录`；
  `最新预测：` 二选一 `还没有可用的 forecast 快照` / `24h N% / 48h N%（置信度：低|中|高|未知）`；
  `订阅群数：N`；末行独立署名 `数据来源：https://codex-reset.com/`。
  首轮未建基线另有 `⚠️ 基线尚未建立（timeline 未成功采集），自动通知暂缓。`；
  数据陈旧另有 `⚠️ 数据可能已过时（最近采集：<时间|未知>）。`
- 判定：PASS = 标签齐全 + 有署名行；FAIL = 缺标题/署名/无响应/回「尚未初始化」。
- 不通过先看：`docker logs <CT> --since 10m 2>&1 | grep -E "初始化完成|指令处理失败|尚未初始化"`

**A-2 `/cr help`**
- 操作：发 `/cr help`。
- 期望：首行 `📖【Codex 重置雷达 · 指令说明】`，且**逐字**出现这 12 个子命令：
  `/cr 或 /cr status`、`/cr forecast`、`/cr latest`、`/cr credits`、`/cr history [n]`、`/cr help`、
  `/cr subscribe`、`/cr unsubscribe`、`/cr settings`、`/cr set <项> <值>`、`/cr groups`、`/cr test`；
  以及两行：
  `备用指令：/codexreset 与 /cr 完全等价（当 /cr 被其他插件占用时使用）。`
  `数据来自第三方站点 codex-reset.com，非官方接口，请自行判断风险。`；末行署名。
- 判定：PASS = 12 子命令 + 2 行说明 + 署名行齐全。
- 不通过先看：`docker logs <CT> --since 10m 2>&1 | grep -E "/cr 可能被插件占用|指令处理失败"`
  （若 `/cr` 无反应而 `/codexreset help` 正常＝短名冲突，此时 help 末尾会多一行
  `提示：/cr 可能被其他插件占用，如无响应请改用 /codexreset。`）

**A-3 `/cr status`（间隔 ≥15s）**
- 操作：等 A-1 满 15 秒后发 `/cr status`。 期望：同 A-1 格式，**不应**出现限频文案。
- 判定：出现 `请等待 N 秒后再试。` → 判「间隔不足」，重做而非 FAIL。
- 不通过先看：`grep "\[codex-reset\]\[repo\] opened db"`（库没开则一切查询异常）。

## B. AC-15 权限（真实群）

前置：测试群已由管理员 `/cr subscribe`（便于对比配置）。

**B-1 非管理员执行三条写指令**
- 操作：用**非管理员**账号逐条发（每条间隔 ≥6s，避开 5s 限频）：

```
/cr subscribe
/cr unsubscribe
/cr set forecast off
```

- 期望：三条**首行逐字**：

```
仅管理员可执行该指令。使用 /cr help 查看权限说明。
```

  整条消息实为「首行 + 空行 + 署名行」（`_finalize()` 给**每条**回复追加署名）：

```
仅管理员可执行该指令。使用 /cr help 查看权限说明。

数据来源：https://codex-reset.com/
```

  ⚠️ 不要按「整条等于该句」判定——署名行是强制的（AC-20）。
- 判定：PASS = 三条**首行**逐字一致 + 均带署名行 + 无「订阅成功/已取消订阅/已更新」字样。

**B-2 证明「配置真没变」（三道独立证据，缺一不算过）**
1. 管理员 `/cr settings`：6 行**全部**为「（全局默认）」，形如
   `确认重置：开启（全局默认）` / `概率预测：开启（全局默认）` / `24h 阈值：50%（全局默认）`…
   任一行变「（群覆盖）」→ AC-15 **FAIL**。
2. DB 稀疏覆盖为空：
   `docker exec -i astrbot python3 - "SELECT subscription_id,group_id,settings_json,version FROM subscriptions WHERE group_id='2001'" < ev/dbq.py`
   期望 `settings_json` 为 `{}`（未订阅则无行），`version` 未被 `set` 递增。
3. 审计只有 denied：
   `docker exec <CT> sh -lc 'grep denied <AD>/plugin_data/astrbot_plugin_codex_reset/audit/audit-$(date +%Y-%m).jsonl | tail -20'`
   期望含 `"action": "subscribe:denied"`、`"unsubscribe:denied"`、`"set:denied"`，`"result": "forbidden"`；
   **不应**出现 `"action": "subscribe"`+`"result": "ok"` 或 `"set:enable_forecast"`+`"ok"`。

**B-3 查询仍可用**
- 操作：该成员发 `/cr status`、`/cr forecast`（间隔 ≥15s）。
- 期望：状态正常；`/cr forecast` 首行 `📈【重置概率预测】`。 判定：两条均无「仅管理员可执行」。

## C. AC-16 订阅 / 退订（真实群）

**C-1 管理员订阅**
- 操作：群里 `/cr subscribe`。 期望（逐字）：

```
✅ 订阅成功：本群将接收 Codex 重置相关推送。

重置开、预告开、预测开、权益开；24h 阈值 50%；48h 阈值 70%

数据来源：https://codex-reset.com/
```

  变体：重复订阅 → `本群已订阅：`+同段回显；达上限 → `订阅失败：已达群上限 50 个，请先取消其他群订阅。`；
  私聊执行 → `请在目标 QQ 群内执行 /cr subscribe（订阅来源必须取自真实群会话）。`
- 判定：PASS = 首行逐字 + 6 项回显与 `/cr settings` 一致 + 署名行。

**C-2 确认「能收到通知」**
- 先证通道：`/cr test` → 收到 `🧪【测试消息】以下为样例，非真实事件` 开头的消息。
  ⚠️ 它**不走投递队列**，只能证明「插件能发到这个群」，**不能**单独证明订阅可推送。
- 再证真实投递（等真实事件）：

```bash
docker exec -i astrbot python3 - "SELECT group_id,last_success_at,last_failure_at,uncertain_count FROM subscriptions" \
  "SELECT status,COUNT(*) FROM deliveries GROUP BY status" \
  "SELECT id,kind,status,created_at,sent_at,error_code FROM deliveries ORDER BY id DESC LIMIT 10" < ev/dbq.py
```

- 期望：至少 1 行 `status='delivered'` 且对应 `last_success_at` 非空。
- 判定：PASS = 群里真收到自动推送且库里 `delivered`；`N/A（未触发）` = 窗口内 `logical_events` 无新增
  （记「未观察到」，**不**记 PASS 也**不**记 FAIL）；FAIL = 有事件新增却无投递行。

**C-3 管理员退订**
- 操作：`/cr unsubscribe`。 期望首行逐字 `✅ 已取消订阅：本群将不再接收任何自动推送。`+署名行；
  未订阅时 → `本群尚未订阅，请管理员执行 /cr subscribe。`

**C-4 验证「不再收到」（证据）**
1. 退订后 ≥24h 该群零自动推送（`/cr test` 不算）。
2. **私聊**机器人 `/cr groups`：列表不再含该群；无订阅时整条为 `📋【订阅群列表】`+`暂无订阅群。`
   （群里执行只会回 `请在私聊中执行 /cr groups（群列表含推送健康信息）。`）
3. WebUI `group_overrides`（「群配置」模板列表）中该群条目消失。
4. DB：`docker exec -i astrbot python3 - "SELECT COUNT(*) FROM subscriptions WHERE group_id='2001'" "SELECT COUNT(*) FROM deliveries" < ev/dbq.py`
   → 订阅行数 0。
   ⚠️ 该群历史 `deliveries` 会**同时消失**：`subscription_id` 是 `REFERENCES subscriptions(...) ON DELETE CASCADE`
   且启动时 `PRAGMA foreign_keys=ON`。「deliveries 总数变小」是预期，不是丢数据。要留档请在退订**前**导出。

## D. AC-17 重启恢复（Docker）

**D-1 重启前快照（T0）**

```bash
mkdir -p ev/before ev/after
for f in codex_reset.db codex_reset.db-wal codex_reset.db-shm; do
  docker cp astrbot:/AstrBot/data/plugin_data/astrbot_plugin_codex_reset/$f ev/before/$f
done
docker exec -i astrbot python3 - "SELECT status,COUNT(*) FROM deliveries GROUP BY status" \
  "SELECT MAX(created_at) FROM deliveries" "SELECT COUNT(*),MAX(first_seen_at) FROM logical_events" \
  "SELECT key,value FROM meta WHERE key LIKE 'runtime:%' OR key LIKE 'health:%'" < ev/dbq.py | tee ev/before/summary.txt
```

记录：订阅行数/`group_id`、`deliveries` 各状态计数、`MAX(created_at)`、`logical_events` 计数、
`runtime:owner`/`runtime:heartbeat_at`/`runtime:shutdown_at` 当前值。（`docker cp` 三文件非原子，副本仅作事后比对；即时判定用上面的 `exec` 查询。）

**D-2 重启**：`docker restart astrbot`
（想顺带验极端场景用 `docker kill astrbot && docker start astrbot`：SIGKILL 不写干净退出标记，
会同时触发 §F-1 的只读风险）

**D-3 重启后 3 分钟看日志（逐条命中）**

```bash
docker logs astrbot --since 5m 2>&1 | grep -E "\[codex-reset\]"
```

| 必须出现的片段 | 含义 |
| --- | --- |
| `[codex-reset][repo] opened db=codex_reset.db` | 库打开成功（用的持久卷那份） |
| `[codex-reset][repo] schema 初始化/升级完成 old=2 new=2` | schema 未降级/未重建 |
| `[codex-reset][pipeline] bootstrap 恢复缓存 endpoints=[...]` | 缓存从库恢复，**不是**重新拉取 |
| `[codex-reset][repo] inflight -> uncertain count=N` | 残留 `inflight` 被显式标 `uncertain`（N=0 时不打印，正常） |
| `[codex-reset][main] 初始化完成 data_dir=... version=1.2.0 readonly=False` | 初始化完成且非只读 |
| `[codex-reset][main] round endpoint=... error=ok judged=0 deliveries=0` | 首轮未产生新投递 |

⚠️ 易错：**没有**叫 `requeue` 的日志，真实字面是 `inflight -> uncertain count=N`；
`bootstrap` 每次进程启动只出现**一次**。
- 判定：PASS = 上表关键行命中（`inflight -> uncertain` 允许缺失）+ `readonly=False`。

**D-4 判定「重启后 30 分钟不重发历史事件」**：先 `date -u +"%Y-%m-%dT%H:%M:%SZ"` 记下重启时刻 T，
30 分钟后重跑 D-1 脚本，并把下面示例时刻替换成 T：

```sql
-- 1) 重启后是否新建了投递行（把示例时刻替换为 T）
SELECT COUNT(*) FROM deliveries WHERE created_at > '2026-10-20T05:30:00+00:00';   -- 期望 0
-- 2) 同一订阅是否对「同一事件同一版本」投递了多次
SELECT subscription_id, logical_id, state_version, COUNT(*) c FROM deliveries
 GROUP BY subscription_id, logical_id, state_version HAVING c > 1;                -- 期望空集
-- 3) 重启后有无 send 记录（sent_at）落在 T 之后
SELECT COUNT(*) FROM deliveries WHERE sent_at > '2026-10-20T05:30:00+00:00';      -- 期望 0
```

- 期望：三条全 0/空集。原理：`deliveries` 有 `UNIQUE (subscription_id, logical_id, state_version)`，
  `bootstrap()` **不写** `logical_events`。若真刷屏，先怀疑 `plugin_data` 没挂持久卷。
  ⚠️ 分组必须带 `subscription_id`：多个群订阅同一事件时，每个群各有 1 行是**正常**的，
  只按 `logical_id` 分组会把正常多群投递误判成重发。
- 例外：`enable_catchup` 默认 **false**；若为 true 且 `max_catchup_hours=6`，重启会**有意**发 1 条
  `📌【补发摘要】共 N 条更新`——属设计行为，需确认 N 仅覆盖最近 6h、每群仅 1 条。
- 判定：PASS = 三条全 0 + 30 分钟内该群实收 0 条（或仅 1 条合规补发摘要）。

**D-5 `uncertain` 的两个数字别搞混**：`deliveries.status='uncertain'` 是真正的超时/强杀行（不重发）；
`subscriptions.uncertain_count` 是**计数器**，`mark_uncertain()` **和**重试到终态失败（`attempt>=3`）
都会 +1，而 `requeue_inflight_as_uncertain()` **不** +1。故 `/cr groups` 的 `uncertain：N`
**不等于** `COUNT(*) WHERE status='uncertain'`，不一致属正常。

## E. AC-22 `/cr test`

**E-1 未订阅群由管理员执行**
- 操作：在**没有** `/cr subscribe` 的群，管理员发 `/cr test`。
- 期望（逐字，样例时间是硬编码 `2026-01-01 00:00`，不随真实时间变）：

```
🧪【测试消息】以下为样例，非真实事件

✅【Codex 全球重置已确认】（示例）
codex-reset.com 已记录一次全球额度重置。
确认记录时间：2026-01-01 00:00（北京时间）

本次为 /cr test 生成，不改变任何真实事件状态。

数据来源：https://codex-reset.com/
```

  时区后缀随 `time_zone` 变（`Asia/Shanghai`→`北京时间`、`UTC`→`UTC`、其他→IANA 原名）；
  非管理员执行 → 首行 `仅管理员可执行该指令。使用 /cr help 查看权限说明。`
- 判定：PASS = 首行 + 4 行正文 + 署名行齐全，且时区后缀与 `time_zone` 一致。

**E-2 确认「不入库」**：执行前后各跑
`docker exec -i astrbot python3 - "SELECT COUNT(*) FROM deliveries" "SELECT COUNT(*) FROM logical_events" "SELECT COUNT(*) FROM subscriptions" < ev/dbq.py`
→ 三个计数**完全不变**。变大即 FAIL（说明走了投递/入库路径）。

**E-3 60 秒限频**：`/cr test` 成功后立即再发一次 → 期望 `请等待 58 秒后再试。`（`RATE_LIMITS["test"]=60`，
实际 55~59 都正常）+ 空行 + 署名行，且群里**没有**第二条测试消息。
不通过先看：限频是内存态（`CommandService._last_seen`），插件重载即归零——先排除两次之间是否重载过。

## F. 附加项（V1.2 特有风险，各 1 条最小操作）

**F-1 只读模式（软单实例）**——⚠️ **不要用热重载制造**，热重载不会进只读。

```bash
docker kill astrbot            # 强杀：不写 runtime:shutdown_at
docker start astrbot           # 120s 内拉起（阈值 = 2×request_spacing_seconds = 120s）
```

- 期望日志：
  `[codex-reset][main] 检测到另一实例正在运行（owner=<pid>:<8位>，心跳 12s 前，阈值 120s），本实例进入只读采集模式（不写事件、不投递）`
  然后 `[codex-reset][main] 只读模式：不启动投递任务`，`初始化完成 ... readonly=True`。
  只读实例**不写** `runtime:heartbeat_at`。
- 期望指令：`/cr subscribe`、`/cr unsubscribe`、`/cr set ...` → 首行
  `🔒 只读模式：检测到另一个插件实例正在运行，本实例仅支持查询，写操作已停用。`；
  `/cr status` 正常；`/cr test` **正常**（`test` 不在 `WRITE_COMMANDS`）；私聊 `/cr groups` 正常。
- 判定：PASS = 日志出现「检测到另一实例正在运行」+ 写指令回只读 + 查询/测试正常。
- 恢复也验一遍：等满 120s 后 `docker restart astrbot`，下次应为 `readonly=False`（即 `ACCEPTANCE.md` §3.1 的 L-1）。
- 两个反直觉事实：① 同一进程内 reload 会打印 `同进程重新加载插件（owner=...），直接接管运行权`，
  因为判定用了 `previous.split(":",1)[0] == str(os.getpid())`；② 只读判定只做一次，对方死了也不会自动转主。

**F-2 `@全体` 仅在 aiocqhttp + `allow_at_all=true` + 重置确认时出现**
- 代码事实：`at_all = (task.kind == "reset") and plugin.allow_at_all`，
  且 `_send` 再加一道 `if at_all and platform == "aiocqhttp"`（`platform = umo.split(":",1)[0]`）。
  故 Watch / 预测 / 权益 / 更正 / 补发摘要**永不** @全体。
- 操作：`allow_at_all=true` 后等一次真实「重置确认」，再改回 `false`（≤5s 热更新）等下一条。
- 期望：true 时 ✅【Codex 全球重置已确认】带 @全体；false 时不带。
- 判定：两方向都观察到才 PASS；无真实事件 → `N/A（无事件）`，**禁止**用「改配置没崩」充当 PASS。

**F-3 429/502 退避：不靠运气、也不违反 PRD**
- **不要**为了逼 429 把 `request_spacing_seconds` 调到 60 以下：配置层会强制抬升
  （`MIN_SPACING_SECONDS=60`）并打 `request_spacing_seconds=30 小于合规下限 60s，已抬升为 60s（AC-19）`。
- **禁止在现有生产容器断网**：这会同时切断 QQ/OneBot 通道，影响机器人其他功能和群消息；不要运行 `docker network disconnect`。
- 将失败注入限定在隔离测试环境（不连接生产 QQ/OneBot）：可用本地 mock/测试替身验证调度器，并记录测试输出；也可单独运行单测：

```bash
python -m pytest tests/test_scheduler.py tests/test_client.py -q
```

- 退避期望是范围，不要求逐字固定值：普通网络/HTTP 失败在 120/240/480/900 秒基准上加入 0~5 秒抖动后，再封顶于 900 秒；因此前三级范围分别为 120~125、240~245、480~485 秒，第四级为 900 秒。成功后 `failures` 归零并回到正常轮换。
- 对 429，必须遵守服务端 `Retry-After`，过短值抬到最小间隔 60 秒；若服务端要求超过 900 秒，等待可超过普通失败的 900 秒基准上限，另有 0~5 秒抖动。不得把 429 的长等待判为退避上限失效。
- `tests/test_scheduler.py` 中的退避、抖动、长 `Retry-After` 单测，以及 `tests/test_client.py` 中的 `Retry-After` 解析测试，仅验证隔离条件下的代码行为，**不能替代真实环境验收**。本项状态仍为待填；没有隔离环境证据时不得记 PASS。
- 若在隔离环境收集调度日志，第二条独立证据为 `meta.health:{endpoint}` = `{"error_code": "...", "at": "..."}`。只读查询：
  `docker exec -i <隔离测试容器> python3 - "SELECT key,value,updated_at FROM meta WHERE key LIKE 'health:%'" < ev/dbq.py`
  → 失败期 `error_code` 变 `network`/`timeout`/`http_5xx`，恢复后回 `ok`。
  ⚠️ `health:*` 是给运维查库用的旁路；`/cr status` 的 `⚠️ forecast 端点最近采集失败…` 读的是
  `source_snapshots.ok`，不是同一张表。
- 判定：隔离环境证据至少包含 ≥2 级退避及 `health:*` 变化；不得在生产容器上制造断网。自动化测试绿或群内零伪造消息都不单独构成此项 PASS。
**F-4 时间显示随 `time_zone` 热更新**
- 操作：改 `<AD>/config/astrbot_plugin_codex_reset_config.json` 的 `"time_zone"` 为 `"UTC"`，**不重启**，
  等 5~10s 发 `/cr status`。
- 期望：日志 `[codex-reset][main] 配置已热更新 spacing=60 monitor=True tz=UTC`（≤5s，`POLL_TICK_SECONDS=5.0`）；
  状态时间后缀由 `（北京时间）` 变 `（UTC）`，数值同时 −8h；`/cr test` 的 `确认记录时间：2026-01-01 00:00（UTC）`。
- 判定：PASS = 热更新日志 + 后缀生效 + **全程未重启**。反向：填 `Mars/Olympus` →
  告警 `time_zone='Mars/Olympus' 非法（...），已回落 Asia/Shanghai`，回到 `（北京时间）`，判「降级正确」不算 FAIL。

## 7. 判读表

| 现象 | 最可能原因 | 下一步 |
| --- | --- | --- |
| 群里**完全没消息** | ① 未 `/cr subscribe`；② `enable_monitor=false`；③ 该群四类开关被 `/cr set` 关了；④ 从无真实事件 | ①`/cr settings` 看是否「本群尚未订阅」；②`/cr status` 首行；③ 看 6 行开关；④ 看「最近采集」是否更新 |
| `/cr` 无响应 | `/cr` 短名被其他插件占用 | 改用 `/codexreset`；grep `/cr 可能被插件占用` |
| 任何指令回「插件尚未初始化完成，请稍后重试。」 | 插件刚重载，`initialize()` 未完成 | 等 10s；仍不行 grep `初始化完成`/`数据库打开失败` |
| 写指令回「🔒 只读模式…」 | ① 另一实例在跑；② 上次被 kill -9/OOM（心跳 <120s 且无干净退出标记）；③ 从旧格式升级（L-4） | 确认无第二容器共用 `data` 卷；**再重启一次**（间隔 >120s）；查 `runtime:owner`/`heartbeat_at` |
| `/cr groups` 只回「请在私聊中执行…」 | 在群里执行了（设计如此） | 私聊机器人执行 |
| `/cr status` 长期「暂无记录」/ 数据陈旧 | 站点不可达、退避中、或 `enable_monitor=false` | 看 `health:*` 与 `backoff_s=` 日志；看 `⚠️ forecast 端点最近采集失败…` 行 |
| 出现「⚠️ 基线尚未建立…自动通知暂缓」 | `timeline` 从未成功采集（首轮无基线） | 等轮换轮到 timeline（`forecast→timeline→forecast→feed`，60s 队列）；持续 `schema_error` 需查站点字段 |
| 重启后重复收到旧消息 | `plugin_data` 未持久化，或容器重建换了路径 | 回查 0.5/0.6；看 `logical_events` 是否为空 |
| `/cr` 后立刻 `/cr status` 被限频 | 二者同属 `status` 键（15s） | 间隔 ≥15s |
| `subscribe` 回「已达群上限 50 个」 | 群数达 `group_limit`（默认 50，**非** schema 字段） | 先 `/cr unsubscribe` 掉不用的群 |
| 卡片改了配置没生效 | 卡片 `_version` 落后 DB → 判为过期丢弃 | grep `卡片版本过期`；以 `/cr subscribe`、`/cr set` 为准（DB 是真源） |
| 日志刷 `save_config 失败` | 配置目录不可写 | 修权限；DB 仍生效，功能不受影响 |

## 8. 记录表（可直接复制填写）

| AC / 项 | 执行时间（含时区） | 执行人 | 命令或操作 | 实际输出摘要 | 判定 | 证据路径 |
| --- | --- | --- | --- | --- | --- | --- |
| A-1 冒烟 `/cr` | | | `/cr` | | PASS/FAIL | `logs/A1-<ts>.txt` |
| A-2 冒烟 `/cr help` | | | `/cr help` | | | `logs/A2-<ts>.txt` |
| A-3 冒烟 `/cr status` | | | `/cr status` | | | `logs/A3-<ts>.txt` |
| AC-15 权限 | | | 非管理员 `/cr subscribe`/`/cr unsubscribe`/`/cr set forecast off`；管理员 `/cr settings` | | | `logs/B*`、`audit-YYYY-MM.jsonl`、截图 |
| AC-16 订阅 | | | `/cr subscribe`、`/cr test`、`/cr unsubscribe`、私聊 `/cr groups` | | | `logs/C*`、`ev/deliveries` 导出 |
| AC-17 重启 | | | `docker restart`；重启前后跑 D-1/D-4 | | | `ev/before/`、`ev/after/`、`logs/D3` |
| AC-22 `/cr test` | | | 未订阅群管理员 `/cr test` ×2 | | | `logs/E*`、前后计数 |
| F-1 只读模式 | | | `docker kill` + 120s 内 `start`，再 `restart` 恢复 | | | `logs/F1` |
| F-2 `@全体` | | | `allow_at_all` true/false 对照 + 真实重置事件 | | | 群消息截图 |
| F-3 退避 | | | 隔离测试环境/单测验证退避、抖动与 `Retry-After`；不对生产容器断网 | | | `logs/F3`（如有）、单测输出、隔离库 `meta` 导出 |
| F-4 时区热更新 | | | 改 `time_zone=UTC`，不重启，发 `/cr status` | | | `logs/F4` |

证据命名带时间戳（如 `A1-20261020T1355.txt`），截图与日志同目录；结论**回填**
`docs/ACCEPTANCE.md` §3「人工验收记录」表（日期/执行人/AstrBot 版本/插件版本/覆盖 AC/结论/备注）。

## 9. 边界：真环境必判 vs 自动化已覆盖

**只能在真实环境判定**（本文 §B~§F）

| 项 | 原因 |
| --- | --- |
| AC-15 | 权限来自 AstrBot `event.is_admin()`，模拟里只是构造出的布尔 |
| AC-16 | 「能收到/不再收到」是跨时间的群内可观测事实 |
| AC-17 | 真实强杀时机、WAL 落盘、持久卷挂载无法等价模拟 |
| AC-22 | 「群里只收到一条」需真实发送路径；模拟只能断言 `deliveries` 计数 |
| F-1 只读 | 依赖两个真实进程/真实心跳时间窗 |
| F-2 `@全体` | `At(qq="all")` 能否真 @ 到人取决于适配器与群设置 |
| F-3 退避 | 需真实网络失败与真实 429/502 |
| F-4 时区 | 需真实配置热更新链路 |

**已由自动化覆盖，不要重复劳动**：AC-01~14、18~21、23、24（映射见 `docs/ACCEPTANCE.md` §1，
左列「自动化测试节点」可逐条点开）。复现：

```bash
cd E:/project/Astrbot/astrbot_plugin_codex_reset
E:/Anaconda3/python.exe -m pytest tests -q                             # 编写本文时：428 passed
E:/Anaconda3/python.exe -m pytest tests/test_acceptance_matrix.py -v   # 矩阵节点存在性校验
```

⚠️ **自动化绿 ≠ AC 通过**：AC-15/16/17/22 在矩阵中仍为 `待验收（模拟测试已覆盖）`，
只有本执行单填完并回填 §3 记录后才能改为「通过」。

**本文不覆盖**：多群并发（AC-13）仅建议抽查两群；>7 天长期 WAL/内存增长与审计跨月切分归长稳；
上游字段变更（AC-23）不可主动构造，不建议为验收伪造站点响应。

## 11. 群内粘贴序列（纯聊天，可直接复制）

> **限制声明（先读）**：AstrBot 是聊天机器人，**不能执行 shell / SQL**。本节四条序列只由**人**在 QQ 里逐条发送，
> 只能判定「**回复文案**」与「**是否收到消息**」这类可观察行为。凡 `docker` / `sqlite3` 证据一律人工在**宿主机**执行
> （用 §0 的 `ev/dbq.py`），对应关系：AC-15→§B-2、AC-16→§C-2/§C-4、AC-17→§D-1/§D-4、AC-22→§E-2。
> 「不重发历史事件」「`inflight→uncertain`」这两件事**聊天侧永远证明不了**，必须看 §D 的 shell 证据。

**准备**：群 A（已订阅测试群）、群 B（**未**订阅群）、一个**非管理员小号**、一个 **AstrBot 管理员**。
限频键是 `(群, 子命令)`：**不同子命令互不影响**；只有同名子命令才共享窗口（注意 `/cr` 与 `/cr status` 共用 15s）。
下文中除标注「立刻重发」的那条，其余按自然节奏逐条发即可；期望值一律是**首行**（整条消息还会多一行署名）。

### 11.1 AC-15 权限 — 群 A

```
# 前置：群 A 已订阅（否则 /cr settings 会回「本群尚未订阅，请管理员执行 /cr subscribe。」）
#
# ↓↓↓ 以下 3 条必须用【非管理员小号】发，每条间隔 ≥6 秒
/cr subscribe
# → 期望首行：仅管理员可执行该指令。使用 /cr help 查看权限说明。
/cr unsubscribe
# → 期望首行：仅管理员可执行该指令。使用 /cr help 查看权限说明。
/cr set forecast off
# → 期望首行：仅管理员可执行该指令。使用 /cr help 查看权限说明。
#
# ↓↓↓ 以下 1 条必须用【管理员】发
/cr settings
# → 期望首行：⚙️【本群生效配置】
# → 且其后 6 行全部以「（全局默认）」结尾；出现任一「（群覆盖）」即 AC-15 FAIL
#
# ↓↓↓ 再切回【非管理员小号】
/cr status
# → 期望首行：📡【Codex 重置雷达 · 状态】
/cr forecast
# → 期望首行：📈【重置概率预测】（首轮未采集时会是「暂无数据：尚未完成首轮采集，请稍后再试。」）
```

### 11.2 AC-16 订阅 / 退订 — 群 A

```
# ↓↓↓ 以下 3 条必须用【管理员】在【群 A】发
/cr subscribe
# → 期望首行：✅ 订阅成功：本群将接收 Codex 重置相关推送。
# → 第 3 行应为：重置开、预告开、预测开、权益开；24h 阈值 50%；48h 阈值 70%
#   （已订阅过则首行变「本群已订阅：」，其余相同，不算失败）
/cr test
# → 期望首行：🧪【测试消息】以下为样例，非真实事件
#   说明：此条只证明「插件能把消息发到这个群」，不证明订阅投递链路
/cr unsubscribe
# → 期望首行：✅ 已取消订阅：本群将不再接收任何自动推送。
#
# ↓↓↓ 以下 1 条必须用【管理员】在【私聊】发（在群里发只会回「请在私聊中执行 /cr groups…」）
/cr groups
# → 期望首行：📋【订阅群列表】（群 A 已在列表消失）；若已无任何订阅则次行是「暂无订阅群。」
#
# 之后 24 小时内群 A 不应再收到任何自动推送（/cr test 产生的不算）
```

### 11.3 AC-22 `/cr test` — 群 B（**未订阅**群）

```
# 前置：群 B 从未 /cr subscribe；以下 2 条必须用【管理员】发
/cr test
# → 期望首行：🧪【测试消息】以下为样例，非真实事件
# → 第 3 行：✅【Codex 全球重置已确认】（示例）
# → 第 5 行：确认记录时间：2026-01-01 00:00（北京时间）   ← 时间是硬编码的，不随当天变化
# → 末行：数据来源：https://codex-reset.com/
/cr test
# ↑ 立刻重发（与上一条间隔 <60 秒，故意撞限频）
# → 期望首行：请等待 58 秒后再试。    （数字 52~59 都正常）
# → 且群里不应出现第二条「🧪【测试消息】」
```

### 11.4 AC-17 重启 — 群 A + 宿主机

```
# ===== 阶段 1：重启前，用【管理员】记录基线 =====
# 注意：11.2 已经把群 A 退订了，本阶段开始前必须先重新订阅，否则「订阅仍在」无从验证
/cr subscribe
# → 期望首行：✅ 订阅成功：本群将接收 Codex 重置相关推送。
/cr status
# → 记录两个值：「最近采集：<时间>」与「订阅群数：N」
/cr history 5
# → 记录条数（无事件时首行是「🗂️【确认重置历史】」+「暂无已确认的重置记录。」）
#
# ===== 阶段 2：人工在宿主机执行（AstrBot 本身不能执行）=====
#   docker restart astrbot
#   并按 §D-1 采集重启前快照、按 §D-3 grep 启动日志
#
# ===== 阶段 3：重启后 3 分钟内，同一管理员、同一群 A，按原样再发一遍 =====
/cr status
# → 「最近采集」应变成重启后的新时间；「订阅群数」仍为 N（订阅没丢）
/cr history 5
# → 条数与阶段 1 一致（变多只能说明「有新事件入库」，不能证明「是否重发」）
/cr subscribe
# → 期望首行：本群已订阅：…（说明订阅关系仍在）
# → 若首行变成「🔒 只读模式：…」→ 撞上 §F-1，按那里恢复（再重启一次，间隔 >120s）
/cr groups        # ← 必须在【私聊】发
# → 群 A 仍在列表；uncertain 计数不应数量级飙升
```

**聊天侧能判 / 不能判（别越界）**

| 事项 | 聊天侧可否判定 | 依据 |
| --- | --- | --- |
| 订阅关系是否保留 | ✅ 可判 | `/cr status` 的「订阅群数」、`/cr subscribe` 回「本群已订阅：」 |
| 数据/事件是否还在 | ✅ 可判 | `/cr history 5`、`/cr latest` 条数与内容 |
| 是否误入只读模式 | ✅ 可判 | `/cr subscribe` 回「🔒 只读模式：…」 |
| 投递队列健康 | ⚠️ 半可判 | 私聊 `/cr groups` 的「最近成功/失败」「uncertain：N」 |
| **重启后是否重发历史事件** | ❌ **不可判** | 必须 §D-4 三条 SQL（`created_at` / `sent_at` 与重启时刻比对） |
| **`inflight → uncertain` 是否发生** | ❌ **不可判** | 必须 §D-3 日志 grep 与 `deliveries.status` 统计 |

### 11.5 一键顺序（照此逐条发，共 21 步）

```
 1 管理员   群A  /cr subscribe              # ✅ 订阅成功…
 2 小号     群A  /cr subscribe              # 仅管理员可执行该指令。…
 3 小号     群A  /cr unsubscribe            # 仅管理员可执行该指令。…
 4 小号     群A  /cr set forecast off       # 仅管理员可执行该指令。…
 5 管理员   群A  /cr settings               # ⚙️【本群生效配置】 / 6 行全「（全局默认）」
 6 小号     群A  /cr status                 # 📡【Codex 重置雷达 · 状态】
 7 小号     群A  /cr forecast               # 📈【重置概率预测】或「暂无数据：…」
 8 管理员   群A  /cr test                   # 🧪【测试消息】以下为样例，非真实事件
 9 管理员   群A  /cr unsubscribe            # ✅ 已取消订阅：…
10 管理员   私聊 /cr groups                 # 📋【订阅群列表】/ 群A 已消失
11 管理员   群B  /cr test                   # 🧪【测试消息】…（群B 必须未订阅）
12 管理员   群B  /cr test   ← 立刻重发      # 请等待 58 秒后再试。
13 管理员   群A  /cr subscribe              # ✅ 订阅成功…（为 AC-17 重新铺路）
14 管理员   群A  /cr status                 # 记录「最近采集 / 订阅群数」
15 管理员   群A  /cr history 5              # 记录条数
16 宿主机   ——   docker restart astrbot     # AstrBot 不能执行；随后按 §D-3 grep 日志
17 管理员   群A  /cr status                 # 采集时间变新、订阅群数不变
18 管理员   群A  /cr history 5              # 条数与第 15 步一致
19 管理员   群A  /cr subscribe              # 「本群已订阅：…」；若是「🔒 只读模式」→ §F-1
20 管理员   私聊 /cr groups                 # 群A 仍在、uncertain 未飙升
21 宿主机   ——   §D-4 三条 SQL + §E-2 计数  # 「不重发」「不入库」「inflight→uncertain」只能这里判
```

> ⚠️ 全局最易误判的一步是 **第 19 步**：很多人看到重启后一切正常就判 AC-17 PASS。
> 但「重启后不重发历史事件」**只能**由第 21 步的 SQL（`deliveries.created_at` / `sent_at` 与重启时刻比对）
> 与 §D-3 日志（`bootstrap` / `inflight -> uncertain`）证明；第 17~20 步只证明「订阅与数据没丢」。
> 另：第 12 步必须**立刻**发才看得到限频，等满 60 秒再发会正常成功，反而会误判「限频失效」。
