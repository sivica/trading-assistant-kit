"""Config loading. config.yaml lives in the data dir (default ~/.pmbot/config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import yaml

from stoplogic import Rule, dec

HOME = Path(os.environ.get("PMBOT_HOME", Path.home() / ".pmbot"))
KEY_FILE = HOME / "wallet.key"
ADDRESS_FILE = HOME / "wallet.address"
CREDS_FILE = HOME / "clob_creds.json"
STATE_FILE = HOME / "state.json"
LOG_FILE = HOME / "bot.log"
ENV_FILE = HOME / "bot.env"
CONFIG_FILE = HOME / "config.yaml"
BUILDER_KEY_FILE = HOME / "builder_key.json"      # Polymarket Builder API key (secret)
DEPOSIT_WALLET_FILE = HOME / "deposit_wallet.address"
# Per-market stops written by `pmbot buy --stop`. Same format as `markets:` in config.yaml,
# keyed by token id. config.yaml entries win if both use the same key.
AUTO_MARKETS_NAME = "auto_stops.yaml"

RULE_KEYS = ("stop_drop", "stop_pct", "stop_price", "min_sell_price", "max_slippage")
TIMER_KEYS = ("stop_confirm_sec", "live_confirm_sec")          # seconds, per market or global
MARKET_KEYS = set(RULE_KEYS) | set(TIMER_KEYS) | {"enabled", "outcome", "note", "live_pause"}
MAX_CONFIRM_SEC = 6 * 3600


@dataclass
class Config:
    dry_run: bool = True
    poll_interval_seconds: int = 60
    confirm_checks: int = 2
    stop_confirm_sec: int = 180     # bid must stay <= stop this long (consecutive checks) before selling
    live_confirm_sec: int = 600     # same, while a sports game is in progress
    live_window_hours: float = 2.5  # a game counts as "in progress" from its start time for this long
    retry_cooldown_seconds: int = 120
    dust_shares: Decimal = Decimal("0.01")
    cancel_open_orders_before_stop: bool = True
    max_buy_usd: Decimal = Decimal("5")
    geoblock_check_minutes: int = 30
    # Trade through Polymarket's Deposit Wallet (required for wallets that never traded before
    # 2026-05-03). Set false only for an old, allowlisted EOA.
    use_deposit_wallet: bool = True
    rpc_urls: list = field(default_factory=lambda: [
        "https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org", "https://1rpc.io/matic"])
    default: Rule = field(default_factory=Rule)
    markets: dict = field(default_factory=dict)        # merged: auto_stops.yaml, then config.yaml
    auto_markets: dict = field(default_factory=dict)   # only the auto_stops.yaml part

    def rule_for(self, position: dict) -> Rule:
        """Default rule, then slug override, then token-id override (most specific wins)."""
        base = {k: getattr(self.default, k) for k in RULE_KEYS}
        base["enabled"] = True
        base["stop_confirm_sec"] = self.stop_confirm_sec
        base["live_confirm_sec"] = self.live_confirm_sec
        base["live_pause"] = False
        matched = "default"
        token = str(position.get("asset", ""))
        outcome = str(position.get("outcome", "")).lower()
        for key in (position.get("eventSlug"), position.get("slug"), token):
            if not key or key not in self.markets:
                continue
            ov = self.markets[key] or {}
            want = ov.get("outcome")
            if want and str(want).lower() != outcome and key != token:
                continue
            for k in RULE_KEYS:
                if k in ov:
                    base[k] = dec(ov[k]) if ov[k] is not None else None
            if "enabled" in ov:
                base["enabled"] = bool(ov["enabled"])
            for k in TIMER_KEYS:
                if ov.get(k) is not None:
                    base[k] = int(ov[k])
            if "live_pause" in ov:
                base["live_pause"] = bool(ov["live_pause"])
            matched = f"override '{key}'"
        r = Rule(**{k: base[k] for k in RULE_KEYS if k in ("stop_drop", "stop_pct", "stop_price")},
                 min_sell_price=base["min_sell_price"] if base["min_sell_price"] is not None else Decimal("0.01"),
                 max_slippage=base["max_slippage"] if base["max_slippage"] is not None else Decimal("0"),
                 enabled=base["enabled"], matched_by=matched,
                 confirm_sec=base["stop_confirm_sec"], live_confirm_sec=base["live_confirm_sec"],
                 live_pause=base["live_pause"])
        return r


def _validate_rule(name: str, r: Rule):
    for k in ("stop_drop", "stop_pct", "stop_price", "min_sell_price", "max_slippage"):
        v = getattr(r, k)
        if v is not None and not (Decimal(0) <= v < Decimal(1)):
            raise ValueError(f"{name}.{k} must be between 0 and 1 (got {v}); prices are in dollars, e.g. 0.20 = 20 cents")


def auto_path_for(path: Optional[Path] = None) -> Path:
    return Path(path or CONFIG_FILE).parent / AUTO_MARKETS_NAME


def _check_secs(name: str, v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= MAX_CONFIRM_SEC):
        raise ValueError(f"{name} must be a number of seconds between 0 and {MAX_CONFIRM_SEC} (got {v!r})")
    return int(v)


def _check_markets(where: str, markets: dict):
    for k, v in markets.items():
        if not isinstance(v, dict):
            raise ValueError(f"{where}.{k} must be a block of settings")
        unknown = set(v) - MARKET_KEYS
        if unknown:
            raise ValueError(f"{where}.{k}: unknown setting(s) {sorted(unknown)}")
        for kk in RULE_KEYS:
            if v.get(kk) is not None and not (0 <= float(v[kk]) < 1):
                raise ValueError(f"{where}.{k}.{kk} must be between 0 and 1")
        for kk in TIMER_KEYS:
            if v.get(kk) is not None:
                _check_secs(f"{where}.{k}.{kk}", v[kk])
        for kk in ("enabled", "live_pause"):
            if kk in v and not isinstance(v[kk], bool):
                raise ValueError(f"{where}.{k}.{kk} must be true or false")


def load_auto_markets(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    m = raw.get("markets") or {}
    if not isinstance(m, dict):
        raise ValueError(f"{path}: 'markets' must be a block of settings")
    m = {str(k): (v or {}) for k, v in m.items()}
    _check_markets(path.name, m)
    return m


def save_auto_market(key: str, entry: Optional[dict], path: Path):
    """Add/replace (entry) or remove (entry=None) one auto override. Atomic write."""
    m = load_auto_markets(path)
    if entry is None:
        m.pop(str(key), None)
    else:
        _check_markets(path.name, {str(key): entry})
        m[str(key)] = entry
    text = ("# Written by `pmbot buy --stop`. Same format as `markets:` in config.yaml (keyed by token id).\n"
            "# The running bot re-reads this file automatically. config.yaml entries with the same key win.\n"
            + yaml.safe_dump({"markets": m}, sort_keys=False, default_flow_style=False))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load(path: Optional[Path] = None, auto_path: Optional[Path] = None) -> Config:
    path = Path(path or CONFIG_FILE)
    auto_path = Path(auto_path) if auto_path else auto_path_for(path)
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    c = Config()
    for k in ("dry_run", "cancel_open_orders_before_stop", "use_deposit_wallet"):
        if k in raw:
            if not isinstance(raw[k], bool):
                raise ValueError(f"{k} must be true or false")
            setattr(c, k, raw[k])
    for k in ("poll_interval_seconds", "confirm_checks", "retry_cooldown_seconds", "geoblock_check_minutes"):
        if k in raw:
            setattr(c, k, int(raw[k]))
    for k in TIMER_KEYS:
        if k in raw:
            setattr(c, k, _check_secs(k, raw[k]))
    if "live_window_hours" in raw:
        v = raw["live_window_hours"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 12):
            raise ValueError("live_window_hours must be between 0 and 12")
        c.live_window_hours = float(v)
    for k in ("dust_shares", "max_buy_usd"):
        if k in raw:
            setattr(c, k, dec(raw[k]))
    if raw.get("rpc_urls"):
        c.rpc_urls = list(raw["rpc_urls"])
    d = raw.get("default") or {}
    c.default = Rule(
        stop_drop=dec(d.get("stop_drop", "0.20")) if d.get("stop_drop", "0.20") is not None else None,
        stop_pct=dec(d.get("stop_pct")),
        stop_price=dec(d.get("stop_price")),
        min_sell_price=dec(d.get("min_sell_price", "0.05")),
        max_slippage=dec(d.get("max_slippage", "0.02")),
    )
    _validate_rule("default", c.default)
    own = {str(k): (v or {}) for k, v in (raw.get("markets") or {}).items()}
    _check_markets("markets", own)
    c.auto_markets = load_auto_markets(auto_path)
    c.markets = dict(c.auto_markets)
    c.markets.update(own)          # hand-written config.yaml wins over auto entries
    if c.poll_interval_seconds < 15:
        raise ValueError("poll_interval_seconds must be at least 15 (be nice to the API)")
    if c.confirm_checks < 1:
        raise ValueError("confirm_checks must be at least 1")
    return c
