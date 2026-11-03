"""Own-turnover restoration, then bounded assortment; persistent 200k/450k windows.

Market settlements select candidates, never purchase depth. Existing cash-floor,
shelf-life, bankruptcy and end-of-round safeguards remain in force.
Outputs one JSON line: {"buy": [{"sku": "...", "qty": 1..20}, ...]}.
"""
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

from adaptive_strategy import MAX_EXPLORATION_SKUS, MAX_WINDOW_BUDGET, decide_restock

from store import (
    ROUND_END_TS,
    SEED_GOODS_CATEGORIES,
    STOP_BUYING_BEFORE_END_S,
    apply_ledger_rows,
    fetch_ledger_rows,
    fetch_own_shop_urls,
    load_catalog,
    load_hub,
    load_inventory,
    load_json_safe,
    mutate_hub,
    queue_forum_action,
    update_capital_stage,
)

# Optimal unit list_price range for Stage 1/2 goods: avoids locking 2M+ in a single sofa/PC
# while delivering high absolute profit and revenue per deal.
MIN_GOODS_LIST_PRICE = 12_000
MAX_GOODS_STAGE1_PRICE = 450_000
MAX_GOODS_STAGE3_PRICE = 650_000

MIN_FOOD_LIST_PRICE = 10_000
MAX_FOOD_LIST_PRICE = 120_000

# Food that outlasts the round (two weeks): no storage fee and no breakage, so it is the cheap way to be visible in
# many categories while we learn what sells. The kit lists a shop to a buyer only with stock of a SKU in the order's
# acceptable price window (market_kit.py, offers), so being in a category means holding SKUs near its usual price.
ROUND_HOURS = 336
SEED_FOOD_CATEGORY_COUNT = 10
SEED_GOODS_COUNT = 4                # seed goods categories whose median list price is at most SEED_GOODS_MAX_MEDIAN
SEED_GOODS_MAX_MEDIAN = 60_000
RECENT_S = 3 * 3600                 # Market signals rank cold-start candidates only.
# Supply opens for 3 minutes every 20 (agent.md); outside a window the kit waits up to 90 s per line and skips it.
# So plan only when a window is open or opens within this many seconds.
WINDOW_LEAD_S = 60
WINDOW_BUDGET = 200_000
PROFITABLE_WINDOW_BUDGET = 450_000  # Base cash-rich window cap; scales up to 900k-1.2M on full live catalog
HIGH_CASH_WINDOW_BUDGET = 900_000
SUPER_CASH_WINDOW_BUDGET = 1_200_000
MISSED_RECENT_S = 3600              # 1-hour window for out-of-stock missed inquiries
MAX_ACTIVE_FOOD_UNITS = 20          # Tight food inventory cap: stop buying food when >= 20 food units are held
HIGH_GOODS_MIN_PRICE = 100_000      # Prioritize >= 100,000 KRW non-food categories for new assortment expansion
OWN_RECENT_S = 20 * 60
TARGET_CAPS = {"G97cd0437": 2, "Gef4d3036": 2, "G0be9ed07": 2,
               "Ge62449c9": 2, "G07ec17d5": 3}
HIGH_VALUE_CAPS = {
    "G1515d951": 2,  # 423,810 Queen hybrid mattress (#1 market revenue SKU)
    "G07ec17d5": 3,  # 279,000 SS hybrid spring mattress (#2 market revenue SKU)
    "G2cb8041b": 2,  # 296,000 Office mesh chair (#3 market revenue SKU)
    "G5cd4ffbc": 2,  # 199,000 Microfiber comforter set (#4 market revenue SKU)
    "Ge62449c9": 3,  # 87,900 Hotel comforter (#5 market revenue SKU)
    "G200e39a3": 3,  # 37,900 Kimchi 10kg (#6 market revenue SKU)
}
EXPLORATION_SKUS = 35
EXPLORATION_UNITS = 80
EXPLORATION_PER_WINDOW = 4


def price_covered(info: dict[str, Any], held: set[str], catalog: dict[str, Any]) -> bool:
    """Coverage follows the buyer's reference-price window, not SKU identity."""
    ref = int(info.get("list_price", 0))
    return any(catalog.get(s, {}).get("category") == info.get("category")
               and .5 * ref <= int(catalog[s].get("list_price", 0)) <= 1.5 * ref
               for s in held if s in catalog)


