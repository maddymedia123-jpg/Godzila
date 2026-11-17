import time
from datetime import datetime, timezone, timedelta
import streamlit as st
from stream_manager import BinanceFuturesStreamManager

st.set_page_config(
    page_title="Godzilla",
    page_icon="🦖",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .block-container { padding-top: 1.4rem; padding-bottom: 2rem; }
    .stMetric {
        background-color: #f8f9fb;
        padding: 12px 14px;
        border-radius: 8px;
        border: 1px solid #e1e4e8;
    }
    .gz-tracker-header {
        background: linear-gradient(90deg, #0d151c 0%, #13202b 100%);
        color: #94a3b8;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 0.08em;
        padding: 12px 16px;
        border-radius: 8px 8px 0 0;
        border: 1px solid #223240;
        margin-bottom: 6px;
    }
    .gz-row-container {
        background: linear-gradient(90deg, #0f1922 0%, #14222e 50%, #0f1922 100%);
        border: 1px solid #223240;
        border-radius: 8px;
        padding: 14px 16px;
        margin-bottom: 10px;
    }
    .gz-progress-track {
        background-color: #1e293b;
        border-radius: 4px;
        height: 8px;
        width: 100%;
        overflow: hidden;
        margin: 4px 0 8px 0;
        border: 1px solid #334155;
    }
    .gz-progress-fill-green {
        background: linear-gradient(90deg, #059669, #34d399);
        height: 100%;
        box-shadow: 0 0 8px rgba(52, 211, 153, 0.4);
    }
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")

manager = get_stream_manager()

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
COIN_BADGES = {
    "BTCUSDT": ("₿", "#f7931a"),
    "ETHUSDT": ("Ξ", "#627eea"),
    "SOLUSDT": ("◎", "#14f195"),
    "XRPUSDT": ("✕", "#00aae4"),
    "DOGEUSDT": ("Ð", "#c2a633"),
}

if "student_coin" not in st.session_state:
    st.session_state.student_coin = "BTCUSDT"
if "student_balance" not in st.session_state:
    st.session_state.student_balance = 100.00


def fmt_px(val: float) -> str:
    if val <= 0:
        return "$0.00"
    if val < 1.0:
        return f"${val:,.4f}"
    if val < 100.0:
        return f"${val:,.3f}"
    return f"${val:,.2f}"


def render_signal_card(sig, mid_price: float, tf_tag: str):
    with st.container(border=True):
        r1, r2, r3 = st.columns([2.2, 2.4, 1.6])
        r1.markdown(f"### {sig.symbol} · **{sig.direction}** `[{sig.timeframe}]`")
        r2.markdown(f"**Strategy:** {sig.risk_tier}\n\n{sig.htf_summary}")
        r3.markdown(f"**Win Prob:** {sig.win_probability}% | **Lev:** {sig.leverage}x\n\n**Margin:** ${sig.margin_required_usd:,.2f}")

        if sig.can_enter:
            st.success(f"✅ **{sig.entry_status}** — Current Price: **{fmt_px(mid_price)}** vs Entry: **{fmt_px(sig.entry_price)}**")
        else:
            st.warning(f"⛔ **{sig.entry_status}** — Current Price: **{fmt_px(mid_price)}** vs Entry: **{fmt_px(sig.entry_price)}**")

        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Entry Zone", fmt_px(sig.entry_price))
        p2.metric("Shared SL", fmt_px(sig.stop_loss))
        p3.metric("TP 1", fmt_px(sig.tp1), sig.eta_tp1_str)
        p4.metric("TP 2", fmt_px(sig.tp2), sig.eta_tp2_str)
        p5.metric("TP 3", fmt_px(sig.tp3), sig.eta_tp3_str)

        st.caption(f"⚠️ {sig.risk_warning}")

        if sig.can_enter:
            if st.button(f"🔒 Commit {tf_tag} Trade to Godzilla Cloud ({sig.symbol} {sig.direction})", key=f"commit_{tf_tag}_{sig.signal_id}"):
                trade_record = {
                    "trade_id": sig.signal_id,
                    "symbol": sig.symbol,
                    "direction": sig.direction,
                    "timeframe": tf_tag,
                    "entry_price": sig.entry_price,
                    "stop_loss": sig.stop_loss,
                    "tp1": sig.tp1,
                    "tp2": sig.tp2,
                    "tp3": sig.tp3,
                    "leverage": sig.leverage,
                    "margin_usd": sig.margin_required_usd,
                    "eta_tp1": sig.eta_tp1_str,
                    "eta_tp2": sig.eta_tp2_str,
                    "eta_tp3": sig.eta_tp3_str,
                    "committed_at": time.time(),
                }
                ok, msg = manager.commit_trade(trade_record)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)
        else:
            st.button(
                f"🚫 {tf_tag} Entry Passed — Waiting for Next Setup",
                key=f"disabled_{tf_tag}_{sig.signal_id}",
                disabled=True,
            )


def render_active_trades_table(trades_list: list, tickers: dict, tab_prefix: str):
    if not trades_list:
        st.caption(f"No active {tab_prefix} trades locked in Godzilla Cloud yet.")
        return

    st.markdown("""
    <div class="gz-tracker-header">
        <div style="display:grid;grid-template-columns:1.2fr 1.0fr 1.1fr 3.2fr 1.3fr 1.2fr 0.4fr;align-items:center;">
            <div>ASSET / PAIR</div>
            <div>CURRENT PRICE</div>
            <div>SHARED SL</div>
            <div>PROXIMITY BAR (SL-LEFT, TP-RIGHT)</div>
            <div>TP TARGET & TIMELINE</div>
            <div>STATUS</div>
            <div style="text-align:center;">KILL</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    for tr in trades_list:
        t_id = tr.get("trade_id", "")
        t_sym = tr.get("symbol", "BTCUSDT")
        t_tf = tr.get("timeframe", "1H")
        t_dir = tr.get("direction", "LONG")
        t_entry = float(tr.get("entry_price", 0.0))
        t_sl = float(tr.get("stop_loss", 0.0))
        t_tp1 = float(tr.get("tp1", 0.0))
        t_tp2 = float(tr.get("tp2", 0.0))
        t_tp3 = float(tr.get("tp3", 0.0))
        eta1 = tr.get("eta_tp1", "~1h")
        eta2 = tr.get("eta_tp2", "~4h")
        eta3 = tr.get("eta_tp3", "~8h")

        live_px = float(tickers.get(t_sym, {}).get("price", 0.0))
        if live_px <= 0:
            live_px = t_entry

        sl_pct = ((t_sl - t_entry) / max(t_entry, 1e-8)) * 100.0
        icon_char, icon_color = COIN_BADGES.get(t_sym, ("◈", "#38bdf8"))
        dir_color = "#34d399" if t_dir == "LONG" else "#f87171"
        tf_badge_bg = "#1d4ed8" if t_tf == "1H" else "#7c3aed"

        def get_progress_val(tp_val: float) -> float:
            if t_dir == "LONG":
                dist = max(tp_val - t_entry, 1e-8)
                progress = live_px - t_entry
            else:
                dist = max(t_entry - tp_val, 1e-8)
                progress = t_entry - live_px
            return float(min(100.0, max(0.0, (progress / dist) * 100.0)))

        p1_pct = get_progress_val(t_tp1)
        p2_pct = get_progress_val(t_tp2)
        p3_pct = get_progress_val(t_tp3)

        with st.container():
            st.markdown('<div class="gz-row-container">', unsafe_allow_html=True)
            cols = st.columns([1.2, 1.0, 1.1, 3.2, 1.3, 1.2, 0.4])

            with cols[0]:
                st.markdown(f"""
                <span style="display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;border-radius:50%;background:{icon_color};color:#0b1117;font-weight:900;margin-right:6px;font-size:11px;">{icon_char}</span>
                <strong style="font-size:14px;color:#fff;">{t_sym.replace('USDT','')}/USDT</strong>
                <span style="background:{tf_badge_bg};color:#fff;font-size:10px;font-weight:800;padding:2px 5px;border-radius:3px;margin-left:4px;">{t_tf}</span><br/>
                <span style="color:{dir_color};font-size:12px;font-weight:700;margin-left:26px;">{t_dir} · {fmt_px(t_entry)}</span>
                """, unsafe_allow_html=True)

            with cols[1]:
                st.markdown(f"<div style='font-size:15px;font-weight:700;color:#ffffff;padding-top:6px;'>{fmt_px(live_px)}</div>", unsafe_allow_html=True)

            with cols[2]:
                st.markdown(f"<div style='font-size:13px;font-weight:600;color:#e2e8f0;padding-top:4px;'>{fmt_px(t_sl)}<br/><span style='color:#f87171;font-size:11px;'>({sl_pct:+.2f}%)</span></div>", unsafe_allow_html=True)

            with cols[3]:
                st.markdown(f"""
                <div style="font-size:11px;color:#94a3b8;margin-bottom:2px;font-weight:700;">TP1 Progress: {p1_pct:.0f}%</div>
                <div class="gz-progress-track"><div class="gz-progress-fill-green" style="width:{p1_pct}%;"></div></div>
                <div style="font-size:11px;color:#94a3b8;margin-bottom:2px;font-weight:700;">TP2 Progress: {p2_pct:.0f}%</div>
                <div class="gz-progress-track"><div class="gz-progress-fill-green" style="width:{p2_pct}%;"></div></div>
                <div style="font-size:11px;color:#94a3b8;margin-bottom:2px;font-weight:700;">TP3 Progress: {p3_pct:.0f}%</div>
                <div class="gz-progress-track"><div class="gz-progress-fill-green" style="width:{p3_pct}%;"></div></div>
                """, unsafe_allow_html=True)

            with cols[4]:
                st.markdown(f"""
                <div style="font-size:11px;line-height:1.7;color:#e2e8f0;font-weight:600;padding-top:2px;">
                    <div><strong>TP1:</strong> {fmt_px(t_tp1)}<br/><span style='color:#38bdf8;font-size:10px;'>🕒 {eta1}</span></div>
                    <div><strong>TP2:</strong> {fmt_px(t_tp2)}<br/><span style='color:#38bdf8;font-size:10px;'>🕒 {eta2}</span></div>
                    <div><strong>TP3:</strong> {fmt_px(t_tp3)}<br/><span style='color:#38bdf8;font-size:10px;'>🕒 {eta3}</span></div>
                </div>
                """, unsafe_allow_html=True)

            with cols[5]:
                def status_badge(lbl: str, tp_val: float, hit: bool, pct: float) -> str:
                    if tr.get("sl_hit"):
                        return "<span style='color:#ef4444;font-weight:800;font-size:11px;'>SL HIT</span>"
                    if hit or pct >= 100.0:
                        return f"<span style='color:#34d399;font-weight:800;font-size:11px;'>{lbl} HIT</span>"
                    return f"<span style='color:#34d399;font-weight:600;font-size:11px;'>{pct:.0f}% to {lbl}</span>"

                b1 = status_badge("TP1", t_tp1, tr.get("tp1_hit", False), p1_pct)
                b2 = status_badge("TP2", t_tp2, tr.get("tp2_hit", False), p2_pct)
                b3 = status_badge("TP3", t_tp3, tr.get("tp3_hit", False), p3_pct)
                st.markdown(f"<div style='line-height:1.9;padding-top:4px;'>{b1}<br/>{b2}<br/>{b3}</div>", unsafe_allow_html=True)

            with cols[6]:
                st.write("")
                if st.button("✕", key=f"kill_{tab_prefix}_{t_id}", help=f"Kill {t_sym} [{t_tf}] trade"):
                    manager.kill_trade(t_id)
                    st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)


# Header Bar
pkt_tz = timezone(timedelta(hours=5))
now_utc = datetime.now(timezone.utc)
now_pkt = now_utc.astimezone(pkt_tz)

hdr_col1, hdr_col2, hdr_col3 = st.columns([3.2, 1.4, 1.4])
with hdr_col1:
    st.markdown("## 🦖 Godzilla")
    st.caption(f"🕒 Server Clock: **{now_pkt.strftime('%H:%M:%S')} PKT** ({now_utc.strftime('%H:%M:%S')} UTC)")

with hdr_col2:
    st.write("")
    scanner_active = st.toggle("🟢 LIVE AUTO-SCANNER: ON", value=True)

with hdr_col3:
    st.write("")
    auto_refresh = st.toggle("⚡ Real-Time Stream (500ms)", value=True)

tab_student, tab_pro = st.tabs([
    "🎯 Godzilla Dual-Timeframe Terminal (1H · 4H)",
    "🔬 Godzilla Order Book & Audit Terminal"
])

with tab_student:
    ctrl_col1, ctrl_col2 = st.columns([2.2, 1.3])
    with ctrl_col1:
        selected_coin = st.radio(
            "Select Asset to Scan",
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
        imbalance_threshold=0.22,
        max_spread_phi=0.0008,
        garch_k=1.5,
        structural_zones=[]
    )

    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_godzilla_dashboard():
        coin = st.session_state.student_coin
        snap = manager.get_snapshot(coin)
        metrics = snap.get("metrics", {})
        liq = snap.get("liq_stats", {})
        tickers = snap.get("coin_tickers", {})
        tf_bias = snap.get("tf_bias", {})
        tf_rsi = snap.get("tf_rsi", {})
        signals_1h = snap.get("signals_1h", [])
        signals_4h = snap.get("signals_4h", [])
        status_1h = snap.get("status_1h", "CALIBRATING...")
        status_4h = snap.get("status_4h", "CALIBRATING...")
        active_trades = snap.get("active_trades", [])
        audit_archive = snap.get("audit_archive", [])

        mid_price = metrics.get("mid_price", 0.0)
        if mid_price <= 0.0 and coin in tickers:
            mid_price = float(tickers[coin].get("price", 0.0))

        imbalance = metrics.get("imbalance_t", 0.0)
        garch_vol = metrics.get("garch_sigma", 0.0045)

        # Top Telemetry Row
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"{coin} Live Price", fmt_px(mid_price))
        m2.metric("1H / 4H Structure", f"1H: {tf_bias.get('1H', '—')} | 4H: {tf_bias.get('4H', '—')}")
        m3.metric("L2 Imbalance / Vol", f"{imbalance:+.3f} | {garch_vol*100:.2f}%")
        m4.metric("Stream Ticks", f"{snap.get('msg_count', 0):,}")

        st.markdown("---")

        # Liquidation & Structure Strip
        st.subheader(f"🔥 Godzilla Liquidation Clusters ({coin})")
        l1, l2, l3 = st.columns(3)
        l1.metric(
            "Long Liquidation Cluster",
            fmt_px(liq.get("long_cluster", 0.0)),
            f"${liq.get('long_usd', 0.0):,.0f} Est. Pool"
        )
        l2.metric(
            "Short Liquidation Cluster",
            fmt_px(liq.get("short_cluster", 0.0)),
            f"${liq.get('short_usd', 0.0):,.0f} Est. Pool"
        )
        l3.metric("RSI Momentum (1H / 4H)", f"1H: {tf_rsi.get('1H', 50.0):.1f} | 4H: {tf_rsi.get('4H', 50.0):.1f}")

        st.markdown("---")

        # Decoupled 1H & 4H Signal Scanner Tabs
        st.subheader(f"📡 Godzilla Signal Scanner — {coin}")
        scan_tab_1h, scan_tab_4h = st.tabs([
            f"⚡ 1H Intraday Scanner ({len(signals_1h)})",
            f"🌊 4H Swing Scanner ({len(signals_4h)})"
        ])

        with scan_tab_1h:
            if signals_1h:
                for sig in signals_1h:
                    render_signal_card(sig, mid_price, "1H")
            else:
                st.info(f"⚡ **1H Intraday Engine:** {status_1h}")

        with scan_tab_4h:
            if signals_4h:
                for sig in signals_4h:
                    render_signal_card(sig, mid_price, "4H")
            else:
                st.info(f"🌊 **4H Swing Engine:** {status_4h}")

        # Persistent Active Cloud Trades
        st.markdown("---")
        trades_1h = [t for t in active_trades if t.get("timeframe") == "1H"]
        trades_4h = [t for t in active_trades if t.get("timeframe") == "4H"]

        st.subheader(f"📊 Godzilla Active Cloud Trades ({len(active_trades)} / 10 Locked)")
        act_tab_all, act_tab_1h, act_tab_4h = st.tabs([
            f"📋 All Active Trades ({len(active_trades)})",
            f"⚡ Active 1H Trades ({len(trades_1h)})",
            f"🌊 Active 4H Trades ({len(trades_4h)})"
        ])

        with act_tab_all:
            render_active_trades_table(active_trades, tickers, "ALL")

        with act_tab_1h:
            render_active_trades_table(trades_1h, tickers, "1H")

        with act_tab_4h:
            render_active_trades_table(trades_4h, tickers, "4H")

        # ------------------------------------------------------------------
        # BACKTESTING & AUDIT LOGBOOK (COLLAPSIBLE ARCHIVE FOR 1D/1W/1M REVIEW)
        # ------------------------------------------------------------------
        st.markdown("---")
        with st.expander(f"📚 Godzilla Backtest & Signal Audit Archive ({len(audit_archive)} Committed Signals)", expanded=False):
            st.caption("Review historical backtested outcomes across 1-day, 1-week, and 1-month intervals to refine edge and win-rate assumptions.")
            if audit_archive:
                for idx, arch in enumerate(audit_archive):
                    c_dt = datetime.fromtimestamp(arch.get("committed_at", time.time()), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                    st.markdown(f"""
                    **#{idx+1} | {arch.get('symbol')} [{arch.get('timeframe')}] {arch.get('direction')}** — Committed at: *{c_dt}*
                    * **Entry:** {fmt_px(arch.get('entry_price', 0))} | **Shared SL:** {fmt_px(arch.get('stop_loss', 0))}
                    * **Targets:** TP1: {fmt_px(arch.get('tp1', 0))} ({arch.get('eta_tp1', '')}) | TP2: {fmt_px(arch.get('tp2', 0))} ({arch.get('eta_tp2', '')}) | TP3: {fmt_px(arch.get('tp3', 0))} ({arch.get('eta_tp3', '')})
                    ---
                    """)
            else:
                st.info("No audit history recorded yet. Commit signals from the scanner above to populate the backtest archive.")

    render_godzilla_dashboard()

with tab_pro:
    st.subheader("🔬 Godzilla Microstructure & Audit Terminal")
    p_col1, p_col2, p_col3, p_col4 = st.columns(4)
    with p_col1:
        pro_symbol = st.selectbox("Pro Asset", ASSETS, index=0)
    with p_col2:
        decay_lambda = st.slider("Decay Lambda (λ)", 0.05, 1.0, 0.35, 0.05)
    with p_col3:
        imbalance_threshold = st.slider("Imbalance Threshold", 0.10, 0.80, 0.22, 0.05)
    with p_col4:
        garch_k = st.slider("GARCH Multiplier (k)", 1.0, 3.0, 1.5, 0.1)

    max_spread_phi = st.number_input("Max Spread Filter (φ)", value=0.0008, format="%.5f")

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
    pm1.metric("Mid Price", fmt_px(pro_metrics.get("mid_price", 0.0)))
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

    st.markdown("**Godzilla Signal Audit Logbook**")
    audit_logs = pro_snap.get("audit_archive", [])
    if audit_logs:
        for entry in audit_logs:
            st.text(f"[{entry.get('symbol')} {entry.get('timeframe')}] {entry.get('direction')} @ {entry.get('entry_price')}")
    else:
        st.caption("No confirmed signals logged yet.")
