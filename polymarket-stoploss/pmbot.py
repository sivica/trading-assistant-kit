#!/usr/bin/env python3
"""pmbot - Polymarket stop-loss bot for your own server.

  pmbot setup                 create the bot key + Deposit Wallet, move pUSD in, approvals, API keys
  pmbot set-builder-key       paste a Polymarket Builder API key (only if setup asks for it)
  pmbot address               print the bot addresses (where to send money)
  pmbot balance               POL / pUSD / USDC balances of the bot key and its Deposit Wallet
  pmbot fund [--amount 10]    move pUSD from the bot key's address into the Deposit Wallet
  pmbot fix-approvals         grant missing approvals (e.g. "allowance is not enough") and refresh
  pmbot geoblock              ask Polymarket whether this server may trade
  pmbot status                positions, best bids and stop levels (never trades)
  pmbot run [--dry-run] [--once]   the stop-loss loop (systemd runs this)
  pmbot buy URL --outcome Yes --usd 3 [--max-price 0.65] [--stop 0.45]
                              place a bet (asks to confirm); --stop saves a fixed stop price for it
  pmbot sell URL --outcome Yes [--min-price 0.40]          sell a whole position now (asks to confirm)
  pmbot wrap [--amount 10]    convert USDC.e in the bot wallet into pUSD
  pmbot convert-usdc          convert native USDC in the bot wallet to pUSD via Polymarket's bridge
  pmbot withdraw --to 0x... --amount 10   send pUSD out of the Deposit Wallet (asks to confirm)
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import pmapi  # noqa: E402
from bot import LiveTrader, StopLossBot  # noqa: E402

log = logging.getLogger("pmbot")


def setup_logging(to_file=True, verbose=False):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if to_file:
        cfg.HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(cfg.LOG_FILE, maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(fmt)
        log.addHandler(fh)


def load_env_file():
    """Load TELEGRAM_* etc from ~/.pmbot/bot.env (KEY=value lines)."""
    if cfg.ENV_FILE.exists():
        for line in cfg.ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def confirm(prompt: str, assume_yes=False) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} Type YES to continue: ").strip() == "YES"
    except EOFError:
        return False


def ensure_not_blocked() -> dict:
    g = pmapi.geoblock()
    if g.get("blocked"):
        print(f"\nSTOP: Polymarket says this server may NOT trade (country={g.get('country')}, "
              f"region={g.get('region')}).\nDelete this server and create one in Helsinki (Finland). "
              "Do not use a VPN to get around this.")
        sys.exit(2)
    return g


def fmt(x, nd=2):
    return "-" if x is None else f"{Decimal(x):.{nd}f}"


# ---------------- commands ----------------

def cmd_geoblock(a, conf):
    g = pmapi.geoblock()
    print(json.dumps(g, indent=1))
    print("=> BLOCKED: this server cannot trade" if g.get("blocked") else "=> OK: this server may trade")
    return 2 if g.get("blocked") else 0


def cmd_address(a, conf):
    import wallet
    print("bot key address (send POL for gas here):", wallet.address())
    if conf.use_deposit_wallet:
        dw = wallet.deposit_wallet_address()
        print("Deposit Wallet (holds pUSD + bets):    ", dw or f"{wallet.predicted_deposit_wallet()} (not set up yet)")


def _print_balances(label, addr, b):
    print(f"{label} {addr}")
    for k, v in b.items():
        print(f"  {k:14} {v:.6f}")


def cmd_balance(a, conf):
    import wallet
    rpc = pmapi.Rpc(conf.rpc_urls)
    addr = a.address or wallet.address()
    b = rpc.wallet_balances(addr)
    _print_balances("wallet" if a.address else "bot key", addr, b)
    if not a.address and conf.use_deposit_wallet and wallet.deposit_wallet_address():
        dw = wallet.deposit_wallet_address()
        _print_balances("Deposit Wallet (trading)", dw, rpc.wallet_balances(dw))
    return b


def cmd_setup(a, conf):
    import wallet
    cfg.HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    print("Step 1/6: checking that Polymarket allows trading from this server...")
    g = ensure_not_blocked()
    print(f"  OK (country {g.get('country')}, IP {g.get('ip')})")

    print("Step 2/6: bot key")
    if wallet.has_wallet():
        print("  key already exists (not creating a new one)")
    else:
        wallet.create_wallet()
        print(f"  created a NEW key. Saved to {cfg.KEY_FILE} (readable only by this user).")
        print("  BACK UP that file somewhere safe (see SETUP.md, section 3.2). Never paste it into chats or websites.")
    addr = wallet.address()
    print(f"\n  BOT KEY ADDRESS (Polygon network):  {addr}\n")
    if not cfg.CONFIG_FILE.exists():
        example = Path(__file__).resolve().parent / "config.example.yaml"
        cfg.CONFIG_FILE.write_text(example.read_text())
        print(f"  wrote default settings to {cfg.CONFIG_FILE} (dry_run: true)")

    print("Step 3/6: balances on the bot key address")
    rpc = pmapi.Rpc(conf.rpc_urls)
    b = cmd_balance(argparse.Namespace(address=None), conf)
    if b["USDC.e"] > 0:
        if b["POL"] < Decimal("0.05"):
            print("  USDC.e found but no POL for gas; send ~1 POL to the bot key address, then re-run setup.")
        else:
            print(f"  Found {b['USDC.e']} USDC.e. Polymarket trades with pUSD, so it should be wrapped (1:1).")
            if confirm(f"  Wrap {b['USDC.e']} USDC.e into pUSD now?", a.yes):
                wallet.wrap_usdce(wallet.signer_client(conf), b["USDC.e"])
                print("  wrapped.")
    if b["USDC (native)"] > 0:
        print(f"  NOTE: {b['USDC (native)']} native USDC found. Polymarket needs pUSD. "
              "Run `pmbot convert-usdc` to convert it via Polymarket's bridge (SETUP.md, section 3.2).")

    if not conf.use_deposit_wallet:
        return _setup_eoa_mode(a, conf, b)

    print("Step 4/6: Polymarket API keys + Deposit Wallet (Polymarket's trading wallet, owned by the bot key)")
    if wallet.load_builder_key() is None:
        print("  creating a Builder API key (lets the bot deploy and use the Deposit Wallet without gas)...")
        if not wallet.try_create_builder_key(conf):
            print("\n  ACTION NEEDED: on polymarket.com (your normal account) open Settings -> Builders,"
                  "\n  create an API key, then run:  pmbot set-builder-key"
                  "\n  and paste the Key, Secret and Passphrase when asked (they stay on this server)."
                  "\n  Then run `pmbot setup` again.")
            return 1
    print("  builder key ready (saved to", cfg.BUILDER_KEY_FILE, ")")
    print(f"  deploying / checking the Deposit Wallet (expected {wallet.predicted_deposit_wallet()}) ...")
    c = wallet.deposit_client(conf, deploy=True)
    dw = str(c.wallet)
    print("  API keys ready (saved to", cfg.CREDS_FILE, ")")
    print(f"\n  DEPOSIT WALLET (holds your pUSD and bets):  {dw}\n")

    print("Step 5/6: move pUSD from the bot key address into the Deposit Wallet")
    pusd_eoa = rpc.erc20_balance(pmapi.PUSD, addr)
    if pusd_eoa <= 0:
        print("  nothing to move")
    elif b["POL"] < Decimal("0.02"):
        print(f"  {pusd_eoa} pUSD waiting, but the bot key has no POL for the transfer fee. Send ~1 POL to {addr}.")
    else:
        print(f"  {pusd_eoa} pUSD is on the bot key address {addr}.")
        print(f"  It must sit in the Deposit Wallet {dw} to trade. Costs a fraction of a cent in POL.")
        if confirm(f"  Move {pusd_eoa} pUSD into the Deposit Wallet now?", False):
            wallet.move_pusd_to_deposit_wallet(conf, pusd_eoa)
            print(f"  moved. Deposit Wallet pUSD now: {rpc.erc20_balance(pmapi.PUSD, dw)}")
        else:
            print("  skipped (run `pmbot fund` later)")

    print("Step 6/6: one-time trading approvals for the Deposit Wallet (gasless)")
    try:
        wallet.setup_approvals(c)
        st = c.get_trading_approvals_state()
        missing = getattr(st, "missing", None)
        empty = missing is None or (not getattr(missing, "erc20", ()) and not getattr(missing, "erc1155", ()))
        print("  approvals done" + ("" if empty else f"; still missing (re-run setup in a minute): {missing}"))
        _fix_neg_risk(c, rpc)
    except Exception as e:  # noqa: BLE001
        print(f"  approvals not confirmed yet ({type(e).__name__}: {str(e)[:160]}). Re-run `pmbot setup` in a minute.")
    print(f"\n  pUSD in the Deposit Wallet: {rpc.erc20_balance(pmapi.PUSD, dw):.2f}")
    print("\nSetup complete. Next: `pmbot status`, then `pmbot run --dry-run --once`."
          "\nIf the service is running: sudo systemctl restart pm-stoploss")


def _setup_eoa_mode(a, conf, b):
    """Legacy: trade straight from the bot key (only for EOAs that traded before 2026-05-03)."""
    import wallet
    if b["POL"] < Decimal("0.2"):
        print("\n  Not enough POL for gas yet. Send 1-2 POL on the POLYGON network to the address above.")
        return 1
    print("Step 4/6: Polymarket API keys + one-time trading approvals (EOA mode, uses a little POL gas)")
    c = wallet.signer_client(conf)
    wallet.setup_approvals(c)
    st = c.get_trading_approvals_state()
    print("  approvals state:", getattr(st, "missing", None))
    print("\nSetup complete (EOA mode).")


def _fix_neg_risk(c, rpc) -> int:
    import wallet
    done = wallet.approve_neg_risk_adapter(c, rpc)
    left = wallet.missing_neg_risk_approvals(rpc, str(c.wallet))
    if left:
        print(f"  Neg Risk Adapter approval still missing {left}; wait a minute and run `pmbot fix-approvals`")
        return 1
    print("  Neg Risk Adapter approvals " + ("granted" if done else "already in place") + "; Polymarket allowance refreshed")
    return 0


def cmd_fix_approvals(a, conf):
    """Grant the Neg Risk Adapter approvals the SDK skips (gasless for a Deposit Wallet) and refresh
    Polymarket's cached allowance. Fixes 'allowance is not enough -> spender: 0xd91E80...'."""
    import wallet
    c = wallet.client(conf)
    rpc = pmapi.Rpc(conf.rpc_urls)
    print(f"checking approvals for {c.wallet} ...")
    wallet.setup_approvals(c)
    return _fix_neg_risk(c, rpc)


