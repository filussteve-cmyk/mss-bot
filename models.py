"""Shared data types for the MSS bot — pure dataclasses, no I/O."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Bar:
    """One bar of any timeframe. idx is the intraday bar index (0-based)."""
    idx: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class MssSetup:
    """One armed ICT Market Structure Shift setup (15m detection).

    Bullish described; bearish is the mirror. All prices are absolute.
    The setup is fully specified at arming time: entry (FVG midpoint
    limit), stop (beyond the swept level / order block), target
    (opposing liquidity or 2R measured move).
    """
    direction: str       # "long" | "short"
    sweep_level: float   # the swept liquidity level (swing low / high)
    pool_idx: int        # 15m bar idx of the swing that defined the pool
    sweep_idx: int       # 15m bar idx that traded through the level
    trigger_level: float  # short-term swing extreme to break for the MSS
    trigger_idx: int
    disp_start_idx: int  # first bar of the displacement leg
    disp_end_idx: int
    mss_idx: int         # first bar trading through the trigger level
    arm_idx: int         # 15m bar idx where the setup completed
    fvg_top: float
    fvg_bottom: float
    ob_top: Optional[float] = None   # order-block zone (None if no OB)
    ob_bottom: Optional[float] = None
    entry: float = 0.0   # FVG 50% midpoint (limit)
    stop: float = 0.0
    target: float = 0.0
    target_method: str = ""  # "opposing_liquidity" | "measured_move"
    risk_points: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable dedupe key (a setup must never be taken twice)."""
        return (f"mss:{self.direction}:{self.pool_idx}:{self.sweep_idx}:"
                f"{self.mss_idx}:{self.entry:.4f}")

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "MssSetup":
        return cls(**d)
