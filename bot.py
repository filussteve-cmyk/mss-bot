"""MSS (ICT Market Structure Shift) bot — ES, paper only.

PAPER MODE ONLY. This bot connects to TopstepX via the ProjectX REST
client (read-only: contracts + bars), frames bias on 1H bars, detects
ICT market structure shifts on 15m bars (liquidity sweep ->
displacement/FVG/order block -> MSS), and "trades" the retracement
into the displacement FVG with a limit at the FVG midpoint — in a
paper executor. It NEVER calls any order-placement API, in any mode.

LIVE MODE IS NOT IMPLEMENTED AND NOT APPROVED: the bot refuses to start
if config mode is anything other than "paper". See README for what
Steve must approve before that ever changes.

Like the S4 / MultiScan / Push350 bots, this uses the ProjectX REST
client directly — NOT TradingSuite, whose realtime WebSockets cannot
traverse this machine's egress proxy. Polling at 60s only needs REST.

Loop ordering (the Push350 fix, kept): the 16:00 ET flatten check sits
ABOVE any RTH-close exit, so the session can never end with the paper
position still open. No overnight, ever.

Modes:
  python3 bot.py            # paper run (config mode must be "paper")
  python3 bot.py --smoke    # offline smoke check: NO TopstepX
                            # authentication, NO orders, synthetic 1H /
                            # 15m / 5m bars through the real detector +
                            # bias + risk gates + paper executor.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

from models import Bar, MssSetup
import risk
from risk import ET
from paper_exec import (PaperExecutor, PaperPosition, PendingLimit,
                        ClosedTrade)
import strategy_mss
from strategy_mss import MssParams

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.yaml"
STATE_PATH = BASE / "state.json"

log = logging.getLogger("mss")


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    # Environment variables take precedence (never commit real keys).
    cfg["username"] = os.environ.get("TOPSTEPX_USERNAME", cfg.get("username"))
    cfg["api_key"] = os.environ.get("TOPSTEPX_API_KEY", cfg.get("api_key"))
    cfg["account_id"] = int(os.environ.get("TOPSTEPX_ACCOUNT_ID",
                                           cfg.get("account_id", 0)))
    # run_paper.sh forces paper mode even if config was edited.
    if os.environ.get("MSS_FORCE_PAPER") == "1":
        cfg["mode"] = "paper"
    return cfg


def _max_trades_per_day(cfg: dict):
    """Daily trade cap from config: 0, blank, or missing = no cap (None).

    A positive value caps entries per day; None/0 lets every setup
    through the trade-count gate (the -$500 halt still applies).
    """
    raw = cfg.get("max_trades_per_day", 0)
    if raw is None:
        return None
    value = int(raw)
    return None if value <= 0 else value


def mss_params_from_dict(d: dict) -> MssParams:
    md = d.get("mss", {}) or {}
    return MssParams(
        tick=float(d.get("market", {}).get("tick", 0.25)),
        swing_lookback_15m=int(md.get("swing_lookback_15m", 2)),
        swing_lookback_1h=int(md.get("swing_lookback_1h", 2)),
        min_displacement_points=float(md.get("min_displacement_points", 3.5)),
        displacement_search_bars=int(md.get("displacement_search_bars", 5)),
        displacement_max_bars=int(md.get("displacement_max_bars", 3)),
        ob_min_candles=int(md.get("ob_min_candles", 2)),
        ob_max_candles=int(md.get("ob_max_candles", 5)),
        entry_zone=str(md.get("entry_zone", "fvg_mid")),
        stop_buffer_points=float(md.get("stop_buffer_points", 1.0)),
        stop_buffer_ticks=int(md.get("stop_buffer_ticks", 1)),
        min_rr=float(md.get("min_rr", 1.5)),
        measured_move_rr=float(md.get("measured_move_rr", 2.0)),
        setup_max_bars_5m=int(md.get("setup_max_bars_5m", 12)),
        bias_lookback_1h_bars=int(md.get("bias_lookback_1h_bars", 40)),
    )


def blank_state() -> dict:
    return {
        "date": risk.now_et().strftime("%Y-%m-%d"),
        "seen_setup_keys": [],    # setup keys already decided on
        "voided_setup_keys": [],
        "done_setup_keys": [],
        "seen_partial_keys": [],  # partial-sweep keys already logged
        "partials_today": [],     # PartialSetup dicts logged today
        "armed_setup": None,      # MssSetup dict or None
        "trades_today": 0,
        "closed_trades": [],      # paper ClosedTrade dicts
        "open_position": None,    # paper PaperPosition dict or None
        "pending_limit": None,    # paper PendingLimit dict or None
        "daily_pnl": 0.0,
        "halted": False,
        "last_5m_idx": -1,
        "last_bias": "neutral",
    }


def load_state() -> dict:
    today = risk.now_et().strftime("%Y-%m-%d")
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
            if st.get("date") == today:
                return st
            log.info("New trading day — resetting daily state.")
        except Exception as e:
            log.warning("Could not read state.json (%s); starting fresh.", e)
    st = blank_state()
    st["date"] = today
    return st


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def executor_from_state(state: dict) -> PaperExecutor:
    ex = PaperExecutor()
    ex.closed = [ClosedTrade(**d) for d in state.get("closed_trades", [])]
    if state.get("open_position"):
        ex.position = PaperPosition.from_dict(state["open_position"])
    if state.get("pending_limit"):
        ex.pending = PendingLimit.from_dict(state["pending_limit"])
    return ex


def sync_state(state: dict, ex: PaperExecutor) -> None:
    state["closed_trades"] = [asdict(t) for t in ex.closed]
    state["open_position"] = ex.position.to_dict() if ex.position else None
    state["pending_limit"] = ex.pending.to_dict() if ex.pending else None
    state["daily_pnl"] = ex.realized_pnl()


def log_new_partials(state: dict, bars_15m: list, params: MssParams) -> None:
    """Log each near-miss sweep once (dedupe by PartialSetup.key).

    The old log dropped partial sweeps silently, so a quiet day hid
    whether anything came close to the 4-pt displacement threshold.
    Updates state["seen_partial_keys"] / state["partials_today"];
    the caller saves state.
    """
    seen_p = set(state.get("seen_partial_keys", []))
    for p in strategy_mss.scan_partials(bars_15m, params):
        if p.key in seen_p:
            continue
        seen_p.add(p.key)
        state["seen_partial_keys"] = sorted(seen_p)
        state.setdefault("partials_today", []).append(asdict(p))
        log.info("PARTIAL MSS %s sweep %.2f @ 15m bar %d: %s "
                 "(best displacement %.2f pts vs %.2f min, FVG %s, "
                 "MSS %s, through 15m bar %d)",
                 p.direction, p.sweep_level, p.sweep_idx, p.reason,
                 p.best_disp_points, params.min_displacement_points,
                 "yes" if p.had_fvg else "no",
                 "yes" if p.had_mss else "no", p.last_bar_idx)


# ---------------------------------------------------------------------------
# Smoke check (offline, no broker, no orders)
# ---------------------------------------------------------------------------

def _smoke_1h_bars():
    """1H staircase: swing highs 7620 -> 7640, swing lows 7606 -> 7626,
    last close 7634 holds above the last swing low -> bullish bias."""
    spec = [
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
    return [Bar(i, o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(spec)]


def _smoke_15m_bars():
    """Textbook bullish MSS on ES: swing high 7660 (idx 2), swing low
    7640 (idx 6, the pool), sweep to 7638 (idx 9), displacement
    idx 10-12 leaving FVG [7651, 7655], MSS through 7660 at idx 11."""
    spec = [
        (7652, 7654, 7650, 7652),
        (7652, 7655, 7651, 7654),
        (7654, 7660, 7653, 7658),
        (7658, 7659, 7652, 7654),
        (7654, 7655, 7648, 7650),
        (7650, 7651, 7646, 7648),
        (7648, 7650, 7640, 7642),
        (7642, 7646, 7642, 7644),
        (7648, 7649, 7642, 7644),
        (7644, 7646, 7638, 7640),
        (7640, 7651, 7640, 7650),
        (7650, 7662, 7656, 7660),
        (7660, 7666, 7655, 7664),
    ]
    return [Bar(i, o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(spec)]


def _smoke_5m_bars():
    """Retracement into the FVG midpoint 7653, then a rally to the
    2R measured-move target 7685.50."""
    spec = [
        (7664, 7665, 7652, 7655),   # fills the limit at 7653
        (7655, 7670, 7654, 7668),
        (7668, 7680, 7667, 7678),
        (7678, 7686, 7677, 7684),   # target 7685.50 hit
    ]
    return [Bar(i, o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(spec)]


def run_smoke(cfg: dict) -> int:
    """Detector + bias + risk + paper executor on synthetic bars."""
    log.info("PAPER SMOKE: offline check — no TopstepX authentication, "
             "ZERO orders will be placed.")
    params = mss_params_from_dict(cfg)
    market = cfg.get("market", {})
    point_value = float(market.get("point_value", 50.0))

    bias = strategy_mss.htf_bias(_smoke_1h_bars(), params)
    assert bias == "bullish", f"smoke bias failed: {bias}"
    log.info("SMOKE bias: %s", bias)

    setups = strategy_mss.scan_15m(_smoke_15m_bars(), params)
    assert len(setups) == 1, f"smoke scan failed: {setups}"
    s = setups[0]
    assert s.direction == "long", s
    assert s.entry == 7653.0, f"entry {s.entry}"
    assert s.stop == 7636.75, f"stop {s.stop}"
    assert s.target == 7685.5, f"target {s.target}"
    assert s.target_method == "measured_move", s.target_method
    log.info("SMOKE setup: %s sweep %.2f -> disp %d-%d -> MSS bar %d, "
             "FVG [%.2f, %.2f] entry %.2f stop %.2f target %.2f (%s, "
             "risk %.2f pts)",
             s.direction, s.sweep_level, s.disp_start_idx, s.disp_end_idx,
             s.mss_idx, s.fvg_bottom, s.fvg_top, s.entry, s.stop,
             s.target, s.target_method, s.risk_points)

    ok, reason = risk.can_open(
        has_position=False, trades_today=0,
        now=datetime(2026, 11, 11, 10, 0, tzinfo=ET), daily_pnl=0.0,
        max_daily_loss=float(cfg.get("max_daily_loss", 500.0)),
        max_trades_per_day=_max_trades_per_day(cfg))
    assert ok, f"smoke risk gate failed: {reason}"

    ex = PaperExecutor()
    assert ex.place_limit(s, "ES", point_value=point_value, size=1), \
        "smoke limit placement failed"
    trade = None
    for bar in _smoke_5m_bars():
        ev = strategy_mss.evaluate_entry_bar(bar, s)
        res = ex.on_bar(bar)
        if isinstance(res, PaperPosition):
            log.info("SMOKE paper fill: LONG @ %.2f (FVG mid), stop %.2f, "
                     "target %.2f", res.entry, res.stop, res.target)
        if isinstance(res, ClosedTrade):
            trade = res
    assert trade is not None, "smoke: position never closed"
    assert trade.exit_reason == "target", f"smoke exit: {trade}"
    assert trade.pnl_points == 32.5, f"smoke pnl pts: {trade.pnl_points}"
    assert trade.pnl_dollars == 1625.0, f"smoke pnl $: {trade.pnl_dollars}"
    log.info("SMOKE paper trade closed: LONG entry=%.2f exit=%.2f via %s "
             "(%+.2f pts, $%+.2f)", trade.entry, trade.exit_price,
             trade.exit_reason, trade.pnl_points, trade.pnl_dollars)
    log.info("PAPER SMOKE PASSED: bias + sweep -> displacement/FVG/OB -> "
             "MSS -> FVG-mid fill -> 2R target — ZERO orders placed.")
    return 0


# ---------------------------------------------------------------------------
# Broker bar helpers (read-only)
# ---------------------------------------------------------------------------

async def resolve_front_month(client, symbol: str) -> str:
    """Return the front-month contract id for a product symbol: the
    contract whose name starts with the product symbol and carries the
    activeContract flag."""
    contracts = await client.list_available_contracts(live=False)
    for c in contracts:
        name = (c.name or "").upper()
        if name.startswith(symbol.upper()) and c.activeContract:
            log.info("%s front-month contract: %s (%s)", symbol, c.id, c.name)
            return c.id
    raise RuntimeError(f"No activeContract front-month contract for {symbol}.")


def bars_from_df(df, trade_date: str, rth_only: bool = True):
    """Convert the SDK's polars bar DataFrame to a Bar list.

    RTH bars are filtered to today's 09:30-16:00 ET session; 1H bars
    (rth_only=False) keep the last two days for the bias lookback.
    """
    bars = []
    for row in df.to_dicts():
        ts = row["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)  # pragma: no cover - defensive
        else:
            ts = ts.astimezone(ET)
        if rth_only:
            if ts.strftime("%Y-%m-%d") != trade_date:
                continue
            t = ts.time()
            if not (risk.RTH_OPEN <= t < risk.RTH_CLOSE):
                continue
        bars.append(Bar(idx=len(bars), open=row["open"], high=row["high"],
                        low=row["low"], close=row["close"],
                        volume=float(row.get("volume") or 0.0)))
    return bars


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true",
                        help="offline paper smoke check (no auth, no orders)")
    args = parser.parse_args()

    cfg = load_config()
    if args.smoke:
        sys.exit(run_smoke(cfg))

    mode = str(cfg.get("mode", "paper")).lower()
    if mode != "paper":
        log.error("Mode %r requested. LIVE MODE IS NOT IMPLEMENTED AND NOT "
                  "APPROVED for this bot. Set mode: \"paper\" — see README.",
                  mode)
        sys.exit(2)
    if not cfg.get("username") or not cfg.get("api_key") \
            or "YOUR_" in str(cfg.get("username", "")) \
            or "YOUR_" in str(cfg.get("api_key", "")):
        log.error("TopstepX credentials are placeholders. Set TOPSTEPX_USERNAME / "
                  "TOPSTEPX_API_KEY env vars (never print them).")
        sys.exit(2)

    params = mss_params_from_dict(cfg)
    market = cfg.get("market", {})
    symbol = str(market.get("symbol", "ES"))
    point_value = float(market.get("point_value", 50.0))
    size = int(cfg.get("size", 1))
    max_daily_loss = float(cfg.get("max_daily_loss", 500.0))
    max_trades = _max_trades_per_day(cfg)
    poll_seconds = int(cfg.get("poll_seconds", 60))

    log.info("MSS starting in PAPER mode: %s, ZERO orders will be placed.",
             symbol)
    log.info("Guardrails: max_trades_per_day=%s, min_displacement=%.2f pts, "
             "halt -$%.0f, entry 09:30-12:00 ET, flat 16:00 ET.",
             "uncapped" if max_trades is None else max_trades,
             params.min_displacement_points, max_daily_loss)

    # ProjectX REST client directly — NOT TradingSuite (its realtime
    # WebSockets cannot traverse this machine's egress proxy). This bot
    # only ever reads: list contracts + get bars. There is no order
    # code path in this file at all.
    from project_x_py import ProjectX

    client = ProjectX(username=cfg["username"], api_key=cfg["api_key"])
    await client.authenticate()
    log.info("Connected to TopstepX (REST, read-only usage).")

    try:
        contract_id = await resolve_front_month(client, symbol)
    except Exception as e:
        log.error("Could not resolve front month for %s: %s", symbol, e)
        sys.exit(2)

    state = load_state()
    executor = executor_from_state(state)
    armed: MssSetup | None = None
    if state.get("armed_setup"):
        armed = MssSetup.from_dict(state["armed_setup"])
        log.warning("Startup: armed MSS setup restored (%s entry %.2f).",
                    armed.direction, armed.entry)
    if executor.has_position:
        log.warning("Startup: paper position restored from state (%s @ %.2f).",
                    executor.position.direction, executor.position.entry)
    if executor.has_pending:
        log.warning("Startup: pending limit restored from state (%s @ %.2f).",
                    executor.pending.direction, executor.pending.entry)
    last_bars_5m: list = []
    last_close_5m: float | None = None
    last_bias = state.get("last_bias", "neutral")
    # Fresh-bar lists, initialized here so the 16:00 flatten branch can
    # run its final partial-sweep pass even on the first iteration
    # (e.g. a post-16:00 restart), when no refresh has happened yet.
    bars_1h: list = []
    bars_15m: list = []
    bars_5m: list = []

    while True:
        now = risk.now_et()
        today = now.strftime("%Y-%m-%d")
        if state.get("date") != today:
            state = blank_state()
            state["date"] = today
            executor = PaperExecutor()
            armed = None
            last_bars_5m, last_close_5m = [], None
            log.info("New trading day %s.", today)

        # 1) 16:00 FLATTEN — FIRST, before any session-end exit.
        if risk.should_flatten(now):
            if executor.has_position:
                px = last_close_5m if last_close_5m is not None \
                    else executor.position.entry
                sk = executor.position.setup_key
                trade = executor.flatten(px)
                state["trades_today"] += 1
                if sk:
                    state["done_setup_keys"].append(sk)
                armed = None
                state["armed_setup"] = None
                log.warning("16:00 ET flatten: paper position closed @ %.2f "
                            "(%+.2f pts, $%+.2f).", px, trade.pnl_points,
                            trade.pnl_dollars)
            if executor.has_pending:
                executor.cancel_pending()
                log.warning("16:00 ET flatten: pending limit cancelled.")
                armed = None
                state["armed_setup"] = None
            sync_state(state, executor)
            save_state(state)
            # Final partial-sweep pass: anything the bot was too busy
            # to log while a position/limit was open still lands in
            # the log for the wrap, with a one-line summary.
            if bars_15m:
                log_new_partials(state, bars_15m, params)
                save_state(state)
            parts = state.get("partials_today", [])
            if parts:
                closest = max(parts,
                              key=lambda d: d["best_disp_points"])
                log.info("MSS partials today: %d near-miss sweep(s); "
                         "closest was %s sweep %.2f, %.2f pts (%s).",
                         len(parts), closest["direction"],
                         closest["sweep_level"],
                         closest["best_disp_points"], closest["reason"])
            log.info("Session over (16:00 ET). Exiting.")
            break

        # 2) Outside RTH (pre-open): wait.
        if not risk.in_rth(now):
            await asyncio.sleep(poll_seconds)
            continue

        if risk.daily_loss_halted(state["daily_pnl"], max_daily_loss) \
                and not state.get("halted"):
            log.error("Daily loss halt hit (%.2f). No new entries today.",
                      state["daily_pnl"])
            state["halted"] = True
            save_state(state)

        # 3) Refresh bars: 1H (bias), 15m (detection), 5m (entry/mgmt).
        bars_1h, bars_15m, bars_5m = [], [], []
        try:
            df = await client.get_bars(symbol=symbol, days=2,
                                       interval=60, unit=2)
            bars_1h = bars_from_df(df, today, rth_only=False)[-40:]
        except Exception as e:
            log.warning("get_bars 1H failed: %s", e)
        try:
            df = await client.get_bars(symbol=symbol, days=2,
                                       interval=15, unit=2)
            bars_15m = bars_from_df(df, today, rth_only=True)
        except Exception as e:
            log.warning("get_bars 15m failed: %s", e)
        try:
            df = await client.get_bars(symbol=symbol, days=2,
                                       interval=5, unit=2)
            bars_5m = bars_from_df(df, today, rth_only=True)
        except Exception as e:
            log.warning("get_bars 5m failed: %s", e)
        if bars_5m:
            last_bars_5m = bars_5m
            last_close_5m = bars_5m[-1].close

        # 4) HTF bias.
        if bars_1h:
            bias = strategy_mss.htf_bias(bars_1h, params)
            if bias != last_bias:
                log.info("HTF bias: %s -> %s", last_bias, bias)
                last_bias = bias
                state["last_bias"] = bias
                save_state(state)
        else:
            bias = last_bias

        # 5) Manage the pending limit / open position on NEW 5m bars.
        new_5m = [b for b in last_bars_5m
                  if b.idx > state.get("last_5m_idx", -1)]
        for bar in new_5m:
            state["last_5m_idx"] = bar.idx
            if executor.has_pending and armed is not None:
                # Bias flip voids the idea (trade only in HTF direction).
                if bias != "neutral" and bias != armed.direction:
                    pend = executor.cancel_pending()
                    state["voided_setup_keys"].append(armed.key)
                    log.warning("Setup voided: HTF bias flipped to %s "
                                "(setup was %s).", bias, armed.direction)
                    armed = None
                    state["armed_setup"] = None
                    sync_state(state, executor)
                    save_state(state)
                    continue
                # Entry window end voids the idea.
                if not risk.in_entry_window(now):
                    executor.cancel_pending()
                    state["voided_setup_keys"].append(armed.key)
                    log.warning("Setup voided: entry window ended (12:00 ET).")
                    armed = None
                    state["armed_setup"] = None
                    sync_state(state, executor)
                    save_state(state)
                    continue
                ev = strategy_mss.evaluate_entry_bar(bar, armed)
                if ev == "void_gap":
                    executor.cancel_pending()
                    state["voided_setup_keys"].append(armed.key)
                    log.warning("Setup voided: price gapped through the "
                                "swept level without filling.")
                    armed = None
                    state["armed_setup"] = None
                    sync_state(state, executor)
                    save_state(state)
                    continue
                res = executor.on_bar(bar)
                if isinstance(res, PaperPosition):
                    log.warning("PAPER entry: MSS %s @ %.2f stop=%.2f "
                                "target=%.2f (%s) — no order placed.",
                                res.direction.upper(), res.entry, res.stop,
                                res.target, armed.target_method)
                    sync_state(state, executor)
                    save_state(state)
                elif executor.has_pending and \
                        executor.pending.bars_seen >= params.setup_max_bars_5m:
                    executor.cancel_pending()
                    state["voided_setup_keys"].append(armed.key)
                    log.warning("Setup voided: no fill in %d 5m bars.",
                                params.setup_max_bars_5m)
                    armed = None
                    state["armed_setup"] = None
                    sync_state(state, executor)
                    save_state(state)
            elif executor.has_position:
                sk = executor.position.setup_key
                res = executor.on_bar(bar)
                if isinstance(res, ClosedTrade):
                    state["trades_today"] += 1
                    if sk:
                        state["done_setup_keys"].append(sk)
                    if armed is not None:
                        armed = None
                        state["armed_setup"] = None
                    log.warning("PAPER exit: MSS %s entry=%.2f exit=%.2f via %s "
                                "(%+.2f pts, $%+.2f). Day P&L $%+.2f.",
                                res.direction, res.entry, res.exit_price,
                                res.exit_reason, res.pnl_points,
                                res.pnl_dollars, executor.realized_pnl())
                    sync_state(state, executor)
                    save_state(state)

        # 6) Scan 15m for newly armed setups; decide once, never carry.
        if bars_15m and not executor.has_position \
                and not executor.has_pending and not state.get("halted"):
            # Log near-miss sweeps once (see log_new_partials).
            log_new_partials(state, bars_15m, params)
            save_state(state)
            seen = set(state.get("seen_setup_keys", []))
            voided = set(state.get("voided_setup_keys", []))
            done = set(state.get("done_setup_keys", []))
            for setup in strategy_mss.scan_15m(bars_15m, params):
                if setup.key in seen or setup.key in voided \
                        or setup.key in done:
                    continue
                seen.add(setup.key)
                state["seen_setup_keys"] = sorted(seen)
                log.info("MSS SETUP %s sweep %.2f -> disp %d-%d -> MSS bar %d, "
                         "FVG [%.2f, %.2f] entry %.2f stop %.2f target %.2f "
                         "(%s, risk %.2f pts)",
                         setup.direction, setup.sweep_level,
                         setup.disp_start_idx, setup.disp_end_idx,
                         setup.mss_idx, setup.fvg_bottom, setup.fvg_top,
                         setup.entry, setup.stop, setup.target,
                         setup.target_method, setup.risk_points)
                if setup.direction != bias:
                    log.info("Setup skipped: direction %s != HTF bias %s.",
                             setup.direction, bias)
                    continue
                if not risk.in_entry_window(now):
                    log.info("Setup skipped: outside entry window "
                             "(09:30-12:00 ET).")
                    continue
                ok, reason = risk.can_open(
                    has_position=False, trades_today=state["trades_today"],
                    now=now, daily_pnl=state["daily_pnl"],
                    max_daily_loss=max_daily_loss,
                    max_trades_per_day=max_trades)
                if not ok:
                    log.info("Entry blocked by risk: %s", reason)
                    continue
                if executor.place_limit(setup, symbol,
                                        point_value=point_value, size=size):
                    armed = setup
                    state["armed_setup"] = setup.to_dict()
                    log.warning("PAPER limit armed: MSS %s @ %.2f stop=%.2f "
                                "target=%.2f (%s) — no order placed.",
                                setup.direction.upper(), setup.entry,
                                setup.stop, setup.target,
                                setup.target_method)
                    sync_state(state, executor)
                    save_state(state)
                    break  # one setup at a time
            else:
                save_state(state)

        await asyncio.sleep(poll_seconds)

    log.info("MSS bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
