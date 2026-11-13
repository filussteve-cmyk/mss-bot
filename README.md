# MSS Bot — ICT Market Structure Shift (ES, paper only)

Paper-only bot that trades the ICT Market Structure Shift (MSS) setup on
ES front month. It frames HTF bias on 1H bars, detects a liquidity
sweep → displacement (FVG + order block) → market structure shift on
15m bars, and enters on the retracement into the displacement FVG with
a limit at the FVG midpoint. It places **ZERO real orders** — fills are
simulated in `paper_exec.py`.

Sources (ICT / @innercircletrader):
- 2022 ICT Mentorship Ep.3 "Internal Range Liquidity & Market Structure
  Shifts" — https://www.youtube.com/watch?v=nQfHZ2DEJ8c
- "Essentials To ICT Market Structure" —
  https://www.youtube.com/watch?v=xGFCIUFxsbw
- 2022 Mentorship Ep.12 "Advanced Study On Market Structure" —
  https://www.youtube.com/watch?v=8GkQfdAXZP0

## Strategy rules (bullish described; bearish is the mirror)

1. **HTF bias first** — 1H swing structure (see `htf_bias()`). Trade
   only in the HTF direction; if bias is unclear, do nothing.
2. **Liquidity sweep** — price trades below an old/equal swing low (sell
   stops) or above an old/equal swing high (buy stops). A sweep alone
   is NOT a signal.
3. **Displacement** — a sharp one-sided move away from the sweep that
   leaves a Fair Value Gap (FVG): 3-candle gap (bullish:
   `bar[i-2].high < bar[i].low`). Displacement also defines the order
   block (consecutive down-closed candles before a bullish move).
4. **Market Structure Shift** — price trades through the short-term
   swing high formed immediately before the swept low. No close
   above/below is required — trading through is enough (Ep.3).
5. **Entry** — wait for retracement into the displacement FVG. Limit at
   the FVG 50% midpoint. Never chase displacement. Entry window
   09:30–12:00 ET (ICT: trade 8:30am–noon NY).
6. **Stops / invalidation** — stop beyond the swept level / order block.
   The idea is void if price gaps through the swept level without
   filling, if HTF bias flips away from the setup, if no fill comes
   within 12 five-minute bars, or if the entry window ends.
7. **Targets** — opposing liquidity (nearest prior swing beyond the
   broken trigger level) if it pays ≥ 1.5R, else a 2R measured move.

## Mechanical choices

ICT teaches these concepts discretionarily. Every open point below is a
named parameter in `config.yaml` (`mss:` block) with the default shown.

