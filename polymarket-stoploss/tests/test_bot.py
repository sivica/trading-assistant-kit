"""Offline tests: stop logic, config, and the runner with fake API/trader. No network, no keys."""
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg
from bot import StopLossBot
from stoplogic import Rule, evaluate, round_up_to_tick

TOKEN = "123456789"
# Public, well-known test address (Hardhat/Anvil account #0) and its derived Deposit Wallet.
TEST_EOA = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TEST_DEPOSIT_WALLET = "0xBc0fF067b7740Eff76C1ca93c875Ba6B890d6B50"


def pos(avg="0.86", size="5.81395", **kw):
    p = {"asset": TOKEN, "title": "Example market?", "outcome": "No", "avgPrice": float(avg),
         "size": float(size), "redeemable": False, "slug": "example-market", "eventSlug": "example-event"}
    p.update(kw)
    return p


def book(bids=(), asks=(), tick="0.01", min_size="5"):
    return {"bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
            "tick_size": tick, "min_order_size": min_size}


class StopLogicTests(unittest.TestCase):
    def test_hold_above_stop(self):
        d = evaluate(pos(), book([("0.70", "100")], [("0.74", "50")]), Rule())
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.stop_level, D("0.66"))

    def test_sell_at_stop_exact_size(self):
        d = evaluate(pos(exact_size=D("5.81395")), book([("0.66", "3"), ("0.65", "100")]), Rule())
        self.assertEqual(d.action, "sell")
        self.assertEqual(d.size, D("5.81395"))       # exact fractional size kept
        self.assertEqual(d.min_price, D("0.64"))     # bid - 2c slippage
        self.assertEqual(d.fillable_shares, D("5.81395"))

    def test_book_order_independent(self):
        # CLOB returns bids ascending; we must find the true best bid
        d = evaluate(pos(), book([("0.10", "5"), ("0.50", "5"), ("0.60", "5")]), Rule())
        self.assertEqual(d.best_bid, D("0.60"))

    def test_floor_blocks_sale(self):
        d = evaluate(pos(), book([("0.04", "100")]), Rule(min_sell_price=D("0.05")))
        self.assertEqual(d.action, "below_floor")
        self.assertIsNone(d.min_price)

    def test_slippage_never_below_floor(self):
        d = evaluate(pos(), book([("0.06", "100")]), Rule(min_sell_price=D("0.05"), max_slippage=D("0.05")))
        self.assertEqual(d.action, "sell")
        self.assertEqual(d.min_price, D("0.05"))

    def test_tick_rounding_up(self):
        self.assertEqual(round_up_to_tick(D("0.6450"), D("0.01")), D("0.65"))
        self.assertEqual(round_up_to_tick(D("0.645"), D("0.001")), D("0.645"))
        d = evaluate(pos(), book([("0.655", "100")], tick="0.001"), Rule(max_slippage=D("0.0125")))
        self.assertEqual(d.min_price, D("0.643"))

    def test_no_bids(self):
        d = evaluate(pos(), book([], [("0.9", "5")]), Rule())
        self.assertEqual(d.action, "no_bids")

    def test_resolved_and_closed_skipped(self):
        self.assertEqual(evaluate(pos(redeemable=True), book([("0.1", "9")]), Rule()).action, "skip")
        self.assertEqual(evaluate(pos(), None, Rule()).action, "skip")

    def test_cheap_entry_has_no_fixed_drop_stop(self):
        d = evaluate(pos(avg="0.15"), book([("0.01", "100")]), Rule())
        self.assertEqual(d.action, "hold")
        d = evaluate(pos(avg="0.15"), book([("0.07", "100")]), Rule(stop_pct=D("0.5")))
        self.assertEqual(d.action, "sell")

    def test_stop_price_or_drop_whichever_higher(self):
        r = Rule(stop_drop=D("0.20"), stop_price=D("0.75"))
        d = evaluate(pos(), book([("0.74", "100")]), r)
        self.assertEqual(d.action, "sell")
        self.assertEqual(d.stop_level, D("0.75"))

    def test_partial_liquidity_and_min_size_warnings(self):
        d = evaluate(pos(size="3", exact_size=D("3")), book([("0.60", "1"), ("0.30", "100")]), Rule())
        self.assertEqual(d.action, "sell")
        self.assertEqual(d.fillable_shares, D("1"))
        self.assertTrue(any("minimum order size" in w for w in d.warnings))
        self.assertTrue(any("can fill" in w for w in d.warnings))

    def test_disabled(self):
        self.assertEqual(evaluate(pos(), book([("0.1", "9")]), Rule(enabled=False)).action, "skip")


class ConfigTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(text); f.close()
        return Path(f.name)

    def test_example_config_loads_and_is_dry_run(self):
        c = cfg.load(Path(__file__).resolve().parents[1] / "config.example.yaml")
        self.assertTrue(c.dry_run)
        self.assertEqual(c.default.stop_drop, D("0.20"))
        self.assertEqual(c.poll_interval_seconds, 60)

    def test_missing_config_defaults_dry_run(self):
        self.assertTrue(cfg.load(Path("/nonexistent/x.yaml")).dry_run)

    def test_overrides_precedence(self):
        c = cfg.load(self.write("""
default: {stop_drop: 0.20}
markets:
  example-event: {stop_price: 0.70, min_sell_price: 0.30}
  example-market: {outcome: "Yes", stop_price: 0.10}
  "123456789": {max_slippage: 0.0}
"""))
        r = c.rule_for(pos())
        self.assertEqual(r.stop_price, D("0.70"))          # event slug applies; market slug filtered by outcome
        self.assertEqual(r.min_sell_price, D("0.30"))
        self.assertEqual(r.max_slippage, D("0"))           # token override applied last
        self.assertIn("123456789", r.matched_by)

    def test_disable_market_and_null_drop(self):
        c = cfg.load(self.write("markets:\n  example-market: {enabled: false}\n  other: {stop_drop: null}\n"))
        self.assertFalse(c.rule_for(pos()).enabled)
        r = c.rule_for(pos(slug="other", eventSlug="x"))
        self.assertIsNone(r.stop_drop)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            cfg.load(self.write("default: {stop_drop: 20}\n"))     # cents typed as dollars
        with self.assertRaises(ValueError):
            cfg.load(self.write("dry_run: \"no\"\n"))
        with self.assertRaises(ValueError):
            cfg.load(self.write("markets:\n  x: {stop: 0.5}\n"))
        with self.assertRaises(ValueError):
            cfg.load(self.write("poll_interval_seconds: 1\n"))


class FakeApi:
    def __init__(self, positions, books, shares, blocked=False):
        self._p, self._b, self._s, self.blocked = positions, books, shares, blocked

    def positions(self, addr): return self._p
    def book(self, tid): return self._b.get(tid)
    def exact_shares(self, addr, tid): return self._s.get(tid, D(0))
    def geoblock(self): return {"blocked": self.blocked, "country": "FI" if not self.blocked else "DE"}


class Resp:
    ok = True
    def model_dump(self): return {"status": "matched", "making_amount": "5.81", "taking_amount": "3.75"}


class FakeTrader:
    def __init__(self, api):
        self.api, self.sells, self.cancels = api, [], []

    def cancel_orders(self, tid): self.cancels.append(tid)

    def sell(self, tid, shares, min_price):
        self.sells.append((tid, shares, min_price))
        self.api._s[tid] = D(0)   # filled
        return Resp()


