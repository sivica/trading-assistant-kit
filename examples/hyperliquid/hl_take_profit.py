# WARNING: PLACES REAL ORDERS. Only run after the human approved this exact order in chat.
"""Add a reduce-only take-profit trigger to an EXISTING position (long or short).

  python hl_take_profit.py BTC 70000 73500     # short: buy back if price falls to 70000 (limit 73500 = worst price)
  python hl_take_profit.py xyz:AAPL 300 285    # long: sell if price rises to 300 (limit 285 = worst price)
"""
import json, sys
from _common import exchange, position, open_orders, confirm

coin, trigger, limit = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
pos = position(coin)
assert pos and float(pos["szi"]) != 0, f"no open {coin} position"
size = abs(float(pos["szi"])); is_short = float(pos["szi"]) < 0
confirm(f"TAKE-PROFIT on {coin} {'short' if is_short else 'long'} {size}: trigger {trigger}, limit {limit}")
tp = exchange().order(coin, is_short, size, limit, {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "tp"}},
                      reduce_only=True)
print("tp", json.dumps(tp))
print("open", json.dumps(open_orders(coin)))
