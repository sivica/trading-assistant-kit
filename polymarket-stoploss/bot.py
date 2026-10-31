"""The stop-loss loop. Network and trading are injected so it can be tested offline."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import games
import pmapi
from config import Config
from stoplogic import Decision, evaluate

log = logging.getLogger("pmbot")

NOTICE_REPEAT_S = {"would_sell": 3600, "no_bids": 6 * 3600, "below_floor": 6 * 3600,
                   "geoblocked": 6 * 3600, "error": 3600}


class PublicApi:
    """Default read-only data source."""
    def __init__(self, conf: Config):
        self.rpc = pmapi.Rpc(conf.rpc_urls)

    def positions(self, addr): return pmapi.positions(addr)
    def book(self, token_id): return pmapi.book(token_id)
    def exact_shares(self, addr, token_id): return self.rpc.exact_shares(addr, token_id)
    def geoblock(self): return pmapi.geoblock()
    def market(self, slug): return pmapi.gamma_market_by_slug(slug)

MARKET_CACHE_S = 300          # re-read Gamma market data (start time, closed) every 5 min


class LiveTrader:
    """Places real orders through the official SDK. Only used when dry_run is false."""
    def __init__(self, client_factory: Callable):
        self._factory = client_factory
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = self._factory()
        return self._client

    def cancel_orders(self, token_id: str):
        return self.client.cancel_market_orders(asset_id=token_id)

    def sell(self, token_id: str, shares: Decimal, min_price: Decimal):
        """Fill-and-kill SELL: fills against bids at >= min_price, never rests on the book."""
        try:
            return self.client.place_market_order(token_id=token_id, side="SELL", shares=str(shares),
                                                  min_price=str(min_price), order_type="FAK")
        except Exception:
            self._client = None  # force a fresh client next time (stale creds/session)
            raise


class StopLossBot:
    def __init__(self, conf: Config, wallet_address: str, api=None, trader=None,
                 state_path: Optional[Path] = None, notifier: Callable[[str], None] = pmapi.notify,
                 clock: Callable[[], float] = time.time, dry_run: Optional[bool] = None):
        self.conf = conf
        self.addr = wallet_address
        self.api = api or PublicApi(conf)
        self.trader = trader
        self.state_path = state_path
        self.notify = notifier
        self.clock = clock
        self.dry_run = conf.dry_run if dry_run is None else dry_run
        self.hits: dict[str, int] = {}
        self._mcache: dict[str, tuple[float, Optional[dict]]] = {}   # slug -> (fetched_at, market)
        self._game_seen: dict[str, str] = {}                         # tid -> last logged game label
        self.state = self._load_state()
        self._geo_checked_at: Optional[float] = None
        self.geo_blocked = False
        if not self.dry_run and self.trader is None:
            raise RuntimeError("live mode needs a trader")

    # ----- state -----
    def _load_state(self) -> dict:
        if self.state_path and self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except Exception:
                log.warning("state file unreadable; starting fresh")
        return {"tokens": {}}

    def _save_state(self):
        if self.state_path:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.state_path)

    def _tok(self, tid: str) -> dict:
        return self.state["tokens"].setdefault(tid, {"notices": {}})

    def _notice(self, tid: str, kind: str, text: str):
        t = self._tok(tid)
        last = t["notices"].get(kind)
        if last is None or self.clock() - last >= NOTICE_REPEAT_S.get(kind, 3600):
            t["notices"][kind] = self.clock()
            self.notify(text)

    # ----- geoblock -----
    def check_geoblock(self, force=False) -> bool:
        now = self.clock()
        if force or self._geo_checked_at is None or now - self._geo_checked_at >= self.conf.geoblock_check_minutes * 60:
            self._geo_checked_at = now
            try:
                g = self.api.geoblock()
                self.geo_blocked = bool(g.get("blocked"))
                if self.geo_blocked:
                    log.error("GEOBLOCKED: Polymarket says this server (%s, %s) may not trade. Sells are paused.",
                              g.get("country"), g.get("region"))
                    self._notice("_server", "geoblocked",
                                 f"Stop-loss bot: Polymarket now blocks this server ({g.get('country')}). Sells are paused!")
            except Exception as e:
                log.warning("geoblock check failed (%s); keeping previous result", type(e).__name__)
        return self.geo_blocked

    # ----- one round -----
    def run_once(self) -> list[Decision]:
        if not self.dry_run:
            self.check_geoblock()
        decisions: list[Decision] = []
        try:
            positions = self.api.positions(self.addr)
        except Exception as e:
            log.error("could not read positions: %s", e)
            return decisions
        seen = set()
        for p in positions:
            tid = str(p.get("asset", ""))
            seen.add(tid)
            try:
                d = self._handle(p)
            except Exception as e:
                log.exception("error handling %s", tid[:12])
                self._notice(tid, "error", f"Stop-loss bot error on {p.get('title','?')}: {type(e).__name__}: {e}")
                continue
            if d is not None:
                decisions.append(d)
        for tid in list(self.hits):
            if tid not in seen:
                self.hits.pop(tid)
        self._save_state()
        return decisions

    # ----- live-game detection -----
    def game_info(self, p: dict) -> games.GameInfo:
        slug = p.get("slug")
        now = self.clock()
        fetch = getattr(self.api, "market", None)
        failed = False
        if not slug or fetch is None:
            m = None
        else:
            hit = self._mcache.get(slug)
            m = hit[1] if hit else None
            if hit is None or now - hit[0] >= MARKET_CACHE_S:
                try:
                    m = fetch(slug)
                    self._mcache[slug] = (now, m)
                except Exception as e:
                    failed = True
                    if hit:              # keep using the last good data (start times rarely change)
                        failed = False
                    log.warning("market data for %s unavailable (%s)%s", slug, type(e).__name__,
                                "; using cached data" if hit else "")
        g = games.classify(m, datetime.fromtimestamp(now, tz=timezone.utc), self.conf.live_window_hours, failed)
        tid = str(p.get("asset", ""))
        key = f"{g.phase}|{g.start}"
        if self._game_seen.get(tid) != key:
            self._game_seen[tid] = key
            name = f"{str(p.get('title',''))[:60]} [{p.get('outcome','')}]"
            if g.phase == "unknown":
                log.warning("GAME TIME UNKNOWN for %s (%s): using the normal confirm delay", name, g.note or "?")
            elif g.phase == "none":
                log.info("%s: not a sports game; normal confirm delay", name)
            elif g.start is not None:
                log.info("GAME %s: %s (start %s local time, from %s)", g.phase.upper(), name,
                         games.local(g.start), g.source)
        return g

    def _reset_timer(self, tid: str, name: str, why: str):
        t = self.state["tokens"].get(tid)
        if t and t.get("below_since") is not None:
            log.info("STOP TIMER RESET %s: %s after %ds", name, why, int(self.clock() - t["below_since"]))
        if t:
            for k in ("below_since", "below_seen", "fired_at"):
                t.pop(k, None)
        self.hits[tid] = 0

    def _handle(self, p: dict) -> Optional[Decision]:
        tid = str(p.get("asset", ""))
        if float(p.get("size") or 0) <= float(self.conf.dust_shares) and not p.get("redeemable"):
            return None
        rule = self.conf.rule_for(p)
        name = f"{str(p.get('title', ''))[:60]} [{p.get('outcome', '')}]"
        g = games.GameInfo("none")
        if not p.get("redeemable") and rule.enabled:
            g = self.game_info(p)
        need = rule.live_confirm_sec if g.live else rule.confirm_sec
        if g.live and rule.live_pause:
            d = evaluate(p, None, rule, dust=self.conf.dust_shares)
            d.action, d.reason = "skip", "stop paused during the live game (live_pause)"
            d.game, d.confirm_sec = g.label(), None
            self._reset_timer(tid, name, "live_pause")
            return d
        b = None
        if not p.get("redeemable") and rule.enabled:
            p = dict(p, exact_size=self.api.exact_shares(self.addr, tid))
            if p["exact_size"] > self.conf.dust_shares:
                b = self.api.book(tid)
        d = evaluate(p, b, rule, dust=self.conf.dust_shares)
        d.game, d.confirm_sec = g.label(), need
        name = f"{d.title[:60]} [{d.outcome}]"

        if not d.triggered:
            if d.action == "hold":           # bid is back above the stop
                self._reset_timer(tid, name, f"bid recovered ({d.reason})")
            return d
        # Stop is hit: bid <= stop (or no bids). Start / continue the confirm timer.
        now = self.clock()
        t = self._tok(tid)
        stale = max(600, 5 * self.conf.poll_interval_seconds)
        if t.get("below_since") is not None and now - (t.get("below_seen") or 0) > stale:
            log.info("STOP TIMER RESET %s: no check for %ds (bot was down?); starting over", name,
                     int(now - (t.get("below_seen") or 0)))
            for k in ("below_since", "below_seen", "fired_at"):
                t.pop(k, None)
        if t.get("below_since") is None:
            t["below_since"] = now
            log.info("STOP TIMER START %s: %s; sells if still at/below the stop for %ds%s", name, d.reason,
                     need, " (live game window)" if g.live else "")
        t["below_seen"] = now
        self.hits[tid] = self.hits.get(tid, 0) + 1
        elapsed = now - t["below_since"]
        if elapsed < need or self.hits[tid] < self.conf.confirm_checks:
            log.info("STOP HIT (waiting to confirm %d/%ds, check %d) %s: %s", int(elapsed), need,
                     self.hits[tid], name, d.reason)
            d.reason = f"{d.reason} (confirming {int(elapsed)}/{need}s)"
            return d
        if t.get("fired_at") is None:
            t["fired_at"] = now
            log.info("STOP TIMER FIRED %s: at/below the stop for %ds (needed %ds%s)", name, int(elapsed),
                     need, ", live game" if g.live else "")

        if d.action == "no_bids":
            log.warning("CANNOT SELL %s: %s", name, d.reason)
            self._notice(tid, "no_bids", f"Stop-loss: {name} hit its stop but nobody is buying. Holding.")
            return d
        if d.action == "below_floor":
            log.warning("NOT SELLING %s: %s", name, d.reason)
            self._notice(tid, "below_floor", f"Stop-loss: {name}: {d.reason}")
            return d

        # action == "sell"
        for w in d.warnings:
            log.warning("%s: %s", name, w)
        if self.dry_run:
            log.info("DRY RUN: would SELL %s shares of %s at >= %s (best bid %s, stop %.4f, est. $%.2f)",
                     d.size, name, d.min_price, d.best_bid, d.stop_level, d.est_proceeds)
            self._notice(tid, "would_sell", f"[dry run] Would sell {d.size} {name} at >= {d.min_price} "
                                            f"(bid {d.best_bid}, stop {d.stop_level:.2f})")
            return d
        if self.geo_blocked:
            log.error("NOT SELLING %s: server is geoblocked", name)
            return d
        t = self._tok(tid)
        last = t.get("last_attempt")
        if last is not None and self.clock() - last < self.conf.retry_cooldown_seconds:
            log.info("sell for %s attempted recently; waiting for cooldown", name)
            return d
        t["last_attempt"] = self.clock()
        self._save_state()   # record the attempt before sending, so a crash can't cause a rapid re-send
        if self.conf.cancel_open_orders_before_stop:
            try:
                self.trader.cancel_orders(tid)
            except Exception as e:
                log.warning("cancel open orders on %s failed: %s", name, type(e).__name__)
            # re-read the exact balance after cancelling
            d.size = self.api.exact_shares(self.addr, tid)
            if d.size <= self.conf.dust_shares:
                log.info("no shares left for %s after cancelling orders; nothing to sell", name)
                return d
        log.info("SELLING %s shares of %s at >= %s (best bid %s, stop %.4f)", d.size, name,
                 d.min_price, d.best_bid, d.stop_level)
        resp = self.trader.sell(tid, d.size, d.min_price)
        ok = getattr(resp, "ok", None)
        info = resp.model_dump() if hasattr(resp, "model_dump") else resp
        if ok:
            log.info("SELL ACCEPTED %s: %s", name, info)
            self.notify(f"Stop-loss SOLD {name}: status {info.get('status')}, "
                        f"shares {info.get('making_amount')}, received ${info.get('taking_amount')}")
        else:
            log.error("SELL REJECTED %s: %s", name, info)
            self._notice(tid, "error", f"Stop-loss sell REJECTED for {name}: {info}")
        return d

    def maybe_reload(self, loader: Callable[[], Config], paths: list):
        """Re-read config.yaml / auto_stops.yaml when they change. A bad file is ignored (old config kept)."""
        sig = tuple((str(x), x.stat().st_mtime_ns if x.exists() else None) for x in paths)
        if getattr(self, "_cfg_sig", None) is None:
            self._cfg_sig = sig
            return False
        if sig == self._cfg_sig:
            return False
        self._cfg_sig = sig
        try:
            new = loader()
        except Exception as e:
            log.error("config changed but is INVALID (%s); keeping the previous settings", e)
            self._notice("_server", "error", f"Stop-loss bot: config change ignored, invalid: {e}")
            return False
        if new.dry_run != self.conf.dry_run:
            log.warning("dry_run changed in config; restart the service for that to take effect")
            new.dry_run = self.conf.dry_run
        self.conf = new
        log.info("config reloaded (%d market override(s), confirm %ds, live %ds)", len(new.markets),
                 new.stop_confirm_sec, new.live_confirm_sec)
        return True

    def loop(self, loader: Optional[Callable[[], Config]] = None, watch: Optional[list] = None):
        log.info("stop-loss bot started for %s (%s), every %ss, confirm %ds (live game %ds, window %.1fh)",
                 self.addr, "DRY RUN" if self.dry_run else "LIVE", self.conf.poll_interval_seconds,
                 self.conf.stop_confirm_sec, self.conf.live_confirm_sec, self.conf.live_window_hours)
        if loader and watch:
            self.maybe_reload(loader, watch)
        if not self.dry_run and self.check_geoblock(force=True):
            log.error("server is geoblocked; the bot keeps watching but will not sell")
        while True:
            start = self.clock()
            if loader and watch:
                try:
                    self.maybe_reload(loader, watch)
                except Exception:
                    log.exception("config reload check failed")
            try:
                ds = self.run_once()
                log.info("checked %d position(s)", len(ds))
            except Exception:
                log.exception("round failed")
            time.sleep(max(5, self.conf.poll_interval_seconds - (self.clock() - start)))
