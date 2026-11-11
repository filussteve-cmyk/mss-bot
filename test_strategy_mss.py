"""Unit tests for the ICT MSS detector — synthetic bars only."""

import pytest

from models import Bar
import strategy_mss
from strategy_mss import MssParams


def mk(spec):
    """spec = list of (open, high, low, close) -> Bar list with idx."""
    return [Bar(i, o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(spec)]


# The textbook bullish 15m sequence (same bars as bot.py --smoke).
BULL_15M = [
    (7652, 7654, 7650, 7652),
    (7652, 7655, 7651, 7654),
    (7654, 7660, 7653, 7658),   # idx 2: swing high 7660 (trigger)
    (7658, 7659, 7652, 7654),
    (7654, 7655, 7648, 7650),
    (7650, 7651, 7646, 7648),
    (7648, 7650, 7640, 7642),   # idx 6: swing low 7640 (pool)
    (7642, 7646, 7642, 7644),
    (7648, 7649, 7642, 7644),   # down (OB candle 1)
    (7644, 7646, 7638, 7640),   # down (OB candle 2) + sweep to 7638
    (7640, 7651, 7640, 7650),   # displacement leg start
    (7650, 7662, 7656, 7660),   # MSS: high 7662 > 7660
    (7660, 7666, 7655, 7664),   # FVG: bar10.high 7651 < bar12.low 7655
]


def mirror(spec):
    """Mirror a bullish bar list into a bearish one (pivot 7650)."""
    out = []
    for (o, h, l, c) in spec:
        m = lambda p: 2 * 7650.0 - p
        out.append((m(o), m(l), m(h), m(c)))
    return out


# ---------------------------------------------------------------------------
# Swings
# ---------------------------------------------------------------------------

def test_swing_high_and_low_fractal():
    bars = mk(BULL_15M)
    highs, lows = strategy_mss.confirmed_swings(bars, 2)
    assert (2, 7660.0) in highs
    assert (6, 7640.0) in lows


def test_swing_requires_full_window():
    # Only 4 bars: no swing can confirm (needs +/-2).
    bars = mk([(100, 102, 99, 101), (101, 103, 100, 102),
               (102, 105, 101, 104), (104, 104, 102, 103)])
    highs, lows = strategy_mss.confirmed_swings(bars, 2)
    assert highs == [] and lows == []


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def test_bullish_sweep_detected():
    bars = mk(BULL_15M)
    sweeps = strategy_mss.detect_sweeps(bars)
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s.direction == "long"
    assert s.level == 7640.0 and s.pool_idx == 6 and s.sweep_idx == 9
    assert s.trigger_level == 7660.0 and s.trigger_idx == 2


def test_no_sweep_without_trade_through():
    # Same bars but the "sweep" bar only touches the pool level exactly.
    spec = list(BULL_15M)
    spec[9] = (7644, 7646, 7640, 7642)  # low == 7640, not below
    bars = mk(spec)
    assert strategy_mss.detect_sweeps(bars) == []


def test_sweep_needs_trigger_swing_before_pool():
    # A swing low with no earlier swing high: no trigger, no sweep.
    bars = mk([
        (100, 101, 99, 100), (100, 101, 98, 99), (99, 100, 97, 98),
        (98, 99, 96, 97), (97, 98, 95, 96), (96, 97, 94, 95),
        (95, 96, 93, 94), (94, 95, 92, 93), (93, 94, 90, 91),
    ])
    assert strategy_mss.detect_sweeps(bars) == []


# ---------------------------------------------------------------------------
# FVG / displacement / order block
# ---------------------------------------------------------------------------

def test_fvg_bullish():
    bars = mk(BULL_15M)
    fvg = strategy_mss.find_fvg(bars, 10, 12, "long")
    assert fvg is not None
    assert fvg.top == 7655.0 and fvg.bottom == 7651.0 and fvg.bar_idx == 12
    assert fvg.mid == 7653.0


def test_fvg_bearish_mirror():
    bars = mk(mirror(BULL_15M))
    fvg = strategy_mss.find_fvg(bars, 10, 12, "short")
    assert fvg is not None
    assert fvg.top == 7649.0 and fvg.bottom == 7645.0


def test_no_fvg_when_gap_filled():
    # bar12 low overlaps bar10 high: no gap.
    spec = list(BULL_15M)
    spec[12] = (7660, 7666, 7650, 7664)
    bars = mk(spec)
    assert strategy_mss.find_fvg(bars, 10, 12, "long") is None


def test_displacement_threshold_blocks_small_move():
    # Same shape but displacement only 2 pts (< 4.0 default).
    bars = mk(BULL_15M)
    sweeps = strategy_mss.detect_sweeps(bars)
    assert sweeps
    params = MssParams(min_displacement_points=30.0)
    assert strategy_mss.find_displacement(bars, sweeps[0], params) is None


def test_displacement_requires_fvg():
    spec = list(BULL_15M)
    spec[12] = (7660, 7666, 7650, 7664)  # kills the FVG
    bars = mk(spec)
    sweeps = strategy_mss.detect_sweeps(bars)
    assert strategy_mss.find_displacement(bars, sweeps[0]) is None


def test_order_block_two_down_candles():
    bars = mk(BULL_15M)
    ob = strategy_mss.find_order_block(bars, 10, "long")
    assert ob is not None
    assert ob.top == 7649.0 and ob.bottom == 7638.0 and ob.candles == 2


def test_order_block_needs_min_candles():
    # Only one down candle before the leg: no OB (setup stays valid).
    spec = list(BULL_15M)
    spec[8] = (7642, 7646, 7642, 7645)  # up-closed now
    bars = mk(spec)
    assert strategy_mss.find_order_block(bars, 10, "long") is None


# ---------------------------------------------------------------------------
# MSS
# ---------------------------------------------------------------------------

def test_mss_trades_through_no_close_needed():
    bars = mk(BULL_15M)
    sweeps = strategy_mss.detect_sweeps(bars)
    disp = strategy_mss.find_displacement(bars, sweeps[0])
    # idx 11 high 7662 > trigger 7660, but its close 7660 is NOT above.
    assert bars[11].close == 7660.0
    assert strategy_mss.find_mss(bars, sweeps[0], disp) == 11


def test_mss_no_break():
    spec = list(BULL_15M)
    spec[11] = (7650, 7659, 7656, 7658)  # high 7659 < trigger 7660
    spec[12] = (7658, 7659, 7655, 7659)  # up-closed, keeps FVG + leg
    bars = mk(spec)
    sweeps = strategy_mss.detect_sweeps(bars)
    disp = strategy_mss.find_displacement(bars, sweeps[0])
    assert disp is not None  # displacement + FVG still form
    assert strategy_mss.find_mss(bars, sweeps[0], disp) is None


# ---------------------------------------------------------------------------
# Full scan
# ---------------------------------------------------------------------------

def test_full_bullish_sequence():
    bars = mk(BULL_15M)
    setups = strategy_mss.scan_15m(bars)
    assert len(setups) == 1
    s = setups[0]
    assert s.direction == "long"
    assert s.entry == 7653.0
    assert s.stop == 7636.75      # min(7640, 7638) - 1.0 - 0.25
    assert s.target == 7685.5     # 2R measured move (7660 too close)
    assert s.target_method == "measured_move"
    assert s.risk_points == 16.25
    assert s.mss_idx == 11 and s.arm_idx == 12


def test_full_bearish_sequence_mirror():
    bars = mk(mirror(BULL_15M))
    setups = strategy_mss.scan_15m(bars)
    assert len(setups) == 1
    s = setups[0]
    assert s.direction == "short"
    assert s.entry == 7647.0
    assert s.stop == 7663.25
    assert s.target == 7614.5
    assert s.target_method == "measured_move"


def test_no_setup_without_sweep():
    # Flat range, no sweep: nothing arms.
    bars = mk([(100, 101, 99, 100)] * 20)
    assert strategy_mss.scan_15m(bars) == []


def test_target_opposing_liquidity_when_far_enough():
    # Opposing swing high 130 sits beyond the broken trigger (113) and
    # pays >= 1.5R: entry 107, stop 98.75 (risk 8.25), 130 - 107 = 23.
    # The trigger itself (113) is excluded — price already broke it.
    spec = [
        (100, 101, 99, 100),
        (100, 102, 99, 101),
        (101, 130, 100, 129),   # idx 2: swing high 130 (opposing pool)
        (129, 128, 120, 122),
        (122, 123, 115, 116),
        (116, 117, 110, 111),
        (111, 112, 108, 109),
        (109, 111, 106, 107),
        (107, 113, 105, 112),   # idx 8: swing high 113 (trigger)
        (112, 112, 104, 105),
        (105, 106, 100, 101),   # idx 10: swing low 100 (pool)
        (101, 103, 101, 102),
        (102, 104, 102, 103),
        (103, 104, 98, 99),     # idx 13: sweep to 98
        (99, 106, 99, 105),     # leg start
        (105, 114, 104, 113),   # MSS: high 114 > trigger 113
        (113, 116, 108, 115),   # FVG: bar14.high 106 < bar16.low 108
    ]
    bars = mk(spec)
    setups = strategy_mss.scan_15m(bars)
    assert len(setups) == 1
    s = setups[0]
    assert s.entry == 107.0
    assert s.stop == 98.75
    assert s.risk_points == 8.25
    assert s.target == 130.0
    assert s.target_method == "opposing_liquidity"


# ---------------------------------------------------------------------------
# HTF bias
# ---------------------------------------------------------------------------

BULL_1H = [
    (7600, 7601, 7599, 7600),
    (7602, 7604, 7601, 7603),
    (7605, 7620, 7604, 7618),
    (7618, 7619, 7610, 7612),
    (7612, 7613, 7608, 7610),
    (7610, 7611, 7606, 7608),
    (7608, 7615, 7608, 7614),
    (7614, 7616, 7607, 7615),
    (7632, 7640, 7630, 7638),
    (7638, 7639, 7628, 7630),
    (7630, 7631, 7626, 7628),
    (7628, 7632, 7627, 7630),
    (7630, 7635, 7628, 7634),
]


def test_bias_bullish():
    assert strategy_mss.htf_bias(mk(BULL_1H)) == "bullish"


def test_bias_bearish_mirror():
    assert strategy_mss.htf_bias(mk(mirror(BULL_1H))) == "bearish"


def test_bias_neutral_flat():
    bars = mk([(100, 101, 99, 100)] * 13)
    assert strategy_mss.htf_bias(bars) == "neutral"


def test_bias_neutral_when_close_below_last_swing_low():
    # HH/HL structure but the last close lost the last swing low.
    spec = list(BULL_1H)
    spec[12] = (7630, 7635, 7620, 7622)  # close 7622 < last low 7626
    assert strategy_mss.htf_bias(mk(spec)) == "neutral"


def test_bias_neutral_too_few_bars():
    assert strategy_mss.htf_bias(mk(BULL_1H[:5])) == "neutral"


# ---------------------------------------------------------------------------
# Entry evaluation on 5m bars
# ---------------------------------------------------------------------------

def test_entry_fill_at_midpoint():
    setup = strategy_mss.scan_15m(mk(BULL_15M))[0]
    assert strategy_mss.evaluate_entry_bar(Bar(0, 7664, 7665, 7652, 7655),
                                          setup) == "fill"


def test_entry_wait_above_midpoint():
    setup = strategy_mss.scan_15m(mk(BULL_15M))[0]
    assert strategy_mss.evaluate_entry_bar(Bar(0, 7660, 7662, 7654, 7658),
                                          setup) == "wait"


def test_entry_void_on_gap_through_swept_level():
    setup = strategy_mss.scan_15m(mk(BULL_15M))[0]
    # Opens below the entry (7653) and trades through the swept level.
    assert strategy_mss.evaluate_entry_bar(Bar(0, 7650, 7651, 7635, 7638),
                                          setup) == "void_gap"


def test_entry_fill_on_gap_that_holds_above_swept_level():
    setup = strategy_mss.scan_15m(mk(BULL_15M))[0]
    # Opens below entry but holds above the swept level: limit still fills.
    assert strategy_mss.evaluate_entry_bar(Bar(0, 7650, 7651, 7645, 7648),
                                          setup) == "fill"
