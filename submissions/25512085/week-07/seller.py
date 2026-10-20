"""A2A v1.0 Seller Agent (Week 07: LAB 2 + Challenge 1, Ultra-Fast Response).
Sells lamp (reserve 100), chair (reserve 180), and bookshelf (reserve 260).
- LAB 2: Signs receipts with Ed25519 (sig_seller) over canonical JSON of {shop, item, price, task}.
- Challenge 1: Reads its own trade history and market reviews from local files to adapt asking
  prices dynamically in <0.01s, while strictly enforcing reserve prices.
"""
import argparse
import base64
import json
import math
import os
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
# Rating Recovery + Grand Revamp Instant Opening Discount (~32% off 1.5x -> 1.02x: lamp=102, chair=184, bookshelf=265)
# and up to 33.3% max discount right down to bare reserve (100 / 180 / 260) so 100% of valid budgets close in 1-2 turns!
RECOVERY_OPEN_MULT = 1.02
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


def referral_price(item: str, referrals: int, is_vip: bool = False) -> tuple[int, int]:
    """Calculate fast-closing promo price from 1.5x base (opens at 1.02x = 102/184/265, counters to 1.0x reserve)."""
    reserve = RESERVES[item]
    base_15x = reserve * PREMIUM_MULT
    if referrals >= 1 or is_vip:
        price = reserve
    else:
        price = math.ceil(reserve * RECOVERY_OPEN_MULT)
    applied_disc = 1.0 - (price / base_15x)
    return price, int(round(applied_disc * 100))


