# Changelog

本文件记录 Codex 重置雷达的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 `vMAJOR.MINOR.PATCH`。

## [Unreleased]

### 文档

- **README 改版**：新增居中徽章区（License / 版本 / AstrBot / 平台 / Python 共 5 枚）、每日预测图片
  示例图、`<details>` 折叠与 GitHub Alert 提示；可见正文由 258 行精简到 203 行（-21%）。
  命令表（15 条）与配置表（26 字段）一条未减，数据来源署名、Docker 持久卷警示、只读模式说明与
  免责声明全部保留。
- 权限模型、只读模式与 `shutdown_at` 语义、`uncertain` 语义、群级覆盖语义下沉至新增的
  [`docs/usage-notes.md`](docs/usage-notes.md)；排障表保留 4 行最高频现象，其余外链到
  `docs/ACCEPTANCE-RUNBOOK.md` §7 判读表。
- 新增 `scripts/render_readme_preview.py`：用插件自身渲染器生成 README 示例图
  （450×600、256 色自适应量化、约 33KB），脱离 AstrBot 可运行，重复生成字节一致。
- README「开发与验收」补充 `docs/QA-REPORT.md` 链接；复现命令去掉本机绝对路径，改为通用
  `python -m pytest tests -q`。

## [v1.3.0] - 2026-10-22

每日概率预测图片 + 投递优先级。**图片链路尚未在真实 QQ 群验证**，见「已知限制」。

### 新增

- **每日预测图片**：每天 `daily_report_time`（默认 `09:00`，时区 `daily_report_timezone`）向开启的群
  发送 900×1200 PNG 卡片，含 24h/48h 概率卡片与进度条、最近一次**已确认**全球重置（无记录显示
  「暂无记录」）、官方预告状态、数据更新时间、模型置信度、实验性声明与来源署名。
  - **深浅双主题**：`daily_report_theme = dark`（深色科技风）/ `light`（浅色简洁风）。
  - **每群每天至多一张**：新增 `daily_reports` 表，`(subscription_id, business_date)` 为主键；
    到期窗口内每 30s 巡检补发，重启后不会重复发送当天图片。
  - **文本降级**：渲染失败（缺 CJK 字体 / 渲染异常 / 超时）时按 `daily_report_text_fallback`
    改发等价文本卡片；**发送超时（结果未知）则记为 `uncertain` 且不再补发文本**，避免重复刷屏。
  - **陈旧不发**：无快照或源端数据超过 `max_source_age_minutes` 时跳过并落 `skipped` 记录。
  - **失败重试**：非终态失败按 `daily_report_retry_minutes` 重试，超过 `daily_report_expiry_minutes`
    到期窗口后不再重发；只读实例不参与日报发送。
- **命令**：`/cr report`（手动立即出图，不占用当日自动额度）、`/cr test image`（测试图片，不入库）、
  `/cr set daily on|off`（群级日报开关）。
- **两级开关**：全局 `enable_daily_report`（默认 `false`）+ 群级 `enable_daily_report` 覆盖；
  `group_overrides` 卡片同步支持该字段。
- **投递优先级**：`deliveries.priority`，`reset(0) < watch(1) < credits(2) < forecast/catchup(3)`，
  领取按 `priority ASC, id ASC`，重置确认优先于其他消息。
- **`@全体成员` 收窄为确认重置**：仅 `kind=reset` 且非更正且 `allow_at_all=true` 时才尝试 @全体成员，
  预警 / 概率 / 权益 / 日报一律不 @。
- **重置消息附带事件说明**：站点提供 `summary` 时追加「事件说明」一行。
- 数据库 schema 由 `2` 升到 `3`（新增 `daily_reports` 表与 `deliveries.priority` 列，
  v1.2 旧库自动迁移）。
- 依赖新增 `Pillow>=10.1`（图片渲染）。

### 修复

- **迁移顺序**：`idx_deliveries_priority` 索引改为在 `ALTER TABLE ... ADD COLUMN priority`
  **之后**创建。此前放在建表 DDL 里，会在旧库上让「打开数据库」直接抛
  `no such column: priority`，导致 v1.2 → v1.3.0 升级即崩溃。

### 已知限制（本次明确不修）

- **AC-25 / AC-26 仍需真实 QQ 群人工联调**：自动化测试只覆盖「假时钟 + 假图片发送通道」的逻辑，
  图片在真实 OneBot 链路上的上传/显示、定时是否真的触发、重启后是否真的不重复，
  在此之前**不得**标记为端到端通过。
- 图片渲染依赖运行环境存在中文字体；缺字体时降级为文本卡片。
- 日报去重粒度为 `(群, 业务日期)`，改 `daily_report_timezone` 可能造成当天重复或跳过一次。

## [v1.2.0] - 2026-10-20

首个公开版本。

### 新增

- **四类通知**：确认重置（F01）、提前预警（F02）、重置概率预测（F03）、额外重置权益动态（F04），
  外加预告/重置更正（F07）。每条消息带独立一行 `数据来源：https://codex-reset.com/`。
- **多群订阅**：每群独立开关四类通知与 24h/48h 阈值；`/cr set` 与 WebUI `group_overrides` 卡片双向同步，
  数据库为唯一真源，卡片 `_version` 过期时不会覆盖数据库。
