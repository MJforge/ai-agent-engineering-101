"""Own runtime adapter for the read-only v4 market kit. No sidecar joins."""
from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
import time
from pathlib import Path

import httpx

from visibility_guard import RuntimeLock, VisibilityGuard, atomic_json, own_sale, paused, read_json


def compatible(kit):
    expected = [(kit.Market.join, ["self", "role", "name", "key", "url", "tries"]),
                (kit.Market.call, ["self", "method", "path", "role", "kw"]),
                (kit.watchdog, ["a", "agent", "d", "market", "keys", "procs", "proxy", "sport", "pport", "state", "every"]),
                (kit.cmd_join, ["a"])]
    return kit.KIT_VERSION == "4" and all(list(inspect.signature(f).parameters) == names for f, names in expected)


def install(kit, directory, *, guard_factory=VisibilityGuard):
    if not compatible(kit):
        raise RuntimeError("guard_incompatible_kit")
    original_market, original_watchdog = kit.Market, kit.watchdog
    directory = Path(directory)

    class GuardedMarket(original_market):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.guard_lock = threading.RLock()
            self.visibility_guard = None
            self.current_url = None
            self.restore_window = None
            self.restore_skus = set()

        def join(self, role, name, key, url=None, tries=6):
            if role != "shop":
                return super().join(role, name, key, url, tries=tries)
            with self.guard_lock:
                # A watchdog can have rotated the proxy before its join failed.
                # Never use joined['shop']'s stale URL for subsequent recovery.
                if self.current_url is not None:
                    if url != self.current_url():
                        raise SystemExit("guard_url_changed")
                try:
                    result = super().join(role, name, key, url, tries=tries)
                except (Exception, SystemExit):
                    raise SystemExit("guard_shop_join_failed") from None
                if self.visibility_guard is None:
                    self.current_url = lambda: url
                    self.visibility_guard = guard_factory(self, directory / ".market", name, lambda: self.current_url())
                    threading.Thread(target=self.visibility_guard.run, daemon=True).start()
                else:
                    self.visibility_guard.good = self.visibility_guard.bad = 0
                    self.visibility_guard.publish("rejoining", "confirmation_pending", url)
                return result

        def supply_allowed(self, body):
            now = time.time()
            if not paused(read_json(directory / ".market" / "visibility_guard.json"), now):
                return True
            md = directory / ".market"
            try:
                if not 0 <= now - (md / "inventory.json").stat().st_mtime <= 120:
                    return False
                inv = read_json(md / "inventory.json")
                if not isinstance(inv.get("units"), list):
                    return False
                window = inv["restock"]
                opens, closes = float(window["opens_at"]), float(window["closes_at"])
                if not opens <= now < closes:
                    return False
                if not isinstance(body, dict):
                    return False
                sku = body.get("sku")
                if (not isinstance(sku, str) or type(body.get("qty")) is not int or body["qty"] != 1
                        or any(not isinstance(u, dict) or (u.get("sku") == sku and u.get("grade") != "expired") for u in inv["units"])
                        or not own_sale(read_json(directory / "state_hub.json"), sku, now)):
                    return False
                window_id = [opens, closes]
                # Persist before the POST: timeouts and restarts cannot duplicate a
                # conservative one-unit restoration in the same actual window.
                path = md / "visibility_supply_reservations.json"
                reserved = read_json(path)
                if path.exists() and (not isinstance(reserved.get("window"), list)
                                      or not isinstance(reserved.get("skus"), list)):
                    return False
                if reserved.get("window") != window_id:
                    reserved = {"window": window_id, "skus": []}
                if not isinstance(reserved.get("skus"), list) or sku in reserved["skus"] or len(reserved["skus"]) >= 500:
                    return False
                reserved["skus"].append(sku)
                atomic_json(path, reserved)
                return True
            except (OSError, ValueError, TypeError, KeyError):
                return False

        def call(self, method, path, role="buyer", **kw):
            if role != "shop":
                return super().call(method, path, role, **kw)
            with self.guard_lock:
                if method.upper() == "POST" and path == "/api/supply" and not self.supply_allowed(kw.get("json") or {}):
                    return httpx.Response(409, json={"error": "visibility_depth_hold"})
                response = super().call(method, path, role, **kw)
                if method.upper() == "GET" and path == "/api/inventory" and response.status_code == 401 and self.visibility_guard:
                    self.visibility_guard.auth_failed = True
                    self.visibility_guard.good = 0
                    self.visibility_guard.publish("unknown", "inventory_401", self.current_url())
                return response

    def watchdog(a, agent, d, market, keys, procs, proxy, sport, pport, state, every=60):
        class LockedProxy:
            # The original watchdog's rotation must share the shop auth lock,
            # too, or it could change the URL during an in-flight guard join.
            def __getattr__(self, name):
                return getattr(proxy, name)

            @property
            def public(self):
                return proxy.public

            @public.setter
            def public(self, url):
                with market.guard_lock:
                    proxy.public = url
                    if market.visibility_guard:
                        market.visibility_guard.good = market.visibility_guard.bad = 0
                        market.visibility_guard.publish("unknown", "url_changed", url)

        with market.guard_lock:
            market.current_url = lambda: proxy.public
        return original_watchdog(a, agent, d, market, keys, procs, LockedProxy(), sport, pport, state, every)

    kit.Market, kit.watchdog = GuardedMarket, watchdog
    return GuardedMarket


def run_join(kit, original, a):
    directory = Path(a.dir).resolve()
    # Existence only: the shared kit alone reads existing private keys.
    if any(not (directory / name).is_file() for name in ("shop_key.pem", "buyer_key.pem")):
        raise SystemExit("guard_existing_keys_required")
    if a.buyer_only:
        return original(a)
    with RuntimeLock(directory / ".market"):
        if not compatible(kit):
            atomic_json(directory / ".market" / "visibility_guard.json",
                        {"version": 1, "checked_at": time.time(), "state": "unknown", "reason": "incompatible_kit"})
            raise SystemExit("guard_incompatible_kit")
        install(kit, directory)
        return original(a)


def main():
    path = Path(__file__).resolve().parents[3] / "scripts" / "market_kit.py"
    spec = importlib.util.spec_from_file_location("_shared_market_kit", path)
    kit = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = kit
    spec.loader.exec_module(kit)
    # Keep the shared argparse and all buyer behavior unchanged.
    original = kit.cmd_join

    def cmd_join(a):
        return run_join(kit, original, a)

    kit.cmd_join = cmd_join
    # Compatibility checks require the original function's exact signature.
    kit.main()


if __name__ == "__main__":
    main()
