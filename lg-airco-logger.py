#!/usr/bin/env python3
"""Raw logger for an LG ThinQ air conditioner, through LG's official ThinQ
Connect API (the calls of LG's own SDK, github.com/thinq-connect/pythinqconnect).

Every answer from LG is stored unchanged, one JSON line per request, in daily
files <logdir>/YYYY/YYYYMMDD.jsonl (UTC), so the data can be interpreted again
later:

  {"t": "2026-11-06T07:55:35.123Z", "kind": "state", "path": "devices/<id>/state",
   "http": 200, "ms": 674, "body": {... LG's answer as it came ...}}

kinds: state (every --interval seconds), energy_today (every --energy-interval
seconds), devices, profile, energy_profile, route, energy (daily), error (a
request that failed: no answer, or not 200).

What it asks for:
  every --interval s   GET devices/<id>/state
  every --energy-interval s
                       the energy of yesterday and today (DAILY): LG keeps
                       today's total up to date during the day, so the
                       differences give the energy per interval; yesterday too,
                       so nothing is lost around midnight, whichever timezone
                       LG counts its days in
  at start, then daily GET devices, devices/<id>/profile, devices/energy/<id>/profile,
                       route, and the energy per day (devices/energy/<id>/usage,
                       DAILY): at start every month LG still keeps (about 12),
                       then daily the last 8 days
On errors it waits longer before the next request (twice as long each time,
up to 30 minutes) and logs each failure.

The token (a ThinQ Connect personal access token, read-only scopes are
enough) is read from $CREDENTIALS_DIRECTORY/token (systemd LoadCredential) or
--token-file; it is never logged. Standard library only.
"""
import argparse
import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_KEY = "v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3"   # public, fixed in LG's SDK (const.py)
MAX_BACKOFF = 1800
DAILY_AT = 8 * 3600 + 30 * 60      # seconds after local midnight: LG updates yesterday's energy in the morning
stop = False


def on_term(*_):
    global stop
    stop = True