def cmd_set_builder_key(a, conf):
    """Store a Polymarket Builder API key typed/pasted by the user. Input is hidden, never printed."""
    import getpass
    import wallet
    cfg.HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    print("Paste the values from polymarket.com -> Settings -> Builders -> your API key.")
    print("Nothing is shown while you paste; press Enter after each one.")
    k = getpass.getpass("API Key: ")
    sct = getpass.getpass("Secret: ")
    pp = getpass.getpass("Passphrase: ")
    try:
        wallet.save_builder_key(k, sct, pp)
    except ValueError as e:
        print("not saved:", e); return 1
    print(f"saved to {cfg.BUILDER_KEY_FILE} (readable only by this user). Now run: pmbot setup")


def cmd_fund(a, conf):
    """Move pUSD from the bot key's own address into the Deposit Wallet."""
    import wallet
    if not conf.use_deposit_wallet:
        print("use_deposit_wallet is false in config; nothing to do"); return 1
    rpc = pmapi.Rpc(conf.rpc_urls)
    addr, dw = wallet.address(), wallet.trading_address(conf)
    have = rpc.erc20_balance(pmapi.PUSD, addr)
    amt = Decimal(a.amount) if a.amount else have
    if amt <= 0 or amt > have:
        print(f"the bot key address holds {have} pUSD; nothing to move"); return 1
    if rpc.pol_balance(addr) < Decimal("0.02"):
        print(f"no POL for the fee; send ~1 POL to {addr}"); return 1
    print(f"Move {amt} pUSD from the bot key {addr}\n  into the Deposit Wallet {dw}")
    if confirm("Move it?", False):
        wallet.move_pusd_to_deposit_wallet(conf, amt)
        print(f"done. Deposit Wallet pUSD: {rpc.erc20_balance(pmapi.PUSD, dw)}")


