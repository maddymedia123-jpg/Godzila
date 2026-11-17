@@ -2,8 +2,6 @@
import time
from typing import List
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from stream_manager import BinanceFuturesStreamManager, WATCHLIST_SYMBOLS

@@ -17,50 +15,27 @@ def format_dual_time(ts: float) -> str:
    return f"{dt_pkt} ({dt_utc})"


st.set_page_config(
    page_title="Institutional Order Flow & Student State-Locked Signals",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.set_page_config(page_title="Student Committed Signals & Coinglass Terminal", page_icon="⚡", layout="wide")

st.markdown(
    """
<style>
.stApp { background-color: #F1F5F9; color: #0F172A; }
.neutral-card {
    background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 14px 16px;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06); color: #0F172A;
}
.neutral-label { font-size: 0.74rem; font-weight: 600; text-transform: uppercase; color: #475569; margin-bottom: 4px; }
.preset-card {
    background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 18px;
    box-shadow: 0 2px 5px rgba(15, 23, 42, 0.06); color: #0F172A;
}
.price-box {
    background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;
    font-family: 'JetBrains Mono', monospace; font-size: 0.88rem; margin: 10px 0;
}
.neutral-card { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 14px 16px; color: #0F172A; }
.preset-card { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 18px; color: #0F172A; }
.price-box { background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 0.88rem; margin: 10px 0; }
.price-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #E2E8F0; }
.price-row:last-child { border-bottom: none; }
.badge-long { background-color: #DCFCE7; color: #166534; border: 1px solid #86EFAC; padding: 3px 10px; border-radius: 5px; font-weight: 800; font-family: monospace; }
.badge-short { background-color: #FEE2E2; color: #991B1B; border: 1px solid #FCA5A5; padding: 3px 10px; border-radius: 5px; font-weight: 800; font-family: monospace; }
.coinglass-banner {
    background: linear-gradient(135deg, #0F172A, #1E293B); color: #F8FAFC; border-radius: 10px; padding: 14px 18px;
    font-family: monospace; font-size: 0.85rem; margin-bottom: 16px; border: 1px solid #334155;
}
.warning-strip {
    background-color: #FEF3C7; border: 1px solid #FCD34D; color: #92400E; border-radius: 6px; padding: 8px 10px;
    font-size: 0.78rem; font-family: monospace; margin-top: 8px;
}
.coinglass-banner { background: linear-gradient(135deg, #0F172A, #1E293B); color: #F8FAFC; border-radius: 10px; padding: 14px 18px; font-family: monospace; font-size: 0.85rem; margin-bottom: 16px; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_stream_manager() -> BinanceFuturesStreamManager:
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")


@@ -73,175 +48,110 @@ def fmt_price(val: float) -> str:
    return f"${val:,.5f}"


def parse_zones(zones_str: str) -> List[float]:
    zones = []
    for token in zones_str.replace("\n", ",").split(","):
        token = token.strip()
        if token:
            try: zones.append(float(token))
            except ValueError: pass
    return sorted(zones)

# Session state for committed trades so they stay locked in memory without flickering
if "committed_trade" not in st.session_state:
    st.session_state.committed_trade = None
if "student_coin" not in st.session_state:
    st.session_state.student_coin = "BTCUSDT"
if "student_balance" not in st.session_state:
    st.session_state.student_balance = 100.0

now_ts = time.time()
top_left, top_mid, top_right = st.columns([2.2, 1.3, 1.3])
with top_left:
    st.markdown("### ⚡ Student State-Locked Signals & Coinglass Liquidation Terminal")
    st.caption(f"🕒 Server Clock: **{format_dual_time(now_ts)}**")
with top_mid:
    scanner_active = st.toggle("🟢 LIVE AUTO-SCANNER: ON", value=True)
with top_right:
    auto_refresh = st.toggle("⚡ Real-Time Stream (500ms)", value=True)

with st.sidebar:
    st.markdown("### 🛠️ Pro Terminal Settings")
    pro_symbol = st.selectbox("Pro Terminal Contract", options=WATCHLIST_SYMBOLS, index=0)
    c_bal, c_risk = st.columns(2)
    with c_bal: account_balance = st.number_input("Pro Equity ($)", min_value=100.0, value=25000.0, step=500.0)
    with c_risk: risk_pct = st.number_input("Pro Risk (%)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
    st.markdown("---")
    decay_lambda = st.slider("Depth Decay (λ)", 0.05, 1.00, 0.35, 0.05)
    imbalance_threshold = st.slider("Imbalance Trigger (|I_t|)", 0.15, 0.85, 0.40, 0.05)
    max_spread_bps = st.slider("Max Spread Friction (bps)", 0.5, 25.0, 5.0, 0.5)
    garch_k = st.slider("GARCH Stop Multiplier (k)", 0.5, 4.0, 1.5, 0.1)
    zones_input = st.text_area("Manual Pro S/R Levels", value="", height=70)
    structural_zones = parse_zones(zones_input)

if "student_coin" not in st.session_state: st.session_state.student_coin = "SOLUSDT"
if "student_balance" not in st.session_state: st.session_state.student_balance = 100.0
st.markdown("### ⚡ Student Committed Signals & Coinglass Liquidation Terminal")
st.caption(f"🕒 Server Clock: **{format_dual_time(now_ts)}**")

s_ctrl1, s_ctrl2 = st.columns([2.2, 1.0])
with s_ctrl1:
    selected_coin = st.segmented_control("Select Asset", options=WATCHLIST_SYMBOLS, default=st.session_state.student_coin)
    if selected_coin: st.session_state.student_coin = selected_coin
with s_ctrl2:
    st.session_state.student_balance = st.number_input("My Account Balance ($)", 50.0, 10_000_000.0, float(st.session_state.student_balance), 50.0)

manager.update_config(
    active_symbol=pro_symbol, scanner_active=scanner_active, account_balance=account_balance,
    student_balance=st.session_state.student_balance, risk_pct=risk_pct, decay_lambda=decay_lambda,
    imbalance_threshold=imbalance_threshold, max_spread_phi=max_spread_bps / 10000.0,
    garch_k=garch_k, structural_zones=structural_zones,
    active_symbol=st.session_state.student_coin, scanner_active=True, account_balance=25000.0,
    student_balance=st.session_state.student_balance, risk_pct=1.0, decay_lambda=0.35,
    imbalance_threshold=0.40, max_spread_phi=0.0005, garch_k=1.5, structural_zones=[]
)

tab_student, tab_pro = st.tabs(["🎯 Student State-Locked Signals (15m · 1H · 4H)", "🔬 Pro Institutional Order Book Terminal"])


# =============================================================================
# TAB 1: STUDENT STATE-LOCKED SIGNALS & COINGLASS BANNER
# =============================================================================
with tab_student:
    s_ctrl1, s_ctrl2 = st.columns([2.2, 1.0])
    with s_ctrl1:
        selected_coin = st.segmented_control("Select Asset", options=WATCHLIST_SYMBOLS, default=st.session_state.student_coin)
        if selected_coin: st.session_state.student_coin = selected_coin
    with s_ctrl2:
        st.session_state.student_balance = st.number_input("My Account Balance ($)", 50.0, 10_000_000.0, float(st.session_state.student_balance), 50.0)

    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_student_dashboard():
        snap = manager.get_snapshot(st.session_state.student_coin)
        metrics = snap.get("metrics", {})
        coin = snap["symbol"]
        liq = snap.get("liq_stats", {})

        # Coinglass Liquidation & Max Pain Banner
        long_c = fmt_price(liq.get("long_cluster", 0))
        short_c = fmt_price(liq.get("short_cluster", 0))
        long_u = liq.get("long_usd", 0)
        short_u = liq.get("short_usd", 0)
        oi_b = liq.get("oi_bias", "Neutral")

        banner_html = (
            f"<div class='coinglass-banner'>"
            f"<b>📊 COINGLASS LIQUIDATION & DERIVATIVES PULSE ({coin}):</b><br>"
            f"📉 Major Long Liquidation Cluster: <b style='color:#F87171;'>{long_c}</b> (${long_u:,.1f}M at risk) &nbsp;|&nbsp; "
            f"📈 Major Short Liquidation Cluster: <b style='color:#34D399;'>{short_c}</b> (${short_u:,.1f}M at risk)<br>"
            f"⚡ Open Interest Bias: <b>{oi_b}</b>"
            f"</div>"
        )
        st.markdown(banner_html, unsafe_allow_html=True)

        if not metrics:
            st.info(f"Calibrating state-locked models for {coin}...")
            return

        locked_sigs = snap.get("locked_signals", {})
        tf_slots = [("15m", "15m Scalp (20x · 0.5% Risk)"), ("1H", "1H Intraday (50x · 1.0% Risk)"), ("4H", "4H Swing (100x · 2.0% Risk)")]
        p_cols = st.columns(3)

        for col, (tf_key, tf_title) in zip(p_cols, tf_slots):
            sig = locked_sigs.get(tf_key)
            with col:
                if sig is None:
                    card_html = (
                        f"<div class='preset-card' style='border-top: 4px solid #64748B;'>"
                        f"<div style='font-weight:800; font-size:1.05rem; color:#334155;'>{tf_title}</div>"
                        f"<div style='font-size:0.8rem; color:#64748B; margin-top:6px;'>Scanning Order Book Confluence...</div>"
                        f"<div style='background:#F8FAFC; border:1px dashed #CBD5E1; border-radius:8px; padding:20px; text-align:center; color:#64748B; font-family:monospace; margin-top:15px;'>"
                        f"⏳ <b>STATE: WAITING FOR SETUP</b><br><span style='font-size:0.72rem;'>Slot locked until institutional gates align.</span>"
                        f"</div></div>"
                    )
                    st.markdown(card_html, unsafe_allow_html=True)
                    continue

                badge_cls = "badge-long" if sig.direction == "LONG" else "badge-short"
                accent = "#15803D" if sig.direction == "LONG" else "#B91C1C"

                card_html = (
                    f"<div class='preset-card' style='border-top: 4px solid {accent};'>"
                    f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                    f"<span style='font-weight:800; font-size:1.05rem; color:{accent};'>{tf_title}</span>"
                    f"<span class='{badge_cls}'>{sig.direction}</span>"
                    f"</div>"
                    f"<div style='font-size:0.75rem; color:#475569; font-family:monospace; margin-top:3px;'>Target ETA: {sig.eta_pkt_utc}</div>"
                    f"<div class='price-box'>"
                    f"<div class='price-row'><span>ENTRY PRICE:</span><b>{fmt_price(sig.entry_price)}</b></div>"
                    f"<div class='price-row'><span>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(sig.stop_loss)}</b></div>"
                    f"<div class='price-row'><span>TP 1:</span><b style='color:#15803D;'>{fmt_price(sig.tp1)}</b></div>"
                    f"<div class='price-row'><span>TP 2:</span><b style='color:#15803D;'>{fmt_price(sig.tp2)}</b></div>"
                    f"<div class='price-row'><span>TP 3:</span><b style='color:#15803D;'>{fmt_price(sig.tp3)}</b></div>"
                    f"</div>"
                    f"<div style='display:grid; grid-template-columns: 1fr 1fr; gap:8px; font-family:monospace; font-size:0.8rem;'>"
                    f"<div style='background:#F8FAFC; border:1px solid #E2E8F0; padding:8px; border-radius:6px;'>"
                    f"<span style='color:#475569; font-size:0.7rem;'>MARGIN ({sig.leverage}x)</span><br><b>${sig.margin_required_usd:,.2f}</b>"
                    f"</div>"
                    f"<div style='background:#F8FAFC; border:1px solid #E2E8F0; padding:8px; border-radius:6px;'>"
                    f"<span style='color:#475569; font-size:0.7rem;'>NOTIONAL SIZE</span><br><b>${sig.position_notional_usd:,.0f}</b>"
                    f"</div></div>"
                    f"<div class='warning-strip'><b>STATUS: {sig.status_note}</b><br>Win Prob: ~{sig.win_probability}%</div>"
                    f"</div>"
                )
                st.markdown(card_html, unsafe_allow_html=True)

        st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
        st.markdown("#### 📚 Diagnostic Trade Outcome Logbook & Mistake Audit")
        
        audit_feed = snap.get("audit_logbook", [])
        if audit_feed:
            audit_rows = []
            for item in audit_feed:
                audit_rows.append({
                    "Time (PKT)": format_dual_time(item["timestamp"]),
                    "Asset": item["symbol"],
                    "Timeframe": item["timeframe"],
                    "Side": item["direction"],
                    "Entry": fmt_price(item["entry"]),
                    "Outcome": item["exit_status"],
                    "Classification": item["classification"],
                })
            st.dataframe(pd.DataFrame(audit_rows), use_container_width=True, hide_index=True)

@st.fragment(run_every=1.0)
def render_live_board():
    snap = manager.get_snapshot(st.session_state.student_coin)
    liq = snap.get("liq_stats", {})
    candidates = snap.get("candidate_signals", [])

    # Coinglass Banner
    st.markdown(
        f"<div class='coinglass-banner'>"
        f"<b>📊 COINGLASS DERIVATIVES PULSE ({snap['symbol']}):</b><br>"
        f"📉 Long Liq Cluster: <b style='color:#F87171;'>{fmt_price(liq.get('long_cluster',0))}</b> (${liq.get('long_usd',0):,.1f}M) &nbsp;|&nbsp; "
        f"📈 Short Liq Cluster: <b style='color:#34D399;'>{fmt_price(liq.get('short_cluster',0))}</b> (${liq.get('short_usd',0):,.1f}M)<br>"
        f"⚡ OI Bias: <b>{liq.get('oi_bias','Neutral')}</b>"
        f"</div>",
        unsafe_allow_html=True,
    )

    col_comm, col_cand = st.columns(2)

    # Column 1: COMMITTED TRADE (Pinned in memory, no flickering)
    with col_comm:
        st.markdown("#### 📌 Pinned Committed Trade")
        if st.session_state.committed_trade is not None:
            t = st.session_state.committed_trade
            badge = "badge-long" if t.direction == "LONG" else "badge-short"
            accent = "#15803D" if t.direction == "LONG" else "#B91C1C"

            st.markdown(
                f"<div class='preset-card' style='border-top: 4px solid {accent};'>"
                f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<span style='font-weight:800; color:{accent};'>{t.symbol} · {t.timeframe}</span>"
                f"<span class='{badge}'>{t.direction}</span>"
                f"</div>"
                f"<div style='font-size:0.75rem; color:#475569; font-family:monospace; margin-top:4px;'>Target ETA: {t.eta_pkt_utc}</div>"
                f"<div class='price-box'>"
                f"<div class='price-row'><span>ENTRY:</span><b>{fmt_price(t.entry_price)}</b></div>"
                f"<div class='price-row'><span>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(t.stop_loss)}</b></div>"
                f"<div class='price-row'><span>TP 1:</span><b style='color:#15803D;'>{fmt_price(t.tp1)}</b></div>"
                f"<div class='price-row'><span>TP 2:</span><b style='color:#15803D;'>{fmt_price(t.tp2)}</b></div>"
                f"<div class='price-row'><span>TP 3:</span><b style='color:#15803D;'>{fmt_price(t.tp3)}</b></div>"
                f"</div>"
                f"<b>Margin Required ({t.leverage}x):</b> ${t.margin_required_usd:,.2f}<br>",
                unsafe_allow_html=True,
            )
            if st.button("❌ Close / Clear Committed Trade"):
                st.session_state.committed_trade = None
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("Closed trades will automatically populate here for student performance review.")

    render_student_dashboard()


# =============================================================================
# TAB 2: PRO INSTITUTIONAL TERMINAL
# =============================================================================
with tab_pro:
    @st.fragment(run_every=0.5 if auto_refresh else None)
    def render_pro_terminal():
        snap = manager.get_snapshot(pro_symbol)
        metrics = snap.get("metrics", {})
        if not metrics:
            st.info("Connecting to pro feeds...")
            return
        st.markdown(f"#### {pro_symbol} Institutional Microstructure Engine")
        st.metric("Mid-Price", fmt_price(metrics["mid_price"]), f"Imbalance: {metrics['imbalance_t']:+.3f}")

    render_pro_terminal()
            st.info("No trade committed yet. Review candidates on the right and click 'Commit' to lock one in.")

    # Column 2: READY CANDIDATES TO COMMIT
    with col_cand:
        st.markdown("#### 🔍 Ready Signal Candidate")
        if candidates:
            c = candidates[0]
            badge = "badge-long" if c.direction == "LONG" else "badge-short"
            accent = "#15803D" if c.direction == "LONG" else "#B91C1C"

            st.markdown(
                f"<div class='preset-card' style='border-top: 4px solid {accent};'>"
                f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<span style='font-weight:800; color:{accent};'>{c.symbol} · {c.timeframe}</span>"
                f"<span class='{badge}'>{c.direction}</span>"
                f"</div>"
                f"<div class='price-box'>"
                f"<div class='price-row'><span>ENTRY:</span><b>{fmt_price(c.entry_price)}</b></div>"
                f"<div class='price-row'><span>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(c.stop_loss)}</b></div>"
                f"<div class='price-row'><span>TP 1:</span><b style='color:#15803D;'>{fmt_price(c.tp1)}</b></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if st.button("📌 Commit to This Setup"):
                st.session_state.committed_trade = c
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("Scanning order books... A stable candidate will appear here shortly.")


render_live_board()
