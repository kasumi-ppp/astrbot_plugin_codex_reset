# Codex 重置雷达 V1.2 — 独立 QA 报告

> 执行人：QA 工程师（严过关）｜执行日期：2026-10-20
> 仓库：`E:/project/Astrbot/astrbot_plugin_codex_reset`｜解释器：`E:/Anaconda3/python.exe`
> 环境事实：Python 3.12 / 无 `pytest-asyncio` / 无 `astrbot`（`main.py` 用最小替身注入后实例化验证）
> 判定依据：`docs/PRD-confirmation.md`（AC-01~24、D-01~D-12、命令表、限频表、模板）、`docs/architecture.md`（§A.4、§C.3、§D、§H、§J）、`docs/ACCEPTANCE.md`
> **本报告不复述工程师的测试结论**：下文所有数字、缺陷与结论均由 QA 独立执行/独立断言得出。

---

## 0. 修复回归（第二轮）

> 执行：主理人指派工程师修复 ｜ 范围：P1-01/02、P2-01~08、附加 A/B/C ｜ 门禁：`pytest tests -q` = **421 passed / 0 xfailed**（三轮收尾后）、`ruff check services tests main.py` = All checks passed、`grep -rn "import astrbot" services/` 无输出

4 处 `@pytest.mark.xfail` 标记**全部删除**（P2-01、P2-02、P2-03 各 1 处，P1-02 1 处），转为普通回归用例。下表逐条对应「修复位置 → 守护测试 → 状态」。

| 编号 | 修复文件:行 | 对应测试 | 状态 |
|---|---|---|---|
| **P1-01** | `services/pipeline.py:314-319`（`ingest` 内接线）、`:504-515`（`_force_reason`） | `test_pipeline.py::test_confirmed_reset_forces_timeline_on_next_slot`、`::test_new_watch_forces_timeline_on_next_slot`、`::test_baseline_round_does_not_force_timeline`、`::test_watch_correction_does_not_force_timeline`；`test_integration.py` 轮次序列已按强制槽位重排并新增「槽位断言」 | `已修复` |
| **P1-02** | `services/notifications.py:498`（`DEFAULT_SEND_TIMEOUT_SECONDS=30`）、`:520-531`（`send_timeout` 注入）、`:605-616`（`asyncio.wait_for` + `uncertain`） | `test_qa_task_lifecycle.py::test_hung_send_should_become_uncertain_instead_of_stalling`（去掉 xfail；注入 `send_timeout=0.05`，并新增断言「同批后续 pending 仍被处理」） | `已修复` |
| **P2-01** | `services/parser.py:445-447`（整键缺失 → `_warn`；`null` 保持静默） | `test_qa_degradation.py::test_latest_alert_key_absence_should_be_reported`（去掉 xfail） | `已修复` |
| **P2-02** | `services/notifications.py:133-146`（删除重复实现，直接复用 `parser.safe_url`） | `test_qa_degradation.py::test_urls_with_whitespace_or_control_chars_should_be_omitted`（去掉 xfail；含换行注入「数据来源：」只允许 1 处） | `已修复` |
| **P2-03** | `services/repository.py:70`（`idx_logical_kind_time`）、`:638-667`（`find_logical_by_time`）；`services/pipeline.py:484-495`（`_normalize` 二次兜底） | `test_qa_cross_api.py::test_cross_round_one_second_skew_should_still_be_one_delivery`（去掉 xfail）、`test_repository.py::test_find_logical_by_time_matches_within_tolerance`、`::test_find_logical_by_time_returns_earliest_candidate`、`::test_logical_kind_time_index_exists` | `已修复` |
| **P2-04** | `services/repository.py:1022-1038`（`has_delivered` 只认 `delivered`）；`services/pipeline.py:541-549`（更正过滤）、`:517-524`（`_is_watch_correction`） | `test_pipeline.py::test_watch_correction_only_reaches_groups_that_got_the_watch`（A 收到预告、B 后加入 → 更正只入队 A）、`test_repository.py::test_has_delivered_only_accepts_delivered_rows`、`::test_has_delivered_ignores_uncertain_rows` | `已修复` |
| **P2-05** | `services/pipeline.py:652-670`（`built` 只要求 timeline 成功）；`services/commands.py:300`、`:306-331`（`_diagnostic_lines` 两行诊断） | `test_pipeline.py::test_baseline_builds_from_timeline_alone`、`test_commands.py::test_status_reports_missing_baseline`、`::test_status_reports_forecast_endpoint_failure`、`::test_status_has_no_diagnostic_lines_when_healthy` | `已修复` |
| **P2-06** | `main.py:762-812`（先分组 → 组内按 `_version` 取最大 → 再比 DB 版本）、`_card_version()`（缺失/非法/`bool` → -1） | `test_qa_task_lifecycle.py::test_apply_config_cards_handles_stale_unknown_and_duplicate` 新增 E 段（过期卡在前、新卡在后必须生效）+ `test_schema.py` AST 断言 | `已修复` |
| **P2-07** | `main.py:79-85`（`runtime:owner` / `health:` 键）、`:130-143`（`on_round` 写端点健康）、`:424-455`（`_claim_runtime_owner`：他实例 + 心跳 < 2×spacing → 只读 + ERROR）、`initialize` 只读分支保持读取可用 | `test_schema.py::test_main_py_declares_soft_single_instance_health_and_hot_reload`、`::test_main_py_single_instance_and_watch_are_ast_wired`（`main.py` 依赖 astrbot，本地无框架只能做静态/AST 守卫，真实双开行为仍待 C2 联调） | `已修复（静态守卫）` |
| **P2-08** | `docs/PRD-confirmation.md` D-03 追加注、`README.md` §1 F04 行追加注（`banked_grant` 仅展示） | 无（文档级，代码本就只判 `banked_state`） | `文档说明` |
| **A** | `services/classifier.py:264-276`（`_is_confirmed_reset_alert` 增加 `and not alert.corrected`） | `test_classifier.py::test_corrected_reset_alert_is_not_a_new_confirmation`（含未更正时的正例） | `已修复` |
| **B** | `services/scheduler.py:153-167`（`set_spacing`，并重算正常槽位 `_next_due_at`，退避中不动）；`services/pipeline.py:360-366`（每次判定按当前全局配置重建 `ForecastEvaluator`）；`main.py:476-545`（`_config_watch_loop` / `_reload_config_if_changed` / `_sync_poll_task`，sha1 摘要避免自写回环） | `test_scheduler.py::test_set_spacing_takes_effect_on_next_slot`、`::test_set_spacing_clamps_to_at_least_one_second`、`test_pipeline.py::test_evaluate_all_uses_current_global_settings`（构造时注入 `confirm_samples=99`，仍按 plugin 的 2 判定；热改 1 后立即生效） | `已修复` |
| **C** | `main.py:250-253`、`:524-528`（`admin_only_subscription=false` 启动 warning）；`services/parser.py:541`（`feed.stale` 缺失）、`:578`（`feed.events` 缺失）同口径告警 | `test_schema.py` 源码断言 + `test_parser.py` / `test_qa_degradation.py` 既有告警断言 | `已修复` |

