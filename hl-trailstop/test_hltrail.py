import hltrail as h
C = {"trail_pct": 0.04, "activate_pct": 0.04, "min_step_pct": 0.005}
def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name); assert cond
# long not activated
r = h.compute("long", 100, 103, None, 90, 120, C, 2); t("long inactive below 4%", not r["activated"] and r["proposed"] is None)
# long activates and trails
r = h.compute("long", 100, 110, None, 90, 120, C, 2); t("long activates, stop 105.6", r["activated"] and r["proposed"] == 105.6)
# best persists when price dips; stop based on best
r2 = h.compute("long", 100, 107, 110, 105.6, 120, C, 2, True); t("long never loosens on dip", r2["proposed"] is None and r2["best"] == 110)
# min step
r = h.compute("long", 100, 110.3, 110, 105.6, 120, C, 2, True); t("long small step skipped", r["proposed"] is None and "min step" in r["reason"])
# TP cap
r = h.compute("long", 100, 110, None, 90, 105, C, 2); t("long TP cap", r["proposed"] is None and "take-profit" in r["reason"])
# short
r = h.compute("short", 80000, 76000, None, 80000, 70000, C, 5); t("short activates, stop 79040", r["activated"] and r["proposed"] == 79040.0)
r = h.compute("short", 100, 98, None, 110, 80, C, 2); t("short inactive", r["proposed"] is None and not r["activated"])
r = h.compute("short", 100, 91, 90, 93, 80, C, 2, True); t("short never loosens (93.6 > 93)", r["proposed"] is None and "loosen" in r["reason"])
r = h.compute("short", 100, 90, None, 110, 94, C, 2); t("short TP cap", r["proposed"] is None and "take-profit" in r["reason"])
r = h.compute("short", 100, 95, 90, 99, 80, C, 2, True); t("short never past current price", r["proposed"] is None and "past current" in r["reason"])
r = h.compute("long", 100, 110, None, None, None, C, 2); t("no stop -> no proposal", r["proposed"] is None and r["reason"] == "no stop order")
# rounding
t("round BTC int", h.round_px(79039.2, 5, "up") == 79040.0)
t("round 5 sig figs", h.round_px(1385.123, 4, "down") == 1385.1)
t("round HYPE", h.round_px(86.8612, 2, "down") == 86.861)
t("round small", h.round_px(0.123456789, 0, "down") == 0.12345)
print("all tests passed")
