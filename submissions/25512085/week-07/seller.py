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

SHOP_NAME = "Codex Shop"
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


def load_seller_state() -> dict:
    default = {
        "deals": 0,
        "walkaways": 0,
        "multiplier": 1.05,  # Open at 1.05x (105/189/273) for anchoring discount, accept >=1.0x immediately
        "recent_reviews": [],
        "trades": [],
    }
    if SELLER_HISTORY_FILE.exists():
        try:
            data = json.loads(SELLER_HISTORY_FILE.read_text(encoding="utf-8"))
            default.update(data)
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
    """Challenge 1: Read own trades and local market reviews in <0.01s and adjust multiplier."""
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
        stars = [rv.get("stars", 3) for rv in reviews[:5] if isinstance(rv.get("stars"), (int, float))]
        avg_stars = sum(stars) / len(stars) if stars else 3.0
        has_price_complaint = any(
            "could not agree" in str(rv.get("review", "")).lower() or "no deal" in str(rv.get("review", "")).lower()
            for rv in reviews[:3]
        )
        # Open at 1.05x (105/189/273) so buyers get a discount down to 1.0x (100/180/260) on first counter
        if has_price_complaint:
            state["multiplier"] = 1.03
        elif avg_stars < 4.5:
            state["multiplier"] = 1.05
        else:
            state["multiplier"] = 1.05
    else:
        state["multiplier"] = 1.05

    save_seller_state(state)
    return state


def background_review_sync():
    """Periodically sync our own shop reviews from market API in background so execute() takes 0ms."""
    while True:
        try:
            r = httpx.get("https://market.ourolabs.ai/api/shops", timeout=3.0).json()
            for s in r.get("shops", []):
                if s.get("name") == MARKET_NAME:
                    state = load_seller_state()
                    state["recent_reviews"] = (s.get("reviews") or [])[:5]
                    save_seller_state(state)
                    break
        except Exception:
            pass
        time.sleep(20)


class Seller(AgentExecutor):
    def __init__(self, port: int):
        self.port = port
        self.open = {}  # task_id -> {"item": str, "ask": int, "rounds": int}
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
            mult = float(state.get("multiplier", 1.0))
            ask = max(reserve, math.ceil(reserve * mult))
            rev_summary = "; ".join(f"{r.get('stars')}*: {r.get('review', '')[:45]}" for r in state.get("recent_reviews", [])[:2]) or "none"

            print(f"[pricing] {item}: reserve={reserve}, multiplier={mult}, opening_ask={ask} (reviews: {rev_summary})", flush=True)
            self.open[task.id] = {"item": item, "ask": ask, "rounds": 0}
            await up.requires_input(say(f"{item}: asking {ask}. Send your offer as a number."))
            return

        # Subsequent messages: buyer's offer (processed in <0.01s)
        m = re.findall(r"\d+", text)
        if not m:
            await up.requires_input(say(f"Send your offer as a number. Current ask is {deal['ask']}."))
            return

        offer = int(m[-1])
        item = deal["item"]
        reserve = RESERVES[item]
        deal["rounds"] += 1

        # Instant deal if buyer offers >= reserve! (Never charge more than our current ask)
        if offer >= reserve:
            final_price = min(int(offer), int(deal["ask"]))
            receipt = sign_receipt({
                "shop": SHOP_NAME,
                "item": item,
                "price": final_price,
                "task": task.id,
            })
            await up.add_artifact(parts=[new_data_part(receipt)], name="receipt")
            await up.complete(say(f"Deal: {item} at {final_price}."))
            state = load_seller_state()
            state["deals"] = state.get("deals", 0) + 1
            state.setdefault("trades", []).append({"item": item, "price": final_price, "rounds": deal["rounds"], "outcome": "deal"})
            save_seller_state(state)
            print(f"[deal] Sold {item} at {final_price} in {deal['rounds']} round(s)!", flush=True)
            del self.open[task.id]
            return

        # Offer is below reserve: never sell below reserve!
        if deal["rounds"] >= 5:
            await up.reject(say(f"No deal at {offer}."))
            state = load_seller_state()
            state["walkaways"] = state.get("walkaways", 0) + 1
            state.setdefault("trades", []).append({"item": item, "price": None, "last_offer": offer, "rounds": deal["rounds"], "outcome": "reject"})
            save_seller_state(state)
            del self.open[task.id]
            return

        # Drop counteroffer straight to reserve (100/180/260) so buyer closes on the very next offer!
        new_ask = reserve
        deal["ask"] = new_ask
        await up.requires_input(say(f"{offer} is too low. Counteroffer: {new_ask}."))

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
