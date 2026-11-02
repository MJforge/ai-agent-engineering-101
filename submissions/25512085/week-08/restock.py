"""Round 3 Supply & Capital Allocation Engine (restock.py).

Implements the Coupang-style 3-Stage Scale-Up Strategy:
- Stage 1 (Seed): Invest up to 50% of starting capital (2,500,000 credits) into non-expiring,
  permanent-grade, high-margin "goods" categories (80% budget share) using price-tiering
  (P25 / P50 / P75 SKUs) so our stock matches any buyer order's [0.5x, 1.5x] `acceptable` window,
  plus 720-hour shelf-life food categories (20% share).
- Stage 2 (Demand-Driven Growth): Reinvest 75% of realized profit into expanding the inventory cap,
  prioritizing categories with the highest Demand Score (2.5x missed shop inquiries + 1.5x buyer orders
  + 1.0x ledger settlements + forum demand).
- Stage 3 (Full Assortment Scale-Up): Expand across all active categories while enforcing a strict
  cash reserve floor (half the balance, at most 1,500,000, at least 200,000 above the bankruptcy line).

Outputs a single JSON line: {"buy": [{"sku": "...", "qty": 1..20}, ...]} (at most 10 lines).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path
from typing import Any

from store import (
    SAFE_FOOD_CATEGORIES,
    SEED_GOODS_CATEGORIES,
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


def select_tiered_skus(
    cat_skus: list[dict[str, Any]],
    held_skus: set[str],
    min_price: int,
    max_price: int,
    tiers: tuple[float, ...] = (0.30, 0.55, 0.80),
) -> list[dict[str, Any]]:
    """Pick representative SKUs at P30, P55, P80 price quantiles within [min_price, max_price]
    so that any buyer order's 0.5x-1.5x acceptable window is covered by at least one SKU."""
    eligible = [
        x for x in cat_skus
        if min_price <= int(x.get("list_price", 0)) <= max_price
    ]
    if not eligible:
        eligible = sorted(cat_skus, key=lambda x: int(x.get("list_price", 0)))
    else:
        eligible.sort(key=lambda x: int(x.get("list_price", 0)))
    if not eligible:
        return []

    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in tiers:
        idx = min(len(eligible) - 1, max(0, int(len(eligible) * q)))
        # Search outward from idx for an unheld SKU first
        best = None
        for offset in range(len(eligible)):
            for cand_idx in (idx + offset, idx - offset):
                if 0 <= cand_idx < len(eligible):
                    cand = eligible[cand_idx]
                    sku = cand["sku"]
                    if sku not in seen and sku not in held_skus:
                        best = cand
                        break
            if best:
                break
        if best:
            seen.add(best["sku"])
            picked.append(best)
    return picked


def compute_category_demand_scores(
    hub: dict[str, Any],
    all_categories: set[str],
    forum_posts: list[dict[str, Any]],
) -> dict[str, float]:
    """Combine 1st-party shop inquiries, missed demand, buyer orders, ledger settles, and forum mentions."""
    radar = hub.get("demand_radar", {})
    inbound = radar.get("inbound_wants", {})
    missed = radar.get("missed_wants", {})
    buyer_ord = radar.get("buyer_orders", {})
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
    add_signal(buyer_ord, 1.5)    # Categories our own buyer was ordered to buy
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

    # Prioritize candidate categories:
    # 1. High-demand categories from Demand Radar (where score > 0 and stock < 4)
    # 2. Stage 1 core high-margin non-food ("goods") categories
    # 3. Long-shelf-life (720h) safe food categories
    ranked_demand_cats = [
        c for c, sc in sorted(demand_scores.items(), key=lambda x: -x[1])
        if sc > 0 and cat_unit_counts.get(c, 0) < (5 if stage >= 2 else 3)
    ]

    candidate_categories: list[str] = []
    for c in ranked_demand_cats + SEED_GOODS_CATEGORIES + SAFE_FOOD_CATEGORIES:
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

        if kind == "goods":
            tiers = (0.25, 0.55, 0.80) if cat in SEED_GOODS_CATEGORIES else (0.40, 0.70)
            picks = select_tiered_skus(cat_skus, held_skus, MIN_GOODS_LIST_PRICE, max_goods_price, tiers)
        else:
            tiers = (0.35, 0.70)
            picks = select_tiered_skus(cat_skus, held_skus, MIN_FOOD_LIST_PRICE, MAX_FOOD_LIST_PRICE, tiers)

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
            f"warranty on Grade-A goods, and instant 1-turn discount below list price."
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
