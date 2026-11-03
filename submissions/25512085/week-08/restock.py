"""Round 3 Supply & Capital Allocation Engine (restock.py).

Implements the Coupang-style 3-Stage Scale-Up Strategy:
- Stage 1 (Seed): within a 2,500,000 stock cap, a few units in 9 "goods" categories plus 2 units in each of the
  24 food categories whose shelf life outlasts the round, picking SKUs priced nearest the category's
  median_price so they fall inside most orders' [0.5x, 1.5x] `acceptable` window.
- Stage 2 (Demand-Driven Growth): Reinvest 75% of realized profit into expanding the inventory cap,
  prioritizing categories with the highest Demand Score (3.0x missed shop inquiries + 1.8x shop inquiries
  + 1.0x ledger settlements + forum demand). Our own buyer's orders are not demand our shop can serve.
- Stage 3 (Full Assortment Scale-Up): Expand across all active categories while enforcing a strict
  cash reserve floor (half the balance, at most 1,500,000, at least 200,000 above the bankruptcy line).
- End of round: no buying in the last 36 hours (stock left when the round ends is worth 0 to net profit).

Outputs a single JSON line: {"buy": [{"sku": "...", "qty": 1..20}, ...]} (at most 10 lines).
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

from store import (
    ROUND_END_TS,
    SEED_GOODS_CATEGORIES,
    STOP_BUYING_BEFORE_END_S,
    apply_ledger_rows,
    fetch_ledger_rows,
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
MAX_GOODS_STAGE1_PRICE = 360_000
MAX_GOODS_STAGE3_PRICE = 650_000

MIN_FOOD_LIST_PRICE = 10_000
MAX_FOOD_LIST_PRICE = 120_000

# Food that outlasts the round (two weeks): no storage fee and no breakage, so it is the cheap way to be visible in
# many categories while we learn what sells. The kit lists a shop to a buyer only with stock of a SKU in the order's
# acceptable price window (market_kit.py, offers), so being in a category means holding SKUs near its usual price.
ROUND_HOURS = 336
SEED_FOOD_CATEGORY_COUNT = 24


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
) -> dict[str, float]:
    """Combine 1st-party shop inquiries, missed demand, ledger settles, and forum mentions."""
    radar = hub.get("demand_radar", {})
    inbound = radar.get("inbound_wants", {})
    missed = radar.get("missed_wants", {})
    settles = radar.get("market_settles", {})

    # Map leaf category names (e.g. "침구", "김치") to full category paths
    leaf_to_full: dict[str, list[str]] = collections.defaultdict(list)
    for c in all_categories:
        leaf = c.split(">")[-1].strip().lower()
        leaf_to_full[leaf].append(c)
        leaf_to_full[c.lower()].append(c)

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
    add_signal(settles, 1.0)      # Confirmed market-wide settlements

    # Scan recent forum posts for category leaf mentions
    for post in (forum_posts or [])[:40]:
        txt = str(post.get("text") or "").lower()
        if not txt:
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
    rows = fetch_ledger_rows(int(load_hub().get("ledger_seq", 0)))
    with mutate_hub() as hub:
        return plan_with_hub(hub, market_dir, catalog, inv, rows)


def plan_with_hub(
    hub: dict[str, Any],
    market_dir: Path,
    catalog: dict[str, dict[str, Any]],
    inv: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """The plan itself. Runs inside mutate_hub, which saves the hub on return: do not save it here."""
    apply_ledger_rows(hub, rows, catalog)
    cs = update_capital_stage(hub, inv)
    forum_posts = load_json_safe(market_dir / "forum.json", []) or []

    stage = int(cs.get("stage", 1))
    avail_budget = int(cs.get("available_budget", 0))
    if avail_budget < 8_000:
        return {"buy": []}

    # Group catalog SKUs by category
    cat_map: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for sku, info in catalog.items():
        if isinstance(info, dict) and info.get("category"):
            item_copy = dict(info, sku=sku)
            cat_map[info["category"]].append(item_copy)

    # Analyze current active inventory
    units = [u for u in (inv.get("units") or []) if u.get("grade") != "expired"]
    held_skus = {u["sku"] for u in units if u.get("sku")}
    cat_unit_counts: dict[str, int] = collections.Counter(u.get("category", "") for u in units)

    demand_scores = compute_category_demand_scores(hub, set(cat_map.keys()), forum_posts)
    medians = category_medians(market_dir, cat_map)

    # Prioritize candidate categories:
    # 1. High-demand categories from Demand Radar (where score > 0 and stock < 4)
    # 2. Stage 1 core high-margin non-food ("goods") categories
    # 3. Long-shelf-life (720h) safe food categories
    ranked_demand_cats = [
        c for c, sc in sorted(demand_scores.items(), key=lambda x: -x[1])
        if sc > 0 and cat_unit_counts.get(c, 0) < (5 if stage >= 2 else 3)
    ]

    candidate_categories: list[str] = []
    for c in ranked_demand_cats + SEED_GOODS_CATEGORIES + long_shelf_food_categories(cat_map):
        if c in cat_map and c not in candidate_categories:
            candidate_categories.append(c)

    # In Stage 2/3, also include remaining goods categories with moderate prices
    if stage >= 2:
        for c, skus in sorted(cat_map.items(), key=lambda x: -len(x[1])):
            if c not in candidate_categories:
                candidate_categories.append(c)

    max_goods_price = MAX_GOODS_STAGE3_PRICE if stage >= 3 else MAX_GOODS_STAGE1_PRICE
    buy_orders: list[dict[str, Any]] = []
    spent_est = 0

    # Target at most 65% of remaining available budget in a single 60s tick to pace capital smoothly
    tick_budget = int(avail_budget * (0.75 if len(units) < 10 else 0.50))
    tick_budget = max(min(avail_budget, 250_000), tick_budget)

    for cat in candidate_categories:
        if len(buy_orders) >= 10 or spent_est >= tick_budget:
            break

        current_cat_qty = cat_unit_counts.get(cat, 0)
        is_hot = demand_scores.get(cat, 0) >= 2.0
        max_cat_units = 5 if (stage >= 2 and is_hot) else (3 if cat in SEED_GOODS_CATEGORIES else 2)
        if current_cat_qty >= max_cat_units:
            continue

        cat_skus = cat_map[cat]
        kind = cat_skus[0].get("kind", "goods")
        shelf = cat_skus[0].get("shelf_hours")

        # Perishable short-shelf food (< 96h) is only stocked JIT when active demand is observed!
        if kind == "food" and shelf is not None and shelf < 96 and not is_hot:
            continue

        median = medians.get(cat) or 0.0
        if kind == "goods":
            n = 3 if cat in SEED_GOODS_CATEGORIES else 2
            picks = select_near_median(cat_skus, held_skus, median, n, MIN_GOODS_LIST_PRICE, max_goods_price)
        else:
            picks = select_near_median(cat_skus, held_skus, median, 2, MIN_FOOD_LIST_PRICE, MAX_FOOD_LIST_PRICE)

        for sku_info in picks:
            if len(buy_orders) >= 10 or spent_est >= tick_budget or current_cat_qty >= max_cat_units:
                break
            list_p = int(sku_info.get("list_price", 0))
            # Estimated supply cost is ~62% of list_price on average
            est_unit_cost = max(1, int(list_p * 0.65))
            if spent_est + est_unit_cost > tick_budget:
                continue

            # Buy 1 unit for high-ticket goods (>80k) to maximize SKU diversity, or up to 2 for hot/lower-ticket items
            qty = 2 if (is_hot and list_p <= 60_000 and spent_est + 2 * est_unit_cost <= tick_budget) else 1
            buy_orders.append({"sku": sku_info["sku"], "qty": qty})
            held_skus.add(sku_info["sku"])
            spent_est += est_unit_cost * qty
            current_cat_qty += qty

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
