"""Risk guards for the MSS bot — pure checks only.

Session rules (family guardrails, same as the S4 / MultiScan / Push350
bots):
  - RTH only: 09:30-16:00 ET. Flat BY 16:00 ET — no overnight, ever.
    The flatten check sits at 16:00 and, in bot.py's loop and in
    can_open below, is evaluated BEFORE any RTH-close exit so it can
    never be skipped (the S4 bot's latent 16:09-vs-16:00 bug, fixed
    here by construction).
  - Entries only in the ICT window 09:30-12:00 ET (trade 8:30am-noon
    NY per ICT; RTH entry window 09:30-12:00 ET).
  - Max 1 open position. Max 2 paper trades/day (configurable).
  - Daily loss halt: no new entries once daily P&L <= -$500.

There are NO broker helpers in this file: this bot is paper-only and
live mode is not implemented, so there is no live code path to keep.
"""

from datetime import datetime, time
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)       # regular trading hours start (ET)
RTH_CLOSE = time(16, 0)      # regular trading hours end (ET)
FLATTEN_TIME = time(16, 0)   # flat by 16:00 ET — no overnight, ever
ENTRY_WINDOW_OPEN = time(9, 30)   # ICT entry window (RTH portion)
ENTRY_WINDOW_CLOSE = time(12, 0)  # no new entries after noon ET
MAX_DAILY_LOSS = 500.0


# ---------------------------------------------------------------------------
# Pure guards
# ---------------------------------------------------------------------------

def now_et() -> datetime:
    return datetime.now(ET)


def _time_of(now: datetime) -> time:
    return now.astimezone(ET).time() if now.tzinfo else now.time()


def in_rth(now: Optional[datetime] = None) -> bool:
    """True during the 09:30-16:00 ET RTH window."""
    now = now or now_et()
    return RTH_OPEN <= _time_of(now) < RTH_CLOSE


def should_flatten(now: Optional[datetime] = None) -> bool:
    """True at/after 16:00 ET — flatten everything, no overnight."""
    now = now or now_et()
    return _time_of(now) >= FLATTEN_TIME


def in_entry_window(now: Optional[datetime] = None) -> bool:
    """True during the 09:30-12:00 ET entry window (ICT: trade
    8:30am-noon NY; the RTH portion is 09:30-12:00 ET)."""
    now = now or now_et()
    return ENTRY_WINDOW_OPEN <= _time_of(now) < ENTRY_WINDOW_CLOSE


def daily_loss_halted(daily_pnl: float,
                      max_daily_loss: float = MAX_DAILY_LOSS) -> bool:
    """Halt new entries once daily P&L hits -max_daily_loss."""
    return daily_pnl <= -abs(max_daily_loss)


def can_open(has_position: bool,
             trades_today: int,
             now: Optional[datetime] = None,
             daily_pnl: float = 0.0,
             max_daily_loss: float = MAX_DAILY_LOSS,
             max_trades_per_day: int = 2) -> Tuple[bool, str]:
    """Gate for a new entry. Returns (ok, reason).

    Order matters: the flatten check comes first so a call at/after
    16:00 ET can never open, whatever the other flags say.
    """
    now = now or now_et()
    if should_flatten(now):
        return False, "past flatten time (16:00 ET)"
    if not in_rth(now):
        return False, "outside RTH"
    if not in_entry_window(now):
        return False, "outside entry window (09:30-12:00 ET)"
    if has_position:
        return False, "position already open (max 1)"
    if trades_today >= max_trades_per_day:
        return False, f"daily trade cap reached ({trades_today}/{max_trades_per_day})"
    if daily_loss_halted(daily_pnl, max_daily_loss):
        return False, f"daily loss halt hit ({daily_pnl:.2f} <= -{max_daily_loss:.2f})"
    return True, "ok"
