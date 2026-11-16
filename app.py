import datetime
import time
from typing import List
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from stream_manager import BinanceFuturesStreamManager, WATCHLIST_SYMBOLS

# -----------------------------------------------------------------------------
# Page Configuration & Dark-Mode Institutional Terminal CSS
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
        background-color: #0B0E14;
        color: #E2E8F0;
    }
    section[data-testid="stSidebar"] {
        background-color: #111622;
        border-right: 1px solid #1E293B;
    }
    .kpi-card {
        background: #131926;
        border: 1px solid #1E293B;
        border-radius: 8px;
        padding: 14px 16px;
    }
    .kpi-label {
        font-size: 0.74rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: #94A3B8;
        margin-bottom: 4px;
    }
    .kpi-value {
        font-size: 1.4rem;
        font-weight: 700;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        color: #F8FAFC;
    }
    .badge-low-vol {
        display: inline-block;
        background: rgba(16, 185, 129, 0.15);
        color: #34D399;
        border: 1px solid #10B981;
        padding: 3px 9px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.8rem;
        font-family: monospace;
    }
    .badge-high-vol {
        display: inline-block;
        background: rgba(239, 68, 68, 0.15);
        color: #F87171;
        border: 1px solid #EF4444;
        padding: 3px 9px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.8rem;
        font-family: monospace;
    }
    .signal-card-long {
        background: linear-gradient(135deg, rgba(16, 185, 129, 0.12), #131926);
        border: 1px solid #10B981;
        border-left: 5px solid #10B981;
        border-radius: 8px;
        padding: 18px;
        margin-bottom: 16px;
    }
    .signal-card-short {
        background: linear-gradient(135deg, rgba(239, 68, 68, 0.12), #131926);
        border: 1px solid #EF4444;
        border-left: 5px solid #EF4444;
        border-radius: 8px;
        padding: 18px;
        margin-bottom: 16px;
    }
    .signal-card-neutral {
        background: #131926;
        border: 1px dashed #334155;
        border-radius: 8px;
        padding: 16px;
        color: #94A3B8;
        margin-bottom: 16px;
    }
    .student-preset-card {
        background: #131926;
        border: 1px solid #1E293B;
        border-radius: 10px;
        padding: 18px;
        height: 100%;
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
# Top Control Header: Active Scanner Switch (Eliminates Dead Mode)
# -----------------------------------------------------------------------------
top_left, top_mid, top_right = st.columns([2.2, 1.3, 1.3])
with top_left:
    st.markdown("### ⚡ Institutional Order Flow & Student Auto-Signals")
with top_mid:
    scanner_active = st.toggle(
        "🟢 LIVE AUTO-SCANNER: ON (Continuous Multi-Coin)",
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
        placeholder="e.g. 94200, 95500, 96800",
        height=70,
    )
    structural_zones = parse_zones(zones_input)

# Session state for Student Tab coin & balance
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
# Two-Tab Architecture: Student Auto-Signals & Pro Institutional Terminal
# -----------------------------------------------------------------------------
tab_student, tab_pro = st.tabs(
    ["🎯 Student Auto-Signals (Low / Med / High Risk)", "🔬 Pro Institutional Order Book Terminal"]
)


# =============================================================================
# TAB 2 (PRIMARY FOR STUDENTS): AUTOMATED LAYMAN SETUPS
# =============================================================================
with tab_student:
    s_ctrl1, s_ctrl2 = st.columns([2.2, 1.0])
    with s_ctrl1:
        selected_coin = st.segmented_control(
            "1-Tap Coin Selector (High-Beta Altcoins & Majors)",
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
            st.warning("⏸️ **LIVE AUTO-SCANNER IS OFF.** Turn ON the top switch to resume live multi-coin scanning and trade generation.")

        # Live 5-Coin Pulse Strip
        tickers = snap.get("coin_tickers", {})
        t_cols = st.columns(len(WATCHLIST_SYMBOLS))
        for idx, sym in enumerate(WATCHLIST_SYMBOLS):
            t_info = tickers.get(sym, {})
            p_val = t_info.get("mid_price", 0.0)
            imb_val = t_info.get("imbalance_t", 0.0)
            arrow = "🟢 ▲" if imb_val > 0.15 else ("🔴 ▼" if imb_val < -0.15 else "⚪ •")
            with t_cols[idx]:
                st.markdown(
                    f"""
                    <div class="kpi-card" style="padding:10px 12px;">
                        <div class="kpi-label">{sym.replace('USDT','')} / USDT {arrow}</div>
                        <div style="font-size:1.1rem; font-weight:700; font-family:monospace;">{fmt_price(p_val) if p_val > 0 else 'Loading...'}</div>
                        <div style="font-size:0.72rem; color:#94A3B8; font-family:monospace;">Book Skew: {imb_val:+.2f}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

        if not metrics:
            st.info(f"Calibrating live order book and 15m / 1H / 4H market structure for {coin}...")
            return

        tf_bias = metrics.get("tf_bias", {})
        profiles = snap.get("student_profiles", {})

        st.markdown(
            f"#### 📊 Auto-Calibrated Trade Plans for **{coin}** &nbsp;|&nbsp; "
            f"Structure Bias: `15m: {tf_bias.get('15m','--')}` · `1H: {tf_bias.get('1H','--')}` · `4H: {tf_bias.get('4H','--')}`"
        )

        # Three Built-In Risk Profiles (Low Risk / Medium Risk / High Risk)
        p_cols = st.columns(3)
        tier_order = [
            ("LOW RISK", "#10B981", "🛡️ Conservative · 0.5% Account Risk"),
            ("MEDIUM RISK", "#38BDF8", "⚖️ Balanced · 1.0% Account Risk"),
            ("HIGH RISK", "#F59E0B", "🔥 Aggressive Scalp · 2.0% Account Risk"),
        ]

        for col, (tier_key, accent_color, subtitle) in zip(p_cols, tier_order):
            sig = profiles.get(tier_key)
            with col:
                if sig is None:
                    st.markdown("<div class='student-preset-card'>Calibrating preset...</div>", unsafe_allow_html=True)
                    continue

                dir_badge_bg = "rgba(16,185,129,0.18)" if sig.direction == "LONG" else "rgba(239,68,68,0.18)"
                dir_color = "#34D399" if sig.direction == "LONG" else "#F87171"
                action_label = "BUY / LONG" if sig.direction == "LONG" else "SELL / SHORT"

                st.markdown(
                    f"""
                    <div class="student-preset-card" style="border-top: 4px solid {accent_color};">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span style="font-weight:800; font-size:1.05rem; color:{accent_color};">{tier_key}</span>
                            <span style="background:{dir_badge_bg}; color:{dir_color}; padding:3px 10px; border-radius:4px; font-weight:800; font-family:monospace;">
                                {action_label}
                            </span>
                        </div>
                        <div style="font-size:0.78rem; color:#94A3B8; margin-top:2px; margin-bottom:12px;">{subtitle}</div>

                        <div style="background:#0E131F; border-radius:6px; padding:10px; font-family:monospace; margin-bottom:10px;">
                            <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
                                <span style="color:#94A3B8;">ENTRY PRICE:</span>
                                <b style="color:#F8FAFC;">{fmt_price(sig.entry_price)}</b>
                            </div>
                            <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
                                <span style="color:#94A3B8;">STOP LOSS:</span>
                                <b style="color:#F87171;">{fmt_price(sig.stop_loss)}</b>
                            </div>
                            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                <span style="color:#94A3B8;">TP 1 (Safe):</span>
                                <b style="color:#34D399;">{fmt_price(sig.tp1)}</b>
                            </div>
                            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                <span style="color:#94A3B8;">TP 2 (Main):</span>
                                <b style="color:#34D399;">{fmt_price(sig.tp2)}</b>
                            </div>
                            <div style="display:flex; justify-content:space-between;">
                                <span style="color:#94A3B8;">TP 3 (Runner):</span>
                                <b style="color:#34D399;">{fmt_price(sig.tp3)}</b>
                            </div>
                        </div>

                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px; font-family:monospace; font-size:0.82rem; margin-bottom:10px;">
                            <div style="background:#0E131F; padding:7px; border-radius:5px;">
                                <span style="color:#94A3B8; font-size:0.7rem;">POSITION SIZE</span><br>
                                <b>${sig.position_notional_usd:,.0f} ({sig.position_size_units:.3f})</b>
                            </div>
                            <div style="background:#0E131F; padding:7px; border-radius:5px;">
                                <span style="color:#94A3B8; font-size:0.7rem;">MAX LEVERAGE</span><br>
                                <b style="color:#38BDF8;">{sig.safe_leverage:.1f}x</b>
                            </div>
                        </div>

                        <div style="font-size:0.78rem; font-family:monospace; padding:8px; border-radius:5px; background:rgba(245,158,11,0.10); border:1px solid rgba(245,158,11,0.35); color:#FBBF24;">
                            <b>WIN PROBABILITY: ~{sig.win_probability}%</b><br>
                            <span style="font-size:0.72rem; color:#E2E8F0;">{sig.risk_warning}</span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

        # Live Multi-Coin Automated Signal Feed (Across all 5 Coins & 15m/1H/4H)
        st.markdown("#### 🔔 Live Multi-Coin Auto-Generated Signal Feed (15m · 1H · 4H Confluence)")
        feed = snap.get("student_signal_feed", [])
        if feed:
            feed_rows = []
            for item in feed:
                t_str = datetime.datetime.fromtimestamp(item.timestamp, tz=datetime.timezone.utc).strftime("%H:%M:%S UTC")
                feed_rows.append(
                    {
                        "Time": t_str,
                        "Coin": item.symbol,
                        "Side": item.direction,
                        "Risk Profile": item.risk_tier,
                        "Entry": fmt_price(item.entry_price),
                        "Stop-Loss": fmt_price(item.stop_loss),
                        "TP1 / TP2 / TP3": f"{fmt_price(item.tp1)} | {fmt_price(item.tp2)} | {fmt_price(item.tp3)}",
                        "Leverage": f"{item.safe_leverage:.1f}x",
                        "Est. Prob": f"{item.win_probability}%",
                        "Structure": item.timeframe_bias,
                    }
                )
            st.dataframe(pd.DataFrame(feed_rows), use_container_width=True, hide_index=True)
        else:
            st.info(
                "Scanner is actively watching order books across BTC, ETH, SOL, XRP, and DOGE. "
                "Verified setups will appear here automatically as institutional absorption triggers."
            )

    render_student_dashboard()


# =============================================================================
# TAB 1 (PRO INSTITUTIONAL TERMINAL): FULL MICROSTRUCTURE & ORDER BOOK DEPTH
# =============================================================================
with tab_pro:
    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_pro_terminal():
        snap = manager.get_snapshot(pro_symbol)
        metrics = snap.get("metrics", {})
        symbol = snap["symbol"]
        status = snap["status"]

        h1, h2 = st.columns([3, 1.3])
        with h1:
            st.markdown(f"#### {symbol} PERP — Institutional Microstructure & GARCH Engine")
        with h2:
            st.markdown(
                f"<div style='text-align:right; font-family:monospace; color:#38BDF8;'>"
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
                f"<div class='kpi-card'><div class='kpi-label'>Mid-Price (P_t)</div>"
                f"<div class='kpi-value'>{fmt_price(mid_price)}</div></div>",
                unsafe_allow_html=True,
            )
        imb_color = "#10B981" if imbalance_t >= imbalance_threshold else (
            "#EF4444" if imbalance_t <= -imbalance_threshold else "#F8FAFC"
        )
        with k2:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-label'>Weighted Imbalance (I_t)</div>"
                f"<div class='kpi-value' style='color:{imb_color};'>{imbalance_t:+.3f}</div></div>",
                unsafe_allow_html=True,
            )
        spread_color = "#10B981" if spread_ok else "#EF4444"
        with k3:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-label'>Spread Friction (φ_t)</div>"
                f"<div class='kpi-value' style='color:{spread_color};'>{phi_bps:.2f} bps</div></div>",
                unsafe_allow_html=True,
            )
        with k4:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-label'>GARCH(1,1)-t Vol (σ̂_t)</div>"
                f"<div class='kpi-value'>{garch_sigma * 100:.3f}%</div></div>",
                unsafe_allow_html=True,
            )
        badge_cls = "badge-high-vol" if "HIGH" in garch_regime else "badge-low-vol"
        spoof_badge = (
            "<span style='color:#F59E0B; font-size:0.74rem; font-family:monospace;'>⚠ SPOOF SUPPRESSION ACTIVE</span>"
            if spoof_active
            else "<span style='color:#64748B; font-size:0.74rem; font-family:monospace;'>BOOK INTEGRITY: CLEAN</span>"
        )
        with k5:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-label'>Volatility Regime</div>"
                f"<div style='margin-top:4px;'><span class='{badge_cls}'>{garch_regime}</span></div>"
                f"<div style='margin-top:4px;'>{spoof_badge}</div></div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

        signal = snap.get("latest_signal")
        if signal is not None:
            age_sec = time.time() - signal.timestamp
            card_cls = "signal-card-long" if signal.direction == "LONG" else "signal-card-short"
            dir_color = "#10B981" if signal.direction == "LONG" else "#EF4444"
            st.markdown(
                f"""
                <div class="{card_cls}">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-size:1.1rem; font-weight:800; color:{dir_color}; font-family:monospace;">
                            ⚡ PRO INSTITUTIONAL SETUP: {signal.direction} {signal.symbol}
                        </span>
                        <span style="font-size:0.8rem; color:#94A3B8; font-family:monospace;">
                            Triggered {age_sec:.1f}s ago | {signal.reason}
                        </span>
                    </div>
                    <hr style="border-color:#1E293B; margin:10px 0;">
                    <div style="display:grid; grid-template-columns: repeat(6, 1fr); gap:12px; font-family:monospace;">
                        <div><span style="color:#94A3B8; font-size:0.74rem;">POST-ONLY ENTRY</span><br><b>{fmt_price(signal.entry_price)}</b></div>
                        <div><span style="color:#94A3B8; font-size:0.74rem;">GARCH STOP-LOSS</span><br><b style="color:#F87171;">{fmt_price(signal.stop_loss)}</b></div>
                        <div><span style="color:#94A3B8; font-size:0.74rem;">1:2.5 TAKE-PROFIT</span><br><b style="color:#34D399;">{fmt_price(signal.take_profit)}</b></div>
                        <div><span style="color:#94A3B8; font-size:0.74rem;">POSITION SIZE</span><br><b>{signal.position_size_units:.4f}</b></div>
                        <div><span style="color:#94A3B8; font-size:0.74rem;">NOTIONAL VALUE</span><br><b>${signal.position_notional_usd:,.2f}</b></div>
                        <div><span style="color:#94A3B8; font-size:0.74rem;">SAFE MAX LEVERAGE</span><br><b style="color:#38BDF8;">{signal.safe_leverage:.1f}x</b></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div class='signal-card-neutral'><b>PRO SCANNER ARMED:</b> Monitoring L2 depth skew, 500ms spoof filters, and CVD tape absorption...</div>",
                unsafe_allow_html=True,
            )

        c1, c2 = st.columns([1.65, 1.0])
        with c1:
            hist = snap.get("imbalance_history", [])
            if len(hist) > 2:
                df_hist = pd.DataFrame(
                    hist, columns=["ts", "mid_price", "imbalance_t", "phi_t", "cvd_15s", "cvd_1m", "cvd_5m"]
                )
                df_hist["dt"] = pd.to_datetime(df_hist["ts"], unit="s")

                fig = make_subplots(
                    rows=3,
                    cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.04,
                    row_heights=[0.44, 0.28, 0.28],
                    subplot_titles=("Mid-Price & Auto/Manual S/R Levels", "Rolling CVD (15s / 1m / 5m)", "Weighted Imbalance (I_t)"),
                )
                fig.add_trace(
                    go.Scatter(x=df_hist["dt"], y=df_hist["mid_price"], name="Mid-Price", line=dict(color="#38BDF8", width=2)),
                    row=1, col=1,
                )
                for z in structural_zones:
                    fig.add_hline(y=z, line_dash="dash", line_color="#F59E0B", row=1, col=1)

                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_15s"], name="CVD 15s", line=dict(color="#10B981", width=1.5)), row=2, col=1)
                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_1m"], name="CVD 1m", line=dict(color="#A855F7", width=1.5)), row=2, col=1)
                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["cvd_5m"], name="CVD 5m", line=dict(color="#F59E0B", width=1.2, dash="dot")), row=2, col=1)

                fig.add_trace(go.Scatter(x=df_hist["dt"], y=df_hist["imbalance_t"], name="I_t", fill="tozeroy", line=dict(color="#60A5FA", width=1.5)), row=3, col=1)
                fig.add_hline(y=imbalance_threshold, line_dash="dot", line_color="#10B981", row=3, col=1)
                fig.add_hline(y=-imbalance_threshold, line_dash="dot", line_color="#EF4444", row=3, col=1)

                fig.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="#131926",
                    plot_bgcolor="#0E131F",
                    height=500,
                    margin=dict(l=10, r=10, t=32, b=10),
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

                fig_ob.add_trace(go.Bar(y=bid_prices, x=[q for _, q in reversed(raw_bids)], orientation="h", name="Raw Bid", marker=dict(color="rgba(16, 185, 129, 0.25)")))
                fig_ob.add_trace(go.Bar(y=bid_prices, x=list(reversed(filt_b)), orientation="h", name="Clean Bid", marker=dict(color="#10B981")))
                fig_ob.add_trace(go.Bar(y=ask_prices, x=[q for _, q in raw_asks], orientation="h", name="Raw Ask", marker=dict(color="rgba(239, 68, 68, 0.25)")))
                fig_ob.add_trace(go.Bar(y=ask_prices, x=filt_a, orientation="h", name="Clean Ask", marker=dict(color="#EF4444")))

            fig_ob.update_layout(
                title="L2 Order Book: Raw vs. Spoof-Filtered",
                template="plotly_dark",
                paper_bgcolor="#131926",
                plot_bgcolor="#0E131F",
                barmode="overlay",
                height=500,
                margin=dict(l=10, r=10, t=42, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
            )
            st.plotly_chart(fig_ob, use_container_width=True, key="pro_ob_chart")

        st.markdown("#### 📋 Microstructure Audit Log (Spoof & Absorption Events)")
        events = snap.get("events_log", [])
        if events:
            rows = [
                {
                    "Time": datetime.datetime.fromtimestamp(ev.timestamp, tz=datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3] + " UTC",
                    "Event Type": ev.event_type,
                    "Price": fmt_price(ev.price),
                    "Volume": f"{ev.size:,.3f}",
                    "Diagnostics": ev.details,
                }
                for ev in events[:25]
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    render_pro_terminal()