class RunnerTests(unittest.TestCase):
    def make(self, bid="0.60", dry=False, confirm=2, blocked=False):
        api = FakeApi([pos()], {TOKEN: book([(bid, "100")])}, {TOKEN: D("5.81395")}, blocked)
        tr = FakeTrader(api)
        c = cfg.Config(); c.dry_run = dry; c.confirm_checks = confirm
        c.stop_confirm_sec = 0          # these older tests check the per-round counter only
        self.now = 1000.0
        self.msgs = []
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        b = StopLossBot(c, "0xabc", api=api, trader=None if dry else tr, state_path=tmp,
                        notifier=self.msgs.append, clock=lambda: self.now)
        return b, api, tr

    def test_confirm_checks_then_single_sell(self):
        b, api, tr = self.make()
        b.run_once()
        self.assertEqual(tr.sells, [])                 # 1st hit: wait to confirm
        b.run_once()
        self.assertEqual(len(tr.sells), 1)
        tid, shares, minp = tr.sells[0]
        self.assertEqual((shares, minp), (D("5.81395"), D("0.58")))
        self.assertEqual(tr.cancels, [TOKEN])
        # data-api still shows the position, but on-chain balance is 0 -> no repeat sell
        self.now += 1000
        b.run_once(); b.run_once()
        self.assertEqual(len(tr.sells), 1)
        self.assertTrue(any("SOLD" in m for m in self.msgs))

    def test_flicker_resets_counter(self):
        b, api, tr = self.make()
        b.run_once()
        api._b[TOKEN] = book([("0.80", "100")])
        b.run_once()
        api._b[TOKEN] = book([("0.60", "100")])
        b.run_once()
        self.assertEqual(tr.sells, [])

    def test_dry_run_never_trades(self):
        b, api, tr = self.make(dry=True, confirm=1)
        for _ in range(3):
            b.run_once()
        self.assertEqual(tr.sells, [])
        self.assertEqual(len([m for m in self.msgs if "dry run" in m]), 1)   # deduped notice

    def test_geoblocked_pauses_sells(self):
        b, api, tr = self.make(confirm=1, blocked=True)
        b.run_once()
        self.assertEqual(tr.sells, [])
        self.assertTrue(any("blocks this server" in m for m in self.msgs))

    def test_cooldown_after_failed_fill(self):
        b, api, tr = self.make(confirm=1)
        def no_fill(tid, shares, minp):
            tr.sells.append((tid, shares, minp))
            class R:
                ok = False
                def model_dump(self): return {"code": "fak_not_filled"}
            return R()
        tr.sell = no_fill
        b.run_once(); b.run_once()
        self.assertEqual(len(tr.sells), 1)               # within cooldown
        self.now += 121
        b.run_once()
        self.assertEqual(len(tr.sells), 2)

    def test_hold_no_trade(self):
        b, api, tr = self.make(bid="0.80", confirm=1)
        ds = b.run_once()
        self.assertEqual(ds[0].action, "hold")
        self.assertEqual(tr.sells, [])

    def test_live_requires_trader(self):
        with self.assertRaises(RuntimeError):
            StopLossBot(cfg.Config(dry_run=False), "0x1", api=FakeApi([], {}, {}))


