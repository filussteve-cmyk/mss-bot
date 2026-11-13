"""ICT Market Structure Shift (MSS) detector.

Rules (bullish described; bearish is the mirror) from @innercircletrader:
  - 2022 ICT Mentorship Ep.3 "Internal Range Liquidity & Market Structure
    Shifts" — https://www.youtube.com/watch?v=nQfHZ2DEJ8c
  - "Essentials To ICT Market Structure" —
    https://www.youtube.com/watch?v=xGFCIUFxsbw
  - 2022 Mentorship Ep.12 "Advanced Study On Market Structure" —
    https://www.youtube.com/watch?v=8GkQfdAXZP0

Sequence, in order:
  1. HTF bias (1H swing structure — see htf_bias()).
  2. Liquidity sweep: price trades below an old/equal swing low (sell
     stops) or above an old/equal swing high (buy stops). A sweep alone
     is NOT a signal.
  3. Displacement: a sharp one-sided move away from the sweep that
     leaves a Fair Value Gap (FVG) — 3-candle gap (bullish:
     bar[i-2].high < bar[i].low). Displacement also defines the order
     block (opposite direction): consecutive down-closed candles before
     a bullish displacement.
  4. Market Structure Shift: price trades through the short-term swing
     high formed immediately before the swept low (bullish). No close
     above/below is required — trading through is enough (Ep.3).
  5. Entry: wait for retracement into the displacement FVG (limit at
     the FVG 50% midpoint). Never chase displacement.
  6. Invalidation: stop beyond the swept level / order block; if price
     gaps through the swept level without filling, or the entry window
     expires, the idea is void.
  7. Targets: opposing liquidity (nearest prior swing beyond the
     broken trigger level — the trigger itself is excluded, price
     already traded through it), 50% equilibrium of the range, or
     measured move. Pick one primary method, document it.

Where ICT is discretionary, this module makes a MECHANICAL choice and
records it in MssParams (see README "Mechanical choices" for the
reasoning and video citations). Detection here is a deterministic
full-session replay over the 15m bars (same pattern as the MultiScan
scanner): given the day's 15m bars, scan_15m() rebuilds every sweep /
displacement / MSS from bar 0, so a bot restart reproduces exactly the
same setup set.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from models import Bar, MssSetup


@dataclass(frozen=True)
class MssParams:
    """All mechanical choices, in one place. Defaults = ES values.

    See README "Mechanical choices" for why each default was picked.
    """
    tick: float = 0.25
    # Swing = fractal extreme of +/-N bars (short-term swings on 15m).
    swing_lookback_15m: int = 2
    swing_lookback_1h: int = 2
    # Displacement: a one-sided leg of >= this many points, starting
    # within displacement_search_bars of the sweep bar, at most
    # displacement_max_bars consecutive same-direction candles long.
    min_displacement_points: float = 3.5
    displacement_search_bars: int = 5
    displacement_max_bars: int = 3
    # Order block: consecutive opposite-closed candles immediately
    # before the displacement leg; at least ob_min_candles to count,
    # at most ob_max_candles used for the zone.
    ob_min_candles: int = 2
    ob_max_candles: int = 5
    # Entry zone: "fvg_mid" (limit at the FVG 50% midpoint).
    entry_zone: str = "fvg_mid"
    # Stop: beyond the swept level / OB by this buffer.
    stop_buffer_points: float = 1.0
    stop_buffer_ticks: int = 1
    # Target: opposing liquidity if it pays >= min_rr, else
    # measured_move_rr x risk.
    min_rr: float = 1.5
    measured_move_rr: float = 2.0
    # A pending setup is void after this many 5m bars without a fill.
    setup_max_bars_5m: int = 12
    # HTF bias looks back over this many 1H bars.
    bias_lookback_1h_bars: int = 40


ES_PARAMS = MssParams()


def snap(price: float, tick: float) -> float:
    """Snap a price to the tick grid."""
    return round(round(price / tick) * tick, 10)


# ---------------------------------------------------------------------------
# Swing detection (fractal extremes)
# ---------------------------------------------------------------------------

def _is_unique_extreme(values: List[float], i: int, lookback: int,
                       want_max: bool) -> bool:
    lo = max(0, i - lookback)
    hi = min(len(values), i + lookback + 1)
    window = values[lo:hi]
    extreme = max(window) if want_max else min(window)
    if values[i] != extreme:
        return False
    # Deterministic tie-break: only the first bar carrying the extreme counts.
    first = window.index(extreme) + lo
    return first == i


def is_swing_high(highs: List[float], i: int, lookback: int = 2) -> bool:
    """True if bar i is the (first) highest high of the +/-lookback window."""
    if i - lookback < 0 or i + lookback >= len(highs):
        return False
    return _is_unique_extreme(highs, i, lookback, want_max=True)


def is_swing_low(lows: List[float], i: int, lookback: int = 2) -> bool:
    """True if bar i is the (first) lowest low of the +/-lookback window."""
    if i - lookback < 0 or i + lookback >= len(lows):
        return False
    return _is_unique_extreme(lows, i, lookback, want_max=False)


def confirmed_swings(bars: List[Bar], lookback: int = 2
                     ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """All confirmed swing highs / lows as (bar_idx, price) pairs.

    A swing at bar i is confirmed only once bar i+lookback has closed,
    so the returned lists never include the last `lookback` bars.
    """
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    sh = [(i, bars[i].high) for i in range(len(bars))
          if is_swing_high(highs, i, lookback)]
    sl = [(i, bars[i].low) for i in range(len(bars))
          if is_swing_low(lows, i, lookback)]
    return sh, sl


# ---------------------------------------------------------------------------
# Liquidity sweep
# ---------------------------------------------------------------------------

@dataclass
class Sweep:
    """Price traded through a prior swing extreme (liquidity pool)."""
    direction: str        # "long" = swept a swing low; "short" = swept high
    level: float
    pool_idx: int         # bar idx of the swing that defined the pool
    sweep_idx: int        # first bar trading through the level
    trigger_level: float  # short-term swing extreme the MSS must break
    trigger_idx: int


def detect_sweeps(bars: List[Bar], params: MssParams = ES_PARAMS
                  ) -> List[Sweep]:
    """Every liquidity sweep in the bar list.

    For each confirmed swing low (bullish pool), the sweep is the first
    bar after the pool bar whose low trades below the pool level. The
    MSS trigger is the most recent confirmed swing high formed BEFORE
    the pool bar. Mirror for swing highs. A pool swept once never
    re-fires: the first bar through is the sweep.
    """
    lb = params.swing_lookback_15m
    swing_highs, swing_lows = confirmed_swings(bars, lb)
    sweeps: List[Sweep] = []

    for (j, level) in swing_lows:
        trig = [(i, p) for (i, p) in swing_highs if i < j]
        if not trig:
            continue
        t_idx, t_level = trig[-1]
        for k in range(j + 1, len(bars)):
            if bars[k].low < level:
                sweeps.append(Sweep("long", level, j, k, t_level, t_idx))
                break

    for (j, level) in swing_highs:
        trig = [(i, p) for (i, p) in swing_lows if i < j]
        if not trig:
            continue
        t_idx, t_level = trig[-1]
        for k in range(j + 1, len(bars)):
            if bars[k].high > level:
                sweeps.append(Sweep("short", level, j, k, t_level, t_idx))
                break

    sweeps.sort(key=lambda s: s.sweep_idx)
    return sweeps


# ---------------------------------------------------------------------------
# Displacement, FVG, order block
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fvg:
    """A 3-candle fair value gap. Bullish: bottom = bar[i-2].high,
    top = bar[i].low. Bearish is the mirror."""
    top: float
    bottom: float
    bar_idx: int          # the third candle of the triple

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class OrderBlock:
    """Consecutive opposite-closed candles before the displacement leg."""
    top: float
    bottom: float
    start_idx: int
    end_idx: int
    candles: int


@dataclass
class Displacement:
    direction: str
    start_idx: int
    end_idx: int
    size_points: float
    fvg: Fvg
    ob: Optional[OrderBlock]


@dataclass
class LegCandidate:
    """One candidate displacement leg for a sweep: consecutive
    same-direction-closed candles starting within the search window,
    with its total move from the sweep extreme and the first 3-candle
    FVG inside it (None when there is none). NOT filtered on the
    minimum size — callers decide what counts, so the near-misses
    stay visible for tuning."""
    start_idx: int
    end_idx: int
    size_points: float
    fvg: Optional[Fvg]


@dataclass
class PartialSetup:
    """A sweep that never became an armed setup — the near-miss the
    log used to drop silently.

    reason:
      - "no_displacement": no leg within the search window moved
        >= min_displacement_points. best_disp_points is the biggest
        leg found, so a 3.25 vs the 3.5 threshold shows up here.
      - "no_fvg": a leg cleared the size threshold but left no
        3-candle FVG inside it.
      - "no_mss": displacement + FVG formed but price never traded
        through the trigger level (as of the last bar scanned — if
        it breaks later, scan_15m() picks it up as a full setup and
        the setup log supersedes this line).
    had_fvg is True when ANY candidate leg left an FVG, even a
    too-small one — so a "no_displacement" partial with had_fvg
    True means size alone blocked it.
    """
    direction: str
    sweep_level: float
    pool_idx: int
    sweep_idx: int
    trigger_level: float
    trigger_idx: int
    reason: str           # "no_displacement" | "no_fvg" | "no_mss"
    best_disp_points: float
    had_fvg: bool
    had_mss: bool
    last_bar_idx: int     # last 15m bar scanned

    @property
    def key(self) -> str:
        """Stable dedupe key: one partial line per sweep."""
        return (f"partial:{self.direction}:{self.pool_idx}:"
                f"{self.sweep_idx}")

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def find_fvg(bars: List[Bar], start_idx: int, end_idx: int,
             direction: str) -> Optional[Fvg]:
    """First 3-candle FVG fully inside [start_idx, end_idx]."""
    for i in range(start_idx + 2, end_idx + 1):
        if direction == "long":
            if bars[i - 2].high < bars[i].low:
                return Fvg(top=bars[i].low, bottom=bars[i - 2].high,
                           bar_idx=i)
        else:
            if bars[i - 2].low > bars[i].high:
                return Fvg(top=bars[i - 2].low, bottom=bars[i].high,
                           bar_idx=i)
    return None


def find_order_block(bars: List[Bar], leg_start_idx: int, direction: str,
                     params: MssParams = ES_PARAMS) -> Optional[OrderBlock]:
    """Consecutive opposite-closed candles immediately before the leg.

    Bullish displacement: down-closed candles. Bearish: up-closed.
    Needs at least ob_min_candles; uses at most ob_max_candles.
    Returns None when the run is too short (the setup stays valid —
    the FVG is the entry zone, the OB is secondary).
    """
    run = []
    i = leg_start_idx - 1
    while i >= 0:
        b = bars[i]
        opposite = (b.close < b.open) if direction == "long" \
            else (b.close > b.open)
        if not opposite:
            break
        run.append(b)
        i -= 1
    if len(run) < params.ob_min_candles:
        return None
    run = run[:params.ob_max_candles]
    return OrderBlock(
        top=max(b.high for b in run),
        bottom=min(b.low for b in run),
        start_idx=run[-1].idx, end_idx=run[0].idx, candles=len(run))


def _leg_candidates(bars: List[Bar], sweep: Sweep,
                    params: MssParams = ES_PARAMS) -> List[LegCandidate]:
    """Every candidate displacement leg for a sweep, unfiltered.

    Same enumeration find_displacement always used: each possible leg
    start within displacement_search_bars of the sweep bar, extended
    over consecutive same-direction-closed candles (capped at
    displacement_max_bars), with its total move from the sweep
    extreme and the first 3-candle FVG inside the leg.
    """
    up = sweep.direction == "long"
    sweep_extreme = bars[sweep.sweep_idx].low if up \
        else bars[sweep.sweep_idx].high
    search_end = min(len(bars),
                     sweep.sweep_idx + 1 + params.displacement_search_bars)
    out: List[LegCandidate] = []
    for leg_start in range(sweep.sweep_idx + 1, search_end):
        first = bars[leg_start]
        if (first.close > first.open) != up:
            continue
        leg_end = leg_start
        while (leg_end + 1 < len(bars)
               and leg_end + 1 - leg_start < params.displacement_max_bars):
            nxt = bars[leg_end + 1]
            if (nxt.close > nxt.open) == up:
                leg_end += 1
            else:
                break
        leg = bars[leg_start:leg_end + 1]
        size = (max(b.high for b in leg) - sweep_extreme) if up \
            else (sweep_extreme - min(b.low for b in leg))
        out.append(LegCandidate(
            start_idx=leg_start, end_idx=leg_end, size_points=size,
            fvg=find_fvg(bars, leg_start, leg_end, sweep.direction)))
    return out


def find_displacement(bars: List[Bar], sweep: Sweep,
                      params: MssParams = ES_PARAMS) -> Optional[Displacement]:
    """The sharp one-sided leg away from a sweep, with its FVG + OB.

    The first candidate leg whose move is >= min_displacement_points
    and which leaves a 3-candle FVG inside it wins. Same semantics
    as before — now built on _leg_candidates so the rejected legs
    stay available to scan_partials.
    """
    for cand in _leg_candidates(bars, sweep, params):
        if cand.size_points < params.min_displacement_points:
            continue
        if cand.fvg is None:
            continue
        ob = find_order_block(bars, cand.start_idx, sweep.direction, params)
        return Displacement(direction=sweep.direction,
                            start_idx=cand.start_idx,
                            end_idx=cand.end_idx,
                            size_points=cand.size_points, fvg=cand.fvg,
                            ob=ob)
    return None


# ---------------------------------------------------------------------------
# Market structure shift
# ---------------------------------------------------------------------------

def find_mss(bars: List[Bar], sweep: Sweep, disp: Displacement) -> Optional[int]:
    """First bar from the displacement leg that trades through the
    trigger level. No close above/below is required (Ep.3)."""
    for k in range(disp.start_idx, len(bars)):
        if sweep.direction == "long" and bars[k].high > sweep.trigger_level:
            return k
        if sweep.direction == "short" and bars[k].low < sweep.trigger_level:
            return k
    return None


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

def compute_target(bars: List[Bar], arm_idx: int, direction: str,
                   entry: float, stop: float, trigger_level: float,
                   params: MssParams = ES_PARAMS) -> Tuple[float, str]:
    """Primary target = opposing liquidity: the nearest prior swing
    BEYOND the broken trigger level (the trigger itself is excluded —
    price already traded through it, so it is not opposing liquidity
    any more). It must pay >= min_rr, otherwise a measured move of
    measured_move_rr x risk. Only swings confirmed by the arm bar count.
    """
    risk = abs(entry - stop)
    prefix = bars[:arm_idx + 1]
    swing_highs, swing_lows = confirmed_swings(
        prefix, params.swing_lookback_15m)
    if direction == "long":
        pools = [p for (_, p) in swing_highs if p > trigger_level]
        if pools:
            nearest = min(pools)
            if nearest - entry >= params.min_rr * risk:
                return nearest, "opposing_liquidity"
        return entry + params.measured_move_rr * risk, "measured_move"
    pools = [p for (_, p) in swing_lows if p < trigger_level]
    if pools:
        nearest = max(pools)
        if entry - nearest >= params.min_rr * risk:
            return nearest, "opposing_liquidity"
    return entry - params.measured_move_rr * risk, "measured_move"


# ---------------------------------------------------------------------------
# Full 15m scan — sweep -> displacement/FVG/OB -> MSS -> armed setup
# ---------------------------------------------------------------------------

def _build_setup(bars: List[Bar], sweep: Sweep, disp: Displacement,
                 mss_idx: int, params: MssParams) -> MssSetup:
    arm_idx = max(mss_idx, disp.fvg.bar_idx)
    if params.entry_zone == "fvg_mid":
        entry = snap(disp.fvg.mid, params.tick)
    elif params.entry_zone == "ob_mid" and disp.ob is not None:
        entry = snap((disp.ob.top + disp.ob.bottom) / 2.0, params.tick)
    else:  # ob_mid requested but no OB found — fall back to the FVG
        entry = snap(disp.fvg.mid, params.tick)
    if sweep.direction == "long":
        base = sweep.level
        if disp.ob is not None:
            base = min(base, disp.ob.bottom)
        stop = snap(base - params.stop_buffer_points
                    - params.stop_buffer_ticks * params.tick, params.tick)
    else:
        base = sweep.level
        if disp.ob is not None:
            base = max(base, disp.ob.top)
        stop = snap(base + params.stop_buffer_points
                    + params.stop_buffer_ticks * params.tick, params.tick)
    target, method = compute_target(bars, arm_idx, sweep.direction,
                                    entry, stop, sweep.trigger_level, params)
    target = snap(target, params.tick)
    return MssSetup(
        direction=sweep.direction, sweep_level=sweep.level,
        pool_idx=sweep.pool_idx, sweep_idx=sweep.sweep_idx,
        trigger_level=sweep.trigger_level, trigger_idx=sweep.trigger_idx,
        disp_start_idx=disp.start_idx, disp_end_idx=disp.end_idx,
        mss_idx=mss_idx, arm_idx=arm_idx,
        fvg_top=disp.fvg.top, fvg_bottom=disp.fvg.bottom,
        ob_top=disp.ob.top if disp.ob else None,
        ob_bottom=disp.ob.bottom if disp.ob else None,
        entry=entry, stop=stop, target=target, target_method=method,
        risk_points=abs(entry - stop),
        detail={"fvg_bar_idx": disp.fvg.bar_idx,
                "disp_size_points": round(disp.size_points, 2),
                "ob_candles": disp.ob.candles if disp.ob else 0})


def scan_15m(bars: List[Bar], params: MssParams = ES_PARAMS) -> List[MssSetup]:
    """Replay the 15m bars and return every armed MSS setup, in arm order."""
    setups: List[MssSetup] = []
    for sweep in detect_sweeps(bars, params):
        disp = find_displacement(bars, sweep, params)
        if disp is None:
            continue
        mss_idx = find_mss(bars, sweep, disp)
        if mss_idx is None:
            continue
        setups.append(_build_setup(bars, sweep, disp, mss_idx, params))
    setups.sort(key=lambda s: s.arm_idx)
    return setups


def scan_partials(bars: List[Bar], params: MssParams = ES_PARAMS
                  ) -> List[PartialSetup]:
    """Every sweep that did NOT become an armed setup, with why.

    Only sweeps whose displacement search window has fully elapsed
    are reported (len(bars) > sweep_idx + 1 + displacement_search_bars),
    so a "no_displacement" verdict can't be undone by a leg that
    hasn't formed yet. A "no_mss" verdict is as of the last bar
    scanned. Sweeps that became full setups are excluded — scan_15m()
    reports those.
    """
    partials: List[PartialSetup] = []
    for sweep in detect_sweeps(bars, params):
        if len(bars) <= sweep.sweep_idx + 1 + params.displacement_search_bars:
            continue
        cands = _leg_candidates(bars, sweep, params)
        best = max((c.size_points for c in cands), default=0.0)
        had_fvg = any(c.fvg is not None for c in cands)
        disp = find_displacement(bars, sweep, params)
        if disp is not None:
            if find_mss(bars, sweep, disp) is not None:
                continue  # full setup — scan_15m() reports it
            reason = "no_mss"
        elif any(c.size_points >= params.min_displacement_points
                 for c in cands):
            reason = "no_fvg"
        else:
            reason = "no_displacement"
        partials.append(PartialSetup(
            direction=sweep.direction, sweep_level=sweep.level,
            pool_idx=sweep.pool_idx, sweep_idx=sweep.sweep_idx,
            trigger_level=sweep.trigger_level,
            trigger_idx=sweep.trigger_idx, reason=reason,
            best_disp_points=round(best, 2), had_fvg=had_fvg,
            had_mss=False, last_bar_idx=len(bars) - 1))
    partials.sort(key=lambda p: p.sweep_idx)
    return partials


# ---------------------------------------------------------------------------
# HTF bias (1H swing structure)
# ---------------------------------------------------------------------------

def htf_bias(bars_1h: List[Bar], params: MssParams = ES_PARAMS) -> str:
    """Mechanical 1H bias from swing structure (see README).

    Bullish: the last two confirmed 1H swing highs ascend AND the last
    two confirmed 1H swing lows ascend AND the last close holds above
    the last swing low. Bearish is the mirror. Anything else is
    neutral — and neutral means no trade.
    """
    window = bars_1h[-params.bias_lookback_1h_bars:] if bars_1h else []
    if len(window) < 2 * params.swing_lookback_1h + 3:
        return "neutral"
    highs, lows = confirmed_swings(window, params.swing_lookback_1h)
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    (h1_i, h1), (h2_i, h2) = highs[-2], highs[-1]
    (l1_i, l1), (l2_i, l2) = lows[-2], lows[-1]
    close = window[-1].close
    if h2 > h1 and l2 > l1 and close > l2:
        return "bullish"
    if h2 < h1 and l2 < l1 and close < h2:
        return "bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Entry evaluation on 5m bars (limit at the FVG midpoint)
# ---------------------------------------------------------------------------

def evaluate_entry_bar(bar: Bar, setup: MssSetup) -> str:
    """One 5m bar against a pending limit entry.

    Returns "fill" | "void_gap" | "wait".
      - fill: the bar traded through the entry midpoint (limit fills
        at the midpoint or better).
      - void_gap: the bar OPENED already below the entry (long) and
        traded through the swept level in the same bar — price gapped
        through the whole idea without ever trading at the entry, so
        the setup is void rather than filled (see README).
      - wait: anything else.
    """
    if setup.direction == "long":
        if bar.low <= setup.entry:
            if bar.open < setup.entry and bar.low < setup.sweep_level:
                return "void_gap"
            return "fill"
        return "wait"
    if bar.high >= setup.entry:
        if bar.open > setup.entry and bar.high > setup.sweep_level:
            return "void_gap"
        return "fill"
    return "wait"
