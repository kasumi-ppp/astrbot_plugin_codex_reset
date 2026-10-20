# Codex 重置雷达（astrbot_plugin_codex_reset）

AstrBot 插件：读取第三方站点 [codex-reset.com](https://codex-reset.com/) 的公开数据，向订阅的 QQ 群推送
**Codex 全球重置确认、提前预警、重置概率、额外重置权益动态**四类消息。

- 版本：`v1.2.0`｜作者：`kasumi-ppp`｜许可：**AGPL-3.0**
- 平台：AstrBot `>=4.16,<5`，消息平台 `aiocqhttp`（OneBot v11）
- 数据来源署名：每条消息都含独立一行 `数据来源：https://codex-reset.com/`（**不可关闭**，许可要求）

---

## 1. 功能

| 类别 | 触发条件 | 消息措辞要点 |
|---|---|---|
| **F01 确认重置** | 站点 `group=reset` 且 `announcement_state=announced` 且 `confirmed=true`；或 `latest_alert` 为 `reset/confirmed`；或 `last_reset_at` 前进 | 唯一允许出现「已确认」的消息；跨端点同一事实只推 1 条 |
| **F02 提前预警** | 站点出现新的 `latest_alert(watch/active)` | 明确「尚未确认」；无窗口时固定输出「预计窗口：暂无明确预计时段」，绝不编造小时数 |
| **F03 概率预测** | 24h/48h 概率连续 `forecast_confirm_samples` 次达标（滞回 + 冷却 + 单日上限） | 标注「实验性预测」「不代表官方承诺」，绝不出现「已确认」「即将重置」 |
| **F04 额外重置权益** | `group=credits` 且 `banked_state ∈ {announced, arriving, available}` | 说明「网站记录该权益已可用（不代表所有账号可用）」；`unknown` 静默入库不推送（注：`banked_grant` 仅作展示，**不参与判定**——AC-12 目标行为 `banked_grant=false`，严格按 F04 字面会破坏验收） |
| **F07 更正** | 已推送的预告过期/被站点更正/重置被降级 | 「此前的重置预告未获确认」；同一原因只发一次；若重置确认**落在窗口内**则不再补发更正 |

补充能力：

- **多群订阅**：每个 QQ 群独立开关（四类通知 + 24h/48h 阈值），支持 WebUI 卡片与 `/cr set` 双向同步；
- **重启不刷屏**：`logical_events` 以 `(logical_id, state_version)` 去重，重启后历史事件不重推；
- **启动补发**（可选）：`enable_catchup=true` 时补最近 `max_catchup_hours` 内的重置/权益，每群最多 1 条摘要；
- **礼貌采集**：固定 UA、请求间隔 ≥60s、429 尊重 `Retry-After`、失败按 2/4/8/15 分钟退避；
- **陈旧抑制**：`feed.stale` 或源端数据过旧时仍入库但不推送，并在查询里标注「数据可能已过时」；
- **SQLite 持久化** + 审计 JSONL（只记操作人与 umo 指纹，不记消息正文）。

---

## 2. 安装

### 方式 A：AstrBot 插件市场

在 AstrBot WebUI →「插件」→「市场」中搜索 **Codex 重置雷达**（`astrbot_plugin_codex_reset`）安装并启用。

### 方式 B：Git 克隆

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/kasumi-ppp/astrbot_plugin_codex_reset.git
pip install -r astrbot_plugin_codex_reset/requirements.txt
```

安装后在 WebUI 插件列表中启用，并按需调整配置（见 §4）。默认情况下插件**不会**主动推送任何消息，
需要管理员在目标群内执行 `/cr subscribe` 才会开始向该群推送。

### 数据卷持久化（Docker 必读）

插件数据默认位于 AstrBot 数据目录：

```
data/plugin_data/astrbot_plugin_codex_reset/
├── codex_reset.db        # SQLite（订阅、快照、逻辑事件、投递队列、预测状态）
├── codex_reset.db-wal
├── codex_reset.db-shm
└── audit/
    └── audit-YYYY-MM.jsonl
```

使用 Docker 部署时，请把 AstrBot 的 `data/` 目录挂载为**持久卷**，否则容器重建会丢失订阅与去重状态
（表现为：重新订阅后才能收到推送，且可能重复收到旧事件）。示例：

```yaml
services:
  astrbot:
    volumes:
      - ./data:/AstrBot/data
```

---

## 3. 命令

主指令 `/cr`，别名 `/codexreset`（完全等价）。当 `/cr` 被其他插件占用时，请改用 `/codexreset`，
插件启动日志与 `/cr help` 都会给出该提示。

| 命令 | 功能 | 权限 | 限频 | 是否请求上游 |
|---|---|---|---|---|
| `/cr`、`/cr status` | 监控状态、最近采集时间、24h/48h 概率、新鲜度 | 群成员 | 15s | 否 |
| `/cr forecast` | 预测详情 + 置信度 + 实验性声明 | 群成员 | 15s | 否 |
| `/cr latest` | 最近一次已确认重置 + 原始链接 | 群成员 | 15s | 否 |
| `/cr credits` | 最近额外重置权益动态 | 群成员 | 15s | 否 |
| `/cr history [n]` | 最近 n 条**已确认重置**（默认 3，上限 10，超出截断提示；已撤回的更正不计入） | 群成员 | 15s | 否 |
| `/cr help` | 指令说明 + 权限表 + `/codexreset` 备用说明 | 群成员 | 15s | 否 |
| `/cr subscribe` | 订阅当前群（`umo` 取自真实群会话） | AstrBot 管理员 | 5s | 否 |
| `/cr unsubscribe` | 取消当前群订阅 | AstrBot 管理员 | 5s | 否 |
| `/cr settings` | 本群生效配置与来源（群覆盖 / 全局默认） | AstrBot 管理员 | 5s | 否 |
| `/cr set reset\|watch\|forecast\|credits on\|off` | 按群开关一类通知 | AstrBot 管理员 | 5s | 否 |
| `/cr set p24\|p48 <0-100>` | 按群设置概率阈值（越界拒绝并回显合法区间） | AstrBot 管理员 | 5s | 否 |
| `/cr groups` | 订阅群列表 + 推送健康（最近成功/失败、uncertain 计数），建议私聊 | AstrBot 管理员 | 10s | 否 |
| `/cr test` | 当前群发送一条标注「测试」的样例消息（不入库、不建投递） | AstrBot 管理员 | 60s | 否 |

所有查询命令**只读内存缓存与 SQLite**，不会触发任何上游请求，因此响应不会被采集阻塞（AC-21）。

### 权限模型

1. 修改与诊断类命令（`subscribe / unsubscribe / settings / set / groups / test`）要求
   `event.is_admin()` 为真；普通成员执行时返回「仅管理员可执行该指令。使用 /cr help 查看权限说明。」
   并照常放行查询类命令。
2. **订阅必须来自真实群会话**：私聊执行 `/cr subscribe` 会被拒绝（禁止用裸群号拼接 umo）。
3. V1.2 不做 QQ 群主/群管授权；`admin_only_subscription=false` 仅为预留字段，**不会**放宽权限。
4. 所有管理动作写入审计日志（`operator_id / umo 指纹 / action / result / UTC 时间`，无消息正文）。
5. 限频按 `(umo, 子命令)` 计数，命中后回复剩余秒数且**不触发**任何上游请求。
6. **只读模式**：同一 `data_dir` 上检测到另一个实例仍在运行（心跳新鲜，且没有**由它自己写出**的
   干净退出标记）时，本实例只提供查询，`/cr subscribe`、`/cr unsubscribe`、`/cr set` 统一回复
   「只读模式」提示，不写任何事件、不建投递、不写心跳、不改 owner。避免两个实例互相覆盖配置。
   注：`runtime:shutdown_at` 的值形如 `{owner}|{ISO 时间}`——只认「记录里的那个实例」写的退出标记，
   这样只读实例的退出不会把仍在运行的实例「洗白」成已退出。

---

## 4. 配置（`_conf_schema.json`，19 个字段）

| 字段 | 类型 | 默认 | 说明 | 可被群覆盖 |
|---|---|---|---|---|
| `enable_monitor` | bool | `true` | 总开关；关闭后不采集，但投递队列继续消化，查询读缓存 | 否 |
| `enable_confirmed_reset` | bool | `true` | 推送「重置已确认」 | 是 |
| `enable_watch` | bool | `true` | 推送「提前预警」 | 是 |
| `enable_forecast` | bool | `true` | 推送「概率预测」 | 是 |
| `enable_credits` | bool | `true` | 推送「额外权益动态」 | 是 |
| `threshold_24h` | int | `50` | 24h 阈值（0=关闭该窗口） | 是 |
| `threshold_48h` | int | `70` | 48h 阈值（0=关闭该窗口） | 是 |
| `forecast_confirm_samples` | int | `2` | 连续达标采样次数（<2 抬到 2） | 否 |
| `forecast_cooldown_hours` | int | `12` | 同群两条预测的最短间隔（1-72） | 否 |
| `forecast_daily_max_per_group` | int | `2` | 同群每个自然日最多预测条数（1-10） | 否 |
| `time_zone` | string | `Asia/Shanghai` | 消息时间显示时区（IANA 名，非法回落并告警） | 否 |
| `request_spacing_seconds` | int | `60` | 两次请求最小间隔（<60 强制抬到 60） | 否 |
| `max_source_age_minutes` | int | `10` | 超过该分钟数视为陈旧：入库但不推送（1-120） | 否 |
| `max_catchup_hours` | int | `6` | 启动补发回溯小时数（0-24） | 否 |
| `enable_catchup` | bool | `false` | 启动时补发最近遗漏（每群最多 1 条摘要） | 否 |
| `allow_at_all` | bool | `false` | 确认重置时尝试 `@全体成员`（仅 aiocqhttp） | 否 |
| `show_source_link` | bool | `true` | 署名行开关；**不可关闭**（置 false 仍渲染并告警） | 否 |
| `admin_only_subscription` | bool | `true` | 预留字段：V1.2 固定只放行 `event.is_admin()` | 否 |
| `group_overrides` | template_list | `[]` | 群级配置卡片（由 `/cr subscribe` 与 `/cr set` 自动维护） | — |

**有效配置 = 全局默认 ⊕ 群级覆盖**：`subscriptions.settings_json` 只保存「显式覆盖过」的字段，
未覆盖字段继续继承全局默认，因此调整全局默认会自动影响所有未覆盖的群。
WebUI 卡片只是编辑入口，**数据库始终是唯一真源**；卡片 `_version` 与库中 `version` 不一致时视为
过期并忽略（不会覆盖数据库），卡片缺失则下次启动按库重新投影回填。

`/cr set` 把某个值改回全局默认时，会**删除**该覆盖字段，重新回到「继承」语义。

---

## 5. 局限性（务必阅读）

1. **非官方数据**：`codex-reset.com` 是第三方独立站点，接口非官方、可能变更或下线；插件对字段缺失/
   枚举污染做 fail-closed 降级，但不保证功能永久可用。
2. **实验性预测**：24h/48h 概率由站点模型给出，插件只做阈值转发；**不代表 OpenAI 官方承诺**。
3. **exactly-once 不可保证**：QQ/OneBot 侧发送超时或进程被强杀时，消息可能已送达但未被确认，
   插件会把该条记为 `uncertain` 并**不自动重发**（避免重复刷屏），因此极端情况下可能漏一条。
   可用 `/cr groups` 查看 `uncertain` 计数。
4. **`uncertain` 语义**：终态，不重试、不累加错误率，仅作为可观测信号；重启时残留的 `inflight`
   会统一转为 `uncertain`。
5. **仅 `aiocqhttp`**：`@全体成员` 仅在 `aiocqhttp` 且 `allow_at_all=true` 时以真实 `At` 组件发送，
   其他平台静默省略。
6. **单实例建议**：同一数据库被两个 AstrBot 实例同时写入时，去重靠唯一约束兜底，但仍可能出现
   重复投递；请勿在同一条数据卷上并行运行多个实例。

---

## 6. 数据来源与许可

- 数据来源：[https://codex-reset.com/](https://codex-reset.com/)（公开 JSON：`/api/forecast`、
  `/api/timeline`、`/api/feed`）。插件仅做只读 GET，不写入、不绕过任何访问限制。
- 每条展示上游数据的消息都包含 `数据来源：https://codex-reset.com/`，该行**不可关闭**。
- 本项目以 **AGPL-3.0** 发布（见 `LICENSE`）。二次分发/修改需保留同样的许可与署名要求。

---

## 7. 故障排查

| 现象 | 排查方向 |
|---|---|
| 群内没有任何推送 | ① 是否执行过 `/cr subscribe`；② `enable_monitor` 是否为 `true`；③ `/cr settings` 看四类开关是否被关闭；④ `/cr status` 的「最近采集」是否长时间不更新 |
| `/cr` 没有响应 | 可能被其他插件占用，改用 `/codexreset`；启动日志中搜索 `/cr 可能被占用` |
| 频繁 429 / 采集被退避 | 调大 `request_spacing_seconds`（≥60），不要降低；插件已按 `Retry-After` 与 2/4/8/15 分钟退避 |
| 大量 502 / 超时 | 站点侧问题，插件不会伪造消息；`/cr status` 会显示数据已陈旧 |
| 时间显示不对 | 检查 `time_zone` 是否为合法 IANA 名（如 `Asia/Shanghai`），非法值会回落并写 WARNING |
| 权限被拒 | 管理指令要求 AstrBot 管理员；`subscribe/unsubscribe` 还必须在**群会话**内执行 |
| 重启后重复收到旧消息 | 确认 `data/plugin_data/astrbot_plugin_codex_reset` 为持久卷；否则去重状态会丢失 |
| 指令回「只读模式」 | 同一 `data_dir` 上还有另一个 AstrBot 实例在跑（或上次异常退出没写退出标记）。停止多余实例即可；确认只有一个实例后重启，插件会写入 `runtime:shutdown_at` 并重新接管。 |
| 想彻底重置 | 停止插件 → 删除 `codex_reset.db*` 与 `audit/` → 重启并重新 `/cr subscribe` |

日志统一带 `[codex-reset][模块]` 前缀，排障时建议 `grep "codex-reset"`。

---

## 8. 开发与验收

| 文档 | 内容 |
|---|---|
| [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) | AC-01 ~ AC-24 验收矩阵、自动化测试节点映射、人工联调清单（AC-15/16/17/22 待真实群验收） |
| [`docs/architecture.md`](docs/architecture.md) | 分层架构、数据库 schema、决策记录（D-01 ~ D-12） |
| [`docs/PRD-confirmation.md`](docs/PRD-confirmation.md) | 需求与验收纪律来源 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更 |

```bash
E:/Anaconda3/python.exe -m pytest tests -q          # 全量测试
E:/Anaconda3/python.exe -m ruff check services tests main.py
```

分层纪律：`services/**` **不得** `import astrbot`（由 `tests/test_schema.py` 守护），只有 `main.py`
可以接触 AstrBot 框架对象；因此业务逻辑可脱离 AstrBot 独立测试。

---

## 9. 免责声明

本插件为社区非官方工具，与 OpenAI、Anthropic 或 `codex-reset.com` 均无隶属关系。
所有数据与预测来自第三方站点，可能不准确、延迟或中断；插件按「现状」提供，不构成任何服务承诺或
订阅建议。因使用本插件产生的任何直接或间接损失，作者不承担责任。请自行判断是否依赖这些信息做出决策。