**回归结论**：**421 项全绿、0 xfailed**（工程师新增 20 项守护用例 + QA 二轮新增 19 项独立验证用例 + 三轮 8 项缺陷守护，共 47 项；既有 374 项零回归），`ruff` 全绿，`services/**` 零 astrbot 导入，`-X dev` 下 0 warnings。8 项 P2 中 **7 项为代码修复、1 项（P2-08）为文档说明**；P2-07 因 `main.py` 依赖 AstrBot 运行时，本地只能以 AST/源码守卫锁定，真实双开与热重载时序仍列入 §5 的人工联调。

> 基线口径：冷启动实测 **370 passed + 4 xfailed = 374 collected**（1 项 P1 + 3 项 P2 均带 `xfail`）。二轮把这 4 个标记全部删除并转为普通回归用例；三轮再补 8 项。因此门禁从「370/374 通过 + xfail」变为「421 全绿、0 xfail、0 skip」。

> 二轮收尾增补（主理人复核时新发现，已一并修复并纳入上表数字）：① `/cr history` 原先返回全部事件（含 Watch/预测），已收窄为 `group_kind="reset"` 并改文案为「确认重置」；② 软单实例在插件热重载时存在**误判风险**——旧实例心跳仍新鲜会让新实例永久只读，已引入 `runtime:shutdown_at` 干净退出标记 + 同 pid 检测，且只读实例不创建任何投递任务（`test_qa_task_lifecycle.py` 新增 2 项守护）；③ 「新 Watch / 确认重置后下一槽位强制 timeline」的用例原先预置响应不全（强制后下一拍实际是 `feed`），已补 `feed` 响应并断言完整轮换序列 `["forecast","timeline","feed"]`。

### 0.2 三轮：QA 复验发现的问题（P1-03 / P2-09~11 / O-7）与修复

QA 对上述 3 处收尾改动做了只读复验，判定为「1 项 PASS、1 项 FAIL、1 项 PASS」并新增 4 个缺陷。**全部已修复**，门禁从 413 升至 **421 passed / 0 failed / 0 xfailed / 0 warnings**。

| 编号 | 问题（QA 判定） | 修复 | 守护用例 |
|---|---|---|---|
| **P1-03** | `terminate()` 的 `worker.stop()`/`gather()`/`client.close()` 不在 `try/finally` 内：任何一步抛异常都会跳过「写 `runtime:shutdown_at` + 关库」，留下「心跳新鲜 + 无标记」的残局 → 下次启动把自己误判成活实例，**永久只读**，只能手工删库恢复 | `main.py:364-404`：`terminate` 改为 `try/finally`，`finally` 调新增的 `_release_runtime()`（写标记 + `repo.close()`，各自独立 try 兜底） | `test_qa_task_lifecycle.py::test_terminate_writes_shutdown_marker_even_if_worker_stop_raises`（把 `_worker` 换成 `stop()` 必抛的对象，用独立 `sqlite3` 句柄确认标记已落库） |
| **P2-09** | 站点把某条 reset 降级时，`reset_corrections` 写的更正事件 `group_kind` 仍是 `reset`（`state_version="downgraded:announced"`）→ `/cr history` 把**已撤回**的重置当成「已确认」列出，用户可见误导 | `services/repository.py:746-775`（`list_events(exclude_corrections=True)` → SQL `state_version NOT LIKE 'downgraded%'`）；`services/commands.py:413-415` history 走该开关 | `test_commands.py::test_history_excludes_non_reset_and_downgraded_rows`（更晚发生的 watch/credits/forecast + 降级更正都必须不出现）、`test_repository.py::test_list_events_excludes_downgraded_corrections`（同时断言「未过滤时确实包含」以证明过滤器有效而非空转） |
| **P2-10** | 只读实例仍可执行写指令：实测只读实例 `/cr subscribe` 返回「✅ 订阅成功」，subscriptions 0→1 | `services/commands.py:70-78`（`WRITE_COMMANDS={subscribe,unsubscribe,set}` + `READONLY_TEXT`）、`:176-196`（注入 `readonly_provider`）、`:211-214`（权限判定之后、限频之前拦截，返回 `error="readonly"`）；`main.py:322` 传 `lambda: self._readonly` | `test_commands.py::test_readonly_instance_rejects_write_commands_but_allows_queries`、`test_qa_task_lifecycle.py::test_live_other_instance_forces_readonly_without_delivery`（追加断言：owner 未被改写 / deliveries==0 / 写指令 `error=="readonly"`） |
| **P2-11** | 抢占非原子：三次独立 `meta_get` + 一次 `meta_set`，两个实例会各自读到「无主」快照而**双双持锁**（实测两实例 `readonly` 均为 False，后到者覆盖了 owner） | `services/repository.py:277-296`（新增 `atomic_meta()`：`BEGIN IMMEDIATE` + `MetaView.get/set/delete` 共用同一连接）；`main.py:455-500`（`_claim_runtime_owner` 全程在单个 `atomic_meta()` 内完成判定与写入，只读分支提前 return） | `test_repository.py::test_atomic_meta_rolls_back_on_exception`、`::test_atomic_meta_reads_see_writes_of_same_transaction`、`test_qa_task_lifecycle.py::test_claim_runtime_owner_does_not_nest_meta_transactions`（把「不得再走 `meta_set`」变成可执行约束）、`::test_second_instance_cannot_overwrite_live_owner` |
| **O-7** | 全量运行偶发 `Event loop is closed` | `-X dev` 定位到唯一泄漏源：`tests/test_qa_false_positive.py` 两个集成用例直接调 `make_env()` 却未 `close()`（2 条 aiosqlite 连接延迟到 GC 回收）。已改 `try/finally: await repo.close()` | `python -X dev -m pytest tests -q` → **421 passed、0 warnings** |

