import time
from datetime import datetime, timezone, timedelta
import streamlit as st
from stream_manager import BinanceFuturesStreamManager

st.set_page_config(
    page_title="Godzilla — Institutional & Student Quantitative Terminal",
    page_icon="🦖",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    .stMetric {
        background-color: #f8f9fb;
        padding: 14px;
        border-radius: 8px;
        border: 1px solid #e1e4e8;
    }
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")

manager = get_stream_manager()

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
if "student_coin" not in st.session_state:
    st.session_state.student_coin = "BTCUSDT"
if "student_balance" not in st.session_state:
    st.session_state.student_balance = 100.00

pkt_tz = timezone(timedelta(hours=5))
now_utc = datetime.now(timezone.utc)
now_pkt = now_utc.astimezone(pkt_tz)

hdr_col1, hdr_col2, hdr_col3 = st.columns([3.2, 1.4, 1.4])
with hdr_col1:
    st.markdown("## 🦖 Godzilla — Quantitative Signals & Liquidation Terminal")
    st.caption(f"🕒 Server Clock: **{now_pkt.strftime('%H:%M:%S')} PKT** ({now_utc.strftime('%H:%M:%S')} UTC)")

with hdr_col2:
    st.write("")
    scanner_active = st.toggle("🟢 LIVE AUTO-SCANNER: ON", value=True)

with hdr_col3:
    st.write("")
    auto_refresh = st.toggle("⚡ Real-Time Stream (500ms)", value=True)

tab_student, tab_pro = st.tabs([
    "🎯 Godzilla Student State-Locked Signals (15m · 1H · 4H)",
    "🔬 Godzilla Pro Order Book Terminal"
])

with tab_student:
    ctrl_col1, ctrl_col2 = st.columns([2.2, 1.3])
    with ctrl_col1:
        selected_coin = st.radio(
            "Select Asset",
            ASSETS,
            index=ASSETS.index(st.session_state.student_coin),
            horizontal=True
        )
        st.session_state.student_coin = selected_coin

    with ctrl_col2:
        student_balance = st.number_input(
            "My Account Balance ($)",
            min_value=10.0,
            value=float(st.session_state.student_balance),
            step=10.0,
            format="%.2f"
        )
        st.session_state.student_balance = student_balance

    manager.update_config(
        active_symbol=st.session_state.student_coin,
        scanner_active=scanner_active,
        account_balance=25000.0,
        student_balance=st.session_state.student_balance,
        risk_pct=1.0,
        decay_lambda=0.35,
        imbalance_threshold=0.40,
        max_spread_phi=0.0005,
        garch_k=1.5,
        structural_zones=[]
    )

    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_student_dashboard():
        coin = st.session_state.student_coin
        snap = manager.get_snapshot(coin)
        metrics = snap.get("metrics", {})
        liq = snap.get("liq_stats", {})
        candidates = snap.get("candidate_signals", [])
        tickers = snap.get("coin_tickers", {})

        mid_price = metrics.get("mid_price", 0.0)
        if mid_price <= 0.0 and coin in tickers:
            t_data = tickers[coin]
            mid_price = float(t_data.get("price", 0.0) if isinstance(t_data, dict) else t_data)

        imbalance = metrics.get("imbalance_t", 0.0)
        garch_vol = metrics.get("garch_sigma", 0.0045)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"{coin} Mark Price", f"${mid_price:,.4f}" if 0 < mid_price < 10 else f"${mid_price:,.2f}")
        m2.metric("Orderbook Imbalance", f"{imbalance:+.3f}")
        m3.metric("GARCH Volatility (σ)", f"{garch_vol * 100:.3f}%")
        m4.metric("Stream Ticks", f"{snap.get('msg_count', 0):,}")

        st.markdown("---")

        st.subheader(f"🔥 Godzilla Liquidation Clusters & OI Bias ({coin})")
        l1, l2, l3 = st.columns(3)
        l1.metric(
            "Long Liquidation Cluster",
            f"${liq.get('long_cluster', 0.0):,.2f}",
            f"${liq.get('long_usd', 0.0):,.0f} Est. Pool"
        )
        l2.metric(
            "Short Liquidation Cluster",
            f"${liq.get('short_cluster', 0.0):,.2f}",
            f"${liq.get('short_usd', 0.0):,.0f} Est. Pool"
        )
        l3.metric("Open Interest Bias", liq.get("oi_bias", "Calibrating..."))

        st.markdown("---")

        st.subheader("📡 Godzilla State-Locked Signal Scanner")
        if candidates:
            for idx, sig in enumerate(candidates):
                s_sym = getattr(sig, "symbol", coin)
                s_dir = getattr(sig, "direction", "LONG")
                s_tf = getattr(sig, "timeframe", "1H Intraday")
                s_tier = getattr(sig, "risk_tier", "MEDIUM RISK")
                s_entry = getattr(sig, "entry_price", mid_price)
                s_sl = getattr(sig, "stop_loss", 0.0)
                s_tp1 = getattr(sig, "tp1", 0.0)
                s_tp2 = getattr(sig, "tp2", 0.0)
                s_tp3 = getattr(sig, "tp3", 0.0)
                s_lev = getattr(sig, "leverage", 20)
                s_margin = getattr(sig, "margin_required_usd", 0.0)
                s_prob = getattr(sig, "win_probability", 70)
                s_eta = getattr(sig, "eta_pkt_utc", "")
                s_warn = getattr(sig, "risk_warning", "Use Isolated Margin")

                with st.container(border=True):
                    top_r1, top_r2, top_r3 = st.columns([2, 2, 2])
                    top_r1.markdown(f"### {s_sym} · **{s_dir}**")
                    top_r2.markdown(f"**Timeframe:** {s_tf} | **{s_tier}**")
                    top_r3.markdown(f"**Win Prob:** {s_prob}% | **Lev:** {s_lev}x")

                    p1, p2, p3, p4, p5 = st.columns(5)
                    p1.metric("Entry Price", f"${s_entry:,.4f}" if s_entry < 10 else f"${s_entry:,.2f}")
                    p2.metric("Stop Loss", f"${s_sl:,.4f}" if s_sl < 10 else f"${s_sl:,.2f}")
                    p3.metric("TP 1", f"${s_tp1:,.4f}" if s_tp1 < 10 else f"${s_tp1:,.2f}")
                    p4.metric("TP 2", f"${s_tp2:,.4f}" if s_tp2 < 10 else f"${s_tp2:,.2f}")
                    p5.metric("TP 3", f"${s_tp3:,.4f}" if s_tp3 < 10 else f"${s_tp3:,.2f}")

                    st.caption(f"💰 Margin Required: **${s_margin:,.2f}** | ⏱️ Target ETA: **{s_eta}** | ⚠️ {s_warn}")

                    if st.button(f"🔒 Lock & Commit Signal to Turso Cloud ({s_sym})", key=f"lock_{s_sym}_{idx}"):
                        payload = {
                            "symbol": s_sym,
                            "direction": s_dir,
                            "timeframe": s_tf,
                            "entry_price": s_entry,
                            "stop_loss": s_sl,
                            "tp1": s_tp1,
                            "tp2": s_tp2,
                            "tp3": s_tp3,
                            "leverage": s_lev,
                            "margin_usd": s_margin,
                            "eta_pkt_utc": s_eta,
                            "committed_at": time.time(),
                            "status": "LOCKED_ACTIVE"
                        }
                        if hasattr(manager, "save_state_to_turso"):
                            manager.save_state_to_turso(f"locked_signal_{s_sym}", payload)
                        st.session_state[f"local_lock_{s_sym}"] = payload
                        st.success(f"Signal locked and persisted for {s_sym}!")
        else:
            st.info(f"Scanning {coin} order book microstructure for ≥ 0.20 directional imbalance...")

        st.markdown("---")
        st.subheader(f"💾 Godzilla Active State-Locked Position ({coin})")
        locked_trade = None
        if hasattr(manager, "load_state_from_turso"):
            try:
                locked_trade = manager.load_state_from_turso(f"locked_signal_{coin}")
            except Exception:
                locked_trade = None
        if not locked_trade:
            locked_trade = st.session_state.get(f"local_lock_{coin}")

        if locked_trade:
            st.json(locked_trade)
            if st.button(f"🔓 Unlock / Clear {coin} Position", key=f"unlock_{coin}"):
                if hasattr(manager, "save_state_to_turso"):
                    manager.save_state_to_turso(f"locked_signal_{coin}", {})
                st.session_state.pop(f"local_lock_{coin}", None)
                st.rerun()
        else:
            st.caption(f"No locked signal currently active for {coin}.")

    render_student_dashboard()