def _rows(decisions):
    out = []
    for d in decisions:
        conf_s = "paused" if d.confirm_sec is None and "live_pause" in d.reason else (
            "-" if d.confirm_sec is None else f"{d.confirm_sec}s")
        out.append([d.title[:44], d.outcome[:8], fmt(d.size), fmt(d.avg_price, 3), fmt(d.best_bid, 3),
                    fmt(d.stop_level, 3), conf_s, d.game or "-", d.action.upper(), d.reason[:70]])
    return out


def a_config_path(a):
    return Path(a.config) if getattr(a, "config", None) else None


def cmd_status(a, conf):
    import wallet
    addr = a.address or wallet.trading_address(conf)
    bot = StopLossBot(conf, addr, state_path=None, notifier=lambda t: None, dry_run=True)
    bot.conf.confirm_checks = 1
    print(f"wallet {addr}   mode in config: {'DRY RUN' if conf.dry_run else 'LIVE'}")
    try:
        g = pmapi.geoblock()
        print(f"server location: {g.get('country')} -> {'BLOCKED' if g.get('blocked') else 'allowed'}")
    except Exception as e:
        print("geoblock check failed:", type(e).__name__)
    logging.getLogger("pmbot").setLevel(logging.ERROR)
    ds = bot.run_once()
    if not ds:
        print("no open positions in this wallet")
        return
    resolved = [d for d in ds if d.action == "skip" and "resolved" in d.reason]
    ds = [d for d in ds if d not in resolved]
    if resolved:
        print(f"({len(resolved)} resolved position(s) hidden; winning ones can be redeemed in the Polymarket app)")
    if not ds:
        print("no open positions to watch")
        return
    hdr = ["market", "side", "shares", "entry", "best bid", "stop", "confirm", "game (local)", "action", "why"]
    rows = [hdr] + _rows(ds)
    w = [max(len(str(r[i])) for r in rows) for i in range(len(hdr))]
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))
    print(f"\n'SELL' means the stop is hit; the bot sells only after the bid stays at/below the stop for the "
          f"'confirm' time\n(default {conf.stop_confirm_sec}s, {conf.live_confirm_sec}s during a live game; "
          f"game = start to +{conf.live_window_hours:g}h).")
    if conf.auto_markets:
        print(f"{len(conf.auto_markets)} stop(s) set by `pmbot buy --stop` in {cfg.auto_path_for(a_config_path(a))}")