class WalletFileTests(unittest.TestCase):
    def test_wallet_created_with_private_permissions_and_not_overwritten(self):
        import importlib
        home = Path(tempfile.mkdtemp()) / "pmbot"
        os.environ["PMBOT_HOME"] = str(home)
        importlib.reload(cfg)
        import wallet
        importlib.reload(wallet)
        addr = wallet.create_wallet()
        self.assertTrue(addr.startswith("0x") and len(addr) == 42)
        self.assertEqual(os.stat(cfg.KEY_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
        self.assertEqual(wallet.address(), addr)
        with self.assertRaises(RuntimeError):
            wallet.create_wallet()
        del os.environ["PMBOT_HOME"]
        importlib.reload(cfg)


class DepositWalletTests(unittest.TestCase):
    """Deposit Wallet plumbing (offline): addresses, files, permissions, config switch."""
    def setUp(self):
        import importlib
        self.home = Path(tempfile.mkdtemp()) / "pmbot"
        os.environ["PMBOT_HOME"] = str(self.home)
        importlib.reload(cfg)
        import wallet
        self.wallet = importlib.reload(wallet)

    def tearDown(self):
        import importlib
        del os.environ["PMBOT_HOME"]
        importlib.reload(cfg)

    def test_predicted_deposit_wallet_matches_sdk(self):
        self.home.mkdir(parents=True)
        cfg.ADDRESS_FILE.write_text(TEST_EOA + "\n")   # public Hardhat/Anvil test account #0, never holds real funds
        # value from polymarket-client 0.14.0 beacon derivation (Polymarket production deposit-wallet factory)
        self.assertEqual(self.wallet.predicted_deposit_wallet(), TEST_DEPOSIT_WALLET)

    def test_trading_address_modes(self):
        self.wallet.create_wallet()
        c = cfg.Config()
        self.assertTrue(c.use_deposit_wallet)
        with self.assertRaises(RuntimeError):
            self.wallet.trading_address(c)                  # not set up yet -> never falls back to EOA
        cfg.DEPOSIT_WALLET_FILE.write_text(TEST_DEPOSIT_WALLET + "\n")
        self.assertEqual(self.wallet.trading_address(c), TEST_DEPOSIT_WALLET)
        c.use_deposit_wallet = False
        self.assertEqual(self.wallet.trading_address(c), self.wallet.address())

    def test_builder_key_saved_private_and_roundtrips(self):
        self.assertIsNone(self.wallet.load_builder_key())
        self.wallet.save_builder_key(" k1 ", "c2VjcmV0", "pp\n")
        self.assertEqual(os.stat(cfg.BUILDER_KEY_FILE).st_mode & 0o777, 0o600)
        b = self.wallet.load_builder_key()
        self.assertEqual((b.key, b.secret, b.passphrase), ("k1", "c2VjcmV0", "pp"))
        self.assertNotIn("k1", repr(b))                     # SDK redacts it in logs
        self.wallet.save_builder_key("k2", "s", "p")        # replace keeps 0600
        self.assertEqual(self.wallet.load_builder_key().key, "k2")
        self.assertEqual(os.stat(cfg.BUILDER_KEY_FILE).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            self.wallet.save_builder_key("", "s", "p")

    def test_config_switch(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write("use_deposit_wallet: false\nrpc_urls: [\"https://polygon.drpc.org\"]\n"); f.close()
        c = cfg.load(Path(f.name))
        self.assertFalse(c.use_deposit_wallet)
        self.assertEqual(c.rpc_urls, ["https://polygon.drpc.org"])
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write("use_deposit_wallet: \"yes\"\n"); f.close()
        with self.assertRaises(ValueError):
            cfg.load(Path(f.name))


class BuyGuardTests(unittest.TestCase):
    """`pmbot buy` refuses before ever creating a trading client."""
    def setUp(self):
        import pmbot, pmapi, wallet
        self.pmbot, self.pmapi, self.wallet = pmbot, pmapi, wallet
        self.saved = (pmapi.Rpc, pmapi.book, pmbot.resolve_token, wallet.trading_address, wallet.client,
                      pmbot.ensure_not_blocked, pmbot.confirm)
        test = self
        class R:
            def __init__(self, urls): pass
            def erc20_balance(self, token, addr): return test.cash
        pmapi.Rpc = R
        pmapi.book = lambda tid: book([("0.69", "100")], [("0.70", "100")], min_size="5")
        pmbot.resolve_token = lambda m, o, want_market=False: ("1", "Example market", "Yes") + ((None,) if want_market else ())
        wallet.trading_address = lambda conf: "0xDW"
        def boom(conf): raise AssertionError("client must not be created")
        wallet.client = boom
        pmbot.ensure_not_blocked = lambda: {}
        pmbot.confirm = lambda *a, **k: False
        self.cash = D("21")

    def tearDown(self):
        (self.pmapi.Rpc, self.pmapi.book, self.pmbot.resolve_token, self.wallet.trading_address,
         self.wallet.client, self.pmbot.ensure_not_blocked, self.pmbot.confirm) = self.saved

    def buy(self, usd):
        import argparse
        a = argparse.Namespace(market="x", outcome="Yes", usd=usd, max_price=None, stop=None)
        return self.pmbot.cmd_buy(a, cfg.Config())

    def test_cap(self):
        self.assertEqual(self.buy("6"), 1)

    def test_under_min_shares_refused(self):
        self.assertEqual(self.buy("3"), 1)                 # 4.28 shares < 5

    def test_not_enough_cash(self):
        self.cash = D("2")
        self.assertEqual(self.buy("4"), 1)

    def test_declined_confirmation_places_nothing(self):
        self.assertEqual(self.buy("4"), 1)                 # confirm() -> False, client never built


class NegRiskApprovalTests(unittest.TestCase):
    """CTF neg-risk markets need pUSD + CTF approvals for the Neg Risk Adapter (SDK skips them)."""
    class Rpc:
        def __init__(self, allowance, approved):
            self.allowance, self.approved = allowance, approved
        def erc20_allowance(self, token, owner, spender): return self.allowance
        def is_approved_for_all(self, token, owner, op): return self.approved

    class Client:
        wallet = TEST_DEPOSIT_WALLET
        wallet_type = "DEPOSIT_WALLET"
        def __init__(self, rpc):
            self.rpc, self.batches, self.refreshes = rpc, [], []
            client = self
            class Clob:
                def get_bytes(self, path, params=None): client.refreshes.append((path, params)); return b""
            class Ctx: secure_clob = Clob()
            self._ctx = Ctx()
        def execute_transaction(self, calls, metadata=None):
            self.batches.append(list(calls))
            rpc = self.rpc
            class H:
                def wait(self):
                    rpc.allowance, rpc.approved = 2**256 - 1, True
            return H()

    def test_grants_both_in_one_gasless_batch_and_refreshes(self):
        import pmapi, wallet
        rpc = self.Rpc(0, False)
        c = self.Client(rpc)
        done = wallet.approve_neg_risk_adapter(c, rpc)
        self.assertEqual([k for k, _ in done], ["erc20", "erc1155"])
        self.assertEqual(len(c.batches), 1)
        erc20, erc1155 = c.batches[0]
        spender = pmapi.NEG_RISK_ADAPTER[2:].lower()
        self.assertEqual(str(erc20.to).lower(), pmapi.PUSD.lower())
        self.assertTrue(str(erc20.data).startswith("0x095ea7b3") and spender in str(erc20.data).lower())
        self.assertEqual(str(erc1155.to).lower(), pmapi.CTF.lower())
        self.assertTrue(str(erc1155.data).startswith("0xa22cb465") and spender in str(erc1155.data).lower())
        self.assertEqual(c.refreshes[0][0], "/balance-allowance/update")
        self.assertEqual(c.refreshes[0][1], {"asset_type": "COLLATERAL", "signature_type": 3})
        self.assertEqual(wallet.missing_neg_risk_approvals(rpc, c.wallet), [])

    def test_nothing_sent_when_already_approved(self):
        import wallet
        rpc = self.Rpc(2**256 - 1, True)
        c = self.Client(rpc)
        self.assertEqual(wallet.approve_neg_risk_adapter(c, rpc), [])
        self.assertEqual(c.batches, [])
        self.assertEqual(len(c.refreshes), 1)       # still refreshes the CLOB cache


# ---------------- new: confirm timer, live games, --stop ----------------
from datetime import datetime, timezone
import games
from zoneinfo import ZoneInfo
games.LOCAL_TZ = ZoneInfo("Etc/GMT-1")   # pin UTC+1 so the expected strings are stable

KICKOFF = datetime(2026, 10, 31, 17, 30, tzinfo=timezone.utc).timestamp()   # 18:30 at UTC+1 (LOCAL_TZ pinned below)
GAME_MARKET = {"slug": "example-market", "closed": False, "gameStartTime": "2026-10-31 17:30:00+00",
              "sportsMarketType": "moneyline",
              "events": [{"slug": "example-match-2026-10-31", "startTime": "2026-10-31T17:30:00Z", "gameId": 1}]}
POLITICS_MARKET = {"slug": "example-politics", "closed": False,
                 "events": [{"slug": "which-party", "startTime": "2026-11-03T12:00:00Z"}]}


class MarketApi(FakeApi):
    def __init__(self, *a, market=None, fail=False, **k):
        super().__init__(*a, **k)
        self.m, self.fail, self.calls = market, fail, 0

    def market(self, slug):
        self.calls += 1
        if self.fail:
            raise OSError("gamma down")
        return self.m


class ConfirmTimerTests(unittest.TestCase):
    def make(self, bid="0.60", market=None, fail=False, start=KICKOFF - 3600, **conf):
        api = MarketApi([pos()], {TOKEN: book([(bid, "100")])}, {TOKEN: D("5.81395")}, market=market, fail=fail)
        tr = FakeTrader(api)
        c = cfg.Config(dry_run=False)
        for k, v in conf.items():
            setattr(c, k, v)
        self.now = start
        b = StopLossBot(c, "0xabc", api=api, trader=tr, state_path=Path(tempfile.mkdtemp()) / "s.json",
                        notifier=lambda t: None, clock=lambda: self.now)
        return b, api, tr

    def step(self, b, secs=60):
        b.run_once(); self.now += secs

    def test_defaults(self):
        c = cfg.Config()
        self.assertEqual((c.stop_confirm_sec, c.live_confirm_sec, c.live_window_hours), (180, 600, 2.5))

    def test_sustained_dip_sells_after_180s(self):
        b, api, tr = self.make()
        with self.assertLogs("pmbot", "INFO") as lg:
            for _ in range(3):                  # t = 0, 60, 120
                self.step(b)
            self.assertEqual(tr.sells, [])
            self.step(b)                        # t = 180
        self.assertEqual(len(tr.sells), 1)
        out = "\n".join(lg.output)
        self.assertIn("STOP TIMER START", out)
        self.assertIn("STOP TIMER FIRED", out)

    def test_dip_that_recovers_never_sells(self):
        b, api, tr = self.make()
        self.step(b); self.step(b)              # 0, 60 below
        api._b[TOKEN] = book([("0.80", "100")])
        with self.assertLogs("pmbot", "INFO") as lg:
            self.step(b)                        # 120 recovered -> reset
        self.assertIn("STOP TIMER RESET", "\n".join(lg.output))
        api._b[TOKEN] = book([("0.60", "100")])
        for _ in range(3):                      # 180, 240, 300: only 120s since the new dip began
            self.step(b)
        self.assertEqual(tr.sells, [])
        self.step(b)                            # 360: 180s continuous
        self.assertEqual(len(tr.sells), 1)

    def test_live_game_uses_longer_window(self):
        b, api, tr = self.make(market=GAME_MARKET, start=KICKOFF + 20 * 60)
        for _ in range(10):                     # 0..540s during the game
            self.step(b)
        self.assertEqual(tr.sells, [])
        ds = b.run_once()                       # 600s
        self.assertEqual(len(tr.sells), 1)
        self.assertEqual(ds[0].confirm_sec, 600)
        self.assertIn("LIVE", ds[0].game)

    def test_goal_dip_recovers_during_live_game(self):
        b, api, tr = self.make(market=GAME_MARKET, start=KICKOFF + 30 * 60)
        for _ in range(8):                      # 7 minutes below the stop
            self.step(b)
        api._b[TOKEN] = book([("0.85", "100")])
        self.step(b)
        api._b[TOKEN] = book([("0.60", "100")])
        for _ in range(9):
            self.step(b)
        self.assertEqual(tr.sells, [])

    def test_before_and_after_game_use_normal_window(self):
        b, api, tr = self.make(market=GAME_MARKET, start=KICKOFF - 2 * 3600)
        for _ in range(4):
            self.step(b)
        self.assertEqual(len(tr.sells), 1)
        b, api, tr = self.make(market=GAME_MARKET, start=KICKOFF + 2.6 * 3600)
        for _ in range(4):
            self.step(b)
        self.assertEqual(len(tr.sells), 1)

    def test_unknown_start_falls_back_to_normal_and_logs(self):
        b, api, tr = self.make(fail=True, start=KICKOFF + 600)
        with self.assertLogs("pmbot", "WARNING") as lg:
            for _ in range(4):
                self.step(b)
        self.assertEqual(len(tr.sells), 1)
        self.assertIn("GAME TIME UNKNOWN", "\n".join(lg.output))

    def test_live_pause_disables_stop_only_during_game(self):
        markets = {"example-market": {"live_pause": True}}
        b, api, tr = self.make(market=GAME_MARKET, start=KICKOFF + 60, markets=markets)
        for _ in range(20):
            ds = b.run_once(); self.now += 60
        self.assertEqual(tr.sells, [])
        self.assertIn("live_pause", ds[0].reason)
        self.now = KICKOFF + 2.5 * 3600 + 60      # game over -> stop active again (normal window)
        for _ in range(4):
            self.step(b)
        self.assertEqual(len(tr.sells), 1)

    def test_per_market_confirm_override(self):
        b, api, tr = self.make(markets={"example-market": {"stop_confirm_sec": 60}})
        self.step(b); self.step(b)
        self.assertEqual(len(tr.sells), 1)

    def test_stale_timer_restarts_after_long_gap(self):
        b, api, tr = self.make()
        self.step(b); self.step(b)
        self.now += 3600                          # bot was down an hour
        self.step(b)
        self.assertEqual(tr.sells, [])

    def test_market_data_cached(self):
        b, api, tr = self.make(bid="0.80", market=GAME_MARKET)
        for _ in range(4):
            self.step(b)
        self.assertEqual(api.calls, 1)


class GameDetectTests(unittest.TestCase):
    def test_real_shapes(self):
        bar = {"gameStartTime": "2026-10-31 20:00:00+00", "sportsMarketType": "moneyline",
               "events": [{"startTime": "2026-10-31T20:00:00Z", "gameId": 9}]}
        st, src, sports = games.game_start(bar)
        self.assertEqual(games.local(st), "31 Oct 21:00")
        self.assertEqual(games.local(games.game_start(GAME_MARKET)[0]), "31 Oct 18:30")
        # event startTime of a politics event must NOT count as a game
        self.assertEqual(games.game_start(POLITICS_MARKET), (None, "", False))
        now = datetime(2026, 11, 3, 13, tzinfo=timezone.utc)
        self.assertEqual(games.classify(POLITICS_MARKET, now, 2.5).phase, "none")

    def test_phases(self):
        k = datetime.fromtimestamp(KICKOFF, tz=timezone.utc)
        from datetime import timedelta
        f = lambda dt, m=GAME_MARKET: games.classify(m, k + dt, 2.5).phase
        self.assertEqual(f(timedelta(minutes=-1)), "pre")
        self.assertEqual(f(timedelta(0)), "live")
        self.assertEqual(f(timedelta(hours=2.49)), "live")
        self.assertEqual(f(timedelta(hours=2.5)), "after")
        self.assertEqual(f(timedelta(minutes=5), dict(GAME_MARKET, closed=True)), "closed")
        self.assertEqual(games.classify(None, k, 2.5, True).phase, "unknown")
        self.assertEqual(f(timedelta(0), {"sportsMarketType": "moneyline"}), "unknown")


class StopResolutionTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.yaml"
        self.path.write_text("default: {stop_drop: 0.20}\nmarkets:\n"
                             "  example-match-2026-10-31-dor: {outcome: \"No\", stop_drop: 0.29}\n")

    def p(self, **kw):
        return pos(avg="0.89", **kw)

    def test_default(self):
        c = cfg.load(self.path)
        self.assertEqual(c.rule_for(self.p(slug="lal-bar", eventSlug="lal")).stop_level(D("0.89")), D("0.69"))

    def test_slug_override(self):
        c = cfg.load(self.path)
        r = c.rule_for(self.p(slug="example-match-2026-10-31-dor", outcome="No"))
        self.assertEqual(r.stop_level(D("0.89")), D("0.60"))
        r = c.rule_for(self.p(slug="example-match-2026-10-31-dor", outcome="Yes"))   # other side: default
        self.assertEqual(r.stop_level(D("0.89")), D("0.69"))

    def test_buy_stop_replaces_drop_and_is_picked_up(self):
        import pmbot
        auto = cfg.auto_path_for(self.path)
        cfg.save_auto_market(TOKEN, pmbot.stop_entry(D("0.30"), "No", {"slug": "example-market"}, "t"), auto)
        c = cfg.load(self.path)
        r = c.rule_for(self.p())
        self.assertEqual(r.stop_level(D("0.89")), D("0.30"))        # absolute stop, default drop removed
        self.assertIn(TOKEN, r.matched_by)
        self.assertIn(TOKEN, c.auto_markets)
        # hand-written config.yaml entry for the same key wins
        self.path.write_text(self.path.read_text() + f'  "{TOKEN}": {{stop_price: 0.50}}\n')
        self.assertEqual(cfg.load(self.path).rule_for(self.p()).stop_level(D("0.89")), D("0.69"))
        cfg.save_auto_market(TOKEN, None, auto)
        self.assertEqual(cfg.load(self.path).auto_markets, {})

    def test_bad_values_rejected(self):
        for bad in ("stop_confirm_sec: -1\n", "live_confirm_sec: \"10m\"\n", "live_window_hours: 0\n",
                    "markets:\n  x: {live_pause: \"yes\"}\n", "markets:\n  x: {stop_confirm_sec: 99999}\n"):
            self.path.write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                cfg.load(self.path)

    def test_hot_reload(self):
        auto = cfg.auto_path_for(self.path)
        c = cfg.load(self.path)
        b = StopLossBot(c, "0x1", api=FakeApi([], {}, {}), dry_run=True)
        watch = [self.path, auto]
        loader = lambda: cfg.load(self.path)
        self.assertFalse(b.maybe_reload(loader, watch))
        cfg.save_auto_market(TOKEN, {"stop_price": 0.4, "stop_drop": None}, auto)
        self.assertTrue(b.maybe_reload(loader, watch))
        self.assertIn(TOKEN, b.conf.markets)
        import time as _t; _t.sleep(0.01)
        auto.write_text("markets: {x: {bogus: 1}}\n")            # broken -> old config kept
        with self.assertLogs("pmbot", "ERROR"):
            self.assertFalse(b.maybe_reload(loader, watch))
        self.assertIn(TOKEN, b.conf.markets)


class BuyStopTests(BuyGuardTests):
    """`pmbot buy --stop` preview and validation (never places anything)."""
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.cpath = self.dir / "config.yaml"
        self.cpath.write_text("markets:\n  example-politics: {outcome: \"Yes\", stop_drop: 0.10}\n")
        mk = {"slug": "example-politics", "events": [{"slug": "which-party"}]}
        self.pmbot.resolve_token = lambda m, o, want_market=False: ("1", "Example market", "Yes", mk)

    def run_buy(self, stop=None, usd="4", max_price=None):
        import argparse, io, contextlib
        a = argparse.Namespace(market="x", outcome="Yes", usd=usd, max_price=max_price, stop=stop,
                               config=str(self.cpath))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.pmbot.cmd_buy(a, cfg.load(self.cpath))
        return rc, out.getvalue()

    def test_preview_shows_slug_override(self):          # the old bug: preview ignored overrides
        rc, out = self.run_buy()
        self.assertIn("~0.600", out)                       # 0.70 ask - 0.10 override, not 0.50 default
        self.assertIn("example-politics", out)

    def test_preview_shows_stop_and_declined_saves_nothing(self):
        rc, out = self.run_buy(stop="0.30")
        self.assertEqual(rc, 1)
        self.assertIn("~0.300", out)
        self.assertIn("--stop", out)
        self.assertIn("180s", out)
        self.assertFalse(cfg.auto_path_for(self.cpath).exists())

    def test_stop_validation(self):
        for bad in ("0.005", "0.70", "0.95", "0.69", "abc"):    # < 0.01, >= max price, >= best bid
            rc, out = self.run_buy(stop=bad)
            self.assertEqual(rc, 1, bad)
            self.assertNotIn("Place", out)

    def test_confirmed_buy_saves_stop_before_order(self):
        seen = {}
        test = self
        class C:
            def place_market_order(self, **kw):
                seen["auto"] = cfg.load(test.cpath).auto_markets.get("1")
                class R:
                    ok = True
                    def model_dump(self): return {"status": "matched"}
                return R()
        self.wallet.client = lambda conf: C()
        self.pmbot.confirm = lambda *a, **k: True
        rc, out = self.run_buy(stop="0.30")
        self.assertEqual(seen["auto"]["stop_price"], 0.30)
        self.assertIsNone(seen["auto"]["stop_drop"])
        self.assertEqual(cfg.load(self.cpath).rule_for({"asset": "1", "outcome": "Yes", "slug": "example-politics"})
                         .stop_level(D("0.70")), D("0.30"))

    def test_failed_order_removes_saved_stop(self):
        def boom(**kw): raise RuntimeError("rejected")
        class C: place_market_order = staticmethod(boom)
        self.wallet.client = lambda conf: C()
        self.pmbot.confirm = lambda *a, **k: True
        with self.assertRaises(RuntimeError):
            self.run_buy(stop="0.30")
        self.assertEqual(cfg.load(self.cpath).auto_markets, {})


if __name__ == "__main__":
    unittest.main()
