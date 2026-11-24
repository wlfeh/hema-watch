"""
HEMA store-stock watcher.

Checks per-store stock of one or more HEMA products and sends a phone notification
(via ntfy.sh) when a store you watch gets more stock (or less).

Usage:
  pip install requests
  python hema_watch.py                          # one check of all PRODUCT_URLS (cron / GitHub)
  python hema_watch.py --loop 30                # check every 30 minutes, forever
  python hema_watch.py --url PRODUCT_URL        # try any product: stock in your WATCH stores, nothing saved
  python hema_watch.py --list [--url URL]       # stores in the CENTERS areas with stock + storeIds
  python hema_watch.py --list-all [--url URL]   # sweep the whole country, show stores with stock
  python hema_watch.py --test-notify            # send test notifications
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

import requests

# ----------------------------- CONFIG ---------------------------------------
# Product pages to watch. The product number (SKU) is taken from the end of each URL.
# Add more URLs, one per line. Every product is checked in the same WATCH stores.
PRODUCT_URLS = [
    "https://www.hema.nl/wonen-slapen/slapen/dekbedovertrek/dekbedovertrek-240x200%2F220cm-flanel-lijnen-groen-5720438.html",
]

# Copied from the browser's network request (F12 > Network > Winkelvoorraad > "Copy as fetch").
# HEMA changes these when it publishes a new version of its site; you get a notification then.
# On GitHub you can update them without touching this file: Settings > Secrets and variables >
# Actions > Variables > HEMA_NEXT_ACTION and HEMA_DEPLOYMENT_ID (they win over the values below).
NEXT_ACTION = os.environ.get("HEMA_NEXT_ACTION") or "40432a920454a409c47c53aa01b68de558af2e79a3"
DEPLOYMENT_ID = os.environ.get("HEMA_DEPLOYMENT_ID") or "68c491c4198fb69e2dcfed69eb6fee6b95de183a"   # header x-deployment-id

# Areas to search: (name, latitude, longitude, radius, max stores). Radius is in the
# same unit the website uses (the site sends 50). Add or remove areas as you like.
CENTERS = [
    ("Den Haag centrum",    52.0778, 4.3136, 3, 15),
    ("Rotterdam Zuidplein", 51.8885, 4.4890, 4, 15),
    ("Bijlmer",             52.3139, 4.9459, 5, 15),
]

# storeId -> label. EDIT ME: run with --list to find the IDs you want.
WATCH = {
    "0035": "Den Haag-Centrum",
    "0059": "Rotterdam-Zuidplein",
    "2079": "Amsterdam Zuid-Oost (Bijlmer?)",   # check the address with --list
}

# Pick a long random topic name, then subscribe to it in the ntfy app (iOS/Android).
# On GitHub this comes from a repository secret (environment variable NTFY_TOPIC).
# Locally you can edit the text below instead.
PLACEHOLDER_TOPIC = "change-me-to-something-long-and-random"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or PLACEHOLDER_TOPIC

# Points used by --list-all to sweep the whole country (name, lat, lon).
NL_SWEEP = [
    ("Groningen", 53.2194, 6.5665), ("Leeuwarden", 53.2012, 5.7999), ("Emmen", 52.7792, 6.9069),
    ("Assen", 52.9929, 6.5642), ("Den Helder", 52.9563, 4.7608), ("Alkmaar", 52.6324, 4.7534),
    ("Hoorn", 52.6425, 5.0597), ("Zwolle", 52.5168, 6.0830), ("Lelystad", 52.5185, 5.4714),
    ("Zaandam", 52.4391, 4.8267), ("Haarlem", 52.3874, 4.6462), ("Amsterdam", 52.3676, 4.9041),
    ("Almere", 52.3508, 5.2647), ("Almelo", 52.3567, 6.6625), ("Enschede", 52.2215, 6.8937),
    ("Deventer", 52.2661, 6.1552), ("Hilversum", 52.2292, 5.1669), ("Apeldoorn", 52.2112, 5.9699),
    ("Amersfoort", 52.1561, 5.3878), ("Leiden", 52.1601, 4.4970), ("Utrecht", 52.0907, 5.1214),
    ("Gouda", 52.0116, 4.7104), ("Den Haag", 52.0705, 4.3007), ("Ede", 52.0402, 5.6649),
    ("Arnhem", 51.9851, 5.8987), ("Rotterdam", 51.9244, 4.4777), ("Nijmegen", 51.8126, 5.8372),
    ("Dordrecht", 51.8133, 4.6901), ("Den Bosch", 51.6978, 5.3037), ("Breda", 51.5719, 4.7683),
    ("Tilburg", 51.5555, 5.0913), ("Roosendaal", 51.5308, 4.4653), ("Middelburg", 51.4988, 3.6110),
    ("Eindhoven", 51.4416, 5.4697), ("Venlo", 51.3704, 6.1724), ("Roermond", 51.1942, 5.9870),
    ("Heerlen", 50.8882, 5.9795), ("Maastricht", 50.8514, 5.6910),
]

STATE_FILE = pathlib.Path(__file__).with_name("hema_state.json")
FAILS_BEFORE_ALERT = 3       # tell me if the tool fails this many runs in a row

# "Still running" messages: "off" = never (you only hear about stock changes),
# "every_run" = a quiet message after every check, "daily" = one per day, from the
# first check after SUMMARY_HOUR (local time).
STATUS_MESSAGES = "off"

# Notification loudness (ntfy priorities): "min" / "low" = silent, "default" = normal
# notification, "high" = louder, "urgent" = loudest.
STATUS_PRIORITY = "default"   # the "still running" message (if enabled above)
ALERT_PRIORITY = "high"       # stock went UP (the one you really want to notice)
DROP_PRIORITY = "low"         # stock went DOWN

TIMEZONE = "Europe/Amsterdam"
SUMMARY_HOUR = 8
# -----------------------------------------------------------------------------

def router_state(path):
    """The next-router-state-tree header for a product page path (without leading slash)."""
    tree = ["", {"children": [["locale", "nl-nl", "d", None], {"children": ["(shop)", {"children": [
        ["slug", path, "c", None], {"children": ["__PAGE__", {}, None, None, 0]}, None, None, 0]},
        None, None, 0]}, None, None, 0]}, None, None, 16]
    return quote(json.dumps(tree, separators=(",", ":")), safe="()")


def make_product(url):
    """Turn a product page URL into everything we need to query its stock."""
    u = urlparse(url.strip())
    m = re.search(r"-(\d+)\.html?$", u.path)
    if not m:
        raise SystemExit(f"Cannot find the product number (SKU) at the end of this URL: {url}")
    sku = m.group(1)
    clean = f"{u.scheme}://{u.netloc}{u.path}"
    slug = u.path.rsplit("/", 1)[-1]
    name = re.sub(r"-\d+\.html?$", "", unquote(slug)).replace("-", " ")
    return {
        "sku": sku, "url": clean, "name": name,
        "headers": {
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-action": NEXT_ACTION,
            "x-deployment-id": DEPLOYMENT_ID,
            "next-router-state-tree": router_state(u.path.lstrip("/")),
            "origin": f"{u.scheme}://{u.netloc}",
            "referer": clean,
            "user-agent": "Mozilla/5.0 (personal stock watcher)",
        },
    }


class SiteChanged(RuntimeError):
    """HEMA no longer accepts our next-action / x-deployment-id values."""


def query_area(prod, lat, lon, radius, limit):
    """One request to HEMA's store-stock endpoint; returns the list of store items."""
    body = json.dumps([{
        "countryCode": "NL",
        "limit": limit,
        "statuses": ["open", "renovation"],
        "geoPointLocation": {"latitude": lat, "longitude": lon, "radius": radius},
        "skus": [prod["sku"]],
        "excludeEmptyStocks": False,
    }])
    r = requests.post(prod["url"], data=body, headers=prod["headers"], timeout=30)
    r.encoding = "utf-8"
    if r.status_code >= 400:
        # Show what the server said, so a failure can be diagnosed from the log.
        keep = {k: v for k, v in r.headers.items()
                if k.lower() in ("content-type", "x-nextjs-cache", "x-action-not-found",
                                 "x-vercel-id", "server", "retry-after", "cf-ray", "via")}
        msg = (f"HTTP {r.status_code} {r.reason}. Headers: {keep}. Body: {r.text[:400]!r}")
        if "Server Action" in r.text or r.status_code in (404, 409):
            raise SiteChanged(msg)
        raise RuntimeError(msg)
    text = r.text
    # The JSON we want follows a base64 blob on the same line, so find it by marker.
    i = text.find('1:{"data"')
    if i < 0:
        raise ValueError("Unexpected response: " + text[:200])
    obj, _ = json.JSONDecoder().raw_decode(text, i + 2)
    if obj.get("error") or not obj.get("data"):
        raise ValueError("API error: " + str(obj.get("error")))
    return obj["data"]["items"]


