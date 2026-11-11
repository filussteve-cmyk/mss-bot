"""Paper execution for the MSS bot — virtual fills only. NO orders.

The executor holds at most one pending limit entry plus at most one
virtual position. Fill model (documented, deliberately conservative):

  - Entry is a LIMIT at the FVG 50% midpoint. The pending limit fills
    when a 5m bar trades through the midpoint (long: bar low <= entry;
    short: bar high >= entry). A gap-through void is decided by the
    caller (strategy_mss.evaluate_entry_bar) before this runs.
  - The fill bar's own range is never used against the position:
    stop/target are evaluated starting with the NEXT 5m bar.
  - Exits are evaluated bar by bar: a long exits at its stop if the
    bar's low <= stop, at its target if the bar's high >= target
    (mirror for shorts). If ONE bar's range spans BOTH stop and
    target, the STOP is assumed to fill first (worst case).
  - The 16:00 ET flatten closes any open position at the last seen
    close with exit_reason "flatten_1600".

P&L: points * point_value (config, $ per 1.00 point per contract) * size.
No fees are simulated.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from models import Bar, MssSetup


@dataclass
class PendingLimit:
    setup_key: str
    market: str
    direction: str       # "long" | "short"
    entry: float
    stop: float
    target: float
    size: int
    point_value: float   # $ per point per contract
    arm_bar_idx: int     # 15m bar idx where the setup armed
    bars_seen: int = 0   # 5m bars watched since arming

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingLimit":
        return cls(**d)


@dataclass
class PaperPosition:
    market: str
    strategy: str
    direction: str
    entry: float
    stop: float
    target: float
    size: int
    point_value: float
    entry_bar_idx: int   # 5m bar idx of the fill bar
    setup_key: str = ""  # key of the MssSetup that produced the trade

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "PaperPosition":
        return cls(**d)


@dataclass
class ClosedTrade:
    market: str
    strategy: str
    direction: str
    entry: float
    exit_price: float
    exit_reason: str     # "stop" | "target" | "flatten_1600"
    pnl_points: float
    pnl_dollars: float


def pnl_points(direction: str, entry: float, exit_price: float) -> float:
    return exit_price - entry if direction == "long" else entry - exit_price


class PaperExecutor:
    def __init__(self):
        self.pending: Optional[PendingLimit] = None
        self.position: Optional[PaperPosition] = None
        self.closed: List[ClosedTrade] = []

    @property
    def has_position(self) -> bool:
        return self.position is not None

    @property
    def has_pending(self) -> bool:
        return self.pending is not None

    def place_limit(self, setup: MssSetup, market: str,
                    point_value: float, size: int = 1) -> bool:
        """Arm the virtual limit order for a setup. False if busy."""
        if self.position is not None or self.pending is not None:
            return False
        self.pending = PendingLimit(
            setup_key=setup.key, market=market,
            direction=setup.direction, entry=setup.entry,
            stop=setup.stop, target=setup.target, size=size,
            point_value=point_value, arm_bar_idx=setup.arm_idx)
        return True

    def cancel_pending(self) -> Optional[PendingLimit]:
        """Void the pending limit (setup invalidated)."""
        pend, self.pending = self.pending, None
        return pend

    def _close(self, exit_price: float, reason: str) -> ClosedTrade:
        pos = self.position
        assert pos is not None
        pts = pnl_points(pos.direction, pos.entry, exit_price)
        trade = ClosedTrade(
            market=pos.market, strategy=pos.strategy,
            direction=pos.direction, entry=pos.entry,
            exit_price=exit_price, exit_reason=reason,
            pnl_points=pts,
            pnl_dollars=pts * pos.point_value * pos.size)
        self.closed.append(trade)
        self.position = None
        return trade

    def on_bar(self, bar: Bar) -> Optional[Union[PaperPosition, ClosedTrade]]:
        """Advance pending limit / open position by one 5m bar.

        Returns the new PaperPosition on a fill, the ClosedTrade on an
        exit, or None. The fill bar itself is never evaluated against
        the stop/target.
        """
        if self.pending is not None:
            pend = self.pending
            pend.bars_seen += 1
            filled = (bar.low <= pend.entry) if pend.direction == "long" \
                else (bar.high >= pend.entry)
            if filled:
                self.pending = None
                self.position = PaperPosition(
                    market=pend.market, strategy="mss",
                    direction=pend.direction, entry=pend.entry,
                    stop=pend.stop, target=pend.target, size=pend.size,
                    point_value=pend.point_value, entry_bar_idx=bar.idx,
                    setup_key=pend.setup_key)
                return self.position
            return None

        pos = self.position
        if pos is None or bar.idx <= pos.entry_bar_idx:
            return None
        if pos.direction == "long":
            if bar.low <= pos.stop:      # stop checked first: worst case
                return self._close(pos.stop, "stop")
            if bar.high >= pos.target:
                return self._close(pos.target, "target")
        else:
            if bar.high >= pos.stop:
                return self._close(pos.stop, "stop")
            if bar.low <= pos.target:
                return self._close(pos.target, "target")
        return None

    def flatten(self, last_close: float) -> Optional[ClosedTrade]:
        """16:00 ET flatten at the last seen close. No overnight, ever."""
        if self.position is None:
            return None
        return self._close(last_close, "flatten_1600")

    def realized_pnl(self) -> float:
        return sum(t.pnl_dollars for t in self.closed)