with tab_pro:
    st.subheader("🔬 Godzilla Microstructure & Hyperparameter Controls")
    p_col1, p_col2, p_col3, p_col4 = st.columns(4)
    with p_col1:
        pro_symbol = st.selectbox("Pro Asset", ASSETS, index=0)
    with p_col2:
        decay_lambda = st.slider("Decay Lambda (λ)", 0.05, 1.0, 0.35, 0.05)
    with p_col3:
        imbalance_threshold = st.slider("Imbalance Threshold", 0.10, 0.80, 0.40, 0.05)
    with p_col4:
        garch_k = st.slider("GARCH Multiplier (k)", 1.0, 3.0, 1.5, 0.1)

    max_spread_phi = st.number_input("Max Spread Filter (φ)", value=0.0005, format="%.5f")

    manager.update_config(
        active_symbol=pro_symbol,
        scanner_active=scanner_active,
        account_balance=25000.0,
        student_balance=st.session_state.student_balance,
        risk_pct=1.0,
        decay_lambda=decay_lambda,
        imbalance_threshold=imbalance_threshold,
        max_spread_phi=max_spread_phi,
        garch_k=garch_k,
        structural_zones=[]
    )

    pro_snap = manager.get_snapshot(pro_symbol)
    pro_metrics = pro_snap.get("metrics", {})

    pm1, pm2, pm3, pm4 = st.columns(4)
    pm1.metric("Mid Price", f"${pro_metrics.get('mid_price', 0.0):,.2f}")
    pm2.metric("Spread (φ)", f"{pro_metrics.get('phi_t', 0.0):.6f}")
    pm3.metric("Weighted Imbalance", f"{pro_metrics.get('imbalance_t', 0.0):+.4f}")
    pm4.metric("Connection Status", str(pro_snap.get("status", "ACTIVE")))

    ob_col1, ob_col2 = st.columns(2)
    with ob_col1:
        st.markdown("**Top Bids (Price, Size)**")
        st.dataframe(pro_metrics.get("raw_bids", [])[:15], width="stretch")
    with ob_col2:
        st.markdown("**Top Asks (Price, Size)**")
        st.dataframe(pro_metrics.get("raw_asks", [])[:15], width="stretch")

    st.markdown("**Godzilla Audit Logbook**")
    audit_logs = pro_snap.get("audit_logbook", [])
    if audit_logs:
        st.write(audit_logs)
    else:
        st.caption("No microstructure anomalies or spoof walls logged yet.")