def price_band(price: int, median: float) -> int:
    return 0 if price <= .67 * median else 1 if price <= 1.5 * median else 2


def supply_window(inv: dict[str, Any]) -> tuple[float, float] | None:
    """Missing/invalid window metadata is not permission to buy."""
    window = inv.get("restock") or {}
    try:
        opens, closes = float(window["opens_at"]), float(window["closes_at"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time()
    if not (math.isfinite(opens) and math.isfinite(closes) and opens < closes):
        return None
    if not opens - WINDOW_LEAD_S <= now < closes:
        return None
    return opens, closes


def long_shelf_food_categories(cat_map: dict[str, list[dict[str, Any]]], n: int = SEED_FOOD_CATEGORY_COUNT) -> list[str]:
    """The n food categories whose shelf life outlasts the round, most SKUs first."""
    cats = [c for c, skus in cat_map.items()
            if skus and skus[0].get("kind") == "food" and (skus[0].get("shelf_hours") or 0) >= ROUND_HOURS]
    return sorted(cats, key=lambda c: -len(cat_map[c]))[:n]


def category_medians(market_dir: Path, cat_map: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    """median_price per category from demand.json (the market's), else the catalog median of the category."""
    medians = {c: float(statistics.median(int(x.get("list_price", 0)) for x in skus))
               for c, skus in cat_map.items() if skus}
    for d in load_json_safe(market_dir / "demand.json", []) or []:
        if isinstance(d, dict) and d.get("category") in medians and d.get("median_price"):
            medians[d["category"]] = float(d["median_price"])
    return medians


def select_near_median(
    cat_skus: list[dict[str, Any]],
    held_skus: set[str],
    median: float,
    n: int,
    min_price: int,
    max_price: int,
) -> list[dict[str, Any]]:
    """The n unheld SKUs priced closest to the category median (by ratio): they fall inside the most orders'
    0.5x-1.5x acceptable windows. Staying near the median (0.75x-1.33x) comes first; min_price/max_price only
    narrow the choice when that still leaves SKUs near the median, so a cheap category is not skipped."""
    unheld = [x for x in cat_skus if x["sku"] not in held_skus]
    near = [x for x in unheld if 0.75 * median <= int(x.get("list_price", 0)) <= 1.33 * median]
    bounded = [x for x in near if min_price <= int(x.get("list_price", 0)) <= max_price]
    pool = bounded or [x for x in near if int(x.get("list_price", 0)) <= max_price] or near
    pool.sort(key=lambda x: abs(math.log(max(1, int(x.get("list_price", 0))) / max(1.0, median))))
    return pool[:n]


def compute_category_demand_scores(
    hub: dict[str, Any],
    all_categories: set[str],
    forum_posts: list[dict[str, Any]],
    cat_recent: dict[str, int] | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Combine 1st-party shop inquiries, missed demand, recent ledger settles, and forum mentions."""
    radar = hub.get("demand_radar", {})
    inbound = radar.get("inbound_wants", {})
    missed = radar.get("missed_wants", {})
    settles = cat_recent or {}

    # Map leaf category names (e.g. "침구", "김치") to full category paths
    leaf_to_full: dict[str, set[str]] = collections.defaultdict(set)
    for c in all_categories:
        leaf = c.split(">")[-1].strip().lower()
        leaf_to_full[leaf].add(c)
        leaf_to_full[c.strip().lower()].add(c)
    for sku, info in (catalog or {}).items():
        category = info.get("category")
        if category in all_categories:
            leaf_to_full[str(sku).lower()].add(category)

    scores: dict[str, float] = {c: 0.0 for c in all_categories}

    def add_signal(source_dict: dict[str, int], weight: float) -> None:
        for key, cnt in source_dict.items():
            k_low = str(key).strip().lower()
            matched_cats = leaf_to_full.get(k_low, [])
            if not matched_cats:
                matched_cats = [c for c in all_categories if k_low in c.lower()]
            for c in matched_cats:
                scores[c] = scores.get(c, 0.0) + float(cnt) * weight

    add_signal(missed, 3.0)       # Highest priority: buyers knocked on our door and we had no stock!
    add_signal(inbound, 1.8)      # Buyers actively asking our shop
    # radar["buyer_orders"] (our own buyer's orders) is left out on purpose: a shop and a buyer with the same
    # owner cannot settle with each other, so those categories are not demand our shop can sell into.
    add_signal(settles, 1.0)      # Market-wide settlements in the last RECENT_S (any shop)

    # Scan recent forum posts for category leaf mentions
    for post in (forum_posts or [])[:40]:
        txt = str(post.get("text") or "").lower()
        # Untrusted prose only supplies a weak category hint, never price/quantity.
        if not txt or not any(word in txt for word in ("구합니다", "구매 희망", "삽니다")):
            continue
        for leaf, full_list in leaf_to_full.items():
            if len(leaf) >= 2 and leaf in txt:
                for c in full_list:
                    scores[c] = scores.get(c, 0.0) + 0.5

    return scores


def build_restock_plan(market_dir: Path) -> dict[str, Any]:
    catalog = load_catalog(market_dir)
    inv = load_inventory(market_dir)
    if not catalog or not inv:
        return {"buy": []}

    # Bankruptcy: with a restock hook the kit files only when we print "bankrupt": true (market_kit.py restock()).
    # Mirror the kit's default rule: below the line, filings left, and nothing left to sell.
    balance = int(inv.get("balance") or 0)
    sellable = [u for u in (inv.get("units") or []) if u.get("grade") != "expired"]
    if balance < int(inv.get("bankrupt_line") or 0) and int(inv.get("bankruptcies_left") or 0) > 0 and not sellable:
        return {"buy": [], "bankrupt": True}

    # Near the end of the round, stock bought now is unlikely to sell before it is worth 0: stop buying
    if time.time() > ROUND_END_TS - STOP_BUYING_BEFORE_END_S:
        return {"buy": []}

    # The ledger fetch (up to a few seconds) runs before taking the lock, so seller.py is not kept waiting
    snapshot = load_hub()
    rows = fetch_ledger_rows(int(snapshot.get("ledger_seq", 0)))
    window = supply_window(inv)
    # Backfill supplies on deployment/window transition even if a different hook consumed their seqs.
    if window and str(window) not in snapshot.get("restock_windows", {}):
        rows += fetch_ledger_rows(0)
    own_urls = fetch_own_shop_urls()
    try:
        inventory_fresh = 0 <= time.time() - (market_dir / "inventory.json").stat().st_mtime <= 120
    except OSError:
        inventory_fresh = False
    with mutate_hub() as hub:
        hub["own_shop_urls"] = sorted(set(hub.get("own_shop_urls", [])) | set(own_urls))
        # A deployment's truncated ledger tail cannot prove this window's prior
        # spending. Sacrifice the first window (including a true cold start), and
        # persist the block across restarts. Never prune window entries.
        if window and not hub.get("restock_windows"):
            hub.setdefault("restock_windows", {})[str(window)] = {
                "baseline_actual": 0, "reserved": WINDOW_BUDGET, "blocked": True}
        return plan_with_hub(hub, market_dir, catalog, inv, rows, inventory_fresh=inventory_fresh)


def plan_with_hub(
    hub: dict[str, Any],
    market_dir: Path,
    catalog: dict[str, dict[str, Any]],
    inv: dict[str, Any],
    rows: list[dict[str, Any]],
    *, inventory_fresh: bool = False,
) -> dict[str, Any]:
    """The plan itself. Runs inside mutate_hub, which saves the hub on return: do not save it here."""
    # Keep evidence of previously verified own sales before store's short tail is
    # pruned. Only catalog SKUs qualify; this is not a market-demand prediction.
    proven = set(hub.get("restock_proven_skus", []))
    proven.update(s for s, times in hub.get("demand_radar", {}).get("own_sku_sold_ts", {}).items()
                  if times)
    proven.update(e.get("sku") for e in hub.get("own_sale_events", {}).values())
    apply_ledger_rows(hub, rows, catalog)
    proven.update(s for s, times in hub.get("demand_radar", {}).get("own_sku_sold_ts", {}).items()
                  if times)
    hub["restock_proven_skus"] = sorted(s for s in proven if s in catalog)
    cs = update_capital_stage(hub, inv)
    strategy = decide_restock(hub, inv, time.time(), inventory_fresh=inventory_fresh)
    forum_posts = load_json_safe(market_dir / "forum.json", []) or []

    stage = int(cs.get("stage", 1))
    window = supply_window(inv)
    if window is None or time.time() > ROUND_END_TS - STOP_BUYING_BEFORE_END_S:
        return {"buy": []}
    opens, closes = window
    actual = sum(s["total"] for s in hub.get("own_supplies", {}).values()
                 if opens <= s["ts"] < closes)
    budget_state = hub.setdefault("restock_windows", {}).setdefault(
        str(window), {"baseline_actual": actual, "reserved": 0})
    if budget_state.get("blocked"):
        return {"buy": []}
    # Failed plans stay reserved for this window. Actual successes replace, rather
    # than double-charge, those estimates; deployment's prior actual spending stays charged.
    balance_now = int(inv.get("balance") or 0)
    is_cash_rich = balance_now >= 5_500_000
    is_full_catalog = len(catalog) >= 50
    if is_cash_rich and is_full_catalog:
        eff_window_budget = SUPER_CASH_WINDOW_BUDGET if balance_now >= 6_200_000 else HIGH_CASH_WINDOW_BUDGET
    else:
        eff_window_budget = PROFITABLE_WINDOW_BUDGET if is_cash_rich else WINDOW_BUDGET
    exp_per_window = 8 if (is_cash_rich and is_full_catalog) else EXPLORATION_PER_WINDOW
    exp_skus_cap = 55 if (is_cash_rich and is_full_catalog) else EXPLORATION_SKUS
    exp_units_cap = 95 if (is_cash_rich and is_full_catalog) else EXPLORATION_UNITS
    if is_cash_rich and is_full_catalog:
        exp_skus_cap = min(MAX_EXPLORATION_SKUS, max(exp_skus_cap, strategy.get("exploration_skus", exp_skus_cap)))
    # A policy/cash change never raises an already-started window's cap.
    eff_window_budget = min(eff_window_budget, MAX_WINDOW_BUDGET,
                            budget_state.setdefault("window_cap", eff_window_budget))
    exploration_cap = int(eff_window_budget * .4 * max(.5, min(1., strategy.get("exploration_factor", 1.))))
    exploration_reserved = budget_state.setdefault("exploration_reserved", budget_state["reserved"])
    used = max(actual, budget_state["baseline_actual"] + budget_state["reserved"])
    avail_budget = int(cs.get("available_budget", 0))
    tick_budget = min(avail_budget, max(0, eff_window_budget - used))
    if tick_budget <= 0:
        return {"buy": []}

    # Group catalog SKUs by category
    cat_map: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    sku_ci_map: dict[str, str] = {}
    for sku, info in catalog.items():
        sku_ci_map[sku.lower()] = sku
        if isinstance(info, dict) and info.get("category"):
            item_copy = dict(info, sku=sku)
            cat_map[info["category"]].append(item_copy)

    # Analyze current active inventory
    units = [u for u in (inv.get("units") or []) if u.get("grade") != "expired"]
    held_skus = {u["sku"] for u in units if u.get("sku")}
    held_food_units = sum(
        1 for u in units
        if (catalog.get(u.get("sku", ""), {}).get("kind") or u.get("kind")) == "food"
    )

    # Market sales rank candidates only; only our own shop's recent sales justify depth.
    now_s = time.time()
    radar = hub.get("demand_radar", {})
    sku_recent = {s: n for s, ts in (radar.get("own_sku_sold_ts") or {}).items()
                  if (n := sum(1 for t in ts if 0 <= now_s - t <= OWN_RECENT_S)) > 0}
    sku_market_recent = {s: n for s, ts in (radar.get("sku_sold_ts") or {}).items()
                         if (n := sum(1 for t in ts if 0 <= now_s - t <= RECENT_S)) > 0}
    cat_recent = {c: n for c, ts in (radar.get("cat_sold_ts") or {}).items()
                  if (n := sum(1 for t in ts if 0 <= now_s - t <= RECENT_S)) > 0}

    # 1-hour out-of-stock missed inquiries (from seller.py missed_ts / missed_wants and failed ledger results)
    missed_skus_1h: dict[str, int] = collections.Counter()
    missed_ts_map = radar.get("missed_ts") or {}
    for key, ts_list in missed_ts_map.items():
        recent_cnt = sum(1 for t in (ts_list or []) if 0 <= now_s - float(t) <= MISSED_RECENT_S)
        if recent_cnt > 0 and str(key).lower() in sku_ci_map:
            missed_skus_1h[sku_ci_map[str(key).lower()]] += recent_cnt
    for key, cnt in (radar.get("missed_wants") or {}).items():
        if key not in missed_ts_map and int(cnt or 0) > 0 and str(key).lower() in sku_ci_map:
            c_sku = sku_ci_map[str(key).lower()]
            missed_skus_1h[c_sku] = max(missed_skus_1h[c_sku], int(cnt))
    own_urls_set = set(hub.get("own_shop_urls") or [])
    for e in rows:
        if e.get("kind") != "result":
            continue
        ts_val = e.get("ts")
        if ts_val is None or not (0 <= now_s - float(ts_val) <= MISSED_RECENT_S):
            continue
        d = e.get("data") or {}
        if d.get("verified") or str(d.get("shop") or "").rstrip("/") not in own_urls_set:
            continue
        rev_txt = f"{d.get('item') or ''} {d.get('review') or ''}"
        for tok in rev_txt.replace("(", " ").replace(")", " ").replace(",", " ").split():
            if tok.lower() in sku_ci_map:
                missed_skus_1h[sku_ci_map[tok.lower()]] += 1

    demand_scores = compute_category_demand_scores(hub, set(cat_map.keys()), forum_posts, cat_recent, catalog)
    medians = {c: float(statistics.median(int(x.get("list_price", 0)) for x in skus))
               for c, skus in cat_map.items()}
    held_count = collections.Counter(u.get("sku") for u in units)

    # Prefer observed demand, then the original small long-shelf/cheap seed set.
    # This is a tie-break preference, not a restriction on catalog categories.
    ranked_demand_cats = [
        c for c, sc in sorted(
            demand_scores.items(),
            key=lambda x: -(x[1] * (math.sqrt(max(15_000.0, medians.get(x[0], 30_000.0))) if is_cash_rich else 1.0)),
        ) if sc > 0
    ]
    seed_goods = [c for c in SEED_GOODS_CATEGORIES if c in cat_map and medians.get(c, 0) <= SEED_GOODS_MAX_MEDIAN][:SEED_GOODS_COUNT]
    candidate_categories: list[str] = []
    for c in ranked_demand_cats + seed_goods + long_shelf_food_categories(cat_map):
        if c in cat_map and c not in candidate_categories:
            candidate_categories.append(c)

    max_goods_price = MAX_GOODS_STAGE3_PRICE if stage >= 3 else MAX_GOODS_STAGE1_PRICE
    buy_orders: list[dict[str, Any]] = []
    spent_est = 0
    exploration_spent = 0
    planned = budget_state.setdefault("planned_skus", {})
    expanded = budget_state.setdefault("assortment_categories", [])
    # Reservations count as coverage even when inventory publication is delayed.
    held_skus.update(s for s in planned if s in catalog)
    projected_units = len(units) + sum(max(0, n - held_count[s]) for s, n in planned.items())

    def add_line(sku: str, qty: int, limit: int, *, experiment: bool = False) -> int:
        """Queue qty units of sku if the tick budget allows; returns how many were queued."""
        nonlocal spent_est, projected_units, exploration_spent
        if sku in planned or any(b["sku"] == sku for b in buy_orders):
            return 0
        unit_cost = max(1, math.ceil(int(catalog.get(sku, {}).get("list_price", 0)) * 0.75))
        if experiment:
            limit = min(limit, spent_est + max(0, exploration_cap - exploration_reserved - exploration_spent))
        qty = min(qty, 20, (min(tick_budget, limit) - spent_est) // unit_cost)
        if qty <= 0:
            return 0
        buy_orders.append({"sku": sku, "qty": qty})
        held_skus.add(sku)
        held_count[sku] += qty
        spent_est += unit_cost * qty
        if experiment:
            exploration_spent += unit_cost * qty
        projected_units += qty
        planned[sku] = qty
        return qty

    # Known small baselines are targets, never orders to purchase regardless of stock.
    active_caps = {**TARGET_CAPS, **HIGH_VALUE_CAPS} if is_cash_rich else TARGET_CAPS
    hot_market_skus = [s for s, n in sku_market_recent.items()
                       if is_cash_rich and n >= 2
                       and catalog.get(s, {}).get("kind", "goods") == "goods"
                       and 35_000 <= int(catalog.get(s, {}).get("list_price", 0)) <= max_goods_price]
    candidates = sorted(
        set(active_caps) | set(proven) | set(missed_skus_1h) | set(sku_recent) | set(hot_market_skus),
        key=lambda s: (
            s not in proven and s not in missed_skus_1h and s not in active_caps,
            held_count[s] > 0,
            catalog.get(s, {}).get("kind", "goods") == "food",
            not (s in proven or s in missed_skus_1h or (s in active_caps and sku_market_recent.get(s, 0) >= 2)),
            -missed_skus_1h.get(s, 0),
            -sku_recent.get(s, 0),
            -sku_market_recent.get(s, 0) * int(catalog.get(s, {}).get("list_price", 0)),
            s,
        ),
    )
    core_limit = min(tick_budget, max(0, int(eff_window_budget * .6) - used))
    high_unit_allowed = not budget_state.get("high_unit_allowance", False)

    def eligible(info: dict[str, Any], own: bool = False) -> bool:
        lp = int(info.get("list_price", 0))
        kind = info.get("kind", "goods")
        if kind == "food" and held_food_units >= MAX_ACTIVE_FOOD_UNITS and not (own and lp < MIN_FOOD_LIST_PRICE):
            return False
        return (0 < lp <= (max_goods_price if kind == "goods" else MAX_FOOD_LIST_PRICE)
                and not (kind == "food" and (info.get("shelf_hours") or 0) < 96 and not own))

    for sku in dict.fromkeys(candidates):
        if len(buy_orders) >= 10:
            break
        info = catalog.get(sku)
        if not info:
            continue
        count = sku_recent.get(sku, 0)
        kind = info.get("kind", "goods")
        lp = int(info.get("list_price", 0))
        if not eligible(info, sku in proven or sku in missed_skus_1h):
            continue
        cap = min(active_caps.get(sku, 3 if kind == "goods" else 4), 3 if kind == "goods" else 4)
        if held_count[sku]:
            # Market popularity or rich cash never justifies topping up stocked SKUs.
            need = min(cap, math.ceil(count * 1.25)) - held_count[sku] if count > held_count[sku] else 0
        elif lp >= MIN_FOOD_LIST_PRICE and (kind == "goods" or (info.get("shelf_hours") or 0) >= 96):
            # Sold-out proven/missed/core items: restore immediately at qty=1 without increasing depth.
            if sku in proven or sku in missed_skus_1h or count > 0 or (sku in active_caps and sku_market_recent.get(sku, 0) >= 2) or sku in hot_market_skus:
                need = 1
            else:
                need = 0
        elif count:
            need = min(2, cap, math.ceil(count * 1.25))
        elif (sku in active_caps and (sku in proven or sku_market_recent.get(sku, 0) >= 2)) or sku in hot_market_skus:
            need = 1
        else:
            need = 0
        if need > 0:
            experiment = sku not in active_caps and sku not in proven and sku not in missed_skus_1h
            if experiment and (
                    len(held_skus) >= exp_skus_cap or projected_units >= exp_units_cap
                    or len(expanded) >= exp_per_window or info.get("category") in expanded
                    or price_covered(info, held_skus, catalog)):
                continue
            cost = math.ceil(lp * .75)
            limit = core_limit
            # One expensive unit may cross the 60% partition, but leaves 50k
            # for breadth in a rich window (e.g. the 423,810 mattress).
            allowance = (is_cash_rich and high_unit_allowed and not held_count[sku]
                         and cost > core_limit - spent_est
                         and spent_est + cost <= tick_budget - 50_000)
            if allowance:
                limit = spent_est + cost
            added = add_line(sku, 1 if allowance else need, limit, experiment=experiment)
            if added:
                if kind == "food":
                    held_food_units += added
                if experiment:
                    expanded.append(info.get("category"))
                if allowance:
                    high_unit_allowed = False
                    budget_state["high_unit_allowance"] = True

    # Soft caps stop experiments, not proven/core stockout restoration above.
    # Rank all categories: absent category first, then 1h missed/demand, then >=100k non-food (goods).
    covered_cats = {catalog[s].get("category") for s in held_skus if s in catalog}
    cat_has_high_goods = {
        c: any(x.get("kind", "goods") == "goods" and HIGH_GOODS_MIN_PRICE <= int(x.get("list_price", 0)) <= max_goods_price for x in skus)
        for c, skus in cat_map.items()
    }
    cat_is_goods = {
        c: any(x.get("kind", "goods") == "goods" for x in skus)
        for c, skus in cat_map.items()
    }
    ranked = sorted(
        cat_map,
        key=lambda c: (
            c in covered_cats,
            -demand_scores.get(c, 0),
            not cat_is_goods.get(c, False),
            not (is_cash_rich and cat_has_high_goods.get(c, False)),
            c not in candidate_categories,
            c,
        ),
    )
    for cat in ranked:
        if (len(buy_orders) >= 10 or len(expanded) >= exp_per_window
                or len(held_skus) >= exp_skus_cap or projected_units >= exp_units_cap):
            break
        if cat in expanded:
            continue
        if is_cash_rich and is_full_catalog and demand_scores.get(cat, 0) <= 0 and not cat_has_high_goods.get(cat, False):
            continue
        held_bands = {price_band(int(catalog[s].get("list_price", 0)), medians[cat])
                      for s in held_skus if s in catalog and catalog[s].get("category") == cat}
        pool = [x for x in cat_map[cat] if x["sku"] not in held_skus and eligible(x)
                and x["sku"] not in TARGET_CAPS and x["sku"] not in HIGH_VALUE_CAPS
                and not price_covered(x, held_skus, catalog)
                and price_band(int(x.get("list_price", 0)), medians[cat]) not in held_bands]
        pool.sort(key=lambda x: (
            not (is_cash_rich and x.get("kind", "goods") == "goods" and int(x.get("list_price", 0)) >= HIGH_GOODS_MIN_PRICE),
            abs(math.log(max(1, int(x.get("list_price", 0))) / max(1., medians[cat]))),
            x["sku"],
        ))
        for info in pool:
            added = add_line(info["sku"], 1, tick_budget, experiment=True)
            if added:
                if info.get("kind") == "food":
                    held_food_units += added
                expanded.append(cat)
                break

    budget_state["reserved"] += spent_est
    budget_state["exploration_reserved"] += exploration_spent

    # Optional Shop Forum promotion (max 1 post per 320s) when we have rich certified stock
    now = int(time.time())
    last_shop_post = int(hub.get("forum_state", {}).get("last_shop_post_ts", 0))
    if len(units) >= 6 and (now - last_shop_post) >= 330:
        refs = (hub.get("demand_radar", {}).get("recent_refs") or [])[-3:]
        top_cats = list({u.get("category", "").split(">")[-1] for u in units if u.get("category")})[:5]
        post_text = (
            f"[mjforge Fulfillment Report] Stage-{stage} inventory active ({len(units)} certified units across "
            f"{', '.join(top_cats)}). All units ship with market certificate (cert=true), honest grade claims, "
            f"6h Safe-Care (안심케어) full-refund warranty on all goods (A/B can drop it for a lower price), "
            f"and instant 1-turn discount below list price."
        )
        queue_forum_action(market_dir, {"post": post_text, "refs": refs, "as": "shop"})
        hub["forum_state"]["last_shop_post_ts"] = now

    return {"buy": buy_orders[:10]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=".market", help="Path to .market directory")
    args = ap.parse_args()

    market_dir = Path(args.state)
    plan = build_restock_plan(market_dir)
    print(json.dumps(plan, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
