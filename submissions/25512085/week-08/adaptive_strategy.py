"""Deterministic, bounded 20-minute policies; no network, LLM, or extra scheduler.

State belongs in the existing locked hub. Missing observations keep the policy.
Old stock is a *risk* signal, never proof of no sales over its entire lifetime.
"""
from __future__ import annotations

import json
import math
import sys

INTERVAL_S = 1200
MAX_WINDOW_BUDGET = 3_000_000
MAX_EXPLORATION_SKUS = 60
MAX_EXPLORATION_UNITS = 95  # Base cash-rich cap before equity-target expansion.
MAX_FAILURE_EVENTS = 600


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def note_ledger_coverage(hub, rows, now):
    """Prove continuous sequence coverage; a truncated/gapped tail restarts warmup."""
    coverage = hub.setdefault("adaptive_ledger_coverage", {})
    entries = {int(e["seq"]): e for e in rows
               if number(e.get("seq")) and number(e.get("ts")) and 0 <= e["ts"] <= now}
    for seq, entry in sorted(entries.items()):
        previous = coverage.get("seq")
        if previous is not None and seq <= previous:
            continue
        if previous is None or seq != previous + 1:
            coverage["since"] = entry["ts"]
        coverage.update(seq=seq, through=entry["ts"])


def decide_restock(hub, inv, now, *, inventory_fresh=False):
    """Return overrides only; cash floors, caps, safety and buy evidence live in restock.

    All event counts use (now-1200, now]. At least 20 min continuous ledger
    coverage plus fresh, fully costed/aged inventory is required for a change.
    """
    state = hub.setdefault("adaptive_strategy", {})
    if now - state.get("last_decision", -INTERVAL_S) < INTERVAL_S:
        return state
    state["last_decision"] = now
    state.setdefault("mode", "maintain")
    state.setdefault("exploration_factor", 1.0)
    coverage = hub.get("adaptive_ledger_coverage", {})
    units = inv.get("units")
    complete = (inventory_fresh and isinstance(units, list) and number(inv.get("balance"))
                and number(coverage.get("since")) and coverage["since"] <= now - INTERVAL_S
                and number(coverage.get("through")) and 0 <= now - coverage["through"] <= 120
                and all(isinstance(u, dict) and number(u.get("cost")) and u["cost"] >= 0
                        and number(u.get("supplied_at")) and 0 < u["supplied_at"] <= now
                        and u.get("sku") for u in units))
    if not complete:
        state["reason"] = "incomplete-observation: policy held"
    else:
        active = [u for u in units if u.get("grade") != "expired"]
        sales = [e for e in hub.get("own_sale_events", {}).values()
                 if now - INTERVAL_S < e.get("ts", 0) <= now]
        sold = {e["sku"] for e in sales}
        radar = hub.get("demand_radar", {})
        misses = sum(sum(now - INTERVAL_S < t <= now for t in times)
                     for times in radar.get("missed_ts", {}).values())
        cost = sum(u["cost"] for u in active)
        # Age + cost exposure + no *recent* own turnover, not cash-profit delta.
        old_cost = sum(u["cost"] for u in active
                       if now - u["supplied_at"] >= 6 * 3600 and u["sku"] not in sold)
        obs = {"at": now, "since": now - INTERVAL_S, "balance": inv["balance"],
               "own_sales": len(sales), "misses": misses, "stock_cost": cost,
               "old_cost": old_cost, "old_cost_share": old_cost / cost if cost else 0,
               "held_skus": len({u["sku"] for u in active})}
        if old_cost >= 300_000 and obs["old_cost_share"] >= .35 and inv.get("balance", 0) < 10_000_000:
            state.update(mode="recover", exploration_factor=.5,
                         reason="aged-cost exposure; core restoration unchanged")
        elif inv["balance"] >= 6_200_000 and sales and (len(sales) >= 3 or misses > 0):
            state.update(mode="expand", exploration_factor=1.0,
                         exploration_skus=MAX_EXPLORATION_SKUS,
                         reason="cash headroom and own turnover/missed demand")
        else:
            state.update(mode="maintain", exploration_factor=1.0,
                         reason="no expansion evidence")
            state.pop("exploration_skus", None)
        state["baseline"] = obs
    # JSON-escaped, local numeric/categorical summary only; never seller prose.
    print(json.dumps("[adaptive] " + json.dumps({k: state.get(k) for k in
          ("last_decision", "mode", "exploration_skus", "exploration_factor", "reason", "baseline")},
          sort_keys=True)), file=sys.stderr, flush=True)
    return state


def record_failure(hub, order, url, sku, reason, now):
    """One failure per order/pair, not one per counter/retry. No inferred shop guilt."""
    if not order or reason not in ("price", "rejected", "timeout", "network-timeout", "unsigned"):
        return
    state = hub.setdefault("adaptive_strategy", {})
    events = [e for e in state.get("buyer_failures", []) if now - INTERVAL_S < e["ts"] <= now]
    event = {"order": str(order), "url": url.rstrip("/"), "sku": sku,
             "reason": reason, "ts": now}
    if not any((e["order"], e["url"], e["sku"]) == (event["order"], event["url"], sku) for e in events):
        events.append(event)
    state["buyer_failures"] = events[-MAX_FAILURE_EVENTS:]


def refresh_buyer_policy(hub, now):
    state = hub.setdefault("adaptive_strategy", {})
    if now - state.get("buyer_last_decision", -INTERVAL_S) < INTERVAL_S:
        return
    state["buyer_last_decision"] = now
    events = [e for e in state.get("buyer_failures", []) if now - INTERVAL_S < e["ts"] <= now]
    state["buyer_failures"] = events[-MAX_FAILURE_EVENTS:]
    grouped = {}
    for e in events:
        # Only transport/signature failures span SKUs. Price/stock stay pair-local.
        scope = "" if e["reason"] in ("network-timeout", "unsigned") else e["sku"]
        key = (e["url"], scope, e["reason"])
        grouped.setdefault(key, set()).add(e["order"])
    cooldowns = [{"url": url, "sku": sku, "reason": reason, "failures": len(orders),
                  "until": now + INTERVAL_S, "hard": reason == "unsigned"}
                 for (url, sku, reason), orders in sorted(grouped.items()) if len(orders) >= 3]
    state["buyer_cooldowns"] = cooldowns
    print(json.dumps(f"[buyer] [adaptive] 20m buyer cooldowns={len(cooldowns)} events={len(events)}"),
          file=sys.stderr, flush=True)


def filter_candidates(candidates, hub, now):
    """Keep ordinary utility ranking; if all soft-cooled, try the least failed safe pair."""
    active = [c for c in hub.get("adaptive_strategy", {}).get("buyer_cooldowns", [])
              if now < c["until"] <= now + INTERVAL_S]
    available, fallback = [], []
    for candidate in candidates:
        _, shop, sku = candidate
        matches = [c for c in active if c["url"] == shop["url"]
                   and (not c["sku"] or c["sku"] == sku["sku"])]
        if not matches:
            available.append(candidate)
        elif not any(c["hard"] for c in matches) and not shop.get("dishonest"):
            fallback.append((sum(c["failures"] for c in matches), candidate))
    if available:
        return available
    return [min(fallback, key=lambda pair: (pair[0], -pair[1][0]))[1]] if fallback else []