- **命令体系**：`/cr`（别名 `/codexreset`）单指令 + `GreedyStr` 手工分派，支持 `/cr` 裸命令等同
  `/cr status`；管理指令要求 `event.is_admin()`，订阅只能来自真实群会话。
- **SQLite 持久化**：`subscriptions / source_snapshots / logical_events / deliveries / forecast_state / meta`
  六表 + WAL；`(logical_id, state_version)` 与 `UNIQUE(subscription_id, logical_id, state_version)`
  保证跨端点、跨重启去重。
- **限速与退避**：固定 UA、请求间隔 ≥60s、轮换 `forecast→timeline→forecast→feed`、429 尊重
  `Retry-After`、失败按 2/4/8/15 分钟退避。
- **投递队列**：`pending → inflight → delivered`，失败 0/30/120s 重试后转 `failed`；发送超时/重启残留
  统一转 `uncertain`（终态，不自动重发）。
- **预测状态机**：连续 N 次达标 + 滞回（跌破 `threshold-10` 才 re-arm）+ 冷却窗口 + 每自然日上限；
  确认重置后 `epoch_seq` 前进并重新开始采样。
- **陈旧抑制**：`feed.stale` 或源端数据超过 `max_source_age_minutes` 时仍入库但不推送，查询追加
  「⚠️ 数据可能已过时」。
- **启动补发**（可选，默认关闭）：`enable_catchup=true` 时补最近 `max_catchup_hours` 内的重置/权益，
  每群每次启动最多 1 条摘要，永不补发预告与概率。
- **可观测性**：审计 JSONL（操作人 + umo 指纹 + 动作 + 结果 + UTC 时间，无消息正文）、
  `[codex-reset][模块]` 结构化日志、`/cr groups` 展示最近成功/失败与 `uncertain` 计数。

### 修复

- 更正判定：确认重置**落在预告窗口内**时不再补发「预告未获确认」更正（此前会误报一条更正）。
- **只读实例不再接受写指令**：检测到另一实例持锁时，`/cr subscribe`、`/cr unsubscribe`、`/cr set`
  一律返回「只读模式」提示且不改动任何数据（此前只读实例仍能成功订阅，造成两个实例的配置互相覆盖）。
- **异常退出不再导致「永久只读」**：`terminate()` 的 `worker.stop()`、任务取消、`client.close()`
  任一环节抛异常时，仍会在 `finally` 中写入 `runtime:shutdown_at` 并关闭数据库。此前异常退出会留下
  「心跳新鲜 + 无退出标记」的残局，下次启动会把自己误判成另一个活实例而只能手工删库恢复。
- **运行权抢占改为单事务**：`runtime:owner` 的「读心跳 → 判定 → 写 owner」收进一个
  `BEGIN IMMEDIATE` 事务（新增 `Repository.atomic_meta()`）。此前分步读写会让两个同时启动的实例
  各自认为自己是主实例（双写者）。
- **`/cr history` 只列确认重置**：排除站点把某条重置降级后产生的「已撤回」更正事件，
  避免把撤回的重置当成「已确认」展示。
- **干净退出标记带身份**：`runtime:shutdown_at` 的值改为 `"{owner}|{iso}"`，只有「记录中的
  owner 自己写的退出标记」才允许下一次启动据此接管。此前标记只是时间戳，只读实例退出后
  会让第三个实例误以为持锁实例已退出而抢先接管，导致两个实例同时写入。
- **取得所有权当次即写心跳**：此前只在采集轮次写心跳，`enable_monitor=false` 或首轮未完成时
  心跳为空，后来的实例无法判断存活状态而直接抢锁；现在取得所有权当次即写心跳，常驻任务按 30s 节流刷新，且只由持锁实例写。
- **退出时关闭失败可重试**：`repo.close()` 抛异常时不再把实例标记为「已释放」，第二次 `terminate()`
  仍会重试关闭，避免 aiosqlite 连接线程拖住进程退出；退出标记单独去重，不产生重复告警。
- **心跳写入节流**：常驻心跳最小间隔 30s（判据阈值最少 120s），避免配置监听每 5s 一拍都写一次 meta
  造成无谓的 WAL 写放大。
- **兼容上游 Brotli 响应**：请求接口时显式使用 `Accept-Encoding: identity`，避免部分运行环境的 `aiohttp`
  无法解码站点返回的 Brotli 内容，导致采集轮次一直报网络错误。

### 已知限制

- 第三方非官方数据源，接口可能变更；预测为实验性结果，不代表官方承诺。
- exactly-once 无法保证：极端情况下（发送超时/进程被强杀）可能漏一条，记录为 `uncertain`。
- 只读判定是**一次性**的：若持锁实例在只读实例启动后才崩溃，只读实例不会自愈，需要再重启一次（不会产生两个写者）。
- 只读判定是**一次性**的：若持锁实例在只读实例启动后才崩溃，只读实例不会自愈，需要再重启一次（不会产生两个写者）。
- 仅支持 `aiocqhttp` 平台的 `@全体成员`，其他平台静默省略。

[Unreleased]: https://github.com/kasumi-ppp/astrbot_plugin_codex_reset/compare/v1.3.0...HEAD
[v1.3.0]: https://github.com/kasumi-ppp/astrbot_plugin_codex_reset/compare/v1.2.0...v1.3.0
[v1.2.0]: https://github.com/kasumi-ppp/astrbot_plugin_codex_reset/releases/tag/v1.2.0
