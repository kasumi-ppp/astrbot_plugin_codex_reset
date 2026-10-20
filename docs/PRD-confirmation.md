# Codex 重置雷达 V1.2 — 需求确认文档（开发范围冻结）

> 文档性质：需求确认书（非营销 PRD）。上游需求书：`astrbot_codex_reset_PRD_v1_2.md`（V1.2）。开发计划书已由用户确认，本文视为**范围冻结依据**，与 PRD 冲突时以本文裁定为准。
> 插件标识 `astrbot_plugin_codex_reset`｜展示名「Codex 重置雷达」｜作者 `kasumi-ppp`｜许可 AGPL-3.0
> 基线：Python 3.12 / AstrBot 4.29.1 / Linux + Windows / OneBot v11（NapCat）
> 证据来源：`samples/forecast.json`、`samples/timeline.json`（77 条事件）、`samples/feed.json`、`samples/openapi.json`、响应头（`x-published-*`）

---

## 1. 产品目标与边界

**一句话目标**：用单实例采集 codex-reset.com 的公开 JSON，按「可信字段触发 + 每群独立订阅」向 QQ 群推送四类重置事实通知，并保证重复数据、重启、断网、多群并发都不刷屏、不误报。

**明确不做（V1.2 不实现，代码中不得出现相关调用）**

| 不做项 | 原因 |
|---|---|
| 读取个人 ChatGPT / Codex 额度、解析 token、要求 Codex CLI、登录 OpenAI | 与定位不符，触碰账号凭据 |
| 调用第三方私有 API / 爬取网页或 X / 使用 MCP 作为必要依赖 | 只依赖 OpenAPI 公开契约 |
| 自行推断未被数据源确认的重置（含"摘要里出现 reset 就算重置"） | 误报源头，见 §3 F01 |
| 消费 / 代领重置权益，承诺"所有账号已到账" | 事实不可知 |
| 独立 WebUI、Redis、MQ、Playwright、独立数据库进程 | 只用 `_conf_schema.json` + SQLite |
| 默认对全群广播、`@全体` 默认开启 | 需管理员显式 `/cr subscribe` |
| F09 多账号额度读取、F10 故障状态主动推送 | 范围外（F10 仅可在 `/cr status` 附一行只读诊断，且不纳入 AC） |

---

## 2. 功能清单与优先级

统一处理链（顺序固定，不得调整）：
`采集接口 → 校验快照 → 更新缓存 → 归并逻辑事件 → 按群评估 → 创建投递记录 → 发送消息 → 保存结果`

采集轮换：`forecast → timeline → forecast → feed` 循环复用；**任意两次 HTTP 请求间隔 ≥ 60s**；出现新 Watch 或确认重置后，下一槽位强制 `timeline`。

**state_version 取值规范**（投递唯一键 `UNIQUE(subscription_id, logical_id, state_version)` 的第二段）

| 类别 | state_version 定义 |
|---|---|
| reset | 常量 `confirmed` |
| watch | `{state}:{window.start_at or "nowindow"}`，同 id 窗口实质变更才产生新版本 |
| credits | `banked_state` 取值（`announced` / `arriving` / `available`），`unknown` 不产生版本 |
| forecast | `{window_kind}:{epoch_seq}`，`epoch_seq` 每次确认重置 +1 |

