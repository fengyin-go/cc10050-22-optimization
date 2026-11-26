"""汇率替换批次的业务自检：多币对提交、部分失败、续办、幂等与指标重算。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SEED_ROWS  # noqa: E402
from app.services import service_for  # noqa: E402
from app.services.currency_conv import CHANGE_TABLE  # noqa: E402
from app.store import store  # noqa: E402

EXTRA_TABLES = (CHANGE_TABLE, "currency_replacement")


class CurrencyReplacementTest(unittest.TestCase):
    def setUp(self) -> None:
        # 原地重置内存仓库，保持已导入的服务持有同一个 store 实例。
        store._tables.clear()
        for name, rows in SEED_ROWS.items():
            store._tables[name] = [dict(row) for row in rows]
        for name in EXTRA_TABLES:
            store._tables[name] = []
        self.currency = service_for("currency_conv")
        self.history = service_for("change_history")

    def submit_partial(self) -> dict:
        return self.currency.submit_replacement([
            {"币对": "USD/CNY", "快照ID": 5},
            {"币对": "JPY/CNY", "快照ID": 8},
        ])

    def test_partial_receipts_split_confirmed_and_pending(self) -> None:
        result = self.submit_partial()
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PARTIAL_RECEIPTS")
        states = {item["币对"]: item for item in result["receipts"]}
        self.assertEqual(states["USD/CNY"]["状态"], "已确认")
        self.assertEqual(states["JPY/CNY"]["状态"], "待补材料")
        self.assertTrue(states["JPY/CNY"]["可重试"])
        # 缺资料币对的旧价仍然可查。
        self.assertEqual(states["JPY/CNY"]["旧汇率"], 0.05)
        self.assertTrue(states["JPY/CNY"]["旧价可查"])
        self.assertTrue(states["JPY/CNY"]["冲突"])
        self.assertEqual(states["USD/CNY"]["变更记录ID"], 1)

    def test_same_date_other_pair_snapshot_cannot_substitute(self) -> None:
        result = self.currency.submit_replacement([
            {"币对": "JPY/CNY", "快照ID": 7},  # EUR/CNY 同日快照
        ])
        receipt = result["receipts"][0]
        self.assertEqual(receipt["状态"], "待补材料")
        self.assertIn("不能用于 JPY/CNY", receipt["回执"])
        # EUR 快照不被生效、不被改写。
        eur = store.find("currency_conv", 7)
        self.assertEqual(eur["status"], "待生效")
        self.assertEqual(eur["汇率值"], "7.8")

    def test_missing_snapshot_is_retryable_and_keeps_old_price(self) -> None:
        result = self.currency.submit_replacement([{"币对": "GBP/CNY"}])
        receipt = result["receipts"][0]
        self.assertEqual(receipt["状态"], "待补材料")
        self.assertTrue(receipt["可重试"])
        self.assertIn("缺替换快照", receipt["回执"])
        pair_state = self.currency.pair_effective_snapshot("JPY/CNY")
        self.assertTrue(pair_state["ok"])
        self.assertEqual(pair_state["rate"], 0.05)

    def test_resubmit_original_pair_completes_and_recomputes_once(self) -> None:
        first = self.submit_partial()
        app_id = first["application"]["id"]
        second = self.currency.resubmit_replacement(app_id, [
            {"币对": "JPY/CNY", "汇率值": 0.051, "快照日期": "2026-11-26"},
            {"币对": "USD/CNY", "快照ID": 5},
        ])
        self.assertEqual(second["code"], "ALL_CONFIRMED")
        self.assertEqual(second["duplicate_pairs"], ["USD/CNY"])
        self.assertEqual(len(second["change_records"]), 2)
        jpy = next(c for c in second["change_records"] if c["币对"] == "JPY/CNY")
        self.assertEqual((jpy["旧汇率"], jpy["新汇率"]), (0.05, 0.051))
        self.assertEqual(jpy["recompute_count"], 1)
        events = [e for r in second["comparison"] for e in [r]]
        self.assertTrue(all(row["recompute_count"] == 1 for row in events))
        # 实际换算金额按补传后的口径重算。
        jpy_cmp = next(r for r in second["comparison"] if r["币对"] == "JPY/CNY")
        self.assertEqual(jpy_cmp["实际换算金额"], 107.1)

    def test_duplicate_submission_creates_no_second_change(self) -> None:
        first = self.submit_partial()
        app_id = first["application"]["id"]
        self.currency.resubmit_replacement(app_id, [
            {"币对": "JPY/CNY", "汇率值": 0.051, "快照日期": "2026-11-26"},
        ])
        self.currency.resubmit_replacement(app_id, [
            {"币对": "USD/CNY", "快照ID": 5},
            {"币对": "JPY/CNY", "快照ID": 8},
        ])
        self.assertEqual(self.currency.conversion_changes()["total"], 2)
        self.assertEqual(len(self.currency.conversion_changes("USD/CNY")["items"]), 1)

    def test_resubmit_rejects_pairs_outside_application(self) -> None:
        first = self.currency.submit_replacement([{"币对": "USD/CNY", "快照ID": 5}])
        result = self.currency.resubmit_replacement(
            first["application"]["id"], [{"币对": "EUR/CNY", "快照ID": 7}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PAIR_NOT_IN_APPLICATION")
        self.assertEqual(store.find("currency_conv", 7)["status"], "待生效")

    def test_consumer_can_wait_for_group_or_release_per_pair(self) -> None:
        first = self.submit_partial()
        app_id = first["application"]["id"]
        group = self.currency.consume_application(app_id, "group")
        self.assertTrue(group["waiting_for_group"])
        self.assertEqual(group["released"], [])
        per_pair = self.currency.consume_application(app_id, "per_pair")
        self.assertEqual(per_pair["released_pairs"], ["USD/CNY"])
        self.assertEqual(per_pair["pending_pairs"], ["JPY/CNY"])
        self.assertEqual(per_pair["recompute_count"], 1)

    def test_metrics_recompute_once_per_change_and_keep_basis(self) -> None:
        first = self.submit_partial()
        self.currency.resubmit_replacement(
            first["application"]["id"],
            [{"币对": "JPY/CNY", "汇率值": 0.051, "快照日期": "2026-11-26"}],
        )
        metrics = self.currency.conversion_metrics()
        self.assertEqual(metrics["changes"], 2)
        self.assertEqual(metrics["实际换算金额合计"], 431.1)
        counts = {pair["币对"]: pair["recompute_count"] for pair in metrics["pairs"]}
        self.assertEqual(counts, {"USD/CNY": 1, "JPY/CNY": 1})
        second = self.currency.conversion_metrics()
        self.assertEqual(
            {pair["币对"]: pair["recompute_count"] for pair in second["pairs"]},
            {"USD/CNY": 1, "JPY/CNY": 1},
        )
        jpy = next(pair for pair in metrics["pairs"] if pair["币对"] == "JPY/CNY")
        self.assertEqual(jpy["汇率版本"], ["0.05 -> 0.051"])
        self.assertEqual(jpy["复核范围"], ["JPY/CNY 价格事件"])
        event = jpy["价格事件"][0]
        self.assertEqual(event["换算前金额"], 105.0)
        self.assertEqual(event["换算后金额"], 107.1)

    def test_change_history_consumes_changes_by_pair(self) -> None:
        first = self.submit_partial()
        self.currency.resubmit_replacement(
            first["application"]["id"],
            [{"币对": "JPY/CNY", "汇率值": 0.051, "快照日期": "2026-11-26"}],
        )
        consumed = self.history.consume_currency_changes()
        self.assertTrue(consumed["ok"])
        self.assertEqual(consumed["changes"], 2)
        self.assertEqual(consumed["review_scope"], ["JPY/CNY 价格事件", "USD/CNY 价格事件"])
        events = self.history.currency_price_events("JPY/CNY")["events"]
        self.assertEqual(events[0]["换算前汇率"], 0.05)
        self.assertEqual(events[0]["换算后汇率"], 0.051)
        self.assertEqual(events[0]["换算依据"]["after"]["snapshot_id"], 8)

    def test_metrics_service_writes_back_actual_amounts(self) -> None:
        first = self.submit_partial()
        self.currency.resubmit_replacement(
            first["application"]["id"],
            [{"币对": "JPY/CNY", "汇率值": 0.051, "快照日期": "2026-11-26"}],
        )
        service = service_for("collect_metrics")
        result = service.recompute_converted_amounts()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_actual_amount"], 431.1)
        self.assertIn("汇率替换指标", [row["站点名称"] for row in store.rows("collect_metrics")])

    def test_only_target_pair_snapshot_expires(self) -> None:
        self.submit_partial()
        self.assertEqual(store.find("currency_conv", 4)["status"], "已过期")
        self.assertEqual(store.find("currency_conv", 5)["status"], "已生效")
        # 其他币对即使同一天也不受影响。
        self.assertEqual(store.find("currency_conv", 7)["status"], "待生效")
        self.assertEqual(store.find("currency_conv", 8)["status"], "待生效")


if __name__ == "__main__":
    unittest.main()
