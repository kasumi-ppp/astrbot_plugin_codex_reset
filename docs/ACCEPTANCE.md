# 验收矩阵（AC-01 ~ AC-24）

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

统计：自动化 `通过` 20 条，`待验收（模拟测试已覆盖）` 4 条（AC-15 / AC-16 / AC-17 / AC-22）。

## 2. 人工联调清单（真实群 / 真实重启）

以下四项必须在装好插件的真实 AstrBot 实例、真实 QQ 群中执行，并把结论（含失败样本与日志）
回填到本文档「人工验收记录」小节。任一项未做，即视为该 AC **未通过**。

| ID | 操作步骤 | 期望结果 | 状态 |
| --- | --- | --- | --- |
| AC-15 | ① 管理员 `/cr subscribe` 订阅本群；② 换一个**非管理员**群成员执行 `/cr subscribe`、`/cr unsub`、`/cr set forecast off` | ② 三条指令均回复固定文案 `仅管理员可执行该指令。使用 /cr help 查看权限说明。`，且群配置无任何变化；③ 该成员执行 `/cr status`、`/cr forecast` 仍正常返回 | 待验收 |
| AC-16 | ① 管理员 `/cr subscribe` 后确认能收到通知；② 管理员 `/cr unsubscribe` | ② 回复退订成功文案；③ 之后不再收到任何自动通知；④ WebUI「群订阅覆盖」卡片中该群条目消失 | 待验收 |
| AC-17 | ① `/cr subscribe` 并等到至少一条真实通知；② `docker restart` AstrBot（保留 `plugin_data` 卷）；③ 重启后观察 30 分钟 | 订阅关系与已有投递记录保留；重启后**不**重发历史事件；启动日志出现 `bootstrap` / `requeue` 相关记录；`uncertain` 状态的投递被标记而非静默丢弃 | 待验收 |
| AC-22 | 管理员在**未订阅**群执行 `/cr test` | 仅当前会话（当前群）收到带「测试」标记的单条消息；`deliveries` 表无新增行；真实事件状态不变；随后 60s 内再次执行被限频拒绝 | 待验收 |

## 3. 人工验收记录

| 日期 | 执行人 | AstrBot 版本 | 插件版本 | 覆盖 AC | 结论 | 备注 / 日志路径 |
| --- | --- | --- | --- | --- | --- | --- |
| （待填） | | | v1.2.0 | AC-15/16/17/22 | | |

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