| 编号 | 功能 | 级别 | 触发条件（唯一充要条件） | 群可见结果 | 幂等 / 去重要求 |
|---|---|---|---|---|---|
| F01 | 全球重置确认通知 | P0 | ① `timeline` 新增行 `group=reset` **且** `announcement_state=="announced"` **且** `confirmed==true`；或 ② `forecast.last_reset_at` 严格大于已持久化最近确认时间；或 ③ `latest_alert.kind=="reset" && state=="confirmed"` | 一条「✅【Codex 全球重置已确认】」纯文本 | 三条来源归并为 1 个 logical_id；同群同 logical_id 只发 1 条；首轮启动仅建基线不推送 |
| F02 | 官方预告与强信号提前通知 | P0 | 新 `latest_alert`：`kind=watch && state=active`，且 `source_at` 晚于本地基线，且该 `alert.id` 未对该群发过 | 一条「⚠️【Codex 全球重置提前预警】」，明确"不是已重置" | 以 `alert.id` 为 logical_id；同 id 窗口实质变更只发 1 条更新；`expired` 历史 Alert 永不作为新预告 |
| F03 | 24h/48h 概率提醒 | P0 | 连续 `forecast_confirm_samples`（默认 2）次**成功采样**中同一窗口 `rounded_24h|48h >= threshold`，且 `armed=true`，且不在冷却期、未超单日上限 | 一条「📈【Codex 重置概率升高】」，含双窗口数字、confidence、更新时间、实验性声明 | 触发后 `armed=false`；回落至 `max(0, threshold-10)` 以下才可重新武装；冷却 12h；单群单日 ≤2 条预测 |
| F04 | 额外重置权益消息 | P0 | 新 `timeline` 行 `group=credits` 且 `banked_grant==true` 且 `banked_state ∈ {announced, arriving, available}`；或同一权益 `banked_state` 版本前移 | 一条「🎟️【Codex 额外重置权益动态】」 | logical_id = credits 行 id；每个 `state_version` 各发 1 次，同版本不重发；`unknown` 静默入库可查不推送 |
| F05 | 群内手动查询 | P0 | `/cr`、`/cr status|forecast|latest|credits|history|help` | 文本查询结果，尾部带来源署名 | 只读缓存，不触发上游请求；同群相同查询 15s 限频 |
| F06 | 多群订阅与管理员配置 | P0 | `/cr subscribe|unsubscribe|settings|set|groups` | 订阅确认 / 设置回显 / 群列表 | 以 `umo` 唯一约束；群与会话严格隔离，跨群互不影响 |
| F07 | 预告过期或更正通知 | P0 | 已推送的 watch 变 `expired`，或 `window.end_at` 已过且无新确认 reset，或 `corrected==true` | 一条「🔁【更正】」并把原状态标为失效 | 每个 (logical_id, 更正原因) 只发 1 次；仅发给原消息已送达的群 |
| F08 | 失败重试、去重及日志 | P0 | 投递失败 / 超时 / 重启恢复 | 无群可见消息（除不确定态排查） | 发送前先建 `deliveries` 记录；最多 3 次（首发 + 30s + 120s）；超时/中断记 `uncertain` 不自动重发 |
| F09 | 多账号额度读取 | **范围外** | — | — | — |
| F10 | 故障服务状态主动推送 | **范围外** | — | — | 仅 `/cr status` 可附一行降级提示，不推送 |

**离线补发（F08 子项）**：`enable_catchup` 默认 `false`。开启后仅补发 `max_catchup_hours`（默认 6h）内的**确认重置 / 权益状态变化**，**永不补发历史预测与历史 Watch**，且每群每次启动最多补 1 条（多条时合并为摘要）。
**陈旧抑制**：`feed.stale==true` 或数据年龄 > `max_source_age_minutes` 时，暂停一切**新自动**通知，仅缓存供查询并打「数据可能已过时」。

---

## 3. 验收矩阵（AC-01 ~ AC-24）

阶段依据 PRD §11。验证方式口径：
- **模拟·单元 / 模拟·集成** = 基于 `samples/` 脱敏 fixture 与按 OpenAPI 构造的合成 fixture，禁止向真实 QQ 群发送。
- **手工联调（真实群）** = 唯一允许触碰真实 OneBot 群的路径，**整体状态 = 待验收**。真实 QQ 端到端联调未完成前，不得宣称"QQ 推送已验收"。

