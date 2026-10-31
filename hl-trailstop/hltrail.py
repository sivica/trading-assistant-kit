#!/usr/bin/env python3
"""hltrail - Hyperliquid trailing-stop helper.

Only ever TIGHTENS existing reduce-only stop orders. Never opens, increases
or flips positions, never creates a stop where none exists.
dry_run: true (default) -> only logs what it would do; needs no key.
"""
import json, math, os, sys, time, random, traceback
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from zoneinfo import ZoneInfo

import requests
import yaml

HOME = os.environ.get("HLTRAIL_HOME", "/home/hlbot/.hltrail")
CONFIG = os.path.join(HOME, "config.yaml")
STATE = os.path.join(HOME, "state.json")
STATUS = os.path.join(HOME, "status.json")
ACTIONS = os.path.join(HOME, "actions.log")
ENVFILE = os.path.join(HOME, "env")
API = "https://api.hyperliquid.xyz/info"
LOCAL_TZ = ZoneInfo(os.environ.get("LOCAL_TZ", "UTC"))   # your timezone, e.g. Europe/Berlin

DEFAULTS = {
    "dry_run": True,
    "account_address": os.environ.get("HL_ACCOUNT_ADDRESS", ""),   # your MAIN Hyperliquid address (public); set in config.yaml or env
    "dexes": ["", "xyz"],
    "interval_sec": 120,
    "trail_pct": 0.04,
    "activate_pct": None,     # None -> same as trail_pct
    "min_step_pct": 0.005,
    "stop_slippage_pct": 0.10,  # limit px offset for stop-market orders (live only)
    "markets": {},
}


# ---------------------------------------------------------------- utils
def now_strs():
    u = datetime.now(timezone.utc)
    return u.strftime("%Y-%m-%d %H:%M:%S UTC"), u.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M local")