def cmd_run(a, conf):
    import wallet
    load_env_file()
    dry = True if a.dry_run else conf.dry_run
    try:
        addr = a.address or wallet.trading_address(conf)
    except RuntimeError as e:
        log.error("%s. The bot is idle until then.", e)
        if not a.once:
            import time
            time.sleep(300)   # systemd restarts us; don't spam the log
        return 1
    if a.address and not dry:
        print("--address is only allowed with --dry-run"); return 2
    trader = None if dry else LiveTrader(lambda: wallet.client(conf))
    bot = StopLossBot(conf, addr, trader=trader, state_path=cfg.STATE_FILE if not a.address else None,
                      dry_run=dry)
    if a.once:
        ds = bot.run_once()
        for d in ds:
            if d.action == "skip" and "resolved" in d.reason:
                continue
            log.info("%s [%s] %s: %s", d.title[:50], d.outcome, d.action.upper(), d.reason)
        return
    cpath = a_config_path(a) or cfg.CONFIG_FILE
    bot.loop(loader=lambda: cfg.load(cpath), watch=[Path(cpath), cfg.auto_path_for(cpath)])


def resolve_token(arg: str, outcome: str | None, want_market: bool = False):
    """Accept a token id, a market URL/slug, or an event URL/slug -> (token_id, title, outcome[, market])."""
    if arg.isdigit():
        r = (arg, arg[:12] + "...", outcome or "?")
        return r + (None,) if want_market else r
    slugs = pmapi.slug_from_url(arg)
    m = None
    for s in slugs:
        m = pmapi.gamma_market_by_slug(s)
        if m:
            break
        ev = pmapi.gamma_event_by_slug(s)
        if ev:
            ms = ev.get("markets") or []
            open_ms = [x for x in ms if not x.get("closed")]
            if len(open_ms) == 1:
                m = open_ms[0]
                break
            print("This event has several markets. Use the URL of the specific market, one of:")
            for x in open_ms[:40]:
                print("  ", x.get("slug"), "-", x.get("question"))
            sys.exit(1)
    if not m:
        print("market not found:", arg); sys.exit(1)
    outs = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
    toks = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
    if not outcome:
        print("add --outcome, one of:", outs); sys.exit(1)
    for o, t in zip(outs, toks):
        if o.lower() == outcome.lower():
            if m.get("closed") or not m.get("acceptingOrders", True):
                print("this market is closed / not accepting orders"); sys.exit(1)
            r = (t, m.get("question", ""), o)
            return r + (m,) if want_market else r
    print(f"outcome {outcome!r} not in {outs}"); sys.exit(1)