| ID | 场景 | 预期 | 阶段 | 验证方式 |
|---|---|---|---|---|
| AC-01 | 新站点确认 `group=reset` + `announced` + `confirmed=true` | 订阅群收到一次确认消息 | 一 | 模拟·单元（fixture `2112432076767289678`） |
| AC-02 | `group=reset` 但 `announcement_state=none`；**或** `announcement_state=announced` 但 `confirmed=false` | 不发「已确认重置」 | 一 | 模拟·单元（fixture `2111492673890824348`、`2109045392684032299`） |
| AC-03 | `forecast.last_reset_at` 前进，timeline 尚无新行 | 可先按确认线索提示，timeline 补齐后不重复 | 一 | 模拟·集成（推进 2 个采集槽位） |
| AC-04 | 同一 reset 同时出现在 feed / timeline / forecast | 同群最多一条确认通知 | 一 | 模拟·集成（三端点同事实注入） |
| AC-05 | 新 `latest_alert` 为 `watch+active` | 推送提前预告，措辞非确认 | 一 | 模拟·单元（合成 watch fixture：现网样本无 Watch） |
| AC-06 | `watch+expired`，首次启动读到历史 Alert | 不作为新预告推送 | 一 | 模拟·单元（空基线 + expired fixture） |
| AC-07 | 已发送的 Watch 过期且未出现确认 | 发送一次适当更正，不刷屏 | 一 | 模拟·集成（active→expired 两轮） |
| AC-08 | Watch 未提供时间窗口 | 不编造预计日期或小时，输出「暂无明确预计时段」 | 一 | 模拟·单元（断言文本不含自造时长） |
| AC-09 | 24h 概率低于阈值 → 连续两次有效采样达标 | 仅一次预测提醒 | 二 | 模拟·单元（采样序列 42→51→54） |
| AC-10 | 概率长期高位，连续轮询数十次 | 不重复通知；冷却与滞回生效 | 二 | 模拟·单元（60 次采样序列） |
| AC-11 | 预测触发但 `confidence=low` | 可发，清楚标注实验性质与低置信度 | 二 | 模拟·单元（现有样本即 `low`） |
| AC-12 | 新 credits，状态 announced → available | 各状态必要时各通知一次；不算全球重置 | 二 | 模拟·单元（`2095651088502591861`→`2107975486798164736`） |
| AC-13 | 两个 QQ 群订阅不同类别 | 各群仅收到自己开启的类别 | 二 | 模拟·集成（双订阅记录） |
| AC-14 | QQ 群未订阅 | 零条主动通知 | 一 | 模拟·集成（空 subscriptions） |
| AC-15 | 群普通成员执行 `/cr subscribe` | 拒绝修改，仍可正常查询 | 一 | 手工联调（真实群）·待验收 |
| AC-16 | 管理员 `/cr unsubscribe` | 后续所有自动通知停止 | 一 | 手工联调（真实群）·待验收 |
| AC-17 | Docker / AstrBot 重启 | 订阅与投递记录保留，历史事件不刷屏 | 二 | 手工联调（真实群重启）·待验收〔另附模拟·集成重启测试〕 |
| AC-18 | 断网、502、429 `Retry-After`、错误 JSON | 不阻塞 QQ、无假消息、按计划退避 | 三 | 模拟·集成（故障注入） |
| AC-19 | 全部 API 请求 | 合规 UA，请求间隔 ≥60s | 三 | 模拟·集成（时间戳断言） |
| AC-20 | 所有展示上游数据的 QQ 消息 | 含 `数据来源：https://codex-reset.com/` 及原文链接（如有） | 一 | 模拟·单元（模板全量断言） |
| AC-21 | 群管理与查询指令 | 响应及时，不因上游请求阻塞对话 | 二 | 模拟·集成（缓存命中 <2s） |
| AC-22 | `/cr test` | 仅当前管理员指定群收到「测试」标记，不改变真实事件状态 | 一 | 手工联调（真实群）·待验收 |
| AC-23 | 网站字段缺失或出现新枚举值 | 安全降级、记录告警、插件不崩溃 | 三 | 模拟·单元（逐字段删除 + 枚举污染） |
| AC-24 | 并发管理指令与轮询同时更新状态 | 唯一约束与锁防止重复投递 | 二 | 模拟·集成（并发 10 任务 × 同一 logical_id） |

**阶段分布**：阶段一 13 条（01–08、14–16、20、22）；阶段二 8 条（09–13、17、21、24）；阶段三 3 条（18、19、23）。
**验收纪律**：AC-15/16/17/22 的真实群行为、以及全部真实 QQ 端到端联调，统一标记「待验收」，不得用单元测试结果替代；测试过程需保留失败样本与日志。

---

## 4. 命令与权限冻结表

主指令 `/cr`，别名 `codexreset`（`@filter.command_group("cr", alias={"codexreset"})`）。若启动时检测到 `/cr` 已被其他插件占用，则降级为 `/codexreset` 并在启动日志与 `/cr help` 中明确提示；**不做全量正则截取普通群聊**。

