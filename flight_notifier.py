#!/usr/bin/env python3
"""
Flight Notifier - tells your family when your flight takes off and lands.

Watches live ADS-B data (free community feeds, no API key) for your aircraft
and sends push (ntfy) and/or SMS (Twilio) messages with a tracking link.

Only uses the Python standard library (3.8+).

Quick examples
--------------
  # Track a flight (settings come from config.json + environment variables)
  python3 flight_notifier.py --reg VH-ZNA --flight QF9

  # Custom thresholds
  python3 flight_notifier.py --reg VH-ZNA --flight QF9 --takeoff-alt 1500 --landed-alt 300

  # Rehearse a whole flight with fake data (sends REAL test notifications)
  python3 flight_notifier.py --reg VH-TEST --flight QF123 --simulate

  # Just send one test message to everyone
  python3 flight_notifier.py --test-notify
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Defaults (override in config.json, environment variables, or CLI flags)
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "takeoff_alt": 1000,        # ft - above this (2 readings in a row) = "taken off"
    "landed_alt": 200,          # ft - below this AND slow (or "ground") = "landed"
    "landed_speed": 80,         # kt - ground speed must be under this to count as landed
    "confirm_readings": 2,      # consecutive readings needed before notifying
    "poll_seconds": 30,         # how often to check
    "max_wait_hours": 12,       # give up if you haven't taken off after this long
    "max_flight_hours": 20,     # give up if you haven't landed after this long
    "lost_signal_minutes": 15,  # if signal disappears LOW (see next line) for this long -> landed
    "lost_signal_below_alt": 3000,
    "tracking_link": "flightaware",   # flightaware | fr24 | adsblol | adsbexchange | custom template
    "your_name": "Me",
    "notify_start": True,       # send a "tracking started" message with the link
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "twilio_sid": "",
    "twilio_token": "",
    "twilio_from": "",
    "sms_to": "",               # comma separated, e.g. "+61400111222,+61400333444"
}

ENV_MAP = {  # environment variable -> setting (handy for secrets / GitHub Actions)
    "NTFY_SERVER": "ntfy_server",
    "NTFY_TOPIC": "ntfy_topic",
    "TWILIO_SID": "twilio_sid",
    "TWILIO_TOKEN": "twilio_token",
    "TWILIO_FROM": "twilio_from",
    "SMS_TO": "sms_to",
    "YOUR_NAME": "your_name",
    "TRACKING_LINK": "tracking_link",
}

# Free ADS-B feeds that share the same v2 JSON format. Tried in order.
FEEDS = [
    ("adsb.lol", "https://api.adsb.lol/v2/reg/{reg}", "https://api.adsb.lol/v2/callsign/{cs}"),
    ("airplanes.live", "https://api.airplanes.live/v2/reg/{reg}", "https://api.airplanes.live/v2/callsign/{cs}"),
]

# Common airline IATA (2-letter, what's on the ticket) -> ICAO (3-letter, what ADS-B broadcasts).
# If yours is missing, just enter the flight as the ICAO callsign, e.g. "QFA9".
IATA_TO_ICAO = {
    "QF": "QFA", "VA": "VOZ", "JQ": "JST", "ZL": "RXA", "TT": "TGW", "QQ": "UTY", "NZ": "ANZ",
    "3K": "JSA", "GK": "JJP", "SQ": "SIA", "TR": "TGW", "MI": "SLK", "CX": "CPA", "UO": "HKE",
    "EK": "UAE", "EY": "ETD", "QR": "QTR", "BA": "BAW", "VS": "VIR", "AA": "AAL", "UA": "UAL",
    "DL": "DAL", "AS": "ASA", "HA": "HAL", "WN": "SWA", "B6": "JBU", "AC": "ACA", "WS": "WJA",
    "NH": "ANA", "JL": "JAL", "KE": "KAL", "OZ": "AAR", "CI": "CAL", "BR": "EVA", "MH": "MAS",
    "AK": "AXM", "D7": "XAX", "TG": "THA", "GA": "GIA", "PR": "PAL", "5J": "CEB", "VN": "HVN",
    "CA": "CCA", "MU": "CES", "CZ": "CSN", "HU": "CHH", "AI": "AIC", "6E": "IGO", "LH": "DLH",
    "AF": "AFR", "KL": "KLM", "LX": "SWR", "OS": "AUA", "TK": "THY", "LA": "LAN", "FJ": "FJI",
    "NF": "AVN", "PX": "ANG", "IE": "SOL", "SB": "ACI", "FD": "AIQ", "VJ": "VJC", "WY": "OMA",
    "SA": "SAA", "ET": "ETH", "FR": "RYR", "U2": "EZY", "W6": "WZZ", "NK": "NKS", "F9": "FFT",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_reg(reg):
    return re.sub(r"[^A-Z0-9]", "", (reg or "").upper())


def norm_callsign(cs):
    """'QFA009 ' -> 'QFA9'. Strips spaces and leading zeros of the number part."""
    cs = re.sub(r"\s", "", (cs or "").upper())
    m = re.match(r"^([A-Z]{3})0*(\d+[A-Z]*)$", cs)
    return m.group(1) + m.group(2) if m else cs


def flight_to_callsign(flight):
    """'QF9' / 'QF 009' -> 'QFA9'. 'QFA9' is passed through."""
    f = re.sub(r"\s", "", (flight or "").upper())
    if not f:
        return ""
    m = re.match(r"^([A-Z0-9]{2})(\d+[A-Z]?)$", f)
    if m and not re.match(r"^[A-Z]{3}", f) and m.group(1) in IATA_TO_ICAO:
        return norm_callsign(IATA_TO_ICAO[m.group(1)] + m.group(2))
    return norm_callsign(f)


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "flight-notifier/1.0 (personal family notifier)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fmt_alt(ac):
    a = ac.get("alt_baro")
    return "on the ground" if a == "ground" else (f"{a:,} ft" if isinstance(a, (int, float)) else "unknown alt")


def local_time():
    return datetime.now().astimezone().strftime("%H:%M %Z").strip()


# --------------------------------------------------------------------------- #
# Tracking links
# --------------------------------------------------------------------------- #
def tracking_url(cfg, reg, callsign, icao):
    style = cfg["tracking_link"]
    r = (reg or "").lower()
    if style == "flightaware" and callsign:
        return f"https://www.flightaware.com/live/flight/{callsign}"
    if style == "fr24":
        return f"https://www.flightradar24.com/data/aircraft/{r}" if r else f"https://www.flightradar24.com/data/flights/{callsign.lower()}"
    if style == "adsbexchange" and icao:
        return f"https://globe.adsbexchange.com/?icao={icao}"
    if style == "adsblol" and icao:
        return f"https://globe.adsb.lol/?icao={icao}"
    if "{" in style:  # custom template
        return style.format(reg=reg or "", reg_lower=r, callsign=callsign or "", icao=icao or "")
    # sensible fallbacks
    if icao:
        return f"https://globe.adsb.lol/?icao={icao}"
    if callsign:
        return f"https://www.flightaware.com/live/flight/{callsign}"
    return f"https://www.flightradar24.com/data/aircraft/{r}"


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.sms_to = [n.strip() for n in cfg["sms_to"].split(",") if n.strip()]
        self.use_ntfy = bool(cfg["ntfy_topic"])
        self.use_sms = bool(cfg["twilio_sid"] and cfg["twilio_token"] and cfg["twilio_from"] and self.sms_to)
        if not (self.use_ntfy or self.use_sms) and not dry_run:
            log("WARNING: no notification method configured (set ntfy_topic and/or Twilio settings). "
                "Messages will only be printed here.")

    def send(self, title, body, link=None, tags=""):
        text = f"{title}\n{body}" + (f"\nTrack: {link}" if link else "")
        log("NOTIFY >> " + text.replace("\n", " | "))
        if self.dry_run:
            return
        if self.use_ntfy:
            self._ntfy(title, body, link, tags)
        if self.use_sms:
            for to in self.sms_to:
                self._sms(to, text)

    def _ntfy(self, title, body, link, tags):
        url = self.cfg["ntfy_server"].rstrip("/") + "/" + urllib.parse.quote(self.cfg["ntfy_topic"])
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": "high"}
        if tags:
            headers["Tags"] = tags
        if link:
            headers["Click"] = link
            headers["Actions"] = f"view, Track flight, {link}"
        try:
            req = urllib.request.Request(url, data=(body + (f"\n{link}" if link else "")).encode(), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            log(f"ntfy send failed: {e}")

    def _sms(self, to, text):
        sid, token = self.cfg["twilio_sid"], self.cfg["twilio_token"]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = urllib.parse.urlencode({"From": self.cfg["twilio_from"], "To": to, "Body": text}).encode()
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        try:
            req = urllib.request.Request(url, data=data, headers={"Authorization": "Basic " + auth}, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as e:
            log(f"SMS to {to} failed: {e.code} {e.read().decode(errors='ignore')[:200]}")
        except Exception as e:
            log(f"SMS to {to} failed: {e}")


# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #
class LiveSource:
    """Looks the aircraft up by registration and callsign on free ADS-B feeds."""

    def fetch(self, reg, callsign):
        results = []
        for name, reg_url, cs_url in FEEDS:
            try:
                if reg:
                    results += http_json(reg_url.format(reg=urllib.parse.quote(reg))).get("ac") or []
                if callsign:
                    results += http_json(cs_url.format(cs=urllib.parse.quote(callsign))).get("ac") or []
                return results  # first feed that answered wins
            except Exception as e:
                log(f"{name} lookup failed ({e}); trying next feed")
                results = []
        return None  # every feed failed (network problem) - different from "not seen"


class SimulatedSource:
    """Plays out a fake flight quickly so you can test the whole thing end-to-end."""

    def __init__(self, reg, callsign):
        self.reg, self.cs, self.t = reg or "VH-TEST", callsign or "TST123", 0
        base = [("prev-leg", None)] * 2                       # aircraft finishing previous sector (other callsign)
        base += [("ground", 0)] * 3 + [(400, 140), (1200, 170), (2500, 200), (9000, 290), (36000, 470)]
        base += [("gap", None)] * 3                           # ocean coverage gap
        base += [(36000, 470), (12000, 300), (3000, 180), (800, 150), (150, 130), ("ground", 60), ("ground", 20)]
        self.script = base

    def fetch(self, reg, callsign):
        if self.t >= len(self.script):
            return []
        alt, gs = self.script[self.t]
        self.t += 1
        if alt == "gap":
            return []
        if alt == "prev-leg":
            return [{"hex": "7c1234", "r": self.reg, "flight": "XYZ999  ", "alt_baro": 30000, "gs": 450, "seen": 1, "lat": -33.9, "lon": 151.2}]
        return [{"hex": "7c1234", "r": self.reg, "flight": self.cs + "  ", "alt_baro": alt, "gs": gs, "seen": 1,
                 "lat": -33.9, "lon": 151.2}]


# --------------------------------------------------------------------------- #
# Tracker (state machine: waiting -> airborne -> landed)
# --------------------------------------------------------------------------- #
class Tracker:
    def __init__(self, cfg, reg, flight, origin, dest, source, notifier, phase="waiting", run_limit_minutes=None):
        self.cfg, self.source, self.n = cfg, source, notifier
        self.reg_input = (reg or "").upper()
        self.reg = norm_reg(reg)
        self.callsign = flight_to_callsign(flight)
        self.flight_label = (flight or self.callsign or self.reg_input).upper()
        self.origin, self.dest = (origin or "").upper(), (dest or "").upper()
        self.phase = phase
        self.icao = None
        self.actual_reg = self.reg_input
        self.swap_reported = False
        self.streak = 0
        self.last_seen_ac = None
        self.last_seen_time = None
        self.started = time.time()
        self.run_deadline = self.started + run_limit_minutes * 60 if run_limit_minutes else None

    # -- choosing the right aircraft ------------------------------------------ #
    def pick(self, acs):
        """Return the aircraft that is actually operating YOUR flight.

        If a flight number is given, the aircraft must be broadcasting that callsign.
        This stops a false 'taken off' when your aircraft flies an earlier sector
        under a different flight number. If only a registration is given, we use it."""
        fresh = [a for a in acs if (a.get("seen") is None or a.get("seen", 99) < 60)]
        if self.callsign:
            matches = [a for a in fresh if norm_callsign(a.get("flight")) == self.callsign]
            if not matches:
                return None
            same_reg = [a for a in matches if norm_reg(a.get("r")) == self.reg]
            return (same_reg or matches)[0]
        regs = [a for a in fresh if norm_reg(a.get("r")) == self.reg]
        return regs[0] if regs else None

    def link(self):
        return tracking_url(self.cfg, self.actual_reg, self.callsign, self.icao)

    def route(self):
        if self.origin and self.dest:
            return f" {self.origin}→{self.dest}"
        return f" to {self.dest}" if self.dest else ""

    # -- main loop ------------------------------------------------------------ #
    def run(self, poll_seconds):
        name = self.cfg["your_name"]
        log(f"Tracking {self.flight_label} (callsign {self.callsign or '-'}, reg {self.reg_input or '-'}) "
            f"phase={self.phase}; takeoff > {self.cfg['takeoff_alt']} ft, landed < {self.cfg['landed_alt']} ft")
        if self.cfg["notify_start"] and self.phase == "waiting":
            self.n.send(f"✈️ {name} is flying {self.flight_label}{self.route()}",
                        f"You'll get a message when the flight takes off and lands.", self.link(), "airplane")

        while True:
            now = time.time()
            if self.run_deadline and now >= self.run_deadline:
                log(f"Run time limit reached; handing over (phase={self.phase})")
                return self.phase
            waited_h = (now - self.started) / 3600
            if self.phase == "waiting" and waited_h > self.cfg["max_wait_hours"]:
                log("Gave up: no takeoff detected within max_wait_hours")
                return "gave_up"
            if self.phase == "airborne" and waited_h > self.cfg["max_flight_hours"]:
                log("Gave up: no landing detected within max_flight_hours")
                return "gave_up"

            acs = self.source.fetch(self.reg_input, self.callsign)
            if acs is None:
                time.sleep(poll_seconds)
                continue
            ac = self.pick(acs)
            if ac:
                self.observe(ac)
            else:
                self.observe_missing()
            if self.phase == "landed":
                return "landed"
            time.sleep(poll_seconds)

    def observe(self, ac):
        self.last_seen_ac, self.last_seen_time = ac, time.time()
        self.icao = ac.get("hex") or self.icao
        seen_reg = (ac.get("r") or "").upper()
        if seen_reg:
            self.actual_reg = seen_reg
        if self.reg and seen_reg and norm_reg(seen_reg) != self.reg and not self.swap_reported:
            log(f"Note: {self.flight_label} is being flown by {seen_reg}, not {self.reg_input} (aircraft swap)")
            self.swap_reported = True

        alt, gs = ac.get("alt_baro"), ac.get("gs") or 0
        log(f"{self.flight_label} {seen_reg or ''} {fmt_alt(ac)}, {gs:.0f} kt  [{self.phase}]")
        c = self.cfg
        name = c["your_name"]

        if self.phase == "waiting":
            hit = isinstance(alt, (int, float)) and alt >= c["takeoff_alt"]
            self.streak = self.streak + 1 if hit else 0
            if self.streak >= c["confirm_readings"]:
                self.phase, self.streak = "airborne", 0
                swap = f" (aircraft {self.actual_reg})" if self.swap_reported else ""
                self.n.send(f"🛫 {name} has taken off", f"{self.flight_label}{self.route()}{swap} departed at {local_time()}.",
                            self.link(), "airplane_departure")

        elif self.phase == "airborne":
            on_ground = alt == "ground" or (isinstance(alt, (int, float)) and alt <= c["landed_alt"] and gs <= c["landed_speed"])
            self.streak = self.streak + 1 if on_ground else 0
            if self.streak >= c["confirm_readings"]:
                self.land("")

    def observe_missing(self):
        """No data this poll. Normal over oceans / before the aircraft powers up."""
        if self.phase != "airborne" or not self.last_seen_ac:
            return
        gone_min = (time.time() - self.last_seen_time) / 60
        alt = self.last_seen_ac.get("alt_baro")
        low = alt == "ground" or (isinstance(alt, (int, float)) and alt <= self.cfg["lost_signal_below_alt"])
        if low and gone_min >= self.cfg["lost_signal_minutes"]:
            self.land(" (tracking signal ended just before landing)")

    def land(self, note):
        self.phase = "landed"
        self.n.send(f"🛬 {self.cfg['your_name']} has landed",
                    f"{self.flight_label}{self.route()} landed at {local_time()}{note}.",
                    self.link(), "airplane_arrival")


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    for env, key in ENV_MAP.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def main():
    p = argparse.ArgumentParser(description="Notify family when your flight takes off and lands.")
    p.add_argument("--reg", help="Aircraft registration, e.g. VH-ZNA")
    p.add_argument("--flight", help="Flight number (QF9) or ICAO callsign (QFA9)")
    p.add_argument("--from", dest="origin", help="Departure airport (for the message), e.g. MEL")
    p.add_argument("--to", dest="dest", help="Arrival airport (for the message), e.g. LHR")
    p.add_argument("--takeoff-alt", type=int, help="Feet. Above this = taken off")
    p.add_argument("--landed-alt", type=int, help="Feet. Below this (and slow) = landed")
    p.add_argument("--name", help="Your name as it appears in messages")
    p.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    p.add_argument("--phase", default="waiting", choices=["waiting", "airborne"], help="Resume state (used by GitHub Actions)")
    p.add_argument("--run-limit-minutes", type=float, help="Stop after N minutes and report phase (GitHub Actions)")
    p.add_argument("--no-start-message", action="store_true")
    p.add_argument("--simulate", action="store_true", help="Fake flight to test notifications end to end")
    p.add_argument("--dry-run", action="store_true", help="Print messages instead of sending them")
    p.add_argument("--test-notify", action="store_true", help="Send one test message and exit")
    a = p.parse_args()

    cfg = load_config(a.config)
    if a.takeoff_alt is not None:
        cfg["takeoff_alt"] = a.takeoff_alt
    if a.landed_alt is not None:
        cfg["landed_alt"] = a.landed_alt
    if a.name:
        cfg["your_name"] = a.name
    if a.no_start_message or a.phase != "waiting":
        cfg["notify_start"] = False
    for k in ("takeoff_alt", "landed_alt", "landed_speed", "confirm_readings", "poll_seconds",
              "max_wait_hours", "max_flight_hours", "lost_signal_minutes", "lost_signal_below_alt"):
        cfg[k] = float(cfg[k])
    cfg["confirm_readings"] = int(cfg["confirm_readings"])
    if cfg["landed_alt"] >= cfg["takeoff_alt"]:
        sys.exit("landed_alt must be lower than takeoff_alt")

    notifier = Notifier(cfg, dry_run=a.dry_run)
    if a.test_notify:
        notifier.send("✅ Flight notifier test", f"This is a test from {cfg['your_name']}'s flight notifier. It works!",
                      "https://globe.adsb.lol", "white_check_mark")
        return

    if not (a.reg or a.flight):
        p.error("give --reg and/or --flight (flight number is strongly recommended)")

    if a.simulate:
        source, poll = SimulatedSource(a.reg, flight_to_callsign(a.flight)), 1
        cfg["lost_signal_minutes"] = 999
    else:
        source, poll = LiveSource(), cfg["poll_seconds"]

    t = Tracker(cfg, a.reg, a.flight, a.origin, a.dest, source, notifier, a.phase, a.run_limit_minutes)
    result = t.run(poll)
    log(f"Finished: {result}")

    # Let GitHub Actions know whether to continue in a fresh job
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"result={result}\n")


if __name__ == "__main__":
    main()