class Logger:
    def __init__(self, a):
        self.base = a.api.rstrip("/")
        self.country = a.country
        self.logdir = Path(a.logdir)
        self.token = self.read_token(a.token_file)
        cid = Path(a.client_id_file)
        if not cid.exists():
            cid.parent.mkdir(parents=True, exist_ok=True)
            cid.write_text(f"lg-airco-logger-{uuid.uuid4()}\n")
        self.client_id = cid.read_text().strip()

    @staticmethod
    def read_token(token_file):
        cred = os.environ.get("CREDENTIALS_DIRECTORY")
        p = Path(cred) / "token" if cred and (Path(cred) / "token").exists() else Path(token_file).expanduser()
        token = p.read_text().strip()
        if not token:
            sys.exit(f"no token in {p}")
        return token

    def write(self, rec):
        now = datetime.now(timezone.utc)
        folder = self.logdir / f"{now:%Y}"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"{now:%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    def get(self, kind, path):
        """One request; the answer is logged as it came. Returns the parsed body or None."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "x-country": self.country,
            "x-message-id": base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2].decode(),
            "x-client-id": self.client_id,
            "x-api-key": API_KEY,
            "x-service-phase": "OP",
        }
        t0 = time.time()
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rec = {"t": stamp, "kind": kind, "path": path}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{self.base}/{path}", headers=headers), timeout=30) as r:
                raw = r.read().decode("utf-8", errors="replace")
                rec.update(http=r.status, ms=round((time.time() - t0) * 1000))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            rec.update(kind="error", request=kind, http=e.code, ms=round((time.time() - t0) * 1000))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            rec.update(kind="error", request=kind, http=None, ms=round((time.time() - t0) * 1000), reason=str(e))
            self.write(rec)
            return None
        try:
            rec["body"] = json.loads(raw)
        except ValueError:
            rec["body_text"] = raw[:2000]
        self.write(rec)
        return rec.get("body") if rec.get("http") == 200 else None

    def find_device(self, wanted):
        body = self.get("devices", "devices")
        if body is None:
            return None
        devices = body.get("response") or []
        if wanted:
            return wanted if any(d.get("deviceId") == wanted for d in devices) else None
        acs = [d["deviceId"] for d in devices if d.get("deviceInfo", {}).get("deviceType") == "DEVICE_AIR_CONDITIONER"]
        return acs[0] if len(acs) == 1 else None

    def daily(self, dev, first_run):
        """Device list, profiles, route and the energy per day; True if all answered."""
        ok = self.get("devices", "devices") is not None
        for kind, path in (("profile", f"devices/{dev}/profile"), ("energy_profile", f"devices/energy/{dev}/profile"),
                           ("route", "route")):
            ok = self.get(kind, path) is not None and ok
        end = date.today() - timedelta(days=1)
        start = date(end.year - 1, end.month, 1) if first_run else end - timedelta(days=7)
        a = start
        while a <= end:                                     # one request per month, as LG's SDK does
            nxt = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
            b = min(end, nxt - timedelta(days=1))
            self.get("energy", f"devices/energy/{dev}/usage?property=energyUsage&period=DAILY"
                               f"&startDate={a:%Y%m%d}&endDate={b:%Y%m%d}")   # older months are refused (1102): logged, fine
            a = nxt
            time.sleep(1)
        return ok

    def energy_today(self, dev):
        """Yesterday's and today's energy so far (local dates)."""
        today = date.today()
        return self.get("energy_today", f"devices/energy/{dev}/usage?property=energyUsage&period=DAILY"
                                        f"&startDate={today - timedelta(days=1):%Y%m%d}&endDate={today:%Y%m%d}")


def next_daily(now):
    """The next local DAILY_AT moment after now (epoch seconds)."""
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    t = midnight + DAILY_AT
    return t if t > now else time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)) + DAILY_AT


def main():
    ap = argparse.ArgumentParser(description="Raw logger for an LG ThinQ air conditioner (ThinQ Connect API)")
    ap.add_argument("--logdir", required=True, help="where the daily .jsonl files go")
    ap.add_argument("--interval", type=float, default=30, help="seconds between status requests (default 30)")
    ap.add_argument("--energy-interval", type=float, default=300,
                    help="seconds between requests for today's energy so far (default 300; 0 = off)")
    ap.add_argument("--device", help="device id (default: the only air conditioner on the account)")
    ap.add_argument("--token-file", default="~/.config/lg-thinq/token", help="used when systemd gives no credential")
    ap.add_argument("--client-id-file", default="client-id", help="random client id, kept between runs")
    ap.add_argument("--country", default="NL")
    ap.add_argument("--api", default="https://api-eic.lgthinq.com", help="LG's region (EIC for Europe)")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    lg = Logger(a)

    fails = 0
    dev = None
    while not stop and dev is None:                         # the device list, with backoff
        dev = lg.find_device(a.device)
        if dev is None:
            fails += 1
            time.sleep(min(MAX_BACKOFF, a.interval * 2 ** fails))
    if stop:
        return
    print(f"logging device {dev} every {a.interval:g} s to {a.logdir}", flush=True)
    lg.daily(dev, first_run=True)
    due_daily = next_daily(time.time())
    due_energy = time.time()
    fails = 0
    next_t = time.time()
    while not stop:
        now = time.time()
        if now < next_t:
            time.sleep(min(1.0, next_t - now))
            continue
        if now >= due_daily:
            lg.daily(dev, first_run=False)
            due_daily = next_daily(time.time())
        if a.energy_interval > 0 and now >= due_energy and fails == 0:
            lg.energy_today(dev)                            # a failure here is logged; the status request decides the backoff
            due_energy = max(due_energy + a.energy_interval, time.time())
        if lg.get("state", f"devices/{dev}/state") is None:
            fails += 1
            wait = min(MAX_BACKOFF, a.interval * 2 ** fails)
            print(f"status request failed ({fails} in a row); next try in {wait:.0f} s", flush=True)
        else:
            fails = 0
            wait = a.interval
        next_t = max(next_t + a.interval, time.time()) if fails == 0 else time.time() + wait


if __name__ == "__main__":
    main()
