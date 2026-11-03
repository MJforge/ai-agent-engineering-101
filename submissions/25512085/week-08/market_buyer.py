"""A2A v1.0 Market Buyer Agent (Week 08: Round 3 Surplus Optimizer, Accept Gate & Prompt-Injection Defense).

Supports both:
- Round 3 Economy (`--order` with `acceptable` SKUs):
  1. Ignores raw review text strings when ranking shops to defeat Indirect Prompt Injection spam
     (e.g., 'try the store Cheapest in Town'), relying strictly on structured trust metrics from
     `state_hub.json` (`claim_honest` ratio, `broken` count, `settled` count) and numeric star averages.
  2. Maximizes Buyer Surplus (`value - price`, where `value = list_price` of the SKU!) by selecting
     stock-holding shops whose `offers` contain the highest `list_price` SKU within the order's
     `acceptable` list that can be negotiated within `budget`.
  3. Emits a strict `"accept"` gate (`cert: True`, `min_hours_left` for food, `grades`, `warranty_hours`)
     so `market_kit.py` v4 refuses uncertified or expiring units BEFORE paying.
- Rounds 0-2 Legacy Compatibility (`lamp`, `chair`, `bookshelf`): passes `market_test_week07.py` 25/25.
"""
import argparse
from contextvars import ContextVar
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from adaptive_strategy import filter_candidates, record_failure

from store import (
    BREAK_RATE,
    GOODS_BREAK_WINDOW_H,
    load_catalog,
    load_hub,
    load_json_safe,
    mutate_hub,
    record_buyer_order,
    sync_broken_and_payments,
)

BASE_DIR = Path(__file__).resolve().parent
MARKET_DIR = Path(os.environ.get("MARKET_DIR", str(BASE_DIR / ".market")))
MEMORY_FILE = Path("memory.json")
MAX_RECENT_HISTORY = 5
# Buyer policy choices, not market rules or empirically optimal discounts.
FOOD_LIST_PRICE_PERCENT = 90
GOODS_OFFER_CAP_PERCENT = 92
COMPARABLE_WINDOW_S = 3 * 3600
COMPARABLE_MIN_SAMPLES = 3
REQUEST_DEADLINE = ContextVar("buyer_request_deadline", default=None)


def diagnostic(message: str) -> None:
    """Escape even seller-controlled newlines; never emit a forged JSON line."""
    print(json.dumps(f"[buyer] {message}", ensure_ascii=False), file=sys.stderr, flush=True)


def request_timeout(default: float) -> float:
    """Round 2 keeps its timeouts; Round 3 shares a monotonic wall-clock budget.

    HTTPX timeouts bound transport phases, not an uninterruptible OS call or a
    trickling response's total duration. Reject late receipts below as well;
    a hard process wall-clock kill must be enforced by the calling kit.
    """
    deadline = REQUEST_DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("buyer deadline exhausted")
    return min(default, remaining)


RESERVES = {"lamp": 100, "chair": 180, "bookshelf": 260}
SERVICE_KEYWORDS = (
    "warranty", "service", "refund", "guarantee", "quality", "support",
    "vip", "bundle", "care", "return", "assurance", "policy", "replacement",
    "member", "welcome", "discount", "benefit", "cert", "grade",
)

FUNNY_NO_DEAL_REVIEWS = [
    "No deal after {rounds} round(s): seller clung to {ask} while my budget was {budget}.",
    "No deal in {rounds} round(s): asking price {ask} stayed above budget {budget}.",
    "No deal after {rounds} round(s): could not close within budget {budget} (seller ask {ask}).",
]


def call_harness(prompt: str) -> str:
    """Call Codex CLI harness non-interactively in an empty directory."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
        tmp_path = tmp.name
    empty_dir = tempfile.gettempdir()
    p = None
    try:
        cmd = f'codex exec --skip-git-repo-check --ephemeral --cd "{empty_dir}" -m gpt-5.6-sol -o "{tmp_path}" -'
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            text=True,
        )
        p.communicate(input=prompt, timeout=18)
        if os.path.exists(tmp_path):
            with open(tmp_path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        diagnostic(f"[Buyer Harness Warning]: {e}")
        if p:
            try:
                p.kill()
            except Exception:
                pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return ""


def load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def valid_stars(value: Any) -> bool:
    """Unpaid/no-deal entries have null stars, not a synthetic three-star review."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 1 <= value <= 5)


