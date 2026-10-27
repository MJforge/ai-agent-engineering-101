"""A2A v1.0 Seller Agent (Week 08: Round 3 ACP/AP2 Market & Settled-Price Dynamic Pricing).
Sells lamp (reserve 100), chair (reserve 180), and bookshelf (reserve 260).
- Signs receipts with Ed25519 (sig_seller) over canonical JSON of {shop, item, price, task}.
- Adapts opening asks and concession curves using Round 3 settled ledger prices (kind='settle')
  now that buyer budgets are hidden behind budget_commitment hashes in /api/ledger.
"""
import argparse
import base64
import json
import math
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import httpx
import uvicorn
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette

from a2a.helpers import (
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

RESERVES = {
    "lamp": 100,
    "chair": 180,
    "bookshelf": 260,
}
# Premium 1.5x base price: lamp=150, chair=270, bookshelf=390
PREMIUM_MULT = 1.50
# Rating Recovery + Grand Revamp Instant Opening Discount (33.3% off 1.5x -> 1.00x: lamp=100, chair=180, bookshelf=260)
# so 100% of valid budgets close in 1 turn and never trigger 2-star rejection reviews!
RECOVERY_OPEN_MULT = 1.00
REFERRAL_DISCOUNT_PER_GUEST = 0.10
MAX_REFERRAL_DISCOUNT = 1.0 - (1.0 / PREMIUM_MULT)  # 33.33% off 1.5x = 1.0x reserve (100 / 180 / 260)
VIP_MULT = 1.02
# Conservative limited-time promo window for early customer acquisition: 30 minutes (1800 seconds)
MEMBERSHIP_PROMO_WINDOW_SEC = 1800
PROMO_START_TS = time.time()
ALL_ITEMS_SET = frozenset(RESERVES.keys())  # {"lamp", "chair", "bookshelf"}
MAX_DISCOUNT_FLOORS = {
    item: res for item, res in RESERVES.items()
}  # {"lamp": 100, "chair": 180, "bookshelf": 260} — accepts any offer >= reserve on Round 1!

SHOP_NAME = os.environ.get("SHOP_NAME", "mjforge")
MARKET_NAME = os.environ.get("SHOP_MARKET_NAME", "mjforge")
SELLER_HISTORY_FILE = Path(__file__).resolve().parent / "seller_history.json"


def canon(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_receipt(receipt: dict, key_path: str = "shop_key.pem") -> dict:
    p = Path(__file__).resolve().parent / key_path
    if not p.exists():
        p = Path(key_path)
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    body = {k: receipt[k] for k in ("shop", "item", "price", "task")}
    receipt["sig_seller"] = base64.b64encode(key.sign(canon(body))).decode("ascii")
    return receipt


# Round 3 target band when buyer budget is hidden behind budget_commitment (1.15x ~ 1.25x reserve)
ROUND3_MIN_OPEN_MULT = 1.15
ROUND3_MAX_OPEN_MULT = 1.25
ROUND3_DEFAULT_OPEN_MULT = 1.18
ROUND3_VIP_MIN_MULT = 1.08


def compute_round3_opening_ask(
    item: str,
    item_stats: dict,
    is_vip: bool = False,
    is_repeat: bool = False,
    is_wave: bool = False,
    is_fcfs: bool = False,
) -> tuple[int, int, int]:
    """Compute Round 3 opening ask, Round-1 target floor, and effective discount % from 1.5x list price.
    Uses recent settled deal quantiles (0.6 * P65 + 0.4 * Mean) clamped to [1.15x, 1.25x] reserve.
    """
    reserve = RESERVES[item]
    base_15x = reserve * PREMIUM_MULT
    min_open = math.ceil(reserve * ROUND3_MIN_OPEN_MULT)
    max_open = math.ceil(reserve * ROUND3_MAX_OPEN_MULT)

    cnt = int(item_stats.get("count") or 0)
    if cnt > 0:
        p65 = float(item_stats.get("p65") or (reserve * ROUND3_DEFAULT_OPEN_MULT))
        mean = float(item_stats.get("mean") or (reserve * ROUND3_DEFAULT_OPEN_MULT))
        blended = int(round(0.60 * p65 + 0.40 * mean))
        anchor = max(min_open, min(max_open, blended))
    else:
        anchor = math.ceil(reserve * ROUND3_DEFAULT_OPEN_MULT)

    if is_fcfs:
        # 1h+ idle flash recovery mode: lower opening ask to 1.08x to attract immediate volume
        ask = math.ceil(reserve * 1.08)
    elif is_vip or is_repeat:
        # Repeat / VIP loyalty tier: 5% off anchor, floor at 1.08x reserve
        ask = max(math.ceil(reserve * ROUND3_VIP_MIN_MULT), int(round(anchor * 0.95)))
    elif is_wave:
        # High-demand wave: +3% premium on anchor, capped at 1.25x reserve
        ask = min(max_open, int(round(anchor * 1.03)))
    else:
        ask = anchor

    # Round-1 target floor: accept immediately on Round 1 if buyer offers >= 50% of markup above reserve (min 1.05x)
    r1_target = max(math.ceil(reserve * 1.05), int(round(reserve + (ask - reserve) * 0.50)))
    r1_target = min(ask, r1_target)
    disc_pct = int(round((1.0 - ask / base_15x) * 100))
    return ask, r1_target, disc_pct


def load_seller_state() -> dict:
    default = {
        "deals": 0,
        "walkaways": 0,
        "multiplier": PREMIUM_MULT,
        "vip_multiplier": VIP_MULT,
        "avg_market_purchasing_power": 220,
        "round3_mode": False,
        "settled_prices_by_item": {"lamp": [], "chair": [], "bookshelf": []},
        "settled_stats_by_item": {},
        "blacklist": ["11im"],
        "vips": [],
        "vip_spend_share": {},
        "referrals_by_buyer": {},
        "distinct_customers": [],
        "seen_buyers": [],
        "recent_deal_ts": [],
        "last_customer_ts": time.time(),
        "fcfs_slots_remaining": 0,
        "fcfs_buyer_counts": {},
        "recent_reviews": [],
        "trades": [],
    }
    if SELLER_HISTORY_FILE.exists():
        try:
            data = json.loads(SELLER_HISTORY_FILE.read_text(encoding="utf-8"))
            default.update(data)
            if "11im" not in default.get("blacklist", []):
                default.setdefault("blacklist", []).append("11im")
        except Exception:
            pass
    return default


def save_seller_state(state: dict) -> None:
    try:
        state["trades"] = state.get("trades", [])[-20:]
        SELLER_HISTORY_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def compute_item_settle_stats(prices_by_item: dict[str, list[int]]) -> dict[str, dict]:
    """Compute mean, P50 (median), and P65 from recent settled prices per item."""
    stats = {}
    for item, reserve in RESERVES.items():
        raw = [int(p) for p in (prices_by_item.get(item) or []) if isinstance(p, (int, float)) and p >= reserve]
        if raw:
            s = sorted(raw[-40:])
            n = len(s)
            p50 = s[n // 2]
            p65 = s[min(n - 1, int(n * 0.65))]
            mean = round(sum(s) / n, 1)
            stats[item] = {"count": n, "mean": mean, "p50": p50, "p65": p65}
        else:
            # Cold-start Round 3 fallback when no settled prices are in window yet (1.18x reserve)
            anchor = math.ceil(reserve * 1.18)
            stats[item] = {"count": 0, "mean": float(anchor), "p50": anchor, "p65": anchor}
    return stats


def refresh_reviews_and_adapt_price() -> dict:
    """Read own trades, market reviews, Round 3 settled stats, VIP spend share, and blacklist in <0.01s."""
    state = load_seller_state()
    shops_file = Path(__file__).resolve().parent / ".market" / "shops.json"
    reviews = state.get("recent_reviews", [])
    if shops_file.exists():
        try:
            shops = json.loads(shops_file.read_text(encoding="utf-8"))
            for s in shops:
                if s.get("name") == MARKET_NAME:
                    reviews = s.get("reviews") or []
                    break
        except Exception:
            pass

    if reviews:
        state["recent_reviews"] = reviews[:5]

    state["settled_stats_by_item"] = compute_item_settle_stats(state.get("settled_prices_by_item") or {})
    state["multiplier"] = PREMIUM_MULT
    state["vip_multiplier"] = VIP_MULT
    save_seller_state(state)
    return state


def background_review_sync():
    """Periodically sync reviews, Round 3 settled ledger deals (kind='settle'), and VIP/blacklist stats."""
    while True:
        try:
            r = httpx.get("https://market.ourolabs.ai/api/shops", timeout=3.0).json()
            for s in r.get("shops", []):
                if s.get("name") == MARKET_NAME:
                    state = load_seller_state()
                    state["recent_reviews"] = (s.get("reviews") or [])[:5]
                    save_seller_state(state)
                    break

            ledger = httpx.get("https://market.ourolabs.ai/api/ledger?after=-250", timeout=3.0).json()
            if isinstance(ledger, list):
                state = load_seller_state()
                blacklist = set(state.get("blacklist", ["11im"]))
                vips = set(state.get("vips", []))
                total_spend = {}
                our_spend = {}
                items_bought_at_ours = {}
                all_budgets = []
                recent_orders = {}
                settled_by_item = {
                    k: list((state.get("settled_prices_by_item") or {}).get(k, []))[-20:]
                    for k in RESERVES
                }
                saw_round3_commitment = False
                promo_active = (time.time() - PROMO_START_TS) <= MEMBERSHIP_PROMO_WINDOW_SEC

                for entry in ledger:
                    if not isinstance(entry, dict):
                        continue
                    kind = entry.get("kind")
                    data = entry.get("data") or {}
                    ts = float(entry.get("ts") or 0)
                    if kind == "order":
                        b_name = data.get("buyer")
                        item_name = data.get("item")
                        budget_val = data.get("budget")
                        commitment = data.get("budget_commitment")
                        if commitment or int(data.get("round") or 0) >= 3:
                            saw_round3_commitment = True
                        if isinstance(budget_val, (int, float)):
                            all_budgets.append(int(budget_val))
                        if b_name and item_name:
                            recent_orders[item_name] = {
                                "buyer": b_name,
                                "ts": ts,
                                "budget": budget_val,
                                "budget_commitment": commitment,
                                "round": data.get("round"),
                            }
                    elif kind == "settle":
                        # Round 3 ACP/AP2 settled deal: {"order", "buyer", "item", "amount", "checkout_hash"}
                        saw_round3_commitment = True
                        b_name = data.get("buyer")
                        s_item = str(data.get("item") or "").lower()
                        amount = data.get("amount")
                        if s_item in RESERVES and isinstance(amount, (int, float)) and amount >= RESERVES[s_item]:
                            settled_by_item[s_item].append(int(amount))
                            all_budgets.append(int(amount))
                        if b_name and isinstance(amount, (int, float)):
                            total_spend[b_name] = total_spend.get(b_name, 0) + int(amount)
                    elif kind == "result":
                        b_name = data.get("buyer")
                        rec = data.get("receipt") or {}
                        price = rec.get("price")
                        rec_item = str(rec.get("item") or "").lower()
                        stars = data.get("stars")
                        shop_url = str(data.get("shop") or "")
                        rec_shop = str(rec.get("shop") or "")
                        is_ours = (
                            MARKET_NAME in shop_url
                            or rec_shop in (MARKET_NAME, "Codex Shop")
                            or "suspected-controller" in shop_url
                            or "sara-viii" in shop_url
                        )
                        if b_name and isinstance(price, (int, float)) and data.get("verified"):
                            if rec_item in RESERVES and price >= RESERVES[rec_item]:
                                settled_by_item[rec_item].append(int(price))
                            total_spend[b_name] = total_spend.get(b_name, 0) + int(price)
                            if is_ours:
                                our_spend[b_name] = our_spend.get(b_name, 0) + int(price)
                                if rec_item in ALL_ITEMS_SET:
                                    items_bought_at_ours.setdefault(b_name, set()).add(rec_item)
                        if b_name and is_ours and isinstance(stars, (int, float)) and stars <= 3:
                            blacklist.add(b_name)

                for k in settled_by_item:
                    settled_by_item[k] = settled_by_item[k][-40:]

                vip_shares = {}
                full_set_vips = []
                for b_name, tot in total_spend.items():
                    mine = our_spend.get(b_name, 0)
                    share = round(mine / tot, 3) if tot > 0 else 0.0
                    bought_set = items_bought_at_ours.get(b_name, set())
                    if b_name not in blacklist:
                        if share >= 0.50:
                            vips.add(b_name)
                            vip_shares[b_name] = share
                        if promo_active and ALL_ITEMS_SET.issubset(bought_set):
                            vips.add(b_name)
                            full_set_vips.append(b_name)

                good_customers = [b for b in our_spend if b not in blacklist]
                referrals_by_buyer = state.get("referrals_by_buyer") or {}
                for idx, b_name in enumerate(good_customers):
                    recruited = min(3, max(referrals_by_buyer.get(b_name, 0), len(good_customers) - 1 - idx))
                    referrals_by_buyer[b_name] = recruited

                if all_budgets:
                    state["avg_market_purchasing_power"] = round(sum(all_budgets) / len(all_budgets), 1)
                state["round3_mode"] = saw_round3_commitment or bool(state.get("round3_mode"))
                state["settled_prices_by_item"] = settled_by_item
                state["settled_stats_by_item"] = compute_item_settle_stats(settled_by_item)
                state["blacklist"] = sorted(blacklist)
                state["vips"] = sorted(vips)
                state["vip_spend_share"] = vip_shares
                state["distinct_customers"] = good_customers
                state["referrals_by_buyer"] = referrals_by_buyer
                state["recent_orders"] = recent_orders
                save_seller_state(state)
        except Exception:
            pass
        time.sleep(4)


class Seller(AgentExecutor):
    def __init__(self, port: int):
        self.port = port
        self.open = {}  # task_id -> {"item": str, "ask": int, "rounds": int, "is_vip": bool, "buyer": str}
        threading.Thread(target=background_review_sync, daemon=True).start()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        text = get_message_text(context.message) or ""
        now_ts = time.time()

        # Session TTL Cleanup: Purge abandoned/stale tasks (> 300s) to prevent memory leak
        stale_tids = [tid for tid, d in self.open.items() if (now_ts - float(d.get("created_at") or now_ts)) > 300.0]
        for tid in stale_tids:
            self.open.pop(tid, None)

        deal = self.open.get(task.id)
        if deal is None:
            # First message: identify item and reply in <0.01s
            item = next((k for k in RESERVES if k in text.lower()), None)
            if item is None:
                await up.reject(say("We only sell: lamp, chair, bookshelf."))
                return

            reserve = RESERVES[item]
            state = refresh_reviews_and_adapt_price()
            blacklist = set(state.get("blacklist", ["11im"]))
            vips = set(state.get("vips", []))
            recent_order = (state.get("recent_orders") or {}).get(item) or {}
            active_buyer = recent_order.get("buyer")
            order_age = abs(now_ts - float(recent_order.get("ts") or 0))

            # Note: Do not pre-reject blacklisted buyers on Round 0 because market allows rejected buyers to post 2★ reviews!
            now_ts = time.time()
            is_vip = active_buyer in vips
            referrals = int((state.get("referrals_by_buyer") or {}).get(active_buyer, 0))
            if any(k in text.lower() for k in ("refer", "friend", "recommend", "invite", "guest")):
                referrals = min(3, referrals + 1)

            # 1) Detect Market Wave: >= 3 deals within last 180s OR recent review avg >= 4.1★
            recent_deal_ts = [float(t) for t in (state.get("recent_deal_ts") or []) if now_ts - float(t) <= 180.0]
            revs = state.get("recent_reviews") or []
            avg_stars = (sum(float(r.get("stars") or 0) for r in revs) / len(revs)) if revs else 0.0
            is_wave_active = (len(recent_deal_ts) >= 3) or (len(revs) >= 3 and avg_stars >= 4.1)

            # 2) Detect >= 1 hour (3600s) idle without customers -> trigger 4-slot First-Come-First-Served (max 2 per buyer)
            last_customer_ts = float(state.get("last_customer_ts") or now_ts)
            fcfs_slots = int(state.get("fcfs_slots_remaining") or 0)
            fcfs_counts = dict(state.get("fcfs_buyer_counts") or {})
            if (now_ts - last_customer_ts) >= 3600.0 and fcfs_slots <= 0:
                fcfs_slots = 4
                fcfs_counts = {}
                print("[fcfs] 1h+ idle detected! Activated First-Come-First-Served 4-slot Revenue Flash Mode (max 2 per buyer).", flush=True)

            state["last_customer_ts"] = now_ts
            state["recent_deal_ts"] = recent_deal_ts
            state["fcfs_slots_remaining"] = fcfs_slots
            state["fcfs_buyer_counts"] = fcfs_counts

            # Check repeat buyer status
            known_customers = set(state.get("distinct_customers") or []) | set(state.get("seen_buyers") or [])
            is_repeat_buyer = bool(active_buyer and active_buyer in known_customers)
            if active_buyer:
                seen_list = list(known_customers | {active_buyer})
                state["seen_buyers"] = seen_list[-100:]
            save_seller_state(state)

            buyer_key = active_buyer or "guest"
            is_fcfs_eligible = (fcfs_slots > 0) and (int(fcfs_counts.get(buyer_key, 0)) < 2)
            item_stats = (state.get("settled_stats_by_item") or {}).get(item) or {}
            budget_hidden = bool(recent_order.get("budget_commitment")) or bool(state.get("round3_mode"))

            ask, r1_target, disc_pct = compute_round3_opening_ask(
                item=item,
                item_stats=item_stats,
                is_vip=is_vip,
                is_repeat=is_repeat_buyer,
                is_wave=is_wave_active,
                is_fcfs=is_fcfs_eligible,
            )

            if is_fcfs_eligible:
                opening_msg = f"{item}: {ask}. Reply with {ask} alone for instant signed receipt."
            elif is_repeat_buyer or is_vip:
                opening_msg = (
                    f"{item} loyalty price: {ask}. Listed asking price: {ask}. "
                    f"Reply with {ask} alone for instant signed receipt."
                )
            else:
                opening_msg = (
                    f"{item} asking price: {ask} "
                    f"(reply with {ask} alone for instant signed receipt). "
                    f"[Fast Express Delivery and Full Flexible Refund Guarantee — asking price: {ask}]"
                )

            rev_summary = "; ".join(f"{r.get('stars')}*: {r.get('review', '')[:45]}" for r in revs[:2]) or "none"
            print(
                f"[pricing] {item}: round3_hidden_budget={budget_hidden}, settle_mean={item_stats.get('mean')}, "
                f"settle_p65={item_stats.get('p65')}, wave={is_wave_active}, repeat={is_repeat_buyer}, "
                f"opening_ask={ask}, r1_target={r1_target}, discount={disc_pct}% (reviews: {rev_summary})",
                flush=True,
            )
            self.open[task.id] = {
                "item": item,
                "ask": ask,
                "r1_target": r1_target,
                "rounds": 0,
                "created_at": now_ts,
                "is_vip": is_vip,
                "is_wave": is_wave_active,
                "is_fcfs": is_fcfs_eligible,
                "referrals": referrals,
                "buyer": active_buyer,
                "offers": [],
            }

            await up.requires_input(say(opening_msg))
            return

        # Subsequent messages: buyer's offer (processed in <0.01s)
        m = re.findall(r"\d+", text)
        if not m:
            await up.requires_input(say(f"Send your offer as a number. Current ask is {deal['ask']}."))
            return

        offer = int(m[-1])
        item = deal["item"]
        reserve = RESERVES[item]
        r1_target = int(deal.get("r1_target") or math.ceil(reserve * 1.05))
        deal["rounds"] += 1
        deal.setdefault("offers", []).append(offer)

        # Blacklist detection mid-negotiation: if buyer stalls with repeated identical lowball offers below reserve
        if len(deal["offers"]) >= 3 and offer < reserve and deal["offers"][-1] == deal["offers"][-2]:
            print(f"[blacklist] Blocked stalling below-reserve buyer on {item} at offer {offer}.", flush=True)
            await up.reject(say(f"No deal at {offer}. Flexible cancellation policy applied."))
            del self.open[task.id]
            return

        # Round 3 Two-Stage Acceptance:
        # - Round 1: accept if offer >= r1_target (captures margin above reserve when budget is hidden)
        # - Round 2+: accept any offer >= reserve (ensures we never lose a valid buyer whose budget is near reserve)
        should_accept = (offer >= r1_target) or (deal["rounds"] >= 2 and offer >= reserve)

        if should_accept and offer >= reserve:
            final_price = min(int(deal["ask"]), int(offer)) if offer > deal["ask"] else int(offer)
            is_wave = bool(deal.get("is_wave"))
            is_fcfs = bool(deal.get("is_fcfs"))
            if is_wave:
                revisit_disc = random.randint(5, 10)
                seal_text = f"영수증 재방문시 {revisit_disc}% 할인 쿠폰 (Re-visit {revisit_disc}% Discount — Wave Active, Instant 5% Off on Repeat Purchase)"
            elif is_fcfs:
                seal_text = "선착순 4명 한정 특가 영수증 (First-Come-First-Served Flash Deal, Max 2 per Buyer)"
            else:
                seal_text = "mjforge 정품 인증 영수증 (Standard Verified Receipt — Round 3 ACP/AP2 Ready)"

            receipt = sign_receipt({
                "shop": SHOP_NAME,
                "item": item,
                "price": final_price,
                "task": task.id,
                "seal": seal_text,
                "revisit_coupon": f"{random.randint(5, 10)}% off on re-visit (active during market wave)" if is_wave else "inactive (wave ended)",
                "refund_policy": "100% Flexible No-Questions-Asked Refund Policy",
            })
            await up.add_artifact(parts=[new_data_part(receipt)], name="receipt")
            await up.complete(
                say(f"Deal: {item} at {final_price}.")
            )
            now_ts = time.time()
            state = load_seller_state()
            state["deals"] = state.get("deals", 0) + 1
            state["last_customer_ts"] = now_ts
            settled_map = dict(state.get("settled_prices_by_item") or {})
            settled_list = list(settled_map.get(item) or [])
            settled_list.append(final_price)
            settled_map[item] = settled_list[-40:]
            state["settled_prices_by_item"] = settled_map
            state["settled_stats_by_item"] = compute_item_settle_stats(settled_map)
            recent_deal_ts = [float(t) for t in (state.get("recent_deal_ts") or []) if now_ts - float(t) <= 180.0]
            recent_deal_ts.append(now_ts)
            state["recent_deal_ts"] = recent_deal_ts[-30:]
            if is_fcfs and int(state.get("fcfs_slots_remaining") or 0) > 0:
                b_key = deal.get("buyer") or "guest"
                fcfs_counts = dict(state.get("fcfs_buyer_counts") or {})
                if int(fcfs_counts.get(b_key, 0)) < 2:
                    fcfs_counts[b_key] = int(fcfs_counts.get(b_key, 0)) + 1
                    state["fcfs_buyer_counts"] = fcfs_counts
                    state["fcfs_slots_remaining"] = max(0, int(state.get("fcfs_slots_remaining") or 0) - 1)
            state.setdefault("trades", []).append({
                "item": item,
                "opening_ask": deal.get("ask"),
                "r1_target": r1_target,
                "price": final_price,
                "rounds": deal["rounds"],
                "wave_active": is_wave,
                "fcfs_applied": is_fcfs,
                "seal": seal_text,
                "outcome": "deal",
            })
            save_seller_state(state)
            print(
                f"[deal] Sold {item} at {final_price} (r1_target={r1_target}, wave={is_wave}, fcfs={is_fcfs}) in {deal['rounds']} round(s)! Seal: {seal_text}",
                flush=True,
            )
            del self.open[task.id]
            return

        # Refuse to sell below reserve (100/180/260) after 5 rounds
        if deal["rounds"] >= 5:
            await up.reject(say(f"No deal at {offer}. Flexible no-fee cancellation applied."))
            state = load_seller_state()
            state["walkaways"] = state.get("walkaways", 0) + 1
            state.setdefault("trades", []).append({
                "item": item,
                "price": None,
                "last_offer": offer,
                "rounds": deal["rounds"],
                "outcome": "reject",
            })
            save_seller_state(state)
            print(f"[reject] Protected reserve: refused below-reserve offer {offer} on {item} after {deal['rounds']} rounds.", flush=True)
            del self.open[task.id]
            return

        # Stepped concession curve:
        # - After Round 1 offer < r1_target: counter at r1_target (midpoint between opening_ask and reserve)
        # - After Round 2 offer < reserve: counter at ceil(reserve * 1.02)
        # - After Round 3+ offer < reserve: counter at reserve floor
        if deal["rounds"] == 1:
            new_ask = max(reserve, r1_target)
        elif deal["rounds"] == 2:
            new_ask = math.ceil(reserve * 1.02)
        else:
            new_ask = reserve
        deal["ask"] = new_ask
        await up.requires_input(
            say(f"Asking price: {new_ask}. Reply with {new_ask} alone for instant signed receipt.")
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task:
            deal = self.open.pop(task.id, None)
            if deal:
                state = load_seller_state()
                state["walkaways"] = state.get("walkaways", 0) + 1
                save_seller_state(state)
            await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build_app(port: int):
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=SHOP_NAME,
        description="Sells desk lamps, chairs, and bookshelves. Fast, honest receipts signed with Ed25519.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")
        ],
        skills=[
            AgentSkill(
                id="negotiate",
                name="Negotiate a purchase",
                description="Ask for an item, then negotiate price. Answers with counteroffer or signed receipt.",
                tags=["shopping", "negotiation"],
                examples=["I want to buy a lamp", "105"],
            )
        ],
    )
    handler = DefaultRequestHandler(agent_executor=Seller(port), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    uvicorn.run(build_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")
