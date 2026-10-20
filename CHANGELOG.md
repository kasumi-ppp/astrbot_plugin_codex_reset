# Changelog

本文件记录 Codex 重置雷达的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 `vMAJOR.MINOR.PATCH`。

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

### 已知限制

- 第三方非官方数据源，接口可能变更；预测为实验性结果，不代表官方承诺。
- exactly-once 无法保证：极端情况下（发送超时/进程被强杀）可能漏一条，记录为 `uncertain`。
- 仅支持 `aiocqhttp` 平台的 `@全体成员`，其他平台静默省略。

[Unreleased]: https://github.com/kasumi-ppp/astrbot_plugin_codex_reset/compare/v1.2.0...HEAD
[v1.2.0]: https://github.com/kasumi-ppp/astrbot_plugin_codex_reset/releases/tag/v1.2.0
