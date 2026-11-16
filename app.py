import datetime
import time
from typing import List
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from stream_manager import BinanceFuturesStreamManager, WATCHLIST_SYMBOLS

# -----------------------------------------------------------------------------
# Timezone Helpers (PKT = UTC+5 & UTC)
# -----------------------------------------------------------------------------
PKT_TZ = datetime.timezone(datetime.timedelta(hours=5), name="PKT")
UTC_TZ = datetime.timezone.utc


def format_dual_time(ts: float) -> str:
    dt_pkt = datetime.datetime.fromtimestamp(ts, tz=PKT_TZ).strftime("%H:%M:%S PKT")
    dt_utc = datetime.datetime.fromtimestamp(ts, tz=UTC_TZ).strftime("%H:%M:%S UTC")
    return f"{dt_pkt} ({dt_utc})"


# -----------------------------------------------------------------------------
# Page Configuration & High-Contrast Neutral Readable Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Institutional Order Flow & Student Auto-Signals",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
.stApp {
    background-color: #F1F5F9;
    color: #0F172A;
}
section[data-testid="stSidebar"] {
    background-color: #E2E8F0;
    border-right: 1px solid #CBD5E1;
}
.neutral-card {
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
    color: #0F172A;
}
.neutral-label {
    font-size: 0.74rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #475569;
    margin-bottom: 4px;
}
.neutral-value {
    font-size: 1.38rem;
    font-weight: 800;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: #0F172A;
}
.preset-card {
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 10px;
    padding: 18px;
    box-shadow: 0 2px 5px rgba(15, 23, 42, 0.06);
    color: #0F172A;
}
.price-box {
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    padding: 12px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.88rem;
    margin: 10px 0;
}
.price-row {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    border-bottom: 1px solid #E2E8F0;
}
.price-row:last-child {
    border-bottom: none;
}
.badge-long {
    background-color: #DCFCE7;
    color: #166534;
    border: 1px solid #86EFAC;
    padding: 3px 10px;
    border-radius: 5px;
    font-weight: 800;
    font-size: 0.8rem;
    font-family: monospace;
}
.badge-short {
    background-color: #FEE2E2;
    color: #991B1B;
    border: 1px solid #FCA5A5;
    padding: 3px 10px;
    border-radius: 5px;
    font-weight: 800;
    font-size: 0.8rem;
    font-family: monospace;
}
.badge-neutral {
    background-color: #E2E8F0;
    color: #334155;
    border: 1px solid #CBD5E1;
    padding: 3px 9px;
    border-radius: 5px;
    font-weight: 700;
    font-size: 0.78rem;
    font-family: monospace;
}
.warning-strip {
    background-color: #FEF3C7;
    border: 1px solid #FCD34D;
    color: #92400E;
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 0.78rem;
    font-family: monospace;
    margin-top: 8px;
}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_stream_manager() -> BinanceFuturesStreamManager:
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")


manager = get_stream_manager()


def fmt_price(val: float) -> str:
    if val >= 100:
        return f"${val:,.2f}"
    elif val >= 1:
        return f"${val:,.4f}"
    return f"${val:,.5f}"


def parse_zones(zones_str: str) -> List[float]:
    zones = []
    for token in zones_str.replace("\n", ",").split(","):
        token = token.strip()
        if token:
            try:
                zones.append(float(token))
            except ValueError:
                pass
    return sorted(zones)


# -----------------------------------------------------------------------------
# Top Control Header: Live Clock (PKT + UTC) & Active Scanner Switch
# -----------------------------------------------------------------------------
now_ts = time.time()
top_left, top_mid, top_right = st.columns([2.2, 1.3, 1.3])
with top_left:
    st.markdown("### ⚡ Institutional Order Flow & Student Auto-Signals")
    st.caption(f"🕒 Server Clock: **{format_dual_time(now_ts)}**")
with top_mid:
    scanner_active = st.toggle(
        "🟢 LIVE AUTO-SCANNER: ON",
        value=True,
        help="When ON, continuously scans BTC, ETH, SOL, XRP, and DOGE across 15m, 1H, and 4H structures.",
    )
