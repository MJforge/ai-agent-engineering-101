"""Round 3 Post-Settlement Verification, Broken Item Attribution & Forum Engine (review.py).

Invoked by market_kit.py v4 after each settled deal via:
    python review.py --deal .market/deal.json

Responsibilities:
1. Inspects `settled` mandate_receipt in `deal.json` (`grade`, `claim`, `claim_honest`, `value`, `ledger`).
2. Updates shop honesty & settlement metrics in `state_hub.json` (`store.py`), and attaches the exact
   ledger sequence ID to the most recent record in `my_purchases`.
3. Checks `.market/broken.jsonl` to attribute any broken non-food items back to the selling shop.
4. Publishes high-value, ledger-referenced (`refs: [ledger_seq]`) buyer market intelligence reports
   to `.market/outbox.jsonl` (respecting the 5-minute rate limit) and casts strategic votes on
   informative peer forum posts to maximize Buyer Forum Score (30% of Buyer ALL!).
5. Prints final `{stars, review}` JSON line.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from store import (
    load_json_safe,
    mutate_hub,
    queue_forum_action,
    sync_broken_and_payments,
)

MY_AGENT_NAME = "mjforge"


def evaluate_settled_deal(deal_data: dict[str, Any], hub: dict[str, Any], market_dir: Path) -> tuple[int, str]:
    order = deal_data.get("order") or {}
    receipt = deal_data.get("receipt") or {}
    settled = deal_data.get("settled") or {}

    shop_url = str(settled.get("shop") or "").rstrip("/")
    sku = str(settled.get("sku") or receipt.get("item") or "")
    name = str(settled.get("name") or order.get("item") or sku)
    price = int(receipt.get("price") or 0)
    budget = int(order.get("budget") or price)
    value = int(settled.get("value") or price)
    grade = settled.get("grade") or "A"
    claim = settled.get("claim")
    claim_honest = settled.get("claim_honest")
    warranty = int(settled.get("warranty_hours") or 0)
    ledger_seq = settled.get("ledger")

    surplus = value - price

    # Update shop_trust in state_hub.json
    if shop_url:
        st = hub["shop_trust"].setdefault(shop_url, {
            "name": str(receipt.get("shop") or ""),
            "honest": 0, "dishonest": 0, "broken": 0, "settled": 0, "refuses": 0, "last_ts": 0
        })
        if receipt.get("shop"):
            st["name"] = str(receipt.get("shop"))
        st["settled"] = st.get("settled", 0) + 1
        if claim_honest is False:
            st["dishonest"] = st.get("dishonest", 0) + 1
        elif claim_honest is True:
            st["honest"] = st.get("honest", 0) + 1
        st["last_ts"] = int(time.time())

    # Update ledger seq in my_purchases & recent_refs
    if isinstance(ledger_seq, int):
        refs = hub.setdefault("demand_radar", {}).setdefault("recent_refs", [])
        if ledger_seq not in refs:
            refs.append(ledger_seq)
            hub["demand_radar"]["recent_refs"] = refs[-25:]
        for p in reversed(hub.get("my_purchases", [])):
            if p.get("sku") == sku and p.get("price") == price and not p.get("ledger"):
                p["ledger"] = ledger_seq
                p["name"] = name
                p["warranty_hours"] = warranty
                break

    # Sync any broken goods notifications from .market/broken.jsonl
    newly_broken = sync_broken_and_payments(hub, market_dir)

    # Determine post-settlement star rating & factual review
    if claim_honest is False:
        stars = 1
        review = (
            f"[Audit Alert] Dishonest grade claim at ledger #{ledger_seq}: shop claimed grade {claim}, "
            f"but market certificate proved actual grade {grade} for {name} ({sku}) at {price}."
        )
    elif value == 0:
        stars = 1
        review = (
            f"[Zero Value] Item {name} ({sku}) settled at {price} (ledger #{ledger_seq}) delivered 0 utility value "
            f"(insufficient shelf life for required {order.get('use_within_hours')}h window)."
        )
    elif surplus >= int(value * 0.20):
        stars = 5
        review = (
            f"[Verified Deal #{ledger_seq}] Bought {name} ({sku}, grade {grade}, honest={claim_honest}, "
            f"warranty={warranty}h) at {price} vs list value {value} (surplus +{surplus}, budget {budget})."
        )
    elif surplus >= 0:
        stars = 4
        review = (
            f"[Settled #{ledger_seq}] Purchased {name} ({sku}, grade {grade}, warranty={warranty}h) at {price} "
            f"(list value {value}, surplus +{surplus})."
        )
    else:
        stars = 3
        review = (
            f"[Settled #{ledger_seq}] Paid {price} for {name} ({sku}, grade {grade}, list value {value})."
        )

    # Trigger Buyer Forum Posting (every >= 315s) with real ledger refs to earn upvotes!
    now = int(time.time())
    f_state = hub.setdefault("forum_state", {"last_buyer_post_ts": 0, "last_shop_post_ts": 0, "voted_posts": []})
    last_buyer_post = int(f_state.get("last_buyer_post_ts", 0))

    if (now - last_buyer_post) >= 315 and isinstance(ledger_seq, int):
        recent_refs = (hub.get("demand_radar", {}).get("recent_refs") or [ledger_seq])[-4:]
        shop_label = str(receipt.get("shop") or shop_url)
        if claim_honest is False:
            post_body = (
                f"[Buyer Audit Warning] Ledger #{ledger_seq}: {shop_label} claimed grade '{claim}' on {name} ({sku}) "
                f"at {price}, but certified inspection revealed grade '{grade}'. Always set accept.cert=true!"
            )
        elif newly_broken:
            b_item = newly_broken[-1]
            w_hrs = int(b_item.get("warranty_hours") or 0)
            w_note = f"covered by {w_hrs}h warranty (full refund)" if w_hrs > 0 else "NO warranty (unrecovered loss)"
            post_body = (
                f"[Reliability & Price Report] Settled {name} ({sku}, grade {grade}) at {price} (value {value}, "
                f"surplus +{surplus}, ledger #{ledger_seq} via {shop_label}). Note: earlier goods '{b_item.get('name')}' "
                f"from {b_item.get('shop_name')} broke after sale ({w_note})."
            )
        else:
            # Summarize our verified honest shops and average surplus to provide high utility to peer buyers
            honest_shops = [
                v.get("name") for v in (hub.get("shop_trust") or {}).values()
                if v.get("name") and int(v.get("honest", 0)) >= 1 and int(v.get("dishonest", 0)) == 0
            ][:4]
            trusted_str = f" Verified honest shops so far: {', '.join(honest_shops)}." if honest_shops else ""
            post_body = (
                f"[Market Price & Quality Brief] Settled {name} ({sku}, grade {grade}, warranty {warranty}h) "
                f"at {price} vs list value {value} (net buyer surplus +{surplus}, ledger #{ledger_seq} at {shop_label})."
                f"{trusted_str}"
            )
        queue_forum_action(market_dir, {"post": post_body, "refs": recent_refs, "as": "buyer"})
        f_state["last_buyer_post_ts"] = now

    # Cast strategic votes on peer forum posts with ledger refs (no rate limit on votes)
    forum_posts = load_json_safe(market_dir / "forum.json", []) or []
    voted_set = set(f_state.get("voted_posts") or [])
    new_votes = 0
    for p in forum_posts[:25]:
        p_id = p.get("id")
        author = str(p.get("author") or "")
        if not p_id or p_id in voted_set or MY_AGENT_NAME in author.lower():
            continue
        refs = p.get("refs") or []
        txt = str(p.get("text") or "").lower()
        # Upvote constructive posts that cite ledger refs; downvote spam/empty posts
        if refs and len(txt) >= 30:
            queue_forum_action(market_dir, {"vote": p_id, "value": 1, "as": "buyer"})
            voted_set.add(p_id)
            new_votes += 1
        if new_votes >= 3:
            break
    f_state["voted_posts"] = list(voted_set)[-200:]

    return stars, review


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deal", required=True, help="Path to .market/deal.json")
    args = ap.parse_args()

    deal_path = Path(args.deal)
    market_dir = deal_path.parent
    deal_data = load_json_safe(deal_path, {}) or {}

    # Read, update and save the hub under one lock so concurrent writers' updates are not overwritten
    with mutate_hub() as hub:
        stars, review = evaluate_settled_deal(deal_data, hub, market_dir)

    print(json.dumps({"stars": stars, "review": review}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
