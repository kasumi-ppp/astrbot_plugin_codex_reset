# 验收矩阵（AC-01 ~ AC-26）

本文件与 `tests/test_acceptance_matrix.py` 的 `AC_TESTS` 字典**同源**：左侧「自动化测试节点」列必须
一一对应真实存在的测试函数，`tests/test_acceptance_matrix.py::test_every_mapped_test_function_exists`
会逐个 `import` 模块并 `hasattr` 校验，任何一处写错（改名、删除、拼错）都会让该测试失败。

- 节点写法：`模块名::函数名`，省略 `tests/` 前缀与 `test_` 模块的 `.py` 后缀。
- 一个 AC 可能被多个自动化测试覆盖，全部列出。
- **状态含义**
  - `通过` —— 自动化测试已覆盖并在 CI 中全绿。
  - `待验收（模拟测试已覆盖）` —— 真实 QQ 群 / 真实重启的行为必须在生产环境人工联调确认，
    按 PRD §3「验收纪律」，**不得**用模拟测试结果替代「通过」。

## 1. 自动化验收

| ID | 场景 | 自动化测试节点 | 状态 |
| --- | --- | --- | --- |
| AC-01 | 新站点确认 `group=reset` + `announcement_state=announced` + `confirmed=true` | `test_classifier::test_announced_confirmed_row_yields_single_event`<br>`test_templates::test_reset_template_contains_confirmation_wording` | 通过 |
| AC-02 | `group=reset` 但 `announcement_state=none`；**或** `announced` 但 `confirmed=false` | `test_classifier::test_trap_row_announced_but_unconfirmed_yields_zero`<br>`test_classifier::test_none_row_yields_zero` | 通过 |
| AC-03 | `forecast.last_reset_at` 前进，timeline 尚无新行 | `test_pipeline::test_forecast_then_timeline_does_not_duplicate` | 通过 |
| AC-04 | 同一 reset 同时出现在 feed / timeline / forecast | `test_pipeline::test_three_endpoints_same_fact_yields_single_delivery`<br>`test_integration::test_end_to_end_four_message_kinds` | 通过 |
| AC-05 | 新 `latest_alert` 为 `watch+active` | `test_watch::test_active_watch_yields_single_prewarning`<br>`test_watch::test_watch_text_is_not_a_confirmation` | 通过 |
| AC-06 | `watch+expired`，首次启动读到历史 Alert | `test_watch::test_expired_alert_with_empty_baseline_never_notifies`<br>`test_pipeline::test_first_round_baseline_records_without_pushing` | 通过 |
| AC-07 | 已发送的 Watch 过期且未出现确认 | `test_watch::test_active_then_expired_yields_single_correction`<br>`test_watch::test_window_elapsed_is_suppressed_when_reset_landed_inside_the_window` | 通过 |
| AC-08 | Watch 未提供时间窗口 | `test_watch::test_nowindow_text_uses_fixed_phrase_without_fake_hours` | 通过 |
| AC-09 | 24h 概率低于阈值 → 连续两次有效采样达标 | `test_forecast::test_two_consecutive_hits_trigger_exactly_once` | 通过 |
| AC-10 | 概率长期高位，连续轮询数十次 | `test_forecast::test_long_high_series_triggers_at_most_once` | 通过 |
| AC-11 | 预测触发但 `confidence=low` | `test_forecast::test_low_confidence_still_pushes_with_experimental_wording` | 通过 |
| AC-12 | 新 credits，状态 announced → available | `test_credits::test_credits_announced_then_available_yield_two_versions` | 通过 |
| AC-13 | 两个 QQ 群订阅不同类别 | `test_pipeline::test_two_subscriptions_only_receive_their_categories` | 通过 |
| AC-14 | QQ 群未订阅 | `test_pipeline::test_empty_subscriptions_yield_zero_deliveries` | 通过 |
| AC-15 | 群普通成员执行 `/cr subscribe` | `test_commands::test_non_admin_is_rejected_but_can_query` | 待验收（模拟测试已覆盖） |
| AC-16 | 管理员 `/cr unsubscribe` | `test_commands::test_unsubscribe_stops_deliveries_and_drops_card` | 待验收（模拟测试已覆盖） |
| AC-17 | Docker / AstrBot 重启 | `test_pipeline::test_restart_reuses_database_without_resending`<br>`test_repository::test_requeue_inflight_as_uncertain` | 待验收（模拟测试已覆盖） |
| AC-18 | 断网、502、429 `Retry-After`、错误 JSON | `test_pipeline::test_fetch_failures_never_deliver_and_backoff`<br>`test_client::test_timeout_and_network_errors_never_raise` | 通过 |
| AC-19 | 全部 API 请求 | `test_scheduler::test_two_requests_are_at_least_spacing_seconds_apart`<br>`test_scheduler::test_user_agent_is_compliant` | 通过 |
| AC-20 | 所有展示上游数据的 QQ 消息 | `test_templates::test_every_template_contains_source_line`<br>`test_templates::test_source_line_cannot_be_disabled` | 通过 |
| AC-21 | 群管理与查询指令 | `test_commands::test_rate_limit_blocks_repeat_and_never_fetches`<br>`test_pipeline::test_command_handle_never_triggers_fetch` | 通过 |
| AC-22 | `/cr test` | `test_commands::test_test_command_is_labelled_and_writes_nothing` | 待验收（模拟测试已覆盖） |
| AC-23 | 网站字段缺失或出现新枚举值 | `test_parser::test_parse_event_missing_required_fields_never_raises`<br>`test_parser::test_parse_event_unknown_group_is_preserved_but_flagged` | 通过 |
| AC-24 | 并发管理指令与轮询同时更新状态 | `test_dedupe::test_concurrent_enqueue_creates_single_row`<br>`test_dedupe::test_ten_concurrent_run_once_only_one_delivery` | 通过 |
| AC-25 | 每日预测图片：定时发送、每群每天至多一张、重启不重复、陈旧不发、降级正确 | `test_daily_report::test_daily_report_sends_once_per_group_per_day`<br>`test_daily_report::test_daily_reports_survive_reopen`<br>`test_daily_report::test_daily_report_stale_data_is_skipped_with_error`<br>`test_daily_report::test_daily_report_text_fallback_on_image_failure`<br>`test_daily_report::test_daily_report_uncertain_send_never_retries_or_falls_back` | 待验收（模拟测试已覆盖） |
| AC-26 | 群级日报开关与手动/测试图片通道 | `test_daily_report::test_daily_report_respects_group_switch`<br>`test_daily_report::test_manual_report_does_not_consume_daily_quota`<br>`test_daily_report::test_manual_report_test_flag_is_labelled`<br>`test_daily_report::test_manual_report_without_snapshot_returns_none`<br>`test_commands::test_report_command_sends_image_and_never_fetches`<br>`test_commands::test_set_daily_switch_writes_override` | 待验收（模拟测试已覆盖） |