| Open point | Mechanical choice (default) | Why |
|---|---|---|
| Displacement size threshold (ICT gives none) | `min_displacement_points: 3.5` ES points, leg starting within 5 bars of the sweep, at most 3 same-direction candles | 3.5 pts is a sharp move on ES without being so large it rarely fires; a leg that takes > 5 bars to start is no longer "displacement" (Ep.3 stresses the move is sharp and immediate) |
| Which swing counts | Fractal extreme of **±2 bars** on 15m (`swing_lookback_15m: 2`), same ±2 on 1H | ±2 is the short-term swing ICT draws by hand; ±5 (the S4 bot's lookback) would make the MSS trigger too slow for a day trade |
| Order-block length | ≥ 2 consecutive opposite-closed candles to count (`ob_min_candles: 2`), at most 5 used for the zone (`ob_max_candles: 5`) | A single down candle is noise; 2+ is a real push. The OB zone is the full run's [low, high]. No OB found → the setup still trades (FVG is the entry zone, OB is secondary) |
| FVG entry level | Limit at the **FVG 50% midpoint** (`entry_zone: "fvg_mid"`); fill when a 5m bar trades through the midpoint | The midpoint is the ICT "equilibrium" of the gap and the standard retracement entry; a limit there cannot chase. `ob_mid` is available in config as an alternative |
| Stop distance | Beyond the swept level / OB by **1.0 pt + 1 tick** (`stop_buffer_points: 1.0`, `stop_buffer_ticks: 1`) = 1.25 pts on ES | 1 tick alone is stop-hunt bait; 1 pt + 1 tick clears the level with room for wick noise. Stop uses the lower of swept level / OB low (longs) |
| Target method | **Opposing liquidity first**: nearest confirmed swing beyond the broken trigger level, if it pays ≥ 1.5R (`min_rr: 1.5`); else **2R measured move** (`measured_move_rr: 2.0`) | The trigger itself is excluded — price already traded through it, so it is not opposing liquidity any more. In practice the trigger is usually too close to pay 1.5R, so the measured move is the workhorse; the liquidity target fires when a distant pool exists |
| HTF bias rule | 1H: bullish iff the last two confirmed swing highs ascend AND the last two swing lows ascend AND the last close holds above the last swing low (mirror for bearish); else neutral = no trade (`bias_lookback_1h_bars: 40`) | Mechanical reading of "market structure" on the higher timeframe (Ep.12): HH + HL with price holding above the last low is an uptrend; anything else is not a trend worth trading |

**What could NOT be decided mechanically (documented, not hidden):**
- **Displacement vs. "just a strong bar":** ICT never quantifies how
  sharp displacement must be. The 4-point / 3-candle rule is a guess
  that needs paper data to tune — if setups fire too often or too
  rarely, this is the first knob to turn.
- **50% equilibrium as a target:** ICT lists the range's 50% level as
  a target option. This bot does not use it (opposing liquidity /
  measured move cover the cases); it is not in the code.
- **NY kill zone (7:00–10:00am ET):** the bot trades RTH only, so the
  7:00–9:30 pre-market portion of the kill zone is out of scope. The
  entry window (09:30–12:00) covers the kill zone's RTH portion.
- **"Old/equal" liquidity age:** ICT prefers sweeping older, more
  obvious pools. The detector treats every confirmed swing as a pool
  with no age weighting — a future scoring layer could rank pools.

## Files

| File | What it is |
|---|---|
| `strategy_mss.py` | Sweep / displacement / FVG / order-block / MSS detectors, HTF bias, entry evaluation, target logic, `MssParams` |
| `models.py` | `Bar`, `MssSetup` dataclasses |
| `risk.py` | Pure guards: RTH, 16:00 flatten, 09:30–12:00 entry window, -$500 halt, 1 position, no daily trade cap |
| `paper_exec.py` | Virtual limit entries, stop/target exits, 16:00 flatten. No order code |
| `bot.py` | Poll loop (60s), 1H/15m/5m bar fetch, bias + scan + arm + manage, `--smoke` |
| `config.yaml` | Mode `paper`, ES market, all `mss:` parameters |
| `run_paper.sh` | Paper launcher (forces `MSS_FORCE_PAPER=1`) |
| `requirements.txt` | `project-x-py`, `pyyaml`, `polars`, `pytest` |
| `test_strategy_mss.py`, `test_risk.py`, `test_paper_exec.py` | pytest unit tests, synthetic bars only |

## How to run (paper)

```bash
export TOPSTEPX_USERNAME=... TOPSTEPX_API_KEY=... TOPSTEPX_ACCOUNT_ID=...
./run_paper.sh            # detached, paper mode, ZERO orders
```

Offline check first (no auth, no orders):

```bash
~/workspace/s4-bot/.venv/bin/python bot.py --smoke
```

State lives in `state.json` (armed setup, pending limit, closed trades,
daily P&L); the daily log is `paper-YYYY-MM-DD.log`.

Near-miss sweeps are logged too, once each, as `PARTIAL MSS ...`
lines (`no_displacement` / `no_fvg` / `no_mss`, with the biggest leg
found vs the 4-pt minimum) — a quiet day used to hide whether
anything came close to the displacement threshold. They are also
kept in `state.json` under `partials_today`, and the 16:00 flatten
logs a one-line partials summary for the wrap.

## Guardrails

- 1 contract, ES only, $50/pt. RTH 09:30–16:00 ET, flat by 16:00,
  no overnight. Entry window 09:30–12:00 ET. Max 1 open position,
  no daily paper-trade cap, -$500 paper daily loss halt.
- A setup is decided once, at first detection (take it or skip it);
  it is never carried to a later poll. A pending limit voids on: gap
  through the swept level, HTF bias flip, 12 unfilled 5m bars, or
  12:00 ET.
- Detection is a full-session replay over the 15m bars (same pattern as
  the MultiScan scanner), so a restart reproduces the same setup set.

## Test results (Nov 11, 2026)

- `pytest test_strategy_mss.py test_risk.py test_paper_exec.py`:
  **49 passed** — sweep, FVG, displacement, order block, MSS (incl.
  no-close-needed), entry fill / gap-void / wait, bias
  (bullish/bearish/neutral/hold-above), opposing-liquidity vs
  measured-move targets, risk guards, paper executor (fill, stop-first
  on both-hit bar, target, flatten, cancel).
- `bot.py --smoke`: **PASSED** — bullish 1H bias → sweep 7640 →
  displacement leaving FVG [7651, 7655] → MSS through 7660 → limit
  fill LONG @ 7653.00 → 2R target 7685.50, **+$1,625.00 paper**,
  zero real orders.
- Live-mode lockout verified: `mode: "live"` → error + exit code 2
  before any authentication. Config restored to `paper`.

## What Steve must approve before live

Same bar as the MultiScan / Push350 bots: this bot is paper-only by
construction and has never been run against a live account. Before any
live consideration Steve must approve (a) the strategy itself after
seeing paper results, (b) mode `live` plus a real account ID, and (c)
the mechanical choices above — especially the 4-point displacement
threshold, which is unvalidated. The bot refuses to start in live mode
today; there is no live code path to enable by accident. Do NOT push
to GitHub (not pushed).