> 原子性另做了并发实证（脚本级，非用例）：实例 A 在 `atomic_meta()` 内写 owner 后 `sleep(0.15s)`，实例 B 的 `atomic_meta()` 被写锁阻塞，读到的是 A 已提交的值（`111:aaaa`）而非 `None` —— 旧的分步实现下 B 会读到旧值。该实证与 `test_second_instance_cannot_overwrite_live_owner` 互为表里。


### 0.1 QA 独立复验（不采信工程师结论）

复验方法：不看修复说明，按 PRD 条款重写断言；新增用例与工程师用例**零复用**（独立工厂 + 独立断言），并直接读源码确认「代码真的改了」，而非只相信测试变绿。

| 缺陷 | 代码级确认 | 独立复验结果 |
|---|---|---|
| **P1-01** | `services/pipeline.py:314-319` 已接线；`_force_reason`（`:504-515`）区分 reset / 新 watch / 更正 | 新增 `tests/test_qa_force_timeline.py` **17 项**：强制槽位优先于轮换、**不推进轮换指针**、只生效一次、未满 `spacing` 不插队、退避不被取消、`feed` 端点同样触发、更正/credits/forecast 不触发、同一 watch 不重复强制、`baseline_only` 不触发、热改 `spacing` 不丢排队 → **全部通过**；调度日志实测出现 `force_timeline reason=confirmed_reset` / `reason=new_watch` |
| **P1-02** | `services/notifications.py:498/520-531/605-616`：`asyncio.wait_for(..., timeout=self._send_timeout)` + `TimeoutError → mark_uncertain` | 原 xfail 用例转正并通过；注入 `send_timeout=0.05` 的「永不返回」`SendFn` 后该行落 `uncertain`，且**同批后续 pending 仍被处理** |
| **P2-01** | `services/parser.py:445-447`：整键缺失 `_warn`，`null` 保持静默 | 原 xfail 转正通过（缺键有告警 / `null` 无告警两向断言都在） |
| **P2-02** | `services/notifications.py`：删除重复实现，`_safe_url` 直接 `return safe_url(value)` | 原 xfail 转正通过；工程师另加了「含 `\n` 的 URL 不得注入第二行 `数据来源：`」断言（比原断言更严） |
| **P2-03** | `services/repository.py`：`find_logical_by_time` + `idx_logical_kind_time`；`services/pipeline.py:484-495` 二次兜底 | 原 xfail 转正通过（跨轮 1s 偏差仍只 1 条投递） |
| P2-04/05/06/07 与 A/B/C | 逐条读源码确认（位置见上表） | 由工程师新增的守卫用例覆盖；`_is_watch_correction`（`:517-524`）、`_advance_baseline` 只要求 timeline（`:652-670`）、`_claim_runtime_owner`（`main.py:424-455`）与报告建议一致，未发现「假修复」 |

> 复验结论：**4 个 xfail 全部真实转正，未出现「放宽断言换绿」**（抽查被改动的 4 个用例，断言强度持平或更强）。二轮唯一新发现是观察项 **O-6**（强制不消费轮换槽位 → 偶发连续两次 timeline），按架构 §D 裁定为**不是缺陷**。

---

## 1. 执行摘要

| 项 | ① 基线（QA 介入前，冷启动） | ② QA 初判（带 xfail） | ③ 二轮修复后 | ④ **终态（交付门禁）** |
|---|---|---|---|---|
| `pytest tests -q` | **236 passed** / 17.96s | 370 passed + 4 xfailed / 29.37s | 413 passed / 6.37s | **421 passed / 7.73s**，0 xfail、0 skip、0 error、**0 warnings**（`-X dev` 亦 0） |
| 其中工程师用例 | 236 | 236 passed（零回归） | 256 passed | **256 passed**（+20 项二轮守护，既有 236 项零回归） |
| 其中 QA 独立用例 | — | 138 collected（134 passed + 4 xfailed） | 157 passed | **165 passed**（+8 项三轮缺陷守护：repository 3、commands 2、lifecycle 3） |
| `ruff check services tests main.py` | All checks passed | All checks passed | All checks passed | **All checks passed** |
| `grep -rn "import astrbot" services/` | 无输出 | 无输出 | 无输出 | **无输出**（分层纪律保持） |

- 通过率：**421 / 421 = 100%**，无 `xfail`、无 `skip`、无 error。历史上那 4 项 `xfail`（1 项 P1 + 3 项 P2，详见 §3）在缺陷修复后已全部删除标记、转为普通回归用例——**不是**靠 XPASS 变绿。
- 代码风格：QA 新增 9 个文件全部通过 `ruff`（line-length 110 / py312 / E,F,W,I,B,UP）。
- 未放宽任何断言：转正时逐条比对过断言强度（持平或更强）；缺陷仍存在时所有 `xfail` 都写了「按 PRD 应当如何」的断言并用 `reason` 指向缺陷编号，**没有**把实现现状当预期。
- 用例分布（`--collect-only` 实测）：QA 侧 `qa_degradation` 34 / `qa_false_positive` 31 / `qa_permissions` 28 / `qa_task_lifecycle` 20 / `qa_force_timeline` 17 / `qa_rate_limit` 16 / `qa_history_flood` 8 / `qa_cross_api` 6 = 165；工程师侧 `repository` 36 / `parser` 26 / `commands` 22 / `config` 21 / `templates` 20 / `watch` 19 / `pipeline` 19 / `classifier` 18 / `forecast` 17 / `dedupe` 16 / `scheduler` 12 / `client` 12 / `schema` 10 / `credits` 8 / `acceptance_matrix` 3 / `integration` 2 = 256。

---

## 2. 新增独立测试清单

断言全部从 PRD 条款反推，payload 由 QA 自建工厂（`tests/qa_helpers.py`）产出上游**原始 JSON**，再喂给真实的 `parser → classifier → pipeline → repository → notifications`，不复用工程师的构造器与断言写法。