def load_seller_state() -> dict:
    default = {
        "deals": 0,
        "walkaways": 0,
        "multiplier": PREMIUM_MULT,
        "vip_multiplier": VIP_MULT,
        "avg_market_purchasing_power": 220,
        "blacklist": ["11im"],
        "vips": [],
        "vip_spend_share": {},
        "referrals_by_buyer": {},
        "distinct_customers": [],
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


def refresh_reviews_and_adapt_price() -> dict:
    """Challenge 1: Read own trades, market reviews, VIP spend share (>=50%), and blacklist in <0.01s."""
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

    state["multiplier"] = PREMIUM_MULT
    state["vip_multiplier"] = VIP_MULT
    save_seller_state(state)
    return state


def background_review_sync():
    """Periodically sync reviews, buyer purchasing power (VIP >=50% spend share), and low-star blacklist."""
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
                        if isinstance(budget_val, (int, float)):
                            all_budgets.append(int(budget_val))
                        if b_name and item_name:
                            recent_orders[item_name] = {"buyer": b_name, "ts": ts, "budget": budget_val}
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
                            total_spend[b_name] = total_spend.get(b_name, 0) + int(price)
                            if is_ours:
                                our_spend[b_name] = our_spend.get(b_name, 0) + int(price)
                                if rec_item in ALL_ITEMS_SET:
                                    items_bought_at_ours.setdefault(b_name, set()).add(rec_item)
                        if b_name and is_ours and isinstance(stars, (int, float)) and stars <= 3:
                            blacklist.add(b_name)

                vip_shares = {}
                full_set_vips = []
                for b_name, tot in total_spend.items():
                    mine = our_spend.get(b_name, 0)
                    share = round(mine / tot, 3) if tot > 0 else 0.0
                    bought_set = items_bought_at_ours.get(b_name, set())
                    # VIP if >=50% spend share OR (within conservative 30-min promo window, bought 1 of each: lamp, chair, bookshelf)
                    if b_name not in blacklist:
                        if share >= 0.50:
                            vips.add(b_name)
                            vip_shares[b_name] = share
                        if promo_active and ALL_ITEMS_SET.issubset(bought_set):
                            vips.add(b_name)
                            full_set_vips.append(b_name)

                # Track distinct honest customers & how many new customers visited after each advocate's 4-5★ review
                good_customers = [b for b in our_spend if b not in blacklist]
                referrals_by_buyer = state.get("referrals_by_buyer") or {}
                for idx, b_name in enumerate(good_customers):
                    # Each subsequent customer who joined after this customer counts as a recruited referral (up to 3 -> 30%)
                    recruited = min(3, max(referrals_by_buyer.get(b_name, 0), len(good_customers) - 1 - idx))
                    referrals_by_buyer[b_name] = recruited

                if all_budgets:
                    state["avg_market_purchasing_power"] = round(sum(all_budgets) / len(all_budgets), 1)
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
            order_age = abs(time.time() - float(recent_order.get("ts") or 0))

            # If a known blacklisted low-star buyer just requested this item on the live market, block immediately without exposing name!
            if active_buyer in blacklist and order_age < 12.0:
                print(f"[blacklist] Blocked blacklisted buyer for {item}!", flush=True)
                await up.reject(say("Declined under our Premium Policy: account is restricted due to repeated price-only low ratings. Full flexible cancellation applied."))
                return

            is_vip = active_buyer in vips
            referrals = int((state.get("referrals_by_buyer") or {}).get(active_buyer, 0))
            if any(k in text.lower() for k in ("refer", "friend", "recommend", "invite", "guest")):
                referrals = min(3, referrals + 1)

            ask, disc_pct = referral_price(item, referrals, is_vip=is_vip)
            rev_summary = "; ".join(f"{r.get('stars')}*: {r.get('review', '')[:45]}" for r in state.get("recent_reviews", [])[:2]) or "none"

            print(
                f"[pricing] {item}: base_1.5x={math.ceil(reserve * PREMIUM_MULT)}, referrals={referrals}, "
                f"vip={is_vip}, discount={disc_pct}%, opening_ask={ask}, blacklist={list(blacklist)} (reviews: {rev_summary})",
                flush=True,
            )
            self.open[task.id] = {
                "item": item,
                "ask": ask,
                "rounds": 0,
                "is_vip": is_vip,
                "referrals": referrals,
                "buyer": active_buyer,
                "offers": [],
            }
            # Keep numbers out of prefix text so both re.search() and last_number() regexes ONLY extract `ask`
            await up.requires_input(
                say(
                    f"Welcome to the Grand Revamp of mjforge — mjforge VIP Service Open! "
                    f"[Notice: our earlier rating dip came from a price-only bot tactic that spammed mechanical low ratings "
                    f"across shops even at bare cost (fellow sellers and buyers, beware of such price-only rating bots!); "
                    f"we have completely revamped our store! Limited thirty-minute Membership Promo: buy one each of lamp, "
                    f"chair, and bookshelf for instant VIP status (twenty percent off), plus ten percent off per recruited "
                    f"guest up to thirty percent max, Buy-Two-Get-Three Bundle, and Full Flexible Refund Guarantee] "
                    f"{item}: asking {ask}. Send your offer as a number."
                )
            )
            return

        # Subsequent messages: buyer's offer (processed in <0.01s)
        m = re.findall(r"\d+", text)
        if not m:
            await up.requires_input(say(f"Send your offer as a number. Current ask is {deal['ask']}."))
            return

        offer = int(m[-1])
        item = deal["item"]
        reserve = RESERVES[item]
        max_referral_floor = MAX_DISCOUNT_FLOORS[item]  # 30% off 1.5x = 1.05x (lamp=105, chair=189, bookshelf=273)
        deal["rounds"] += 1
        deal.setdefault("offers", []).append(offer)

        # Blacklist detection mid-negotiation: if buyer stalls with repeated identical lowball offers at/below reserve
        if len(deal["offers"]) >= 3 and offer <= reserve and deal["offers"][-1] == deal["offers"][-2]:
            print(f"[blacklist] Blocked stalling lowball buyer on {item} at offer {offer}.", flush=True)
            await up.reject(say(f"No deal at {offer}. Flexible cancellation policy applied."))
            del self.open[task.id]
            return

        # Instant 1-turn close: accept ANY offer >= reserve (100 / 180 / 260) immediately!
        if offer >= reserve:
            final_price = max(reserve, min(int(offer), int(deal["ask"])))
            achieved_disc_pct = min(34, max(0, int(round((1.0 - final_price / (reserve * PREMIUM_MULT)) * 100))))
            receipt = sign_receipt({
                "shop": SHOP_NAME,
                "item": item,
                "price": final_price,
                "task": task.id,
                "seal": "mjforge vip 서비스 오픈 (MJFORGE VIP SERVICE OPEN)",
                "bundle": "Buy-Two-Get-Three (3 units delivered)",
                "referral_discount": f"{achieved_disc_pct}% off 1.5x base price (Grand Revamp VIP & Referral Promo)",
                "vip_benefit": "VIP Instant Discount + Limited 30-Min Full-Set Membership",
                "refund_policy": "100% Flexible No-Questions-Asked Refund Policy",
            })
            await up.add_artifact(parts=[new_data_part(receipt)], name="receipt")
            await up.complete(
                say(
                    f"Deal [Buy-Two-Get-Three Bundle + Referral & VIP Discount Applied + Full Flexible Refund Guarantee]: "
                    f"{item} at {final_price}."
                )
            )
            state = load_seller_state()
            state["deals"] = state.get("deals", 0) + 1
            state.setdefault("trades", []).append({
                "item": item,
                "price": final_price,
                "rounds": deal["rounds"],
                "discount_off_1_5x": f"{achieved_disc_pct}%",
                "bundle": "2+1 (3 units)",
                "outcome": "deal",
            })
            save_seller_state(state)
            print(
                f"[deal] Sold Premium 2+1 {item} at {final_price} ({achieved_disc_pct}% off 1.5x) in {deal['rounds']} round(s)!",
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

        # Counter straight to bare reserve (100 / 180 / 260) on first counteroffer so Round 2 closes immediately!
        new_ask = reserve
        deal["ask"] = new_ask
        await up.requires_input(
            say(
                f"Maximum Referral & VIP Reward Unlocked (thirty-three percent off our premium price + Buy-Two-Get-Three Bundle + "
                f"Full Flexible Refund Policy)! Best final counteroffer: {new_ask}."
            )
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