def cmd_buy(a, conf):
    import wallet
    from stoplogic import parse_book
    usd = Decimal(a.usd)
    if usd > conf.max_buy_usd:
        print(f"${usd} is above max_buy_usd (${conf.max_buy_usd}) in config.yaml. Keep bets small."); return 1
    tid, title, outcome, m = resolve_token(a.market, a.outcome, want_market=True)
    stop_abs = None
    if getattr(a, "stop", None) is not None:
        try:
            stop_abs = Decimal(str(a.stop))
        except Exception:
            print(f"--stop must be a price like 0.45 (got {a.stop!r})"); return 1
    taddr = wallet.trading_address(conf)
    cash = pmapi.Rpc(conf.rpc_urls).erc20_balance(pmapi.PUSD, taddr)
    if cash < usd:
        print(f"only {cash} pUSD in the trading wallet {taddr}"
              + ("; run `pmbot fund` first" if conf.use_deposit_wallet else "")); return 1
    bids, asks, tick, min_size = parse_book(pmapi.book(tid))
    if not asks:
        print("nobody is selling this outcome right now"); return 1
    best_ask = asks[0][0]
    max_price = Decimal(a.max_price) if a.max_price else best_ask
    shares = usd / best_ask
    ppos = position_stub(tid, outcome, m, title)
    entry = None
    if stop_abs is not None:
        if not (Decimal("0.01") <= stop_abs < max_price):
            print(f"--stop {stop_abs} must be at least 0.01 and below the max price {max_price}"); return 1
        if bids and stop_abs >= bids[0][0]:
            print(f"--stop {stop_abs} is at/above the best bid {bids[0][0]}: the bot would sell right away. "
                  "Pick a lower stop."); return 1
        entry = stop_entry(stop_abs, outcome, m, title)
        tmp = cfg.Config(**{k: getattr(conf, k) for k in conf.__dataclass_fields__})
        tmp.markets = dict(conf.markets)
        if tid in tmp.markets and tid not in conf.auto_markets:
            print(f"  note: config.yaml already has an entry for this token; it wins over --stop")
        else:
            tmp.markets[tid] = entry
        rule = tmp.rule_for(ppos)
    else:
        rule = conf.rule_for(ppos)
    stop = rule.stop_level(best_ask) if rule.enabled else None
    held = None
    try:
        held = pmapi.Rpc(conf.rpc_urls).exact_shares(taddr, tid)
    except Exception:
        pass
    import games
    from datetime import datetime, timezone
    g = games.classify(m, datetime.now(timezone.utc), conf.live_window_hours) if m else None
    game = g.label() if g else ""
    if not rule.enabled:
        stop_line = f"  stop-loss: OFF for this market ({rule.matched_by})"
    else:
        src = "--stop" if stop_abs is not None and "override" in rule.matched_by and tid in rule.matched_by \
            else rule.matched_by
        stop_line = (f"  stop-loss would sell if the bid drops to ~{fmt(stop, 3)}  (rule: {src}"
                     + (f"; computed from entry ~{best_ask}, uses your average entry after buying" if stop_abs is None else "")
                     + ")\n"
                     f"  confirm: bid must stay at/below the stop for {rule.confirm_sec}s"
                     f" ({rule.live_confirm_sec}s during a live game"
                     + ("; PAUSED during the game" if rule.live_pause else "") + ")"
                     + (f"\n  game: {game}" if game else ""))
        if held and held > conf.dust_shares and stop_abs is None:
            stop_line += f"\n  you already hold {held:.2f} shares; the stop uses the new average entry"
    print(f"\nBUY  {title}  [{outcome}]\n  spend up to ${usd} pUSD, pay at most {max_price} per share "
          f"(best ask now {best_ask}, best bid {bids[0][0] if bids else '-'})\n"
          f"  ~{shares:.2f} shares; pays ${shares:.2f} if {outcome} wins\n" + stop_line)
    if shares < min_size:
        print(f"  STOP: under the {min_size}-share minimum for this market; Polymarket would reject it."
              f" Use at least ${(min_size * best_ask + Decimal('0.25')).quantize(Decimal('0.01'))}.")
        return 1
    ensure_not_blocked()
    if not confirm("Place this bet with REAL money?", False):
        print("cancelled"); return 1
    apath = cfg.auto_path_for(a_config_path(a))
    prev = conf.auto_markets.get(tid)
    if entry is not None:
        # saved BEFORE the order so a crash right after a fill can't leave the bet on the default stop
        cfg.save_auto_market(tid, entry, apath)
        print(f"  saved stop {stop_abs} for this outcome in {apath} (the running bot picks it up within a minute)")
    try:
        r = wallet.client(conf).place_market_order(token_id=tid, side="BUY", amount=str(usd),
                                                   max_price=str(max_price), order_type="FAK")
    except Exception:
        if entry is not None:
            cfg.save_auto_market(tid, prev, apath)
            print("  order failed; saved stop removed again")
        raise
    print("result:", wallet.order_summary(r))
    if entry is not None and getattr(r, "ok", True) is False:
        cfg.save_auto_market(tid, prev, apath)
        print("  order not accepted; saved stop removed again")


