"""Unit tests for the MSS risk guards — pure checks, no I/O."""

from datetime import datetime

import risk
from risk import ET


def at(h, m):
    return datetime(2026, 11, 11, h, m, tzinfo=ET)


def test_in_rth_boundaries():
    assert risk.in_rth(at(9, 30))
    assert risk.in_rth(at(15, 59))
    assert not risk.in_rth(at(9, 29))
    assert not risk.in_rth(at(16, 0))


def test_entry_window_boundaries():
    assert risk.in_entry_window(at(9, 30))
    assert risk.in_entry_window(at(11, 59))
    assert not risk.in_entry_window(at(9, 29))
    assert not risk.in_entry_window(at(12, 0))
    assert not risk.in_entry_window(at(14, 0))


def test_should_flatten_at_1600():
    assert risk.should_flatten(at(16, 0))
    assert risk.should_flatten(at(16, 30))
    assert not risk.should_flatten(at(15, 59))


def test_daily_loss_halt():
    assert risk.daily_loss_halted(-500.0)
    assert risk.daily_loss_halted(-501.0)
    assert not risk.daily_loss_halted(-499.99)
    assert not risk.daily_loss_halted(0.0)


def test_can_open_ok_in_window():
    ok, reason = risk.can_open(False, 0, now=at(10, 0))
    assert ok, reason


def test_can_open_blocked_after_noon():
    ok, reason = risk.can_open(False, 0, now=at(12, 30))
    assert not ok and "entry window" in reason


def test_can_open_blocked_at_flatten():
    ok, reason = risk.can_open(False, 0, now=at(16, 0))
    assert not ok and "flatten" in reason


def test_can_open_blocked_with_position():
    ok, reason = risk.can_open(True, 0, now=at(10, 0))
    assert not ok and "position" in reason


def test_can_open_blocked_at_trade_cap():
    ok, reason = risk.can_open(False, 2, now=at(10, 0),
                               max_trades_per_day=2)
    assert not ok and "cap" in reason
    ok, _ = risk.can_open(False, 1, now=at(10, 0), max_trades_per_day=2)
    assert ok


def test_can_open_blocked_on_daily_loss():
    ok, reason = risk.can_open(False, 0, now=at(10, 0), daily_pnl=-500.0)
    assert not ok and "halt" in reason