| 子命令 | 功能 | 权限 | 限频 | 上游请求 |
|---|---|---|---|---|
| `/cr`、`/cr status` | 最近确认重置、活跃 Watch、24h/48h 概率、更新时间、数据新鲜度 | 群成员 | 同群相同查询 15s | 否（读缓存） |
| `/cr forecast` | 完整预测详情 + 模型置信度 + 实验性声明 | 群成员 | 15s | 否 |
| `/cr latest` | 最近已确认重置事件 + 原文链接 | 群成员 | 15s | 否 |
| `/cr credits` | 最近 Banked Reset 状态 | 群成员 | 15s | 否 |
| `/cr history [n]` | 最近 n 条确认重置（默认 3，上限 10，超出截断并提示） | 群成员 | 15s | 否 |
| `/cr help` | 帮助信息（含 `/codexreset` 备用说明与权限表） | 群成员 | 15s | 否 |
| `/cr subscribe` | 订阅当前群（umo 取自真实事件） | AstrBot 管理员 | 5s | 否 |
| `/cr unsubscribe` | 取消当前群订阅 | AstrBot 管理员 | 5s | 否 |
| `/cr settings` | 当前群有效配置（默认 ⊕ 群覆盖）回显 | AstrBot 管理员 | 5s | 否 |
| `/cr set reset\|watch\|forecast\|credits on\|off` | 按群开关一类通知 | AstrBot 管理员 | 5s | 否 |
| `/cr set p24 <0-100>` / `/cr set p48 <0-100>` | 按群设置阈值（取整，越界拒绝并回显合法区间） | AstrBot 管理员 | 5s | 否 |
| `/cr groups` | 订阅群列表 + 推送健康（最近成功/失败、uncertain 计数）；建议私聊 | AstrBot 管理员 | 10s | 否 |
| `/cr test` | 仅向当前群发送标注「测试」的样例消息 | AstrBot 管理员 | 60s | 否 |

**权限规则**
1. 修改与诊断类命令一律 `@filter.permission_type(filter.PermissionType.ADMIN)` 校验；`admin_only_subscription=false` 时仍**只**放行 `event.is_admin()`，不引入 QQ 群主/群管等价（V1.2 不做群角色授权）。
2. 普通成员越权调用：返回「仅管理员可执行」并附 `/cr help` 提示；查询类命令不受影响。
3. 非群会话（私聊）执行 `subscribe/unsubscribe`：拒绝并提示到目标群内执行（`umo` 必须来自真实群会话，禁止裸群号拼接）。
4. 所有变更写审计日志：`operator_id / umo / action / 结果 / UTC 时间`，不含消息正文。
5. 限频按 `(umo, 命令签名)` 计数，命中冷却回复剩余秒数，**不触发上游请求**。

---

## 5. 配置字段冻结表（`_conf_schema.json`）

字段名逐字采用 PRD §5，不得改名（改名即破坏已确认范围）。

| 字段名 | 类型 | 默认值 | 取值范围 / 校验 | 可被群覆盖 |
|---|---|---|---|---|
| `enable_monitor` | bool | `true` | — | 否（全局总闸） |
| `enable_confirmed_reset` | bool | `true` | — | **是** |
| `enable_watch` | bool | `true` | — | **是** |
| `enable_forecast` | bool | `true` | — | **是** |
| `enable_credits` | bool | `true` | — | **是** |
| `threshold_24h` | int | `50` | 0–100 | **是** |
| `threshold_48h` | int | `70` | 0–100 | **是** |
| `forecast_confirm_samples` | int | `2` | ≥2 | 否 |
| `forecast_cooldown_hours` | int | `12` | 1–72 | 否 |
| `forecast_daily_max_per_group` | int | `2` | 1–10 | 否 |
| `time_zone` | string | `Asia/Shanghai` | 必须为合法 IANA 时区，非法则回落默认并告警 | 否 |
| `request_spacing_seconds` | int | `60` | ≥60（<60 强制抬到 60 并告警） | 否 |
| `max_source_age_minutes` | int | `10` | 1–120 | 否 |
| `max_catchup_hours` | int | `6` | 0–24 | 否 |
| `enable_catchup` | bool | `false` | — | 否 |
| `allow_at_all` | bool | `false` | — | 否 |
| `show_source_link` | bool | `true` | 只读，**不可关闭**（置 false 仍渲染署名并告警） | 否 |
| `admin_only_subscription` | bool | `true` | — | 否 |
| `group_overrides` | `template_list` | `[]` | 见下 | —（本身即覆盖容器） |