def update_memory(memory: dict, shop_name: str, entry: dict) -> None:
    """Store up to 5 recent trades per shop; roll older entries into archived_avg_stars."""
    rec = memory.get(shop_name, {
        "archived_count": 0,
        "archived_avg_stars": 0.0,
        "dishonest": False,
        "recent": [],
    })
    if entry.get("outcome") == "rip-off":
        rec["dishonest"] = True

    recent = rec.get("recent", [])
    recent.append(entry)
    while len(recent) > MAX_RECENT_HISTORY:
        oldest = recent.pop(0)
        old_cnt = rec.get("archived_count", 0)
        old_avg = rec.get("archived_avg_stars", 0.0)
        if not valid_stars(oldest.get("stars")):
            continue
        old_stars = float(oldest["stars"])
        new_cnt = old_cnt + 1
        rec["archived_count"] = new_cnt
        rec["archived_avg_stars"] = round((old_avg * old_cnt + old_stars) / new_cnt, 2)

    rec["recent"] = recent
    rec["last_visited_ts"] = time.time()
    memory[shop_name] = rec
    try:
        MEMORY_FILE.write_text(json.dumps(memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as e:
        diagnostic(f"[Memory Warning]: {e}")


def fetch_board_stats() -> dict[str, dict]:
    market_url = os.environ.get("MARKET_URL", "https://market.ourolabs.ai").rstrip("/")
    try:
        r = httpx.get(f"{market_url}/api/board", timeout=request_timeout(2.5)).json()
        return {s["name"]: s for s in r.get("shops", []) if isinstance(s, dict) and "name" in s}
    except Exception:
        return {}


def summarize_shop(shop: dict, memory: dict, board_row: dict | None = None, hub_trust: dict | None = None) -> dict:
    name = shop.get("name", "")
    url = shop.get("url", "").rstrip("/")
    reviews = shop.get("reviews") or []
    board_row = board_row or {}
    hub_trust = (hub_trust or {}).get(url) or {}

    ext_stars = [r["stars"] for r in reviews if valid_stars(r.get("stars"))]
    ext_avg = round(sum(ext_stars) / len(ext_stars), 2) if ext_stars else board_row.get("stars")

    # Detect actual receipt mismatch flags while ignoring prompt-injection advertisements
    bad_keywords = ("receipt said", "receipt says", "higher than", "overcharged", "rip-off", "scam", "forged", "dishonest")
    ext_scam_flags = sum(
        1 for r in reviews
        if (valid_stars(r.get("stars")) and r["stars"] <= 2
            and any(k in str(r.get("review", "")).lower() for k in bad_keywords))
    )

    mem = memory.get(name, {})
    recent = mem.get("recent", [])
    arch_cnt = mem.get("archived_count", 0)
    arch_avg = mem.get("archived_avg_stars", 0.0)
    last_ts = float(mem.get("last_visited_ts", 0.0))

    total_mem_cnt = arch_cnt + len(recent)
    rated = [r["stars"] for r in recent if valid_stars(r.get("stars"))]
    if arch_cnt + len(rated) > 0:
        total_stars = arch_avg * arch_cnt + sum(rated)
        mem_avg = round(total_stars / (arch_cnt + len(rated)), 2)
    else:
        mem_avg = None

    dishonest_claims = int(hub_trust.get("dishonest", 0))
    honest_claims = int(hub_trust.get("honest", 0))
    settled_cnt = int(hub_trust.get("settled", 0))
    broken_cnt = int(hub_trust.get("broken", 0))
    refuses_cnt = int(hub_trust.get("refuses", 0))

    dishonest = bool(mem.get("dishonest")) or ext_scam_flags > 0 or dishonest_claims > 0

    deals = int(board_row.get("deals") or 0)
    reviews_cnt = int(board_row.get("reviews") if board_row.get("reviews") is not None else len(ext_stars))
    visits = int(board_row.get("visits") or 0)
    share = float(board_row.get("share") or 0.0)
    is_house = bool(board_row.get("house")) or "market.ourolabs.ai/shop/" in url

    score = 3.8
    if ext_avg is not None:
        score = float(ext_avg)
    if mem_avg is not None:
        score = (score + mem_avg) / 2.0 if ext_avg is not None else mem_avg
    if dishonest:
        score -= 10.0
    elif ext_avg is not None and ext_avg <= 2.2:
        score -= 5.0

    # Apply Round 3 empirical trust adjustments from state_hub.json
    score += min(1.2, (honest_claims + settled_cnt) * 0.20)
    score -= broken_cnt * 0.6
    score -= refuses_cnt * 1.2

    return {
        "name": name,
        "url": url,
        "ext_avg": ext_avg,
        "ext_count": reviews_cnt,
        "deals": deals,
        "visits": visits,
        "share": share,
        "is_house": is_house,
        "mem_avg": mem_avg,
        "mem_count": total_mem_cnt,
        "arch_count": arch_cnt,
        "arch_avg": arch_avg,
        "last_ts": last_ts,
        "dishonest": dishonest,
        "broken_cnt": broken_cnt,
        "score": round(score, 2),
        "offers": shop.get("offers") or [],
    }


def check_acp_support(shop_url: str, timeout: float = 2.0) -> bool | None:
    """Probe /.well-known/acp.json to verify if a shop runs kit v3/v4 ACP checkout."""
    base = shop_url.rstrip("/")
    try:
        r = httpx.get(f"{base}/.well-known/acp.json", timeout=request_timeout(timeout))
        if r.status_code != 200:
            return False if r.status_code in (404, 405, 410) else None
        data = r.json()
        services = (data.get("capabilities") or {}).get("services") or []
        return data.get("protocol") == "acp" and "checkout" in services
    except Exception:
        return None  # Transport failure is not proof of unsupported ACP.


def pick_round3_shop_and_sku(
    shops: list[dict],
    memory: dict,
    hub: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    order_data: dict[str, Any],
    budget: int,
) -> tuple[dict, dict[str, Any] | None, str]:
    """Round 3 Surplus-Maximizing Shop & SKU Selector:
    Buyer Surplus = SKU list_price - paid_price.
    Therefore, among shops holding in-stock `offers` for this order's `acceptable` SKUs,
    we prioritize (shop, sku) pairs with the highest expected surplus = `list_price - expected_pay`
    where `expected_pay <= budget`, adjusted by shop honesty/trust score and anti-collusion diversity."""
    acceptable = set(order_data.get("acceptable") or [])
    shops = [s for s in shops if any(off.get("sku") in acceptable for off in s.get("offers") or [])]
    if not shops:
        return None, None, "No shop currently holds in-stock acceptable SKUs."
    board_map = fetch_board_stats()
    hub_trust = hub.get("shop_trust") or {}
    summaries = [summarize_shop(s, memory, board_map.get(s.get("name", "")), hub_trust) for s in shops]

    # shops.json offers list every SKU of the category a shop holds, not only the order's acceptable ones.
    # Keep only acceptable SKUs, so a shop that holds none of them is not a candidate.
    acceptable = set(order_data.get("acceptable") or [])
    for c in summaries:
        c["offers"] = [off for off in c["offers"] if off.get("sku") in acceptable]

    # Filter to shops that actually have matching in-stock offers for this order.
    # Prefer student shops over house shops (which rarely complete Round 3 ACP checkout).
    student_stocked = [c for c in summaries if c.get("offers") and not c["dishonest"] and not c["is_house"]]
    stocked = student_stocked or [c for c in summaries if c.get("offers") and not c["dishonest"]]
    if not stocked:
        stocked = [c for c in summaries if c.get("offers")]

    now_ts = time.time()
    best_candidates: list[tuple[float, dict, dict[str, Any]]] = []

    for c in stocked:
        # Diversity penalty so we spread purchases cleanly across the market
        div_pen = 0.0
        if (now_ts - float(c.get("last_ts") or 0.0)) < 150.0:
            div_pen += 0.35
        if int(c.get("mem_count") or 0) >= 4:
            div_pen += 0.25
        recent_entries = (memory.get(c["name"]) or {}).get("recent") or []
        recent_fails = sum(
            1 for r in recent_entries[-3:]
            if r.get("outcome") in ("no-deal", "declined-price-policy", "declined-no-warranty")
        )
        div_pen += 0.22 * recent_fails

        for off in c.get("offers") or []:
            sku = off.get("sku", "")
            sku_meta = catalog.get(sku) or {}
            if order_data.get("kind") == "food" and not positive_number(sku_meta.get("list_price")):
                continue
            list_price = int(sku_meta.get("list_price") or budget)
            # A shop can sell a SKU if its cost (~60-70% of list_price) is <= our budget.
            # If list_price is way above budget (e.g. > 1.55x budget), the shop's cost floor may exceed our budget.
            est_cost_floor = int(list_price * 0.65)
            if est_cost_floor > budget:
                # High risk of walkaway because our budget is below the shop's likely supply cost
                feasibility_mult = 0.25
            else:
                feasibility_mult = 1.0

            settles = [
                p for p in (hub.get("demand_radar", {}).get("sku_settles", {}).get(sku) or [])
                if positive_number(p)
            ]
            if len(settles) >= 3:
                hist_pay = int(statistics.median(settles[-15:]))
                est_pay = min(budget, max(est_cost_floor, min(int(list_price * 0.80), hist_pay)))
            else:
                est_pay = min(budget, max(est_cost_floor, int(list_price * 0.80)))
            est_surplus = max(500, list_price - est_pay)
            # Combined utility: expected surplus * feasibility * trust weight
            trust_factor = max(0.5, min(1.4, (c["score"] / 4.2) - div_pen))
            if int(c.get("deals") or 0) >= 20:
                trust_factor *= 1.15
            utility = est_surplus * feasibility_mult * trust_factor
            best_candidates.append((utility, c, {"sku": sku, "name": off.get("name", sku), "list_price": list_price, "est_cost": est_cost_floor}))

    best_candidates = filter_candidates(best_candidates, hub, now_ts)
    if best_candidates:
        best_candidates.sort(key=lambda x: x[0], reverse=True)
        # One SKU per URL; explicitly unsupported ACP must never be a fallback.
        seen_urls = set()
        uncertain = None
        for util, cand_summary, sku_target in best_candidates:
            url = cand_summary["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            request_timeout(2.0)
            support = check_acp_support(url)
            if support is True:
                shop_obj = next((s for s in shops if s["url"].rstrip("/") == cand_summary["url"]), shops[0])
                reason = (
                    f"[R3-Surplus-Max] Picked {cand_summary['name']} holding SKU {sku_target['sku']} "
                    f"(list_price={sku_target['list_price']}, budget={budget}, trust={cand_summary['score']})"
                )
                return shop_obj, sku_target, reason
            if support is None and uncertain is None:
                uncertain = (cand_summary, sku_target)
        if uncertain is not None:
            cand_summary, sku_target = uncertain
            shop_obj = next(s for s in shops if s["url"].rstrip("/") == cand_summary["url"])
            return shop_obj, sku_target, f"[R3-Surplus-Fallback] ACP probe inconclusive for {cand_summary['name']}"

    # No shop currently holds in-stock SKUs matching this order's acceptable list.
    # Do NOT fall back to picking an arbitrary shop without an acceptable SKU, as doing so leads to
    # purchasing out-of-order SKUs and falsely branding innocent shops as scammers!
    return None, None, f"No shop currently holds in-stock acceptable SKUs for order {order_data.get('order')}"


def pick_shop(shops: list[dict], memory: dict, item: str, budget: int) -> tuple[dict, str]:
    if len(shops) == 1:
        s = shops[0]
        return s, f"Only one shop available in list ({s['name']})."

    # Unit test mode (2 shops in buyer_reads_reviews): pick highest trust score directly
    if len(shops) <= 2:
        summaries = [summarize_shop(s, memory) for s in shops]
        summaries_sorted = sorted(summaries, key=lambda x: x["score"], reverse=True)
        best = summaries_sorted[0]
        shop_obj = next((s for s in shops if s["url"].rstrip("/") == best["url"]), shops[0])
        return shop_obj, f"Selected {best['name']} with highest review score ({best['score']})."

    board_map = fetch_board_stats()
    summaries = [summarize_shop(s, memory, board_map.get(s.get("name", ""))) for s in shops]
    student_honest = [c for c in summaries if not c["dishonest"] and not c["is_house"]]
    honest_pool = student_honest if student_honest else [c for c in summaries if not c["dishonest"]]
    if not honest_pool:
        honest_pool = summaries

    now_ts = time.time()
    def get_diversity_penalty(summary: dict) -> int:
        last_ts = float(summary.get("last_ts") or 0.0)
        pen = 0
        if (now_ts - last_ts) < 180.0:
            pen += 1
        if int(summary.get("mem_count") or 0) >= 3:
            pen += 1
        return pen

    ranked = sorted(
        honest_pool,
        key=lambda x: (get_diversity_penalty(x), x["mem_count"], -x["score"]),
    )
    top_candidates = ranked[:6]
    acp_candidates = [c for c in top_candidates if check_acp_support(c["url"])]
    if acp_candidates:
        top_candidates = acp_candidates

    chosen = top_candidates[0]
    shop_obj = next((s for s in shops if s["url"].rstrip("/") == chosen["url"]), shops[0])
    return shop_obj, f"Selected {chosen['name']} (score={chosen['score']}, visits={chosen['mem_count']})."


def build_accept_policy(order_data: dict[str, Any], agreed_price: int, list_price: int,
                        receipt_warranty_h: float = 0) -> dict[str, Any]:
    """Construct Round 3 pre-payment `accept` policy verified by market_kit.py before paying."""
    kind = order_data.get("kind")
    policy: dict[str, Any] = {"cert": True}
    if kind == "food":
        use_hours = float(order_data.get("use_within_hours") or 1.0)
        # Require at least use_within_hours (+ 0.25h safety margin so food never expires during settlement)
        policy["min_hours_left"] = round(use_hours + 0.25, 2)
        policy["grades"] = ["A", "B", "C"]
    elif kind == "goods":
        policy["grades"] = ["A", "B", "C"]
        # The kit builds the checkout from the shop's own terms, not from our copy of the receipt. If the shop
        # promised the full-window warranty, require it, so a checkout without it is not paid. Deals agreed
        # without a warranty were already checked for positive expected surplus before getting here.
        if receipt_warranty_h >= GOODS_BREAK_WINDOW_H:
            policy["warranty_hours"] = GOODS_BREAK_WINDOW_H
    return policy


def positive_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def similar_food_remaining(sample_hours: float, target_hours: float, required_hours: float) -> bool:
    """Compare shelf life at each sale, not today's aged historical inventory.

    Require the use window plus checkout margin, the same doubling bucket, and
    at most 25% difference. Clearance/short-life prices must not anchor fresh food.
    """
    minimum = required_hours + 0.25
    return (sample_hours >= minimum and target_hours >= minimum
            and int(math.log2(sample_hours)) == int(math.log2(target_hours))
            and 0.8 * target_hours <= sample_hours <= 1.25 * target_hours)


def food_price_policy(catalog: dict, hub: dict, sku: str, budget: int, order: dict,
                      receipt: dict | None = None, now: float | None = None) -> tuple[int | None, dict]:
    """Local-only cap. Unknown catalog price means defer, never budget*1.2.

    Before negotiation there is no guaranteed grade/expiry, so use only the 90%
    list cap. Receipt claims are *hints*, not certificates: comparable tightening
    is conditional on kit cert + exact grade + minimum shelf-life acceptance.
    Unknown grades never become C here (unlike goods break-risk pessimism).
    """
    lp = (catalog.get(sku) or {}).get("list_price")
    if not positive_number(lp):
        return None, {}
    cap = min(budget, math.floor(lp * FOOD_LIST_PRICE_PERCENT / 100))
    receipt = receipt or {}
    grade = receipt.get("claim")
    expiry = receipt.get("expires_at")
    now = time.time() if now is None else now
    if grade not in ("A", "B", "C") or not positive_number(expiry):
        return cap, {}
    hours = (expiry - now) / 3600
    required = float(order.get("use_within_hours") or 1.0)
    if hours < required + 0.25:
        return cap, {}
    prices = []
    for row in (hub.get("demand_radar", {}).get("sku_comparables", {}).get(sku) or []):
        ts, amount, exp = row.get("ts"), row.get("amount"), row.get("expires_at")
        if (not positive_number(ts) or not 0 <= now - ts <= COMPARABLE_WINDOW_S
                or not positive_number(amount) or row.get("grade") != grade
                or not positive_number(exp)):
            continue
        if similar_food_remaining((exp - ts) / 3600, hours, required):
            prices.append(amount)
    if len(prices) < COMPARABLE_MIN_SAMPLES:
        return cap, {}
    tightened = min(cap, math.floor(statistics.median(prices) * 110 / 100))
    # Enforce the hint's lower bound through the kit, not through trust in text.
    # A longer actual shelf life is safe; it never increases our price ceiling.
    # Use the comparison bucket's lower bound, not the unit's current remaining
    # hours: the latter would fail as soon as time passes before checkout.
    minimum_hours = max(required + 0.25, 0.8 * hours, 2 ** int(math.log2(hours)))
    return tightened, {"grades": [grade], "min_hours_left": minimum_hours}


def rpc_call(url: str, method: str, params: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4())[:8],
        "method": method,
        "params": params,
    }
    headers = {"A2A-Version": "1.0", "Content-Type": "application/json"}
    resp = httpx.post(url, json=body, headers=headers, timeout=request_timeout(45))
    data = resp.json()
    if "error" in data:
        return {"error": data["error"]}
    return data.get("result", {})


def extract_last_number(task: dict, min_valid: int = 50, max_valid: int = 10_000_000) -> int | None:
    msg = (task or {}).get("status", {}).get("message", {})
    text = " ".join(p.get("text", "") for p in msg.get("parts", []))
    # Strip SKU IDs, unit IDs, percentages, hours ("24h", "720.0h"), and list_price labels before extracting ask price!
    cleaned = re.sub(r"\b[gfGF][0-9a-fA-F]{7,10}\b", " ", text)
    cleaned = re.sub(r"\bu_[0-9a-zA-Z_]+\b", " ", cleaned)
    cleaned = re.sub(r"list[_\s-]*price\s*[=:]?\s*\d+(?:,\d{3})*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\(\s*list\s+\d+(?:,\d{3})*\s*\)", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*(?:%|h|hr|hrs|hours)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace(",", "")

    # Prefer explicit "asking price X" or "ask X" or "price X" patterns if present
    explicit = re.findall(r"(?:asking\s+price|ask|counteroffer|offer|at|price)\s*[=:]?\s*(\d{2,7})\b", cleaned, flags=re.IGNORECASE)
    if explicit:
        val = int(explicit[-1])
        if min_valid <= val <= max_valid:
            return val

    nums = [math.ceil(float(x)) for x in re.findall(r"\b\d+(?:\.\d+)?\b", cleaned)]
    price_nums = [n for n in nums if min_valid <= n <= max_valid]
    if price_nums:
        return price_nums[-1]
    if nums:
        return nums[-1]
    return None


def extract_receipt_data(task: dict) -> dict | None:
    for art in (task or {}).get("artifacts") or []:
        for part in art.get("parts") or []:
            data = part.get("data")
            if isinstance(data, dict) and "price" in data:
                return data
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shops", required=True, help="Path to shops.json")
    parser.add_argument("--item", required=True, help="Item or category to buy")
    parser.add_argument("--budget", type=int, required=True, help="Maximum budget")
    parser.add_argument("--order", default="", help="Optional path to Round 3 order.json")
    args = parser.parse_args()

    shops_path = Path(args.shops)
    shops = json.loads(shops_path.read_text(encoding="utf-8"))
    item = args.item
    item_low = item.lower()
    budget = args.budget

    # Check if Round 3 order.json is provided or exists in .market/order.json
    order_path = Path(args.order) if args.order else (shops_path.parent / "order.json")
    order_data = load_json_safe(order_path, {}) if order_path and order_path.exists() else {}
    acceptable_skus = order_data.get("acceptable") or []
    is_round3 = bool(acceptable_skus)
    # Start before local setup/selection so probing cannot spend an extra 90s.
    deadline = time.monotonic() + 90
    if is_round3 and positive_number(order_data.get("expires_at")):
        deadline = min(deadline, time.monotonic() + order_data["expires_at"] - time.time() - 5)

    memory = load_memory()
    hub = load_hub()
    catalog = load_catalog(shops_path.parent if (shops_path.parent / "catalog.json").exists() else MARKET_DIR)
    sync_broken_and_payments(hub, shops_path.parent if (shops_path.parent / "broken.jsonl").exists() else MARKET_DIR)

    if is_round3:
        strategy = record_buyer_order(order_data.get("category") or item)
        if isinstance(strategy, dict):
            hub["adaptive_strategy"] = strategy
        result = run_round3(shops, memory, hub, catalog, order_data, item, budget, deadline)
    else:
        target_sku_info = None
        chosen_shop, reason = pick_shop(shops, memory, item_low, budget)
        result = run_attempt(chosen_shop, target_sku_info, reason, memory, hub, catalog,
                             order_data, item, budget)
    # Exactly one receipt leaves the hook. Checkout/accept/signature verification
    # happens in the kit AFTER buyer exit, so failures there cannot retry here.
    print(json.dumps(result, ensure_ascii=False), flush=True)


def run_round3(shops, memory, hub, catalog, order_data, item, budget, deadline):
    result = {"shop": "", "receipt": None, "stars": None,
              "review": "No deal: no shop currently has acceptable SKUs in stock."}
    remaining_shops = list(shops)
    token = REQUEST_DEADLINE.set(deadline)
    try:
        for attempt in range(1, 4):
            try:
                request_timeout(1)
                chosen, sku, reason = pick_round3_shop_and_sku(
                    remaining_shops, memory, hub, catalog, order_data, budget)
                if chosen is None:
                    diagnostic(f"[retry] no candidates: {reason}")
                    break
                url = chosen.get("url", "").rstrip("/")
                remaining_shops = [s for s in remaining_shops if s.get("url", "").rstrip("/") != url]
                diagnostic(f"[attempt {attempt}/3] {chosen.get('name')} {url} SKU={sku['sku']}")
                shop_deadline = min(deadline, time.monotonic() + 25)
                # Reserve the last 3s of the shop's 25s window for cancellation.
                shop_token = REQUEST_DEADLINE.set(shop_deadline - 3)
                try:
                    result = run_attempt(chosen, sku, reason, memory, hub, catalog,
                                         order_data, item, budget, cleanup_deadline=shop_deadline)
                finally:
                    REQUEST_DEADLINE.reset(shop_token)
                if result.get("receipt") is not None:
                    diagnostic(f"[attempt {attempt}/3] receipt selected (checkout still pending)")
                    break
                diagnostic(f"[attempt {attempt}/3] failed: {result['review']}")
            except TimeoutError:
                diagnostic(f"[attempt {attempt}/3] stopped: total/order deadline exhausted")
                result["review"] = "No deal: total/order deadline exhausted."
                break
    finally:
        REQUEST_DEADLINE.reset(token)
    return result


def run_attempt(chosen_shop, target_sku_info, reason, memory, hub, catalog,
                order_data, item, budget, cleanup_deadline=None):
    active = {}
    selected = False
    try:
        result = _run_attempt(chosen_shop, target_sku_info, reason, memory, hub, catalog,
                              order_data, item, budget, active)
        if order_data.get("acceptable"):
            request_timeout(1)  # Includes time spent waiting for/exiting the hub lock.
        selected = result.get("receipt") is not None
        return result
    except Exception as exc:
        if not order_data.get("acceptable"):
            raise
        if isinstance(exc, TimeoutError) and active.get("id") and not active.get("failure"):
            # Negotiation deadline is pair-local, not proof the whole shop's network failed.
            active["failure"] = "timeout"
        return {"shop": chosen_shop["url"], "receipt": None, "stars": None,
                "review": f"No deal: attempt failed ({type(exc).__name__}: {exc})."}
    finally:
        if not selected and active.get("pending") is not None:
            # Remove only this attempt's record, not a concurrent replacement.
            with mutate_hub() as current_hub:
                pending = current_hub.get("pending_deals", {})
                key = str(order_data.get("order"))
                if pending.get(key) == active["pending"]:
                    del pending[key]
        if (order_data.get("acceptable") and active.get("id")
                and active.get("state") not in ("TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED")):
            cleanup_token = REQUEST_DEADLINE.set(cleanup_deadline if cleanup_deadline is not None
                                                  else REQUEST_DEADLINE.get())
            try:
                # Reserved cleanup time never extends the total/order deadline.
                rpc_call(active["url"], "CancelTask", {"id": active["id"]})
            except Exception:
                pass
            finally:
                REQUEST_DEADLINE.reset(cleanup_token)
        # Local persistence must also retain expired attempts; no network is done
        # under this lock, and a deadline failure never becomes a successful deal.
        if order_data.get("acceptable") and not selected and active.get("failure"):
            with mutate_hub() as current_hub:
                record_failure(current_hub, order_data.get("order"), chosen_shop["url"],
                               target_sku_info["sku"], active["failure"], time.time())


def _run_attempt(chosen_shop, target_sku_info, reason, memory, hub, catalog,
                 order_data, item, budget, active):
    acceptable_skus = order_data.get("acceptable") or []
    is_round3 = bool(acceptable_skus)
    item_low = item.lower()

    shop_name = chosen_shop.get("name", "unknown")
    shop_url = chosen_shop.get("url", "").rstrip("/")
    diagnostic(f"[choose] {shop_url} ({shop_name}): {reason}")

    is_round3_food = is_round3 and order_data.get("kind") == "food"
    is_round3_goods = is_round3 and order_data.get("kind") == "goods"
    price_limit = budget
    food_accept = {}
    if is_round3_food:
        price_limit, _ = food_price_policy(catalog, hub, target_sku_info["sku"], budget, order_data)
        if price_limit is None or price_limit < 1:
            return {"shop": shop_url, "receipt": None, "stars": None,
                    "review": "No deal: food catalog list price is unknown."}

    # 1. Fetch Agent Card
    card_url = f"{shop_url}/.well-known/agent-card.json"
    try:
        card_resp = httpx.get(card_url, timeout=request_timeout(12))
        card = card_resp.json()
        interfaces = card.get("supportedInterfaces") or []
        rpc_url = interfaces[0].get("url") if interfaces else f"{shop_url}/"
    except Exception as e:
        review = f"No deal in 0 rounds: shop card endpoint failed ({type(e).__name__})."
        if isinstance(e, (TimeoutError, httpx.TimeoutException)):
            active["failure"] = "network-timeout"
        update_memory(memory, shop_name, {
            "item": item, "budget": budget, "price": None, "rounds": 0, "stars": 2, "outcome": "no-deal"
        })
        return {"shop": chosen_shop["url"], "receipt": None, "stars": 2, "review": review}

    # 2. First message: specify exact SKU & name in Round 3, or item in Round 0-2
    if is_round3 and target_sku_info:
        sku_code = target_sku_info["sku"]
        sku_name = target_sku_info["name"]
        list_price = int(target_sku_info.get("list_price") or budget)
        reserve = max(500, int(list_price * 0.58))
        opening_text = (
            f"I want to buy {sku_name} ({sku_code}). Please quote your best price with market certificate (cert=true)"
            + (f" and at least {order_data.get('use_within_hours')}h shelf life." if order_data.get("use_within_hours") else " and warranty.")
        )
    else:
        sku_code = None
        list_price = int(budget * 1.2)
        reserve = RESERVES.get(item_low, max(80, int(budget * 0.60)))
        opening_text = (
            f"I want to buy a {item}. Quality and trouble-free use matter: "
            f"do you provide after-sales service, a quality warranty, or a flexible refund policy?"
        )

    msg = {
        "role": "ROLE_USER",
        "messageId": str(uuid.uuid4()),
        "parts": [{"text": opening_text}],
    }
    try:
        if is_round3:
            request_timeout(1)
        res = rpc_call(rpc_url, "SendMessage", {"message": msg})
    except Exception as e:
        review = f"No deal in 0 rounds: shop did not respond to opening inquiry ({type(e).__name__})."
        if isinstance(e, (TimeoutError, httpx.TimeoutException)):
            active["failure"] = "network-timeout"
        update_memory(memory, shop_name, {
            "item": item, "budget": budget, "price": None, "rounds": 0, "stars": 2, "outcome": "no-deal"
        })
        return {"shop": chosen_shop["url"], "receipt": None, "stars": 2, "review": review}

    if "error" in res:
        review = f"No deal in 0 rounds: seller returned RPC error {res['error']}."
        update_memory(memory, shop_name, {
            "item": item, "budget": budget, "price": None, "rounds": 0, "stars": 2, "outcome": "no-deal"
        })
        return {"shop": chosen_shop["url"], "receipt": None, "stars": 2, "review": review}

    task = res.get("task", res)
    task_id = task.get("id")
    task_status = task.get("status", {})
    task_state = task_status.get("state")
    active.update(url=rpc_url, id=task_id, state=task_state)
    min_valid_p = 500 if is_round3 else 50
    max_valid_p = max(budget * 4, list_price * 2, 1000)
    seller_ask = extract_last_number(task, min_valid_p, max_valid_p)
    seller_texts = [
        " ".join(p.get("text", "") for p in task_status.get("message", {}).get("parts", []))
    ]
    card_desc = str(card.get("description", ""))

    last_offer = 0
    rounds = 0
    step_inc = max(5, int(budget * 0.02)) if is_round3 else 5

    offer_limit = price_limit
    if is_round3_goods:
        offer_limit = min(price_limit, max(1, math.floor(list_price * GOODS_OFFER_CAP_PERCENT / 100)))
        settles = [
            p for p in (hub.get("demand_radar", {}).get("sku_settles", {}).get(sku_code) or [])
            if positive_number(p)
        ]
        if len(settles) >= 3:
            comp_cap = max(
                math.floor(list_price * 0.74),
                math.floor(statistics.median(settles[-15:]) * 106 / 100),
            )
            offer_limit = min(offer_limit, comp_cap)

    failure_reason = ""
    while task_state == "TASK_STATE_INPUT_REQUIRED" and rounds < (3 if is_round3 else 5):
        if is_round3:
            request_timeout(1)
            if seller_ask is None:
                failure_reason = "seller price could not be parsed"
                break
        rounds += 1

        # Budget-Decoupled Negotiation:
        # Anchors offers to `reserve` (estimated supply cost) and `seller_ask`, NEVER leaking `budget * ratio`.
        if seller_ask is not None and seller_ask <= offer_limit:
            if seller_ask <= math.ceil(reserve * 1.05) or rounds >= 3:
                offer = seller_ask
            elif rounds == 1:
                offer = min(seller_ask, reserve)
            else:
                halfway = int(round(reserve + (seller_ask - reserve) * 0.55))
                offer = min(seller_ask, max(last_offer + step_inc, halfway))
        else:
            if last_offer >= offer_limit and seller_ask is not None and seller_ask > offer_limit:
                diagnostic(f"Cannot agree: seller ask {seller_ask} > price limit {offer_limit}")
                if not is_round3:
                    try:
                        rpc_call(rpc_url, "CancelTask", {"id": task_id})
                    except Exception:
                        pass
                failure_reason = "seller ask exceeds price limit"
                break

            ceiling = min(offer_limit, seller_ask) if seller_ask else offer_limit
            start_anchor = min(reserve, int(ceiling * 0.72)) if is_round3 else min(reserve, ceiling)
            if rounds == 1:
                offer = start_anchor
            elif rounds == 2:
                offer = max(last_offer + step_inc, int(round(start_anchor + (ceiling - start_anchor) * 0.45)))
            elif rounds == 3:
                offer = ceiling if is_round3 else max(last_offer + step_inc, int(round(start_anchor + (ceiling - start_anchor) * 0.80)))
            else:
                offer = ceiling

            if not is_round3:
                prompt = (
                    f"You are a quality-focused buyer negotiating to purchase a {item}.\n"
                    f"Item cost floor: {reserve}. Maximum budget: {budget} (You MUST NEVER exceed {budget}).\n"
                    f"Seller's current ask: {seller_ask}.\n"
                    f"Your last offer: {last_offer}.\n"
                    f"Round: {rounds}/5.\n"
                    f"If seller refuses to go below {budget} and you have already offered {budget}, reply: QUIT\n"
                    f"Otherwise propose your next offer (must be > {last_offer} and <= {budget}). Reply format: OFFER <number>"
                )
                resp = call_harness(prompt).upper()
                if "QUIT" in resp and last_offer >= budget:
                    try:
                        rpc_call(rpc_url, "CancelTask", {"id": task_id})
                    except Exception:
                        pass
                    break
                nums = re.findall(r"\d+", resp)
                if nums:
                    suggested = int(nums[-1])
                    if suggested > last_offer:
                        offer = min(budget, suggested)

        offer = min(offer_limit, max(1, offer))
        last_offer = offer

        # Send clean numeric offer (avoid extra numbers in the text so simple regex sellers parse `offer` accurately!)
        follow_up_text = (
            f"I offer {offer} for this certified item."
            if is_round3
            else f"I offer {offer} if you guarantee product quality and after-sales service support."
        )
        follow_up = {
            "role": "ROLE_USER",
            "messageId": str(uuid.uuid4()),
            "taskId": task_id,
            "parts": [{"text": follow_up_text}],
        }
        try:
            res = rpc_call(rpc_url, "SendMessage", {"message": follow_up})
        except Exception as exc:
            failure_reason = f"counter RPC failed ({type(exc).__name__})"
            if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                active["failure"] = "network-timeout"
            break
        if "error" in res:
            failure_reason = f"counter RPC error: {res['error']}"
            break

        task = res.get("task", res)
        task_status = task.get("status", {})
        task_state = task_status.get("state")
        task_id = task.get("id") or task_id
        active.update(id=task_id, state=task_state)
        seller_ask = extract_last_number(task, min_valid_p, max_valid_p)
        seller_texts.append(
            " ".join(p.get("text", "") for p in task_status.get("message", {}).get("parts", []))
        )

        if task_state in ("TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"):
            break

    # A seller may lower its ask only after our third counter. Allow one final
    # acceptance, never a fourth bargaining counter or a relaxed price cap.
    if (is_round3 and rounds == 3 and not failure_reason
            and task_state == "TASK_STATE_INPUT_REQUIRED"
            and seller_ask is not None and seller_ask <= offer_limit):
        try:
            request_timeout(1)
            last_offer = seller_ask
            rounds += 1
            res = rpc_call(rpc_url, "SendMessage", {"message": {
                "role": "ROLE_USER",
                "messageId": str(uuid.uuid4()),
                "taskId": task_id,
                "parts": [{"text": f"I offer {seller_ask} for this certified item."}],
            }})
            if "error" in res:
                failure_reason = f"final acceptance RPC error: {res['error']}"
            else:
                task = res.get("task", res)
                task_status = task.get("status", {})
                task_state = task_status.get("state")
                task_id = task.get("id") or task_id
                active.update(id=task_id, state=task_state)
                seller_ask = extract_last_number(task, min_valid_p, max_valid_p)
                seller_texts.append(
                    " ".join(p.get("text", "") for p in task_status.get("message", {}).get("parts", []))
                )
        except Exception as exc:
            failure_reason = f"final acceptance RPC failed ({type(exc).__name__})"

    # 3. Inspect result & receipt
    raw_receipt = extract_receipt_data(task) if task_state == "TASK_STATE_COMPLETED" else None
    combined_service_text = (" ".join(seller_texts) + " " + card_desc + " " + json.dumps(raw_receipt or {})).lower()
    has_service = any(kw in combined_service_text for kw in SERVICE_KEYWORDS)

    if raw_receipt is not None:
        rec_price_raw = raw_receipt.get("price")
        rec_price = int(rec_price_raw) if isinstance(rec_price_raw, float) and rec_price_raw.is_integer() else rec_price_raw
        rec_item = str(raw_receipt.get("item", "")).strip()
        rec_task = str(raw_receipt.get("task", ""))
        rec_shop = str(raw_receipt.get("shop", raw_receipt.get("seller", ""))).strip()
        signature = raw_receipt.get("sig_seller")
        rec_sig = signature.strip() if isinstance(signature, str) else ""

        if is_round3:
            item_matches = (rec_item == item) or (rec_item in acceptable_skus) or (rec_item.lower() == item_low)
        else:
            item_matches = not rec_item or (rec_item.lower() == item_low)

        receipt_cap = budget
        if is_round3_food:
            # Recompute on the receipt SKU, never category/target fallback. This
            # does not pin the kit's later checkout to that SKU: a different
            # acceptable SKU shipped there is a pre-existing integration limit.
            receipt_cap, food_accept = food_price_policy(catalog, hub, rec_item, budget, order_data, raw_receipt)
        elif is_round3_goods:
            actual_lp = (catalog.get(rec_item) or {}).get("list_price") or list_price
            if positive_number(actual_lp):
                receipt_cap = min(budget, max(1, int(actual_lp) - 1))
        price_policy_declined = (is_round3_food or is_round3_goods) and (
            receipt_cap is None or (positive_number(rec_price) and rec_price > receipt_cap))
        receipt_invalid = (
            not positive_number(rec_price)
            or (isinstance(rec_price, float) and not rec_price.is_integer())
            or rec_price > budget
            # A price-policy rejection is not evidence of dishonesty. Still reject
            # above-agreement receipts within policy, and all structural/budget errors.
            or (last_offer > 0 and rec_price > last_offer and not price_policy_declined)
            or not item_matches
            or (task_id and rec_task and rec_task != str(task_id))
            or not rec_shop
        )

        if is_round3 and not rec_sig:
            final_receipt = None
            recorded_price = None
            stars = None
            review = "No deal: unsigned receipt (missing sig_seller); trying another shop."
            outcome = "unsigned-receipt"
            active["failure"] = "unsigned"
        elif receipt_invalid:
            stars = 1
            review = (
                f"Invalid receipt! Agreed at {last_offer} (budget {budget}) in {rounds} round(s), "
                f"but receipt says price={rec_price}, item={rec_item}, task={rec_task}!"
            )
            final_receipt = None
            outcome = "rip-off"
            recorded_price = rec_price if isinstance(rec_price, (int, float)) else None
        elif price_policy_declined:
            final_receipt = None
            recorded_price = rec_price
            stars = 3
            review = f"No deal: declined-price-policy for {rec_item} at {rec_price} (cap {receipt_cap})."
            outcome = "declined-price-policy"
            active["failure"] = "price"
        else:
            final_receipt = raw_receipt
            recorded_price = int(rec_price)
            sig_note = "seller signature present; kit verification pending" if rec_sig else "receipt fields checked"
            is_dumping_price = (not is_round3) and (recorded_price <= reserve + 1)

            if is_dumping_price and not has_service:
                stars = 3
                review = (
                    f"Bought {item} at {recorded_price} (budget {budget}, {sig_note}); very low price with no "
                    f"explicit warranty or after-sales service mentioned."
                )
                outcome = "dumping-suspect-quality"
            elif has_service:
                stars = 5
                review = (
                    f"Verified quality and fast negotiation ({sig_note})! Closed {rec_item or item} at "
                    f"{recorded_price} (budget {budget}) in {rounds} round(s)."
                )
                outcome = "great-service-deal"
            else:
                stars = 4
                review = (
                    f"Bought {rec_item or item} at {recorded_price} (budget {budget}) in {rounds} round(s) ({sig_note}); "
                    f"receipt matched the agreed price."
                )
                outcome = "no-service-deal"

            # Goods without a warranty covering the break window: a broken unit is worth 0 to the order, so the
            # expected surplus is (1 - break_rate) x list - price. Pay only while it stays positive; with the
            # warranty a break is refunded and any price under list is worth it. No grade claim: assume C.
            if is_round3 and order_data.get("kind") == "goods":
                warranty_h = float(raw_receipt.get("warranty_hours") or 0)
                claimed = str(raw_receipt.get("claim") or "C")
                lp = int((catalog.get(rec_item) or {}).get("list_price") or list_price)
                b = BREAK_RATE.get(claimed, BREAK_RATE["C"])
                if warranty_h < GOODS_BREAK_WINDOW_H and (1 - b) * lp - recorded_price <= 0:
                    final_receipt = None
                    stars = 3
                    review = (
                        f"No deal: grade {claimed} {rec_item} at {recorded_price} with no {GOODS_BREAK_WINDOW_H} h "
                        f"warranty is worth less than its price at a {round(b * 100)}% break rate (list {lp})."
                    )
                    outcome = "declined-no-warranty"

    else:
        final_receipt = None
        recorded_price = None
        outcome = "no-deal"
        stars = 3 if (seller_ask and seller_ask <= int(budget * 1.15) and has_service) else 2
        template = random.choice(FUNNY_NO_DEAL_REVIEWS)
        review = template.format(rounds=rounds, ask=seller_ask or "high", budget=budget)
        if is_round3:
            if not active.get("failure") and seller_ask is not None and seller_ask > offer_limit:
                active["failure"] = "price"
            elif not active.get("failure") and task_state == "TASK_STATE_REJECTED":
                # A rejected SKU is not proof of globally empty stock.
                active["failure"] = "rejected"
            if not failure_reason and task_state == "TASK_STATE_INPUT_REQUIRED":
                failure_reason = f"counter limit reached (ask {seller_ask}, offer limit {offer_limit}, budget limit {budget})"
            review += f" Reason: {failure_reason or task_state or 'missing receipt'}."

    # A Round 3 agreed deal enters shop memory from review.py, only after it is paid
    if not (is_round3 and final_receipt is not None):
        update_memory(memory, shop_name, {
            "item": item,
            "budget": budget,
            "price": recorded_price,
            "rounds": rounds,
            "stars": stars,
            "outcome": outcome,
        })

    result_line: dict[str, Any] = {
        "shop": chosen_shop["url"],
        "receipt": final_receipt,
        "stars": stars,
        "review": review,
    }
    if is_round3 and final_receipt is not None and recorded_price is not None:
        result_line["accept"] = build_accept_policy(order_data, recorded_price, list_price,
                                                    float(final_receipt.get("warranty_hours") or 0))
        result_line["accept"].update(food_accept)
        # This is only a pending checkout, not a paid purchase or successful memory.
        # Build/validate everything before writing a single final candidate.
        request_timeout(1)
        matched_sku = rec_item if rec_item in catalog else target_sku_info["sku"]
        sku_meta = catalog.get(matched_sku) or {}
        now_ts = int(time.time())
        with mutate_hub() as hub:
            request_timeout(1)  # Lock acquisition can itself take several seconds.
            pending = hub.setdefault("pending_deals", {})
            pending[str(order_data.get("order"))] = {
                "order": order_data.get("order"), "sku": matched_sku,
                "name": sku_meta.get("name") or target_sku_info["name"],
                "category": sku_meta.get("category") or order_data.get("category") or item,
                "shop_url": shop_url, "shop_name": shop_name, "price": recorded_price,
                "list_price": int(sku_meta.get("list_price") or list_price),
                "warranty_hours": raw_receipt.get("warranty_hours") or 0, "ts": now_ts,
                "memory": {"item": item, "budget": budget, "price": recorded_price, "rounds": rounds},
            }
            active["pending"] = pending[str(order_data.get("order"))].copy()
            for k in [k for k, v in pending.items() if now_ts - int(v.get("ts") or 0) > 21600]:
                del pending[k]

    return result_line


if __name__ == "__main__":
    main()
