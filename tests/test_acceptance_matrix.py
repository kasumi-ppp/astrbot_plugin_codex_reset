"""验收矩阵静态核对：AC-01 ~ AC-24 每条都必须映射到**真实存在**的测试函数。

只做静态断言（`importlib` 导入对应测试模块 + `hasattr`），不依赖
`pytest --collect-only` 的输出格式，因此在任何 pytest 版本下都稳定。
`docs/ACCEPTANCE.md` 的「自动化测试节点」列与本文件保持同源。
"""

from __future__ import annotations

import importlib
from typing import Any

#: AC → ((模块名, 测试函数名), ...)（一个 AC 可被多个自动化测试覆盖）
AC_TESTS: dict[str, tuple[tuple[str, str], ...]] = {
    "AC-01": (
        ("test_classifier", "test_announced_confirmed_row_yields_single_event"),
        ("test_templates", "test_reset_template_contains_confirmation_wording"),
    ),
    "AC-02": (
        ("test_classifier", "test_trap_row_announced_but_unconfirmed_yields_zero"),
        ("test_classifier", "test_none_row_yields_zero"),
    ),
    "AC-03": (("test_pipeline", "test_forecast_then_timeline_does_not_duplicate"),),
    "AC-04": (
        ("test_pipeline", "test_three_endpoints_same_fact_yields_single_delivery"),
        ("test_integration", "test_end_to_end_four_message_kinds"),
    ),
    "AC-05": (
        ("test_watch", "test_active_watch_yields_single_prewarning"),
        ("test_watch", "test_watch_text_is_not_a_confirmation"),
    ),
    "AC-06": (
        ("test_watch", "test_expired_alert_with_empty_baseline_never_notifies"),
        ("test_pipeline", "test_first_round_baseline_records_without_pushing"),
    ),
    "AC-07": (
        ("test_watch", "test_active_then_expired_yields_single_correction"),
        ("test_watch", "test_window_elapsed_is_suppressed_when_reset_landed_inside_the_window"),
    ),
    "AC-08": (("test_watch", "test_nowindow_text_uses_fixed_phrase_without_fake_hours"),),
    "AC-09": (("test_forecast", "test_two_consecutive_hits_trigger_exactly_once"),),
    "AC-10": (("test_forecast", "test_long_high_series_triggers_at_most_once"),),
    "AC-11": (("test_forecast", "test_low_confidence_still_pushes_with_experimental_wording"),),
    "AC-12": (("test_credits", "test_credits_announced_then_available_yield_two_versions"),),
    "AC-13": (("test_pipeline", "test_two_subscriptions_only_receive_their_categories"),),
    "AC-14": (("test_pipeline", "test_empty_subscriptions_yield_zero_deliveries"),),
    "AC-15": (("test_commands", "test_non_admin_is_rejected_but_can_query"),),
    "AC-16": (("test_commands", "test_unsubscribe_stops_deliveries_and_drops_card"),),
    "AC-17": (
        ("test_pipeline", "test_restart_reuses_database_without_resending"),
        ("test_repository", "test_requeue_inflight_as_uncertain"),
    ),
    "AC-18": (
        ("test_pipeline", "test_fetch_failures_never_deliver_and_backoff"),
        ("test_client", "test_timeout_and_network_errors_never_raise"),
    ),
    "AC-19": (
        ("test_scheduler", "test_two_requests_are_at_least_spacing_seconds_apart"),
        ("test_scheduler", "test_user_agent_is_compliant"),
    ),
    "AC-20": (
        ("test_templates", "test_every_template_contains_source_line"),
        ("test_templates", "test_source_line_cannot_be_disabled"),
    ),
    "AC-21": (
        ("test_commands", "test_rate_limit_blocks_repeat_and_never_fetches"),
        ("test_pipeline", "test_command_handle_never_triggers_fetch"),
    ),
    "AC-22": (("test_commands", "test_test_command_is_labelled_and_writes_nothing"),),
    "AC-23": (
        ("test_parser", "test_parse_event_missing_required_fields_never_raises"),
        ("test_parser", "test_parse_event_unknown_group_is_preserved_but_flagged"),
    ),
    "AC-24": (
        ("test_dedupe", "test_concurrent_enqueue_creates_single_row"),
        ("test_dedupe", "test_ten_concurrent_run_once_only_one_delivery"),
    ),
}

#: 真实群行为需人工验收的 AC（自动化仅覆盖模拟路径）
MANUAL_ACCEPTANCE: frozenset[str] = frozenset({"AC-15", "AC-16", "AC-17", "AC-22"})


def _load(module_name: str) -> Any:
    return importlib.import_module(module_name)


def test_matrix_covers_every_acceptance_id() -> None:
    expected = {f"AC-{index:02d}" for index in range(1, 25)}
    assert set(AC_TESTS) == expected
    assert MANUAL_ACCEPTANCE <= set(AC_TESTS)


def test_every_mapped_test_function_exists() -> None:
    missing: list[str] = []
    for acceptance_id, mappings in AC_TESTS.items():
        assert mappings, f"{acceptance_id} 至少需要一个自动化测试"
        for module_name, function_name in mappings:
            assert function_name.startswith("test_"), (acceptance_id, function_name)
            module = _load(module_name)
            if not callable(getattr(module, function_name, None)):
                missing.append(f"{acceptance_id}: {module_name}.{function_name}")
    assert missing == []


def test_mapped_modules_are_test_modules() -> None:
    modules = {module_name for mappings in AC_TESTS.values() for module_name, _ in mappings}
    assert modules
    for module_name in modules:
        assert module_name.startswith("test_"), module_name
        module = _load(module_name)
        assert module.__name__ == module_name