**`group_overrides` 条目结构（template_list item）**

| 子字段 | 类型 | 说明 | 群内可改 |
|---|---|---|---|
| `subscription_id` | string | 隐藏字段，由 `/cr subscribe` 自动写入，WebUI 不展示 | 否 |
| `group_name` | string | 群名展示（`/cr subscribe` 时尽力获取，缺失显示 `群 {group_id}`） | 否 |
| `enable_confirmed_reset` | bool | 该群确认推送开关 | **是** |
| `enable_watch` | bool | 该群预告推送开关 | **是** |
| `enable_forecast` | bool | 该群预测推送开关 | **是** |
| `enable_credits` | bool | 该群权益推送开关 | **是** |
| `threshold_24h` | int | 该群 24h 阈值，0–100 | **是** |
| `threshold_48h` | int | 该群 48h 阈值，0–100 | **是** |

**有效配置** = 全局默认 ⊕ 群覆盖（`subscriptions.settings_json` 优先于 `group_overrides`，`group_overrides` 仅作为 WebUI 编辑入口与同步出口）；配置变更经验证后立即生效，必要时重建定时器但不重置投递状态。

---

## 6. 消息模板冻结（中文纯文本）

**通用规则**
1. 每条消息必须包含独立一行 `数据来源：https://codex-reset.com/`；原始链接为空/无效时**省略该行但保留署名行**。
2. 「确认」与「预测」措辞严格分离：**仅 F01** 可写「已确认」/「已记录一次全球重置」；**F03 一律**写「网站模型预测」「不代表官方承诺」，禁止出现「即将重置」「确认」等词。
3. 时间统一格式 `YYYY-MM-DD HH:mm（{time_zone 中文名}）`；缺字段时整行省略，不写"未知时间"以外的臆测。
4. 不使用「OpenAI 官方推送」表述；统一称「codex-reset.com 记录 / 监控站」。
5. 纯文本，无图片、无富文本；`allow_at_all=true` 时也仅在确认重置消息前置 `@全体`，且平台不支持时静默降级。

### 6.1 全球重置确认（F01）

```text
✅【Codex 全球重置已确认】

codex-reset.com 已记录一次全球额度重置。
确认记录时间：2026-10-20 14:33（北京时间）
提醒：个人账号额度生效可能存在延迟，请以 Codex 实际状态为准。

原始公告：https://x.com/...
数据来源：https://codex-reset.com/
```

### 6.2 官方预告与强信号（F02）

```text
⚠️【Codex 全球重置提前预警】

检测到有效重置信号（尚未确认完成）。
预计窗口：今日 20:00—22:00（北京时间）
当前状态：监控站 Watch active
提醒：这不是已重置通知，到时仍需等待确认。

原始消息：https://x.com/...
数据来源：https://codex-reset.com/
```

无窗口时该行固定为：`预计窗口：暂无明确预计时段`（不得出现自造小时数）。`latest_alert.pending=="on_its_way"` 时追加一行：`补充：网站尚未测得实际重置，确认待定。`

### 6.3 概率预测（F03）

```text
📈【Codex 重置概率升高】

未来 24 小时：54%
未来 48 小时：73%
模型置信度：低
统计更新时间：2026-10-20 16:00（北京时间）

⚠️ 这是独立站点实验性预测，不代表官方承诺。
数据来源：https://codex-reset.com/
```

仅 48h 触发时标题改为「未来 48 小时重置预测」；双窗口同时触发合并为一条，标题固定 24h 版。`confidence` 映射：`low→低`、`medium→中`、`high→高`、其他→原样并追加「（未识别置信度）」。

### 6.4 额外重置权益（F04）

```text
🎟️【Codex 额外重置权益动态】

事件：Banked Reset
状态：已宣布（尚不能视为所有账号已到账）
更新日期：2026-10-20
详情：请查看原始消息与个人账号实际信息。

原始来源：https://x.com/...
数据来源：https://codex-reset.com/
```