def position_stub(tid, outcome, m, title=""):
    """What the data-api position will look like, so rule_for() matches slug overrides too."""
    ev = ((m or {}).get("events") or [{}])[0] or {}
    return {"asset": tid, "outcome": outcome, "title": title, "slug": (m or {}).get("slug"),
            "eventSlug": ev.get("slug")}


def stop_entry(stop: Decimal, outcome: str, m, title: str) -> dict:
    from datetime import datetime, timezone
    return {"outcome": outcome, "stop_price": float(stop), "stop_drop": None, "stop_pct": None,
            "note": f"pmbot buy --stop {stop} on {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z: "
                    f"{(m or {}).get('slug') or title}"[:200]}


def cmd_sell(a, conf):
    import wallet
    from stoplogic import parse_book, round_up_to_tick
    tid, title, outcome = resolve_token(a.market, a.outcome)
    addr = wallet.trading_address(conf)
    shares = pmapi.Rpc(conf.rpc_urls).exact_shares(addr, tid)
    if shares <= 0:
        print("the bot wallet holds no shares of that outcome"); return 1
    bids, _, tick, _ = parse_book(pmapi.book(tid))
    if not bids:
        print("nobody is buying right now"); return 1
    minp = round_up_to_tick(Decimal(a.min_price), tick) if a.min_price else bids[0][0]
    print(f"\nSELL ALL {shares} shares of {title} [{outcome}] at >= {minp} (best bid {bids[0][0]})")
    ensure_not_blocked()
    if not confirm("Sell now?", False):
        print("cancelled"); return 1
    r = wallet.client(conf).place_market_order(token_id=tid, side="SELL", shares=str(shares),
                                               min_price=str(minp), order_type="FAK")
    print("result:", wallet.order_summary(r))


def cmd_wrap(a, conf):
    import wallet
    have = pmapi.Rpc(conf.rpc_urls).erc20_balance(pmapi.USDCE, wallet.address())
    amt = Decimal(a.amount) if a.amount else have
    if amt <= 0 or amt > have:
        print(f"USDC.e balance is {have}; nothing to wrap"); return 1
    if confirm(f"Wrap {amt} USDC.e into pUSD?", a.yes):
        wallet.wrap_usdce(wallet.signer_client(conf), amt)
        print("done" + (" (now run `pmbot fund` to move it into the Deposit Wallet)" if conf.use_deposit_wallet else ""))