| 文件 | 用例数 | 覆盖 AC / 条款 | 关键断言 |
|---|---|---|---|
| `tests/test_qa_false_positive.py` | 31 | AC-01、**AC-02**、D-01、D-02、D-03、F04 | 合规行必须恰好 1 事件（防"全杀假绿"）；`announcement_state=none`、`announced+confirmed=false`、缺字段、`preview=true`、`group=boost/unlock/weird/RESET`、摘要含 reset、`confirmed` 非布尔 → **零 reset 事件**；`kind=reset/state=active`、`kind=watch/state=confirmed` → 零事件；credits 三态各 1 条、unknown/缺失/污染 0 条；集成口径整批陷阱行 **0 投递 0 事件** + 唯一真实行仍投 1 条 |
| `tests/test_qa_history_flood.py` | 8 | **AC-06**、**AC-17**、AC-04（反向）、F08 子项、Q-4 | 空库首轮 6 条历史 confirmed reset → 0 投递且 `baseline_only=True`；第二轮仅新增 1 条 → 恰好 1 条；feed/forecast 再报同一批仍 1 条；重启 `bootstrap` 0 重发；事件早于 `subscribed_at` → 0 投递；补发每群 ≤1 条摘要且**只含 reset/credits**（Watch 被排除）；超 `max_catchup_hours` 不补 |
| `tests/test_qa_cross_api.py` | 6 | **AC-03**、**AC-04**、D-08、架构 §C.3 | forecast 先给 `last_reset_at`（无 alert id）→ 1 条；timeline 同时间事件 → 仍 1 条且 canonical 唯一、`reset:{id}` 已登记为该 canonical 的别名；feed 同 id → 仍 1 条；`latest_alert` 同源 id → 仍 1 条；同轮两条不同 id 指向同一事实 → 只保留 canonical；同快照重放 3 次（不同 batch）→ 1 条 |
| `tests/test_qa_permissions.py` | 28 | **AC-15**、**AC-16**、AC-20、AC-21、**AC-22**、PRD §4 | 6 个管理指令对非管理员全部 `forbidden` 且**配置零变化**、随后查询仍正常；6 个查询指令非空 + 含 `数据来源` 署名；`/cr groups` 群内只回提示、私聊才给群列表；私聊 `subscribe` 被拒且 0 订阅；订阅→退订卡片同步/移除；`/cr test` 不写 deliveries/logical_events；`/cr set` 6 类非法输入全部拒绝且不落覆盖、设回默认值回到"继承"；6 个查询总耗时 < 2s 且 ReadModel 无 client 属性（零请求） |
| `tests/test_qa_task_lifecycle.py` | 17 | 架构 **§A.4**、§J 修正②、AC-19 门禁、AC-21 | AST 断言：`terminate` 必含 `cancel()`+`gather(return_exceptions=)`、`initialize` 幂等、`_send` 兜底 Exception、`/cr` 的 `GreedyStr` **无默认值**、`_apply_config_cards` 读 `_version`、`services/**` 零 `astrbot` 导入；替身注入后真实实例化：`initialize→terminate` 后 **无未完成任务**、双次调用幂等；`@全体` 只在 aiocqhttp+at_all 时注入 `At(qq="all")`；异常→False、TimeoutError→抛出；卡片未知 id/过期版本/重复 id 处理；`enable_monitor=false` 时 `poll_round` 返回 None 且 **client 调用 0 次**；`SendFn` 挂起时 `run_once` 不会被内部超时收敛（P1-02 守护，已转正）；**新增 2 项软单实例守护**：他 pid 留下的干净退出标记必须允许接管、他实例心跳新鲜时本实例必须只读且**不创建投递任务** |
| `tests/test_qa_degradation.py` | 34 | **AC-23**、AC-20 | 必填缺失→`schema_error`；概率越界/非整数→收敛+告警；坏行跳过+告警；`confirmed` 缺失→告警+fail-closed；`announcement_state`/`banked_state` 缺失→按 D-01/D-03 静默 fail-closed；枚举全污染不崩且不被当 reset；`javascript:`/`data:`/`ftp:`/无 host/含空格/超长 URL 全部丢弃；渲染省略「原始…」行但保留署名；**1000 条事件解析+判定 < 1s**；彻底垃圾 payload 全部 `schema_error` 不抛 |
| `tests/test_qa_rate_limit.py` | 16 | PRD §4 限频表、§4.5 | 12 个命令逐项验证 `limit-0.1s` 拒绝（剩余秒数向上取整）/`limit+0.1s` 放行；被拒那次**不刷新**计时起点；限频键 `(umo, 子命令)` 隔离；`handle` 命中时回「请等待 N 秒后再试」且不查库；越权拒绝发生在限频之前（连点 3 次仍回越权文案）；`/cr test` 60s 冷却 |
| `tests/test_qa_force_timeline.py` | 17（QA 二轮新增，专测 P1-01 修复） | PRD §2「下一槽位强制 timeline」、**AC-19**、AC-18、架构 §D | 强制的下一槽位必须是 `timeline`（轮换本应给 `feed`/`forecast` 时也一样）、强制**不推进**轮换指针、只生效一次（不会连插两条）、未满 `spacing` 仍返回 None（不得插队）、退避中不取消冷却且排队的强制在退避结束后生效、`feed` 端点出现的确认重置同样触发、`kind=watch` 带 `reason`（更正）与 `credits`/`forecast` 不触发、同一 watch 重复出现不重复强制（不饿死预测端点）、`baseline_only` 首轮不触发、热改 `spacing` 不丢排队槽位、`_force_reason` 无副作用 |
| `tests/qa_helpers.py` | —（工厂，非用例） | — | 上游原始 JSON 构造器 + 临时环境装配（`env()` 上下文管理器负责关闭仓库，避免 aiosqlite 线程悬挂） |

---

## 3. 缺陷清单

> 级别定义：**P0** 误报/刷屏/数据损坏/崩溃；**P1** 明确 PRD 功能缺失或健壮性缺口（影响时效/可恢复性）；**P2** 契约漂移、诊断/纵深防御缺口、文档级偏差。

### P0：无

### P1（2 项）

#### P1-01 采集轮换「强制 timeline」未接线

- **位置**：实现 `services/scheduler.py:147-151`；调用点缺失：`services/pipeline.py:210-234`（`poll_round`）、`main.py`（全文无调用）
- **复现**：`grep -rn "force_timeline" services/ main.py` → 只有定义与文档字符串，无任何业务调用；`tests/test_scheduler.py:49` 只测了调度器自身
- **PRD 依据**：§2「采集轮换：…**出现新 Watch 或确认重置后，下一槽位强制 `timeline`**」
- **期望**：确认重置事件（或新 watch/active）入队后，下一次 `next_endpoint()` 返回 `timeline`
- **实际**：始终按 `forecast→timeline→forecast→feed` 轮换，timeline 可能延后 1–3 个槽位（每槽 ≥60s）
- **影响**：AC-03/AC-04 的「timeline 补齐」变慢（预测已发、时间线晚到）；不产生误报（`reset:ts` 别名兜底），也不刷屏 → 判定为时效性缺陷
- **建议**：`Pipeline.ingest` 的 `new_events` 中出现 `kind in {"reset","watch"}` 且 `not baseline_mode` 时调用 `self._scheduler.force_timeline("confirmed_reset")`