def log_action(line):
    u, s = now_strs()
    msg = f"{u} | {s} | {line}"
    print(msg, flush=True)
    try:
        with open(ACTIONS, "a") as f:
            f.write(msg + "\n")
    except OSError as e:
        print(f"could not write actions.log: {e}", flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            user = yaml.safe_load(f) or {}
        cfg.update(user)
    cfg["markets"] = cfg.get("markets") or {}
    return cfg


def coin_cfg(cfg, coin):
    m = dict(cfg["markets"].get(coin) or {})
    trail = float(m.get("trail_pct", cfg["trail_pct"]))
    act = m.get("activate_pct", cfg.get("activate_pct"))
    act = trail if act is None else float(act)
    return {
        "enabled": bool(m.get("enabled", True)),
        "trail_pct": trail,
        "activate_pct": act,
        "min_step_pct": float(m.get("min_step_pct", cfg["min_step_pct"])),
    }


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- price rules
def price_step(px, sz_decimals, max_decimals=6):
    """Smallest valid price increment near px: 5 sig figs and
    (max_decimals - szDecimals) decimals; integers always allowed."""
    dec = max(max_decimals - sz_decimals, 0)
    sig_step = Decimal(10) ** (math.floor(math.log10(px)) - 4)
    dec_step = Decimal(10) ** (-dec)
    step = max(sig_step, dec_step)
    return min(step, Decimal(1))


def round_px(px, sz_decimals, direction):
    """direction 'down' or 'up'."""
    step = price_step(px, sz_decimals)
    q = (Decimal(str(px)) / step).to_integral_value(ROUND_FLOOR if direction == "down" else ROUND_CEILING)
    return float(q * step)


# ---------------------------------------------------------------- trailing math (pure)
def compute(side, entry, price, best, stop, tp, ccfg, sz_decimals, activated=False):
    """Return dict(best, activated, proposed, reason).
    side: 'long' or 'short'. stop/tp may be None. proposed None => no move."""
    trail, act, step = ccfg["trail_pct"], ccfg["activate_pct"], ccfg["min_step_pct"]
    long = side == "long"
    if best is None:
        best = price
    best = max(best, price) if long else min(best, price)
    profit_best = (best - entry) / entry if long else (entry - best) / entry
    if not activated and profit_best >= act - 1e-12:
        activated = True
    out = {"best": best, "activated": activated, "proposed": None, "reason": ""}
    if stop is None:
        out["reason"] = "no stop order"
        return out
    if not activated:
        trig = entry * (1 + act) if long else entry * (1 - act)
        out["reason"] = f"not active (needs price {'>=' if long else '<='} {trig:.6g})"
        return out
    raw = best * (1 - trail) if long else best * (1 + trail)
    new = round_px(raw, sz_decimals, "down" if long else "up")
    # never past current price
    if (long and new >= price) or (not long and new <= price):
        out["reason"] = "proposed stop would be past current price"
        return out
    # never past TP
    if tp is not None and ((long and new >= tp) or (not long and new <= tp)):
        out["reason"] = "proposed stop would be past take-profit"
        return out
    # only tighten
    improve = (new - stop) if long else (stop - new)
    if improve <= 0:
        out["reason"] = "would loosen or equal current stop"
        return out
    if improve < step * price:
        out["reason"] = f"improvement {improve / price:.2%} < min step {step:.2%}"
        return out
    out["proposed"] = new
    out["reason"] = f"trail {trail:.1%} from best {best:.6g}"
    return out


# ---------------------------------------------------------------- API
class Api:
    def __init__(self):
        self.s = requests.Session()

    def post(self, body, tries=6):
        delay = 2.0
        for i in range(tries):
            try:
                r = self.s.post(API, json=body, timeout=15)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa
                if i == tries - 1:
                    raise
                wait = delay + random.uniform(0, delay / 2)
                print(f"API error ({e}); retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
                delay = min(delay * 2, 120)


def classify_orders(orders, coin, long, price):
    """Return (stops, tps) lists of reduce-only trigger orders closing the position."""
    stops, tps = [], []
    close_side = "A" if long else "B"
    for o in orders:
        if o.get("coin") != coin or not o.get("isTrigger") or o.get("side") != close_side:
            continue
        if not o.get("reduceOnly") and not o.get("isPositionTpsl"):
            continue
        tpx = float(o["triggerPx"])
        ot = (o.get("orderType") or "").lower()
        if "take profit" in ot:
            tps.append(o)
        elif "stop" in ot:
            stops.append(o)
        else:  # fall back to which side of price it sits
            losing = tpx < price if long else tpx > price
            (stops if losing else tps).append(o)
    return stops, tps


def snapshot(api, cfg):
    addr = cfg["account_address"]
    if not addr or not str(addr).startswith("0x"):
        raise SystemExit("set account_address (your main Hyperliquid address) in config.yaml or HL_ACCOUNT_ADDRESS")
    out = []
    for dex in cfg["dexes"]:
        ch = api.post({"type": "clearinghouseState", "user": addr, "dex": dex})
        positions = [p["position"] for p in ch.get("assetPositions", []) if float(p["position"]["szi"]) != 0]
        if not positions:
            continue
        orders = api.post({"type": "frontendOpenOrders", "user": addr, "dex": dex})
        mids = api.post({"type": "allMids", "dex": dex})
        meta = api.post({"type": "meta", "dex": dex})
        szd = {u["name"]: u["szDecimals"] for u in meta["universe"]}
        for p in positions:
            coin = p["coin"]
            szi = float(p["szi"])
            long = szi > 0
            price = float(mids.get(coin) or 0)
            if price <= 0:
                continue
            stops, tps = classify_orders(orders, coin, long, price)
            out.append({"coin": coin, "dex": dex, "side": "long" if long else "short",
                        "size": abs(szi), "entry": float(p["entryPx"]), "price": price,
                        "sz_decimals": szd.get(coin, 0), "stops": stops, "tps": tps})
    return out


# ---------------------------------------------------------------- live execution
class Live:
    def __init__(self, cfg):
        key = None
        if os.path.exists(ENVFILE):
            with open(ENVFILE) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("HL_API_WALLET_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not key:
            raise SystemExit(f"live mode: {ENVFILE} missing or has no HL_API_WALLET_KEY; refusing to start")
        st = os.stat(ENVFILE)
        if st.st_mode & 0o077:
            raise SystemExit(f"live mode: {ENVFILE} must be chmod 600; refusing to start")
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        wallet = eth_account.Account.from_key(key)
        del key
        self.ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=cfg["account_address"],
                           perp_dexs=cfg["dexes"])
        self.cfg = cfg

    def replace_stop(self, api, pos, new_stop, old_stops):
        long = pos["side"] == "long"
        is_buy = not long
        slip = float(self.cfg["stop_slippage_pct"])
        lim = new_stop * (1 + slip) if is_buy else new_stop * (1 - slip)
        lim = round_px(lim, pos["sz_decimals"], "up" if is_buy else "down")
        otype = {"trigger": {"triggerPx": new_stop, "isMarket": True, "tpsl": "sl"}}
        res = self.ex.order(pos["coin"], is_buy, pos["size"], lim, otype, reduce_only=True)
        statuses = (res.get("response", {}).get("data", {}) or {}).get("statuses", []) if isinstance(res, dict) else []
        if res.get("status") != "ok" or not statuses or "error" in statuses[0]:
            log_action(f"ERROR {pos['coin']} placing new stop {new_stop} failed: {res}; old stop kept")
            return False
        st0 = statuses[0]
        new_oid = (st0.get("resting") or {}).get("oid")
        if new_oid is None:
            log_action(f"ERROR {pos['coin']} new stop not resting: {st0}; old stop kept")
            return False
        time.sleep(2)
        orders = api.post({"type": "frontendOpenOrders", "user": self.cfg["account_address"], "dex": pos["dex"]})
        if not any(o.get("oid") == new_oid for o in orders):
            log_action(f"ERROR {pos['coin']} new stop oid {new_oid} not found resting; old stop kept")
            return False
        for o in old_stops:
            r = self.ex.cancel(pos["coin"], o["oid"])
            log_action(f"LIVE {pos['coin']} cancelled old stop oid {o['oid']} @ {o['triggerPx']}: {r.get('status')}")
        log_action(f"LIVE {pos['coin']} new stop {new_stop} resting oid {new_oid}")
        return True


# ---------------------------------------------------------------- cycle
def run_cycle(api, cfg, live=None, write=True):
    state = load_json(STATE, {})
    positions = snapshot(api, cfg)
    seen = set()
    rows = []
    for pos in positions:
        coin = pos["coin"]
        seen.add(coin)
        ccfg = coin_cfg(cfg, coin)
        long = pos["side"] == "long"
        st = state.get(coin)
        if not st or st.get("side") != pos["side"] or abs(st.get("entry", 0) - pos["entry"]) > 1e-9 * pos["entry"]:
            st = {"side": pos["side"], "entry": pos["entry"], "best": None, "activated": False,
                  "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        stops = pos["stops"]
        tps = pos["tps"]
        stop_px = None
        if stops:
            # tightest stop = closest to price
            stop_px = (max if long else min)(float(o["triggerPx"]) for o in stops)
        tp_px = None
        if tps:
            tp_px = (min if long else max)(float(o["triggerPx"]) for o in tps)
        r = compute(pos["side"], pos["entry"], pos["price"], st["best"], stop_px, tp_px, ccfg,
                    pos["sz_decimals"], st["activated"])
        st["best"], st["activated"] = r["best"], r["activated"]
        state[coin] = st
        if not ccfg["enabled"]:
            r["proposed"], r["reason"] = None, "disabled in config"
        if write:
            if stop_px is None and not st.get("warned_nostop"):
                st["warned_nostop"] = True
                log_action(f"WARNING {coin} {pos['side']} has NO stop order (price {pos['price']}, entry {pos['entry']})")
            if stop_px is not None:
                st.pop("warned_nostop", None)
            key = f"{stop_px}->{r['proposed']}"
            if r["proposed"] is not None and (live is not None or st.get("last_logged") != key):
                st["last_logged"] = key
                tag = "DRY-RUN would move" if live is None else "LIVE moving"
                log_action(f"{tag} {coin} {pos['side']} stop {stop_px} -> {r['proposed']} | price {pos['price']} | "
                           f"best {r['best']} | entry {pos['entry']} | {r['reason']}")
                if live is not None:
                    live.replace_stop(api, pos, r["proposed"], stops)
        rows.append({"coin": coin, "side": pos["side"], "size": pos["size"], "entry": pos["entry"],
                     "price": pos["price"], "stop": stop_px, "stop_oids": [o["oid"] for o in stops],
                     "tp": tp_px, "best": r["best"], "activated": r["activated"],
                     "proposed": r["proposed"], "reason": r["reason"],
                     "trail_pct": ccfg["trail_pct"], "activate_pct": ccfg["activate_pct"]})
    for c in list(state):
        if c not in seen:
            del state[c]  # position closed: reset tracking
    u, s = now_strs()
    status = {"last_run_utc": u, "last_run_local": s, "dry_run": live is None,
              "interval_sec": cfg["interval_sec"], "positions": rows}
    if write:
        save_json(STATE, state)
        save_json(STATUS, status)
    return status


def fmt(x):
    return "-" if x is None else f"{x:,.6g}"


def print_status(st):
    print(f"hltrail  mode: {'DRY-RUN (no orders)' if st['dry_run'] else 'LIVE'}  every {st['interval_sec']}s")
    print(f"last run: {st['last_run_local']}  ({st['last_run_utc']})")
    if not st["positions"]:
        print("no open positions")
    for p in st["positions"]:
        print(f"\n{p['coin']}  {p['side']} {p['size']:g}")
        print(f"  entry {fmt(p['entry'])}  price {fmt(p['price'])}  best {fmt(p['best'])}")
        print(f"  stop  {fmt(p['stop'])}  (oid {', '.join(map(str, p['stop_oids'])) or 'NONE'})  tp {fmt(p['tp'])}")
        print(f"  trailing {'ACTIVE' if p['activated'] else 'waiting'}  trail {p['trail_pct']:.1%}")
        print(f"  proposed stop: {fmt(p['proposed'])}  ({p['reason']})")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    cfg = load_config()
    api = Api()
    if cmd == "status":
        if "--fresh" in sys.argv:
            print_status(run_cycle(api, cfg, write=False))
        else:
            st = load_json(STATUS, None)
            if not st:
                print("no status yet; is hl-trailstop running?  (try: hltrail status --fresh)")
                return
            print_status(st)
        return
    if cmd == "once":
        print_status(run_cycle(api, cfg, write=True))
        return
    if cmd == "run":
        live = None
        if not cfg.get("dry_run", True):
            live = Live(cfg)
        log_action(f"START hltrail {'DRY-RUN' if live is None else 'LIVE'} interval {cfg['interval_sec']}s "
                   f"trail {cfg['trail_pct']} min_step {cfg['min_step_pct']}")
        fails = 0
        while True:
            try:
                cfg = load_config()
                if live is None and not cfg.get("dry_run", True):
                    log_action("config switched to live; restart service to enable live mode (staying dry-run)")
                if live is not None and cfg.get("dry_run", True):
                    log_action("config switched to dry_run; exiting so service restarts in dry-run")
                    sys.exit(0)
                run_cycle(api, cfg, live)
                fails = 0
                time.sleep(int(cfg["interval_sec"]))
            except SystemExit:
                raise
            except Exception as e:
                fails += 1
                wait = min(30 * 2 ** (fails - 1), 900)
                log_action(f"WARNING cycle error #{fails}: {e!r}; backing off {wait}s")
                traceback.print_exc()
                time.sleep(wait)
        return
    print("usage: hltrail [status [--fresh] | once | run]")


if __name__ == "__main__":
    main()
