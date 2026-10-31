# WARNING: PLACES REAL ORDERS. Only run after the human approved this exact order in chat.
"""Market-open a small 1x isolated SHORT, then attach a reduce-only stop-loss.

  python hl_short_with_stop.py BTC 0.0001 88000
  (coin, size in coins, stop trigger price above entry)
"""
import json, sys
from _common import exchange, confirm

coin, size, stop = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
confirm(f"SHORT {size} {coin} at market, 1x isolated, stop-loss trigger {stop}")
ex = exchange()
print("leverage", json.dumps(ex.update_leverage(1, coin, is_cross=False)))
r = ex.market_open(coin, False, size, None, 0.01)          # is_buy=False -> short, 1% max slippage
print("open", json.dumps(r))
st = r.get("response", {}).get("data", {}).get("statuses", [{}])[0] if r.get("status") == "ok" else {}
if "filled" in st:
    limit = round(stop * 1.05)                               # worst fill price once triggered
    sl = ex.order(coin, True, size, limit, {"trigger": {"triggerPx": stop, "isMarket": True, "tpsl": "sl"}},
                  reduce_only=True)
    print("stop", json.dumps(sl))
else:
    print("not filled, no stop placed")