统计：自动化 `通过` 20 条，`待验收（模拟测试已覆盖）` 6 条
（AC-15 / AC-16 / AC-17 / AC-22 / AC-25 / AC-26）。

> **v1.3.0 注意**：AC-25 / AC-26 的自动化只验证「假时钟 + 假图片发送通道」下的逻辑。
> 真实 QQ 群中的**图片能否正常显示**（OneBot 图片上传、大小限制、风控）、
> 以及**每天定时是否真的发出**、**重启后是否真的不重复**，必须在真实环境人工确认，
> 在此之前两条 AC 一律记为 `待验收`，**不得**标记为端到端通过。

## 2. 人工联调清单（真实群 / 真实重启）

以下各项必须在装好插件的真实 AstrBot 实例、真实 QQ 群中执行，并把结论（含失败样本与日志）
回填到本文档「人工验收记录」小节。任一项未做，即视为该 AC **未通过**。

| ID | 操作步骤 | 期望结果 | 状态 |
| --- | --- | --- | --- |
| AC-15 | ① 管理员 `/cr subscribe` 订阅本群；② 换一个**非管理员**群成员执行 `/cr subscribe`、`/cr unsubscribe`、`/cr set forecast off` | ② 三条指令均回复固定文案 `仅管理员可执行该指令。使用 /cr help 查看权限说明。`，且群配置无任何变化；③ 该成员执行 `/cr status`、`/cr forecast` 仍正常返回 | 待验收 |
| AC-16 | ① 管理员 `/cr subscribe` 后确认能收到通知；② 管理员 `/cr unsubscribe` | ② 回复退订成功文案；③ 之后不再收到任何自动通知；④ WebUI「群订阅覆盖」卡片中该群条目消失 | 待验收 |
| AC-17 | ① `/cr subscribe` 并等到至少一条真实通知；② `docker restart` AstrBot（保留 `plugin_data` 卷）；③ 重启后观察 30 分钟 | 订阅关系与已有投递记录保留；重启后**不**重发历史事件；启动日志出现 `bootstrap` / `requeue` 相关记录；`uncertain` 状态的投递被标记而非静默丢弃 | 待验收 |
| AC-22 | 管理员在**未订阅**群执行 `/cr test` | 仅当前会话（当前群）收到带「测试」标记的单条消息；`deliveries` 表无新增行；真实事件状态不变；随后 60s 内再次执行被限频拒绝 | 待验收 |
| AC-25 | ① 装好中文字体，`enable_daily_report=true` 且本群 `/cr set daily on`；② 等到一个 `daily_report_time`（默认 09:00）自然触发；③ 次日 09:00 前 `docker restart` 一次 | ② 该群当天收到**一张** 900×1200 预测图片，含 24h/48h 概率、最近确认重置/「暂无记录」、更新时间、置信度、`数据来源：https://codex-reset.com/`；③ 重启后当天**不再**重复发送同一张图；数据陈旧（`max_source_age_minutes`）时当天不发图且落 `skipped` 记录；缺字体时降级为文本卡片且内容一致 | 待验收 |
| AC-26 | ① 管理员 `/cr set daily on` 后 `/cr report`；② **未订阅**群 `/cr report`；③ `/cr set daily off` 后等下一个定时点；④ 管理员 `/cr test image`；⑤ 非管理员 `/cr report` | ① 立即收到图片，且**不占用**当天自动额度（定时点仍会再发一次自动图）；② 未订阅群也可用（手动通道与 `/cr test` 同语义），但**当天不会收到自动图**；③ 该群当日不再收到自动图片；④ 收到带「测试图片」标记的图片，`daily_reports` 无新增行，真实事件不受影响；⑤ 回「仅管理员可执行该指令」 | 待验收 |