#### P1-02 投递发送无超时保护，可能整条投递链停摆

- **位置**：`services/notifications.py:599`（`accepted = await self._send(task.umo, text, at_all)`，全文无 `asyncio.wait_for`）
- **复现（已实测）**：`grep -c wait_for services/notifications.py` → 0。构造一条 pending 投递并注入「永不返回」的 `SendFn`，用 `asyncio.wait_for(run_once, 0.5)` 强杀 → `TimeoutError`，且该行**永久停在 `inflight`**（连 `uncertain` 都没有写），直到进程重启才被 `requeue_inflight_as_uncertain` 兜底
- **PRD 依据**：F08「投递失败 / 超时 / 重启恢复 … 超时/中断记 `uncertain` 不自动重发」
- **期望**：单条发送超时（建议 30s）→ `mark_uncertain(timeout)`，循环继续
- **实际**：只有平台自身抛 `TimeoutError` 才进 uncertain；若适配器静默挂起，`run_once` 与整个 `delivery_loop` 阻塞，后续消息全部积压（`poll_loop` 独立仍存活，故 `/cr status` 看起来正常，只有 `/cr groups` 的 pending 数会涨）
- **建议**：`await asyncio.wait_for(self._send(...), timeout=30)`，`TimeoutError` 分支复用现有 `mark_uncertain`
- **对应测试**：`test_qa_task_lifecycle.py::test_hung_send_should_become_uncertain_instead_of_stalling`（xfail）

### P2（8 项）

#### P2-01 `forecast.latest_alert` 整键缺失不记录告警

- **位置**：`services/parser.py:177-181`（`raw is None → return None, ()`）
- **依据**：AC-23「字段缺失 … 记录告警」+ OpenAPI `Forecast.required` 含 `latest_alert`
- **期望**：key 不存在 → `warnings` 非空（`"latest_alert" not in payload` 可区分 null 与缺失）
- **实际**：静默返回 `None`，`warnings` 为空（`null` 时应保持静默，缺失时应告警）
- **对应测试**：`test_qa_degradation.py::test_latest_alert_key_absence_should_be_reported`（xfail）

#### P2-02 渲染层 `_safe_url` 不拒绝空白/控制字符（与 parser 不一致）

- **位置**：`services/notifications.py:133-146` vs `services/parser.py:84-104`（后者有 `char.isspace()/ord<0x20` 检查，前者没有）
- **复现**：`render_watch({"window": None, "url": "https://exa mple.com/x"}, ...)` 仍输出「原始消息：https://exa mple.com/x」；URL 含换行时可在消息体内注入第二行「数据来源：」
- **期望**：无效链接一律省略「原始…」行（PRD §6 通用规则 1）
- **实际**：含空格的 URL 被放行；含 `\n` 的 URL 可伪造署名行
- **可达性**：当前所有入队 payload 的 url 都已经 `parser.safe_url` 清洗，属**纵深防御缺口**（旧库 payload / 补发摘要 / 未来新增入口）
- **建议**：`notifications` 直接复用 `parser.safe_url`
- **对应测试**：`test_qa_degradation.py::test_urls_with_whitespace_or_control_chars_should_be_omitted`（xfail）

#### P2-03 跨轮 1s 抖动的同一 reset 未归并（AC-03 意图）

- **位置**：`services/classifier.py:39`（`RESET_TS_TOLERANCE=2s`）、`services/classifier.py:229-243`（容差只在**同轮**生效）、`services/classifier.py:268-283`（别名是 `reset:ts:{ISO}` 精确字符串）
- **复现**：轮 1 `forecast.last_reset_at=06:33:48Z` → `reset:ts:…48+00:00`；轮 2 timeline `announced_at=06:33:49Z` → 第二个 canonical → **2 条投递**
- **期望**：同一事实（AC-03）在 timeline 补齐后不得再发一条
- **实际**：2 条（`UNIQUE(subscription_id, logical_id, state_version)` 因 logical_id 不同而无法兜底）
- **备注**：架构 §J「关键风险①」已预判；真实样本两来源秒级完全一致，故当前风险低
- **建议**：别名查询失败时，对最近 N 分钟内的 `reset:ts:*` 做 ±2s 容差匹配再登记别名
- **对应测试**：`test_qa_cross_api.py::test_cross_round_one_second_skew_should_still_be_one_delivery`（xfail）

#### P2-04 F07 更正未按「仅发给原消息已送达的群」过滤

- **位置**：`services/pipeline.py:486-501`（`_enqueue_for_subscriptions` 遍历所有订阅，未查 `deliveries` 中原 watch 是否 delivered）
- **依据**：F07 幂等列「仅发给原消息已送达的群」
- **期望**：只入队给原 watch 已送达的群（`has_delivery(sub, watch_logical_id)` 且状态 delivered）
- **实际**：任何开启 `enable_watch` 的群都会收到「此前的重置预告未获确认」，包括从未收到预告的新订阅群
- **影响**：轻微困惑，不构成刷屏（每个 `(logical_id, reason)` 只发 1 次）

#### P2-05 基线建立依赖 forecast，长期失败会导致「永久零通知」

- **位置**：`services/pipeline.py:600-609`（`built = forecast.ok and timeline.ok`）
- **复现**：让 forecast 端点持续 5xx（timeline 正常），`baseline.built` 永远为 False → 后续所有确认重置都按 `baseline_mode` **只入库不投递**
- **期望**：任一方向长期失败应至少有一次性 ERROR/告警（PRD F10 允许 `/cr status` 附一行只读诊断）
- **实际**：静默零通知；`/cr status` 只显示"最近采集/陈旧"，看不出"基线未建立"
- **建议**：`built` 允许 timeline 单侧建立（timeline 已能独立证明 reset），或在 `/cr status` 增加「基线未建立（forecast 未成功）」一行

