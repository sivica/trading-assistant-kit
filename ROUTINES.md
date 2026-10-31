# Example routines (all read-only)

Create these as scheduled tasks in your AI agent. **Every routine is read-only: it may look
at prices, positions, news and logs, and it may *suggest* a trade with an Approve/Skip
button, but it never places, modifies or cancels an order.** Times below are examples in
your own timezone; adjust them when daylight saving time changes.

## 1. Morning positions update (daily, ~09:00)
- List every open position (Hyperliquid perps, Polymarket bets): entry, current price, P/L in $,
  and the live stop and take-profit for each. Flag any position without a stop.
- What the trailing-stop helper would have done overnight (`hltrail status`, dry-run log).
- Today's calendar: earnings and big data releases (jobs, inflation, central banks) with local times.
- Picks scorecard from the picks log: wins, losses, $ result so far.
- Health: are `pm-stoploss` and `hl-trailstop` running? Any errors in the logs?

## 2. Evening snapshot (daily, ~22:30)
- Same positions table, short. Anything that hit a stop or take-profit today.
- Polymarket bets that settled: result in $, update the picks log status.

## 3. Earnings fast alert (on earnings days, around the release windows)
- Before US open (~12:00-15:30 local for CET) and after US close (~22:15-23:45 local).
- For companies on your watch list that just reported: headline numbers vs. estimates,
  guidance, and the first price reaction. Suggest a reaction trade only if it meets the
  thresholds in ASSISTANT.md, with Approve/Skip.

## 4. Stock big-moves watch (weekdays, during US market hours)
- Every hour or so: any stock perp you hold or watch moving more than ~5%, with one line on why.
- Alert only on news; otherwise stay quiet.

## 5. Post-earnings reaction check (one-off, day after a report, ~30 min after US open)
- Example: "Day after the report, 30 min after US open: check <TICKER> vs. the plan."
- Compare price vs. the pre-report close; if the move and the numbers meet the plan,
  send a single suggestion with Approve/Skip. Otherwise report "no trade".

## 6. One-off event checks
- A sports bet: after the match ends, report the result and P/L.
- An election or court decision: next morning recap of how your bets settled.
- A pending limit order: "check next morning whether the limit entry filled and the TP/SL are live".

## Tips
- Keep each message to a few lines. Silence is fine when nothing changed.
- Routines read prices from public APIs (no key needed). Only approved, human-confirmed
  actions in chat may use the API wallet key.
