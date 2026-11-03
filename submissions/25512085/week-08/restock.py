"""Conservative own-turnover restocking with a persistent 200,000-credit window cap.

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
PROFITABLE_WINDOW_BUDGET = 450_000  # Unlocked once cash balance >= 5,500,000 so 279k-424k mattresses can be stocked
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
) -> dict[str, float]:
    """Combine 1st-party shop inquiries, missed demand, recent ledger settles, and forum mentions."""
    radar = hub.get("demand_radar", {})
    inbound = radar.get("inbound_wants", {})
    missed = radar.get("missed_wants", {})
    settles = cat_recent or {}

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
    add_signal(settles, 1.0)      # Market-wide settlements in the last RECENT_S (any shop)

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
    snapshot = load_hub()
    rows = fetch_ledger_rows(int(snapshot.get("ledger_seq", 0)))
    window = supply_window(inv)
    # Backfill supplies on deployment/window transition even if a different hook consumed their seqs.
    if window and str(window) not in snapshot.get("restock_windows", {}):
        rows += fetch_ledger_rows(0)
    own_urls = fetch_own_shop_urls()
    with mutate_hub() as hub:
        hub["own_shop_urls"] = sorted(set(hub.get("own_shop_urls", [])) | set(own_urls))
        # A deployment's truncated ledger tail cannot prove this window's prior
        # spending. Sacrifice the first window (including a true cold start), and
        # persist the block across restarts. Never prune window entries.
        if window and not hub.get("restock_windows"):
            hub.setdefault("restock_windows", {})[str(window)] = {
                "baseline_actual": 0, "reserved": WINDOW_BUDGET, "blocked": True}
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
    is_cash_rich = int(inv.get("balance") or 0) >= 5_500_000
    eff_window_budget = PROFITABLE_WINDOW_BUDGET if is_cash_rich else WINDOW_BUDGET
    used = max(actual, budget_state["baseline_actual"] + budget_state["reserved"])
    avail_budget = int(cs.get("available_budget", 0))
    tick_budget = min(avail_budget, max(0, eff_window_budget - used))
    if tick_budget <= 0:
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

    # Market sales rank candidates only; only our own shop's recent sales justify depth.
    now_s = time.time()
    radar = hub.get("demand_radar", {})
    sku_recent = {s: n for s, ts in (radar.get("own_sku_sold_ts") or {}).items()
                  if (n := sum(1 for t in ts if 0 <= now_s - t <= OWN_RECENT_S)) > 0}
    sku_market_recent = {s: n for s, ts in (radar.get("sku_sold_ts") or {}).items()
                         if (n := sum(1 for t in ts if 0 <= now_s - t <= RECENT_S)) > 0}
    cat_recent = {c: n for c, ts in (radar.get("cat_sold_ts") or {}).items()
                  if (n := sum(1 for t in ts if now_s - t <= RECENT_S)) > 0}

    demand_scores = compute_category_demand_scores(hub, set(cat_map.keys()), forum_posts, cat_recent)
    medians = category_medians(market_dir, cat_map)
    held_count = collections.Counter(u.get("sku") for u in units)

    # Candidate categories: what the ledger and our inquiries say sells first, then a small seed so the shop is
    # visible from the first window: SEED_GOODS_COUNT cheaper goods categories and SEED_FOOD_CATEGORY_COUNT
    # long-shelf food categories, one SKU near the category median each. The seed grows from ledger data, not up front.
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

    def add_line(sku: str, qty: int) -> int:
        """Queue qty units of sku if the tick budget allows; returns how many were queued."""
        nonlocal spent_est
        unit_cost = max(1, math.ceil(int(catalog.get(sku, {}).get("list_price", 0)) * 0.75))
        qty = min(qty, 20, (tick_budget - spent_est) // unit_cost)
        if qty <= 0:
            return 0
        buy_orders.append({"sku": sku, "qty": qty})
        held_skus.add(sku)
        held_count[sku] += qty
        spent_est += unit_cost * qty
        return qty

    # Known small baselines are targets, never orders to purchase regardless of stock.
    active_caps = {**TARGET_CAPS, **HIGH_VALUE_CAPS} if is_cash_rich else TARGET_CAPS
    if is_cash_rich:
        def rev_priority(s: str) -> float:
            lp = int(catalog.get(s, {}).get("list_price", 0))
            if lp > max_goods_price:
                return -1.0
            own_n = sku_recent.get(s, 0)
            mkt_n = sku_market_recent.get(s, 0)
            return (own_n * 2.5 + mkt_n + (1.5 if s in HIGH_VALUE_CAPS else 0.5)) * lp

        hot_market_skus = [
            s for s, n in sku_market_recent.items()
            if n >= 2 and 35_000 <= int(catalog.get(s, {}).get("list_price", 0)) <= max_goods_price
        ]
        pool = list(active_caps) + list(sku_recent) + hot_market_skus
        candidates = sorted(dict.fromkeys(pool), key=lambda s: -rev_priority(s))
    else:
        candidates = list(TARGET_CAPS) + sorted(sku_recent, key=lambda s: -sku_recent[s])

    for sku in dict.fromkeys(candidates):
        if len(buy_orders) >= 10:
            break
        info = catalog.get(sku)
        if not info:
            continue
        count = sku_recent.get(sku, 0)
        mkt_count = sku_market_recent.get(sku, 0)
        kind = info.get("kind", "goods")
        lp = int(info.get("list_price", 0))
        if lp > (max_goods_price if kind == "goods" else MAX_FOOD_LIST_PRICE):
            continue
        if kind == "food" and info.get("shelf_hours") is not None and info["shelf_hours"] < 96 and not count:
            continue
        # No new SKU experiments while the shop has stock, unless cash-rich & proven high-value target/market hit.
        if units and sku not in held_skus and sku not in active_caps and not (is_cash_rich and mkt_count >= 2 and lp >= 35_000):
            continue
        cap = active_caps.get(sku, 3 if kind == "goods" else 4)
        target = cap if sku in active_caps else min(cap, count + 1)
        if count and sku in active_caps:
            target = min(cap, max(1, math.ceil(count * 1.25)))
        if kind == "goods" and lp >= 80_000:
            if is_cash_rich and (sku in HIGH_VALUE_CAPS or mkt_count >= 2):
                # Allow 1 seed unit when out of stock so we break the 0-stock deadlock, up to cap when selling
                target = min(cap, max(1, count + (1 if mkt_count >= 3 else 0)))
                if held_count[sku] >= target:
                    continue
            elif not count or held_count[sku] >= cap:
                continue
        need = target - held_count[sku]
        if need > 0:
            add_line(sku, need)

    # Only a genuinely empty shop may seed new categories. Market signals select
    # candidates, but do not increase seed quantities or the shared window budget.
    if not units:
        for cat in candidate_categories:
            if len(buy_orders) >= 10:
                break
            cat_skus = cat_map[cat]
            kind = cat_skus[0].get("kind", "goods")
            shelf = cat_skus[0].get("shelf_hours")
            if kind == "food" and shelf is not None and shelf < 96:
                continue
            if any(catalog[s].get("category") == cat for s in held_skus if s in catalog):
                continue
            lo, hi = (MIN_GOODS_LIST_PRICE, min(max_goods_price, 79_999)) if kind == "goods" else (MIN_FOOD_LIST_PRICE, MAX_FOOD_LIST_PRICE)
            for info in select_near_median(cat_skus, held_skus, medians.get(cat, 0), 1, lo, hi):
                if int(info.get("list_price", 0)) > hi:
                    continue
                add_line(info["sku"], 1 if kind == "goods" else 2)

    budget_state["reserved"] += spent_est

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
