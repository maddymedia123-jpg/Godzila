import csv
import io
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
    @keyframes gzBlinkRed {
        0% { opacity: 1; box-shadow: 0 0 12px rgba(248, 113, 113, 0.95); }
        50% { opacity: 0.25; box-shadow: 0 0 2px rgba(248, 113, 113, 0.2); }
        100% { opacity: 1; box-shadow: 0 0 12px rgba(248, 113, 113, 0.95); }
    }
    .gz-blink-red {
        animation: gzBlinkRed 1.0s infinite;
        background-color: #1f0508 !important;
        border: 1px solid #f87171 !important;
        color: #ff6b6b !important;
        font-weight: 900 !important;
        padding: 8px 12px !important;
        border-radius: 6px !important;
        display: inline-block;
    }
    .gz-blink-dot {
        display: inline-block;
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background-color: #ff3b3b;
        margin-right: 8px;
        animation: gzBlinkRed 0.9s infinite;
    }
    .stApp, .block-container {
        background-color: #000000 !important;
        color: #ffffff !important;
        padding-top: 1.2rem;
        padding-bottom: 2rem;
    }
    h1, h2, h3, h4, h5, h6, p, span, div, label {
        color: #ffffff;
    }
    [data-testid="stWidgetLabel"] p,
    [data-testid="stWidgetLabel"] label,
    .stRadio label span,
    .stCheckbox label span,
    .stToggle label span {
        color: #ffffff !important;
        font-weight: 700 !important;
    }
    .stMetric {
        background-color: #070c12 !important;
        padding: 14px 16px !important;
        border-radius: 8px !important;
        border: 1px solid #1e3a5f !important;
    }
    [data-testid="stMetricLabel"] > div,
    [data-testid="stMetricLabel"] p {
        color: #38bdf8 !important;
        font-weight: 800 !important;
        font-size: 13px !important;
    }
    [data-testid="stMetricValue"] > div {
        color: #ffffff !important;
        font-weight: 900 !important;
    }
    [data-testid="stMetricDelta"] > div,
    [data-testid="stMetricDelta"] span {
        color: #34d399 !important;
        font-weight: 800 !important;
        background-color: #0b1926 !important;
        border-radius: 6px !important;
        padding: 2px 8px !important;
    }
    .gz-entry-banner {
        background-color: #051712 !important;
        border: 1px solid #34d399 !important;
        border-radius: 8px !important;
        padding: 12px 16px !important;
        color: #6ee7b7 !important;
        font-weight: 800 !important;
        margin-bottom: 12px !important;
    }
    .gz-nosignal-box {
        background-color: #000000 !important;
        border: 1px solid #38bdf8 !important;
        border-radius: 8px !important;
        padding: 16px 18px !important;
        color: #38bdf8 !important;
        font-weight: 800 !important;
        line-height: 1.7 !important;
    }
    .gz-dark-wrapper {
        background-color: #000000;
        color: #ffffff;
        padding: 18px;
        border-radius: 12px;
        border: 1px solid #1e3a5f;
        margin-top: 10px;
        margin-bottom: 20px;
    }
    .gz-tracker-header {
        background: linear-gradient(90deg, #07111a 0%, #0e1f2f 100%);
        color: #ffffff;
        font-size: 13px;
        font-weight: 900;
        letter-spacing: 0.06em;
        padding: 14px 16px;
        border-radius: 8px 8px 0 0;
        border: 1px solid #1e3a5f;
        margin-bottom: 8px;
    }
    .gz-row-container {
        background: linear-gradient(90deg, #070e16 0%, #0c1824 50%, #070e16 100%);
        border: 1px solid #1e3a5f;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
        color: #ffffff;
    }
    .gz-progress-track {
        background-color: #132232;
        border-radius: 4px;
        height: 10px;
        width: 100%;
        overflow: hidden;
        margin: 6px 0 10px 0;
        border: 1px solid #25415e;
    }
    .gz-progress-fill-green {
        background: linear-gradient(90deg, #059669, #34d399);
        height: 100%;
    }
    .gz-progress-fill-red {
        background: linear-gradient(90deg, #dc2626, #f87171);
        height: 100%;
    }
    </style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")


manager = get_stream_manager()

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
COIN_BADGES = {
    "BTCUSDT": ("₿", "#facc15"),
    "ETHUSDT": ("Ξ", "#38bdf8"),
    "SOLUSDT": ("◎", "#34d399"),
    "XRPUSDT": ("✕", "#22d3ee"),
    "DOGEUSDT": ("Ð", "#fbbf24"),
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


def build_audit_csv(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Symbol", "Timeframe", "Direction", "Entry", "SL", "TP1", "TP2", "TP3", "Status", "SqueezeAlert", "LoggedTS"])
    for r in rows:
        writer.writerow([
            r.get("symbol", ""),
            r.get("timeframe", ""),
            r.get("direction", ""),
            r.get("entry_price", 0),
            r.get("stop_loss", 0),
            r.get("tp1", 0),
            r.get("tp2", 0),
            r.get("tp3", 0),
            r.get("status_note", ""),
            r.get("squeeze_alert", ""),
            r.get("committed_at", 0),
        ])
    return buf.getvalue()


def render_prioritized_signal_card(sig, mid_price: float, rank_idx: int):
    is_long = sig.direction == "LONG"
    dir_color = "#34d399" if is_long else "#f87171"
    rank_label = "PRIMARY PRIORITY SIGNAL (#1)" if rank_idx == 0 else f"SECONDARY QUALIFIED SIGNAL (#{rank_idx + 1})"
    rank_color = "#facc15" if rank_idx == 0 else "#38bdf8"

    with st.container(border=True):
        st.markdown(
            f"<div style='color:{rank_color};font-weight:900;font-size:13px;letter-spacing:0.05em;margin-bottom:4px;'>★ {rank_label} · ACCURACY INDEX: {sig.accuracy_score:.1f}</div>",
            unsafe_allow_html=True
        )
        r1, r2, r3 = st.columns([2.2, 2.6, 1.4])
        r1.markdown(
            f"### {sig.symbol} · <span style='color:{dir_color};'>{sig.direction}</span> · <span style='color:#facc15;'>[{sig.timeframe}]</span>",
            unsafe_allow_html=True
        )
        r2.markdown(
            f"<div style='color:#ffffff;font-weight:700;'><strong>Mode:</strong> {sig.risk_tier}</div>"
            f"<div style='color:#ffffff;font-weight:700;margin-top:4px;'>{sig.htf_summary}</div>"
            f"<div style='color:#34d399;font-weight:900;margin-top:4px;'>Alignment: {sig.alignment_status}</div>",
            unsafe_allow_html=True
        )
        r3.markdown(
            f"<div style='color:#ffffff;font-weight:800;'>Leverage: <span style='color:#facc15;'>{sig.leverage}x</span></div>"
            f"<div style='color:#ffffff;font-weight:800;margin-top:4px;'>Margin: <span style='color:#38bdf8;'>${sig.margin_required_usd:,.2f}</span></div>",
            unsafe_allow_html=True
        )

        st.markdown(
            f"<div class='gz-entry-banner'>✅ {sig.entry_status} — Current Price: <strong>{fmt_px(mid_price)}</strong> vs Entry: <strong>{fmt_px(sig.entry_price)}</strong></div>",
            unsafe_allow_html=True
        )

        if sig.squeeze_alert:
            st.markdown(
                f"<div class='gz-blink-red' style='margin-bottom:12px;'><span class='gz-blink-dot'></span>{sig.squeeze_alert}</div>",
                unsafe_allow_html=True
            )

        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Entry Zone", fmt_px(sig.entry_price))
        p2.metric("Shared SL", fmt_px(sig.stop_loss))
        p3.metric("TP 1", fmt_px(sig.tp1), sig.eta_tp1_str)
        p4.metric("TP 2", fmt_px(sig.tp2), sig.eta_tp2_str)
        p5.metric("TP 3", fmt_px(sig.tp3), sig.eta_tp3_str)

        st.markdown(f"<div style='color:#facc15;font-weight:700;margin-bottom:8px;'>⚠️ {sig.risk_warning}</div>", unsafe_allow_html=True)

        if st.button(
            f"🔒 Lock [{sig.timeframe}] {sig.symbol} {sig.direction} into Active Trades",
            key=f"commit_prio_{rank_idx}_{sig.signal_id}"
        ):
            trade_record = {
                "trade_id": sig.signal_id,
                "symbol": sig.symbol,
                "direction": sig.direction,
                "timeframe": sig.timeframe,
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
                "alignment_status": sig.alignment_status,
                "accuracy_score": sig.accuracy_score,
                "squeeze_alert": sig.squeeze_alert,
                "committed_at": time.time(),
            }
            ok, msg = manager.commit_trade(trade_record)
            if ok:
                st.success(msg)
            else:
                st.error(msg)


def render_active_trades_table(trades_list: list, tickers: dict, tab_prefix: str):
    if not trades_list:
        st.markdown(
            f"<div style='color:#ffffff;font-weight:700;'>No active {tab_prefix} trades locked in Godzilla Cloud yet.</div>",
            unsafe_allow_html=True
        )
        return

    st.markdown('<div class="gz-dark-wrapper">', unsafe_allow_html=True)
    st.markdown("""
    <div class="gz-tracker-header">
        <div style="display:grid;grid-template-columns:1.3fr 1.3fr 1.2fr 3.2fr 1.4fr 1.3fr 0.4fr;align-items:center;color:#38bdf8;">
            <div>ASSET / PAIR</div>
            <div>LIVE vs ENTRY PRICE</div>
            <div>SHARED SL</div>
            <div>PROXIMITY BAR (SL-LEFT, TP-RIGHT)</div>
            <div>TP TARGET & PKT TIME</div>
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
        sq_alert = tr.get("squeeze_alert", "")

        live_px = float(tickers.get(t_sym, {}).get("price", 0.0))
        if live_px <= 0:
            live_px = t_entry

        is_long = t_dir == "LONG"
        sl_hit = tr.get("sl_hit", False)
        tp1_hit = tr.get("tp1_hit", False)
        tp2_hit = tr.get("tp2_hit", False)
        tp3_hit = tr.get("tp3_hit", False)

        sl_pct = ((t_sl - t_entry) / max(t_entry, 1e-8)) * 100.0
        price_diff_pct = ((live_px - t_entry) / max(t_entry, 1e-8)) * 100.0
        diff_color = "#34d399" if (price_diff_pct >= 0 if is_long else price_diff_pct <= 0) else "#f87171"

        icon_char, icon_color = COIN_BADGES.get(t_sym, ("◈", "#38bdf8"))
        dir_color = "#34d399" if is_long else "#f87171"
        fill_class = "gz-progress-fill-green" if is_long else "gz-progress-fill-red"
        tf_badge_bg = "#2563eb" if t_tf == "1H" else "#7c3aed"

        def get_progress_val(tp_val: float, hit: bool) -> float:
            if hit or tp3_hit:
                return 100.0
            if sl_hit:
                return 0.0
            if is_long:
                dist = max(tp_val - t_entry, 1e-8)
                progress = live_px - t_entry
            else:
                dist = max(t_entry - tp_val, 1e-8)
                progress = t_entry - live_px
            return float(min(100.0, max(0.0, (progress / dist) * 100.0)))

        p1_pct = get_progress_val(t_tp1, tp1_hit)
        p2_pct = get_progress_val(t_tp2, tp2_hit)
        p3_pct = get_progress_val(t_tp3, tp3_hit)

        with st.container():
            st.markdown('<div class="gz-row-container">', unsafe_allow_html=True)
            if sq_alert and not sl_hit:
                st.markdown(
                    f"<div class='gz-blink-red' style='margin-bottom:10px;'><span class='gz-blink-dot'></span>{sq_alert}</div>",
                    unsafe_allow_html=True
                )
            cols = st.columns([1.3, 1.3, 1.2, 3.2, 1.4, 1.3, 0.4])

            with cols[0]:
                st.markdown(f"""
                <span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:{icon_color};color:#000000;font-weight:900;margin-right:6px;font-size:13px;">{icon_char}</span>
                <strong style="font-size:16px;color:#ffffff;">{t_sym.replace('USDT','')}/USDT</strong>
                <span style="background:{tf_badge_bg};color:#ffffff;font-size:11px;font-weight:900;padding:2px 6px;border-radius:3px;margin-left:4px;">{t_tf}</span><br/>
                <span style="color:{dir_color};font-size:14px;font-weight:900;margin-left:30px;">{t_dir}</span>
                """, unsafe_allow_html=True)

            with cols[1]:
                st.markdown(f"""
                <div style='font-size:15px;font-weight:900;color:#ffffff;'>Live: {fmt_px(live_px)}</div>
                <div style='font-size:13px;font-weight:800;color:#38bdf8;'>Entry: {fmt_px(t_entry)}</div>
                <div style='font-size:13px;font-weight:900;color:{diff_color};'>({price_diff_pct:+.2f}%)</div>
                """, unsafe_allow_html=True)

            with cols[2]:
                st.markdown(
                    f"<div style='font-size:15px;font-weight:800;color:#ffffff;padding-top:4px;'>{fmt_px(t_sl)}<br/>"
                    f"<span style='color:#f87171;font-size:13px;font-weight:800;'>({sl_pct:+.2f}%)</span></div>",
                    unsafe_allow_html=True
                )

            with cols[3]:
                if sl_hit:
                    st.markdown(
                        "<div style='font-size:13px;color:#f87171;font-weight:900;padding:12px 0;text-align:center;'>STOP-LOSS HIT — MONITORING FROZEN</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.markdown(f"""
                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#ffffff;margin-bottom:2px;font-weight:800;">
                        <span>TP1 Progress:</span><span style="color:#34d399;">{p1_pct:.0f}%</span>
                    </div>
                    <div class="gz-progress-track"><div class="{fill_class}" style="width:{p1_pct}%;"></div></div>
                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#ffffff;margin-bottom:2px;font-weight:800;">
                        <span>TP2 Progress:</span><span style="color:#34d399;">{p2_pct:.0f}%</span>
                    </div>
                    <div class="gz-progress-track"><div class="{fill_class}" style="width:{p2_pct}%;"></div></div>
                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#ffffff;margin-bottom:2px;font-weight:800;">
                        <span>TP3 Progress:</span><span style="color:#34d399;">{p3_pct:.0f}%</span>
                    </div>
                    <div class="gz-progress-track"><div class="{fill_class}" style="width:{p3_pct}%;"></div></div>
                    """, unsafe_allow_html=True)

            with cols[4]:
                if sl_hit:
                    st.markdown("<div style='font-size:13px;color:#f87171;font-weight:800;padding-top:14px;'>Halted at SL.</div>", unsafe_allow_html=True)
                else:
                    st.markdown(f"""
                    <div style="font-size:13px;line-height:1.7;color:#ffffff;font-weight:800;padding-top:2px;">
                        <div>TP1: <strong>{fmt_px(t_tp1)}</strong><br/><span style='color:#38bdf8;font-size:12px;'>🕒 {eta1}</span></div>
                        <div>TP2: <strong>{fmt_px(t_tp2)}</strong><br/><span style='color:#38bdf8;font-size:12px;'>🕒 {eta2}</span></div>
                        <div>TP3: <strong>{fmt_px(t_tp3)}</strong><br/><span style='color:#38bdf8;font-size:12px;'>🕒 {eta3}</span></div>
                    </div>
                    """, unsafe_allow_html=True)

            with cols[5]:
                def status_badge(lbl: str, hit: bool, pct: float) -> str:
                    if sl_hit:
                        return "<span class='gz-blink-red'><span class='gz-blink-dot'></span>SL HIT</span>"
                    if tp3_hit and lbl == "TP3":
                        return "<span style='color:#34d399;font-weight:900;font-size:13px;'>ALL TPs HIT</span>"
                    if hit or pct >= 100.0:
                        return f"<span style='color:#34d399;font-weight:900;font-size:13px;'>{lbl} HIT</span>"
                    return f"<span style='color:#38bdf8;font-weight:800;font-size:13px;'>{pct:.0f}% to {lbl}</span>"

                b1 = status_badge("TP1", tp1_hit, p1_pct)
                b2 = status_badge("TP2", tp2_hit, p2_pct)
                b3 = status_badge("TP3", tp3_hit, p3_pct)
                st.markdown(f"<div style='line-height:2.0;padding-top:4px;'>{b1}<br/>{b2}<br/>{b3}</div>", unsafe_allow_html=True)

            with cols[6]:
                st.write("")
                if st.button("✕", key=f"kill_{tab_prefix}_{t_id}", help=f"Kill {t_sym} [{t_tf}] trade"):
                    manager.kill_trade(t_id)
                    st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


pkt_tz = timezone(timedelta(hours=5))
now_pkt = datetime.now(timezone.utc).astimezone(pkt_tz)

hdr_col1, hdr_col2, hdr_col3 = st.columns([3.2, 1.4, 1.4])
with hdr_col1:
    st.markdown("## 🦖 Godzilla")
    st.markdown(
        f"<div style='color:#ffffff;font-weight:800;'>🕒 Server Clock: <span style='color:#facc15;'>{now_pkt.strftime('%H:%M:%S')} PKT</span> · Testing Mode: <span style='color:#34d399;'>1H & 4H ONLY</span></div>",
        unsafe_allow_html=True
    )

with hdr_col2:
    st.write("")
    scanner_active = st.toggle("🟢 LIVE AUTO-SCANNER: ON", value=True)

with hdr_col3:
    st.write("")
    auto_refresh = st.toggle("⚡ Real-Time Stream (500ms)", value=True)

tab_student, tab_pro = st.tabs([
    "🎯 Godzilla Prioritized Signal Terminal (1H · 4H)",
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
        imbalance_threshold=0.25,
        max_spread_phi=0.0008,
        garch_k=1.8,
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
        prioritized_signals = snap.get("prioritized_signals", [])
        status_1h = snap.get("status_1h", "CALIBRATING...")
        status_4h = snap.get("status_4h", "CALIBRATING...")
        active_trades = snap.get("active_trades", [])
        audit_archive = snap.get("audit_archive", [])
        squeeze_state = metrics.get("squeeze_state", "NORMAL")
        squeeze_msg = metrics.get("squeeze_msg", "")
        has_sl_in_active = any(t.get("sl_hit") or t.get("squeeze_alert") for t in active_trades)

        if squeeze_state != "NORMAL" or has_sl_in_active:
            alert_txt = squeeze_msg or "ACTIVE TRADE WARNING: STOP-LOSS OR SQUEEZE CONDITION DETECTED"
            st.markdown(
                f"<div class='gz-blink-red' style='width:100%;margin-bottom:12px;'><span class='gz-blink-dot'></span>{alert_txt}</div>",
                unsafe_allow_html=True
            )

        mid_price = metrics.get("mid_price", 0.0)
        if mid_price <= 0.0 and coin in tickers:
            mid_price = float(tickers[coin].get("price", 0.0))

        imbalance = metrics.get("imbalance_t", 0.0)
        garch_vol = metrics.get("garch_sigma", 0.0045)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"{coin} Live Price", fmt_px(mid_price))
        m2.metric("1H / 4H / 1D Bias", f"{tf_bias.get('1H', '—')} / {tf_bias.get('4H', '—')} / {tf_bias.get('1D', '—')}")
        m3.metric("L2 Imbalance / Vol", f"{imbalance:+.3f} | {garch_vol*100:.2f}%")
        m4.metric("Stream Ticks", f"{snap.get('msg_count', 0):,}")

        st.markdown("---")

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

        st.subheader(f"📡 Godzilla High-Accuracy Signal Queue — {coin}")
        if prioritized_signals:
            for idx, sig in enumerate(prioritized_signals):
                render_prioritized_signal_card(sig, mid_price, idx)
        else:
            st.markdown(f"""
            <div class="gz-nosignal-box">
                ⏸️ NO QUALIFYING SIGNAL RIGHT NOW — Accuracy filters are holding back trades.<br/><br/>
                • 4H Status: {status_4h}<br/>
                • 1H Status: {status_1h}
            </div>
            """, unsafe_allow_html=True)

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

        st.markdown("---")
        st.subheader(f"📚 Godzilla Automatic Signal Audit Logbook ({len(audit_archive)} Tracked Bot Signals)")
        st.markdown(
            f"<div style='color:#38bdf8;font-weight:700;margin-bottom:8px;'>Storage: <strong>{snap.get('db_status', 'LOCAL FILE ONLY')}</strong> · Every signal emitted by the bot is automatically recorded, tracked against live price for TP1/TP2/TP3/SL, and saved.</div>",
            unsafe_allow_html=True
        )

        st.download_button(
            "⬇️ Export Audit Logbook (CSV)",
            data=build_audit_csv(audit_archive),
            file_name="godzilla_audit_logbook.csv",
            mime="text/csv"
        )

        audit_filter = st.radio(
            "Audit Timeframe Filter",
            ["All Time", "Last 24 Hours (1 Day)", "Last 7 Days (1 Week)", "Last 30 Days (1 Month)"],
            horizontal=True
        )

        filtered_audit = []
        now_ts = time.time()
        for arch in audit_archive:
            c_time = arch.get("committed_at", now_ts)
            diff = now_ts - c_time
            if audit_filter == "Last 24 Hours (1 Day)" and diff > 86400:
                continue
            if audit_filter == "Last 7 Days (1 Week)" and diff > 7 * 86400:
                continue
            if audit_filter == "Last 30 Days (1 Month)" and diff > 30 * 86400:
                continue
            filtered_audit.append(arch)

        sl_count = sum(1 for a in filtered_audit if a.get("sl_hit"))
        tp_count = sum(1 for a in filtered_audit if a.get("tp1_hit") or a.get("tp2_hit") or a.get("tp3_hit"))
        open_count = sum(1 for a in filtered_audit if not a.get("sl_hit") and not a.get("tp3_hit"))

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total Signals Logged", f"{len(filtered_audit)}")
        s2.metric("TP Hits (TP1+)", f"{tp_count}")
        s3.metric("SL Hits", f"{sl_count}")
        s4.metric("Still Tracking", f"{open_count}")

        with st.expander(f"📖 Full Automated Audit Logbook ({len(filtered_audit)} Signals)", expanded=True):
            if filtered_audit:
                for idx, arch in enumerate(filtered_audit):
                    c_pkt = datetime.fromtimestamp(arch.get("committed_at", time.time()) + 18000, tz=timezone.utc).strftime('%a %H:%M PKT').upper()
                    d_col = "#34d399" if arch.get('direction') == "LONG" else "#f87171"
                    status_note = arch.get('status_note', 'ACTIVE')
                    status_color = "#f87171" if "SL" in status_note or "KILLED" in status_note else "#34d399"
                    src_tag = arch.get("source_mode", "BOT_SIGNAL")
                    st.markdown(f"""
                    <div style='color:#ffffff;font-weight:700;'>
                    <strong>#{idx+1} | {arch.get('symbol')} <span style='color:#facc15;'>[{arch.get('timeframe')}]</span> <span style='color:{d_col};'>{arch.get('direction')}</span></strong>
                    — Outcome: <span style='color:{status_color};font-weight:900;'>{status_note}</span>
                    — Source: <span style='color:#38bdf8;'>{src_tag}</span>
                    — Logged: <span style='color:#facc15;'>{c_pkt}</span><br/>
                    Entry: <strong>{fmt_px(arch.get('entry_price', 0))}</strong> | SL: <strong style='color:#f87171;'>{fmt_px(arch.get('stop_loss', 0))}</strong> |
                    TP1: <strong style='color:#34d399;'>{fmt_px(arch.get('tp1', 0))}</strong> |
                    TP2: <strong style='color:#34d399;'>{fmt_px(arch.get('tp2', 0))}</strong> |
                    TP3: <strong style='color:#34d399;'>{fmt_px(arch.get('tp3', 0))}</strong>
                    </div>
                    <hr style='border-color:#1e3a5f;margin:8px 0;'/>
                    """, unsafe_allow_html=True)
            else:
                st.markdown(
                    "<div style='color:#ffffff;font-weight:700;'>No signals logged in this window yet. As soon as a 1H or 4H signal qualifies, it will be recorded automatically.</div>",
                    unsafe_allow_html=True
                )

    render_godzilla_dashboard()

with tab_pro:
    st.subheader("🔬 Godzilla Microstructure & Audit Terminal")
    p_col1, p_col2, p_col3, p_col4 = st.columns(4)
    with p_col1:
        pro_symbol = st.selectbox("Pro Asset", ASSETS, index=0)
    with p_col2:
        decay_lambda = st.slider("Decay Lambda (λ)", 0.05, 1.0, 0.35, 0.05)
    with p_col3:
        imbalance_threshold = st.slider("Imbalance Threshold", 0.10, 0.80, 0.25, 0.05)
    with p_col4:
        garch_k = st.slider("GARCH Multiplier (k)", 1.0, 3.0, 1.8, 0.1)

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
            st.text(f"[{entry.get('symbol')} {entry.get('timeframe')}] {entry.get('direction')} @ {entry.get('entry_price')} - {entry.get('status_note', 'ACTIVE')}")
    else:
        st.markdown("<div style='color:#ffffff;font-weight:700;'>No signals logged yet.</div>", unsafe_allow_html=True)
