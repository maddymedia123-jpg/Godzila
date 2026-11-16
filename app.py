import datetime
import time
from typing import List
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from stream_manager import BinanceFuturesStreamManager

# -----------------------------------------------------------------------------
# Page Configuration & Institutional Dark-Mode Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="HFT Order Book & GARCH Co-Pilot",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
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
        text-align: left;
    }
    .kpi-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: #94A3B8;
        margin-bottom: 4px;
    }
    .kpi-value {
        font-size: 1.45rem;
        font-weight: 700;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        color: #F8FAFC;
    }
    .badge-low-vol {
        display: inline-block;
        background: rgba(16, 185, 129, 0.15);
        color: #34D399;
        border: 1px solid #10B981;
        padding: 4px 10px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.85rem;
        font-family: monospace;
    }
    .badge-high-vol {
        display: inline-block;
        background: rgba(239, 68, 68, 0.15);
        color: #F87171;
        border: 1px solid #EF4444;
        padding: 4px 10px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.85rem;
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
        padding: 18px;
        color: #94A3B8;
        margin-bottom: 16px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Singleton Background Stream Manager
# -----------------------------------------------------------------------------
@st.cache_resource
def get_stream_manager() -> BinanceFuturesStreamManager:
    return BinanceFuturesStreamManager(symbol="BTCUSDT")


manager = get_stream_manager()


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
# Sidebar Controls
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ⚡ Execution & Risk Controls")

    symbol_choice = st.selectbox(
        "Perpetual Futures Contract",
        options=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"],
        index=0,
    )
    custom_symbol = st.text_input("Or Custom Binance Futures Symbol", value="").strip().upper()
    active_symbol = custom_symbol if custom_symbol else symbol_choice

    col_bal, col_risk = st.columns(2)
    with col_bal:
        account_balance = st.number_input(
            "Equity ($)", min_value=100.0, max_value=100_000_000.0, value=25000.0, step=500.0
        )
    with col_risk:
        risk_pct = st.number_input(
            "Max Risk (%)", min_value=0.1, max_value=10.0, value=1.0, step=0.1
        )

    st.markdown("---")
    st.markdown("### 🔬 Microstructure Parameters")

    decay_lambda = st.slider(
        "Depth Decay (λ)", min_value=0.05, max_value=1.00, value=0.35, step=0.05,
        help="Exponential decay weight w_k = exp(-λ*(k-1)) across L2 depth levels."
    )
    imbalance_threshold = st.slider(
        "Imbalance Trigger Threshold (|I_t|)", min_value=0.15, max_value=0.85, value=0.40, step=0.05
    )
    max_spread_bps = st.slider(
        "Max Spread Friction φ_t (bps)", min_value=0.5, max_value=25.0, value=5.0, step=0.5
    )
    garch_k = st.slider(
        "GARCH Stop Multiplier (k · σ_t)", min_value=0.5, max_value=4.0, value=1.5, step=0.1
    )

    st.markdown("---")
    st.markdown("### 🎯 Structural Price Zones")
    zones_input = st.text_area(
        "Key S/R Levels (comma-separated)",
        value="",
        placeholder="e.g. 94200, 95500, 96800 (Leave blank to scan all levels)",
        height=75,
    )
    structural_zones = parse_zones(zones_input)

    st.markdown("---")
    auto_refresh = st.toggle("Live Stream Auto-Refresh (500ms)", value=True)

# Push live UI parameters to background worker
manager.update_config(
    symbol=active_symbol,
    account_balance=account_balance,
    risk_pct=risk_pct,
    decay_lambda=decay_lambda,
    imbalance_threshold=imbalance_threshold,
    max_spread_phi=max_spread_bps / 10000.0,
    garch_k=garch_k,
    structural_zones=structural_zones,
)

# -----------------------------------------------------------------------------
# Live Telemetry Fragment
# -----------------------------------------------------------------------------
@st.fragment(run_every=0.5 if auto_refresh else None)
def render_terminal():
    snap = manager.get_snapshot()
    metrics = snap.get("metrics", {})
    symbol = snap["symbol"]
    status = snap["status"]

    # Header Status Strip
    h_col1, h_col2 = st.columns([3, 1])
    with h_col1:
        st.markdown(
            f"## {symbol} PERP — Institutional Order Flow & GARCH Co-Pilot"
        )
    with h_col2:
        st.markdown(
            f"<div style='text-align:right; padding-top:14px; font-family:monospace; color:#38BDF8;'>"
            f"FEED: <b>{status}</b> | TICKS: {snap['msg_count']:,}</div>",
            unsafe_allow_html=True,
        )

    if not metrics:
        st.info(f"Connecting to Binance USD-M Futures depth20@100ms & aggTrade streams for {symbol}...")
        return

    mid_price = metrics["mid_price"]
    imbalance_t = metrics["imbalance_t"]
    phi_t = metrics["phi_t"]
    phi_bps = phi_t * 10000.0
    garch_sigma = snap["garch_sigma"]
    garch_regime = snap["garch_regime"]
    spoof_active = metrics["spoof_active"]
    spread_ok = metrics["spread_ok"]

    # -------------------------------------------------------------------------
    # Top KPI Banner
    # -------------------------------------------------------------------------
    k1, k2, k3, k4, k5 = st.columns(5)

    with k1:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Mid-Price (P_t)</div>
                <div class="kpi-value">${mid_price:,.2f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    imb_color = "#10B981" if imbalance_t >= imbalance_threshold else (
        "#EF4444" if imbalance_t <= -imbalance_threshold else "#F8FAFC"
    )
    with k2:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Weighted Imbalance (I_t)</div>
                <div class="kpi-value" style="color:{imb_color};">{imbalance_t:+.3f}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    spread_color = "#10B981" if spread_ok else "#EF4444"
    with k3:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Spread Friction (φ_t)</div>
                <div class="kpi-value" style="color:{spread_color};">{phi_bps:.2f} bps</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with k4:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">GARCH(1,1)-t Vol (σ̂_t)</div>
                <div class="kpi-value">{garch_sigma * 100:.3f}%</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    badge_cls = "badge-high-vol" if "HIGH" in garch_regime else "badge-low-vol"
    spoof_badge = (
        "<span style='color:#F59E0B; font-size:0.75rem; font-family:monospace;'>⚠ SPOOF SUPPRESSION ACTIVE</span>"
        if spoof_active
        else "<span style='color:#64748B; font-size:0.75rem; font-family:monospace;'>BOOK INTEGRITY: CLEAN</span>"
    )
    with k5:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Volatility Regime</div>
                <div style="margin-top:4px;"><span class="{badge_cls}">{garch_regime}</span></div>
                <div style="margin-top:4px;">{spoof_badge}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # Actionable Trade Setup Card
    # -------------------------------------------------------------------------
    signal = snap.get("latest_signal")
    if signal is not None:
        age_sec = time.time() - signal.timestamp
        card_cls = "signal-card-long" if signal.direction == "LONG" else "signal-card-short"
        dir_color = "#10B981" if signal.direction == "LONG" else "#EF4444"
        st.markdown(
            f"""
            <div class="{card_cls}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:1.15rem; font-weight:800; color:{dir_color}; font-family:monospace;">
                        ⚡ ACTIONABLE CO-PILOT SETUP: {signal.direction} {signal.symbol}
                    </span>
                    <span style="font-size:0.8rem; color:#94A3B8; font-family:monospace;">
                        Triggered {age_sec:.1f}s ago | {signal.reason}
                    </span>
                </div>
                <hr style="border-color:#1E293B; margin:10px 0;">
                <div style="display:grid; grid-template-columns: repeat(6, 1fr); gap:12px; font-family:monospace;">
                    <div><span style="color:#94A3B8; font-size:0.75rem;">POST-ONLY ENTRY</span><br><b>${signal.entry_price:,.2f}</b></div>
                    <div><span style="color:#94A3B8; font-size:0.75rem;">GARCH STOP-LOSS</span><br><b style="color:#F87171;">${signal.stop_loss:,.2f}</b></div>
                    <div><span style="color:#94A3B8; font-size:0.75rem;">1:2.5 TAKE-PROFIT</span><br><b style="color:#34D399;">${signal.take_profit:,.2f}</b></div>
                    <div><span style="color:#94A3B8; font-size:0.75rem;">POSITION SIZE</span><br><b>{signal.position_size_units:.4f} {symbol.replace('USDT','')}</b></div>
                    <div><span style="color:#94A3B8; font-size:0.75rem;">NOTIONAL VALUE</span><br><b>${signal.position_notional_usd:,.2f}</b></div>
                    <div><span style="color:#94A3B8; font-size:0.75rem;">SAFE MAX LEVERAGE</span><br><b style="color:#38BDF8;">{signal.safe_leverage:.1f}x</b></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="signal-card-neutral">
                <b>SCANNER ARMED:</b> Monitoring Level-2 order book imbalance, 500ms spoof filters, and CVD tape absorption for institutional confluence...
            </div>
            """,
            unsafe_allow_html=True,
        )

    # -------------------------------------------------------------------------
    # Live Visuals: Panel 1 (Price + CVD + I_t) & Panel 2 (Raw vs Filtered Depth)
    # -------------------------------------------------------------------------
    chart_col1, chart_col2 = st.columns([1.65, 1.0])

    with chart_col1:
        hist = snap.get("imbalance_history", [])
        if len(hist) > 2:
            df_hist = pd.DataFrame(
                hist,
                columns=["ts", "mid_price", "imbalance_t", "phi_t", "cvd_15s", "cvd_1m", "cvd_5m"],
            )
            df_hist["dt"] = pd.to_datetime(df_hist["ts"], unit="s")

            fig = make_subplots(
                rows=3,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.04,
                row_heights=[0.44, 0.28, 0.28],
                subplot_titles=("Mid-Price & Structural S/R Zones", "Rolling CVD (15s / 1m / 5m)", "Weighted Imbalance (I_t)"),
            )

            # Row 1: Mid Price + Structural Zones
            fig.add_trace(
                go.Scatter(
                    x=df_hist["dt"],
                    y=df_hist["mid_price"],
                    mode="lines",
                    name="Mid-Price",
                    line=dict(color="#38BDF8", width=2),
                ),
                row=1,
                col=1,
            )
            for z in structural_zones:
                fig.add_hline(
                    y=z,
                    line_dash="dash",
                    line_color="#F59E0B",
                    annotation_text=f"Zone {z:,.2f}",
                    row=1,
                    col=1,
                )

            # Row 2: Rolling CVD
            fig.add_trace(
                go.Scatter(
                    x=df_hist["dt"], y=df_hist["cvd_15s"], name="CVD 15s", line=dict(color="#10B981", width=1.5)
                ),
                row=2,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df_hist["dt"], y=df_hist["cvd_1m"], name="CVD 1m", line=dict(color="#A855F7", width=1.5)
                ),
                row=2,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df_hist["dt"], y=df_hist["cvd_5m"], name="CVD 5m", line=dict(color="#F59E0B", width=1.2, dash="dot")
                ),
                row=2,
                col=1,
            )

            # Row 3: Weighted Imbalance Oscillator
            fig.add_trace(
                go.Scatter(
                    x=df_hist["dt"],
                    y=df_hist["imbalance_t"],
                    name="I_t",
                    fill="tozeroy",
                    line=dict(color="#60A5FA", width=1.5),
                ),
                row=3,
                col=1,
            )
            fig.add_hline(y=imbalance_threshold, line_dash="dot", line_color="#10B981", row=3, col=1)
            fig.add_hline(y=-imbalance_threshold, line_dash="dot", line_color="#EF4444", row=3, col=1)

            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#131926",
                plot_bgcolor="#0E131F",
                height=520,
                margin=dict(l=10, r=10, t=32, b=10),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            fig.update_yaxes(range=[-1.05, 1.05], row=3, col=1)
            st.plotly_chart(fig, use_container_width=True, key="micro_ts_chart")

    with chart_col2:
        raw_bids = metrics.get("raw_bids", [])[:15]
        raw_asks = metrics.get("raw_asks", [])[:15]
        filt_b = metrics.get("filtered_bid_vols", [])[:15]
        filt_a = metrics.get("filtered_ask_vols", [])[:15]

        fig_ob = go.Figure()
        if raw_bids and raw_asks:
            bid_prices = [f"{p:,.2f}" for p, _ in reversed(raw_bids)]
            raw_bid_q = [q for _, q in reversed(raw_bids)]
            filt_bid_q = list(reversed(filt_b))

            ask_prices = [f"{p:,.2f}" for p, _ in raw_asks]
            raw_ask_q = [q for _, q in raw_asks]
            filt_ask_q = filt_a

            # Raw liquidity (faint ghost bars showing spoof walls if filtered)
            fig_ob.add_trace(
                go.Bar(
                    y=bid_prices,
                    x=raw_bid_q,
                    orientation="h",
                    name="Raw Bid Wall",
                    marker=dict(color="rgba(16, 185, 129, 0.25)"),
                )
            )
            fig_ob.add_trace(
                go.Bar(
                    y=bid_prices,
                    x=filt_bid_q,
                    orientation="h",
                    name="Clean Bid Liq",
                    marker=dict(color="#10B981"),
                )
            )
            fig_ob.add_trace(
                go.Bar(
                    y=ask_prices,
                    x=raw_ask_q,
                    orientation="h",
                    name="Raw Ask Wall",
                    marker=dict(color="rgba(239, 68, 68, 0.25)"),
                )
            )
            fig_ob.add_trace(
                go.Bar(
                    y=ask_prices,
                    x=filt_ask_q,
                    orientation="h",
                    name="Clean Ask Liq",
                    marker=dict(color="#EF4444"),
                )
            )

        fig_ob.update_layout(
            title="L2 Order Book: Raw vs. Spoof-Filtered Liquidity",
            template="plotly_dark",
            paper_bgcolor="#131926",
            plot_bgcolor="#0E131F",
            barmode="overlay",
            height=520,
            margin=dict(l=10, r=10, t=42, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
        )
        st.plotly_chart(fig_ob, use_container_width=True, key="ob_depth_chart")

    # -------------------------------------------------------------------------
    # Real-Time Microstructure Audit Log (Spoof Alerts & Absorption Events)
    # -------------------------------------------------------------------------
    st.markdown("### 📋 Market Microstructure Audit Log (Spoof & Absorption Events)")
    events = snap.get("events_log", [])
    if events:
        rows = []
        for ev in events[:25]:
            dt_str = datetime.datetime.fromtimestamp(ev.timestamp, tz=datetime.timezone.utc).strftime(
                "%H:%M:%S.%f"
            )[:-3] + " UTC"
            rows.append(
                {
                    "Time": dt_str,
                    "Event Type": ev.event_type,
                    "Price": f"${ev.price:,.2f}",
                    "Volume": f"{ev.size:,.3f}",
                    "Diagnostics": ev.details,
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No spoof wall pullbacks or tape absorption anomalies detected in the active window.")


render_terminal()
