"""Pure stop-loss decision logic. No network, no keys: easy to unit test.

A "position" here is a small dict built from the data-api positions endpoint
plus the exact on-chain share balance. A "book" is the CLOB /book response.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Optional

D = Decimal


def dec(x: Any) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass
class Rule:
    """Effective stop rule for one position (global defaults + overrides)."""
    stop_drop: Optional[Decimal] = D("0.20")   # sell if bid <= avg - stop_drop
    stop_pct: Optional[Decimal] = None          # sell if bid <= avg * (1 - stop_pct)
    stop_price: Optional[Decimal] = None        # sell if bid <= stop_price
    min_sell_price: Decimal = D("0.05")         # never sell below this
    max_slippage: Decimal = D("0.02")           # accept fills down to bid - this
    enabled: bool = True
    matched_by: str = "default"
    confirm_sec: int = 180          # stop must hold this long (bid <= stop) before selling
    live_confirm_sec: int = 600     # same, while a sports game is in progress
    live_pause: bool = False        # true = never stop-sell while the game is in progress

    def stop_level(self, avg: Decimal) -> Optional[Decimal]:
        """Highest (earliest-triggering) of the configured stop levels."""
        levels = []
        if self.stop_drop is not None:
            levels.append(avg - self.stop_drop)
        if self.stop_pct is not None:
            levels.append(avg * (D(1) - self.stop_pct))
        if self.stop_price is not None:
            levels.append(self.stop_price)
        levels = [l for l in levels if l > 0]
        return max(levels) if levels else None


@dataclass
class Decision:
    action: str            # "hold" | "sell" | "skip" | "no_bids" | "below_floor"
    reason: str
    token_id: str = ""
    title: str = ""
    outcome: str = ""
    size: Decimal = D(0)
    avg_price: Optional[Decimal] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    stop_level: Optional[Decimal] = None
    min_price: Optional[Decimal] = None      # lowest price we accept (FAK protect)
    tick_size: Decimal = D("0.01")
    min_order_size: Decimal = D("5")
    fillable_shares: Decimal = D(0)          # bid depth at >= min_price
    est_proceeds: Decimal = D(0)
    warnings: list = field(default_factory=list)
    confirm_sec: Optional[int] = None        # confirm window that applied this round
    game: str = ""                           # e.g. "live since 18:30", "starts 21:00", "" (not a game)

    @property
    def triggered(self) -> bool:
        return self.action in ("sell", "no_bids", "below_floor")


def round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def parse_book(book: Optional[dict]):
    """Return (bids desc, asks asc, tick, min_size) with Decimal values."""
    if not book:
        return [], [], D("0.01"), D("5")
    bids = sorted(((dec(b["price"]), dec(b["size"])) for b in book.get("bids") or []), key=lambda x: -x[0])
    asks = sorted(((dec(a["price"]), dec(a["size"])) for a in book.get("asks") or []), key=lambda x: x[0])
    tick = dec(book.get("tick_size")) or D("0.01")
    min_size = dec(book.get("min_order_size")) or D("5")
    return bids, asks, tick, min_size


def evaluate(position: dict, book: Optional[dict], rule: Rule, dust: Decimal = D("0.01")) -> Decision:
    """Decide what to do with one position. Never has side effects."""
    tid = str(position.get("asset", ""))
    d = Decision(action="hold", reason="", token_id=tid, title=position.get("title", ""),
                 outcome=position.get("outcome", ""))
    size = dec(position.get("exact_size"))
    if size is None:
        size = dec(position.get("size")) or D(0)
    d.size = size
    d.avg_price = dec(position.get("avgPrice"))

    if not rule.enabled:
        d.action, d.reason = "skip", f"disabled in config ({rule.matched_by})"
        return d
    if position.get("redeemable"):
        d.action, d.reason = "skip", "market resolved (redeem it instead)"
        return d
    if size <= dust:
        d.action, d.reason = "skip", "no shares (already sold?)"
        return d
    if book is None:
        d.action, d.reason = "skip", "no order book (market closed or not tradable)"
        return d
    if d.avg_price is None or d.avg_price <= 0:
        if rule.stop_price is None:
            d.action, d.reason = "skip", "unknown entry price and no stop_price set"
            return d

    bids, asks, tick, min_size = parse_book(book)
    d.tick_size, d.min_order_size = tick, min_size
    d.best_bid = bids[0][0] if bids else None
    d.best_ask = asks[0][0] if asks else None
    d.stop_level = rule.stop_level(d.avg_price or D(0))
    if d.stop_level is None:
        d.action, d.reason = "hold", "no stop level applies (entry too cheap for a fixed drop; set stop_price or stop_pct)"
        return d

    if d.best_bid is None:
        # Nobody is buying. We cannot sell, but the user should know.
        d.action, d.reason = "no_bids", "no buyers in the order book; cannot sell"
        return d

    if d.best_bid > d.stop_level:
        d.action = "hold"
        d.reason = f"bid {d.best_bid} above stop {d.stop_level:.3f}"
        return d

    # Stop is hit.
    floor = round_up_to_tick(max(rule.min_sell_price, tick), tick)
    if d.best_bid < floor:
        d.action = "below_floor"
        d.reason = f"stop hit but best bid {d.best_bid} is below your floor {floor}; not selling"
        return d
    min_price = round_up_to_tick(max(floor, d.best_bid - rule.max_slippage), tick)
    if min_price > d.best_bid:   # rounding pushed us above the bid
        min_price = d.best_bid
    d.min_price = min_price

    remaining = size
    for price, lvl_size in bids:
        if price < min_price or remaining <= 0:
            break
        take = min(remaining, lvl_size)
        d.fillable_shares += take
        d.est_proceeds += take * price
        remaining -= take
    if d.fillable_shares < size:
        d.warnings.append(f"only ~{d.fillable_shares} of {size} shares can fill at >= {min_price}; "
                          "the rest is retried next round")
    if size < min_size:
        d.warnings.append(f"position ({size}) is below the market minimum order size ({min_size}); "
                          "Polymarket may reject the sell")
    d.action = "sell"
    d.reason = f"bid {d.best_bid} <= stop {d.stop_level:.3f}"
    return d
