"""POLICY.md §9 unified buying. Stdout remains a single {buy: [...]} JSON line."""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from adaptive_strategy import decide_restock
from store import (
    ROUND_END_TS, STOP_BUYING_BEFORE_END_S, apply_ledger_rows,
    fetch_ledger_rows, fetch_own_shop_urls, load_catalog, load_hub,
    load_inventory, load_json_safe, mutate_hub, queue_forum_action,
    update_capital_stage,
)

POPULARITY_S = 40 * 60
NO_DEMAND_S = 2 * 3600
DEAD_AGE_S = 4 * 3600
MAX_DEAD_SKUS = 500
WINDOW_LEAD_S = 60
MIN_FOOD_LIST_PRICE = 10_000
MAX_FOOD_LIST_PRICE = 120_000
DEFAULT_RATIO = .40


def supply_window(inv: dict[str, Any]) -> tuple[float, float] | None:
    window = inv.get("restock") or {}
    try:
        opens, closes = float(window["opens_at"]), float(window["closes_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if (math.isfinite(opens) and math.isfinite(closes) and opens < closes
            and opens - WINDOW_LEAD_S <= time.time() < closes):
        return opens, closes
    return None


def market_ratio(market_dir, catalog):
    """Only existing public files; missing financial observations fail safe, never fetch."""
    shops = load_json_safe(market_dir / "shops.json", []) or []
    board = load_json_safe(market_dir / "board.json", {}) or {}
    shops = shops.get("shops", []) if isinstance(shops, dict) else shops
    board = board.get("shops", []) if isinstance(board, dict) else board
    metrics = {s.get("name"): s for s in board if isinstance(s, dict)}
    ratios = []
    for shop in shops:
        if not isinstance(shop, dict):
            continue
        data = {**shop, **metrics.get(shop.get("name"), {})}
        revenue, profit, stock = data.get("revenue"), data.get("profit"), shop.get("stock")
        if (not isinstance(revenue, (int, float)) or not math.isfinite(revenue)
                or not isinstance(profit, (int, float)) or not math.isfinite(profit)
                or not isinstance(stock, dict)):
            continue
        if any(s not in catalog or not isinstance(q, (int, float)) or not math.isfinite(q) or q < 0
               for s, q in stock.items()):
            continue
        estimated = sum(q * int(catalog[s].get("list_price", 0)) * .75 for s, q in stock.items())
        cash = 5_000_000 + profit
        if cash < 0 or cash + estimated <= 0:
            continue
        ratios.append((revenue, str(shop.get("name", "")), estimated / (cash + estimated)))
    if len(ratios) < 3:
        return DEFAULT_RATIO, {"source": "default: insufficient public shops/financials", "default": True,
                               "shops": len(ratios)}
    top = sorted(ratios, key=lambda r: (-r[0], r[1]))[:5]
    return max(.30, min(.60, statistics.median(r[2] for r in top))), {
        "source": "shops.json+board.json: top revenue median", "default": False, "shops": len(top)}


def popularity(inquiries, misses, settlements):
    if inquiries >= 4:
        return "hot", max(3, min(8, inquiries))
    if inquiries >= 2 or misses >= 2 or settlements >= 21:
        return "popular", 2
    if inquiries == 1 or misses == 1 or 11 <= settlements <= 20:
        return "observe", 1
    return "none", 0


def estimated_cost(hub, sku, info):
    actual = hub.get("purchase_costs", {}).get(sku, {}).get("cost", 0)
    return max(1, math.ceil(actual if actual > 0 else int(info.get("list_price", 0)) * .75))


def build_restock_plan(market_dir: Path) -> dict[str, Any]:
    catalog, inv = load_catalog(market_dir), load_inventory(market_dir)

    def stopped(reason, *, bankrupt=False):
        cs = update_capital_stage({"capital_stage": {}}, inv)
        stock = cs.get("stock_cost_value", 0)
        equity = int(inv.get("balance") or 0) + stock
        ratio, source = market_ratio(market_dir, catalog)
        print(json.dumps({"E": equity, "R": ratio, "R_source": source, "T": ratio * equity,
                          "deficit": max(0, ratio * equity - stock), "W": 0,
                          "grades": {g: {"skus": 0, "reserved": 0} for g in ("hot", "popular", "observe")},
                          "grade_budget": {"hot_popular": 0, "observe": 0},
                          "dead_new": 0, "dead_total": len(load_hub().get("dead_skus", {})),
                          "excluded": {}, "stop": reason}), file=sys.stderr, flush=True)
        return {"buy": [], "bankrupt": True} if bankrupt else {"buy": []}

    if not catalog or not inv:
        return stopped("missing_input")
    balance = int(inv.get("balance") or 0)
    sellable = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
    if balance < int(inv.get("bankrupt_line") or 0) and int(inv.get("bankruptcies_left") or 0) > 0 and not sellable:
        return stopped("bankruptcy", bankrupt=True)
    if time.time() > ROUND_END_TS - STOP_BUYING_BEFORE_END_S:
        return stopped("round_end")
    snapshot = load_hub()
    rows = fetch_ledger_rows(int(snapshot.get("ledger_seq", 0)))
    window = supply_window(inv)
    if window and str(window) not in snapshot.get("restock_windows", {}):
        rows += fetch_ledger_rows(0)
    own_urls = fetch_own_shop_urls()
    try:
        fresh = 0 <= time.time() - (market_dir / "inventory.json").stat().st_mtime <= 120
    except OSError:
        fresh = False
    with mutate_hub() as hub:
        hub["own_shop_urls"] = sorted(set(hub.get("own_shop_urls", [])) | set(own_urls))
        if window and not hub.get("restock_windows"):
            hub.setdefault("restock_windows", {})[str(window)] = {
                "baseline_actual": 0, "reserved": 0, "blocked": True}
        return plan_with_hub(hub, market_dir, catalog, inv, rows, inventory_fresh=fresh)


def plan_with_hub(hub, market_dir, catalog, inv, rows, *, inventory_fresh=False):
    """Locked caller persists reservations, inquiries and dead history atomically."""
    now = time.time()
    apply_ledger_rows(hub, rows, catalog)
    cs = update_capital_stage(hub, inv)
    decide_restock(hub, inv, now, inventory_fresh=inventory_fresh)  # metrics only
    radar = hub.get("demand_radar", {})
    units = inv.get("units") or []
    active = [u for u in units if u.get("grade") != "expired"]
    held = collections.Counter(u.get("sku") for u in active)
    costs = hub.setdefault("purchase_costs", {})
    for u in units:
        if u.get("sku") in catalog and u.get("cost", 0) > 0:
            sku, ts = u["sku"], u.get("supplied_at", 0)
            if ts >= costs.get(sku, {}).get("ts", 0):
                costs[sku] = {"cost": u["cost"], "ts": ts}
    dead = hub.setdefault("dead_skus", {})
    new_dead = 0
    for u in units:
        sku, supplied = u.get("sku"), u.get("supplied_at")
        if (sku in catalog and isinstance(supplied, (int, float)) and 0 < supplied <= now - DEAD_AGE_S
                and hub.get("own_last_sale_ts", {}).get(sku, 0) < supplied and sku not in dead):
            if len(dead) < MAX_DEAD_SKUS:
                dead[sku] = now
                new_dead += 1
            else:
                hub["dead_skus_overflow"] = True  # never evict a round-long prohibition
    coverage = hub.get("adaptive_ledger_coverage", {})
    covered = (coverage.get("since", now) <= now - NO_DEMAND_S
               and 0 <= now - coverage.get("through", 0) <= 120)
    ratio, source = market_ratio(market_dir, catalog)
    stock = cs["stock_cost_value"]
    equity = int(inv.get("balance") or 0) + stock
    target = ratio * equity
    deficit = max(0, target - stock)
    excluded = collections.Counter()
    candidates = {g: [] for g in ("hot", "popular", "observe")}
    storage = float(inv.get("storage_per_day", .01))
    for sku, info in catalog.items():
        events = radar.get("sku_inquiries", {}).get(sku.lower(), [])
        recent = [e for e in events if 0 <= now - e["ts"] <= POPULARITY_S]
        misses = sum(bool(e.get("miss")) for e in recent)
        # Unheld SKUs use stockout inquiries only, not old quotes for stock no longer held.
        inquiries = len(recent) if held[sku] else misses
        sales = radar.get("sku_sold_ts", {}).get(sku, [])
        grade, depth = popularity(inquiries, misses, sum(0 <= now - t <= POPULARITY_S for t in sales))
        lp, cost = int(info.get("list_price", 0)), estimated_cost(hub, sku, info)
        prices = radar.get("sku_settles", {}).get(sku, [])
        expected_price = statistics.median(prices) if prices else .95 * lp
        margin = expected_price * .97 - cost  # net expected proceeds, including settlement fee
        reason = None
        if sku in dead or hub.get("dead_skus_overflow"):
            reason = "dead" if sku in dead else "dead_history_full"
        elif covered and not any(0 <= now - t <= NO_DEMAND_S for t in sales) and not any(
                0 <= now - e["ts"] <= NO_DEMAND_S for e in events):
            reason = "no_demand_2h"
        elif depth == 0:
            reason = "no_popularity"
        elif lp <= 0:
            reason = "invalid_price"
        elif (info.get("kind") == "food" and grade != "hot"
              and (not MIN_FOOD_LIST_PRICE <= lp <= MAX_FOOD_LIST_PRICE
                   or (not held[sku] and (info.get("shelf_hours") or 0) < 96))):
            reason = "food_safety"
        elif margin < 2 * storage * cost:
            reason = "storage_margin"
        elif depth <= held[sku]:
            reason = "target_met"
        if reason:
            excluded[reason] += 1
        else:
            candidates[grade].append((sku, depth - held[sku], cost, margin))
    for group in candidates.values():
        group.sort(key=lambda c: (-c[3], c[0]))
    restore = sum(n * c for g in ("hot", "popular") for s, n, c, m in candidates[g] if not held[s])
    calculated = max(0, int(min(cs["available_budget"], max(restore, deficit / 3), .25 * equity)))
    diag = {"E": equity, "R": ratio, "R_source": source, "T": target, "deficit": deficit,
            "W": calculated, "cash_floor": cs["cash_floor"], "restore_cost": restore,
            "grades": {g: {"skus": len(v), "reserved": 0} for g, v in candidates.items()},
            "grade_budget": {"hot_popular": calculated, "observe": calculated // 2},
            "dead_new": new_dead, "dead_total": len(dead), "excluded": excluded,
            "no_demand_deferred": not covered}

    def finish(orders, reason=None):
        if reason:
            diag["stop"] = reason
        print(json.dumps(diag, ensure_ascii=False), file=sys.stderr, flush=True)
        return {"buy": orders}

    if time.time() > ROUND_END_TS - STOP_BUYING_BEFORE_END_S:
        return finish([], "round_end")
    window = supply_window(inv)
    if window is None:
        return finish([], "outside_window")
    actual = sum(s["total"] for s in hub.get("own_supplies", {}).values() if window[0] <= s["ts"] < window[1])
    state = hub.setdefault("restock_windows", {}).setdefault(str(window), {"baseline_actual": actual, "reserved": 0})
    if state.get("blocked"):
        return finish([], "first_window")
    cap = state.setdefault("window_cap", calculated)
    diag["W"] = cap  # immutable initial ceiling; current cash can only reduce usable funds
    used = max(actual, state["baseline_actual"] + state["reserved"])
    budget = min(cs["available_budget"], calculated, max(0, cap - used))
    diag["usable_budget"] = budget
    planned = state.setdefault("planned_skus", {})
    grade_spent = state.setdefault("grade_reserved", {g: 0 for g in candidates})
    spent, orders = 0, []
    for grade, group in candidates.items():
        for sku, need, cost, margin in group:
            if sku in planned:
                excluded["window_duplicate"] += 1
                continue
            allowance = budget - spent
            if grade == "observe":
                allowance = min(allowance, max(0, cap // 2 - grade_spent[grade]))
            qty = min(need, 20, max(0, allowance) // cost)
            if not qty or len(orders) >= 10:
                continue
            orders.append({"sku": sku, "qty": qty})
            planned[sku] = qty
            spent += qty * cost
            grade_spent[grade] += qty * cost
    state["reserved"] += spent
    for grade in candidates:
        diag["grades"][grade]["reserved"] = grade_spent[grade]
    diag["grade_budget"] = {"hot_popular": cap, "observe": cap // 2}
    # Preserve the existing optional shop promotion; it does not affect buying.
    last_post = int(hub.get("forum_state", {}).get("last_shop_post_ts", 0))
    if len(active) >= 6 and now - last_post >= 330:
        refs = (radar.get("recent_refs") or [])[-3:]
        cats = sorted({u.get("category", "").split(">")[-1] for u in active if u.get("category")})[:5]
        text = (f"[mjforge Fulfillment Report] Stage-{cs['stage']} inventory active ({len(active)} certified units across "
                f"{', '.join(cats)}). All units ship with market certificate (cert=true), honest grade claims, "
                "6h Safe-Care (안심케어) full-refund warranty on all goods (A/B can drop it for a lower price), "
                "and instant 1-turn discount below list price.")
        queue_forum_action(market_dir, {"post": text, "refs": refs, "as": "shop"})
        hub.setdefault("forum_state", {})["last_shop_post_ts"] = int(now)
    return finish(orders)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=".market", help="Path to .market directory")
    args = ap.parse_args()
    print(json.dumps(build_restock_plan(Path(args.state)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
