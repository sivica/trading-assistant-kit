"""Shared helpers for the Hyperliquid examples.

Reads everything from environment variables, never from files in this repo:
  HL_ACCOUNT_ADDRESS  public address of your MAIN Hyperliquid account
  HL_API_WALLET_KEY   private key of an API (agent) wallet approved in the Hyperliquid app.
                      API wallets can trade but CANNOT withdraw. Never use your main wallet key.
Never print the key.
"""
import os, sys, time

from hyperliquid.info import Info
from hyperliquid.utils import constants

API_URL = constants.MAINNET_API_URL
DEXES = ["", "xyz"]   # "" = crypto perps, "xyz" = HIP-3 stock perps (coins like "xyz:AAPL")


def account() -> str:
    a = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
    if not a.startswith("0x"):
        sys.exit("set HL_ACCOUNT_ADDRESS to your main Hyperliquid address")
    return a


def info() -> Info:
    return Info(API_URL, skip_ws=True, perp_dexs=DEXES)


def exchange():
    """Exchange client signed by the API wallet. Retries the init on HTTP 429 (rate limit)."""
    import eth_account
    from hyperliquid.exchange import Exchange
    key = os.environ.get("HL_API_WALLET_KEY", "").strip()
    if not key:
        sys.exit("set HL_API_WALLET_KEY (API/agent wallet key, never the main key)")
    agent = eth_account.Account.from_key(key)
    for i in range(3):
        try:
            return Exchange(agent, API_URL, account_address=account(), perp_dexs=DEXES)
        except Exception as e:  # noqa: BLE001
            print("init retry:", type(e).__name__); time.sleep(15)
    sys.exit("could not reach Hyperliquid")


def dex_of(coin: str) -> str:
    return coin.split(":", 1)[0] if ":" in coin else ""


def position(coin: str):
    st = info().post("/info", {"type": "clearinghouseState", "user": account(), "dex": dex_of(coin)})
    for p in st.get("assetPositions", []):
        if p["position"]["coin"] == coin:
            return p["position"]
    return None


def open_orders(coin: str):
    oo = info().post("/info", {"type": "frontendOpenOrders", "user": account(), "dex": dex_of(coin)})
    return [o for o in oo if o["coin"] == coin]


def confirm(summary: str):
    """Last safety net: the human must type YES. The assistant asks for Approve in chat first."""
    print(summary)
    if os.environ.get("HL_CONFIRM") == "YES":
        return
    if input("Type YES to send this order: ").strip() != "YES":
        sys.exit("cancelled, nothing sent")