def cmd_convert_usdc(a, conf):
    """Send native USDC to the bot wallet's own Polymarket bridge deposit address; the bridge
    converts it to pUSD and credits it back to the same wallet (docs.polymarket.com/trading/bridge/deposit)."""
    import wallet
    addr = wallet.address()
    have = pmapi.Rpc(conf.rpc_urls).erc20_balance(pmapi.USDC_NATIVE, addr)
    if have < 2:
        print(f"native USDC balance is {have}; the bridge minimum is about $2"); return 1
    # credit the trading wallet directly (the Deposit Wallet once set up)
    target = wallet.deposit_wallet_address() if conf.use_deposit_wallet else None
    target = target or addr
    r = pmapi.http_json("https://bridge.polymarket.com/deposit", data={"address": target})
    dep = (r.get("address") or {}).get("evm")
    if not dep:
        print("bridge did not return a deposit address:", r); return 1
    print(f"Convert {have} native USDC -> pUSD for {target}\n  via Polymarket bridge deposit address {dep}"
          "\n  (arrives as pUSD in that wallet, usually within minutes)")
    if confirm("Send it?", False):
        c = wallet.signer_client(conf)
        c.transfer_erc20(token_address=pmapi.USDC_NATIVE, recipient_address=dep,
                         amount=int(have * 10**6)).wait()
        print("sent; check with `pmbot balance` in a few minutes")


def cmd_withdraw(a, conf):
    import wallet
    from eth_utils import is_address, to_checksum_address
    if not is_address(a.to):
        print("not a valid 0x address"); return 1
    amt = Decimal(a.amount)
    from_signer = a.from_signer or not conf.use_deposit_wallet
    src = wallet.address() if from_signer else wallet.trading_address(conf)
    have = pmapi.Rpc(conf.rpc_urls).erc20_balance(pmapi.PUSD, src)
    if amt > have:
        print(f"only {have} pUSD in {src}"); return 1
    to = to_checksum_address(a.to)
    print(f"Send {amt} pUSD (Polygon) from {src} to {to}")
    print("Double-check the address. Crypto transfers cannot be undone.")
    if not from_signer and wallet.load_builder_key() is None:
        print("no builder key; run `pmbot setup` first"); return 1
    if confirm("Send?", False):
        c = wallet.signer_client(conf) if from_signer else wallet.deposit_client(conf)
        wallet.wait_tx(c.transfer_erc20(token_address=pmapi.PUSD, recipient_address=to,
                                        amount=int(amt * 10**6)), "withdrawal")
        print("sent; check with `pmbot balance`")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pmbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml (default ~/.pmbot/config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--yes", action="store_true", help="don't ask before wrapping")
    sub.add_parser("address")
    sub.add_parser("set-builder-key")
    sub.add_parser("fix-approvals")
    s = sub.add_parser("fund"); s.add_argument("--amount")
    s = sub.add_parser("balance"); s.add_argument("--address")
    sub.add_parser("geoblock")
    s = sub.add_parser("status"); s.add_argument("--address", help="look at another wallet (read-only)")
    s = sub.add_parser("run"); s.add_argument("--dry-run", action="store_true"); s.add_argument("--once", action="store_true")
    s.add_argument("--address", help="(dry run only) watch another wallet")
    s = sub.add_parser("buy"); s.add_argument("market", help="polymarket.com market URL, slug, or token id")
    s.add_argument("--outcome"); s.add_argument("--usd", required=True); s.add_argument("--max-price")
    s.add_argument("--stop", help="fixed stop price for this bet, e.g. 0.45 (saved for the stop-loss bot)")
    s = sub.add_parser("sell"); s.add_argument("market"); s.add_argument("--outcome"); s.add_argument("--min-price")
    s = sub.add_parser("wrap"); s.add_argument("--amount"); s.add_argument("--yes", action="store_true")
    sub.add_parser("convert-usdc")
    s = sub.add_parser("withdraw"); s.add_argument("--to", required=True); s.add_argument("--amount", required=True)
    s.add_argument("--from-signer", action="store_true", help="send from the bot key address instead of the Deposit Wallet")
    a = ap.parse_args(argv)
    setup_logging(to_file=a.cmd == "run" and not getattr(a, "address", None), verbose=a.verbose)
    conf = cfg.load(Path(a.config) if a.config else None)
    fn = globals()["cmd_" + a.cmd.replace("-", "_")]
    try:
        rc = fn(a, conf)
    except KeyboardInterrupt:
        print("\nstopped"); rc = 130
    except RuntimeError as e:
        if a.verbose:
            raise
        print("error:", e); rc = 1
    except Exception as e:  # noqa: BLE001
        if "allowance is not enough" in str(e):
            print("Polymarket refused: missing approval. Nothing was spent.\n"
                  "Fix it with:  sudo -u pmbot pmbot fix-approvals   then try again.\n", str(e)[:300])
            return 1
        raise
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
