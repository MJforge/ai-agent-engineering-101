"""Equity-target regressions; temporary files and mocks only, never the market."""
import contextlib
import io
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import restock
import store

NOW = 1_790_000_000


class EquityTargetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="equity-restock-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.hub = store.default_hub()
        self.catalog = {}
        self.inv = {"balance": 12_000_000, "units": [],
                    "restock": {"opens_at": NOW-20, "closes_at": NOW+160}}
        self.stderr = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stderr(self.stderr))
        stack.enter_context(patch.object(restock.time, "time", return_value=NOW))
        stack.enter_context(patch.object(restock, "ROUND_END_TS", NOW+10_000_000))
        stack.enter_context(patch.object(restock, "queue_forum_action"))
        stack.enter_context(patch.object(store.urllib.request, "urlopen",
                                         side_effect=AssertionError("network forbidden")))
        for name in ("fetch_ledger_rows", "fetch_own_shop_urls"):
            stack.enter_context(patch.object(restock, name,
                                             side_effect=AssertionError("fetch forbidden")))

    def item(self, sku, price=100_000, kind="goods", held=0, proven=False, shelf=400):
        self.catalog[sku] = {"category": sku, "list_price": price,
                             "kind": kind, "shelf_hours": shelf}
        self.inv["units"].extend({"sku": sku, "grade": "A", "cost": math.ceil(price*.75)}
                                 for _ in range(held))
        if proven:
            self.hub.setdefault("restock_proven_skus", []).append(sku)

    def plan(self):
        return restock.plan_with_hub(self.hub, self.path, self.catalog, self.inv, [])

    def buys(self):
        return {b["sku"]: b["qty"] for b in self.plan()["buy"]}

    def state(self):
        return next(iter(self.hub["restock_windows"].values()))

    def cost(self, plan):
        return sum(math.ceil(self.catalog[b["sku"]]["list_price"]*.75)*b["qty"]
                   for b in plan["buy"])

    def test_proven_depth_three_for_goods_high_goods_and_food(self):
        self.item("goods", 20_000, held=1, proven=True)
        self.item("high", 150_000, held=1, proven=True)
        self.item("food", 20_000, "food", held=1, proven=True)
        self.assertEqual(self.buys(), {"goods": 2, "high": 2, "food": 2})

    def test_live_explicit_depth_and_unproven_market_never_top_up(self):
        self.item("G07ec17d5", held=1, proven=True)
        self.item("unproven", held=1)
        self.hub["demand_radar"]["sku_sold_ts"] = {"unproven": [NOW]*100}
        self.assertEqual(self.buys(), {"G07ec17d5": 3})

    def test_all_own_evidence_sources_qualify(self):
        self.item("radar", held=1)
        self.item("event", held=1)
        self.hub["demand_radar"]["own_sku_sold_ts"] = {"radar": [NOW-10_000]}
        self.hub["own_sale_events"] = {"1": {"sku": "event", "ts": NOW-10_000}}
        self.assertEqual(self.buys(), {"radar": 2, "event": 2})

    def test_sku_and_unit_caps_expand(self):
        for i in range(55):
            self.item(f"held{i}", held=1)
        self.item("new", 150_000)
        self.assertIn("new", self.buys())
        self.assertIn("sku_cap=76 units_cap=127", self.stderr.getvalue())

    def test_zero_deficit_keeps_base_caps_and_proven_restoration(self):
        self.item("held", 1_000_000, held=16)
        self.item("proven", 20_000, proven=True)
        self.assertEqual(self.buys(), {"proven": 1})
        self.assertIn("deficit=0 sku_cap=35 units_cap=80", self.stderr.getvalue())

    def test_budget_and_expansion_absolute_caps(self):
        self.inv["balance"] = 100_000_000
        self.item("core", proven=True)
        self.plan()
        self.assertEqual(self.state()["window_cap"], 3_000_000)
        self.assertIn("sku_cap=120 units_cap=250", self.stderr.getvalue())

    def test_store_available_budget_is_hard_limit(self):
        for i in range(10):
            self.item(str(i), 200_000, proven=True)
        original = restock.update_capital_stage
        def limited(hub, inv):
            cs = original(hub, inv)
            cs["available_budget"] = 160_000
            return cs
        with patch.object(restock, "update_capital_stage", side_effect=limited):
            plan = self.plan()
        self.assertGreater(self.cost(plan), 0)
        self.assertLessEqual(self.cost(plan), 160_000)

    def test_cash_floor_unchanged(self):
        self.inv["balance"] = 800_000
        self.item("core", 20_000, proven=True)
        plan = self.plan()
        self.assertLessEqual(self.cost(plan), 100_000)
        self.assertGreaterEqual(self.inv["balance"]-self.cost(plan), 700_000)
        self.inv["balance"] = 600_000
        self.assertEqual(self.buys(), {})

    def test_midwindow_cap_and_reservations_survive_policy_change(self):
        self.item("core", 20_000, held=1, proven=True)
        with patch.object(restock, "STOCK_TARGET_RATIO", 0):
            self.plan()
        cap = self.state()["window_cap"]
        first = self.plan()
        self.assertEqual(self.state()["window_cap"], cap)
        self.assertEqual(first["buy"], [{"sku": "core", "qty": 2}])
        path = self.path / "hub.json"
        store.save_hub(self.hub, path)
        self.hub = store.load_hub(path)
        self.assertEqual(self.buys(), {})
        self.assertEqual(self.state()["reserved"], self.cost(first))

    def test_food_limit_during_depth_replenishment_and_short_shelf(self):
        self.item("held-food", 20_000, "food", held=19)
        self.item("proven-food", 20_000, "food", proven=True)
        self.item("short-unproven", 20_000, "food", shelf=12)
        self.assertEqual(self.buys(), {"proven-food": 1})

    def test_recovery_halves_exploration_and_diagnostic_preserves_json(self):
        for i in range(8):
            self.item(str(i), 150_000)
        with patch.object(restock, "decide_restock", return_value={"exploration_factor": .5}):
            plan = self.plan()
        self.assertEqual(set(plan), {"buy"})
        self.assertLessEqual(self.cost(plan), self.state()["window_cap"]*.5)
        self.assertIn("equity=12000000 target=5400000 stock_cost=0 deficit=5400000",
                      self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