with top_right:
    auto_refresh = st.toggle("⚡ Real-Time UI Stream (500ms)", value=True)

# -----------------------------------------------------------------------------
# Sidebar Controls (Pro Microstructure Tuning)
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🛠️ Pro Terminal Settings")
    pro_symbol = st.selectbox("Pro Terminal Contract", options=WATCHLIST_SYMBOLS, index=0)

    col_bal, col_risk = st.columns(2)
    with col_bal:
        account_balance = st.number_input("Pro Equity ($)", min_value=100.0, value=25000.0, step=500.0)
    with col_risk:
        risk_pct = st.number_input("Pro Risk (%)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)

    st.markdown("---")
    decay_lambda = st.slider("Depth Decay (λ)", min_value=0.05, max_value=1.00, value=0.35, step=0.05)
    imbalance_threshold = st.slider("Imbalance Trigger (|I_t|)", min_value=0.15, max_value=0.85, value=0.40, step=0.05)
    max_spread_bps = st.slider("Max Spread Friction (bps)", min_value=0.5, max_value=25.0, value=5.0, step=0.5)
    garch_k = st.slider("GARCH Stop Multiplier (k)", min_value=0.5, max_value=4.0, value=1.5, step=0.1)

    st.markdown("---")
    zones_input = st.text_area(
        "Manual Pro S/R Levels (optional)",
        value="",
        placeholder="e.g. 64800, 65500, 66200",
        height=70,
    )
    structural_zones = parse_zones(zones_input)

if "student_coin" not in st.session_state:
    st.session_state.student_coin = "SOLUSDT"
if "student_balance" not in st.session_state:
    st.session_state.student_balance = 1000.0

manager.update_config(
    active_symbol=pro_symbol,
    scanner_active=scanner_active,
    account_balance=account_balance,
    student_balance=st.session_state.student_balance,
    risk_pct=risk_pct,
    decay_lambda=decay_lambda,
    imbalance_threshold=imbalance_threshold,
    max_spread_phi=max_spread_bps / 10000.0,
    garch_k=garch_k,
    structural_zones=structural_zones,
)

# -----------------------------------------------------------------------------
# Two-Tab Architecture
# -----------------------------------------------------------------------------
tab_student, tab_pro = st.tabs(
    ["🎯 Student Auto-Signals (20x · 50x · 100x)", "🔬 Pro Institutional Order Book Terminal"]
)


# =============================================================================
# TAB 1: STUDENT AUTO-SIGNALS (NEUTRAL READABLE UI + AUTO-CROSSED STALE TRADES)
# =============================================================================
with tab_student:
    s_ctrl1, s_ctrl2 = st.columns([2.2, 1.0])
    with s_ctrl1:
        selected_coin = st.segmented_control(
            "1-Tap Coin Selector",
            options=WATCHLIST_SYMBOLS,
            default=st.session_state.student_coin,
            key="student_coin_selector",
        )
        if selected_coin:
            st.session_state.student_coin = selected_coin
    with s_ctrl2:
        st.session_state.student_balance = st.number_input(
            "My Trading Balance ($)",
            min_value=50.0,
            max_value=10_000_000.0,
            value=float(st.session_state.student_balance),
            step=100.0,
        )

    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_student_dashboard():
        snap = manager.get_snapshot(st.session_state.student_coin)
        metrics = snap.get("metrics", {})
        coin = snap["symbol"]

        if not scanner_active:
            st.warning("⏸️ **LIVE AUTO-SCANNER IS OFF.** Turn ON the top switch to resume live multi-coin scanning.")

        # Live 5-Coin Pulse Strip
        tickers = snap.get("coin_tickers", {})
        t_cols = st.columns(len(WATCHLIST_SYMBOLS))
        for idx, sym in enumerate(WATCHLIST_SYMBOLS):
            t_info = tickers.get(sym, {})
            p_val = t_info.get("mid_price", 0.0)
            imb_val = t_info.get("imbalance_t", 0.0)
            arrow = "▲ BUY PRESSURE" if imb_val > 0.15 else ("▼ SELL PRESSURE" if imb_val < -0.15 else "• BALANCED")
            with t_cols[idx]:
                card_html = (
                    f"<div class='neutral-card' style='padding:10px 12px;'>"
                    f"<div class='neutral-label'>{sym.replace('USDT','')} / USDT</div>"
                    f"<div style='font-size:1.15rem; font-weight:800; font-family:monospace; color:#0F172A;'>"
                    f"{fmt_price(p_val) if p_val > 0 else 'Loading...'}"
                    f"</div>"
                    f"<div style='font-size:0.74rem; color:#475569; font-family:monospace;'>{arrow} ({imb_val:+.2f})</div>"
                    f"</div>"
                )
                st.markdown(card_html, unsafe_allow_html=True)

        st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

        if not metrics:
            st.info(f"Calibrating live order book and 15m / 1H / 4H market structure for {coin}...")
            return

        tf_bias = metrics.get("tf_bias", {})
        profiles = snap.get("student_profiles", {})

        st.markdown(
            f"#### 📊 Live Auto-Calibrated Trade Plans for **{coin}** &nbsp;|&nbsp; "
            f"`15m: {tf_bias.get('15m','--')}` · `1H: {tf_bias.get('1H','--')}` · `4H: {tf_bias.get('4H','--')}`"
        )

        # Three Built-In Risk Profiles (20x / 50x / 100x Leverage)
        p_cols = st.columns(3)
        tier_order = [
            ("LOW RISK", "#15803D", "🛡️ Conservative · 20x Leverage · 0.5% Risk"),
            ("MEDIUM RISK", "#0369A1", "⚖️ Balanced · 50x Leverage · 1.0% Risk"),
            ("HIGH RISK", "#B45309", "🔥 Aggressive Scalp · 100x Leverage · 2.0% Risk"),
        ]

        for col, (tier_key, accent_color, subtitle) in zip(p_cols, tier_order):
            sig = profiles.get(tier_key)
            with col:
                if sig is None:
                    st.markdown("<div class='preset-card'>Calibrating preset...</div>", unsafe_allow_html=True)
                    continue

                badge_cls = "badge-long" if sig.direction == "LONG" else "badge-short"
                action_label = "BUY / LONG" if sig.direction == "LONG" else "SELL / SHORT"

                card_html = (
                    f"<div class='preset-card' style='border-top: 4px solid {accent_color};'>"
                    f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                    f"<span style='font-weight:800; font-size:1.05rem; color:{accent_color};'>{tier_key} ({sig.leverage}x)</span>"
                    f"<span class='{badge_cls}'>{action_label}</span>"
                    f"</div>"
                    f"<div style='font-size:0.78rem; color:#475569; margin-top:3px;'>{subtitle}</div>"
                    f"<div style='font-size:0.72rem; color:#64748B; font-family:monospace; margin-top:2px;'>Updated: {format_dual_time(sig.timestamp)}</div>"
                    f"<div class='price-box'>"
                    f"<div class='price-row'><span style='color:#475569;'>ENTRY PRICE:</span><b>{fmt_price(sig.entry_price)}</b></div>"
                    f"<div class='price-row'><span style='color:#475569;'>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(sig.stop_loss)}</b></div>"
                    f"<div class='price-row'><span style='color:#475569;'>TP 1 (Safe):</span><b style='color:#15803D;'>{fmt_price(sig.tp1)}</b></div>"
                    f"<div class='price-row'><span style='color:#475569;'>TP 2 (Main):</span><b style='color:#15803D;'>{fmt_price(sig.tp2)}</b></div>"
                    f"<div class='price-row'><span style='color:#475569;'>TP 3 (Runner):</span><b style='color:#15803D;'>{fmt_price(sig.tp3)}</b></div>"
                    f"</div>"
                    f"<div style='display:grid; grid-template-columns: 1fr 1fr; gap:8px; font-family:monospace; font-size:0.8rem;'>"
                    f"<div style='background:#F8FAFC; border:1px solid #E2E8F0; padding:8px; border-radius:6px;'>"
                    f"<span style='color:#475569; font-size:0.7rem;'>MARGIN REQUIRED ({sig.leverage}x)</span><br>"
                    f"<b>${sig.margin_required_usd:,.2f}</b>"
                    f"</div>"
                    f"<div style='background:#F8FAFC; border:1px solid #E2E8F0; padding:8px; border-radius:6px;'>"
                    f"<span style='color:#475569; font-size:0.7rem;'>POSITION SIZE</span><br>"
                    f"<b>${sig.position_notional_usd:,.0f} ({sig.position_size_units:.3f})</b>"
                    f"</div>"
                    f"</div>"
                    f"<div class='warning-strip'>"
                    f"<b>WIN PROBABILITY: ~{sig.win_probability}%</b><br>"
                    f"<span>{sig.risk_warning}</span>"
                    f"</div>"
                    f"</div>"
                )
                st.markdown(card_html, unsafe_allow_html=True)

        st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

        # Live Multi-Coin Automated Signal Feed with Auto-Crossed Missed/Completed Trades
        st.markdown("#### 🔔 Live Multi-Coin Auto-Generated Signal Feed (Stale / Hit Trades Automatically Crossed Out)")
        st.caption(
            "✅ **ACTIVE** setups are inside the valid entry window. "
            "~~Crossed-out rows~~ indicate the entry was missed or TP/SL was already hit — **do not enter crossed-out trades**."
        )

        feed = snap.get("student_signal_feed", [])
        if feed:
            for item in feed[:15]:
                t_dual = format_dual_time(item.timestamp)
                is_closed_or_missed = item.status != "ACTIVE"

                if is_closed_or_missed:
                    row_md = (
                        f"- ~~**[{t_dual}] {item.symbol} · {item.direction} ({item.risk_tier} · {item.leverage}x)** | "
                        f"Entry: `{fmt_price(item.entry_price)}` | SL: `{fmt_price(item.stop_loss)}` | "
                        f"TP1: `{fmt_price(item.tp1)}` · TP2: `{fmt_price(item.tp2)}` · TP3: `{fmt_price(item.tp3)}` | "
                        f"Margin: `${item.margin_required_usd:,.2f}`~~ — 🚫 **{item.status_note}**"
                    )
                else:
                    icon = "🟢" if item.direction == "LONG" else "🔴"
                    row_md = (
                        f"- {icon} **[{t_dual}] {item.symbol} · {item.direction} ({item.risk_tier} · {item.leverage}x)** | "
                        f"Entry: **`{fmt_price(item.entry_price)}`** | SL: **`{fmt_price(item.stop_loss)}`** | "
                        f"TP1: **`{fmt_price(item.tp1)}`** · TP2: **`{fmt_price(item.tp2)}`** · TP3: **`{fmt_price(item.tp3)}`** | "
                        f"Margin: `${item.margin_required_usd:,.2f}` | Est. Prob: `~{item.win_probability}%` — ✅ **{item.status_note}**"
                    )
                st.markdown(row_md)
        else:
            st.info(
                "Scanner is actively monitoring BTC, ETH, SOL, XRP, and DOGE order books. "
                "Verified setups will appear here automatically as order book skew and structure align."
            )

    render_student_dashboard()


# =============================================================================
# TAB 2: PRO INSTITUTIONAL ORDER BOOK TERMINAL
# =============================================================================
with tab_pro:
    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_pro_terminal():
        snap = manager.get_snapshot(pro_symbol)
        metrics = snap.get("metrics", {})
        symbol = snap["symbol"]
        status = snap["status"]

        h1, h2 = st.columns([2.6, 1.6])
        with h1:
            st.markdown(f"#### {symbol} PERP — Institutional Microstructure & GARCH Engine")
        with h2:
            st.markdown(
                f"<div style='text-align:right; font-family:monospace; color:#0369A1;'>"
                f"FEED: <b>{status}</b> | TICKS: {snap['msg_count']:,}</div>",
                unsafe_allow_html=True,
            )

        if not metrics:
            st.info(f"Connecting to perpetual futures depth & trade streams for {symbol}...")
            return

        mid_price = metrics["mid_price"]
        imbalance_t = metrics["imbalance_t"]
        phi_bps = metrics["phi_t"] * 10000.0
        garch_sigma = snap["garch_sigma"]
        garch_regime = snap["garch_regime"]
        spoof_active = metrics["spoof_active"]
        spread_ok = metrics["spread_ok"]

        k1, k2, k3, k4, k5 = st.columns(5)
        with k1:
            st.markdown(
                f"<div class='neutral-card'><div class='neutral-label'>Mid-Price (P_t)</div>"
                f"<div class='neutral-value'>{fmt_price(mid_price)}</div></div>",
                unsafe_allow_html=True,
            )
        imb_color = "#15803D" if imbalance_t >= imbalance_threshold else (
            "#B91C1C" if imbalance_t <= -imbalance_threshold else "#0F172A"
        )
        with k2:
            st.markdown(
                f"<div class='neutral-card'><div class='neutral-label'>Weighted Imbalance (I_t)</div>"
                f"<div class='neutral-value' style='color:{imb_color};'>{imbalance_t:+.3f}</div></div>",
                unsafe_allow_html=True,
            )
        spread_color = "#15803D" if spread_ok else "#B91C1C"
        with k3:
            st.markdown(
                f"<div class='neutral-card'><div class='neutral-label'>Spread Friction (φ_t)</div>"
                f"<div class='neutral-value' style='color:{spread_color};'>{phi_bps:.2f} bps</div></div>",
                unsafe_allow_html=True,
            )
        with k4:
            st.markdown(
                f"<div class='neutral-card'><div class='neutral-label'>GARCH(1,1)-t Vol (σ̂_t)</div>"
                f"<div class='neutral-value'>{garch_sigma * 100:.3f}%</div></div>",
                unsafe_allow_html=True,
            )
        regime_cls = "badge-short" if "HIGH" in garch_regime else "badge-long"
        spoof_txt = "⚠ SPOOF SUPPRESSION ACTIVE" if spoof_active else "BOOK INTEGRITY: CLEAN"
        with k5:
            st.markdown(
                f"<div class='neutral-card'><div class='neutral-label'>Volatility Regime</div>"
                f"<div style='margin-top:4px;'><span class='{regime_cls}'>{garch_regime}</span></div>"
                f"<div style='margin-top:5px; font-size:0.72rem; font-family:monospace; color:#475569;'>{spoof_txt}</div></div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

        signal = snap.get("latest_signal")
        if signal is not None:
            border_c = "#15803D" if signal.direction == "LONG" else "#B91C1C"
            badge_c = "badge-long" if signal.direction == "LONG" else "badge-short"
            card_html = (
                f"<div class='preset-card' style='border-left:6px solid {border_c}; margin-bottom:14px;'>"
                f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<span class='{badge_c}'>⚡ PRO SETUP: {signal.direction} {signal.symbol}</span>"
                f"<span style='font-size:0.8rem; color:#475569; font-family:monospace;'>"
                f"{format_dual_time(signal.timestamp)} | {signal.reason}</span>"
                f"</div>"
                f"<div style='display:grid; grid-template-columns: repeat(6, 1fr); gap:12px; font-family:monospace; margin-top:12px;'>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>POST-ONLY ENTRY</span><br><b>{fmt_price(signal.entry_price)}</b></div>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>GARCH STOP-LOSS</span><br><b style='color:#B91C1C;'>{fmt_price(signal.stop_loss)}</b></div>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>1:2.5 TAKE-PROFIT</span><br><b style='color:#15803D;'>{fmt_price(signal.take_profit)}</b></div>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>POSITION SIZE</span><br><b>{signal.position_size_units:.4f}</b></div>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>NOTIONAL VALUE</span><br><b>${signal.position_notional_usd:,.2f}</b></div>"
                f"<div><span style='color:#475569; font-size:0.72rem;'>SAFE LEVERAGE</span><br><b style='color:#0369A1;'>{signal.safe_leverage:.1f}x</b></div>"
                f"</div></div>"
            )
            st.markdown(card_html, unsafe_allow_html=True)
        else:
            st.info("PRO SCANNER ARMED: Monitoring L2 depth skew, 500ms spoof filters, and CVD tape absorption...")

        c1, c2 = st.columns([1.65, 1.0])
        with c1:
            hist = snap.get("imbalance_history", [])
            if len(hist) > 2:
                df_hist = pd.DataFrame(
                    hist, columns=["ts", "mid_price", "imbalance_t", "phi_t", "cvd_15s", "cvd_1m", "cvd_5m"]
                )
                df_hist["dt"] = pd.to_datetime(df_hist["ts"], unit="s") + pd.Timedelta(hours=5)

                fig = make_subplots(
                    rows=3,
                    cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.05,
                    row_heights=[0.44, 0.28, 0.28],
                    subplot_titles=("Mid-Price (PKT)", "Rolling CVD (15s / 1m / 5m)", "Weighted Imbalance (I_t)"),
                )
                fig.add_trace(
                    go.Scatter(x=df_hist["dt"], y=df_hist["mid_price"], name="Mid-Price", line=dict(color="#0369A1", width=2)),
                    row=1, col=1,
                )
                for z in structural_zones:
                    fig.add_hline(y=z, line_dash="dash", line_color="#B45309", row=1, col=1)

                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_15s"], name="CVD 15s", line=dict(color="#15803D", width=1.5)), row=2, col=1)
                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_1m"], name="CVD 1m", line=dict(color="#7E22CE", width=1.5)), row=2, col=1)
                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_5m"], name="CVD 5m", line=dict(color="#B45309", width=1.2, dash="dot")), row=2, col=1)

                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["imbalance_t"], name="I_t", fill="tozeroy", line=dict(color="#2563EB", width=1.5)), row=3, col=1)
                fig.add_hline(y=imbalance_threshold, line_dash="dot", line_color="#15803D", row=3, col=1)
                fig.add_hline(y=-imbalance_threshold, line_dash="dot", line_color="#B91C1C", row=3, col=1)

                fig.update_layout(
                    template="plotly_white",
                    paper_bgcolor="#FFFFFF",
                    plot_bgcolor="#F8FAFC",
                    height=500,
                    margin=dict(l=10, r=10, t=35, b=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                fig.update_yaxes(range=[-1.05, 1.05], row=3, col=1)
                st.plotly_chart(fig, use_container_width=True, key="pro_ts_chart")

        with c2:
            raw_bids = metrics.get("raw_bids", [])[:15]
            raw_asks = metrics.get("raw_asks", [])[:15]
            filt_b = metrics.get("filtered_bid_vols", [])[:15]
            filt_a = metrics.get("filtered_ask_vols", [])[:15]

            fig_ob = go.Figure()
            if raw_bids and raw_asks:
                bid_prices = [fmt_price(p) for p, _ in reversed(raw_bids)]
                ask_prices = [fmt_price(p) for p, _ in raw_asks]

                fig_ob.add_trace(go.Bar(y=bid_prices, x=[q for _, q in reversed(raw_bids)], orientation="h", name="Raw Bid", marker=dict(color="rgba(21, 128, 61, 0.25)")))
                fig_ob.add_trace(go.Bar(y=bid_prices, x=list(reversed(filt_b)), orientation="h", name="Clean Bid", marker=dict(color="#15803D")))
                fig_ob.add_trace(go.Bar(y=ask_prices, x=[q for _, q in raw_asks], orientation="h", name="Raw Ask", marker=dict(color="rgba(185, 28, 28, 0.25)")))
                fig_ob.add_trace(go.Bar(y=ask_prices, x=filt_a, orientation="h", name="Clean Ask", marker=dict(color="#B91C1C")))

            fig_ob.update_layout(
                title="L2 Order Book: Raw vs. Spoof-Filtered",
                template="plotly_white",
                paper_bgcolor="#FFFFFF",
                plot_bgcolor="#F8FAFC",
                barmode="overlay",
                height=500,
                margin=dict(l=10, r=10, t=42, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
            )
            st.plotly_chart(fig_ob, use_container_width=True, key="pro_ob_chart")

        st.markdown("#### 📋 Microstructure Audit Log (PKT & UTC)")
        events = snap.get("events_log", [])
        if events:
            rows = [
                {
                    "Time (PKT / UTC)": format_dual_time(ev.timestamp),
                    "Event Type": ev.event_type,
                    "Price": fmt_price(ev.price),
                    "Volume": f"{ev.size:,.3f}",
                    "Diagnostics": ev.details,
                }
                for ev in events[:25]
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    render_pro_terminal()
