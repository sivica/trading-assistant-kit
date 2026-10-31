# WARNING: PLACES REAL ORDERS. Only run after the human approved this exact order in chat.
"""Limit-buy entry with take-profit and stop-loss attached as one group (normalTpsl).
The TP/SL become active only when the entry fills.

  python hl_long_grouped_tpsl.py HYPE 0.25 40.0 42.5 38.5
  (coin, size, entry limit, take-profit trigger, stop-loss trigger)
"""
import json, sys
from _common import exchange, info, confirm

coin, size, entry, tp, sl = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
print("mid now", info().all_mids().get(coin))
confirm(f"LONG {size} {coin}: limit {entry} GTC, 1x isolated, TP {tp}, SL {sl}")
ex = exchange()
print("leverage", json.dumps(ex.update_leverage(1, coin, is_cross=False)))
reqs = [
    {"coin": coin, "is_buy": True, "sz": size, "limit_px": entry, "order_type": {"limit": {"tif": "Gtc"}},
     "reduce_only": False},
    # trigger-market sells: limit_px is the worst accepted price (here 10% below the trigger)
    {"coin": coin, "is_buy": False, "sz": size, "limit_px": round(tp * 0.9, 2),
     "order_type": {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}}, "reduce_only": True},
    {"coin": coin, "is_buy": False, "sz": size, "limit_px": round(sl * 0.9, 2),
     "order_type": {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}}, "reduce_only": True},
]
print("orders", json.dumps(ex.bulk_orders(reqs, grouping="normalTpsl")))