def stock_of(item, sku):
    return sum(x.get("sellableStock", 0) for x in item.get("stocks", []) if str(x.get("sku")) == sku)


def fetch_stores(prod):
    """Return {storeId: {name, qty, address}} for all stores in all CENTERS."""
    stores = {}
    for _, lat, lon, radius, limit in CENTERS:
        for it in query_area(prod, lat, lon, radius, limit):
            addr = it.get("storeAddresses", {}).get("visitingAddress", {}).get("address", "")
            stores[it["storeId"]] = {"name": it["name"], "qty": stock_of(it, prod["sku"]),
                                     "address": addr}
    return stores


def notify(title, message, priority="high", click=None):
    if NTFY_TOPIC == PLACEHOLDER_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not set (still the placeholder)")
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title.encode("latin-1", "replace").decode("latin-1"),
                 "Priority": priority, "Tags": "bed", "Click": click or PRODUCT_URLS[0]},
        timeout=15,
    ).raise_for_status()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"qty": {}, "fails": 0, "broken_notified": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:  # no timezone database available: fall back to UTC
        return datetime.now(timezone.utc)


def check_once():
    state = load_state()
    products = [make_product(u) for u in PRODUCT_URLS]
    try:
        results = [(p, fetch_stores(p)) for p in products]
    except SiteChanged as e:
        print(f"[error] {e}", file=sys.stderr)
        if not state.get("site_changed_notified"):
            notify("HEMA watcher: site changed, update needed",
                   "HEMA changed its website, so the stock check no longer works and "
                   "NOTHING IS BEING WATCHED. The next-action and x-deployment-id values "
                   "need to be refreshed from the browser (GitHub variables "
                   "HEMA_NEXT_ACTION and HEMA_DEPLOYMENT_ID).\n"
                   f"Details: {e}", "high")
            state["site_changed_notified"] = True
        save_state(state)
        return False
    except Exception as e:  # network error, temporary glitch, etc.
        state["fails"] = state.get("fails", 0) + 1
        print(f"[error] {e}", file=sys.stderr)
        if state["fails"] >= FAILS_BEFORE_ALERT and not state.get("broken_notified"):
            notify("HEMA watcher is broken",
                   f"The stock check failed {state['fails']} times in a row, so you may miss "
                   f"alerts until it is fixed.\nLast error: {e}", "default")
            state["broken_notified"] = True
        save_state(state)
        return False
    state["fails"] = 0
    state["broken_notified"] = False
    state["site_changed_notified"] = False

    prev = state.get("qty", {})
    # Older state files used plain storeIds (single product): move them to "sku:storeId".
    for k in [k for k in prev if ":" not in k]:
        prev[f"{products[0]['sku']}:{k}"] = prev.pop(k)
    first_run = not prev
    any_change, status_parts = False, []

    for prod, stores in results:
        ups, downs = [], []
        for sid, label in WATCH.items():
            s = stores.get(sid)
            if s is None:
                print(f"[warn] store {sid} ({label}) not in results; check CENTERS (radius/limit)?")
                continue
            key = f"{prod['sku']}:{sid}"
            qty, old = s["qty"], prev.get(key)
            print(f"{prod['name'][:28]:<28} {s['name']:<28} {qty} in stock"
                  + ("" if old is None else f" (was {old})"))
            status_parts.append(f"{prod['name'][:30]} / {s['name']}: {qty}")
            if old is not None and qty != old:
                (ups if qty > old else downs).append((s, old, qty))
            prev[key] = qty

        # 1) Stock changed: tell me right away.
        if ups or downs:
            any_change = True
            lines = [f"{s['name']} ({s['address']}): from {old} to {qty}"
                     for s, old, qty in ups + downs]
            short = prod["name"][:40]
            if ups:
                title = f"HEMA {short}: more stock at " + ", ".join(s["name"] for s, _, _ in ups)
                tail = "\n\nTip: phone the store first, there are only a few units."
                priority = ALERT_PRIORITY
            else:
                title = f"HEMA {short}: stock went down at " + ", ".join(s["name"] for s, _, _ in downs)
                tail = ""
                priority = DROP_PRIORITY
            notify(title, f"Stock has changed!\n{prod['name']}\n" + "\n".join(lines)
                   + "\n\nCurrent stock - " + "; ".join(status_parts) + tail,
                   priority, click=prod["url"])
            state["changed_since_summary"] = True
            state["qty"] = prev
            save_state(state)

    state["qty"] = prev
    status = "Current stock - " + "; ".join(status_parts)

    # 2) Confirm the watcher is alive.
    if STATUS_MESSAGES == "every_run":
        # After every check. If stock changed, the alert above already said so.
        if not any_change:
            head = ("Still running! First check recorded." if first_run
                    else "Still running! Stock has not changed.")
            notify("HEMA watcher: still running", head + "\n" + status, STATUS_PRIORITY)
    elif STATUS_MESSAGES == "daily":
        # Once a day, from the first check after SUMMARY_HOUR.
        now = local_now()
        today = now.date().isoformat()
        if now.hour >= SUMMARY_HOUR and state.get("last_summary") != today:
            if state.get("changed_since_summary"):
                head = "Still running. Stock changed since the last daily message (you were alerted)."
            else:
                head = "Still running! Stock has not changed."
            notify("HEMA watcher: still running", head + "\n" + status, STATUS_PRIORITY)
            state["last_summary"] = today
            state["changed_since_summary"] = False

    save_state(state)
    return True


