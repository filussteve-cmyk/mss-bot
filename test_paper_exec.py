"""Unit tests for the MSS paper executor — virtual fills only."""

from models import Bar, MssSetup
from paper_exec import PaperExecutor, PaperPosition, ClosedTrade


def _setup(direction="long", entry=7653.0, stop=7636.75, target=7685.5):
    return MssSetup(
        direction=direction, sweep_level=7640.0, pool_idx=6, sweep_idx=9,
        trigger_level=7660.0, trigger_idx=2, disp_start_idx=10,
        disp_end_idx=12, mss_idx=11, arm_idx=12, fvg_top=7655.0,
        fvg_bottom=7651.0, entry=entry, stop=stop, target=target,
        target_method="measured_move", risk_points=abs(entry - stop))


def _bar(idx, o, h, l, c):
    return Bar(idx, o, h, l, c, 1000.0)


def test_place_limit_and_fill_at_midpoint():
    ex = PaperExecutor()
    assert ex.place_limit(_setup(), "ES", point_value=50.0, size=1)
    assert ex.has_pending and not ex.has_position
    res = ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    assert isinstance(res, PaperPosition)
    assert res.entry == 7653.0 and res.direction == "long"
    assert not ex.has_pending and ex.has_position


def test_no_fill_above_midpoint():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    assert ex.on_bar(_bar(0, 7660, 7662, 7654, 7658)) is None
    assert ex.has_pending


def test_short_fill_on_high_touch():
    ex = PaperExecutor()
    s = _setup(direction="short", entry=7647.0, stop=7663.25, target=7614.5)
    ex.place_limit(s, "ES", point_value=50.0)
    res = ex.on_bar(_bar(0, 7640, 7648, 7638, 7642))
    assert isinstance(res, PaperPosition) and res.direction == "short"


def test_place_limit_refused_when_busy():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    assert not ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))  # fill
    assert not ex.place_limit(_setup(), "ES", point_value=50.0)


def test_fill_bar_never_stops_out():
    # The fill bar's range spans the stop: it must NOT close the trade.
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    res = ex.on_bar(_bar(0, 7664, 7700, 7630, 7655))
    assert isinstance(res, PaperPosition)
    assert ex.has_position


def test_target_hit_after_fill():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    trade = ex.on_bar(_bar(1, 7670, 7686, 7668, 7680))
    assert isinstance(trade, ClosedTrade)
    assert trade.exit_reason == "target" and trade.exit_price == 7685.5
    assert trade.pnl_points == 32.5 and trade.pnl_dollars == 1625.0
    assert not ex.has_position


def test_stop_hit_after_fill():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    trade = ex.on_bar(_bar(1, 7655, 7656, 7635, 7640))
    assert trade.exit_reason == "stop" and trade.exit_price == 7636.75
    assert trade.pnl_points == -16.25
    assert trade.pnl_dollars == -812.5


def test_both_hit_bar_closes_at_stop_worst_case():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    trade = ex.on_bar(_bar(1, 7655, 7700, 7630, 7680))
    assert trade.exit_reason == "stop", trade


def test_flatten_1600():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    trade = ex.flatten(7660.0)
    assert trade.exit_reason == "flatten_1600"
    assert trade.pnl_points == 7.0 and trade.pnl_dollars == 350.0


def test_cancel_pending():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    pend = ex.cancel_pending()
    assert pend is not None and pend.entry == 7653.0
    assert not ex.has_pending
    assert ex.cancel_pending() is None


def test_realized_pnl_sums_closed():
    ex = PaperExecutor()
    ex.place_limit(_setup(), "ES", point_value=50.0)
    ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    ex.on_bar(_bar(1, 7670, 7686, 7668, 7680))  # +1625 target
    assert ex.realized_pnl() == 1625.0


def test_setup_key_carried_to_position():
    ex = PaperExecutor()
    s = _setup()
    ex.place_limit(s, "ES", point_value=50.0)
    res = ex.on_bar(_bar(0, 7664, 7665, 7652, 7655))
    assert isinstance(res, PaperPosition)
    assert res.setup_key == s.key
