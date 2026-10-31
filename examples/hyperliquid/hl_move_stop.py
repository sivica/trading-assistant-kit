# WARNING: PLACES REAL ORDERS. Only run after the human approved this exact order in chat.
"""Move a stop-loss: place the NEW stop first, and cancel the OLD one only if the new one rests.
That way the position is never left without a stop.

  python hl_move_stop.py BTC 85000 <OLD_STOP_OID>
"""
import json, sys
from _common import exchange, position, open_orders, confirm

coin, new_stop, old_oid = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
pos = position(coin)
assert pos and float(pos["szi"]) != 0, f"no open {coin} position"
size = abs(float(pos["szi"])); is_short = float(pos["szi"]) < 0
limit = round(new_stop * (1.05 if is_short else 0.95), 2)
confirm(f"MOVE STOP on {coin} {'short' if is_short else 'long'} {size}: new trigger {new_stop}, cancel oid {old_oid}")
ex = exchange()
sl = ex.order(coin, is_short, size, limit, {"trigger": {"triggerPx": new_stop, "isMarket": True, "tpsl": "sl"}},
              reduce_only=True)
print("new_sl", json.dumps(sl))
ok = sl.get("status") == "ok" and "resting" in sl["response"]["data"]["statuses"][0]
if ok:
    print("cancel_old", json.dumps(ex.cancel(coin, old_oid)))
else:
    print("new stop not resting; old stop kept")
print("open", json.dumps(open_orders(coin)))