状态文案映射（冻结）：`announced→已宣布（尚不能视为所有账号已到账）`、`arriving→正在发放（网站监测账户未全部到账）`、`available→网站记录该权益已可用（不代表所有账号可用）`。`unknown` 不出自动消息。

### 6.5 更正消息（F07）

```text
🔁【更正】此前的重置预告未获确认

此前预告（20:00—22:00）已过期，截至现在未见确认重置记录。
原预告状态：已失效（expired）
说明：本次更正仅同步网站数据变化，不代表重置不会发生。

原始消息：https://x.com/...
数据来源：https://codex-reset.com/
```

重置被撤回/降级时：`🔁【更正】此前记录的重置已被网站更新`，正文写「网站已将此前记录状态调整为 {new_state}，请以此为准，本插件不再重复推送该事件。」权益状态回退时：`🔁【更正】额外重置权益状态已更新为 {状态文案}`。

### 6.6 测试消息（`/cr test`）

```text
🧪【测试消息】以下为样例，非真实事件

✅【Codex 全球重置已确认】（示例）
codex-reset.com 已记录一次全球额度重置。
确认记录时间：2026-01-01 00:00（北京时间）

本次为 /cr test 生成，不改变任何真实事件状态。
数据来源：https://codex-reset.com/
```

---

## 7. 歧义与决策记录（裁定 + 理由）

| # | 待定点 | 最终裁定 | 理由 |
|---|---|---|---|
| D-01 | `announcement_state` 字段缺失的旧归档事件是否算「确认」 | **不算新确认，不触发 F01**。确认的唯一充要条件是 `group=reset && confirmed=true && announcement_state=="announced"`；缺失字段走 fail-closed。这些行只在「首轮建基线」时写入 `last_reset_at` 基准与 `/cr history` 展示 | 实测 40 条缺失行 100% 为 `source=archive`、日期 ≤2026-08-11，属降级归档数据，无法区分 announced/none；且它们不会新增，只作历史 |
| D-02 | `announcement_state=announced` 是否足以确认 | **不足**。必须叠加 `confirmed==true` | 实测 `2109045392684032299`（2026-10-11，`operator-observed`）`announcement_state=announced` 但 `confirmed=false`（OpenAPI：`confirmed` 表示「站点核验为真实全球重置」）。只判 state 会误报，直接违反 AC-02 |
| D-03 | timeline credits 行没有 `announcement_state` 时如何判权益 | **权益状态只看 `banked_state`**（`announced/arriving/available/unknown`），完全忽略 `announcement_state`；`banked_state` 缺失 → 视为 `unknown`，静默入库、可查询、不推送。**`banked_grant` 仅作展示，不参与判定** | 实测 13 条 credits 行全部无 `announcement_state` 字段（OpenAPI 也未将其列为 credits 必填）；credits 行 `confirmed` 恒为 true，不可用作重置或权益依据。另：F04 字面的 `banked_grant==true` 与 AC-12 目标行 `2107975486798164736` 冲突（该行 `banked_grant=false` 但 `banked_state=available`），若严格按字面实现会让 AC-12 无法通过，故该字段一律不参与判定，仅供 `/cr credits` 展示 |
| D-04 | Watch 过期更正 与「列表消失」的边界 | **消失 ≠ 撤回**。timeline/feed 某 id 不再出现不产生任何消息；仅当站点字段权威变化（`latest_alert.state` 转 `expired`、`corrected==true`、同 id 行 `announcement_state` 降级）才补一次更正 | 列表可能因分页/过滤/`dataset` 变化而变动，非语义事件；以"缺失即撤回"会造成批量误更正 |
| D-05 | 预测「同群单日上限」与「确认重置后重置采样」的计数口径 | 单日上限按**配置时区自然日 00:00 归零**，只统计 F03 自动消息，确认/预告/权益不计入，且**不因 reset 归零**；确认 reset 只清空 `consecutive_hits`、`armed=false`、`epoch_seq+1` | 若 reset 也归零 day_count，23:59 确认重置后可立刻再发 2 条预测，等于绕过防刷屏；两者是不同维度的计数 |
| D-06 | 滞回带 `threshold-10` 的下限保护 | 重新武装阈值 = `max(0, threshold-10)`；且**`threshold==0` 语义为「关闭该窗口自动预测提醒」**，不视为"永远达标" | 0 阈值配滞回会导致每轮必发；把 0 定义为关闭可让 0–100 全区间语义自洽，避免除零/负值分支 |
| D-07 | 时区展示与 UTC 存储的转换点 | 存储层**只存** timezone-aware UTC（ISO8601）；转换只发生在「渲染文本」与「自然日计数」两处，统一用 `time_zone`；Watch 窗口若带 `time_zone`（如 `America/Los_Angeles`）仅作为原文 `label` 展示，不改变换算基准 | 单一转换点可避免"北京时间当 UTC"类错误；`window.time_zone` 指 Tibo 原话时区，与群展示时区职责不同 |
| D-08 | `latest_alert` 与 `timeline` 事件如何建立别名 | 直接用 `latest_alert.id` 作为 timeline 事件 id 的**同源别名**（不靠时间模糊匹配）；`latest_alert.url`/`confirmation_url` 为辅证 | 实测 `latest_alert.id == 2112432076767289678 == timeline 事件 id`；同源 id 可零风险去重，避免时间窗匹配误合并 |
| D-09 | `forecast.time_window`（如 `3 AM - 6 AM UTC`）能否当 Watch 预计窗口 | **不能**。Watch 窗口只取 `latest_alert.window`；缺失时回退到 timeline 同 id 行的 `official_window`；两者皆无才输出「暂无明确预计时段」 | `time_window` 是统计节律窗口（cadence/time_window），与 Watch 预告无关，混用即编造时间，违反 AC-08 |
| D-10 | `feed.banked_observations[].after_announcement=false` 的权益如何处理 | 视为「监测账户观测到权益到账」：只更新状态、可查询，文案用「网站监测账户已观测到该权益到账」，**禁止**输出「已宣布」 | 样本原文即「Tibo has not announced it on X yet」；`BankedObservation` 是观测事实，不是公告 |
| D-11 | 缓存未刷新时是否重复判定 | 记录 `x-published-batch`，同批次（或 `x-published-cache-status=HIT` 且 `x-published-checked-at` 未前移）**跳过事件判定**，只更新 `last_seen_at` | 站点头 `max-age=60` 且实测返回 `x-published-*`；同批次重判会重复触发通知 |
| D-12 | 采集与判定失败时的基线推进 | 无效 JSON / 字段缺失 / 5xx / 429：**绝不推进** `last_reset_at`、`consecutive_hits`、`epoch`，也不写入 `source_snapshots` 的有效快照位；连续失败进入 2/4/8/15 分钟退避 | 失败快照推进基线会造成"漏发即永久漏发"，与"重启不刷屏"同属不可逆错误 |

