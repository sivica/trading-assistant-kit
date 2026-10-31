"""Read-only public Polymarket + Polygon calls (no keys needed) and Telegram notify."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Optional

log = logging.getLogger("pmbot")
UA = {"user-agent": "pm-stoploss-bot/1.0", "accept": "application/json"}
DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"

# Polygon token / contract addresses (docs.polymarket.com/resources/contracts)
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # bridged USDC.e
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"     # wraps USDC.e -> pUSD
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"        # Conditional Tokens (ERC-1155)
POSITION_MANAGER = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"  # Protocol V2 (ERC-1155)
# Neg Risk Adapter for CTF ("v1") negative-risk markets. The CLOB checks pUSD allowance to it on BUYs
# and CTF operator approval on SELLs, but SDK setup_trading_approvals() does not grant it
# (docs.polymarket.com/trading/wallets-auth -> Set Up Trading Approvals).
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"


class NotFound(Exception):
    pass


def http_json(url: str, params: Optional[dict] = None, data: Optional[dict] = None,
              retries: int = 3, timeout: int = 15) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(UA, **({"content-type": "application/json"} if body else {}))
    last: Exception = RuntimeError("no attempt")
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            last = e
            if e.code == 400:
                raise
            time.sleep(2 * (i + 1) if e.code == 429 else 1)
        except Exception as e:  # network hiccup
            last = e
            time.sleep(1 + i)
    raise last


# ---------- Polymarket public reads ----------

def geoblock() -> dict:
    """{"blocked": bool, "ip": ..., "country": ..., "region": ...}. See
    https://docs.polymarket.com/developers/CLOB/geoblock"""
    return http_json(GEOBLOCK)


def positions(user: str) -> list[dict]:
    out, offset = [], 0
    while True:
        page = http_json(f"{DATA_API}/positions", {"user": user, "sizeThreshold": 0,
                                                    "limit": 500, "offset": offset})
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected positions response: {str(page)[:200]}")
        out.extend(page)
        if len(page) < 500:
            return out
        offset += 500


def book(token_id: str) -> Optional[dict]:
    """CLOB order book, or None when the market has no book (closed/resolved)."""
    try:
        b = http_json(f"{CLOB}/book", {"token_id": token_id})
    except NotFound:
        return None
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return None
        raise
    if isinstance(b, dict) and b.get("error"):
        return None
    return b


def gamma_market_by_slug(slug: str) -> Optional[dict]:
    r = http_json(f"{GAMMA}/markets", {"slug": slug})
    return r[0] if isinstance(r, list) and r else None


def gamma_event_by_slug(slug: str) -> Optional[dict]:
    r = http_json(f"{GAMMA}/events", {"slug": slug})
    return r[0] if isinstance(r, list) and r else None


def slug_from_url(s: str) -> list[str]:
    """'https://polymarket.com/event/ev-slug/market-slug?x' -> ['market-slug', 'ev-slug']"""
    s = s.strip()
    if "://" in s:
        path = urllib.parse.urlparse(s).path
        parts = [p for p in path.split("/") if p and p not in ("event", "market")]
        return list(reversed(parts))
    return [s]


# ---------- Polygon RPC ----------

class Rpc:
    def __init__(self, urls: list[str]):
        self.urls = urls

    def call(self, method: str, params: list) -> Any:
        last: Exception = RuntimeError("no RPC configured")
        for url in self.urls:
            try:
                r = http_json(url, data={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, retries=1)
                if "error" in r:
                    raise RuntimeError(r["error"])
                return r["result"]
            except Exception as e:
                last = e
        raise RuntimeError(f"all Polygon RPCs failed: {last}")

    def pol_balance(self, addr: str) -> Decimal:
        return Decimal(int(self.call("eth_getBalance", [addr, "latest"]), 16)) / Decimal(10**18)

    def erc20_balance(self, token: str, addr: str) -> Decimal:
        data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
        return Decimal(int(self.call("eth_call", [{"to": token, "data": data}, "latest"]), 16)) / Decimal(10**6)

    def erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        data = "0xdd62ed3e" + owner[2:].lower().rjust(64, "0") + spender[2:].lower().rjust(64, "0")
        return int(self.call("eth_call", [{"to": token, "data": data}, "latest"]), 16)

    def is_approved_for_all(self, token: str, owner: str, operator: str) -> bool:
        data = "0xe985e9c5" + owner[2:].lower().rjust(64, "0") + operator[2:].lower().rjust(64, "0")
        return int(self.call("eth_call", [{"to": token, "data": data}, "latest"]), 16) != 0

    def erc1155_balance(self, token_contract: str, addr: str, token_id: str) -> Decimal:
        data = "0x00fdd58e" + addr[2:].lower().rjust(64, "0") + format(int(token_id), "064x")
        raw = self.call("eth_call", [{"to": token_contract, "data": data}, "latest"])
        return Decimal(int(raw, 16)) / Decimal(10**6)

    def exact_shares(self, addr: str, token_id: str) -> Decimal:
        """Exact outcome-share balance (6 decimals). Checks both position ledgers."""
        best = Decimal(0)
        for contract in (CTF, POSITION_MANAGER):
            try:
                best = max(best, self.erc1155_balance(contract, addr, token_id))
            except Exception as e:
                log.debug("balance read on %s failed: %s", contract, e)
        return best

    def wallet_balances(self, addr: str) -> dict:
        return {"POL": self.pol_balance(addr), "pUSD": self.erc20_balance(PUSD, addr),
                "USDC.e": self.erc20_balance(USDCE, addr), "USDC (native)": self.erc20_balance(USDC_NATIVE, addr)}


# ---------- Notifications ----------

def notify(text: str) -> None:
    """Send a Telegram message if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        http_json(f"https://api.telegram.org/bot{tok}/sendMessage",
                  data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, retries=2)
    except Exception as e:
        log.warning("telegram notify failed: %s", type(e).__name__)
