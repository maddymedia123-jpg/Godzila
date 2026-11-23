"""Trend Paper Trade: daily trend rule tracked with pretend money (no real orders).

Rules (locked; do not tune while paper trading):
  * Coins: BTC, ETH, SOL, XRP, DOGE USDT perpetual swaps on OKX.
  * Pretend account split into 15 equal slices: 5 coins x 3 averages (30, 40, 50 days).
  * At each daily close (00:00 UTC = 05:00 PKT) a slice is LONG if the close is above its
    simple moving average, otherwise it is in CASH. Long only, no leverage.
  * Costs: 0.05% taker fee on every entry and exit; funding paid/received while long.

Everything is rebuilt from OKX daily candles on each page load, so app restarts and
Streamlit sleep lose nothing.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
AVERAGES = [30, 40, 50]
START_DATE = pd.Timestamp("2026-11-25")  # first day a position can be held
START_BALANCE = 10_000.0
FEE = 0.0005  # per side
BACKTEST_DAYS = 180

OKX = "https://www.okx.com"


def _get(path, params):
    url = f"{OKX}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "godzilla-trend-paper"})
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode())
    if payload.get("code") not in ("0", 0):
        raise RuntimeError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
    return payload.get("data", [])


def fetch_daily_closes(coin, days):
    """Completed UTC daily closes, oldest first. Index = UTC day (date of the candle)."""
    rows, after = [], None
    while len(rows) < days:
        params = {"instId": f"{coin}-USDT-SWAP", "bar": "1Dutc", "limit": "100"}
        if after:
            params["after"] = after
        data = _get("/api/v5/market/history-candles", params)
        if not data:
            break
        rows.extend(data)
        after = data[-1][0]
        if len(data) < 100:
            break
    recs = [(int(r[0]), float(r[4])) for r in rows if len(r) < 9 or r[8] == "1"]
    s = pd.Series({pd.to_datetime(t, unit="ms").normalize(): c for t, c in recs}).sort_index()
    return s[~s.index.duplicated()]


def fetch_daily_funding(coin):
    """Sum of funding rates per UTC day (OKX keeps roughly the last 3 months)."""
    rows, after = [], None
    for _ in range(4):
        params = {"instId": f"{coin}-USDT-SWAP", "limit": "100"}
        if after:
            params["after"] = after
        data = _get("/api/v5/public/funding-rate-history", params)
        if not data:
            break
        rows.extend(data)
        after = data[-1]["fundingTime"]
        if len(data) < 100:
            break
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame({
        "d": pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms").normalize(),
        "r": [float(r.get("realizedRate") or r.get("fundingRate") or 0.0) for r in rows],
    }).drop_duplicates()
    return df.groupby("d")["r"].sum()


def load_market(days=BACKTEST_DAYS + max(AVERAGES) + 20):
    closes, funding = {}, {}
    for c in COINS:
        closes[c] = fetch_daily_closes(c, days)
        try:
            funding[c] = fetch_daily_funding(c)
        except Exception:
            funding[c] = pd.Series(dtype=float)
    return closes, funding


def run_paper(closes, funding, start):
    """Simulate the 15 slices from `start` (first day a position is held).

    Decision at close of day d-1 sets the position held during day d.
    Returns (equity DataFrame, trades list, positions now list, funding_known flag).
    """
    slice_cap = START_BALANCE / (len(COINS) * len(AVERAGES))
    eq_cols, trades, now_pos = {}, [], []
    funding_gap = False
    for coin in COINS:
        c = closes[coin].dropna()
        f = funding.get(coin, pd.Series(dtype=float))
        f_first = f.index.min() if len(f) else None
        for n in AVERAGES:
            sma = c.rolling(n).mean()
            sig = (c > sma) & sma.notna()
            days = c.index[c.index >= start]
            val, long_, entry = slice_cap, False, None
            curve = {}
            # position decided at the close before the first day
            prev_days = c.index[c.index < start]
            if len(prev_days):
                d0 = prev_days[-1]
                if sig.loc[d0]:
                    long_, entry = True, (d0, c.loc[d0], val)
                    val *= 1 - FEE
            for d in days:
                prev = c.index[c.index.get_loc(d) - 1]
                if long_:
                    r = c.loc[d] / c.loc[prev] - 1
                    fr = float(f.get(d, 0.0))
                    if f_first is None or d < f_first:
                        funding_gap = True
                    val *= 1 + r - fr
                # decide at today's close for tomorrow
                want = bool(sig.loc[d])
                if want and not long_:
                    val *= 1 - FEE
                    long_, entry = True, (d, c.loc[d], val)
                elif long_ and not want:
                    val *= 1 - FEE
                    trades.append(dict(
                        coin=coin, avg=n, entry_close=entry[0], entry_px=entry[1],
                        exit_close=d, exit_px=c.loc[d],
                        pnl_pct=(val / entry[2] * (1 - FEE) - 1) * 100 if entry[2] else 0.0,
                        days=(d - entry[0]).days, status="closed"))
                    long_, entry = False, None
                curve[d] = val
            if long_:
                last = c.index[-1]
                trades.append(dict(
                    coin=coin, avg=n, entry_close=entry[0], entry_px=entry[1],
                    exit_close=None, exit_px=c.loc[last],
                    pnl_pct=(val / entry[2] * (1 - FEE) - 1) * 100,
                    days=(last - entry[0]).days, status="open"))
            eq_cols[f"{coin}-{n}"] = pd.Series(curve)
            last = c.index[-1]
            now_pos.append(dict(coin=coin, avg=n, close=c.loc[last], sma=sma.loc[last],
                                long=bool(sig.loc[last]), slice_value=val))
    eq = pd.DataFrame(eq_cols).sort_index().ffill()
    eq["total"] = eq.sum(axis=1)
    # benchmark: equal dollars in each coin at the start, held
    bench = 0
    for coin in COINS:
        c = closes[coin].dropna()
        prev_days = c.index[c.index < start]
        if not len(prev_days):
            continue
        base = c.loc[prev_days[-1]]
        bench = bench + (c[c.index >= start] / base) * (START_BALANCE / len(COINS)) * (1 - FEE)
    eq["buy_and_hold"] = bench
    return eq, trades, now_pos, funding_gap


def next_close_utc(now=None):
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ----------------------------------------------------------------------------- UI
def render():
    import plotly.graph_objects as go
    import streamlit as st

    @st.cache_data(ttl=900, show_spinner="Loading OKX daily candles...")
    def _load():
        return load_market()

    st.markdown("### 📈 Trend Paper Trade — 30/40/50-day average, long only")
    st.caption(
        "Pretend money only, no real orders. Rules are locked: BTC, ETH, SOL, XRP, DOGE perps; "
        "$10,000 split into 15 equal parts (5 coins × 30/40/50-day averages). At each daily close "
        "(05:00 PKT) a part is LONG if the close is above its average, otherwise CASH. "
        "0.05% fee per entry/exit and funding included."
    )
    try:
        closes, funding = _load()
    except Exception as e:  # network/API problem
        st.error(f"Could not load OKX daily candles: {e}")
        return

    last_day = min(s.index[-1] for s in closes.values())
    nxt = next_close_utc()
    pkt = timezone(timedelta(hours=5))
    hrs = (nxt - datetime.now(timezone.utc)).total_seconds() / 3600
    st.markdown(
        f"Last completed daily close: **{last_day:%d %b %Y}** · Next close: "
        f"**{nxt.astimezone(pkt):%d %b %H:%M} PKT** (in {hrs:.1f} h)"
    )

    started = last_day >= START_DATE - pd.Timedelta(days=1)
    start = START_DATE if started else last_day + pd.Timedelta(days=1)
    eq, trades, now_pos, fgap = run_paper(closes, funding, start)

    # --- today's positions
    st.markdown("#### What to hold today")
    cols = st.columns(len(COINS))
    for col, coin in zip(cols, COINS):
        ps = [p for p in now_pos if p["coin"] == coin]
        n_long = sum(p["long"] for p in ps)
        color = "#34d399" if n_long == 3 else "#facc15" if n_long else "#94a3b8"
        lines = "<br>".join(
            f"{p['avg']}d avg {_px(p['sma'])}: <b>{'LONG' if p['long'] else 'CASH'}</b>" for p in ps)
        col.markdown(
            f"<div style='border:1px solid {color};border-radius:8px;padding:10px;'>"
            f"<div style='font-weight:800;color:{color};'>{coin} · {n_long}/3 long</div>"
            f"<div style='font-size:0.85em;'>Close {_px(ps[0]['close'])}<br>{lines}</div></div>",
            unsafe_allow_html=True)

    # --- paper account
    st.markdown("#### Paper account")
    if not started or eq.empty:
        st.info(f"Paper trading starts with the close of {START_DATE - pd.Timedelta(days=1):%d %b %Y} "
                f"(05:00 PKT {START_DATE:%d %b}). The positions above are what it will hold first.")
    else:
        total = eq["total"].iloc[-1]
        bh = eq["buy_and_hold"].iloc[-1]
        peak_dd = (eq["total"] / eq["total"].cummax() - 1).min() * 100
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Paper balance", f"${total:,.0f}", f"{(total / START_BALANCE - 1) * 100:+.1f}%")
        m2.metric("Buy & hold all 5", f"${bh:,.0f}", f"{(bh / START_BALANCE - 1) * 100:+.1f}%")
        m3.metric("Worst drop from peak", f"{peak_dd:.1f}%")
        m4.metric("Days running", f"{len(eq)}")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eq.index, y=eq["total"], name="Paper (trend rule)"))
        fig.add_trace(go.Scatter(x=eq.index, y=eq["buy_and_hold"], name="Buy & hold"))
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="$")
        st.plotly_chart(fig, width="stretch")
        if fgap:
            st.caption("Note: OKX only returns ~3 months of funding history; older days are counted as 0 funding.")
        _trade_table(st, trades)

    # --- same rules on recent history (clearly a backtest)
    with st.expander(f"Same rules on the last {BACKTEST_DAYS} days (backtest, not paper)"):
        bt_start = last_day - pd.Timedelta(days=BACKTEST_DAYS - 1)
        beq, btr, _, bgap = run_paper(closes, funding, bt_start)
        if not beq.empty:
            t, b = beq["total"].iloc[-1], beq["buy_and_hold"].iloc[-1]
            st.markdown(f"Trend rule **{(t / START_BALANCE - 1) * 100:+.1f}%** vs buy & hold "
                        f"**{(b / START_BALANCE - 1) * 100:+.1f}%** since {bt_start:%d %b %Y}.")
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=beq.index, y=beq["total"], name="Trend rule"))
            fig.add_trace(go.Scatter(x=beq.index, y=beq["buy_and_hold"], name="Buy & hold"))
            fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="$")
            st.plotly_chart(fig, width="stretch")
            if bgap:
                st.caption("Funding older than ~3 months is not available from OKX and is counted as 0.")
            _trade_table(st, btr)


def _trade_table(st, trades):
    if not trades:
        st.caption("No trades yet.")
        return
    df = pd.DataFrame(trades).sort_values("entry_close", ascending=False)
    df["entry_close"] = df["entry_close"].dt.strftime("%d %b %Y")
    df["exit_close"] = df["exit_close"].apply(lambda x: x.strftime("%d %b %Y") if x is not None and not pd.isna(x) else "open")
    df["pnl_pct"] = df["pnl_pct"].round(2)
    df = df.rename(columns={"coin": "Coin", "avg": "Avg (days)", "entry_close": "Entry close",
                            "entry_px": "Entry price", "exit_close": "Exit close", "exit_px": "Exit/last price",
                            "pnl_pct": "P&L %", "days": "Days", "status": "Status"})
    st.dataframe(df, width="stretch", hide_index=True)


def _px(v):
    v = float(v)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.5f}"