#### P2-06 `_apply_config_cards` 的重复 id 判定早于版本判定

- **位置**：`main.py:552-570`（`seen.add()` 在版本检查之前）
- **复现**：卡片列表 `[{sub_id, _version: 0}, {sub_id, _version: 1}]` → 第一张过期卡占住 `seen`，第二张（更新的）被当成"重复"丢弃 → 这一轮**没有任何**覆盖生效
- **期望**：过期卡片不影响同一 sub 的后续卡片（应先比版本再判重复，或按 sub_id 取最新版本）
- **实际**：丢失一次同步机会；下一次启动 `_project_cards` 会按 DB 重投影，最终态自洽
- **影响**：低（同一 sub 出现多张卡片本身是手工编辑/竞态产物）

#### P2-07 软单实例与端点健康 meta 未实现

- **位置**：`main.py:92` 只写 `runtime:heartbeat_at`；全文无 `runtime:owner` / `health:{endpoint}`（架构 §A.4、§D meta 键表）
- **期望**：启动写 `runtime:owner`，心跳 <2×spacing 的他实例 → 只读模式 + ERROR，`/cr status` 可诊断
- **实际**：无单实例检测；`/cr status` 的降级行只有陈旧提示
- **影响**：双开时不会重复推送（`deliveries` UNIQUE + 原子 claim 兜底），但缺少可观测性与互斥提示

#### P2-08 PRD 内部冲突：F04 的 `banked_grant==true` vs AC-12 的样本行

- **位置**：`services/classifier.py:534-581`（`credit_candidates` 只判 `banked_state`，从不读 `banked_grant`）
- **冲突事实**：AC-12 指定的目标行 `2107975486798164736`（`available`）的 `banked_grant=false`；另有 4 行 `banked_grant=false` 但 `state ∈ {arriving, available}`（`2095979536043401428`、`2090964822422949999`、`2090947196107764189`、`2097752790177370535`）
- **结论**：若严格实现 F04 的 `banked_grant==true`，AC-12 将无法通过 → **当前实现（只判 banked_state）是正确的取舍，不建议修改**，但应在 PRD/README 注明该字段不参与判定

### 观察项（不算缺陷，已按裁定确认）

| # | 项 | 位置 | 裁定依据 |
|---|---|---|---|
| O-1 | `announcement_state` 缺失不告警 | `services/parser.py:267-279` | D-01：缺失即 fail-closed，40 条归档行若告警会污染每条日志 |
| O-2 | `banked_state` 缺失不告警 | `services/parser.py:281-290` | D-03「缺失 → unknown，静默入库」 |
| O-3 | `feed.stale` / `feed.events` 缺失静默降级 | `services/parser.py:527-566` | 两者缺失时 `stale=False`、`events=()` 属安全降级；建议纳入 P2-01 一并补告警 |
| O-4 | `admin_only_subscription` 未参与任何判定 | `services/commands.py`（无引用） | PRD §4.1 与 U-2：V1.2 仍只放行 `is_admin`，字段仅"预留"；建议启动时记一行日志 |
| O-5 | `credits_state` 更正模板分支无生产者 | `services/notifications.py:364-369` | 保留分支无害 |
| O-6 | **强制 timeline 不消费轮换槽位 → 可能出现连续两次 timeline** | `services/scheduler.py:97-110`（`_pending` 分支不推进 `_pointer`） | 与架构 §D「强制只插入一次性优先级，不破坏轮换指针」一致，故不算缺陷；副作用是 `forecast → 强制 timeline → 轮换 timeline` 连采两次（工程师因此重排了 `test_integration.py` 的轮次序列）。若后续想要「强制即占用当轮槽位」，应改架构文档后再动代码；`test_qa_force_timeline.py::test_forced_slot_wins_over_rotation_and_keeps_pointer` 已锁定当前语义 |
| O-7 | 全量运行时偶发 `PytestUnhandledThreadExceptionWarning: Event loop is closed` | 跨文件（单文件运行不复现） | aiosqlite 连接线程晚于事件循环收尾，属测试卫生问题，不影响判定结果；建议后续给残留仓库统一走 `qa_helpers.env()` 上下文管理器 | **已修（2026-10-20 收尾）**：`-X dev` 定位到唯一泄漏源 `tests/test_qa_false_positive.py` 的两个集成用例直接调 `make_env()` 却未 `close()`（2 条 aiosqlite 连接在 GC 时才回收）。已改为 `try/finally: await repo.close()`；`python -X dev -m pytest tests -q` 现为 **421 passed、0 warnings**（此前 2 warnings） |

---

## 4. 高风险代码审查结论（PRD §11 点名的 5 类）

### 4.1 误报确认（F01 充要条件 / AC-02 / D-01 / D-02）

| 检查点 | 结论 | 证据 |
|---|---|---|
| 是否把 `group=reset` 单独当确认 | **PASS** | `classifier.py:245-261` 四个条件全部 `continue` 守卫 |
| 是否把 `announcement_state=announced` 单独当确认 | **PASS** | 同上，额外要求 `confirmed is True` 与 `preview is False` |
| 是否把摘要文本含 reset 当确认 | **PASS** | `classifier.py:245-261` 完全不读 `summary`；`test_qa_false_positive.py` 用满屏 reset 摘要的 credits 行验证 |
| `latest_alert.kind=reset/state=confirmed` 是否也校验 `confirmed` | **PASS（口径一致）** | `classifier.py:263-266`；`LatestAlert` 无独立 `confirmed` 字段（`models.py:41-57`），PRD F01③ 亦只要求 `kind+state`；`confirmed` 仅存在于 timeline 行（`models.py:93`） |
| `preview` / `boost` / `unlock` / 未知枚举 | **PASS** | `parser.py:31`（`GROUP_KINDS`）+ `classifier.py:252`（严格 `== "reset"`） |
| credits 只看 `banked_state` | **PASS** | `classifier.py:534-581`，`build_state_version` 对 unknown/None 返回 `None` |
| **RISK-低**：`latest_alert.corrected=true` 仍按确认处理 | **RISK** | `classifier.py:224-225` 未检查 `alert.corrected`；同时 reset 侧没有基于 `corrected` 的更正路径（`classifier.py:584-630` 只看 timeline 行）。建议 `_is_confirmed_reset_alert` 增加 `and not alert.corrected` |

### 4.2 历史刷屏（AC-06 / AC-17 / F08）