## 3. 人工验收记录

| 日期 | 执行人 | AstrBot 版本 | 插件版本 | 覆盖 AC | 结论 | 备注 / 日志路径 |
| --- | --- | --- | --- | --- | --- | --- |
| （待填） | | | v1.3.0 | AC-15/16/17/22/25/26 | | |

## 3.1 已知限制（上线前请知悉）

| # | 限制 | 影响 | 规避方式 |
| --- | --- | --- | --- |
| L-1 | **只读判定是一次性的**：插件的软单实例只在启动时判定一次。若持锁实例在只读实例启动**之后**才崩溃，该只读实例不会自动转为主实例 | 该实例仍需重启一次才能恢复采集与投递；期间另一个（已崩溃的）实例本来就不在写，**不会产生两个写者**，也不会损坏数据 | 重启一次 AstrBot 即可；`/cr status` 的「最近采集」会显示长时间未更新 |
| L-2 | **exactly-once 不保证**：发送超时或进程被强杀时，该条投递记为 `uncertain`，不自动重发 | 极端情况下漏一条通知 | `/cr groups` 查看 `uncertain` 计数；`/cr history` 可人工补查 |
| L-3 | **只读实例接受查询但拒绝写指令**：`/cr subscribe`、`/cr unsubscribe`、`/cr set` 返回「只读模式」 | 需要在只读实例上改配置时会失败 | 在持锁实例上执行，或先停止多余实例 |
| L-4 | `runtime:shutdown_at` 为 V1.2 新格式 `{owner}\|{iso}`；旧格式（纯时间戳）**不被信任** | 从更早的内部测试版本升级后，首次启动可能判定为只读 | 正常重启一次即可接管（进程会写入新格式标记） |
| L-5 | **预测图片依赖本地中文字体**：容器/精简 Linux 镜像常缺 CJK 字体 | 找不到字体时该次日报降级为文本卡片，不会发送缺字图片 | 镜像里装 `fonts-noto-cjk`（或任意 CJK ttf/ttc），或接受文本降级 |
| L-6 | **图片发送结果未知时既不重发图片也不补发文本**（记为 `uncertain`） | OneBot 上传超时等场景下，极端情况当天没有收到图片 | 用 `/cr report` 手工补发；本行为与既有 `uncertain` 语义一致，是为了避免重复刷屏 |

## 4. 复现命令

```bash
cd E:/project/Astrbot/astrbot_plugin_codex_reset

# 全量测试（含验收矩阵静态核对）
E:/Anaconda3/python.exe -m pytest tests -q

# 只跑验收矩阵
E:/Anaconda3/python.exe -m pytest tests/test_acceptance_matrix.py -v

# 代码风格
E:/Anaconda3/python.exe -m ruff check services tests main.py
```