def dry_run(prod):
    """Show current stock of one product in the WATCH stores. Sends and saves nothing."""
    print(f"{prod['name']}  (SKU {prod['sku']})\n")
    stores = fetch_stores(prod)
    for sid, label in WATCH.items():
        s = stores.get(sid)
        print(f"{(s['name'] if s else label):<32} " + (f"{s['qty']} in stock" if s else "not found in CENTERS"))
    total = sum(1 for s in stores.values() if s["qty"] > 0)
    print(f"\n{len(stores)} stores searched in your CENTERS areas, {total} with stock.")


def list_stores(prod):
    print(f"{prod['name']}  (SKU {prod['sku']})")
    stores = fetch_stores(prod)
    print(f"{len(stores)} stores found\n")
    for sid, s in stores.items():
        mark = "*" if sid in WATCH else " "
        print(f"{mark} {sid}  {s['name']:<32} stock: {s['qty']}  {s['address']}")
    print("\n* = currently in WATCH")


def list_all(prod, show_empty=False):
    """Sweep the whole country and print every store that has the product."""
    found, capped = {}, []
    for name, lat, lon in NL_SWEEP:
        try:
            items = query_area(prod, lat, lon, 25, 60)
        except Exception as e:
            print(f"[warn] {name}: {e}", file=sys.stderr)
            continue
        if len(items) >= 60:
            capped.append(name)   # hit the per-request cap, so some stores here may be missing
        for it in items:
            addr = it.get("storeAddresses", {}).get("visitingAddress", {})
            found[it["storeId"]] = (it["name"], stock_of(it, prod["sku"]), addr.get("address", ""))
        time.sleep(1)   # be gentle with the server
    with_stock = sorted((v for v in found.values() if v[1] > 0), key=lambda v: v[0])
    print(f"{prod['name']}  (SKU {prod['sku']})\n{len(found)} stores checked, {len(with_stock)} with stock\n")
    rows = sorted(found.values(), key=lambda v: v[0]) if show_empty else with_stock
    for name, qty, addr in rows:
        print(f"{name:<38} stock: {qty:<3} {addr}")
    if capped:
        print("\nNote: these areas hit the per-request cap, so some stores may be missing: "
              + ", ".join(capped))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="product page URL (for --list, --list-all, or a one-off check)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--list-all", action="store_true",
                    help="sweep the whole country, show stores with stock")
    ap.add_argument("--show-empty", action="store_true",
                    help="with --list-all: also show stores without stock")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--loop", type=int, metavar="MINUTES")
    a = ap.parse_args()
    prod = make_product(a.url or PRODUCT_URLS[0])

    if a.test_notify:
        # One sample of each message type, at the priorities you configured above.
        notify("HEMA TEST 1/3: still running",
               "TEST of the 'still running' message. Not real.", STATUS_PRIORITY)
        time.sleep(2)
        notify("HEMA TEST 2/3: more stock at Den Haag-Centrum",
               "TEST of the restock alert. Not real: stock has NOT changed.", ALERT_PRIORITY)
        time.sleep(2)
        notify("HEMA TEST 3/3: stock went down",
               "TEST of the stock-went-down message. Not real.", DROP_PRIORITY)
    elif a.list_all:
        list_all(prod, a.show_empty)
    elif a.list:
        list_stores(prod)
    elif a.url:
        dry_run(prod)
    elif a.loop:
        while True:
            check_once()
            time.sleep(a.loop * 60)
    else:
        sys.exit(0 if check_once() else 1)   # non-zero = red X in GitHub Actions


if __name__ == "__main__":
    main()
