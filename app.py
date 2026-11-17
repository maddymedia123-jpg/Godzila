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
        color: #8b9eb0;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.08em;
        padding: 10px 14px;
        border-radius: 8px 8px 0 0;
        border: 1px solid #223240;
    }
    .gz-row-card {
        background: linear-gradient(90deg, #0f1922 0%, #14222e 50%, #0f1922 100%);
        color: #e6edf3;
        padding: 12px 14px;
        border-left: 1px solid #223240;
        border-right: 1px solid #223240;
        border-bottom: 1px solid #1e2d3a;
    }
    .gz-pill-green {
        display: inline-block;
        background: rgba(16, 185, 129, 0.18);
        border: 1px solid #10b981;
        color: #34d399;
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 4px;
        margin-bottom: 3px;
    }
    .gz-pill-red {
        display: inline-block;
        background: rgba(239, 68, 68, 0.18);
        border: 1px solid #ef4444;
        color: #f87171;
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 4px;
        margin-bottom: 3px;
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


def build_proximity_bar_html(
    label: str,
    entry: float,
    curr: float,
    sl: float,
    tp: float,
    direction: str,
    tp_hit: bool,
    sl_hit: bool,
) -> str:
    if direction == "LONG":
        sl_dist = max(entry - sl, 1e-8)
        tp_dist = max(tp - entry, 1e-8)
        move = curr - entry
    else:
        sl_dist = max(sl - entry, 1e-8)
        tp_dist = max(entry - tp, 1e-8)
        move = entry - curr

    if tp_hit or move >= tp_dist:
        pct_right = 100.0
        pct_left = 0.0
        badge = f"<span style='background:#10b981;color:#042f2e;font-weight:800;font-size:10px;padding:1px 6px;border-radius:4px;'>{label} HIT</span>"
        callout = "<span style='color:#34d399;font-size:10px;font-weight:700;'>TARGET REACHED</span>"
    elif sl_hit or move <= -sl_dist:
        pct_right = 0.0
        pct_left = 100.0
        badge = "<span style='background:#ef4444;color:#fff;font-weight:800;font-size:10px;padding:1px 6px;border-radius:4px;'>SL HIT</span>"
        callout = "<span style='color:#f87171;font-size:10px;font-weight:700;'>STOPPED OUT</span>"
    elif move >= 0:
        pct_right = min(100.0, (move / tp_dist) * 100.0)
        pct_left = 0.0
        rem_pct = abs((tp - curr) / max(curr, 1e-8)) * 100.0
        badge = ""
        callout = f"<span style='color:#34d399;font-size:10px;font-weight:600;'>+{rem_pct:.2f}% to {label}</span>"
    else:
        pct_left = min(100.0, (abs(move) / sl_dist) * 100.0)
        pct_right = 0.0
        rem_sl_pct = abs((curr - sl) / max(curr, 1e-8)) * 100.0
        badge = ""
        callout = f"<span style='color:#f87171;font-size:10px;font-weight:600;'>{rem_sl_pct:.2f}% to SL</span>"

    return f"""
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
        <span style="width:30px;color:#8b9eb0;font-size:11px;font-weight:700;">{label}</span>
        <div style="flex:1;display:flex;align-items:center;position:relative;height:14px;">
            <div style="width:50%;height:5px;background:#1e293b;border-radius:3px 0 0 3px;display:flex;justify-content:flex-end;overflow:hidden;">
                <div style="width:{pct_left:.1f}%;height:100%;background:linear-gradient(90deg,#ef4444,#f87171);"></div>
            </div>
            <div style="width:2px;height:12px;background:#64748b;z-index:2;"></div>
            <div style="width:50%;height:5px;background:#1e293b;border-radius:0 3px 3px 0;display:flex;justify-content:flex-start;overflow:hidden;">
                <div style="width:{pct_right:.1f}%;height:100%;background:linear-gradient(90deg,#059669,#34d399);box-shadow:0 0 8px #10b981;"></div>
            </div>
        </div>
        <div style="width:145px;text-align:right;white-space:nowrap;">{badge} {callout}</div>
    </div>
    """


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
        <div style="display:grid;grid-template-columns:1.35fr 1.1fr 1.2fr 3.2fr 1.1fr 1.15fr 0.4fr;align-items:center;">
            <div>ASSET / PAIR</div>
            <div>CURRENT PRICE</div>
            <div>SHARED SL</div>
            <div>PROXIMITY BAR (SL-LEFT, TP-RIGHT)</div>
            <div>TP TARGET PRICE</div>
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

        live_px = float(tickers.get(t_sym, {}).get("price", 0.0))
        if live_px <= 0:
            live_px = t_entry

        sl_pct = ((t_sl - t_entry) / max(t_entry, 1e-8)) * 100.0
        icon_char, icon_color = COIN_BADGES.get(t_sym, ("◈", "#38bdf8"))
        dir_color = "#34d399" if t_dir == "LONG" else "#f87171"
        tf_badge_bg = "#1d4ed8" if t_tf == "1H" else "#7c3aed"

        bar1 = build_proximity_bar_html("TP1", t_entry, live_px, t_sl, t_tp1, t_dir, tr.get("tp1_hit", False), tr.get("sl_hit", False))
        bar2 = build_proximity_bar_html("TP2", t_entry, live_px, t_sl, t_tp2, t_dir, tr.get("tp2_hit", False), tr.get("sl_hit", False))
        bar3 = build_proximity_bar_html("TP3", t_entry, live_px, t_sl, t_tp3, t_dir, tr.get("tp3_hit", False), tr.get("sl_hit", False))

        def status_pill(lbl: str, tp_val: float, hit: bool) -> str:
            if tr.get("sl_hit"):
                return "<div class='gz-pill-red'>SL HIT</div>"
            if hit:
                return f"<div class='gz-pill-green'>{lbl} HIT</div>"
            tot = abs(tp_val - t_entry)
            prog = (live_px - t_entry) if t_dir == "LONG" else (t_entry - live_px)
            if prog >= 0:
                pct = min(99, int((prog / max(tot, 1e-8)) * 100))
                return f"<div class='gz-pill-green'>{pct}% to {lbl}</div>"
            sl_tot = abs(t_entry - t_sl)
            sl_p = min(99, int((abs(prog) / max(sl_tot, 1e-8)) * 100))
            return f"<div class='gz-pill-red'>{sl_p}% to SL</div>"

        p1_pill = status_pill("TP1", t_tp1, tr.get("tp1_hit", False))
        p2_pill = status_pill("TP2", t_tp2, tr.get("tp2_hit", False))
        p3_pill = status_pill("TP3", t_tp3, tr.get("tp3_hit", False))

        row_cols = st.columns([9.1, 0.5])
        with row_cols[0]:
            st.markdown(f"""
            <div class="gz-row-card">
                <div style="display:grid;grid-template-columns:1.35fr 1.15fr 1.25fr 3.35fr 1.15fr 1.15fr;align-items:center;gap:6px;">
                    <div>
                        <span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;background:{icon_color};color:#0b1117;font-weight:900;margin-right:6px;">{icon_char}</span>
                        <strong style="font-size:14px;">{t_sym.replace('USDT','')}/USDT</strong>
                        <span style="background:{tf_badge_bg};color:#fff;font-size:10px;font-weight:800;padding:1px 5px;border-radius:3px;margin-left:4px;">{t_tf}</span><br/>
                        <span style="color:{dir_color};font-size:11px;font-weight:700;margin-left:28px;">{t_dir} · Entry {fmt_px(t_entry)}</span>
                    </div>
                    <div style="font-size:15px;font-weight:700;color:#ffffff;">
                        {fmt_px(live_px)}
                    </div>
                    <div style="font-size:13px;font-weight:600;color:#e2e8f0;">
                        {fmt_px(t_sl)} <span style="color:#f87171;font-size:11px;">({sl_pct:+.2f}%)</span>
                    </div>
                    <div>
                        {bar1}{bar2}{bar3}
                    </div>
                    <div style="font-size:12px;line-height:1.65;color:#cbd5e1;font-weight:600;">
                        <div>{fmt_px(t_tp1)}</div>
                        <div>{fmt_px(t_tp2)}</div>
                        <div>{fmt_px(t_tp3)}</div>
                    </div>
                    <div style="line-height:1.4;">
                        {p1_pill}<br/>{p2_pill}<br/>{p3_pill}
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        with row_cols[1]:
            st.write("")
            if st.button("✕", key=f"kill_{tab_prefix}_{t_id}", help=f"Kill {t_sym} [{t_tf}] trade"):
                manager.kill_trade(t_id)
                st.rerun()


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
    audit_logs = pro_snap.get("audit_logbook", [])
    if audit_logs:
        for entry in audit_logs:
            st.text(entry)
    else:
        st.caption("No confirmed 1H/4H signals logged yet.")
