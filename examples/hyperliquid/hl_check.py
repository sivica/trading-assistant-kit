"""Read-only: account value, positions and open orders. Needs NO key (public info API).

  HL_ACCOUNT_ADDRESS=0x... python hl_check.py
"""
import json
from _common import account, info, DEXES

i = info()
for dex in DEXES:
    st = i.post("/info", {"type": "clearinghouseState", "user": account(), "dex": dex})
    print(f"[dex {dex or 'main'}] account value:", st["marginSummary"]["accountValue"])
    for p in st.get("assetPositions", []):
        q = p["position"]
        print("  position", q["coin"], "size", q["szi"], "entry", q["entryPx"], "uPnL", q["unrealizedPnl"])
    for o in i.post("/info", {"type": "frontendOpenOrders", "user": account(), "dex": dex}):
        print("  order", o["coin"], o["side"], o["sz"], o.get("orderType"), "trigger", o.get("triggerPx"), "oid", o["oid"])
print("spot", json.dumps(i.spot_user_state(account())["balances"]))