| 检查点 | 结论 | 证据 |
|---|---|---|
| `baseline` 建立条件 | **PASS** | `pipeline.py:600-609`；首轮 `baseline_mode` 只入库不投递（`pipeline.py:290-311`） |
| `subscribed_at` 过滤 | **PASS** | `pipeline.py:503-508`；`test_qa_history_flood.py` 覆盖 |
| `enable_catchup` 默认关闭 | **PASS** | `config.py:186`（`_coerce_bool(..., "enable_catchup", False, ...)`）+ `pipeline.py:709-710` 早退 |
| 补发只含 reset/credits 且每群 ≤1 条 | **PASS** | `pipeline.py:54`（`CATCHUP_KINDS`）、`:716-717`（`list_events_since(kinds=...)` 空集即返回 []）、`:739-751`（每群 1 条摘要）、`:736`（`has_delivery` 去重）、`:57`（200 条上限） |
| 重启不刷屏 | **PASS** | `pipeline.py:168-205` 不写 `logical_events`；`repository.py:941-953` inflight→uncertain 不重发 |
| **RISK**：forecast 长期失败 → 永不建基线 | **RISK** | 见 P2-05 |

### 4.3 跨接口重复（AC-03 / AC-04 / D-08）

| 检查点 | 结论 | 证据 |
|---|---|---|
| `reset:ts:` 临时键 → timeline id 的别名迁移是否双向 | **PASS** | `pipeline.py:458-476` 对每个候选的**全部**别名先查后登记，`add_alias` first-wins（`repository.py:637-645`）；因此 ts→id、id→ts 都能命中 |
| 同轮多候选是否只保留 canonical | **PASS** | `_normalize` 用 `result.setdefault(canonical, ...)` 收敛（`pipeline.py:461-476`）+ `_dedupe_sorted`（`classifier.py:99-109`）；`test_qa_cross_api.py` 两条不同 id 同事实 → 1 canonical 1 投递 |
| `deliveries` 唯一约束是否覆盖所有入队路径 | **PASS** | 全仓仅一处 `INSERT INTO deliveries`（`repository.py:759`），三个入队点（`pipeline.py:375/499/750`）都走 `enqueue_delivery` |
| **RISK**：跨轮秒级抖动 | **RISK** | 见 P2-03 |

### 4.4 管理越权（AC-15 / AC-16 / AC-22 / PRD §4）

| 检查点 | 结论 | 证据 |
|---|---|---|
| `ADMIN_COMMANDS` 是否都在 `handle` 早期被拦截 | **PASS** | `commands.py:211-213`，位于限频与任何副作用之前；`test_qa_permissions.py` 逐命令验证 + 配置零变化 |
| `groups` 群内是否只回提示 | **PASS** | `commands.py:527-528` |
| `test` 是否不写库 | **PASS** | `commands.py:242-243` 仅调用 `render_test`；测试断言 deliveries/logical_events 均为 0 |
| `subscribe` 是否拒绝私聊拼 umo | **PASS** | `commands.py:414-415`（`req.is_group and req.group_id` 双条件）；`main.py:414` 的 `is_group=bool(group_id)` 亦为 fail-safe |
| 越权是否消耗限频 | **PASS** | 限频在权限之后（`commands.py:215`）；`test_admin_denied_calls_do_not_consume_rate_limit` |
| 审计日志字段 | **PASS** | `repository.py:1139-1165`：`operator_id / umo_hash / action / result / at`，**不含消息正文** |

### 4.5 后台任务泄漏 / 阻塞

| 检查点 | 结论 | 证据 |
|---|---|---|
| `poll_round` / `run_once` 是否有 `await` 之外的长阻塞 | **PASS** | 无 `time.sleep`/同步 I/O；唯一线程卸载是审计落盘 `asyncio.to_thread`（`repository.py:1157`） |
| DB 写是否单锁事务 | **PASS** | `repository.py:226-237`：`asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout=5000`；WAL 下读不加锁 |
| 命令处理是否零网络 | **PASS** | `CommandService` 只依赖 `repo + ReadModel`，构造参数里没有 client（`commands.py:176-193`）；测试用无 client 的 ReadModel 替身守门 |
| `terminate` 无孤儿任务 / `initialize` 幂等 | **PASS** | `main.py:280-301`（cancel + `gather(return_exceptions=True)`）、`main.py:188-192`；并有**真实实例化**测试（替身注入）验证 `asyncio.all_tasks()` 收敛 |
| **RISK**：发送无超时 | **RISK** | 见 P1-02 |
| 单次 `run_once` 串行发送 ≤10 条 | **RISK-低** | `notifications.py:530-548` 串行；单条慢会拖慢整批（无并发上限问题，但与 P1-02 叠加会放大阻塞） |

### 4.6 附加检查（主理人点名的三处）

| 检查点 | 结论 | 证据 |
|---|---|---|
| `/cr` 参数解析：`GreedyStr` 无默认值才算贪婪，`/cr set p24 60` 三个参数都拿到 | **PASS** | `main.py:352-364`：签名 `(self, event, args: GreedyStr)` 且**无默认值**（AST 断言 `cr.args.defaults == []`）；`parts = str(args).split()` → `subcommand=parts[0]`、`argv=parts[1:]`，`set` 分支读 `args[0]/args[1]`（`commands.py:471-474`）。真实框架行为仍需联调（见 §5） |
| `_apply_config_cards` 过期版本 / 未知 id / 重复 id | **PASS（有 1 项顺序瑕疵）** | 未知 id 丢弃 `main.py:556-561`；版本过期不覆盖 `main.py:562-570`；重复保留首个 `main.py:552-555`；末尾 `_project_cards` 按 DB 重投影保证自洽。顺序瑕疵见 P2-06 |
| `_send` 的 `Comp.At(qq="all")` 条件 | **PASS** | 双保险：Worker 只对 `kind=="reset"` 且 `allow_at_all` 置 `at_all`（`notifications.py:597`），`_send` 再判 `platform=="aiocqhttp"`（`main.py:338-341`）；模板文本永不含「@全体成员」（`notifications.py` 全文无该串） |

---

## 5. 未覆盖项与人工联调待办

**自动化无法替代（PRD §3「验收纪律」：真实群行为必须人工确认，状态只能记"待验收"）**

