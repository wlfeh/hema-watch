#!/usr/bin/env python3
"""
HEMA store-stock watcher.

Checks per-store stock of one HEMA product and sends a phone notification
(via ntfy.sh) when a store you watch gets stock, or gets more stock.

Usage:
  pip install requests
  python hema_watch.py --list              # show stores in the CENTERS areas with stock + storeIds
  python hema_watch.py --test-notify       # send a test notification
  python hema_watch.py                     # one check (use with cron / Task Scheduler)
  python hema_watch.py --loop 30           # check every 30 minutes, forever
"""
import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

import requests

# ----------------------------- CONFIG ---------------------------------------
PRODUCT_NAME = "HEMA dekbedovertrek flanel lijnen groen 240x200/220"
PRODUCT_URL = ("https://www.hema.nl/wonen-slapen/slapen/dekbedovertrek/"
               "dekbedovertrek-240x200%2F220cm-flanel-lijnen-groen-5720438.html")
SKU = "5720438"

# Copied from the browser's network request. These can change when HEMA
# deploys a new version of its site (see "If it stops working" notes).
NEXT_ACTION = "40c432e06a4538ce43bbf3e88ff3da48a035ab11fb"
ROUTER_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22nl-nl%22%2C%22d%22%2Cnull%5D%2C"
    "%7B%22children%22%3A%5B%22(shop)%22%2C%7B%22children%22%3A%5B%5B%22slug%22%2C%22wonen-slapen"
    "%2Fslapen%2Fdekbedovertrek%2Fdekbedovertrek-240x200%252F220cm-flanel-lijnen-groen-5720438.html"
    "%22%2C%22c%22%2Cnull%5D%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%2C0%5D%7D"
    "%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C16%5D"
)

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

STATE_FILE = pathlib.Path(__file__).with_name("hema_state.json")
FAILS_BEFORE_ALERT = 3       # tell me if the tool fails this many runs in a row

# "Still running" message. True = send one after EVERY check (quiet, no sound).
# False = send only one per day, from the first check after SUMMARY_HOUR (local time).
STATUS_EVERY_RUN = True
# Notification loudness (ntfy priorities): "min" / "low" = silent, "default" = normal
# notification, "high" = louder, "urgent" = loudest.
STATUS_PRIORITY = "default"   # the "still running" message
ALERT_PRIORITY = "high"       # stock went UP (the one you really want to notice)
DROP_PRIORITY = "low"         # stock went DOWN
TIMEZONE = "Europe/Amsterdam"
SUMMARY_HOUR = 8
# -----------------------------------------------------------------------------

HEADERS = {
    "accept": "text/x-component",
    "content-type": "text/plain;charset=UTF-8",
    "next-action": NEXT_ACTION,
    "next-router-state-tree": ROUTER_STATE,
    "origin": "https://www.hema.nl",
    "referer": PRODUCT_URL,
    "user-agent": "Mozilla/5.0 (personal stock watcher)",
}


def query_area(lat, lon, radius, limit):
    """One request to HEMA's store-stock endpoint; returns the list of store items."""
    body = json.dumps([{
        "countryCode": "NL",
        "limit": limit,
        "statuses": ["open", "renovation"],
        "geoPointLocation": {"latitude": lat, "longitude": lon, "radius": radius},
        "skus": [SKU],
        "excludeEmptyStocks": False,
    }])
    r = requests.post(PRODUCT_URL, data=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    text = r.text
    # The JSON we want follows a base64 blob on the same line, so find it by marker.
    i = text.find('1:{"data"')
    if i < 0:
        raise ValueError("Unexpected response: " + text[:200])
    obj, _ = json.JSONDecoder().raw_decode(text, i + 2)
    if obj.get("error") or not obj.get("data"):
        raise ValueError("API error: " + str(obj.get("error")))
    return obj["data"]["items"]


def fetch_stores():
    """Return {storeId: {name, qty, address}} for all stores in all CENTERS."""
    stores = {}
    for _, lat, lon, radius, limit in CENTERS:
        for it in query_area(lat, lon, radius, limit):
            qty = sum(s.get("sellableStock", 0) for s in it.get("stocks", [])
                      if str(s.get("sku")) == SKU)
            addr = it.get("storeAddresses", {}).get("visitingAddress", {}).get("address", "")
            stores[it["storeId"]] = {"name": it["name"], "qty": qty, "address": addr}
    return stores


def notify(title, message, priority="high"):
    if NTFY_TOPIC == PLACEHOLDER_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not set (still the placeholder)")
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title.encode("latin-1", "replace").decode("latin-1"),
                 "Priority": priority, "Tags": "bed", "Click": PRODUCT_URL},
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
    try:
        stores = fetch_stores()
    except Exception as e:  # network error, HEMA changed something, etc.
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

    prev = state.get("qty", {})
    first_run = not prev
    ups, downs, status_parts = [], [], []
    for sid, label in WATCH.items():
        s = stores.get(sid)
        if s is None:
            print(f"[warn] store {sid} ({label}) not in results; check CENTERS (radius/limit)?")
            continue
        qty, old = s["qty"], prev.get(sid)
        print(f"{s['name']:<35} {qty} in stock" + ("" if old is None else f" (was {old})"))
        status_parts.append(f"{s['name']}: {qty}")
        if old is not None and qty != old:
            (ups if qty > old else downs).append((s, old, qty))
        prev[sid] = qty
    state["qty"] = prev
    status = "Current stock - " + "; ".join(status_parts)

    # 1) Stock changed: tell me right away.
    if ups or downs:
        lines = [f"{s['name']} ({s['address']}): from {old} to {qty}"
                 for s, old, qty in ups + downs]
        if ups:
            title = "HEMA blanket: more stock at " + ", ".join(s["name"] for s, _, _ in ups)
            tail = "\n\nTip: phone the store first, there are only a few units."
            priority = ALERT_PRIORITY
        else:
            title = "HEMA blanket: stock went down at " + ", ".join(s["name"] for s, _, _ in downs)
            tail = ""
            priority = DROP_PRIORITY
        notify(title, f"Stock has changed!\n{PRODUCT_NAME}\n" + "\n".join(lines)
               + "\n\n" + status + tail, priority)
        state["changed_since_summary"] = True
        save_state(state)

    # 2) Confirm the watcher is alive.
    if STATUS_EVERY_RUN:
        # After every check. If stock changed, the alert above already said so.
        if not (ups or downs):
            head = ("Still running! First check recorded." if first_run
                    else "Still running! Stock has not changed.")
            notify("HEMA watcher: still running", head + "\n" + status, STATUS_PRIORITY)
    else:
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


def list_stores():
    stores = fetch_stores()
    print(f"{len(stores)} stores found\n")
    for sid, s in stores.items():
        mark = "*" if sid in WATCH else " "
        print(f"{mark} {sid}  {s['name']:<32} stock: {s['qty']}  {s['address']}")
    print("\n* = currently in WATCH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--loop", type=int, metavar="MINUTES")
    a = ap.parse_args()

    if a.test_notify:
        notify("HEMA watcher test", "If you see this, notifications work.", "default")
    elif a.list:
        list_stores()
    elif a.loop:
        while True:
            check_once()
            time.sleep(a.loop * 60)
    else:
        sys.exit(0 if check_once() else 1)   # non-zero = red X in GitHub Actions


if __name__ == "__main__":
    main()
