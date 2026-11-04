"""Own velocity and price coverage: isolated fake state, no network or market writes."""
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import restock
import store

NOW = 1_790_000_000


class VelocityCoverageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="velocity-coverage-offline-")
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

    def item(self, sku, category="침구", price=20_000, held=0, own=0,
             kind="goods", shelf=400, age=0):
        self.catalog[sku] = {"category": category, "list_price": price,
                             "kind": kind, "shelf_hours": shelf}
        self.inv["units"].extend({"sku": sku, "grade": "A", "cost": math.ceil(price*.75)}
                                 for _ in range(held))
        events = self.hub.setdefault("own_sale_events", {})
        start = len(events)
        events.update({str(start+i): {"sku": sku, "ts": NOW-age} for i in range(own)})

    def plan(self):
        return restock.plan_with_hub(self.hub, self.path, self.catalog, self.inv, [])

    def buys(self):
        return {b["sku"]: b["qty"] for b in self.plan()["buy"]}

    def cost(self, plan):
        return sum(math.ceil(self.catalog[b["sku"]]["list_price"]*.75)*b["qty"]
                   for b in plan["buy"])

    def test_three_own_sales_target_four_and_partial_stock(self):
        self.item("soldout", own=3)
        self.item("held", category="chair", own=3, held=2)
        self.assertEqual(self.buys(), {"soldout": 4, "held": 2})
        line = next(x for x in self.stderr.getvalue().splitlines() if x.startswith("{"))
        diagnostics = {x["sku"]: x for x in json.loads(line)["restock_depth"]}
        self.assertEqual(diagnostics["held"],
                         {"sku": "held", "own20m": 3, "target": 4, "held": 2, "shortfall": 2})

    def test_seven_and_large_own_velocity_capped_at_eight(self):
        self.item("seven", own=7)
        self.item("many", category="chair", own=100)
        self.assertEqual(self.buys(), {"many": 8, "seven": 8})

    def test_market_hundred_never_increases_held_depth(self):
        self.item("market", held=2)
        self.hub["demand_radar"]["sku_sold_ts"] = {"market": [NOW]*100}
        self.assertEqual(self.buys(), {})

    def test_old_evidence_keeps_base_three(self):
        self.item("old", own=7, age=1201)
        self.assertEqual(self.buys(), {"old": 3})

    def test_zero_deficit_does_not_unlock_new_depth(self):
        self.item("stock", category="stock", price=1_000_000, held=16)
        self.item("core", own=7)
        self.assertEqual(self.buys(), {"core": 1})

    def test_short_food_keeps_old_policy_and_total_food_cap(self):
        self.item("short", kind="food", shelf=12, own=7)
        self.assertEqual(self.buys(), {"short": 2})
        self.hub = store.default_hub()
        self.item("held-food", kind="food", held=19)
        self.item("food", category="food", kind="food", own=7)
        self.assertEqual(self.buys(), {"food": 1})

    def test_live_catalog_same_velocity_and_breadth_budget(self):
        self.item("core", price=100_000, own=7)
        for i in range(500):
            self.item(f"cold{i}", category=f"cold{i}", price=100_000)
        plan = self.plan()
        self.assertEqual(plan["buy"][0], {"sku": "core", "qty": 8})
        self.assertGreater(len(plan["buy"]), 1)
        self.assertLessEqual(self.cost(plan), 3_000_000)

    def test_repeated_window_and_reload_no_duplicate(self):
        self.item("core", own=7)
        first = self.plan()
        path = self.path / "hub.json"
        store.save_hub(self.hub, path)
        self.hub = store.load_hub(path)
        self.assertEqual(self.buys(), {})
        state = next(iter(self.hub["restock_windows"].values()))
        self.assertEqual(state["reserved"], self.cost(first))


if __name__ == "__main__":
    unittest.main()