| # | 待验收项 | 具体动作 | 关注点 |
|---|---|---|---|
| 1 | **AC-15** 普通成员越权 | 真实群内用非管理员账号执行 `subscribe/unsubscribe/settings/set/groups/test` | 回复文案一致、群配置零变化、随后 `status/forecast` 正常 |
| 2 | **AC-16** 管理员 `/cr unsubscribe` | 订阅→收到通知→退订→观察 30 分钟 | 不再收到任何自动推送；WebUI 卡片条目消失 |
| 3 | **AC-17** 真实重启 | `docker restart`（保留 `plugin_data` 卷） | 订阅/投递记录保留、不重发历史、启动日志出现 `bootstrap`/`requeue`、`uncertain` 被标记 |
| 4 | **AC-22** `/cr test` | 未订阅群内管理员执行 | 仅当前会话收到、`deliveries` 无新增、60s 内第二次被限频 |
| 5 | `/cr` 贪婪参数 | 真实 AstrBot 4.29 下执行 `/cr set p24 60` 与 `/cr set reset off` | 第三个参数未被框架丢弃（本地仅静态断言） |
| 6 | 短名冲突检测 | 与其它注册了 `cr` 的插件共存 | `_detect_short_name_conflict` 依赖真实 `star_map` 结构；本地为替身 |
| 7 | 消息链路 | 真实 `MessageChain().message()` / `context.send_message` 返回值 | 替身假设 `True=平台接受`；需确认真实语义（这决定 `mark_delivered` 是否准确） |
| 8 | 时区 / 路径 | 真实 `get_astrbot_data_path()`（Linux 容器 + Windows 中文路径） | 数据目录可写、WAL 正常、`Asia/Shanghai` 渲染正确 |
| 9 | 真实网络故障 | 真实 502 / 429 `Retry-After: 120` / 超时 | 退避 2/4/8/15 分钟、无假消息、QQ 侧不阻塞 |
| 10 | 长消息 | 真实 QQ 单条长度限制下的 1200 字符截断 | 不出现半条消息或被平台截断 |

**自动化已覆盖但未做真实端到端**：AC-01/02/03/04/05/06/07/08/09/10/11/12/13/14/18/19/20/21/23/24 均有单元或集成用例（含 QA 新增 157 项），但仍属"模拟"口径。

**未覆盖的技术面（明确声明）**：多进程/多实例并发写同一 SQLite（WAL 锁竞争）、AstrBot 热重载下的真实 `initialize/terminate` 时序、插件卸载（`uninstall`）清理逻辑、WebUI `template_list` 渲染。

---

## 6. 结论：**有条件交付 → 条件 C1 已满足**

> **第二轮更新（2026-10-20）**：P1-01 / P1-02 / P2-01~07 及附加 A/B/C 已全部修复，全量 **421 passed / 0 xfailed**（三轮又修掉 P1-03 与 P2-09~11，见 §0.2）、`ruff` 全绿、`services/**` 零 astrbot 导入（明细见 §0）。据此 **C1 已满足**（选择「修复」而非「接受偏差」）、**C3 已满足**（`banked_grant` 不参与判定已写入 PRD D-03 与 README F04）；**C2（10 项人工联调）仍待验收**，`docs/ACCEPTANCE.md` 保持原状。

**判定理由**

1. **质量面达标**：全量 421 项全部通过（370 基线 + 4 项原 xfail 转正 + 47 项新增守卫），既有 236 项**零回归**，`ruff` 全绿；5 类高风险项的核心判定全部 **PASS**，AC-02/AC-06/AC-03/AC-04/AC-15/AC-16/AC-22/AC-23 的 PRD 条款均被独立断言守住；无 P0（无误报、无刷屏、无数据损坏、无崩溃）。
2. **存在 2 项 P1**：P1-01（`force_timeline` 未接线，PRD §2 明文功能缺失，仅影响时效）、P1-02（投递无发送超时，适配器挂起会导致投递链停摆）。二者都不影响"会不会误报/重复"，但都触碰**已冻结的 PRD 行为**，不应在"可交付"里被默认接受。
3. **8 项 P2 + 5 项观察项**已逐条给出文件行号与修法，其中 P2-08 是 PRD 内部冲突（建议只改文档，不改代码），P2-01/P2-02/P2-03 已有 xfail 用例锁定，修好后自动转 XPASS 作为回归门禁。
4. **4 项 AC（15/16/17/22）必须真实群联调**：按 PRD §3，模拟测试不得替代"通过"；当前状态只能记 `待验收`。

**交付条件（三条，全部满足即可上线）**

- **C1**：P1-01、P1-02 在 V1.2 发布前修复；或由产品/架构书面确认"接受该偏差"并写入 `docs/ACCEPTANCE.md` 的已知问题。→ **已满足（选择修复：P1-01/P1-02 均已在代码中修复并有回归用例）**
- **C2**：§5 的 10 项人工联调完成并回填 `docs/ACCEPTANCE.md`，尤其 AC-15/16/17/22。→ **待验收**
- **C3**：P2-08 的 PRD 内部冲突在 `docs/PRD-confirmation.md` 或 README 注明（`banked_grant` 不参与权益判定），避免后续维护者按 F04 字面"修正"而打破 AC-12。→ **已满足（PRD D-03 + README §1 F04 行）**

> C1 选择的是"修复"路径：最终回归结果 **421 passed / 0 xfailed**（既有 374 项零回归 + 新增 47 项 = 工程师 20 项守护 + QA 27 项独立验证），4 个 xfail 标记已全部删除并转为普通回归用例。剩余交付前置条件只有 C2。

---

## 附：复现命令

```bash
cd E:/project/Astrbot/astrbot_plugin_codex_reset

# 全量（含 QA 独立用例）
E:/Anaconda3/python.exe -m pytest tests -q

# 只看 QA 新增
E:/Anaconda3/python.exe -m pytest tests/test_qa_false_positive.py tests/test_qa_history_flood.py \
  tests/test_qa_cross_api.py tests/test_qa_permissions.py tests/test_qa_task_lifecycle.py \
  tests/test_qa_degradation.py tests/test_qa_rate_limit.py tests/test_qa_force_timeline.py -q

# 只跑「强制 timeline」专项（P1-01 独立验证，17 项）
E:/Anaconda3/python.exe -m pytest tests/test_qa_force_timeline.py -q

# 只看 xfail（二轮修复后应为 0；`-rx` 只用于确认没有残留标记）
E:/Anaconda3/python.exe -m pytest tests -q -rx

# 代码风格
E:/Anaconda3/python.exe -m ruff check services tests main.py
```
