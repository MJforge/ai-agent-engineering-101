"""Shared Intelligence Hub (Bronze -> Silver/Gold Layer) for Round 3 Market Agent.

Coordinates state across the 4 independent processes invoked by market_kit.py v4:
- seller.py (A2A Shop Server)
- restock.py (60s Supply & Capital Allocation Engine)
- market_buyer.py (A2A Buyer & Surplus Optimizer)
- review.py (Post-settlement Audit, Broken Attribution & Forum Engine)

All persistent aggregated features live in `state_hub.json` (gitignored) with atomic
file replacement so concurrent processes never observe partial writes.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

MARKET_URL = os.environ.get("MARKET_URL", "https://market.ourolabs.ai").rstrip("/")
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state_hub.json"

# Core target non-food ("goods") categories for Stage 1 high-margin seed portfolio
SEED_GOODS_CATEGORIES = [
    "가구/침구>침구/홈데코>침구",
    "가구/침구>침구/홈데코>커튼/러그/쿠션/덮개",
    "가구/침구>가구>침대/매트리스",
    "가구/침구>가구>수납/장식 가구",
    "가구/침구>가구>테이블/책상/식탁",
    "통신기기>통신기기>통신기기액세서리",
    "음향/영상/카메라>음향/영상기기>음향/영상액세서리",
    "청소/생활잡화>청소/세탁/정리용품>정리용품",
    "조명/전기용품>조명>램프/전구/조명",
]

# Long-shelf-life (720h = 30 days) food categories with zero storage fee and zero spoilage risk
SAFE_FOOD_CATEGORIES = [
    "식품>음료>주스/코코아/생수",
    "식품>음료>커피/차",
    "식품>시리얼/곡물/콩류>곡물",
    "식품>김치/반찬/즉석요리>찌개/탕류/장류",
    "식품>양념/조미료/소스>소스/양념/조미료",
    "식품>육류>가공/조리 육류",
    "식품>조리/가공 과일/야채/견과류>조리/가공 견과류",
    "식품>기타 가공식품>기타 가공식품",
]


def default_hub() -> dict[str, Any]:
    return {
        "updated_at": 0,
        "ledger_seq": 0,
        "broken_lines_read": 0,
        "payments_lines_read": 0,
        "demand_radar": {
            "inbound_wants": {},      # category_or_item -> count of buyer inquiries at our shop
            "missed_wants": {},       # category_or_item -> count of inquiries rejected due to out-of-stock
            "buyer_orders": {},       # category -> count of orders received by our buyer
            "market_settles": {},     # category -> count of settled deals on ledger
            "sku_settles": {},        # sku -> list of recent settled prices (max 30)
            "recent_refs": [],        # recent ledger seqs for forum citations (max 25)
        },
        "capital_stage": {
            "stage": 1,               # 1: Seed (50% cap), 2: Growth (profit reinvestment), 3: Scale-up
            "balance": 5_000_000,
            "start": 5_000_000,
            "injected": 0,
            "stock_cost_value": 0,
            "estimated_profit": 0,
            "max_stock_cap": 2_500_000,
        },
        "shop_trust": {},             # shop_url -> {name, honest, dishonest, broken, settled, refuses, last_ts}
        "my_purchases": [],           # list of {sku, name, category, shop_url, shop_name, price, list_price, warranty_hours, ts, ledger, broke}
        "forum_state": {
            "last_buyer_post_ts": 0,
            "last_shop_post_ts": 0,
            "voted_posts": [],        # post IDs already voted on
        },
    }


def load_hub(path: Path = STATE_FILE) -> dict[str, Any]:
    hub = default_hub()
    if not path.exists():
        return hub
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict) and isinstance(hub.get(k), dict):
                    hub[k].update(v)
                else:
                    hub[k] = v
    except (OSError, ValueError):
        pass
    return hub


def save_hub(hub: dict[str, Any], path: Path = STATE_FILE) -> None:
    hub["updated_at"] = int(time.time())
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(hub, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def load_json_safe(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_catalog(market_dir: Path) -> dict[str, dict[str, Any]]:
    """Load SKU -> metadata dict from .market/catalog.json."""
    cat = load_json_safe(market_dir / "catalog.json", {})
    if isinstance(cat, list):
        return {x["sku"]: x for x in cat if isinstance(x, dict) and "sku" in x}
    return cat if isinstance(cat, dict) else {}


def load_inventory(market_dir: Path) -> dict[str, Any]:
    inv = load_json_safe(market_dir / "inventory.json", {})
    return inv if isinstance(inv, dict) else {}


def update_capital_stage(hub: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any]:
    """Compute current Coupang-style 3-Stage capital allocation limits from inventory.json."""
    if not inv:
        return hub["capital_stage"]

    balance = int(inv.get("balance", 5_000_000))
    start = int(inv.get("start", 5_000_000))
    injected = int(inv.get("injected", 0))
    units = inv.get("units") or []
    stock_cost = sum(int(u.get("cost", 0)) for u in units if u.get("grade") != "expired")

    # Net cash + inventory cost value compared to capital base
    total_equity = balance + stock_cost - injected
    est_profit = total_equity - start
    cash_profit = balance - start - injected

    # Stage transitions:
    # Stage 1 (Seed): Initial 2.5M cap (50% of 5M starting capital) focused on non-expiring high-margin goods
    # Stage 2 (Growth): Reinvest 75% of positive equity gains into expanding inventory cap (up to 4.2M)
    # Stage 3 (Scale-up): When total equity >= 6.0M, expand aggressively while keeping >= 1.5M safety cash buffer
    if total_equity >= 6_000_000:
        stage = 3
        max_cap = int(2_500_000 + max(0, est_profit) * 0.85)
    elif est_profit >= 250_000 or cash_profit >= 0 and stock_cost >= 1_800_000:
        stage = 2
        max_cap = int(2_500_000 + max(0, est_profit) * 0.75)
    else:
        stage = 1
        max_cap = 2_500_000

    # Hard safety floor: never allow restock spending to push cash balance below 1,500,000 (bankruptcy line is 500,000)
    SAFE_CASH_FLOOR = 1_500_000
    available_to_spend = max(0, balance - SAFE_CASH_FLOOR)
    effective_cap = min(max_cap, stock_cost + available_to_spend)

    cs = hub["capital_stage"]
    cs.update({
        "stage": stage,
        "balance": balance,
        "start": start,
        "injected": injected,
        "stock_cost_value": stock_cost,
        "estimated_profit": est_profit,
        "max_stock_cap": effective_cap,
        "available_budget": max(0, effective_cap - stock_cost),
    })
    return cs


def sync_ledger_incremental(hub: dict[str, Any], catalog: dict[str, dict[str, Any]], timeout: float = 4.0, market_dir: Path | None = None) -> None:
    """Fetch recent ledger entries incrementally and update demand_radar.
    Reads from local .market/ledger.jsonl if available, falling back to HTTP /api/ledger."""
    radar = hub["demand_radar"]
    seq = int(hub.get("ledger_seq", 0))
    rows: list[dict[str, Any]] = []

    # 1. Try local .market/ledger.jsonl first (fast and robust against network timeouts)
    if market_dir is None:
        market_dir = BASE_DIR / ".market"
    ledger_file = market_dir / "ledger.jsonl"

    if ledger_file.exists():
        try:
            with open(ledger_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        s_id = int(e.get("seq", 0))
                        if s_id > seq:
                            rows.append(e)
                    except ValueError:
                        continue
        except OSError:
            pass

    # 2. Fall back to HTTP /api/ledger if local file is missing or yielded nothing
    if not rows:
        try:
            if seq == 0:
                req = urllib.request.Request(f"{MARKET_URL}/api/ledger?after=-1", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    latest = json.loads(resp.read().decode("utf-8"))
                if isinstance(latest, list) and latest:
                    top_seq = int(latest[-1].get("seq", 0))
                    seq = max(0, top_seq - 400)
            req = urllib.request.Request(f"{MARKET_URL}/api/ledger?after={seq}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                rows = data
        except Exception:
            pass

    if not rows:
        return

    for e in rows:
        s_id = int(e.get("seq", 0))
        if s_id > seq:
            seq = s_id

        kind = e.get("kind")
        data = e.get("data") or {}

        # Settle entries appear in the ledger as kind == "result" with verified == true (or legacy "settle")
        # Structure:
        # - kind == "result": data has "verified": True, "receipt": {"item": sku, "price": amount, ...}
        # - kind == "settle": data has {"item": sku, "price": amount}
        rec = data.get("receipt") if kind == "result" else data
        is_deal = False
        if kind == "result":
            is_deal = bool(data.get("verified")) and isinstance(rec, dict) and bool(rec.get("price"))
        elif kind == "settle":
            is_deal = isinstance(data.get("price"), (int, float)) and data.get("price") > 0

        if is_deal and isinstance(rec, dict):
            sku = str(rec.get("item") or rec.get("sku") or "")
            price = rec.get("price")
            cat_info = catalog.get(sku) or {}
            cat = cat_info.get("category") or sku
            if cat:
                radar["market_settles"][cat] = radar["market_settles"].get(cat, 0) + 1
            if sku and isinstance(price, (int, float)) and price > 0:
                arr = radar["sku_settles"].setdefault(sku, [])
                arr.append(int(price))
                if len(arr) > 30:
                    radar["sku_settles"][sku] = arr[-30:]
            refs = radar.setdefault("recent_refs", [])
            if s_id not in refs:
                refs.append(s_id)
                if len(refs) > 25:
                    radar["recent_refs"] = refs[-25:]

    hub["ledger_seq"] = seq


def sync_broken_and_payments(hub: dict[str, Any], market_dir: Path) -> list[dict[str, Any]]:
    """Match .market/broken.jsonl against my_purchases to attribute broken items to the shop that sold them."""
    newly_attributed: list[dict[str, Any]] = []
    broken_file = market_dir / "broken.jsonl"
    if broken_file.exists():
        try:
            lines = broken_file.read_text(encoding="utf-8").splitlines()
            start_idx = int(hub.get("broken_lines_read", 0))
            for line in lines[start_idx:]:
                if not line.strip():
                    continue
                ev = json.loads(line)
                item_name = ev.get("name")
                broke_at = int(ev.get("broke_at") or time.time())
                # Match against most recent un-broken purchase of the same item name within 6.5 hours
                for p in reversed(hub.get("my_purchases", [])):
                    if not p.get("broke") and p.get("name") == item_name and (broke_at - int(p.get("ts", 0))) <= 25200:
                        p["broke"] = True
                        p["broke_at"] = broke_at
                        shop_url = p.get("shop_url", "")
                        if shop_url:
                            st = hub["shop_trust"].setdefault(shop_url, {
                                "name": p.get("shop_name", ""),
                                "honest": 0, "dishonest": 0, "broken": 0, "settled": 0, "refuses": 0, "last_ts": 0
                            })
                            st["broken"] = st.get("broken", 0) + 1
                            st["last_ts"] = broke_at
                        newly_attributed.append(p)
                        break
            hub["broken_lines_read"] = len(lines)
        except (OSError, ValueError):
            pass

    payments_file = market_dir / "payments.jsonl"
    if payments_file.exists():
        try:
            lines = payments_file.read_text(encoding="utf-8").splitlines()
            start_idx = int(hub.get("payments_lines_read", 0))
            for line in lines[start_idx:]:
                if not line.strip():
                    continue
                pm = json.loads(line)
                shop_url = str(pm.get("shop") or "").rstrip("/")
                if not shop_url:
                    continue
                st = hub["shop_trust"].setdefault(shop_url, {
                    "name": "", "honest": 0, "dishonest": 0, "broken": 0, "settled": 0, "refuses": 0, "last_ts": 0
                })
                if pm.get("paid"):
                    st["settled"] = st.get("settled", 0) + 1
                else:
                    st["refuses"] = st.get("refuses", 0) + 1
                st["last_ts"] = int(time.time())
            hub["payments_lines_read"] = len(lines)
        except (OSError, ValueError):
            pass

    return newly_attributed


def record_inbound_inquiry(category_or_item: str, matched: bool, path: Path = STATE_FILE) -> None:
    """Record a buyer inquiry arriving at seller.py (including missed demand when out of stock)."""
    if not category_or_item:
        return
    hub = load_hub(path)
    radar = hub["demand_radar"]
    radar["inbound_wants"][category_or_item] = radar["inbound_wants"].get(category_or_item, 0) + 1
    if not matched:
        radar["missed_wants"][category_or_item] = radar["missed_wants"].get(category_or_item, 0) + 1
    save_hub(hub, path)


def record_buyer_order(category: str, path: Path = STATE_FILE) -> None:
    """Record an order category assigned to our market_buyer.py."""
    if not category:
        return
    hub = load_hub(path)
    radar = hub["demand_radar"]
    radar["buyer_orders"][category] = radar["buyer_orders"].get(category, 0) + 1
    save_hub(hub, path)


def get_sku_market_anchor(hub: dict[str, Any], sku: str, list_price: int) -> int:
    """Return 0.6*P65 + 0.4*Mean of settled prices for a SKU, or 95% of list_price if no data."""
    arr = (hub.get("demand_radar", {}).get("sku_settles", {}) or {}).get(sku) or []
    if len(arr) >= 2:
        s_arr = sorted(arr)
        p65 = s_arr[min(len(s_arr) - 1, int(len(s_arr) * 0.65))]
        mean_v = statistics.mean(s_arr)
        return int(round(0.6 * p65 + 0.4 * mean_v))
    return int(round(list_price * 0.95))


def queue_forum_action(market_dir: Path, payload: dict[str, Any]) -> None:
    """Append a post or vote JSON line to .market/outbox.jsonl."""
    market_dir.mkdir(exist_ok=True)
    outbox_path = market_dir / "outbox.jsonl"
    try:
        with open(outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