**裁定总数：12 条。**

---

## 8. 待确认问题（需用户拍板，均附默认假设，开发不阻塞）

| # | 问题 | 默认假设（未回复即按此实现） | 影响面 |
|---|---|---|---|
| Q-1 | 订阅群上限是否设硬上限？（防 `/cr groups` 与推送队列无界增长） | 默认 **50 群**，超出拒绝订阅并提示 | F06、AC-24 |
| Q-2 | `threshold_24h=0` 的语义确认（是否接受"0 = 关闭该窗口提醒"） | 按 D-06 执行：0 = 关闭 | F03 |
| Q-3 | `/cr test` 是否允许在任意群执行（含未订阅群）？ | 允许，但仍要求管理员身份且 60s 限频；不写入 `deliveries` | AC-22 |
| Q-4 | 是否接受"补发（`enable_catchup`）每次启动每群最多 1 条合并摘要" | 接受；超出 6h 一律不补 | F08、AC-17 |
| Q-5 | 预测消息是否附加 `signals`/`cadence` 等辅助字段（如 `recent_median_days`） | **不加**，V1.2 只展示 24h/48h/confidence/更新时间 | F03、模板冻结 |

---

## 附：本文件对开发的三条硬约束

1. **不得**用「摘要含 reset」「`group=reset` 单独成立」「`announcement_state=announced` 单独成立」任一条件触发确认推送（D-01/D-02）。
2. **不得**让任何非 F01 消息出现「已确认」；不得让任何 F03 消息出现「即将重置」。
3. 除 `docs/` 之外的仓库文件（`main.py`、`metadata.yaml`、`README.md`、`LICENSE`）在本次需求确认阶段一律不改动。
